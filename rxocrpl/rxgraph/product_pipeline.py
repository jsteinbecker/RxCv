from rxocrpl.rxgraph.traversal import clinical_drugs
from typing import Optional
from django.db import transaction
from rxocrpl.models import RxNormConcept, RxNormConceptRelation, Product
from rxocrpl.rxnorm.enrichment import get_rxnorm_enrichment
from rxocrpl.rxgraph.pipeline import materialize_concept
from rxocrpl.rxnorm.client import fetch_ndcs_by_rxcui
from rxocrpl.rxgraph.traversal import dose_form_group


def materialize_product(rxcui: str, ndc: Optional[str] = None) -> Product:
      """
      Materialize a Product model from an RxNorm Concept (rxcui) and an NDC.
      This uses RxNorm graph data and enrichment to populate Product fields.
      If ndc is not provided, it is looked up via RxNorm API, prioritizing SCDG.
      """
      # 1. Materialize the concept and its graph if needed
      materialize_concept(rxcui)

      # Lookup NDC if not provided
      if not ndc:
            concept = RxNormConcept.objects.get(rxcui=rxcui)
            # Try SCDG first
            scd: RxNormConcept | None = clinical_drugs(concept).first()
            if scd:
                  ndcs = fetch_ndcs_by_rxcui(scd.rxcui)
            else:
                  ndcs = fetch_ndcs_by_rxcui(rxcui)

            if not ndcs:
                  raise ValueError(f"No NDC found for RXCUI {rxcui} (SCDG lookup failed)")
            ndc = ndcs[0]  # Take the first one

      # 2. Get enrichment data for the NDC
      enrichment = get_rxnorm_enrichment(ndc)

      # 3. Re-fetch materialized RxNormConcept (concept might have changed if we used SCDG but still need the original RXCUI)
      concept = RxNormConcept.objects.get(rxcui=rxcui)

      # 4. Prepare product data (this is a simplified mapping based on available information)
      # The Product model requires more data than just the concept,
      # so we use whatever is available from enrichment or concept.

      product_data = {
            "generic_name": concept.name,
            "brand_name": None,  # Could be derived from relations
            "labeler_name": "Unknown",  # Need to fetch from FDA or other source
            "dosage_form": "Unknown",  # Could be derived from relations
            "route": [],
            "active_ingredients": [],
            "rxcui_mapping": {ndc: {"rxcui": rxcui, "tty": concept.tty}}
      }

      # Fill ingredients from relations
      ingredients = RxNormConceptRelation.objects.filter(source=concept, rela="has_ingredient")
      for rel in ingredients:
            product_data["active_ingredients"].append({
                  "name": rel.target.name,
                  "rxcui": rel.target.rxcui,
                  "numerator_value": float(rel.numerator_value) if rel.numerator_value else None,
                  "numerator_unit": rel.numerator_unit,
            })

      # 5. Create or update the product
      with transaction.atomic():
            product, created = Product.objects.update_or_create(
                  product_ndc=ndc,
                  defaults=product_data
            )

      return product
