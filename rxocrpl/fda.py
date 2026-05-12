import json
import re
from collections.abc import Mapping, Sequence
from json import dumps
from pathlib import Path

import requests

_LABELERS_PATH = Path(__file__).parent / "db" / "labelers.json"
PAGE_SIZE = 100

_NAME_SUFFIXES = ["pharmaceuticals usa", "pharmaceuticals north america", "pharmaceutical industries",
                  "pharmaceutical sciences", "consumer healthcare", "consumer products", "animal health", "health care",
                  "healthcare", "laboratories", "laboratory", "manufacturing", "industries", "international",
                  "incorporated", "corporation", "pharmaceuticals", "pharmaceutical", "biosciences", "therapeutics",
                  "biologics", "holdings", "products", "company", "limited", "pharma", "biotech", "group", "corp",
                  "labs", "ltd", "llc", "inc", "co", "us", "usa", "na", ]

_SUFFIX_RE = re.compile(r"(?:[\s,.\-&/]+(?:" + "|".join(re.escape(s) for s in _NAME_SUFFIXES) + r"))+\.?$",
                        re.IGNORECASE, )
_PAREN_RE = re.compile(r"\s*\([^)]*\)\s*")
_PUNCT_RE = re.compile(r"[,.\-&/]+")
_WS_RE = re.compile(r"\s+")

URL = "https://api.fda.gov/drug/ndc.json"
TIMEOUT = 1000


def infer_schema(obj):
      """
      Recursively infer a JSON-like schema from a Python object.
      """

      if isinstance(obj, Mapping):
            return {"type": "object", "properties": {key: infer_schema(value) for key, value in obj.items()}}

      elif isinstance(obj, Sequence) and not isinstance(obj, (str, bytes)):
            if not obj:
                  return {"type": "array", "items": "unknown"}

            # Infer schemas for all items
            item_schemas = [infer_schema(item) for item in obj]

            # Deduplicate equivalent schemas
            unique = []
            for schema in item_schemas:
                  if schema not in unique:
                        unique.append(schema)

            return {"type": "array", "items": unique[0] if len(unique) == 1 else unique}

      elif isinstance(obj, str):
            return {"type": "string"}

      elif isinstance(obj, bool):
            return {"type": "boolean"}

      elif isinstance(obj, int):
            return {"type": "integer"}

      elif isinstance(obj, float):
            return {"type": "number"}

      elif obj is None:
            return {"type": "null"}

      return {"type": type(obj).__name__}


def ndc_variants(ndc: str) -> list[str]:
      """
      Return all plausible 10-digit hyphenated NDC representations
      for querying the openFDA API.
      """
      if not isinstance(ndc, str):
            raise TypeError(f"ndc must be str, got {type(ndc).__name__}")

      digits = re.sub(r"\D", "", ndc)
      if not digits:
            raise ValueError(f"no digits found in {ndc!r}")

      variants = []

      if len(digits) == 10:
            variants.append(f"{digits[:4]}-{digits[4:8]}-{digits[8:]}")  # 4-4-2
            variants.append(f"{digits[:5]}-{digits[5:8]}-{digits[8:]}")  # 5-3-2
            variants.append(f"{digits[:5]}-{digits[5:9]}-{digits[9:]}")  # 5-4-1

      elif len(digits) == 11:
            p1, p2, p3 = digits[:5], digits[5:9], digits[9:]
            if p1.startswith("0"):
                  variants.append(f"{p1[1:]}-{p2}-{p3}")
            if p2.startswith("0"):
                  variants.append(f"{p1}-{p2[1:]}-{p3}")
            if p3.startswith("0"):
                  variants.append(f"{p1}-{p2}-{p3[1:]}")
            if not variants:
                  variants.append(f"{p1}-{p2}-{p3}")
      else:
            raise ValueError(f"expected 10 or 11 digits after stripping separators, "
                             f"got {len(digits)} from {ndc!r}")

      seen = set()
      return [v for v in variants if not (v in seen or seen.add(v))]


def get_queries_result(queries: list[str]):
      """
      Try each query in order. Return the first non-empty result list,
      or None if nothing matched.
      """
      for q in queries:
            try:
                  r = requests.get(URL, params={"search": q, "limit": 5}, timeout=TIMEOUT)
            except requests.RequestException as e:
                  print(f"request failed for query {q!r}: {e}")
                  continue

            if r.status_code == 404:
                  # openFDA returns 404 with an error payload when there are no matches
                  continue
            if r.status_code != 200:
                  print(f"unexpected status {r.status_code} for query {q!r}")
                  continue

            try:
                  payload = r.json()
            except ValueError:
                  continue

            results = payload.get("results") or []
            if not results:
                  continue

            return [{"labeler_name": result.get("labeler_name"), "brand_name": result.get("brand_name"),
                     "generic_name": result.get("generic_name"), "product_ndc": result.get("product_ndc"),
                     "active_ingredients": result.get("active_ingredients"), "dosage_form": result.get("dosage_form"),
                     "route": result.get("route"), "packaging": result.get("packaging"), } for result in results]

      return None


def _escape_lucene(s: str) -> str:
      """Escape characters that have special meaning in Lucene query syntax."""
      # Backslash first, then everything else
      s = s.replace("\\", "\\\\")
      for ch in '+-&|!(){}[]^"~*?:/':
            s = s.replace(ch, f"\\{ch}")
      return s


def lookup_ndc_package(package_ndc: str):
      package_ndc = package_ndc.strip()
      variants = ndc_variants(package_ndc)

      # Flatten: try product_ndc match first per variant, then packaging match.
      # Was: list of tuples (which broke requests).
      queries = []
      for v in variants:
            queries.append(f'package_ndc:"{v}"')
            queries.append(f'packaging.package_ndc:"{v}"')

      return get_queries_result(queries)


def lookup_generic_name(generic_name: str, dosage_form: str = None, route: str = None, brand_name: str = None, ):
      generic_name = generic_name.strip()
      gname_esc = _escape_lucene(generic_name)

      parts = [f'generic_name:"{gname_esc}"']
      if route:
            parts.append(f'route:"{_escape_lucene(route)}"')
      if dosage_form:
            parts.append(f'dosage_form:"{_escape_lucene(dosage_form)}"')
      if brand_name:
            parts.append(f'brand_name:"{_escape_lucene(brand_name)}"')

      queries = [" AND ".join(parts)]
      results = get_queries_result(queries)
      if not results:
            return results

      key = generic_name.lower()

      def _name_str(r):
            # generic_name can come back as a string or (rarely) a list
            n = r.get("generic_name")
            if isinstance(n, list):
                  n = n[0] if n else ""
            return (n or "").lower()

      def _rank(r):
            name = _name_str(r)
            if name == key: return 0
            if name.startswith(key): return 1
            return 2

      results.sort(key=_rank)
      return results


def clean_labeler_name(name: str) -> str:
      """Reduce a labeler name to a fuzzy-match-friendly base form.

      Strips parentheticals, trailing corporate suffixes (Inc, LLC, Pharmaceuticals, etc.),
      collapses punctuation/whitespace. Returns the original (whitespace-normalized) name
      if cleaning would produce an empty string.
      """
      original = _WS_RE.sub(" ", name).strip()
      cleaned = _PAREN_RE.sub(" ", original)
      # Repeatedly strip suffixes since names often stack them ("Acme Pharmaceuticals, Inc.")
      prev = None
      while prev != cleaned:
            prev = cleaned
            cleaned = _SUFFIX_RE.sub("", cleaned).strip()
      cleaned = _PUNCT_RE.sub(" ", cleaned)
      cleaned = _WS_RE.sub(" ", cleaned).strip()
      return cleaned or original


def get_labeler_directory():
      """Query for all labelers and return a dict mapping labeler_code to
      {"name": cleaned_name, "full_name": original_name, "directory": labeler_directory}."""
      try:
            with open(_LABELERS_PATH) as f:
                  return json.load(f)
      except (FileNotFoundError, json.JSONDecodeError):
            pass

      labelers: dict[str, dict] = {}
      skip = 0
      while True:
            r = requests.get(URL, params={"search": "_exists_:labeler_name", "limit": PAGE_SIZE, "skip": skip},
                             timeout=TIMEOUT, )
            r.raise_for_status()
            payload = r.json()
            results = payload.get("results") or []
            if not results:
                  break
            for result in results:
                  name = result.get("labeler_name")
                  product_ndc = result.get("product_ndc", "")
                  code = product_ndc.split("-", 1)[0] if product_ndc else ""
                  if not (name and code):
                        continue
                  if code not in labelers:
                        labelers[code] = {"name": clean_labeler_name(name), "full_name": name,
                                          "directory": result.get("labeler_directory"), }
            total = payload.get("meta", {}).get("results", {}).get("total", 0)
            skip += PAGE_SIZE
            if skip >= total or skip >= 25000:
                  break

      if not labelers:
            raise ValueError("Labeler directory API returned no results")

      labelers = dict(sorted(labelers.items()))

      _LABELERS_PATH.parent.mkdir(parents=True, exist_ok=True)
      tmp = _LABELERS_PATH.with_suffix(".json.tmp")
      with open(tmp, "w") as f:
            json.dump(labelers, f, indent=4)
      tmp.replace(_LABELERS_PATH)

      return labelers


if __name__ == "__main__":
      get_labeler_directory()

      print("* R1 ****" * 5)
      r = lookup_ndc_package("0338-0049-02")
      print(dumps(r, indent=4))
      print(dumps(infer_schema(r), indent=4, skipkeys=True))

      print("* R2 ****" * 5)
      r2 = lookup_generic_name("phenylephrine", dosage_form="INJECTION")
      if r2: print(dumps(r2, indent=4))
