from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth.models import AbstractUser

from rxocrpl.rxnorm.parser import parse_rxnorm_string, RxNormParts
import re
from typing import Any

from django.conf import settings
from django.db import models
from django.core.validators import MinValueValidator
from django.core.exceptions import ValidationError
from django.utils import timezone
from computedfields.models import ComputedFieldsModel, ComputedField
import pint

from .quantities import Quantity
from .rxgraph.traversal import RxConceptTraversalMixin
from .substance import SubstanceQuantity, Substance
from .dailymed import get_dailymed_url


class User(AbstractUser):
      """Pharmacy staff. is_rph distinguishes pharmacists (who can verify) from techs."""
      USERNAME_FIELD = "username"
      username = models.CharField(max_length=150, unique=True)
      first_name = models.CharField(max_length=150)
      last_name = models.CharField(max_length=150)
      email = models.EmailField(unique=True)
      is_rph = models.BooleanField(default=False)
      facility = models.ForeignKey("Facility", on_delete=models.SET_NULL, null=True, blank=True, related_name="users", )
      user_type = models.CharField(max_length=100, null=True, blank=True)

      def has_role (self, role_name: str, facility=None, organization=None) -> bool:
            """
            Check if user holds an active grant for the role at the given scope,
            considering hierarchy inheritance.
            """
            from rxocrpl.models import RoleGrant

            now = timezone.now()
            base_qs = RoleGrant.objects.filter(
                  user=self, role__name=role_name, revoked_at__isnull=True
            ).exclude(expires_at__lt=now)

            if organization:
                  if base_qs.filter(organization=organization).exists():
                        return True

            if facility:
                  if base_qs.filter(facility=facility).exists():
                        return True

                  curr = facility.parent
                  while curr:
                        if base_qs.filter(facility=curr).exists():
                              return True
                        curr = curr.parent

                  if facility.organization:
                        if base_qs.filter(organization=facility.organization).exists():
                              return True

            return False

      @property
      def is_admin (self) -> bool:
            """Returns True if the user has any active admin grant."""
            from rxocrpl.models import RoleGrant

            return (
                  RoleGrant.objects.filter(
                        user=self,
                        role__name__in=["org_admin", "facility_admin"],
                        revoked_at__isnull=True,
                  )
                  .exclude(expires_at__lt=timezone.now())
                  .exists()
            )


class TermType(models.TextChoices):
      """Type of term extracted from an OCR result."""

      IN = "IN", "Ingredient"
      PIN = "PIN", "Precise Ingredient"
      SCDC = "SCDC", "Semantic Clinical Drug Component"
      SCD = "SCD", "Semantic Clinical Drug"
      SCDG = "SCDG", "Semantic Clinical Drug Dose Form Group"
      BN = "BN", "Branded Name"
      SBDC = "SBDC", "Semantic Branded Drug Component"
      SBD = "SBD", "Semantic Branded Drug"
      BPCK = "BPCK", "Brand Name Pack"
      GPCK = "GPCK", "Generic Pack"


class RouteOfAdministration(models.TextChoices):
      """Route of administration for a clinical drug."""

      ORAL = "oral", "Oral"
      ORAL__DISINTEGRATING = "oral_disintegrating", "Oral Disintegrating"
      ORAL__SUBLINGUAL = "oral_sublingual", "Oral Sublingual"
      ORAL__LIQ = "oral_liquid", "Oral Liquid"
      ORAL__LIQ__SYRUP = "oral_liquid_syrup", "Oral Liquid Syrup"
      INTRAVENOUS = "intravenous", "Intravenous"
      INTRAMUSCULAR = "intramuscular", "Intramuscular"
      SUBCUTANEOUS = "subcutaneous", "Subcutaneous"
      TOPICAL = "topical", "Topical"
      INHALATION = "inhalation", "Inhalation"
      RECTAL = "rectal", "Rectal"
      VAGINAL = "vaginal", "Vaginal"
      INTRAVESICULAR = "intravesicular", "Intravesicular"
      NASAL = "nasal", "Nasal"
      OPHTHALMIC = "ophthalmic", "Ophthalmic"
      OTIC = "otic", "Otic"


class DoseForm(models.TextChoices):
      """Dose form for a clinical drug."""

      PILL = "pill", "Pill"
      TABLET = "tablet", "Tablet"
      CAPSULE = "capsule", "Capsule"
      INJECTION = "injection", "Injection"
      SOLUTION = "solution", "Solution"
      SUSPENSION = "suspension", "Suspension"
      CREAM = "cream", "Cream"
      OINTMENT = "ointment", "Ointment"
      GEL = "gel", "Gel"
      PATCH = "patch", "Patch"
      DROPS = "drops", "Drops"
      SPRAY = "spray", "Spray"
      POWDER = "powder", "Powder"
      SUPPOSITORY = "suppository", "Suppository"


class RxNormConcept(RxConceptTraversalMixin, ComputedFieldsModel):
      """RxNorm concept mapping."""

      rxcui = models.CharField(max_length=20, primary_key=True)
      name = models.CharField(max_length=255, null=True, blank=True)
      tty = models.CharField(max_length=10, choices=TermType.choices, null=True, blank=True)
      active = models.BooleanField(default=True)
      attributes = models.JSONField(default=dict, blank=True)
      synced_at = models.DateTimeField(auto_now=True)
      rxnorm_release = models.CharField(max_length=20, null=True, blank=True)
      rxid = ComputedField(models.PositiveIntegerField(null=True, blank=True),
                           lambda self: int(self.rxcui) if self.rxcui and self.rxcui.isdigit() else None,
                           depends=[("rxcui", [])], )

      class Meta:
            verbose_name = "Concept"
            verbose_name_plural = "Concepts"

      def parsed (self) -> RxNormParts | None:
            return parse_rxnorm_string(self.name) if self.name else None

      def __str__ (self):
            return f"[{self.tty}.{self.rxcui}] {self.name}"


class RxNormConceptRelation(models.Model):
      """Edges in the RxNorm graph (e.g. SCD --consists_of--> SCDC)."""

      source = models.ForeignKey(RxNormConcept, related_name="outbound_relations", on_delete=models.CASCADE)
      target = models.ForeignKey(RxNormConcept, related_name="inbound_relations", on_delete=models.CASCADE)
      rela = models.CharField(max_length=50)
      # Optional strength attributes (materialized from SCDC/SCD has_ingredient edges)
      numerator_value = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
      numerator_unit = models.CharField(max_length=20, null=True, blank=True)
      denominator_value = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
      denominator_unit = models.CharField(max_length=20, null=True, blank=True)

      class Meta:
            unique_together = ("source", "target", "rela")
            verbose_name = "Concept Relation"
            verbose_name_plural = "Concept Relations"

      def __str__ (self):
            return f"{self.source.rxcui} -[{self.rela}]-> {self.target.rxcui}"


def check_quantified_form (name):
      """Check if the clinical drug name contains a quantified form (e.g., '500 MG')."""
      if not name:
            return False
      # Simple heuristic: look for a number followed by a unit (MG, ML, etc.)
      import re

      qty_re = re.compile(r"\b\d+(\.\d+)?\s*(MG|ML|G|L|MCG|IU)\b", re.IGNORECASE)
      conc_re = re.compile(r"\b\d+(\.\d+)?\s*(MG/ML|G/L|MCG/ML)\b", re.IGNORECASE)
      quantity_match = qty_re.search(name)
      conc_match = conc_re.search(name)
      if quantity_match and conc_match:
            qty = pint.Quantity(f"{quantity_match.group(0)}")
            conc = pint.Quantity(f"{conc_match.group(0)}")
            try:
                  total = qty * conc
                  return True
            except pint.errors.DimensionalityError:
                  return False
      return False


class Labeler(models.Model):
      """Drug labeler details."""

      labeler_code = models.CharField(max_length=10, primary_key=True)
      name = models.CharField(max_length=255)
      verbose_name = models.CharField(max_length=255, null=True, blank=True)
      active = models.BooleanField(default=True)

      def __str__ (self):
            return f"{self.name} ({self.labeler_code})"


class Product(ComputedFieldsModel):
      """FDA drug product details."""

      product_ndc = models.CharField(max_length=20, primary_key=True)
      generic_name = models.CharField(max_length=400)
      brand_name = models.CharField(max_length=100, null=True, blank=True)
      labeler = models.ForeignKey(Labeler, on_delete=models.SET_NULL, null=True, blank=True)
      labeler_name = models.CharField(max_length=100)
      dosage_form = models.CharField(max_length=100)
      route = models.JSONField(default=list)  # List of route strings
      active_ingredients = models.JSONField(default=list)  # List of ingredient dicts
      ingredient_count = ComputedField(models.PositiveIntegerField(default=0),
                                       depends=[("active_ingredients", [])], compute=lambda self: len(self.active_ingredients))
      possible_brands = ComputedField(
            models.CharField(max_length=400, blank=True, default=""),
            depends=[("active_ingredients", []), ("dosage_form", [])],
            compute=lambda self: self._compute_possible_brands(),
      )
      rxcui_mapping = models.JSONField(default=dict)  # Mapping of NDC to RxCUI info
      active = models.BooleanField(default=True)
      concepts = models.ManyToManyField(RxNormConcept, related_name="products")

      def __str__ (self):
            if self.as_substance():
                  return f"{self.as_substance()} ({self.product_ndc})"
            return f"{self.describe()} ({self.product_ndc})"

      def to_dict (self) -> dict[str, Any]:
            return {
                  "product_ndc": self.product_ndc,
                  "generic_name": self.generic_name,
                  "brand_name": self.brand_name,
                  "labeler_name": self.labeler_name,
                  "dosage_form": self.dosage_form,
                  "route": self.route,
                  "active_ingredients": self.active_ingredients,
                  "rxcui_mapping": self.rxcui_mapping,
            }

      def describe (self) -> str:
            parts = [self.generic_name]
            if self.brand_name and self.brand_name.upper() != self.generic_name.upper():
                  parts.append(f"({self.brand_name})")
            if self.dosage_form:
                  parts.append(self.dosage_form)
            return " ".join(parts)

      @property
      def is_kit (self) -> bool:
            """True for FDA products whose dosage_form is a multi-item KIT.

            A KIT is not a single drug with one strength profile -- it bundles
            several distinct component products (e.g. a Z-Pak or Medrol
            Dosepak). `active_ingredients` must not be treated as one drug's
            ingredient list for these; see `as_substance` and `components`.
            """
            return (self.dosage_form or "").strip().upper() == "KIT"

      @classmethod
      def from_fda_result (cls, result: object) -> "Product":
            def _read (res, key, default=None):
                  if isinstance(res, dict):
                        return res.get(key, default)
                  return getattr(res, key, default)

            active_ingredients = _read(result, "active_ingredients") or []
            # Normalize active ingredients to the list of dicts format
            ingredients_data = []
            for ai in active_ingredients:
                  if hasattr(ai, "name"):
                        ingredients_data.append(
                              {"name": ai.name, "strength": getattr(ai, "strength", "")}
                        )
                  else:
                        ingredients_data.append(ai)

            return cls(
                  generic_name=_read(result, "generic_name", ""),
                  brand_name=_read(result, "brand_name"),
                  labeler_name=_read(result, "labeler_name", ""),
                  product_ndc=_read(result, "product_ndc", ""),
                  active_ingredients=ingredients_data,
                  dosage_form=_read(result, "dosage_form", ""),
                  route=_read(result, "route") or [],
            )

      @classmethod
      def lookup_by_ndc (cls, ndc: str, fetch_rxcui: bool = True) -> "Product | None":
            from .fda import lookup_ndc_package

            results = lookup_ndc_package(ndc, fetch_rxcui=fetch_rxcui)
            if results:
                  return cls.from_fda_result(results[0])
            return None

      @classmethod
      def lookup_by_generic_name (
                cls,
                generic_name: str,
                dosage_form: str | None = None,
                brand_name: str | None = None,
                fetch_rxcui: bool = True,
      ) -> "list[Product]":
            from .fda import lookup_generic_name

            results = lookup_generic_name(
                  generic_name, dosage_form, brand_name=brand_name, fetch_rxcui=fetch_rxcui
            )
            if results:
                  return [cls.from_fda_result(r) for r in results]
            return []

      def as_substance (self) -> str:
            if self.is_kit:
                  return self._kit_substance()

            substances: list[SubstanceQuantity] = []
            for ing in self.active_ingredients:
                  try:
                        name = ing.get("name") or ing.get("ingredient")
                        if not name:
                              continue

                        strength = ing.get("strength", ing.get("value", ""))
                        unit = ing.get("unit", "")
                        full_str = f"{strength} {unit}".strip()

                        match = re.search(
                              r"(.+?)\s*/\s*(\d*\.?\d+)\s*([A-Za-zµμ]*)",
                              full_str,
                              re.IGNORECASE,
                        )
                        if match and float(match.group(2)) != 0:
                              numerator_str = match.group(1).strip()
                              divisor = float(match.group(2))
                              denominator_unit = match.group(3).strip()

                              q_num = Quantity(numerator_str)
                              new_mag = q_num.value / divisor
                              if denominator_unit:
                                    q = Quantity(new_mag, f"{q_num.unit}/{denominator_unit}")
                              else:
                                    q = Quantity(new_mag, q_num.unit)
                        else:
                              q = Quantity(full_str)

                        if "/" in q.unit and q.dimension.is_dimensionless:
                              q = q.to("%")

                        name = re.sub(r"\.Alpha\.", "α", name, flags=re.IGNORECASE)
                        name = re.sub(r"\.Beta\.", "β", name, flags=re.IGNORECASE)
                        name = re.sub(r"\.Gamma\.", "γ", name, flags=re.IGNORECASE)
                        name = re.sub(r"\.Delta\.", "δ", name, flags=re.IGNORECASE)
                        name = re.sub(r"\.Mu\.", "μ", name, flags=re.IGNORECASE)
                        name = re.sub(r"\.Omega\.", "ω", name, flags=re.IGNORECASE)

                        # Sterochemical Prefixes move to front
                        name = re.sub(
                              r"^\s*(.+?)\s*,\s*((?:DL|LD|D|L)-)(\s*(?:\(.*\))?)\s*$",
                              lambda m: f"{m.group(2).upper()}{m.group(1)}{m.group(3)}",
                              name,
                              flags=re.IGNORECASE,
                        )

                        substances.append(
                              SubstanceQuantity(value=q.value, unit=q.unit, substance=Substance(name=name))
                        )
                  except (ValueError, KeyError, TypeError, AttributeError):
                        # Skip only the bad ingredient, not the whole product
                        continue

            if not substances:
                  return ""

            # RxNorm SCD-style ordering: alphabetical by ingredient name
            substances.sort(key=lambda s: s.substance.name.lower())

            return " / ".join(
                  f"{s.substance.name.lower()} {self._format_strength(s.value, s.unit)}" for s in substances
            )

      def _kit_substance (self) -> str:
            """Build a substance string for a KIT from its resolved components.

            Unlike a combination drug, a kit's ingredients don't belong to one
            strength profile -- each component keeps its own. Returns "" (not
            a merged fake SCD string) when no components have been
            materialized yet, so callers fall back to `describe()`.
            """
            parts = [component.name for component in self.components.all() if component.name]
            return " + ".join(parts)

      @staticmethod
      def _format_strength (value: float, unit: str) -> str:
            """Render a strength the way RxNorm does: bare integer if whole, upper-cased unit."""
            value_str = str(int(value)) if float(value).is_integer() else f"{value:g}"
            if unit == "%":
                  return f"{value_str}%"
            return f"{value_str} {unit.upper()}"

      @staticmethod
      def _ingredient_name_set (active_ingredients: list) -> frozenset:
            """Normalized set of ingredient names, for matching drugs with identical formulas."""
            names = set()
            for ing in active_ingredients or []:
                  name = (ing.get("name") or ing.get("ingredient") or "").strip().upper()
                  if name:
                        names.add(name)
            return frozenset(names)

      @staticmethod
      def _dose_form_matches (concept: "RxNormConcept", rxnorm_dose_form: str) -> bool:
            parsed = concept.parsed()
            return bool(parsed and parsed.dose_form and parsed.dose_form.lower() == rxnorm_dose_form.lower())

      def _rxnorm_chain_brand_names (self) -> set:
            """Brand names reachable from this product's linked RxNormConcept(s)
            via a BN, SBD, or SBDC target, gated on the chain concept's own
            dose form matching this product's dosage_form.

            Takes precedence over the ingredient-set heuristic below when
            non-empty, since a materialized RxNorm chain is a stronger signal
            than an inferred ingredient/dosage_form match.
            """
            from rxocrpl.local_rxnorm_linker import guess_rxnorm_dose_form

            rxnorm_dose_form = guess_rxnorm_dose_form(self)
            if not rxnorm_dose_form:
                  return set()

            anchors = list(self.concepts.all())
            if not anchors:
                  return set()

            # GPCK/BPCK anchors aren't themselves SCD/SBD -- resolve to their
            # member drugs first.
            expanded = []
            for concept in anchors:
                  if concept.tty in ("GPCK", "BPCK"):
                        member_ids = RxNormConceptRelation.objects.filter(
                              source=concept, rela="contains"
                        ).values_list("target_id", flat=True)
                        expanded.extend(RxNormConcept.objects.filter(pk__in=member_ids))
                  else:
                        expanded.append(concept)

            names = set()
            for concept in expanded:
                  if not self._dose_form_matches(concept, rxnorm_dose_form):
                        continue

                  # SCD/SCDC --has_tradename--> SBD/SBDC
                  target_ids = RxNormConceptRelation.objects.filter(
                        source=concept, rela="has_tradename", target__tty__in=("SBD", "SBDC")
                  ).values_list("target_id", flat=True)
                  for target in RxNormConcept.objects.filter(pk__in=target_ids):
                        if self._dose_form_matches(target, rxnorm_dose_form):
                              parsed = target.parsed()
                              names.add((parsed.brand_name if parsed else None) or target.name)

                  # IN --has_tradename--> BN (bare trade names carry no dose form
                  # of their own; trusted because `concept`'s dose form already
                  # matched above)
                  ingredient_ids = RxNormConceptRelation.objects.filter(
                        source=concept, rela="has_ingredient", target__tty="IN"
                  ).values_list("target_id", flat=True)
                  bn_ids = RxNormConceptRelation.objects.filter(
                        source_id__in=ingredient_ids, rela="has_tradename", target__tty="BN"
                  ).values_list("target_id", flat=True)
                  names.update(RxNormConcept.objects.filter(pk__in=bn_ids).values_list("name", flat=True))

                  # SCD --consists_of--> SCDC --has_tradename--> SBDC
                  component_ids = RxNormConceptRelation.objects.filter(
                        source=concept, rela="consists_of", target__tty="SCDC"
                  ).values_list("target_id", flat=True)
                  sbdc_ids = RxNormConceptRelation.objects.filter(
                        source_id__in=component_ids, rela="has_tradename", target__tty="SBDC"
                  ).values_list("target_id", flat=True)
                  for target in RxNormConcept.objects.filter(pk__in=sbdc_ids):
                        if self._dose_form_matches(target, rxnorm_dose_form):
                              parsed = target.parsed()
                              names.add((parsed.brand_name if parsed else None) or target.name)

            return {n for n in names if n}

      def _compute_possible_brands (self) -> str:
            """"&&"-separated brand names of products sharing this product's
            ingredients and dosage_form -- e.g. a generic Diazepam Tablet
            resolves to "Valium" even though it has no brand_name itself.

            If a materialized RxNorm concept chain reaches a BN/SBD/SBDC with
            a matching dosage form, that takes precedence.
            """
            rxnorm_brands = self._rxnorm_chain_brand_names()
            if rxnorm_brands:
                  return " && ".join(sorted(rxnorm_brands))

            self_names = self._ingredient_name_set(self.active_ingredients)
            if not self_names or not self.dosage_form:
                  return ""

            candidates = (
                  Product.objects.filter(dosage_form=self.dosage_form)
                  .exclude(brand_name__isnull=True)
                  .exclude(brand_name="")
                  .exclude(pk=self.pk)
                  .values_list("brand_name", "generic_name", "active_ingredients")
            )

            # FDA data sets brand_name = generic_name for plain generics, so
            # skip those -- only a brand_name that actually differs from the
            # generic name represents a real trade name (e.g. "Valium").
            brands = {
                  brand_name
                  for brand_name, generic_name, ingredients in candidates
                  if brand_name.strip().upper() != (generic_name or "").strip().upper()
                     and self._ingredient_name_set(ingredients) == self_names
            }
            return " && ".join(sorted(brands))

      @property
      def dailymed_url (self) -> str:
            """Outgoing DailyMed URL for this product, for use as a detail-page href."""
            return get_dailymed_url(self.product_ndc)


class ListedIngredient(models.Model):
      product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="ingredients", editable=False)
      name = models.CharField(max_length=400)
      strength = models.CharField(max_length=300)
      unit = models.CharField(max_length=300)

      class Meta:
            ordering = ["name"]

      def __str__ (self):
            return f"{self.name} {self.strength} {self.unit}"

      def normalize_strength (self):
            try:
                  if re.match(r"^\.\d+", self.strength):
                        self.strength = "0" + self.strength
            except Exception:
                  pass

      def save (self, *args, **kwargs):
            self.normalize_strength()
            super().save(*args, **kwargs)


class ProductComponent(models.Model):
      """A distinct sub-item of a KIT product (e.g. one drug in a Z-Pak).

      Unlike `ListedIngredient`, which assumes all of a product's ingredients
      belong to one strength profile, each `ProductComponent` is its own
      drug with its own `active_ingredients`. Populated from the member
      SCD/SBD concepts of a resolved RxNorm BPCK/GPCK pack, since FDA's NDC
      ingredient data does not group ingredients by kit component.
      """

      product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="components")
      sequence = models.PositiveIntegerField(default=0)
      rxnorm_concept = models.ForeignKey(RxNormConcept, on_delete=models.SET_NULL, null=True, blank=True)
      name = models.CharField(max_length=400)
      active_ingredients = models.JSONField(default=list)  # List of ingredient dicts
      quantity = models.CharField(max_length=100, blank=True)  # e.g. "6 TABLET"

      class Meta:
            ordering = ["sequence"]
            db_table = "rxocrpl_productcomponent"

      def __str__ (self):
            return self.name


class PackagedProduct(ComputedFieldsModel):
      """Packaged product details."""

      product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="packaged_products")
      package_code = models.CharField(max_length=2)
      description = models.CharField(max_length=400)
      active = models.BooleanField(default=True)
      package_ndc = ComputedField(
            models.CharField(max_length=13, unique=True),
            depends=[("product", ["product_ndc"]), ("package_code", [])],
            compute=lambda self: f"{self.product.product_ndc}-{self.package_code}",
      )

      def __str__ (self):
            return f"{self.product.generic_name} - {self.description} ({self.package_ndc})"

      SEGMENT_RE = re.compile(
            r"^\s*(?P<qty>[\d.]+)\s+(?P<unit>.+?)\s+in\s+(?P<count>\d+)\s+(?P<container>.+?)"
            r"(?:\s+\((?P<ndc>[\d-]+)\))?\s*$"
      )

      def parse_package_description (self):
            """
            Parse a nested NDC package description into an ordered list of levels,
            outermost first.

            Returns a list of dicts: {qty, unit, count, container, ndc}
            Returns None if any segment fails to match.
            """
            levels = []
            for raw in self.description.split("/"):
                  m = self.SEGMENT_RE.match(raw)
                  if not m:
                        return None
                  d = m.groupdict()
                  d["qty"] = float(d["qty"]) if "." in d["qty"] else int(d["qty"])
                  d["count"] = int(d["count"])
                  levels.append(d)
            return levels


class CspOrder(models.Model):
      """Pharmacy preparation order."""

      id = models.AutoField(primary_key=True)
      reference_image = models.FileField(upload_to="orders/reference/", null=True, blank=True)
      certified_subset = models.BooleanField(default=False)
      scanned_barcodes = models.JSONField(default=list, blank=True)
      expected_components = models.JSONField(default=list, blank=True)
      created_at = models.DateTimeField(auto_now_add=True)

      def __str__ (self):
            return f"Order {self.id} ({self.created_at})"

      class Meta:
            verbose_name = "CSP Order"
            verbose_name_plural = "CSP Orders"


class VerificationImage(models.Model):
      """Additional images verified against the reference inventory."""

      order = models.ForeignKey(CspOrder, related_name="verification_images", on_delete=models.CASCADE)
      image = models.FileField(upload_to="orders/verification/")

      def __str__ (self):
            return f"Verification image for Order {self.order.id}"


class Component(models.Model):
      """A specific drug component within an order."""

      order = models.ForeignKey(CspOrder, related_name="components", on_delete=models.CASCADE)
      product = models.ForeignKey(Product, on_delete=models.PROTECT)
      # Quantities stored as magnitude + unit string (matching quantities.py logic)
      numerator_mag = models.DecimalField(max_digits=12, decimal_places=4)
      numerator_unit = models.CharField(max_length=20)
      denominator_mag = models.DecimalField(max_digits=12, decimal_places=4)
      denominator_unit = models.CharField(max_length=20)

      quantity = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)])
      lot = models.CharField(max_length=100, null=True, blank=True)
      exp = models.CharField(max_length=100, null=True, blank=True)
      package_ndc = models.CharField(max_length=20, null=True, blank=True)

      def __str__ (self):
            return f"{self.product.generic_name} in Order {self.order.id}"

      @property
      def numerator (self):
            return Quantity(self.numerator_mag, self.numerator_unit)

      @numerator.setter
      def numerator (self, q):
            self.numerator_mag = q.value
            self.numerator_unit = q.unit

      @property
      def denominator (self):
            return Quantity(self.denominator_mag, self.denominator_unit)

      @denominator.setter
      def denominator (self, q):
            self.denominator_mag = q.value
            self.denominator_unit = q.unit

      @property
      def concentration (self) -> Quantity:
            return self.get_concentration()

      def get_concentration (self) -> Quantity:
            return self.numerator / self.denominator

      def concentration_str (self) -> str:
            return f"{self.numerator}/{self.denominator}"

      def describe (self) -> str:
            parts = [self.product.generic_name, self.concentration_str()]
            if self.product.dosage_form:
                  parts.append(self.product.dosage_form)
            if self.quantity != 1:
                  parts.append(f"x{self.quantity}")
            return " ".join(parts)

      def to_dict (self) -> dict[str, Any]:
            return {
                  "product": self.product.to_dict() if self.product else None,
                  "numerator": str(self.numerator),
                  "denominator": str(self.denominator),
                  "quantity": self.quantity,
                  "lot": self.lot,
                  "exp": self.exp,
                  "package_ndc": self.package_ndc,
            }

      def matches_ndc (self, ndc: str) -> bool:
            if self.package_ndc and self.package_ndc == ndc:
                  return True
            if self.product.product_ndc and self.product.product_ndc == ndc:
                  return True
            return False


class OCRFields(models.Model):
      """Fields extracted from an image crop via the OCR pipeline."""

      # This might be linked to a specific detection in a PipelineResult,
      # but as a base model it stores the extracted drug attributes.
      lot = models.CharField(max_length=100, null=True, blank=True)
      exp = models.CharField(max_length=100, null=True, blank=True)
      ndc = models.CharField(max_length=20, null=True, blank=True)
      barcode_ndc = models.CharField(max_length=20, null=True, blank=True)
      mfg = models.CharField(max_length=100, null=True, blank=True)
      product = models.CharField(max_length=100, null=True, blank=True)
      brand = models.CharField(max_length=100, null=True, blank=True)
      strength = models.CharField(max_length=100, null=True, blank=True)
      vol_ml = models.CharField(max_length=50, null=True, blank=True)
      mdv = models.CharField(max_length=10, null=True, blank=True)
      instructions = models.CharField(max_length=100, null=True, blank=True)
      # Confidences
      lot_confidence = models.FloatField(default=0.0)
      exp_confidence = models.FloatField(default=0.0)
      ndc_confidence = models.FloatField(default=0.0)
      barcode_ndc_confidence = models.FloatField(default=0.0)
      mfg_confidence = models.FloatField(default=0.0)
      product_confidence = models.FloatField(default=0.0)
      brand_confidence = models.FloatField(default=0.0)
      strength_confidence = models.FloatField(default=0.0)
      vol_ml_confidence = models.FloatField(default=0.0)
      mdv_confidence = models.FloatField(default=0.0)
      instructions_confidence = models.FloatField(default=0.0)

      raw_texts = models.JSONField(default=list)

      def __str__ (self):
            return f"OCR results (NDC: {self.ndc or self.barcode_ndc})"


class Organization(models.Model):
      """Top-level organizational entity."""

      name = models.CharField(max_length=255)

      def __str__ (self):
            return self.name


class Role(models.Model):
      """Lookup table for roles (e.g. org_admin, facility_admin, pharmacist, etc.)."""

      name = models.CharField(max_length=50, unique=True)
      description = models.TextField(blank=True)

      def __str__ (self):
            return self.name


class Facility(models.Model):
      """Pharmacy facility or hospital unit (CareLocation)."""

      name = models.CharField(max_length=255)
      organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="facilities",
                                       null=True, blank=True, )
      parent = models.ForeignKey("self", on_delete=models.CASCADE, null=True, blank=True, related_name="children")
      facility_type = models.CharField(max_length=100, null=True, blank=True)
      org = models.CharField(max_length=255, null=True, blank=True, help_text="Legacy organization field")
      admin_id = models.CharField(max_length=100, null=True, blank=True)

      class Meta:
            verbose_name_plural = "Facilities"

      def __str__ (self):
            return self.name

      @property
      def current_admins (self):
            """Returns users with an active 'facility_admin' role for this facility."""
            from django.contrib.auth import get_user_model

            return (
                  get_user_model().objects.filter(
                        role_grants__facility=self,
                        role_grants__role__name="facility_admin",
                        role_grants__revoked_at__isnull=True,
                  )
                  .exclude(role_grants__expires_at__lt=timezone.now())
                  .distinct()
            )


class RoleGrant(models.Model):
      """
      Append-only grant ledger for admin permissions.
      A new record per permission change, never edited or deleted, only closed out.
      """

      user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="role_grants")
      role = models.ForeignKey(Role, on_delete=models.CASCADE)
      # Scope: org_id OR facility_id, never both (enforce via check constraint / clean())
      organization = models.ForeignKey(Organization, on_delete=models.CASCADE, null=True, blank=True)
      facility = models.ForeignKey(Facility, on_delete=models.CASCADE, null=True, blank=True)
      granted_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name="granted_permissions", )
      granted_at = models.DateTimeField(auto_now_add=True)
      revoked_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name="revoked_permissions", )
      revoked_at = models.DateTimeField(null=True, blank=True)
      expires_at = models.DateTimeField(null=True, blank=True, help_text="Optional, for time-boxed elevated access")
      reason = models.TextField(blank=True, help_text="Optional. Must be 'system_bootstrap' when granted_by is null.", )

      class Meta:
            constraints = [
                  models.CheckConstraint(
                        condition=(
                                  (
                                            models.Q(organization__isnull=False)
                                            & models.Q(facility__isnull=True)
                                  )
                                  | (
                                            models.Q(organization__isnull=True)
                                            & models.Q(facility__isnull=False)
                                  )
                        ),
                        name="grant_scope_exclusive_check",
                  )
            ]

      def clean (self):
            # Enforce scope: org_id OR facility_id, never both
            if self.organization and self.facility:
                  raise ValidationError(
                        "A grant cannot have both an organization and a facility scope."
                  )
            if not self.organization and not self.facility:
                  raise ValidationError(
                        "A grant must have either an organization or a facility scope."
                  )

            # Bootstrap case: granted_by is null only when reason is 'system_bootstrap'
            if self.granted_by is None and self.reason != "system_bootstrap":
                  raise ValidationError(
                        "granted_by can only be null if reason is 'system_bootstrap'."
                  )

      def save (self, *args, **kwargs):
            is_new = self._state.adding
            self.full_clean()

            # Authorization check on write: before inserting a grant, verify granter holds an active grant
            if self.granted_by and not kwargs.get("force_bootstrap", False):
                  if not self._granter_has_authority():
                        raise ValidationError(
                              "Granter does not hold an active grant at this scope or an ancestor scope."
                        )

            super().save(*args, **kwargs)

            if is_new:
                  RoleGrantEvent.objects.create(
                        grant=self,
                        event_type="grant_created",
                        actor=self.granted_by,
                        notes=f"Initial grant. Reason: {self.reason}",
                  )

      def revoke (self, revoked_by, reason: str = ""):
            """Closes out the grant record (append-only principle)."""
            self.revoked_by = revoked_by
            self.revoked_at = timezone.now()
            self.save()
            RoleGrantEvent.objects.create(
                  grant=self, event_type="grant_revoked", actor=revoked_by, notes=reason
            )

      def _granter_has_authority (self) -> bool:
            """
            Verify granted_by holds an active grant at that scope or an ancestor scope.
            Ideally this would check for a specific 'admin' role, but here we check for any active grant.
            """
            # To avoid infinite recursion or complexity, we use the User.has_role helper
            # or a simplified version of it.
            # Assuming 'org_admin' or 'facility_admin' roles are required.
            return self.granted_by.has_role(
                  "org_admin", organization=self.organization, facility=self.facility
            ) or self.granted_by.has_role("facility_admin", facility=self.facility)

      @property
      def is_active (self) -> bool:
            now = timezone.now()
            return self.revoked_at is None and (
                      self.expires_at is None or self.expires_at > now
            )

      def __str__ (self):
            scope = self.organization or self.facility
            return f"{self.user} granted {self.role} at {scope}"


class RoleGrantEvent(models.Model):
      """Finer-grained audit detail for the grant lifecycle."""

      grant = models.ForeignKey(
            RoleGrant, on_delete=models.CASCADE, related_name="events"
      )
      event_type = models.CharField(
            max_length=50
      )  # e.g., 'denied_attempt', 'auto_expiry', 'annotation'
      timestamp = models.DateTimeField(auto_now_add=True)
      actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
      notes = models.TextField(blank=True)

      def __str__ (self):
            return f"{self.event_type} on {self.grant.id} at {self.timestamp}"


class ApprovedProductReconstitutionScheme(models.Model):
      """Approved reconstitution parameters for specific products at a facility."""

      product_ndcs = models.JSONField(default=list)  # List of NDCs this scheme applies to
      concept = models.ForeignKey(RxNormConcept, on_delete=models.SET_NULL, null=True, blank=True)
      facility = models.ForeignKey(Facility, on_delete=models.CASCADE)
      user = models.ForeignKey(User, on_delete=models.CASCADE)
      whole_product_strength_mag = models.DecimalField(max_digits=12, decimal_places=4)
      whole_product_strength_unit = models.CharField(max_length=20)
      approved_diluents = models.JSONField(default=list)
      diluent_volume_ml = models.DecimalField(
            max_digits=12, decimal_places=4, null=True, blank=True
      )
      final_volume_ml = models.DecimalField(
            max_digits=12, decimal_places=4, null=True, blank=True
      )

      def __str__ (self):
            return f"Scheme for {self.product_ndcs} at {self.facility.name}"

      @property
      def whole_product_strength (self):
            return Quantity(
                  self.whole_product_strength_mag, self.whole_product_strength_unit
            )

      @whole_product_strength.setter
      def whole_product_strength (self, q):
            self.whole_product_strength_mag = q.value
            self.whole_product_strength_unit = q.unit


class ProductRxNormMapping(models.Model):
      """NDC to RxNorm RxCUI mapping for faster lookups."""

      product_ndc = models.CharField(max_length=20, primary_key=True)
      rxcui = models.CharField(max_length=20)
      tty = models.CharField(max_length=10, null=True, blank=True)
      name = models.CharField(max_length=255, null=True, blank=True)

      def __str__ (self):
            return f"{self.product_ndc} -> {self.rxcui} ({self.tty})"
