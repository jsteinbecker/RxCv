"""
Management command: parse Product.generic_name (RxNorm SCD/SBD style) into
related Ingredient rows.

    python manage.py parse_rxnorm_ingredients --dry-run
    python manage.py parse_rxnorm_ingredients --limit 200
    python manage.py parse_rxnorm_ingredients          # writes

Place at: <app>/management/commands/parse_rxnorm_ingredients.py
"""

import re
from collections import Counter
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count

from rxocrpl.models import Product

# --------------------------------------------------------------------------
# Unit vocabulary
# --------------------------------------------------------------------------
# Base (numerator) units seen in RxNorm strength expressions.
BASE_UNITS = {
      "MG", "MCG", "NG", "PG", "G", "KG",
      "ML", "L", "DL",
      "MEQ", "MMOL", "MOL",
      "UNT", "U",
      "%",
      "CELLS", "BAU", "AU", "PNU", "SQ-HDM", "MG-PE",
      "10*3.UNT", "10*6.UNT", "10*9.UNT", "10*6.CFU", "10*9.CFU",
}

# Denominator units. May carry a leading numeric multiplier ("0.5ML", "24HR").
BASE_DENOMINATORS = {
      "ML", "L", "MG", "G", "KG", "MEQ", "MMOL", "UNT",
      "HR", "MIN", "D", "WK",
      "ACTUAT", "SQCM", "M2", "CM2",
}

STRENGTH_TOKEN_RE = re.compile(
            r"""
    (?<![\w.])                                  # not mid-number / mid-word
    (?P<value>\d+(?:\.\d+)?)                    # 325, 0.02
    \s*
    (?P<unit>
        %                                       # bare percent
      | [A-Za-z][\w*.\-]*                       # MG, UNT, 10*6.UNT
        (?: / [\d.]* [A-Za-z][\w*.\-]* )?       # /ML, /0.5ML, /ACTUAT, /24HR
    )
    (?![\w])
    """,
      re.VERBOSE,
)

BRACKET_RE = re.compile(r"\[[^\]]*\]")  # brand suffix: [Tylenol]
SEPARATOR_RE = re.compile(r"\s+/\s+")  # ingredient separator
WS_RE = re.compile(r"\s+")

# Structures we refuse to guess at.
SKIP_MARKERS = ("{", "}", "(", ")")


class ParseError(Exception):
      pass


def normalize_unit (raw):
      """Return canonical unit string, or raise ParseError if unrecognized."""
      unit = raw.upper()
      if "/" in unit:
            num, _, den = unit.partition("/")
            # strip a numeric multiplier off the denominator: 0.5ML -> ML
            den_base = re.sub(r"^[\d.]+", "", den)
            if num not in BASE_UNITS or den_base not in BASE_DENOMINATORS:
                  raise ParseError(f"unrecognized unit {raw!r}")
            return f"{num}/{den}"
      if unit not in BASE_UNITS:
            raise ParseError(f"unrecognized unit {raw!r}")
      return unit


def parse_segment (segment, is_last):
      """
      Parse one ' / '-delimited segment, e.g. 'amoxicillin 875 MG'
      or (if last) 'clavulanate 125 MG Oral Tablet'.

      Returns (name, Decimal(strength), unit, trailing_text).
      """
      candidates = []
      for m in STRENGTH_TOKEN_RE.finditer(segment):
            try:
                  unit = normalize_unit(m.group("unit"))
            except ParseError:
                  continue  # e.g. 'Vitamin B 12'
            candidates.append((m, unit))

      if not candidates:
            raise ParseError(f"no strength found in {segment!r}")
      if len(candidates) > 1:
            raise ParseError(
                  f"{len(candidates)} strength candidates in {segment!r}"
            )

      match, unit = candidates[0]
      name = segment[: match.start()].strip(" ,")
      trailing = segment[match.end():].strip()

      if not name:
            raise ParseError(f"empty ingredient name in {segment!r}")
      if trailing and not is_last:
            # Only the final segment may carry the dose form.
            raise ParseError(f"unexpected text {trailing!r} in {segment!r}")

      try:
            value = Decimal(match.group("value"))
      except InvalidOperation:
            raise ParseError(f"bad strength {match.group('value')!r}")

      return name, value, unit, trailing


def parse_generic_name (generic_name):
      """
      Parse a full RxNorm-style name into
      (list_of_ingredient_dicts, dose_form_or_None).
      Raises ParseError on anything ambiguous.
      """
      text = BRACKET_RE.sub("", generic_name or "")
      text = WS_RE.sub(" ", text).strip()

      if not text:
            raise ParseError("empty generic_name")
      if any(ch in text for ch in SKIP_MARKERS):
            raise ParseError("pack / parenthetical form — skipped")

      segments = SEPARATOR_RE.split(text)
      ingredients = []
      dose_form = None

      for i, seg in enumerate(segments):
            is_last = i == len(segments) - 1
            name, value, unit, trailing = parse_segment(seg, is_last)
            ingredients.append({"name": name, "strength": value, "unit": unit})
            if is_last:
                  dose_form = trailing or None

      return ingredients, dose_form


# --------------------------------------------------------------------------
# Command
# --------------------------------------------------------------------------
class Command(BaseCommand):
      help = "Populate Product.ingredients by parsing RxNorm-style generic_name."

      def add_arguments (self, parser):
            parser.add_argument("--dry-run", action="store_true")
            parser.add_argument("--limit", type=int, default=None)
            parser.add_argument(
                  "--show-failures", action="store_true",
                  help="Print every unparsed generic_name, not just a tally.",
            )

      def handle (self, *args, **opts):
            qs = (
                  Product.objects
                  .filter(ingredients__isnull=True)
                  .exclude(generic_name__isnull=True)
                  .exclude(generic_name__exact="")
                  .order_by("pk")
            )
            if opts["limit"]:
                  qs = qs[: opts["limit"]]

            parsed = created = 0
            reasons = Counter()
            failures = []

            with transaction.atomic():
                  for product in qs.iterator(chunk_size=500):
                        try:
                              ingredients, dosage_form = parse_generic_name(
                                    product.generic_name
                              )
                        except ParseError as exc:
                              reasons[str(exc).split(" in ")[0]] += 1
                              failures.append((product.pk, product.generic_name, exc))
                              continue

                        parsed += 1
                        if opts["dry_run"]:
                              self.stdout.write(
                                    f"{product.pk}: {product.generic_name}\n"
                                    f"    form={dosage_form!r}"
                              )
                              for ing in ingredients:
                                    self.stdout.write(
                                          f"    - {ing['name']} | "
                                          f"{ing['strength']} | {ing['unit']}"
                                    )
                              continue

                        for ing in ingredients:
                              product.ingredients.create(**ing)
                              created += 1

                        if dosage_form and product.dosage_form != dosage_form:
                              product.dosage_form = dosage_form
                              product.save(update_fields=["dosage_form"])

                  if opts["dry_run"]:
                        transaction.set_rollback(True)

            if opts["show_failures"]:
                  for pk, name, exc in failures:
                        self.stdout.write(self.style.WARNING(f"{pk}: {name}  -- {exc}"))

            self.stdout.write(self.style.SUCCESS(
                  f"\nparsed={parsed} ingredients_created={created} "
                  f"skipped={sum(reasons.values())}"
            ))
            for reason, n in reasons.most_common():
                  self.stdout.write(f"  {n:>6}  {reason}")
