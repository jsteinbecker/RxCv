import logging
import re
from dataclasses import dataclass
from itertools import zip_longest
from typing import Any

from django.db import transaction

from rxocrpl.fda import lookup_ndc_package
from rxocrpl.dailymed_rxnorm_linker import (
      link_product_from_setid_map,
      load_setid_map,
)
from rxocrpl.models import (
      Labeler,
      ListedIngredient,
      Product,
      ProductComponent,
      ProductRxNormMapping,
      RxNormConcept,
)
from rxocrpl.rxgraph.pipeline import add_concept_by_ndc
from rxocrpl.rxnorm.enrichment import get_rxnorm_enrichment
from rxocrpl.rxnorm.parser import parse_rxnorm_string

logger = logging.getLogger(__name__)

# Ordered so more specific dose-form keywords (e.g. "Injectable Suspension")
# are checked before broader ones. RxNorm dose forms don't carry a route
# field of their own, so route is inferred from the words in the dose form.
_DOSE_FORM_ROUTE_KEYWORDS = [
      ("Ophthalmic", "OPHTHALMIC"),
      ("Otic", "OTIC"),
      ("Nasal", "NASAL"),
      ("Inhalant", "RESPIRATORY (INHALATION)"),
      ("Inhaler", "RESPIRATORY (INHALATION)"),
      ("Rectal", "RECTAL"),
      ("Vaginal", "VAGINAL"),
      ("Buccal", "BUCCAL"),
      ("Sublingual", "SUBLINGUAL"),
      ("Transdermal", "TRANSDERMAL"),
      ("Topical", "TOPICAL"),
      ("Injectable", "INTRAVENOUS"),
      ("Injection", "INTRAVENOUS"),
      ("Prefilled Syringe", "INTRAVENOUS"),
      ("Oral", "ORAL"),
]

STRENGTH_RE = re.compile(r"^\s*(?P<strength>\d*\.\d+|\d+(?:\.\d+)?)\s*(?P<unit>.*?)\s*$")

_KIT_COMPONENT_SEGMENT_RE = re.compile(
      r"(?P<qty>[\d.]+)\s+(?P<unit>[A-Za-z%]+)\s+in\s+(?P<count>\d+)\s+"
      r"(?P<container>[A-Za-z][A-Za-z,\- ]*?)\s*\(\s*(?P<ndc>\d{4,5}-\d{3,4}-\d{1,2})\s*\)"
)


@dataclass(frozen=True, slots=True)
class IngredientData:
      name: str
      strength: str
      unit: str

      def as_dict (self) -> dict[str, str]:
            return {
                  "name": self.name,
                  "strength": self.strength,
                  "unit": self.unit,
            }


def normalize_ndc (ndc: str) -> str:
      """
      Perform basic NDC cleanup without changing segment padding.

      More aggressive normalization should be handled by the FDA/RxNorm lookup
      layer because converting 10-digit NDCs to 11 digits is format-dependent.
      """
      return ndc.strip()


def get_labeler_code (ndc: str) -> str:
      return ndc.split("-", maxsplit=1)[0]


def parse_strength (strength_str: str | None) -> tuple[str, str]:
      """
      Split a strength string into its numeric strength and unit.

      Examples:
          "9 g/1000mL" -> ("9", "g/1000mL")
          "2.5 mg/mL"  -> ("2.5", "mg/mL")
      """
      if not strength_str:
            return "", ""

      value = strength_str.strip()
      match = STRENGTH_RE.fullmatch(value)

      if not match:
            return value, ""

      return (
            match.group("strength"),
            match.group("unit").strip(),
      )


def ingredients_from_fda_product (fda_product: Any) -> list[IngredientData]:
      ingredients: list[IngredientData] = []

      for ingredient in fda_product.active_ingredients or []:
            strength, unit = parse_strength(ingredient.strength)

            ingredients.append(
                  IngredientData(
                        name=(ingredient.name or "").strip(),
                        strength=strength,
                        unit=unit,
                  )
            )

      return ingredients


def ingredients_from_ndc_entry (entry: Any) -> list[IngredientData]:
      """
      Build ingredient data from a local NDC directory entry.

      zip_longest prevents malformed source rows from silently dropping values
      when the source arrays have unequal lengths.
      """
      ingredients: list[IngredientData] = []

      for name, strength, unit in zip_longest(
                entry.substances or [],
                entry.strengths or [],
                entry.units or [],
                fillvalue="",
      ):
            ingredients.append(
                  IngredientData(
                        name=(name or "").strip(),
                        strength=str(strength or "").strip(),
                        unit=(unit or "").strip(),
                  )
            )

      return ingredients


def route_from_dose_form (dose_form: str | None) -> list[str]:
      if not dose_form:
            return []

      for keyword, route in _DOSE_FORM_ROUTE_KEYWORDS:
            if keyword.lower() in dose_form.lower():
                  return [route]

      return []


def ingredients_from_generic_name (generic_name: str) -> list[IngredientData]:
      parsed = parse_rxnorm_string(generic_name)

      ingredients: list[IngredientData] = []
      for component in parsed.components:
            unit = component.strength_unit or ""
            if component.denom_unit:
                  denom_num = f"{component.denom_num} " if component.denom_num else ""
                  unit = f"{unit}/{denom_num}{component.denom_unit}"

            ingredients.append(
                  IngredientData(
                        name=component.ingredient.strip(),
                        strength=component.strength_num or "",
                        unit=unit,
                  )
            )

      return ingredients


def fallback_from_generic_name (
          generic_name: str | None,
) -> tuple[list[IngredientData], str, list[str]]:
      """
      Last-resort recovery of ingredients, dosage form, and route from a
      generic_name string when FDA/RxNorm returned none of them.

      Only works when generic_name follows the normalized RxNorm naming
      convention (e.g. "Metformin 500 MG Oral Tablet"); anything else parses
      to empty results and is simply not used.
      """
      if not generic_name:
            return [], "", []

      parsed = parse_rxnorm_string(generic_name)
      if parsed.is_pack:
            return [], "", []

      dosage_form = parsed.dose_form or ""
      route = route_from_dose_form(dosage_form)
      ingredients = ingredients_from_generic_name(generic_name)
      return ingredients, dosage_form, route


def update_product_ingredients (
          product: Product,
          ingredients: list[IngredientData],
) -> None:
      product.ingredients.all().delete()  # ty: ignore[unresolved-attribute]

      if not ingredients:
            return

      ListedIngredient.objects.bulk_create(
            [
                  ListedIngredient(
                        product=product,
                        name=ingredient.name,
                        strength=ingredient.strength,
                        unit=ingredient.unit,
                  )
                  for ingredient in ingredients
            ]
      )


def is_kit_dosage_form (dosage_form: str | None) -> bool:
      return (dosage_form or "").strip().upper() == "KIT"


def extract_kit_component_ndcs (fda_product: Any) -> list[tuple[str, str]]:
      """
      Extract a KIT's nested component NDCs (and their pack quantity) from its
      openFDA packaging descriptions.

      A KIT's packaging description enumerates every nested item, e.g.
      "1 KIT in 1 KIT (85766-065-01) / 1 mL in 1 VIAL (0378-8065-32) / 1 mL in
      1 SYRINGE (0378-8066-32)". The first NDC always shares the KIT's own
      product NDC -- that's just the kit's outer packaging, not a component --
      so it's excluded; every other embedded NDC identifies an actual bundled
      item. Returns a list of (ndc, quantity) tuples in description order,
      e.g. [("0378-8065-32", "1 VIAL"), ("0378-8066-32", "1 SYRINGE")].
      """
      own_product_ndc = fda_product.product_ndc or ""
      seen: set[str] = set()
      components: list[tuple[str, str]] = []

      for packaging in fda_product.packaging or []:
            for match in _KIT_COMPONENT_SEGMENT_RE.finditer(packaging.description or ""):
                  ndc = match.group("ndc")
                  if ndc in seen:
                        continue
                  product_portion = "-".join(ndc.split("-")[:2])
                  if product_portion == own_product_ndc:
                        continue
                  seen.add(ndc)
                  quantity = f"{match.group('count')} {match.group('container')}".strip()
                  components.append((ndc, quantity))

      return components


def resolve_kit_component_ndc (ndc: str) -> dict[str, Any] | None:
      """
      Resolve one of a KIT's embedded component NDCs to component data.

      openFDA is tried first since it carries structured ingredient/strength
      data. RxNorm/RxNav is only queried when openFDA has no record for the
      NDC -- common for devices, diluents, or older repackaged items that
      never made it into the NDC directory. Returns None if neither source
      knows the NDC.
      """
      fda_results = lookup_ndc_package(ndc)
      if fda_results:
            fda_component = fda_results[0]
            ingredients = ingredients_from_fda_product(fda_component)
            return {
                  "rxcui": None,
                  "name": fda_component.generic_name or fda_component.brand_name or "",
                  "active_ingredients": [ingredient.as_dict() for ingredient in ingredients],
            }

      enrichment = get_rxnorm_enrichment(ndc)
      if enrichment.concept:
            return {
                  "rxcui": enrichment.concept_rxcui,
                  "name": enrichment.concept.name or "",
                  "active_ingredients": [],
            }

      return None


def build_kit_components_from_fda_product (fda_product: Any) -> list[dict[str, Any]]:
      """
      Resolve a KIT's member products from its own packaging description.

      Returns a list of plain dicts (no DB writes) -- see
      `save_kit_components` for persistence. Returns [] when the packaging
      description has no resolvable component NDCs.
      """
      components: list[dict[str, Any]] = []
      for sequence, (ndc, quantity) in enumerate(extract_kit_component_ndcs(fda_product)):
            resolved = resolve_kit_component_ndc(ndc)
            if resolved is None:
                  continue
            resolved["sequence"] = sequence
            resolved["quantity"] = quantity
            components.append(resolved)
      return components


def build_kit_components (ndc: str) -> list[dict[str, Any]]:
      """Fetch *ndc*'s own openFDA record and resolve its KIT components."""
      fda_results = lookup_ndc_package(ndc)
      if not fda_results:
            return []
      return build_kit_components_from_fda_product(fda_results[0])


def save_kit_components (product: Product, components: list[dict[str, Any]]) -> None:
      product.components.all().delete()  # ty: ignore[unresolved-attribute]

      if not components:
            return

      rxcuis = {c["rxcui"] for c in components if c["rxcui"]}
      concepts_by_rxcui = (
            {c.rxcui: c for c in RxNormConcept.objects.filter(rxcui__in=rxcuis)}
            if rxcuis
            else {}
      )

      ProductComponent.objects.bulk_create(
            [
                  ProductComponent(
                        product=product,
                        sequence=component["sequence"],
                        rxnorm_concept=concepts_by_rxcui.get(component["rxcui"]),
                        name=component["name"],
                        active_ingredients=component["active_ingredients"],
                        quantity=component["quantity"],
                  )
                  for component in components
            ]
      )


def get_or_update_labeler (
          *,
          ndc: str,
          labeler_name: str | None,
) -> Labeler | None:
      labeler_name = (labeler_name or "").strip()

      if not labeler_name:
            return None

      labeler, _ = Labeler.objects.update_or_create(
            labeler_code=get_labeler_code(ndc),
            defaults={"name": labeler_name},
      )
      return labeler


def update_product_from_ndc_entry (
          product: Product,
          entry: Any,
) -> Product:
      """Update a Product from a local NDC directory entry."""
      is_kit = is_kit_dosage_form(entry.dosage_form)
      ingredients = [] if is_kit else ingredients_from_ndc_entry(entry)
      if not is_kit:
            print(ingredients or "No ingredients found for NDC entry:", entry)

      # External FDA/RxNorm calls happen before opening the DB transaction.
      kit_components = build_kit_components(product.product_ndc) if is_kit else []

      with transaction.atomic():
            labeler = get_or_update_labeler(
                  ndc=product.product_ndc,
                  labeler_name=entry.labeler,
            )

            product.generic_name = entry.nonproprietary_name or ""
            product.brand_name = entry.proprietary_name
            product.dosage_form = entry.dosage_form or ""
            product.route = (
                  [route.strip() for route in entry.route.split(";") if route.strip()]
                  if entry.route
                  else []
            )
            product.active_ingredients = [
                  ingredient.as_dict() for ingredient in ingredients
            ]
            product.labeler_name = entry.labeler or ""
            product.labeler = labeler

            product.save(
                  update_fields=[
                        "generic_name",
                        "brand_name",
                        "dosage_form",
                        "route",
                        "active_ingredients",
                        "labeler_name",
                        "labeler",
                  ]
            )

            update_product_ingredients(product, ingredients)
            if is_kit:
                  save_kit_components(product, kit_components)

      return product


def sync_product_from_external_sources (ndc: str) -> Product | None:
      """
      Synchronize a product from FDA and RxNorm data.

      External calls are performed outside the database transaction so slow
      network or graph operations do not hold database locks.
      """
      ndc = normalize_ndc(ndc)

      # External requests should happen before opening the DB transaction.
      fda_results = lookup_ndc_package(ndc, fetch_rxcui=True)
      if not fda_results:
            return None

      fda_product = fda_results[0]
      is_kit = is_kit_dosage_form(fda_product.dosage_form)
      ingredients = [] if is_kit else ingredients_from_fda_product(fda_product)
      dosage_form = fda_product.dosage_form or ""
      route = fda_product.route or []

      # If FDA/RxNorm didn't give us ingredients, dosage form, or route,
      # fall back to parsing generic_name -- it's sometimes already in
      # RxNorm's normalized "<ingredient> <strength> <dose form>" shape.
      if not is_kit and (not ingredients or not dosage_form or not route):
            fallback_ingredients, fallback_dosage_form, fallback_route = (
                  fallback_from_generic_name(fda_product.generic_name)
            )
            ingredients = ingredients or fallback_ingredients
            dosage_form = dosage_form or fallback_dosage_form
            route = route or fallback_route

      # This request is also intentionally outside the transaction.
      enrichment = get_rxnorm_enrichment(ndc)

      concept = enrichment.concept if enrichment.concept_rxcui else None
      enrichment_dict = enrichment.to_dict() if enrichment.concept_rxcui else None
      setid_index = None if enrichment.concept_rxcui else load_setid_map()

      # Kit components are resolved from the KIT's own packaging description,
      # not FDA's flat ingredient array -- also outside the transaction.
      kit_components = build_kit_components_from_fda_product(fda_product) if is_kit else []

      with transaction.atomic():
            labeler = get_or_update_labeler(
                  ndc=ndc,
                  labeler_name=fda_product.labeler_name,
            )

            product_defaults = {
                  "generic_name": fda_product.generic_name or "",
                  "brand_name": fda_product.brand_name,
                  "labeler": labeler,
                  "labeler_name": fda_product.labeler_name or "",
                  "dosage_form": dosage_form,
                  "route": route,
                  "active_ingredients": [ingredient.as_dict() for ingredient in ingredients],
            }

            if enrichment_dict is not None:
                  product_defaults["rxcui_mapping"] = enrichment_dict

            product, _ = Product.objects.update_or_create(
                  product_ndc=ndc,
                  defaults=product_defaults,
            )

            if is_kit:
                  save_kit_components(product, kit_components)

            update_product_ingredients(product, ingredients)

            if enrichment.concept_rxcui:
                  ProductRxNormMapping.objects.update_or_create(
                        product_ndc=ndc,
                        defaults={
                              "rxcui": enrichment.concept_rxcui,
                              "tty": (concept.tty if concept else "") or "",
                              "name": (concept.name if concept else "") or "",
                        },
                  )

      # Do not run graph/network work while the DB transaction is open.
      if enrichment.concept_rxcui:
            try:
                  anchor = add_concept_by_ndc(ndc, chain=True)
                  if anchor is not None:
                        product.concepts.add(anchor)
            except Exception:
                  logger.exception(
                        "Error materializing concept graph for NDC %s",
                        ndc,
                  )
      elif setid_index is not None:
            try:
                  result = link_product_from_setid_map(product, setid_index)
                  if result is not None:
                        anchor = add_concept_by_ndc(ndc, chain=True)
                        if anchor is not None:
                              product.concepts.add(anchor)
            except Exception:
                  logger.exception(
                        "Error linking product %s from DailyMed setid map",
                        ndc,
                  )

      return product
