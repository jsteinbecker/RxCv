import re
from django.db import transaction
from rxocrpl.models import Product, ListedIngredient, Labeler, ProductRxNormMapping
from rxocrpl.fda import lookup_ndc_package
from rxocrpl.rxnorm.enrichment import get_rxnorm_enrichment
from rxocrpl.rxgraph.pipeline import add_concept_by_ndc
from rxocrpl.ocr.ndc_directory import get_directory

STRENGTH_RE = re.compile(r"^([\d.]+)\s*(.*)$")


def parse_strength(strength_str):
    """Split a strength string like '9 g/1000mL' into ('9', 'g/1000mL')."""
    if not strength_str:
        return "", ""
    match = STRENGTH_RE.match(strength_str.strip())
    if match:
        return match.group(1), match.group(2).strip()
    return strength_str, ""


def update_product_from_ndc_entry(product, entry):
    """Update a Product model instance from an NDCEntry (local FDA data)."""
    # Prepare active ingredients data
    ingredients_data = []
    # entry.substances, entry.strengths, entry.units are lists
    for name, strength, unit in zip(entry.substances, entry.strengths, entry.units):
        ingredients_data.append({"name": name, "strength": strength, "unit": unit})

    # Update Product fields
    product.generic_name = entry.nonproprietary_name or ""
    product.brand_name = entry.proprietary_name
    product.dosage_form = entry.dosage_form or ""
    product.route = [r.strip() for r in entry.route.split(";")] if entry.route else []
    product.active_ingredients = ingredients_data
    product.labeler_name = entry.labeler or ""

    if entry.labeler:
        labeler_code = product.product_ndc.split("-")[0]
        labeler, _ = Labeler.objects.get_or_create(
            labeler_code=labeler_code, defaults={"name": entry.labeler}
        )
        product.labeler = labeler

    product.save()

    # Update ListedIngredient models
    product.ingredients.all().delete()
    for ing in ingredients_data:
        ListedIngredient.objects.create(
            product=product,
            name=ing["name"],
            strength=ing["strength"],
            unit=ing["unit"],
        )
    return product


def sync_product_from_external_sources(ndc):
    """
    Orchestrate data pulling from FDA and RxNorm for a given NDC.
    Updates Product, ListedIngredient, and ProductRxNormMapping models.
    Materializes the concept graph for identified RxCUIs.
    """
    # 1. Fetch FDA data
    fda_results = lookup_ndc_package(ndc, fetch_rxcui=True)
    if not fda_results:
        # Try to at least get RxNorm data if FDA fails?
        # The requirement says "uses fda.py and rxnorm/enrichment.py".
        return None

    fda_product = fda_results[0]

    with transaction.atomic():
        # 2. Get or create Labeler
        labeler = None
        if fda_product.labeler_name:
            labeler_code = ndc.split("-")[0]
            labeler, _ = Labeler.objects.get_or_create(
                labeler_code=labeler_code, defaults={"name": fda_product.labeler_name}
            )

        # 3. Prepare active ingredients data
        ingredients_data = []
        for ai in fda_product.active_ingredients:
            strength, unit = parse_strength(ai.strength)
            ingredients_data.append(
                {"name": ai.name or "", "strength": strength, "unit": unit}
            )

        # 4. Update or create Product
        product, created = Product.objects.update_or_create(
            product_ndc=ndc,
            defaults={
                "generic_name": fda_product.generic_name or "",
                "brand_name": fda_product.brand_name,
                "labeler": labeler,
                "labeler_name": fda_product.labeler_name or "",
                "dosage_form": fda_product.dosage_form or "",
                "route": fda_product.route or [],
                "active_ingredients": ingredients_data,
            },
        )

        # 5. Update ListedIngredient models (clear and recreate)
        product.ingredients.all().delete()  # ty:ignore[unresolved-attribute]
        for ing in ingredients_data:
            ListedIngredient.objects.create(
                product=product,
                name=ing["name"],
                strength=ing["strength"],
                unit=ing["unit"],
            )

        # 6. Fetch RxNorm data and update mapping
        enrichment = get_rxnorm_enrichment(ndc)
        if enrichment.concept_rxcui:
            product.rxcui_mapping = enrichment.to_dict()
            product.save()

            ProductRxNormMapping.objects.update_or_create(
                product_ndc=ndc,
                defaults={
                    "rxcui": enrichment.concept_rxcui,
                    "tty": (enrichment.concept.tty if enrichment.concept else "") or "",
                    "name": (enrichment.concept.name if enrichment.concept else "")
                    or "",
                },
            )

            # 7. Materialize concept graph
            try:
                add_concept_by_ndc(ndc)
            except Exception as e:
                # Log error but don't fail the whole sync?
                print(f"Error materializing graph for {ndc}: {e}")

    return product
