import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, asdict
from json import dumps
from pathlib import Path
from typing import Literal

import pandas as pd
import requests

__all__ = [
      "infer_schema",
      "lookup_ndc_package",
      "lookup_generic_name",
      "get_labeler_directory",
]

try:
      from .rxnorm import get_all_rxcui
except ImportError:
      from rxocrpl.rxnorm import get_all_rxcui

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

MatchedVia = Literal[
      "generic_name",
      "generic_name_split",
      "generic_name_split_wildcard",
      "generic_as_brand",
      "brand_name",
      "brand_name_split_wildcard",
      "brand_name_narrowed",
      "other",
]


@dataclass
class PackageInfo(dict):
      package_count: int
      package_size: str
      package_type: str

      def __str__ (self) -> str:
            return f"{self.package_count} {self.package_size} in {self.package_type}"


@dataclass
class ActiveIngredient:
      name: str
      strength: str | None = None


@dataclass
class NdcPackaging:
      description: str
      package_ndc: str | None = None
      marketing_start_date: str | None = None
      sample: bool | None = None


@dataclass
class NdcProduct:
      labeler_name: str | None
      brand_name: str | None
      generic_name: str | None
      product_ndc: str | None
      active_ingredients: list[ActiveIngredient]
      dosage_form: str | None
      route: list[str] | None
      packaging: list[NdcPackaging]
      rxcui: list[str] | None = None
      # enriched fields (optional, added by lookup functions)
      _matched_via: MatchedVia | None = None
      product_type: str | None = None
      packaged_as: PackageInfo | None = None
      package_type: str | None = None
      package_size: str | None = None


@dataclass
class LabelerEntry:
      name: str  # cleaned
      full_name: str  # original
      directory: str | None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LABELERS_PATH = Path(__file__).parent / "db" / "labelers.json"
_PAGE_SIZE = 100
_URL = "https://api.fda.gov/drug/ndc.json"
_TIMEOUT = 10

_NAME_SUFFIXES = [
      "pharmaceuticals usa",
      "pharmaceuticals north america",
      "pharmaceuticals america",
      "pharmaceutical industries",
      "pharmaceutical sciences",
      "consumer healthcare",
      "consumer products",
      "animal health",
      "health care",
      "medicines",
      "healthcare",
      "laboratories",
      "laboratory",
      "manufacturing",
      "industries",
      "international",
      "incorporated",
      "of new york",
      "corporation",
      "pharmaceuticals",
      "pharmaceutical",
      "biosciences",
      "therapeutics",
      "biologics",
      "holdings",
      "products",
      "company",
      "limited",
      "pharma",
      "biotech",
      "group",
      "corp",
      "labs",
      "ltd",
      "llc",
      "inc",
      "co",
      "us",
      "lp",
      "usa",
      "na",
      "srl",
      "liability",
      "pvt",
      "and",
      "a subsidiary of Pfizer",
      "dba PAI",
      "solutions",
]

PACKAGE_PATTERN = re.compile(
      r"/\s*"
      r"(?P<package_size>\d+(?:\.\d+)?\s*[A-Za-zµμ]+)"
      r"\s+in\s+"
      r"(?P<package_count>\d+)\s+"
      r"(?P<package_type>[A-Za-z][A-Za-z,\- ]*?)"
      r"(?=\s*(?:\(|$))",
      re.IGNORECASE,
)


def _dotted (suffix: str) -> str:
      words = [r"\.?\s?".join(re.escape(c) for c in w) for w in suffix.split()]
      return r"\s+".join(words)


_SUFFIX_RE = re.compile(
      r"(?:[\s,.\-&/]+(?:" + "|".join(_dotted(s) for s in _NAME_SUFFIXES) + r"))+\.?$",
      re.IGNORECASE,
)
_DIV_RE = re.compile(r"[\s,]+div(?:ision)?\.?(?:\s+of)?\s+.+$", re.IGNORECASE)
_PAREN_RE = re.compile(r"\s*\([^)]*\)\s*")
_PUNCT_RE = re.compile(r"[,.\-&/]+")
_WS_RE = re.compile(r"\s+")
_CAPS_RE = re.compile(r"\b[A-Z][A-Z,\-]*(?:[ ][A-Z][A-Z,\-]*)*\b")


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def extract_package_info (value: str) -> PackageInfo | None:
      m = PACKAGE_PATTERN.search(value)
      if not m:
            return None
      return PackageInfo(
            package_count=int(m.group("package_count")),
            package_size=" ".join(m.group("package_size").split()),
            package_type=" ".join(m.group("package_type").split()),
      )


def _parse_active_ingredients (raw: list[dict] | None) -> list[ActiveIngredient]:
      if not raw:
            return []
      return [
            ActiveIngredient(name=ai.get("name", ""), strength=ai.get("strength"))
            for ai in raw
      ]


def _parse_packaging (raw: list[dict] | None) -> list[NdcPackaging]:
      if not raw:
            return []
      return [
            NdcPackaging(
                  description=p.get("description", ""),
                  package_ndc=p.get("package_ndc"),
                  marketing_start_date=p.get("marketing_start_date"),
                  sample=p.get("sample"),
            )
            for p in raw
      ]


def parse_product (res: dict, fetch_rxcui: bool = False) -> NdcProduct:
      # Extract RXCUI strings from the nested dictionary structure
      rxcui_result = None
      if fetch_rxcui and res.get("product_ndc"):
            rxcui_dict = get_all_rxcui(res.get("product_ndc", ""))
            if rxcui_dict:
                  rxcui_result = list(
                        set([ndc.rxcui for ndc_list in rxcui_dict.values() for ndc in ndc_list])
                  )

      return NdcProduct(
            labeler_name=res.get("labeler_name"),
            brand_name=res.get("brand_name"),
            generic_name=res.get("generic_name"),
            product_ndc=res.get("product_ndc"),
            active_ingredients=_parse_active_ingredients(res.get("active_ingredients")),
            dosage_form=res.get("dosage_form"),
            route=res.get("route"),
            packaging=_parse_packaging(res.get("packaging")),
            rxcui=rxcui_result,
      )


# ---------------------------------------------------------------------------
# Schema inference
# ---------------------------------------------------------------------------


def infer_schema (obj):
      if isinstance(obj, Mapping):
            return {
                  "type": "object",
                  "properties": {k: infer_schema(v) for k, v in obj.items()},
            }
      if isinstance(obj, Sequence) and not isinstance(obj, (str, bytes)):
            if not obj:
                  return {"type": "array", "items": "unknown"}
            seen: list = []
            for s in [infer_schema(i) for i in obj]:
                  if s not in seen:
                        seen.append(s)
            return {"type": "array", "items": seen[0] if len(seen) == 1 else seen}
      type_map: dict[type, str] = {
            str: "string",
            bool: "boolean",
            int: "integer",
            float: "number",
            type(None): "null",
      }
      obj_type = type(obj)
      return {"type": type_map.get(obj_type, obj_type.__name__)}


# ---------------------------------------------------------------------------
# NDC helpers
# ---------------------------------------------------------------------------


def ndc_variants (ndc: str) -> list[str]:
      """Return all plausible hyphenated NDC representations."""
      if not isinstance(ndc, str):
            raise TypeError(f"ndc must be str, got {type(ndc).__name__}")

      variants: list[str] = []
      if "-" in ndc:
            variants.append(ndc)

      digits = re.sub(r"\D", "", ndc)
      if not digits:
            if variants:
                  return variants
            raise ValueError(f"no digits found in {ndc!r}")

      if len(digits) == 10:
            variants.extend(
                  [
                        f"{digits[:4]}-{digits[4:8]}-{digits[8:]}",  # 4-4-2
                        f"{digits[:5]}-{digits[5:8]}-{digits[8:]}",  # 5-3-2
                        f"{digits[:5]}-{digits[5:9]}-{digits[9:]}",  # 5-4-1
                  ]
            )
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
      elif len(digits) == 8:
            variants.extend(
                  [
                        f"{digits[:5]}-{digits[5:]}",  # 5-3
                        f"{digits[:4]}-{digits[4:]}",  # 4-4
                  ]
            )
      elif len(digits) == 9:
            variants.append(f"{digits[:5]}-{digits[5:]}")  # 5-4

      if not variants:
            variants.append(ndc)

      seen: set[str] = set()
      return [v for v in variants if not (v in seen or seen.add(v))]


def _escape_lucene (s: str) -> str:
      s = s.replace("\\", "\\\\")
      for ch in '+-&|!(){}[]^"~*?:/':
            s = s.replace(ch, f"\\{ch}")
      return s


def _norm (s: str) -> str:
      return _WS_RE.sub(" ", (s or "").lower()).strip()


# ---------------------------------------------------------------------------
# openFDA fetch
# ---------------------------------------------------------------------------


def _fetch (query: str, fetch_rxcui: bool = False, try_unfinished: bool = False) -> list[NdcProduct] | None:
      def _execute (q):
            try:
                  print(f"fetching {q!r} from openFDA...")
                  r = requests.get(_URL, params={"search": q, "limit": 100}, timeout=_TIMEOUT)
                  if r.status_code == 200:
                        return r.json().get("results") or []
                  if r.status_code == 404:
                        return []
                  print(f"unexpected status {r.status_code} for {q!r}")
                  return None
            except requests.RequestException as e:
                  print(f"request failed for {q!r}: {e}")
                  return None

      results = _execute(query)

      if results is not None and not results and try_unfinished and "finished:" not in query:
            # If it runs through all its contingencies and still doesn't have a result
            # for an NDC, add the finished:false to the query and attempt with that as well
            results = _execute(f"{query} AND finished:false")

      if not results:
            return None
      return [parse_product(res, fetch_rxcui=fetch_rxcui) for res in results] or None


def get_queries_result (
          queries: list[str], fetch_rxcui: bool = False
) -> list[NdcProduct] | None:
      for q in queries:
            result = _fetch(q, fetch_rxcui=fetch_rxcui)
            if result:
                  return result
      return None


# ---------------------------------------------------------------------------
# Public lookup functions
# ---------------------------------------------------------------------------


def lookup_ndc_package (
          package_ndc: str, fetch_rxcui: bool = False
) -> list[NdcProduct] | None:
      variants = ndc_variants(package_ndc.strip())
      fields = ["package_ndc", "packaging.package_ndc", "product_ndc"]
      queries = [f'{field}:"{v}"' for v in variants for field in fields]
      return get_queries_result(queries, fetch_rxcui=fetch_rxcui)


def lookup_generic_name (
          generic_name: str | None = None,
          dosage_form: str | None = None,
          route: str | None = None,
          brand_name: str | None = None,
          ingredient_names: Sequence[str] | None = None,
          max_active_ingredients: int | None = None,
          extract_product_type: bool = True,
          extract_package: bool = True,
          labelers_cleanup: bool = False,
          fetch_rxcui: bool = False,
) -> list[NdcProduct] | None:
      def _build_query (
                generic=None, brand=None, ingredients=None, ingredient_wildcard=False
      ):
            pts = []
            if generic:
                  pts.append(f'generic_name:"{_escape_lucene(generic.strip())}"')
            if brand:
                  pts.append(f'brand_name:"{_escape_lucene(brand.strip())}"')
            if route:
                  pts.append(f'route:"{_escape_lucene(route.strip())}"')
            if dosage_form:
                  pts.append(f'dosage_form:"{_escape_lucene(dosage_form.strip())}"')
            for ing in ingredients or []:
                  ing = ing.strip()
                  if ing:
                        pts.append(
                              f"active_ingredients.name:{_escape_lucene(ing)}*"
                              if ingredient_wildcard
                              else f'active_ingredients.name:"{_escape_lucene(ing)}"'
                        )
            return " AND ".join(pts) if pts else None

      def _run (query) -> list[NdcProduct]:
            return (
                  get_queries_result([query], fetch_rxcui=fetch_rxcui) or [] if query else []
            )

      def _split_combo (name: str) -> list[str] | None:
            if not name or " and " not in name.lower():
                  return None
            pts, idx = [], 0
            while True:
                  hit = name.lower().find(" and ", idx)
                  if hit == -1:
                        pts.append(name[idx:].strip())
                        break
                  pts.append(name[idx:hit].strip())
                  idx = hit + 5
            return [p for p in pts if p] or None

      def _get_product_type (product: NdcProduct) -> str | None:
            if product.packaging:
                  types = [
                        _CAPS_RE.findall(p.description)[0]
                        for p in product.packaging
                        if _CAPS_RE.search(p.description)
                  ]
                  return ", ".join(set(types)) if types else None
            return None

      matched_via: MatchedVia = ("generic_name" if generic_name else ("brand_name" if brand_name else "other"))

      results = _run(
            _build_query(
                  generic=generic_name, brand=brand_name, ingredients=ingredient_names
            )
      )

      if not results and generic_name:
            components = _split_combo(generic_name)
            if components and len(components) > 1:
                  results = _run(_build_query(brand=brand_name, ingredients=components))
                  if results:
                        matched_via = "generic_name_split"
                  else:
                        results = _run(
                              _build_query(
                                    brand=brand_name,
                                    ingredients=components,
                                    ingredient_wildcard=True,
                              )
                        )
                        if results:
                              matched_via = "generic_name_split_wildcard"

      if not results and generic_name and not brand_name:
            results = _run(_build_query(brand=generic_name, ingredients=ingredient_names))
            if results:
                  matched_via = "generic_as_brand"

      if brand_name and matched_via == "brand_name":
            if not results:
                  components = _split_combo(brand_name)
                  if components and len(components) > 1:
                        results = _run(
                              _build_query(ingredients=components, ingredient_wildcard=True)
                        )
                        if results:
                              matched_via = "brand_name_split_wildcard"
            if not results:
                  raise ValueError(f"no results found for brand_name {brand_name!r}")

            first = results[0]
            raw_generic = first.generic_name
            discovered = (
                  [g for g in raw_generic if g]
                  if isinstance(raw_generic, list)
                  else [raw_generic]
                  if raw_generic
                  else []
            )
            if discovered:
                  parts = [f'brand_name:"{_escape_lucene(brand_name.strip())}"']
                  parts += [f'generic_name:"{_escape_lucene(g.strip())}"' for g in discovered]
                  if route:
                        parts.append(f'route:"{_escape_lucene(route.strip())}"')
                  if dosage_form:
                        parts.append(f'dosage_form:"{_escape_lucene(dosage_form.strip())}"')
                  narrowed = get_queries_result([" AND ".join(parts)]) or []
                  if narrowed:
                        results, matched_via = narrowed, "brand_name_narrowed"

      if max_active_ingredients is not None:
            results = [
                  res for res in results if len(res.active_ingredients) <= max_active_ingredients
            ]
            if not results:
                  return None

      rank_key = (generic_name or brand_name or "").lower()

      def _rank (res: NdcProduct) -> int:
            generic = _norm(res.generic_name or "")
            brand = _norm(res.brand_name or "")
            for key, val in [(generic_name, generic), (brand_name, brand)]:
                  if key:
                        if val == rank_key:
                              return 0
                        if val.startswith(rank_key):
                              return 1
            return 2

      results.sort(key=_rank)

      for res in results:
            res._matched_via = matched_via
            if extract_product_type:
                  res.product_type = _get_product_type(res)
            if extract_package and res.packaging:
                  res.packaged_as = extract_package_info(res.packaging[0].description)
                  res.package_type = res.packaged_as.package_type if res.packaged_as else None
                  if res.packaged_as and res.packaged_as.package_count == 1:
                        res.package_size = res.packaged_as.package_size

      if labelers_cleanup:
            for res in results:
                  if res.labeler_name:
                        res.labeler_name = clean_labeler_name(res.labeler_name)

      return results


# ---------------------------------------------------------------------------
# Labeler directory
# ---------------------------------------------------------------------------


def clean_labeler_name (name: str) -> str:
      cleaned = _PAREN_RE.sub(" ", _WS_RE.sub(" ", name).strip())
      cleaned = _DIV_RE.sub("", cleaned).strip()
      prev = None
      while prev != cleaned:
            prev = cleaned
            cleaned = _SUFFIX_RE.sub("", cleaned).strip()
      cleaned = _WS_RE.sub(" ", _PUNCT_RE.sub(" ", cleaned)).strip()
      return cleaned or name


def get_labeler_directory (reclean: bool = False) -> dict[str, LabelerEntry]:
      try:
            with open(_LABELERS_PATH) as f:
                  raw = json.load(f)
            labelers = {code: LabelerEntry(**entry) for code, entry in raw.items()}
            if reclean:
                  changed = 0
                  for entry in labelers.values():
                        new_name = clean_labeler_name(entry.full_name)
                        if new_name != entry.name:
                              entry.name = new_name
                              changed += 1
                  if changed:
                        _write_labelers(labelers)
                  print(f"recleaned {changed} of {len(labelers)} labeler names")
            return labelers
      except (FileNotFoundError, json.JSONDecodeError):
            pass

      labelers: dict[str, LabelerEntry] = {}
      skip = 0
      while True:
            r = requests.get(
                  _URL,
                  params={
                        "search": "_exists_:labeler_name",
                        "limit": _PAGE_SIZE,
                        "skip": skip,
                  },
                  timeout=_TIMEOUT,
            )
            r.raise_for_status()
            payload = r.json()
            results = payload.get("results") or []
            if not results:
                  break
            for res in results:
                  name = res.get("labeler_name")
                  code = (res.get("product_ndc", "") or "").split("-", 1)[0]
                  if name and code and code not in labelers:
                        labelers[code] = LabelerEntry(
                              name=clean_labeler_name(name),
                              full_name=name,
                              directory=res.get("labeler_directory"),
                        )
            total = payload.get("meta", {}).get("results", {}).get("total", 0)
            skip += _PAGE_SIZE
            if skip >= total or skip >= 25000:
                  break

      if not labelers:
            raise ValueError("labeler directory API returned no results")
      labelers = dict(sorted(labelers.items()))
      _write_labelers(labelers)
      return labelers


def _write_labelers (labelers: dict[str, LabelerEntry]) -> None:
      _LABELERS_PATH.parent.mkdir(parents=True, exist_ok=True)
      tmp = _LABELERS_PATH.with_suffix(".json.tmp")
      with open(tmp, "w") as f:
            json.dump(
                  {code: asdict(entry) for code, entry in labelers.items()}, f, indent=4
            )
      tmp.replace(_LABELERS_PATH)


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
      get_labeler_directory(reclean=True)

      for label, call in [
            ("R1", lambda: lookup_ndc_package("0338-0049-02")),
            ("R1b", lambda: lookup_ndc_package("70092-0004-37")),
            ("R2", lambda: lookup_generic_name("phenylephrine", dosage_form="INJECTION")),
            (
                        "R3",
                        lambda: lookup_generic_name(
                              ingredient_names=["sodium chloride"],
                              dosage_form="INJECTION",
                              max_active_ingredients=1,
                        ),
            ),
            (
                        "R4",
                        lambda: lookup_generic_name(
                              ingredient_names=["sodium chloride"],
                              dosage_form="INJECTION",
                              max_active_ingredients=2,
                        ),
            ),
            (
                        "R5",
                        lambda: lookup_generic_name(
                              ingredient_names=["ampicillin sodium", "sulbactam sulbactam"],
                              max_active_ingredients=2,
                        ),
            ),
            (
                        "R6",
                        lambda: lookup_generic_name(
                              generic_name="penicillin", dosage_form="INJECTION"
                        ),
            ),
      ]:
            print(f"* {label} " + "****" * 5)
            r = call()
            if r:
                  print(dumps([asdict(p) for p in r], indent=4))
