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
    ProductRxNormMapping,
)
from rxocrpl.rxgraph.pipeline import add_concept_by_ndc
from rxocrpl.rxnorm.enrichment import get_rxnorm_enrichment


logger = logging.getLogger(__name__)

STRENGTH_RE = re.compile(r"^\s*(?P<strength>\d+(?:\.\d+)?)\s*(?P<unit>.*?)\s*$")


@dataclass(frozen=True, slots=True)
class IngredientData:
    name: str
    strength: str
    unit: str

    def as_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "strength": self.strength,
            "unit": self.unit,
        }


def normalize_ndc(ndc: str) -> str:
    """
    Perform basic NDC cleanup without changing segment padding.

    More aggressive normalization should be handled by the FDA/RxNorm lookup
    layer because converting 10-digit NDCs to 11 digits is format-dependent.
    """
    return ndc.strip()


def get_labeler_code(ndc: str) -> str:
    return ndc.split("-", maxsplit=1)[0]


def parse_strength(strength_str: str | None) -> tuple[str, str]:
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


def ingredients_from_fda_product(fda_product: Any) -> list[IngredientData]:
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


def ingredients_from_ndc_entry(entry: Any) -> list[IngredientData]:
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


def update_product_ingredients(
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


def get_or_update_labeler(
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


def update_product_from_ndc_entry(
    product: Product,
    entry: Any,
) -> Product:
    """Update a Product from a local NDC directory entry."""
    ingredients = ingredients_from_ndc_entry(entry)

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

    return product


def sync_product_from_external_sources(ndc: str) -> Product | None:
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
    ingredients = ingredients_from_fda_product(fda_product)

    # This request is also intentionally outside the transaction.
    enrichment = get_rxnorm_enrichment(ndc)

    concept = enrichment.concept if enrichment.concept_rxcui else None
    enrichment_dict = enrichment.to_dict() if enrichment.concept_rxcui else None
    setid_index = None if enrichment.concept_rxcui else load_setid_map()

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
            "dosage_form": fda_product.dosage_form or "",
            "route": fda_product.route or [],
            "active_ingredients": [ingredient.as_dict() for ingredient in ingredients],
        }

        if enrichment_dict is not None:
            product_defaults["rxcui_mapping"] = enrichment_dict

        product, _ = Product.objects.update_or_create(
            product_ndc=ndc,
            defaults=product_defaults,
        )

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
