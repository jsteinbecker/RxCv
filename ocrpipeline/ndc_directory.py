"""
FDA NDC Directory loader.

Wraps the FDA's ndcproduct.csv as a queryable resource that powers:
  - NDC -> product/strength/manufacturer/brand lookup (replaces NDC_DB)
  - Manufacturer name recognition (replaces MFG_PATTERNS)
  - Brand name recognition (replaces BRAND_PATTERNS)
  - Product (generic) name vocabulary (replaces PRODUCT_VOCAB)

Loaded once, cached in memory. Build vocabulary indexes for fast substring
matching against OCR text.

The FDA file is large (~120k rows) but only ~5MB. We keep the full row index
in memory; for tighter footprint, switch the indexes to SQLite or DuckDB.
"""

from __future__ import annotations
import csv
import re
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable, Optional


@dataclass(frozen=True)
class NDCEntry:
      """One row from the FDA NDC Directory, decoded into the fields we use."""
      product_ndc: str  # "0264-7800"  (labeler-product, no package code)
      proprietary_name: str  # brand, e.g. "Avycaz"
      nonproprietary_name: str  # generic, e.g. "Ceftazidime and Avibactam"
      dosage_form: str  # "INJECTION, SOLUTION"
      route: str  # "INTRAVENOUS"
      labeler: str  # "Pfizer Laboratories Div Pfizer Inc"
      substances: tuple[str, ...]  # ("CEFTAZIDIME", "AVIBACTAM SODIUM")
      strengths: tuple[str, ...]  # ("2", ".5") parallel to substances
      units: tuple[str, ...]  # ("g/1", "g/1") parallel to substances
      pharm_classes: str
      excluded: bool  # NDC_EXCLUDE_FLAG == 'Y'

      # Convenience derived fields
      @property
      def is_iv_bag(self) -> bool:
            df = self.dosage_form.upper()
            # Premix injectable solutions in non-vial containers tend to be bags.
            return "INJECTION" in df and "VIAL" not in df and "AMPULE" not in df

      @property
      def is_vial(self) -> bool:
            df = self.dosage_form.upper()
            return "INJECTION" in df or "POWDER, FOR SOLUTION" in df or "POWDER, FOR SUSPENSION" in df

      def display_strength(self) -> Optional[str]:
            """Combine strength + unit into a human-readable string. Returns the
            first/primary substance's strength when multi-component."""
            if not self.strengths or not self.units:
                  return None
            s = self.strengths[0]
            u = self.units[0]
            if not s or not u:
                  return None
            # FDA units often look like 'g/1', 'mg/mL', 'meq/1'. Strip the trivial
            # '/1' denominator.
            u_clean = re.sub(r"/1\b", "", u)
            return f"{s} {u_clean}".strip()

      def normalized_manufacturer(self) -> str:
            """Strip corporate suffixes for cleaner matching against label text."""
            return _normalize_company_name(self.labeler)


# ---------------------------------------------------------------------------
# Helpers for FDA-format quirks
# ---------------------------------------------------------------------------
_CORP_SUFFIXES = re.compile(
      r",?\s+(?:"
      r"INC\.?|LLC|LLP|LTD\.?|CORP\.?|CORPORATION|COMPANY|CO\.?|"
      r"PHARMACEUTICALS?|PHARMA|LABORATORIES|LABS|"
      r"DIV(?:ISION)?|GMBH|S\.?A\.?|N\.?V\.?|PLC|"
      r"USA|US|U\.S\.A?\.?"
      r")\b\.?",
      re.IGNORECASE,
)


def _normalize_company_name(raw: str) -> str:
      """Reduce 'Pfizer Laboratories Div Pfizer Inc' -> 'Pfizer'."""
      if not raw:
            return ""
      name = raw.strip()
      # Iteratively strip suffixes from the right.
      prev = None
      while prev != name:
            prev = name
            name = _CORP_SUFFIXES.sub("", name).strip().rstrip(",").strip()
      # If a multi-word name reduces to "<Name> Laboratories Div <Name>",
      # collapse to first word.
      parts = name.split()
      if len(parts) >= 2 and parts[0].lower() == parts[-1].lower():
            return parts[0]
      return parts[0] if parts else name


def _split_semis(s: str) -> tuple[str, ...]:
      """FDA semicolon-delimited multi-value fields."""
      if not s:
            return ()
      return tuple(p.strip() for p in s.split(";") if p.strip())


def _is_descriptive_proprietary(proprietary: str, nonproprietary: str) -> bool:
      """True when a 'proprietary name' is really just a descriptive variant of
      the generic — e.g. '0.9% Sodium Chloride' for nonproprietary 'Sodium
      Chloride'. These exist in the FDA file and shouldn't be treated as brands.
      """
      if not proprietary or not nonproprietary:
            return False
      p_norm = re.sub(r"[^a-z0-9]+", " ", proprietary.lower()).strip()
      g_norm = re.sub(r"[^a-z0-9]+", " ", nonproprietary.lower()).strip()
      if not p_norm or not g_norm:
            return False
      if g_norm in p_norm or p_norm in g_norm:
            return True
      # Strip leading percentages and common qualifiers, then compare again.
      p_stripped = re.sub(r"^\d+(\.\d+)?\s*%?\s*", "", p_norm).strip()
      if p_stripped and (p_stripped in g_norm or g_norm in p_stripped):
            return True
      return False


def _normalize_ndc_for_lookup(ndc: str) -> Optional[str]:
      """Convert a label-printed NDC (e.g. '0264-7800-10') to its product-level
      key ('0264-7800'). Accepts 10- or 11-digit forms with or without hyphens."""
      if not ndc:
            return None
      # Strip hyphens/spaces.
      digits = re.sub(r"\D", "", ndc)
      if len(digits) == 10:
            # Common segmentations: 4-4-2, 5-3-2, 5-4-1.
            # Try them all and pick the one that doesn't strand a 1-digit segment
            # at the start or middle — the FDA stores 4-4 or 5-3 / 5-4 product codes.
            candidates = [
                  (digits[:4], digits[4:8]),  # 4-4-2: drop last 2
                  (digits[:5], digits[5:8]),  # 5-3-2
                  (digits[:5], digits[5:9]),  # 5-4-1
            ]
            # Without more info, default: try them all in lookup.
            return None  # caller should use the multi-candidate path
      if len(digits) == 11:
            # 11-digit pad form (CMS billing). Standard split is 5-4-2.
            return f"{digits[:5]}-{digits[5:9]}"
      # Already segmented as labeler-product? '4-4' or '5-3' or '5-4'.
      parts = ndc.split("-")
      if len(parts) == 2:
            return ndc
      if len(parts) == 3:
            return f"{parts[0]}-{parts[1]}"
      return None


def _ndc_lookup_candidates(ndc: str) -> list[str]:
      """All plausible product-level NDC keys for a printed package NDC."""
      if not ndc:
            return []
      parts = ndc.split("-")
      if len(parts) == 3:
            return [f"{parts[0]}-{parts[1]}"]
      digits = re.sub(r"[^\d]", "", ndc)
      if len(digits) == 11:
            return [f"{digits[:5]}-{digits[5:9]}"]
      if len(digits) == 10:
            # Try all three segmentations.
            return [
                  f"{digits[:4]}-{digits[4:8]}",
                  f"{digits[:5]}-{digits[5:8]}",
                  f"{digits[:5]}-{digits[5:9]}",
            ]
      return []


# ---------------------------------------------------------------------------
# The directory itself
# ---------------------------------------------------------------------------
class NDCDirectory:
      """Loads and indexes the FDA NDC Directory CSV."""

      def __init__(self, csv_path: str, *, exclude_delisted: bool = True):
            self.csv_path = csv_path
            self.exclude_delisted = exclude_delisted
            self._by_ndc: dict[str, NDCEntry] = {}
            self._brand_index: dict[str, str] = {}  # UPPER -> canonical
            self._generic_index: dict[str, str] = {}  # UPPER -> canonical (full string)
            self._labeler_index: dict[str, str] = {}  # UPPER short name -> canonical
            self._brand_pattern: Optional[re.Pattern] = None
            self._generic_pattern: Optional[re.Pattern] = None
            self._labeler_pattern: Optional[re.Pattern] = None
            self._loaded = False

      def load(self) -> "NDCDirectory":
            if self._loaded:
                  return self
            if not os.path.exists(self.csv_path):
                  raise FileNotFoundError(f"NDC directory CSV not found: {self.csv_path}")

            # FDA file is tab-delimited despite the .csv extension in some
            # distributions; auto-detect.
            with open(self.csv_path, "r", encoding="utf-8", errors="replace", newline="") as f:
                  sample = f.read(4096)
                  f.seek(0)
                  try:
                        dialect = csv.Sniffer().sniff(sample, delimiters="\t,")
                  except csv.Error:
                        dialect = csv.excel_tab
                  reader = csv.DictReader(f, dialect=dialect)

                  # Tolerate lower/upper-case header variants.
                  def col(row: dict, *names: str) -> str:
                        for n in names:
                              if n in row and row[n] is not None:
                                    return row[n]
                              nl = n.lower()
                              for k, v in row.items():
                                    if k and k.lower() == nl and v is not None:
                                          return v
                        return ""

                  for row in reader:
                        excluded = col(row, "NDC_EXCLUDE_FLAG").strip().upper() == "Y"
                        if excluded and self.exclude_delisted:
                              continue
                        product_ndc = col(row, "PRODUCTNDC").strip()
                        if not product_ndc:
                              continue
                        entry = NDCEntry(
                              product_ndc=product_ndc,
                              proprietary_name=col(row, "PROPRIETARYNAME").strip(),
                              nonproprietary_name=col(row, "NONPROPRIETARYNAME").strip(),
                              dosage_form=col(row, "DOSAGEFORMNAME").strip(),
                              route=col(row, "ROUTENAME").strip(),
                              labeler=col(row, "LABELERNAME").strip(),
                              substances=_split_semis(col(row, "SUBSTANCENAME")),
                              strengths=_split_semis(col(row, "ACTIVE_NUMERATOR_STRENGTH")),
                              units=_split_semis(col(row, "ACTIVE_INGRED_UNIT")),
                              pharm_classes=col(row, "PHARM_CLASSES").strip(),
                              excluded=excluded,
                        )
                        # Don't overwrite — first occurrence wins (CSV may have duplicate
                        # productndc rows with different package codes; we only keep
                        # product-level info).
                        self._by_ndc.setdefault(product_ndc, entry)

                        # Build vocabulary indexes.
                        # Brand index: only include proprietary names that are
                        # genuinely distinct from the generic. Many FDA generics list
                        # PROPRIETARYNAME equal to (or a trivial variant of) the
                        # NONPROPRIETARYNAME — those aren't real trade names, they're
                        # descriptive labels, and indexing them creates false-positive
                        # brand matches when OCR sees the generic.
                        if entry.proprietary_name and len(entry.proprietary_name) >= 4 \
                                  and not _is_descriptive_proprietary(
                              entry.proprietary_name, entry.nonproprietary_name):
                              self._brand_index.setdefault(
                                    entry.proprietary_name.upper(),
                                    entry.proprietary_name,
                              )
                        if entry.nonproprietary_name and len(entry.nonproprietary_name) >= 4:
                              self._generic_index.setdefault(
                                    entry.nonproprietary_name.upper(),
                                    entry.nonproprietary_name,
                              )
                        short_labeler = entry.normalized_manufacturer()
                        if short_labeler and len(short_labeler) >= 3:
                              self._labeler_index.setdefault(
                                    short_labeler.upper(), short_labeler,
                              )

            self._build_match_patterns()
            self._loaded = True
            return self

      def _build_match_patterns(self) -> None:
            """Compile big alternation patterns for one-shot text matching.
            Sorted by descending length so longer (more specific) names win."""

            def make_pattern(items: Iterable[str]) -> Optional[re.Pattern]:
                  tokens = sorted({i for i in items if i}, key=len, reverse=True)
                  if not tokens:
                        return None
                  escaped = [re.escape(t) for t in tokens]
                  return re.compile(r"\b(?:" + "|".join(escaped) + r")\b", re.IGNORECASE)

            self._brand_pattern = make_pattern(self._brand_index.keys())
            # Generic names are long, so we cap the index to single-substance generics
            # (no commas/semicolons) for performance — multi-component names are
            # rarely matched verbatim in OCR.
            single_generics = [g for g in self._generic_index.keys()
                               if "," not in g and ";" not in g and len(g) <= 60]
            self._generic_pattern = make_pattern(single_generics)
            self._labeler_pattern = make_pattern(self._labeler_index.keys())

      # -----------------------------------------------------------------------
      # Public API
      # -----------------------------------------------------------------------
      def lookup_ndc(self, label_ndc: str) -> Optional[NDCEntry]:
            """Look up an entry by package NDC (with or without package suffix)."""
            if not self._loaded:
                  self.load()
            for cand in _ndc_lookup_candidates(label_ndc):
                  entry = self._by_ndc.get(cand)
                  if entry:
                        return entry
            return None

      def find_brand(self, text: str) -> Optional[str]:
            if not self._loaded:
                  self.load()
            if self._brand_pattern is None:
                  return None
            m = self._brand_pattern.search(text)
            if not m:
                  return None
            return self._brand_index.get(m.group(0).upper(), m.group(0))

      def find_generic(self, text: str) -> Optional[str]:
            if not self._loaded:
                  self.load()
            if self._generic_pattern is None:
                  return None
            m = self._generic_pattern.search(text)
            if not m:
                  return None
            return self._generic_index.get(m.group(0).upper(), m.group(0))

      def find_labeler(self, text: str) -> Optional[str]:
            if not self._loaded:
                  self.load()
            if self._labeler_pattern is None:
                  return None
            m = self._labeler_pattern.search(text)
            if not m:
                  return None
            return self._labeler_index.get(m.group(0).upper(), m.group(0))


# ---------------------------------------------------------------------------
# Module-level singleton, lazily initialized.
# ---------------------------------------------------------------------------
_DEFAULT_PATH = os.environ.get("NDC_DIRECTORY_CSV", "db/ndcproduct.csv")
_directory_singleton: Optional[NDCDirectory] = None


def get_directory(path: Optional[str] = None) -> NDCDirectory:
      """Get the shared NDCDirectory, loading on first use."""
      global _directory_singleton
      if _directory_singleton is None:
            _directory_singleton = NDCDirectory(path or _DEFAULT_PATH)
            _directory_singleton.load()
      return _directory_singleton


def set_directory(directory: NDCDirectory) -> None:
      """Override the singleton (useful for tests)."""
      global _directory_singleton
      _directory_singleton = directory
