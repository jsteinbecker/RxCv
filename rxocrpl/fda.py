import json
import re
from collections.abc import Mapping, Sequence
from json import dumps
from pathlib import Path

import pandas as pd
import requests

__all__ = ["infer_schema", "lookup_ndc_package", "lookup_generic_name", "get_labeler_directory"]

try:
      from .rxnorm import get_all_rxcui
except ImportError:
      from rxnorm import get_all_rxcui


def _dotted(suffix: str) -> str:
      """Build a regex that matches a suffix with optional periods between letters.

      'llc' -> 'l\\.?l\\.?c'  matches 'llc', 'l.l.c', 'l.l.c.', etc.
      Multi-word suffixes get each word built this way.
      """
      words = []
      for word in suffix.split():
            # Optional period+optional space between every pair of letters
            chars = [re.escape(c) for c in word]
            words.append(r"\.?\s?".join(chars))
      return r"\s+".join(words)


_LABELERS_PATH = Path(__file__).parent / "db" / "labelers.json"
_PAGE_SIZE = 100

_NAME_SUFFIXES = ["pharmaceuticals usa", "pharmaceuticals north america", "pharmaceuticals america",
                  "pharmaceutical industries", "pharmaceutical sciences", "consumer healthcare", "consumer products",
                  "animal health", "health care", "medicines", "healthcare", "laboratories", "laboratory",
                  "manufacturing", "industries", "international", "incorporated", "of new york", "corporation",
                  "pharmaceuticals", "pharmaceutical", "biosciences", "therapeutics", "biologics", "holdings",
                  "products", "company", "limited", "pharma", "biotech", "group", "corp", "labs", "ltd", "llc", "inc",
                  "co", "us", "lp", "usa", "na", "srl", "liability", "pvt", "and", "a subsidiary of Pfizer", "dba PAI",
                  "solutions"]

_SUFFIX_ALT = "|".join(_dotted(s) for s in _NAME_SUFFIXES)

_SUFFIX_RE = re.compile(r"(?:[\s,.\-&/]+(?:" + _SUFFIX_ALT + r"))+\.?$", re.IGNORECASE, )
_DIV_RE = re.compile(r"[\s,]+div(?:ision)?\.?(?:\s+of)?\s+.+$", re.IGNORECASE, )
_PAREN_RE = re.compile(r"\s*\([^)]*\)\s*")
_PUNCT_RE = re.compile(r"[,.\-&/]+")
_WS_RE = re.compile(r"\s+")
_JSONATA_PTYPE_EXTRACTOR = r"$ ~> |**[description]|{'product_type': $reverse($match(description, /\b[A-Z\s,\-]{3,}/).match)[0]}|"


_URL = "https://api.fda.gov/drug/ndc.json"
_TIMEOUT = 10


def infer_schema(obj):
      """
      Recursively infer a JSON-like schema from a Python object.
      """

      if isinstance(obj, Mapping):
            return {"type": "object", "properties": {key: infer_schema(value) for key, value in obj.items()}}

      elif isinstance(obj, Sequence) and not isinstance(obj, (str, bytes)):
            if not obj:
                  return {"type": "array", "items": "unknown"}

            item_schemas = [infer_schema(item) for item in obj]

            # Deduplicate
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


def get_queries_result(queries: list[str], fetch_rxcui: bool = False):
      """
      Try each query in order. Return the first non-empty result list,
      or None if nothing matched.
      """
      for q in queries:
            try:
                  r = requests.get(_URL, params={"search": q, "limit": 100}, timeout=_TIMEOUT)
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

            return [{
                  "labeler_name": result.get("labeler_name"),
                  "brand_name": result.get("brand_name"),
                  "generic_name": result.get("generic_name"),
                  "product_ndc": result.get("product_ndc"),
                  "active_ingredients": result.get("active_ingredients"),
                  "dosage_form": result.get("dosage_form"),
                  "route": result.get("route"),
                  "packaging": result.get("packaging"),
                  "rxcui": get_all_rxcui(result.get("product_ndc"))
                  if fetch_rxcui else None
                     } for result in results]

      return None


def _escape_lucene(s: str) -> str:
      """Escape characters that have special meaning in Lucene query syntax."""
      # Backslash first, then everything else
      s = s.replace("\\", "\\\\")
      for ch in '+-&|!(){}[]^"~*?:/':
            s = s.replace(ch, f"\\{ch}")
      return s


def _norm(s: str) -> str:
      """Lowercase/ collapse whitespace for loose name comparison."""
      return _WS_RE.sub(" ", (s or "").lower()).strip()


def _has_active_ingredient(result: dict, target: str) -> bool:
      """True if `target` appears as an active ingredient name (substring match,
      case-insensitive). Substring rather than equality because openFDA names
      include salts/forms like 'SODIUM CHLORIDE' vs 'sodium chloride 0.9%'."""
      target = _norm(target)
      if not target:
            return False
      for ai in result.get("active_ingredients") or []:
            name = _norm(ai.get("name") if isinstance(ai, dict) else ai)
            if target in name:
                  return True
      return False


def lookup_ndc_package(package_ndc: str, fetch_rxcui: bool = False):
      package_ndc = package_ndc.strip()
      variants = ndc_variants(package_ndc)

      # Flatten: try product_ndc match first per variant, then packaging match.
      # Was: list of tuples (which broke requests).
      queries = []
      for v in variants:
            queries.append(f'package_ndc:"{v}"')
            queries.append(f'packaging.package_ndc:"{v}"')

      return get_queries_result(queries, fetch_rxcui=fetch_rxcui)


def lookup_generic_name(generic_name: str = None, dosage_form: str = None,
                        route: str = None, brand_name: str = None,
                        require_active: bool = False, ingredient_names: Sequence[str] = None,
                        max_active_ingredients: int = None,
                        labelers_cleanup: bool = False, fetch_rxcui: bool = False):
      def _build_query(generic=None, brand=None, ingredients=None, ingredient_wildcard=False, ):
            """Assemble a Lucene query from optional fields. route/dosage_form
            come from the enclosing scope since they never change across retries."""
            parts = []

            if generic:
                  parts.append(f'generic_name:"{_escape_lucene(generic.strip())}"')

            if brand:
                  parts.append(f'brand_name:"{_escape_lucene(brand.strip())}"')

            if route:
                  parts.append(f'route:"{_escape_lucene(route.strip())}"')

            if dosage_form:
                  parts.append(f'dosage_form:"{_escape_lucene(dosage_form.strip())}"')

            if ingredients:
                  for ing in ingredients:
                        ing = ing.strip()
                        if not ing:
                              continue
                        if ingredient_wildcard:
                              # Wildcard queries can't be quoted in Lucene
                              parts.append(f"active_ingredients.name:{_escape_lucene(ing)}*")
                        else:
                              parts.append(f'active_ingredients.name:"{_escape_lucene(ing)}"')

            return " AND ".join(parts) if parts else None

      def _run(query):
            if not query:
                  return []
            return get_queries_result([query], fetch_rxcui=fetch_rxcui) or []

      def _split_combo(name):
            """Split 'ampicillin and sulbactam' -> ['ampicillin', 'sulbactam'].
            Returns None if there's no ' and ' to split on."""
            if not name or " and " not in name.lower():
                  return None
            lowered = name.lower()
            idx = 0
            parts = []
            while True:
                  hit = lowered.find(" and ", idx)
                  if hit == -1:
                        parts.append(name[idx:].strip())
                        break
                  parts.append(name[idx:hit].strip())
                  idx = hit + len(" and ")
            return [p for p in parts if p]

      matched_via = "generic_name" if generic_name else ("brand_name" if brand_name else "other")

      results = _run(_build_query(generic=generic_name, brand=brand_name, ingredients=ingredient_names, ))

      if not results and generic_name:
            components = _split_combo(generic_name)
            if components and len(components) > 1:
                  # First try exact phrases per component
                  results = _run(_build_query(brand=brand_name, ingredients=components, ingredient_wildcard=False, ))
                  if results:
                        matched_via = "generic_name_split"
                  else:
                        # Wildcard fallback catches salt-suffixed forms
                        results = _run(
                              _build_query(brand=brand_name, ingredients=components, ingredient_wildcard=True, ))
                        if results:
                              matched_via = "generic_name_split_wildcard"

      if not results and generic_name and not brand_name:
            results = _run(_build_query(brand=generic_name, ingredients=ingredient_names, ))
            if results:
                  matched_via = "generic_as_brand"

      if not results:
            if require_active:
                  return None
            return results

      # ---- Stage 4: brand_name -> generic enrichment ----
      # If the user gave a brand name and it matched, pull the generic(s) off
      # the first hit and re-query with both brand AND generic to narrow.
      if brand_name and matched_via == "brand_name":
            first = results[0]
            generic_field = first.get("generic_name")
            if isinstance(generic_field, list):
                  discovered_generics = [g for g in generic_field if g]
            elif generic_field:
                  discovered_generics = [generic_field]
            else:
                  discovered_generics = []

            if discovered_generics:
                  # AND every discovered generic together with the brand
                  narrowed_parts = [f'brand_name:"{_escape_lucene(brand_name.strip())}"']
                  for g in discovered_generics:
                        narrowed_parts.append(f'generic_name:"{_escape_lucene(g.strip())}"')
                  if route:
                        narrowed_parts.append(f'route:"{_escape_lucene(route.strip())}"')
                  if dosage_form:
                        narrowed_parts.append(f'dosage_form:"{_escape_lucene(dosage_form.strip())}"')

                  narrowed = get_queries_result([" AND ".join(narrowed_parts)]) or []
                  if narrowed:
                        results = narrowed
                        matched_via = "brand_name_narrowed"

      # ---- require_active filter ----
      if require_active:
            if not generic_name:
                  raise ValueError("require_active=True requires generic_name.")
            results = [r for r in results if _has_active_ingredient(r, generic_name)]
            if not results:
                  return None

      # ---- max_active_ingredients filter ----
      if max_active_ingredients is not None:
            def _count_active(r):
                  ai = r.get("active_ingredients") or []
                  return len(ai) if isinstance(ai, list) else 0

            results = [r for r in results if _count_active(r) <= max_active_ingredients]
            if not results:
                  return None

      # ---- Ranking ----
      rank_key = (generic_name or brand_name or "").lower()

      def _field_str(r, field):
            n = r.get(field)
            if isinstance(n, list):
                  n = n[0] if n else ""
            return (n or "").lower()

      def _rank(r):
            generic = _field_str(r, "generic_name")
            brand = _field_str(r, "brand_name")

            if generic_name:
                  if generic == rank_key:
                        return 0
                  if generic.startswith(rank_key):
                        return 1

            if brand_name:
                  if brand == rank_key:
                        return 0
                  if brand.startswith(rank_key):
                        return 1

            return 2

      results.sort(key=_rank)

      # Tag each result with how it was found
      for r in results:
            r["_matched_via"] = matched_via

      if labelers_cleanup:
            df = pd.DataFrame(results)
            df['labeler_name'] = df['labeler_name'].apply(clean_labeler_name)
            results = df.to_dict(orient="records")
            del df

      return results


def clean_labeler_name(name: str) -> str:
      """Reduce a labeler name to a fuzzy-match-friendly base form."""
      original = _WS_RE.sub(" ", name).strip()
      cleaned = _PAREN_RE.sub(" ", original)
      cleaned = _DIV_RE.sub("", cleaned).strip()
      # Repeatedly strip suffixes since names often stack them
      prev = None
      while prev != cleaned:
            prev = cleaned
            cleaned = _SUFFIX_RE.sub("", cleaned).strip()
      cleaned = _PUNCT_RE.sub(" ", cleaned)
      cleaned = _WS_RE.sub(" ", cleaned).strip()
      return cleaned or original


def get_labeler_directory(reclean: bool = False):
      """Query for all labelers and return a dict mapping labeler_code to
      {"name": cleaned_name, "full_name": original_name, "directory": labeler_directory}.

      If reclean=True and the cache exists, re-run clean_labeler_name() on every
      entry's full_name and rewrite the file. Avoids refetching from openFDA.
      """
      try:
            with open(_LABELERS_PATH) as f:
                  labelers = json.load(f)
            if reclean:
                  changed = 0
                  for entry in labelers.values():
                        full = entry.get("full_name")
                        if not full:
                              continue
                        new_name = clean_labeler_name(full)
                        if new_name != entry.get("name"):
                              entry["name"] = new_name
                              changed += 1
                  if changed:
                        _write_labelers(labelers)
                  print(f"recleaned {changed} of {len(labelers)} labeler names")
            return labelers
      except (FileNotFoundError, json.JSONDecodeError):
            pass

      labelers: dict[str, dict] = {}
      skip = 0
      while True:
            r = requests.get(_URL, params={"search": "_exists_:labeler_name", "limit": _PAGE_SIZE, "skip": skip},
                             timeout=_TIMEOUT, )
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
            skip += _PAGE_SIZE
            if skip >= total or skip >= 25000:
                  break

      if not labelers:
            raise ValueError("Labeler directory API returned no results")

      labelers = dict(sorted(labelers.items()))
      _write_labelers(labelers)
      return labelers


def _write_labelers(labelers: dict) -> None:
      """Atomic write of the labelers cache."""
      _LABELERS_PATH.parent.mkdir(parents=True, exist_ok=True)
      tmp = _LABELERS_PATH.with_suffix(".json.tmp")
      with open(tmp, "w") as f:
            json.dump(labelers, f, indent=4)
      tmp.replace(_LABELERS_PATH)


if __name__ == "__main__":
      get_labeler_directory(reclean=True)

      print("* R1 ****" * 5)
      r = lookup_ndc_package("0338-0049-02")
      print(dumps(r, indent=4))
      print(dumps(infer_schema(r), indent=4, skipkeys=True))

      print("* R2 ****" * 5)
      r2 = lookup_generic_name("phenylephrine", dosage_form="INJECTION")
      if r2:
            print(dumps(r2, indent=4))

      print("* R3 ****" * 5)
      r3 = lookup_generic_name(ingredient_names=["sodium chloride"], dosage_form="INJECTION", max_active_ingredients=1)
      if r3:
            print(dumps(r3, indent=4))

      print("* R4 ****" * 5)
      r4 = lookup_generic_name(ingredient_names=["sodium chloride"], dosage_form="INJECTION", max_active_ingredients=2)
      if r4:
            print(dumps(r4, indent=4))
            print(dumps(infer_schema(r4), indent=4, skipkeys=True))
