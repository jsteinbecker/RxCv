"""
rxnorm
parser.py

A regex-based parser for RxNorm concept name strings (SCD / SBD / GPCK / BPCK
style strings, i.e. the kind returned as RXNSTRING/STR for term types like
SCD, SBD, SCDC, GPCK, BPCK).

RxNorm strings aren't produced from a single formal grammar, but the
generation templates are consistent enough that a layered regex approach
reliably recovers:

    - Pack wrapper:            "{ N (...) / N (...) } Pack"
    - Leading dose qualifier:  "24 HR", "12 HR" (extended-release timing)
    - Brand name suffix:       "[Vicodin]"
    - Ingredient / strength components, split on " / "
    - Numerator (and optional denominator) strength unit for each component
    - Trailing dose form, matched against a known RxNorm dose-form vocabulary

Limitations (this is NOT a full RxNorm grammar):
    - Dose form recognition depends on the DOSE_FORMS list below. Anything
      not in that list falls back to "everything after the last strength
      token", which is usually right but not guaranteed for oddly-ordered
      multi-ingredient strings.
    - Pack (GPCK/BPCK) strings are parsed recursively but assume the
      "{N (SCD/SBD) / N (SCD/SBD) ...} Pack [Brand]" shape RxNorm actually
      uses.
    - Ingredient names that themselves contain digits (rare, e.g. some
      vitamin/mineral names) can confuse the strength boundary; the parser
      takes the *last* strength-shaped match in each segment as the
      ingredient's strength, which is correct for essentially all real
      RxNorm strings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

# Longest-first so the alternation (which regex tries in order) prefers the
# more specific dose form over a shorter substring of it.
_RAW_DOSE_FORMS = [
      "Extended Release Oral Tablet", "Extended Release Oral Capsule",
      "Delayed Release Oral Tablet", "Delayed Release Oral Capsule",
      "Disintegrating Oral Tablet", "Chewable Extended Release Tablet",
      "Prefilled Syringe", "Injectable Suspension", "Injectable Solution",
      "Injectable Foam", "Extended Release Suspension",
      "Extended Release Injectable Suspension",
      "Metered Dose Inhaler", "Dry Powder Inhaler", "Inhalant Solution",
      "Inhalant Powder", "Transdermal System", "Topical Suspension",
      "Ophthalmic Suspension", "Ophthalmic Solution", "Ophthalmic Ointment",
      "Ophthalmic Gel", "Ophthalmic Cream", "Otic Suspension", "Otic Solution",
      "Nasal Spray", "Nasal Solution", "Nasal Suspension", "Rectal Suppository",
      "Rectal Solution", "Vaginal Suppository", "Vaginal Cream",
      "Vaginal Insert", "Vaginal Tablet", "Buccal Film", "Buccal Tablet",
      "Sublingual Tablet", "Sublingual Film", "Medicated Patch", "Medicated Pad",
      "Chewable Tablet", "Disintegrating Tablet", "Oral Tablet", "Oral Capsule",
      "Oral Suspension", "Oral Solution", "Oral Syrup", "Oral Powder",
      "Oral Cream", "Oral Paste", "Oral Gel", "Oral Film",
      "Topical Cream", "Topical Ointment", "Topical Gel", "Topical Lotion",
      "Topical Solution", "Topical Foam", "Topical Spray", "Topical Powder",
      "Injection", "Cream", "Ointment", "Lotion", "Gel", "Foam", "Powder",
      "Solution", "Suspension", "Tablet", "Capsule", "Patch", "Suppository",
      "Shampoo", "Paste", "Film", "Spray", "Swab", "Enema", "Bar Soap",
      "Liquid Soap", "Chewing Gum", "Mouthwash", "Toothpaste",
]
DOSE_FORMS = sorted(set(_RAW_DOSE_FORMS), key=len, reverse=True)

_UNIT = r"(?:MG|MCG|G|ML|L|MEQ|MMOL|UNT|%|HR|ACTUAT)"

# One ingredient's strength: "325 MG", "100 UNT/ML", "0.02 MG/ML"
STRENGTH_RE = re.compile(
      rf"(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>{_UNIT})"
      rf"(?:\s*/\s*(?P<denom_num>\d+(?:\.\d+)?)?\s*(?P<denom_unit>{_UNIT}))?",
      re.IGNORECASE,
)

BRAND_RE = re.compile(r"\[(?P<brand>[^\[\]]+)\]\s*$")

LEADING_QUALIFIER_RE = re.compile(r"^(?P<qualifier>\d+\s*HR)\s+", re.IGNORECASE)

DOSE_FORM_RE = re.compile(
      r"(?P<dose_form>" + "|".join(re.escape(df) for df in DOSE_FORMS) + r")\s*$",
      re.IGNORECASE,
)

# "N (....)" pack component, at top level (used inside a {...} Pack body)
PACK_COMPONENT_RE = re.compile(r"(?P<qty>\d+(?:\.\d+)?)\s*\((?P<body>.+?)\)")

PACK_WRAPPER_RE = re.compile(
      r"^\{\s*(?P<body>.*?)\s*\}\s*Pack(?:\s*\[[^\[\]]+\])?\s*$",
      re.IGNORECASE | re.DOTALL,
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class IngredientStrength:
      ingredient: str
      strength_num: Optional[str] = None
      strength_unit: Optional[str] = None
      denom_num: Optional[str] = None
      denom_unit: Optional[str] = None

      def strength_str(self) -> Optional[str]:
            if self.strength_num is None:
                  return None
            s = f"{self.strength_num} {self.strength_unit}"
            if self.denom_unit:
                  s += f"/{(self.denom_num + ' ') if self.denom_num else ''}{self.denom_unit}"
            return s


@dataclass
class PackComponent:
      quantity: str
      parsed: "RxNormParts"


@dataclass
class RxNormParts:
      raw: str
      is_pack: bool = False
      pack_components: List[PackComponent] = field(default_factory=list)
      leading_qualifier: Optional[str] = None
      components: List[IngredientStrength] = field(default_factory=list)
      dose_form: Optional[str] = None
      brand_name: Optional[str] = None
      unparsed_remainder: Optional[str] = None  # anything left over, for debugging


# ---------------------------------------------------------------------------
# Core parser
# ---------------------------------------------------------------------------

def parse_rxnorm_string(name: str) -> RxNormParts:
      """Split an RxNorm concept name string into its regex-recognizable parts."""
      original = name.strip()
      working = original

      # 1. Pack wrapper: "{ 2 (...) / 3 (...) } Pack"
      pack_match = PACK_WRAPPER_RE.match(working)
      if pack_match:
            body = pack_match.group("body")
            remainder_after_wrapper = working[pack_match.end():].strip()

            result = RxNormParts(raw=original, is_pack=True)

            # brand name can trail the whole pack, e.g. "...} Pack [Estrostep]"
            brand_match = BRAND_RE.search(remainder_after_wrapper) or BRAND_RE.search(working)
            if brand_match:
                  result.brand_name = brand_match.group("brand").strip()

            for comp_match in PACK_COMPONENT_RE.finditer(body):
                  qty = comp_match.group("qty")
                  sub_str = comp_match.group("body").strip()
                  result.pack_components.append(
                        PackComponent(quantity=qty, parsed=parse_rxnorm_string(sub_str))
                  )
            return result

      result = RxNormParts(raw=original)

      # 2. Brand name suffix, e.g. "... Oral Tablet [Vicodin]"
      brand_match = BRAND_RE.search(working)
      if brand_match:
            result.brand_name = brand_match.group("brand").strip()
            working = working[: brand_match.start()].strip()

      # 3. Leading dose qualifier, e.g. "24 HR Metformin ..."
      qualifier_match = LEADING_QUALIFIER_RE.match(working)
      if qualifier_match:
            result.leading_qualifier = qualifier_match.group("qualifier").strip()
            working = working[qualifier_match.end():].strip()

      # 4. Trailing dose form
      dose_match = DOSE_FORM_RE.search(working)
      if dose_match:
            result.dose_form = dose_match.group("dose_form").strip()
            working = working[: dose_match.start()].strip()
      else:
            result.unparsed_remainder = None  # nothing to flag yet

      # 5. Split remaining "ingredient strength / ingredient strength / ..."
      #    Split on " / " with surrounding spaces so we don't break a strength
      #    ratio like "100 UNT/ML" (no spaces around that slash).
      segments = re.split(r"\s+/\s+", working) if working else []

      leftover_flags = []
      for seg in segments:
            seg = seg.strip()
            if not seg:
                  continue
            strength_matches = list(STRENGTH_RE.finditer(seg))
            if strength_matches:
                  last = strength_matches[-1]
                  ingredient = seg[: last.start()].strip(" ,")
                  comp = IngredientStrength(
                        ingredient=ingredient,
                        strength_num=last.group("num"),
                        strength_unit=last.group("unit").upper(),
                        denom_num=last.group("denom_num"),
                        denom_unit=(last.group("denom_unit") or "").upper() or None,
                  )
            else:
                  # no strength found at all -- keep whole segment as ingredient
                  comp = IngredientStrength(ingredient=seg)
                  leftover_flags.append(seg)
            result.components.append(comp)

      if leftover_flags:
            result.unparsed_remainder = " | ".join(leftover_flags)

      return result


# ---------------------------------------------------------------------------
# Pretty printer, handy for debugging / demos
# ---------------------------------------------------------------------------

def describe(parts: RxNormParts, indent: int = 0) -> str:
      pad = "  " * indent
      lines = [f"{pad}raw: {parts.raw!r}"]
      if parts.is_pack:
            lines.append(f"{pad}is_pack: True")
            if parts.brand_name:
                  lines.append(f"{pad}brand_name: {parts.brand_name}")
            for pc in parts.pack_components:
                  lines.append(f"{pad}component x{pc.quantity}:")
                  lines.append(describe(pc.parsed, indent + 2))
            return "\n".join(lines)

      if parts.leading_qualifier:
            lines.append(f"{pad}leading_qualifier: {parts.leading_qualifier}")
      for c in parts.components:
            lines.append(f"{pad}ingredient: {c.ingredient!r}  strength: {c.strength_str()}")
      lines.append(f"{pad}dose_form: {parts.dose_form}")
      if parts.brand_name:
            lines.append(f"{pad}brand_name: {parts.brand_name}")
      if parts.unparsed_remainder:
            lines.append(f"{pad}unparsed_remainder: {parts.unparsed_remainder!r}")
      return "\n".join(lines)


if __name__ == "__main__":
      examples = [
            "acetaminophen 325 MG Oral Tablet",
            "lisinopril 10 MG / hydrochlorothiazide 12.5 MG Oral Tablet",
            "Acetaminophen 325 MG / Hydrocodone Bitartrate 5 MG Oral Tablet [Vicodin]",
            "24 HR Metformin hydrochloride 500 MG Extended Release Oral Tablet",
            "Enalapril maleate 5 MG Oral Tablet [Vasotec]",
            "Insulin Human 100 UNT/ML Injectable Solution",
            "Fluticasone Propionate 0.05 MG/ACTUAT Nasal Spray",
            "{4 (Ethinyl Estradiol 0.02 MG / Norethindrone Acetate 1 MG Oral Tablet) "
            "/ 3 (Ethinyl Estradiol 0.035 MG / Norethindrone Acetate 1 MG Oral Tablet) } "
            "Pack [Estrostep]",
      ]
      for ex in examples:
            print("=" * 80)
            print(describe(parse_rxnorm_string(ex)))
