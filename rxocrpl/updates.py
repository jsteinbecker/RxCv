import re

try:
      from rxocrpl.models import Product, Labeler, PackagedProduct, ListedIngredient
except ImportError:
      try:
            from .models import Product, Labeler, PackagedProduct, ListedIngredient
      except ImportError:
            raise ImportError("Could not import models from rxocrpl.models or .models")

from django.db.models import Q, Count


def rec (x):
      return re.compile(r".*" + re.escape(x) + r".*", re.IGNORECASE)


def update_product_labelers ():
      """
      Update the labeler information for all products in the database.
      """
      products = list(
            Product.objects.filter(labeler__isnull=True).only(
                  "pk", "labeler_name", "product_ndc"
            )
      )
      if not products:
            return

      unique_names = {p.labeler_name for p in products}

      existing = {
            l.verbose_name: l for l in Labeler.objects.filter(verbose_name__in=unique_names)
      }

      missing_names = unique_names - existing.keys()

      # Identifies missing labelers, persists new records, and updates cache
      if missing_names:
            sample_for_name = {}
            for p in products:
                  if (
                            p.labeler_name in missing_names
                            and p.labeler_name not in sample_for_name
                  ):
                        sample_for_name[p.labeler_name] = p

            new_labelers = [
                  Labeler(
                        verbose_name=name,
                        name=name,
                        labeler_code=(
                              sample.product_ndc.split("-")[0].zfill(5)
                              if sample.product_ndc
                              else None
                        ),
                  )
                  for name, sample in sample_for_name.items()
            ]
            Labeler.objects.bulk_create(new_labelers, ignore_conflicts=True)

            existing.update(
                  {
                        l.verbose_name: l
                        for l in Labeler.objects.filter(verbose_name__in=missing_names)
                  }
            )

      for product in products:
            product.labeler = (
                  existing[product.labeler_name] if product.labeler_name in existing else None
            )

      Product.objects.bulk_update(products, ["labeler"], batch_size=500)


def _deactivate_by_product_filter (match_q):
      """
      Shared deactivation routine.

      Given a Q filter identifying irrelevant *products*, this:
        1. Deactivates matching active products.
        2. Deactivates their packaged products.
        3. Deactivates any labelers left with no active products.

      Returns a summary dict of the counts affected.
      """
      # 1. Deactivate all matching products that are currently active
      products_to_deactivate = (
            Product.objects.filter(active=True).filter(match_q).distinct()
      )
      product_ids = list(products_to_deactivate.values_list("pk", flat=True))

      # 2. Deactivate corresponding packaged products
      pkg_to_deactivate = PackagedProduct.objects.filter(
            active=True, product_id__in=product_ids
      )
      pkg_count = pkg_to_deactivate.count()

      Product.objects.filter(pk__in=product_ids).update(active=False)
      pkg_to_deactivate.update(active=False)

      # 3. Deactivate labelers that have no active products left
      labelers_to_deactivate_qs = (
            Labeler.objects.filter(active=True)
            .annotate(
                  total_products=Count("product"),
                  active_products=Count("product", filter=Q(product__active=True)),
            )
            .filter(total_products__gt=0, active_products=0)
      )

      labeler_ids = list(labelers_to_deactivate_qs.values_list("pk", flat=True))
      labelers_count = len(labeler_ids)
      Labeler.objects.filter(pk__in=labeler_ids).update(active=False)

      return {
            "labelers": labelers_count,
            "products": len(product_ids),
            "packaged_products": pkg_count,
      }


def deactivate_hospital_irrelevant_labelers ():
      cosmetic_patterns = [
            # UV filters / sunscreen actives
            r".*Avobenzone.*",
            r".*Octisalate.*",
            r".*Octocrylene.*",
            r".*Oxybenzone.*",
            r".*Zinc Oxide.*",
            r".*Titanium Dioxide.*",
            r".*Homosalate.*",
            r".*Octinoxate.*",
            r".*Ensulizole.*",
            r".*Padimate.*",
            r".*Sulisobenzone.*",
            r".*Meradimate.*",
            r".*Trolamine Salicylate.*",
            r".*Sunscreen.*",
            r".*SPF.*",
            # Hair Regrowth
            r".*Minoxidil.*",
            ".*Hair Oil.*",
            # Antiseptics / hand sanitizer
            r".*Benzethonium Chloride.*",
            r".*Benzalkonium Chloride.*",
            r".*Cetylpyridinium Chloride.*",
            r".*Chlorhexidine.*",
            r".*Triclosan.*",
            r".*Hexylresorcinol.*",
            r".*Hand Sanitizer.*",
            r".*Alcohol.*",
            r".*Isopropyl.*",
            r".*Povidone.Iodine.*",
            # Acne / skin actives
            r".*Salicylic Acid.*",
            r".*Benzoyl Peroxide.*",
            r".*Adapalene.*",
            ".*Hyaluronic Acid.*",
            r".*Glycolic Acid.*",
            r".*Retinol.*",
            r".*Hydroquinone.*",
            r".*Niacinamide.*",
            # Topical anesthetics
            r".*\w+caine.*",
            r".*Pramoxine.*",
            r".*Phenol.*",
            # Skin protectants / emollients / diaper & anti-itch
            r".*Petrolatum.*",
            r".*Dimethicone.*",
            r".*Glycerin.*",
            r".*Lanolin.*",
            r".*Allantoin.*",
            r".*Calamine.*",
            r".*Colloidal Oatmeal.*",
            r".*Cocoa Butter.*",
            r".*Shea Butter.*",
            r".*Aloe.*",
            r".*Urea.*",
            # Anti-itch / topical steroid & antihistamine
            r".*Hydrocortisone.*",
            r".*Diphenhydramine.*",
            # Antifungals (OTC topical)
            r".*Clotrimazole.*",
            r".*Miconazole.*",
            r".*Terbinafine.*",
            r".*Tolnaftate.*",
            r".*Undecylenic Acid.*",
            r".*Ketoconazole.*",
            r".*Butenafine.*",
            r".*Ciclopirox.*",
            r".*Pyrithione.*",
            # Topical analgesics / counterirritants (rubs, patches)
            r".*Menthol.*",
            r".*Methyl Salicylate.*",
            r".*Camphor.*",
            r".*Capsaicin.*",
            r".*Trolamine.*",
            # Oral care
            r".*Fluoride.*",
            r".*Sodium Monofluorophosphate.*",
            r".*Stannous Fluoride.*",
            r".*Potassium Nitrate.*",
            r".*Carbamide Peroxide.*",
            # Eye / nasal / lip OTC
            r".*Tetrahydrozoline.*",
            r".*Naphazoline.*",
            r".*Ketotifen.*",
            r".*Oxymetazoline.*",
            r".*Phenylephrine.*",
            r".*Saline.*",
            r".*Polyethylene Glycol.*",
            r".*Carboxymethylcellulose.*",
            r".*Hypromellose.*",
            # Wart / callus / astringent
            r".*Witch Hazel.*",
            # Antiperspirant actives
            r".*Aluminum Chlorohydrate.*",
            r".*Aluminum Zirconium.*",
      ]

      match_q = Q()
      for p in cosmetic_patterns:
            match_q |= Q(brand_name__iregex=p) | Q(generic_name__iregex=p)

      return _deactivate_by_product_filter(match_q)


def deactivate_homeopathic_labelers ():
      """
      Deactivate labelers that have any product labeled with an ingredient measured
      with a homeopathic unit (ex. [hp_<letter>], etc).
      """

      match_q = Q(active_ingredients__units__icontains="hp_")

      return _deactivate_by_product_filter(match_q)


def move_rxcui_mappings_to_concepts_m2m ():
      """
      Move all existing RxCUI mappings to the new Concept M2M table.
      """
      from rxocrpl.models import RxNormConcept

      # Get all products with an RxCUI mapping
      products_with_rxcui = Product.objects.filter(rxcui_mapping__isnull=False)

      # format: {"rxcui": {"concept": [["2740417", "SCD"]], "drug": [["2740417", "SCD"]], "product": [["2740417", "SCD"]]}, "concept": "<2740417 [SCD]> Capsicum extract 0.005 MG/MG / menthol 0.1 MG/MG Medicated Patch", "concept_rxcui": "2740417", "status": null, "scdc_group": ["2740416", "384557"], "volume_group_key": null, "labeler": {"code": "87502", "name": "Sheng Chang Pharmaceutical Co Ltd Zhongli Factory", "full_name": "Sheng Chang Pharmaceutical Co, Ltd. Zhongli Factory", "source": "local", "codes": ["87502"], "active_rx_product_count": 2, "active_ndc_product_count": 2, "in_rxnorm": true}}
      # get stuff at rxcui.concept.0.0, rxcui.drug.0.0, rxcui.product.0.0, and rxcui.concept_rxcui
      for product in products_with_rxcui:
            rxcui_data = product.rxcui_mapping
            concept_rxcui = rxcui_data.get("concept_rxcui")
            if not concept_rxcui:
                  continue

            # Get or create the RxNormConcept instance
            concept = RxNormConcept.objects.filter(rxcui=concept_rxcui).first()
            if concept:
                  # Add the concept to the product's concepts M2M field
                  product.concepts.add(concept)
