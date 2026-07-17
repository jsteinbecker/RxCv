"""Populate a :class:`~rxocrpl.models.Product` from external drug data sources.

Wraps :meth:`Product.lookup_by_ndc` (openFDA) so the admin "Enrich from
Outside Sources" action can refresh an existing `Product` row in place
rather than getting back a throwaway unsaved instance.
"""
from __future__ import annotations

from .models import Product
from .updates import update_product_labelers

__all__ = ["sync_product_from_external_sources"]


def sync_product_from_external_sources(product_ndc: str) -> Product:
    """Fetch openFDA data for *product_ndc* and upsert the `Product` row.

    Raises ``ValueError`` if openFDA has no record for the NDC — there is
    nothing to populate the product with in that case.
    """
    fetched = Product.lookup_by_ndc(product_ndc)
    if fetched is None:
        raise ValueError(f"no openFDA match found for NDC {product_ndc!r}")

    product, _ = Product.objects.update_or_create(
        product_ndc=product_ndc,
        defaults={
            "generic_name": fetched.generic_name,
            "brand_name": fetched.brand_name,
            "labeler_name": fetched.labeler_name,
            "dosage_form": fetched.dosage_form,
            "route": fetched.route,
            "active_ingredients": fetched.active_ingredients,
        },
    )
    update_product_labelers()
    product.refresh_from_db()
    return product
