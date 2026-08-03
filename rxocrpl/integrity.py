"""
Data Integrity Checks for RXOCRPL
"""
from __future__ import annotations

import logging

from django.db.models import QuerySet

from models import RxNormConcept

try:
      from .models import Product
except ImportError as e:
      try:
            from rxocrpl.models import Product
      except ImportError:
            raise ImportError("Could not import required modules. Ensure this script is run within the Django project context.") from e

try:
      from .enrichment_service import IngredientData, sync_product_from_external_sources, update_product_ingredients
except ImportError:
      from rxocrpl.enrichment_service import IngredientData, sync_product_from_external_sources, update_product_ingredients

logger = logging.getLogger(__name__)


def products_without_ingredients () -> QuerySet[Product]:
      """
      Check for products that do not have any associated ingredients.

      Returns:
            QuerySet[Product]: A queryset of products without ingredients.
      """
      return Product.objects.filter(active=True, ingredients__isnull=True).filter(components__isnull=True)


def attempt_repair_for_products_without_ingredients () -> QuerySet[Product]:
      """
      Attempt to repair products that do not have any associated ingredients.

      Products whose `active_ingredients` JSON is already populated (e.g. from
      a bulk import path that only wrote the JSON field) are repaired locally
      by rebuilding their `ListedIngredient` rows from it. Products with no
      ingredient data at all are re-synced from FDA/RxNorm instead. KIT
      products are skipped -- they store ingredients per-component on
      `ProductComponent`, not on `ListedIngredient`, so lacking one is expected
      rather than a data integrity problem.

      Returns:
            QuerySet[Product]: The products that still lack ingredients after
            repair was attempted.
      """
      for product in products_without_ingredients():
            if product.is_kit:
                  continue

            ingredients = [
                  IngredientData(
                        name=(ingredient.get("name") or ingredient.get("ingredient") or "").strip(),
                        strength=str(ingredient.get("strength") or "").strip(),
                        unit=str(ingredient.get("unit") or "").strip(),
                  )
                  for ingredient in product.active_ingredients
            ]
            ingredients = [ingredient for ingredient in ingredients if ingredient.name]

            if ingredients:
                  update_product_ingredients(product, ingredients)
                  continue

            try:
                  sync_product_from_external_sources(product.product_ndc)
            except Exception:
                  logger.exception(
                        "Failed to repair ingredients for product %s from external sources",
                        product.product_ndc,
                  )

      return products_without_ingredients()


# RxNorm dose forms → route(s). Extend as needed for your data.
DOSE_FORM_ROUTES = {
      "Oral Tablet": ["ORAL"],
      "Oral Capsule": ["ORAL"],
      "Oral Solution": ["ORAL"],
      "Oral Suspension": ["ORAL"],
      "Delayed Release Oral Tablet": ["ORAL"],
      "Delayed Release Oral Capsule": ["ORAL"],
      "Extended Release Oral Tablet": ["ORAL"],
      "Extended Release Oral Capsule": ["ORAL"],
      "Disintegrating Oral Tablet": ["ORAL"],
      "Sublingual Tablet": ["SUBLINGUAL"],
      "Sublingual Film": ["SUBLINGUAL"],
      "Buccal Tablet": ["BUCCAL"],
      "Chewable Tablet": ["ORAL"],
      "Injectable Solution": ["INJECTABLE"],
      "Injectable Suspension": ["INJECTABLE"],
      "Injection": ["INJECTABLE"],
      "Prefilled Syringe": ["INJECTABLE"],
      "Auto-Injector": ["INJECTABLE"],
      "Cartridge": ["INJECTABLE"],
      "Pen Injector": ["INJECTABLE"],
      "Topical Cream": ["TOPICAL"],
      "Topical Ointment": ["TOPICAL"],
      "Topical Gel": ["TOPICAL"],
      "Topical Solution": ["TOPICAL"],
      "Topical Lotion": ["TOPICAL"],
      "Transdermal System": ["TRANSDERMAL"],
      "Ophthalmic Solution": ["OPHTHALMIC"],
      "Ophthalmic Suspension": ["OPHTHALMIC"],
      "Ophthalmic Ointment": ["OPHTHALMIC"],
      "Otic Solution": ["OTIC"],
      "Otic Suspension": ["OTIC"],
      "Nasal Spray": ["NASAL"],
      "Metered Dose Inhaler": ["INHALATION"],
      "Dry Powder Inhaler": ["INHALATION"],
      "Inhalation Solution": ["INHALATION"],
      "Inhalation Suspension": ["INHALATION"],
      "Rectal Suppository": ["RECTAL"],
      "Rectal Cream": ["RECTAL"],
      "Vaginal Cream": ["VAGINAL"],
      "Vaginal Tablet": ["VAGINAL"],
      "Vaginal Ring": ["VAGINAL"],
      "Vaginal Insert": ["VAGINAL"],
      "Mucosal Spray": ["MUCOSAL"],
      "Oral Lozenge": ["ORAL"],
      "Granules": ["ORAL"],
      "Oral Powder": ["ORAL"],
}

# Sort longest-first so "Delayed Release Oral Tablet" wins over "Oral Tablet"
_SORTED_FORMS = sorted(DOSE_FORM_ROUTES, key=len, reverse=True)


def route_from_concept_name (name: str) -> list[str]:
      for form in _SORTED_FORMS:
            if name.endswith(form) or f" {form} " in name:  # endswith covers SCD; the contains check covers SBD "[Brand]" suffixes
                  return DOSE_FORM_ROUTES[form]
      return []


def backfill_routes (batch_size: int = 1000, dry_run: bool = False) -> dict:
      qs = (
            Product.objects
            .filter(route=[], concepts__isnull=False)
            .prefetch_related("concepts")
            .distinct()
      )

      updated, unmatched = [], []

      for product in qs.iterator(chunk_size=batch_size):
            routes: set[str] = set()
            concepts: QuerySet[RxNormConcept] = product.concepts.all()
            for concept in concepts:
                  routes.update(route_from_concept_name(concept.name or ""))
            if routes:
                  product.route = sorted(routes)
                  updated.append(product)
            else:
                  unmatched.append(product.pk)

            if len(updated) >= batch_size and not dry_run:
                  Product.objects.bulk_update(updated, ["route"])
                  updated.clear()

      if updated and not dry_run:
            Product.objects.bulk_update(updated, ["route"])

      return {"updated": "flushed via bulk_update", "unmatched_pks": unmatched}
