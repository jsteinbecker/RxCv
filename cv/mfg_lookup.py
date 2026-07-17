"""
Manufacturer registry.

Parses the human-editable manufacturer reference at /ref/mfg-data.txt and
exposes lookup helpers used during field extraction.

File format (one manufacturer per non-blank, non-comment line):

    Canonical Name [Alt1, Alt2, ...]: 12345, 23456
    Canonical Name: 12345

- Canonical name is everything before `[` or `:`.
- Alt names live inside `[...]`, comma-separated, optional.
- After the colon: comma-separated 5-digit labeler codes (with or without
  leading zeros — both `0409` and `00409` accepted, normalized to 5 digits).
- Lines starting with `#` and blank lines are ignored.

The same canonical name may appear on multiple lines; codes accumulate.
The same code appearing under multiple manufacturers triggers a warning,
and the first occurrence wins (rare in practice).
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_REGISTRY_PATH = Path("ref/mfg-data.txt")

# Legal labeler codes are 4–6 digits; 5-digit is current FDA standard.
# We normalize to 5-digit zero-padded form for hashing.
_CODE_RE = re.compile(r"\b\d{4,6}\b")


@dataclass
class Manufacturer:
      canonical: str
      aliases: list[str] = field(default_factory=list)
      codes: set[str] = field(default_factory=set)

      @property
      def all_names(self) -> list[str]:
            """Canonical + aliases, useful for name-fallback search."""
            return [self.canonical, *self.aliases]


@dataclass
class ManufacturerRegistry:
      """In-memory registry. Build via `load_registry(path)`."""
      by_code: dict[str, Manufacturer] = field(default_factory=dict)
      by_canonical: dict[str, Manufacturer] = field(default_factory=dict)
      # name_lookup maps a normalized name (canonical or alias, lowercased) → Manufacturer
      name_lookup: dict[str, Manufacturer] = field(default_factory=dict)

      # ------------------------------------------------------------------
      # Lookups
      # ------------------------------------------------------------------

      def lookup_by_ndc(self, ndc: str | None) -> Manufacturer | None:
            """Resolve a manufacturer from a full NDC string (any common format)."""
            code = _extract_labeler_code(ndc)
            if not code:
                  return None
            return self.by_code.get(code)

      def lookup_by_code(self, code: str | None) -> Manufacturer | None:
            if not code:
                  return None
            return self.by_code.get(_normalize_code(code))

      def lookup_by_name(self, text: str | None) -> Manufacturer | None:
            """
            Scan free text for any known manufacturer name or alias.
            Longest match wins (so 'Pfizer Laboratories' beats 'Pfizer').
            """
            if not text:
                  return None
            low = text.lower()

            best: tuple[int, Manufacturer] | None = None
            for name_low, mfg in self.name_lookup.items():
                  # Word-boundary match to avoid partial hits inside other words
                  if re.search(rf"\b{re.escape(name_low)}\b", low):
                        if best is None or len(name_low) > best[0]:
                              best = (len(name_low), mfg)
            return best[1] if best else None

      def lookup_by_api(self, text: str | None) -> Manufacturer | None:
            """
            Scan free text for any known manufacturer name or alias.
            Longest match wins (so 'Pfizer Laboratories' beats 'Pfizer').
            This version is intended for API use, so it returns None if the
            input is empty or whitespace-only.
            """
            url = f"https://api.fda.gov/drug/ndc.json?search=product_ndc:{text}"

      # ------------------------------------------------------------------
      # Stats
      # ------------------------------------------------------------------

      def __len__(self) -> int:
            return len(self.by_canonical)

      def summary(self) -> str:
            return (f"{len(self.by_canonical)} manufacturers, "
                    f"{len(self.by_code)} labeler codes, "
                    f"{len(self.name_lookup)} indexed names")


# ============================================================
# Code normalization
# ============================================================

def _normalize_code(code: str) -> str:
      """Strip non-digits and zero-pad to 5 digits (FDA standard)."""
      digits = re.sub(r"[^0-9]", "", code)
      if not digits:
            return ""
      if len(digits) < 5:
            digits = digits.zfill(5)
      return digits[:5]


def _extract_labeler_code(ndc: str | None) -> str | None:
      """
      Extract the 5-digit labeler code from any NDC format:
          00409-1234-56  → 00409
          0409-1234-56   → 00409  (4-digit padded)
          00409123456    → 00409
      """
      if not ndc:
            return None
      digits = re.sub(r"[^0-9]", "", ndc)
      # Try canonical hyphenated form first
      m = re.match(r"^(\d{4,6})", ndc)
      if m:
            return _normalize_code(m.group(1))
      if len(digits) >= 4:
            return _normalize_code(digits[:5] if len(digits) >= 5 else digits[:4])
      return None


# ============================================================
# Parser
# ============================================================

# Matches: "Name [Alt1, Alt2] : 12345, 23456"
#   group 1: canonical name
#   group 2: alt block contents (or None)
#   group 3: codes block
_LINE_RE = re.compile(r"""
    ^\s*
    (?P<name>[^\[\]:]+?)            # canonical name (greedy stop at [ or :)
    \s*
    (?:\[\s*(?P<aliases>[^\]]*)\s*\])?   # optional [aliases]
    \s*
    :\s*
    (?P<codes>[0-9,\s]+?)
    \s*$
    """, re.VERBOSE, )


def parse_registry_text(text: str, *, source: str = "<string>") -> ManufacturerRegistry:
      """Parse the textual manufacturer file into a ManufacturerRegistry."""
      reg = ManufacturerRegistry()

      for lineno, raw in enumerate(text.splitlines(), start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                  continue

            m = _LINE_RE.match(line)
            if not m:
                  print(f"[mfg_lookup] {source}:{lineno}: skipping unparseable line: "
                        f"{line!r}", file=sys.stderr)
                  continue

            canonical = m.group("name").strip()
            aliases_blob = m.group("aliases")
            codes_blob = m.group("codes")

            aliases = []
            if aliases_blob:
                  aliases = [a.strip() for a in aliases_blob.split(",") if a.strip()]

            codes_raw = [c.strip() for c in codes_blob.split(",") if c.strip()]
            codes = set()
            for c in codes_raw:
                  norm = _normalize_code(c)
                  if not norm or len(norm) != 5:
                        print(f"[mfg_lookup] {source}:{lineno}: bad code {c!r} "
                              f"for {canonical!r}", file=sys.stderr)
                        continue
                  codes.add(norm)

            # Merge into existing canonical entry if present
            key = canonical.lower()
            if key in reg.by_canonical:
                  mfg = reg.by_canonical[key]
                  mfg.codes.update(codes)
                  for a in aliases:
                        if a not in mfg.aliases:
                              mfg.aliases.append(a)
            else:
                  mfg = Manufacturer(canonical=canonical, aliases=list(aliases), codes=set(codes))
                  reg.by_canonical[key] = mfg

            # Index codes
            for code in codes:
                  existing = reg.by_code.get(code)
                  if existing and existing.canonical != mfg.canonical:
                        print(f"[mfg_lookup] {source}:{lineno}: code {code} already "
                              f"mapped to {existing.canonical!r}; keeping first "
                              f"(ignoring {canonical!r})", file=sys.stderr)
                  else:
                        reg.by_code[code] = mfg

            # Index names
            for name in mfg.all_names:
                  reg.name_lookup[name.lower()] = mfg

      return reg


def load_registry(path: str | Path = DEFAULT_REGISTRY_PATH) -> ManufacturerRegistry:
      """Load and parse a registry file. Returns empty registry if file missing."""
      p = Path(path)
      if not p.exists():
            print(f"[mfg_lookup] registry file not found at {p}; "
                  f"running without manufacturer lookup", file=sys.stderr)
            return ManufacturerRegistry()
      text = p.read_text(encoding="utf-8")
      return parse_registry_text(text, source=str(p))


# ============================================================
# Singleton accessor
# ============================================================

_DEFAULT_REGISTRY: ManufacturerRegistry | None = None


def get_registry(path: str | Path | None = None, *, reload: bool = False) -> ManufacturerRegistry:
      """
      Return a process-wide registry instance, lazily loaded from
      DEFAULT_REGISTRY_PATH (or the path you pass).
      """
      global _DEFAULT_REGISTRY
      if _DEFAULT_REGISTRY is None or reload or path is not None:
            _DEFAULT_REGISTRY = load_registry(path or DEFAULT_REGISTRY_PATH)
      return _DEFAULT_REGISTRY


# ============================================================
# CLI: quick sanity check
# ============================================================

def _main():
      import argparse
      ap = argparse.ArgumentParser(description="Inspect the manufacturer registry.")
      ap.add_argument("--path", default=str(DEFAULT_REGISTRY_PATH))
      ap.add_argument("--code", help="Look up a labeler code or full NDC")
      ap.add_argument("--name", help="Search for a manufacturer name in free text")
      args = ap.parse_args()

      reg = load_registry(args.path)
      print(reg.summary())

      if args.code:
            m = reg.lookup_by_ndc(args.code) or reg.lookup_by_code(args.code)
            print(f"  code {args.code!r} → {m.canonical if m else 'NO MATCH'}")
            if m:
                  print(f"     aliases: {m.aliases}")
                  print(f"     codes:   {sorted(m.codes)}")

      if args.name:
            m = reg.lookup_by_name(args.name)
            print(f"  name search in {args.name!r} → "
                  f"{m.canonical if m else 'NO MATCH'}")


if __name__ == "__main__":
      _main()
