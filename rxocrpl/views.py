from rxocrpl.rxnorm_export_import import import_rxnorm_export
import re

from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib import messages
from django.db.models import Q
from django.forms import fields, forms
from django.shortcuts import get_object_or_404, render
from django.views import View

from rxocrpl.dailymed import get_dailymed_url
from rxocrpl.integrity import products_without_ingredients
from rxocrpl.model_classifiers.labeler_classifiers import LabelerClassifier

from .base_views import (
      AjaxTemplateResponseMixin,
      CsvImportView,
      JsonActionView,
      QuerySetActionView,
      SearchableListView,
      SlugDetailView,
      StaticContextView,
      StatsView,
)
from .graph import build_concept_graph_context  # see graph.py
from .integrity import attempt_repair_for_products_without_ingredients
from .models import (
      DoseForm,
      Facility,
      Labeler,
      ListedIngredient,
      PackagedProduct,
      Product,
      ProductRxNormMapping,
      Role,
      RoleGrant,
      RxNormConcept,
      RxNormConceptRelation,
)


# ---------------------------------------------------------------------------
# Dashboards
# ---------------------------------------------------------------------------

class IndexView(StatsView):
      template_name = "rxocrpl/index.html"
      stats = {
            "total_concepts": RxNormConcept,
            "total_products": Product.objects.filter(active=True),
            "total_ingredients": ListedIngredient.objects.filter(product__active=True),
            "products_without_ingredients": products_without_ingredients,
      }


class SiteStatsView(StatsView):
      template_name = "rxocrpl/stats.html"
      stats = {
            "total_concepts": RxNormConcept,
            "total_relations": RxNormConceptRelation,
            "total_products": Product,
            "total_ingredients": ListedIngredient,
            "total_packages": PackagedProduct,
      }


# ---------------------------------------------------------------------------
# Integrity
# ---------------------------------------------------------------------------

class ProductsWithoutIngredientsView(SearchableListView):
      template_name = "rxocrpl/products_without_ingredients.html"
      context_object_name = "products"
      paginate_by = None  # original view showed all

      def get_queryset (self):
            return products_without_ingredients()

      def get_context_data (self, **kwargs):
            context = super().get_context_data(**kwargs)
            context["count"] = context["products"].count()
            return context


class RepairProductsWithoutIngredientsView(QuerySetActionView):
      success_url_name = "rxocrpl:products_without_ingredients"

      def perform_action (self):
            starting = products_without_ingredients().count()
            remaining = attempt_repair_for_products_without_ingredients()
            return starting, remaining.count()

      def get_message (self, result):
            starting, remaining = result
            return (f"Repair attempted. {starting - remaining} products repaired, "
                    f"{remaining} still missing ingredients.")


# ---------------------------------------------------------------------------
# Concept graph
# ---------------------------------------------------------------------------

class ConceptGraphView(AjaxTemplateResponseMixin, SlugDetailView):
      model = RxNormConcept
      lookup_field = "rxcui"
      template_name = "rxocrpl/concept_graph.html"
      context_object_name = "concept"

      def get_context_data (self, **kwargs):
            context = super().get_context_data(**kwargs)
            context.update(build_concept_graph_context(self.object))
            return context

      def get_ajax_payload (self, context):
            return context["graph_data"]


class SyncConceptFromRxNormView(JsonActionView):
      http_method_names = ["post"]

      def perform_action (self, rxcui):
            from rxocrpl.rxgraph.pipeline import materialize_concept
            result = materialize_concept(rxcui)
            return {
                  "status": "ok",
                  "rxcui": rxcui,
                  "concepts": len(result.concepts),
                  "created_concepts": result.created_concepts,
                  "relations": len(result.relations),
                  "created_relations": result.created_relations,
            }


class QuantifiedFormEnrichmentView(JsonActionView):
      def perform_action (self, **kwargs):
            from rxocrpl.enrichment_quant_scd import link_quantified_forms
            return link_quantified_forms(batch_size=1000)


class ConceptTtyListView(SearchableListView):
      model = RxNormConcept
      template_name = "rxocrpl/concept_tty_list.html"
      search_fields = ("name", "rxcui")
      paginate_by = 50
      ordering = "name"

      def get_queryset (self):
            return super().get_queryset().filter(tty=self.kwargs["tty"])

      def get_context_data (self, **kwargs):
            context = super().get_context_data(**kwargs)
            context["tty"] = self.kwargs["tty"]
            context["total"] = RxNormConcept.objects.filter(
                  tty=self.kwargs["tty"]
            ).count()
            return context


# ---------------------------------------------------------------------------
# SCD/NDC lookup helper page
# ---------------------------------------------------------------------------

class ScdLookupForm(forms.Form):
      dfg = fields.ChoiceField(choices=DoseForm.choices, label="Dose Form Group")
      ingr = fields.CharField(max_length=100, label="Ingredient Name")


class ScdNdcLookupView(StaticContextView):
      template_name = "rxocrpl/scd_ndc_lookup.html"
      static_context = {
            "decoded_query": (
                  'rxnorm.findRxcuiByString) (allsrc:"1", search:"9"): idGroup.rxnormId; '
                  'rxnorm.getRelatedByType (expand:"psn", tty:" SCDF SBDF SCDFP SBDFP SCDG SBDG SCDGP"): '
                  'relatedGroup.conceptGroup.conceptProperties.rxcui; rxnorm.getRelatedByType (expand:"", '
                  'tty:" SCD GPCK"): relatedGroup.conceptGroup.conceptProperties.rxcui; rxnorm.getNDCs '
                  "(): ndcGroup.ndcList.ndc"
            )
      }


# ---------------------------------------------------------------------------
# NDC products
# ---------------------------------------------------------------------------

class NdcProductListView(SearchableListView):
      model = Product
      template_name = "rxocrpl/ndc_product_list.html"
      search_fields = ("generic_name", "brand_name", "product_ndc")
      base_filters = {"active": True}
      ordering = "generic_name"
      paginate_by = 25


def _mapping_rows_for_product (product):
      rows = []
      seen = set()

      mapping = ProductRxNormMapping.objects.filter(
            product_ndc=product.product_ndc
      ).first()
      if mapping:
            rows.append({
                  "rxcui": mapping.rxcui,
                  "tty": mapping.tty,
                  "name": mapping.name,
                  "source": "ProductRxNormMapping",
            })
            seen.add(mapping.rxcui)

      payload = product.rxcui_mapping or {}
      if isinstance(payload, dict):
            entries = (
                  payload.values()
                  if any(isinstance(v, dict) for v in payload.values())
                  else [payload]
            )
            for entry in entries:
                  if not isinstance(entry, dict):
                        continue
                  rxcui = (
                            entry.get("rxcui")
                            or entry.get("rxnormId")
                            or entry.get("rxnorm_id")
                  )
                  if not rxcui or rxcui in seen:
                        continue
                  rows.append({
                        "rxcui": rxcui,
                        "tty": entry.get("tty"),
                        "name": entry.get("name") or entry.get("rxstring"),
                        "source": "Product.rxcui_mapping",
                  })
                  seen.add(rxcui)

      return rows


class NdcProductDetailView(SlugDetailView):
      model = Product
      lookup_field = "product_ndc"
      lookup_url_kwarg = "ndc"
      template_name = "rxocrpl/ndc_product_detail.html"
      context_object_name = "product"

      dose_bearing_ttys = {"SCD", "SCDC", "BPCK", "GPCK"}

      def get_context_data (self, **kwargs):
            context = super().get_context_data(**kwargs)
            product = self.object
            ingredients = ListedIngredient.objects.filter(product=product)
            confirmed_mappings = _mapping_rows_for_product(product)
            confirmed_rxcuis = {row["rxcui"] for row in confirmed_mappings}

            strengths = self._product_strengths(ingredients)
            concepts = RxNormConcept.objects.filter(
                  name__icontains=product.generic_name
            )
            filtered = sorted(
                  (
                        c for c in concepts
                        if c.rxcui not in confirmed_rxcuis
                           and self._is_relevant(c, strengths)
                  ),
                  key=lambda c: (c.tty, c.name),
            )
            n = ingredients.count()
            context.update({
                  "packages": PackagedProduct.objects.filter(product=product),
                  "ingredients": ingredients,
                  "confirmed_mappings": confirmed_mappings,
                  "close_concepts": [
                        c for c in filtered
                        if self._concept_ingredient_count(c.name) <= n
                  ],
                  "obscure_concepts": [
                        c for c in filtered
                        if self._concept_ingredient_count(c.name) > n
                  ],
                  "dailymed_url": get_dailymed_url(product.product_ndc),
            })
            return context

      @staticmethod
      def _product_strengths (ingredients):
            strengths = set()
            for ing in ingredients:
                  num = re.match(r"\s*([\d.]+)", ing.strength or "")
                  if num:
                        strengths.add(num.group(1).rstrip("0").rstrip("."))
            return strengths

      def _is_relevant (self, concept, strengths):
            if concept.tty not in self.dose_bearing_ttys:
                  return True
            name_nums = set(re.findall(r"([\d.]+)\s*MG", concept.name, re.IGNORECASE))
            name_nums = {n.rstrip("0").rstrip(".") for n in name_nums}
            return bool(name_nums & strengths)

      @staticmethod
      def _concept_ingredient_count (name):
            base = re.split(r"\d", name)[0]
            return base.count("/") + 1


# ---------------------------------------------------------------------------
# Labelers
# ---------------------------------------------------------------------------

class LabelerListView(SearchableListView):
      model = Labeler
      template_name = "rxocrpl/labeler_list.html"
      search_fields = ("name", "labeler_code")
      base_filters = {"active": True}
      ordering = "name"
      paginate_by = 50


class LabelerDetailView(SlugDetailView):
      model = Labeler
      lookup_field = "labeler_code"
      template_name = "rxocrpl/labeler_detail.html"
      context_object_name = "labeler"

      def get_context_data (self, **kwargs):
            context = super().get_context_data(**kwargs)
            from django.core.paginator import Paginator
            products = Product.objects.filter(labeler=self.object).order_by("generic_name")
            paginator = Paginator(products, 25)
            context["page_obj"] = paginator.get_page(self.request.GET.get("page"))
            return context


class LabelerClassifierView(SearchableListView):
      model = Labeler
      template_name = "rxocrpl/labeler_classifier.html"
      search_fields = ("name",)
      base_filters = {"active": True}
      ordering = "name"
      paginate_by = None

      def get_context_data (self, **kwargs):
            context = super().get_context_data(**kwargs)
            classifier = LabelerClassifier()
            context["results"] = classifier.classify_queryset(
                  context["object_list"]
            )
            return context


# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------

class ImportNdcProductsView(CsvImportView):
      path = "/Users/jts/PycharmProjects/RxCv/rxocrpl/db/ndcproduct.csv"
      success_message = "NDC products imported successfully."

      def handle_row (self, row):
            Product.objects.update_or_create(
                  product_ndc=row["PRODUCTNDC"],
                  defaults={
                        "generic_name": row["NONPROPRIETARYNAME"],
                        "brand_name": row["PROPRIETARYNAME"],
                        "labeler_name": row["LABELERNAME"],
                        "dosage_form": row["DOSAGEFORMNAME"],
                        "route": row["ROUTENAME"],
                        "active_ingredients": [{
                              "ingredient": row["SUBSTANCENAME"],
                              "strength": row["ACTIVE_NUMERATOR_STRENGTH"],
                              "unit": row["ACTIVE_INGRED_UNIT"],
                        }],
                        "rxcui_mapping": {},
                  },
            )


class ImportPackagesView(CsvImportView):
      path = "/Users/jts/PycharmProjects/RxCv/rxocrpl/db/package.txt"
      delimiter = "\t"
      success_message = "NDC packages imported successfully."

      def handle_row (self, row):
            product = Product.objects.filter(
                  product_ndc=row["PRODUCTNDC"]
            ).first()
            if not product:
                  return False
            PackagedProduct.objects.update_or_create(
                  product=product,
                  package_code=row["NDCPACKAGECODE"].strip().split("-")[-1],
                  defaults={"description": row["PACKAGEDESCRIPTION"]},
            )


class IngredientsDictToModelView(JsonActionView):
      """Materialize Product.active_ingredients JSON into ListedIngredient rows."""

      def perform_action (self, **kwargs):
            count = 0
            for product in Product.objects.all():
                  for ingredient in product.active_ingredients or []:
                        ListedIngredient.objects.update_or_create(
                              product=product,
                              name=ingredient.get(
                                    "ingredient", ingredient.get("name")
                              ),
                              defaults={
                                    "strength": ingredient["strength"],
                                    "unit": ingredient["unit"],
                              },
                        )
                        count += 1
            return {
                  "status": "success",
                  "message": "Ingredients imported successfully.",
                  "rows": count,
            }


class ConceptCollectedDataUpdateView(JsonActionView):
      """Update RxNormConcept.collected_data from ProductRxNormMapping and Product.rxcui_mapping."""

      def perform_action (self, **kwargs):
            res = import_rxnorm_export()
            return {
                  "status": "success",
                  "message": "Concept collected_data updated successfully.",
            }


# ---------------------------------------------------------------------------
# Facilities
# ---------------------------------------------------------------------------

class FacilityListView(SearchableListView):
      model = Facility
      template_name = "rxocrpl/facility_list.html"
      ordering = "name"
      paginate_by = None

      def get_queryset (self):
            return (
                  super().get_queryset().select_related("organization", "parent")
            )

      def get_context_data (self, **kwargs):
            context = super().get_context_data(**kwargs)
            context["facility_data"] = [
                  {
                        "facility": f,
                        "admins": (admins := f.current_admins),
                        "is_unclaimed": not admins.exists(),
                  }
                  for f in context["object_list"]
            ]
            return context


class ClaimFacilityView(LoginRequiredMixin, View):
      """Multi-state claim workflow — intentionally left as an explicit View,
      since its branching doesn't fit a list/detail/action primitive."""

      template_name = "rxocrpl/facility_claim.html"

      def _render (self, request, facility, state, **extra):
            return render(
                  request, self.template_name,
                  {"facility": facility, "state": state, **extra},
            )

      def dispatch (self, request, *args, **kwargs):
            self.facility = get_object_or_404(Facility, pk=kwargs["pk"])
            return super().dispatch(request, *args, **kwargs)

      def _gate (self, request):
            admins = self.facility.current_admins
            if admins.exists():
                  return self._render(
                        request, self.facility, "already_claimed", admins=admins
                  )
            if request.user.facility_id and request.user.facility_id != self.facility.pk:
                  return self._render(
                        request, self.facility, "wrong_facility",
                        your_facility=request.user.facility,
                  )
            return None

      def get (self, request, *args, **kwargs):
            return self._gate(request) or self._render(
                  request, self.facility, "confirm",
                  existing_profile=request.user if request.user.facility_id else None,
            )

      def post (self, request, *args, **kwargs):
            gated = self._gate(request)
            if gated:
                  return gated

            if not request.user.facility_id:
                  request.user.facility = self.facility
                  request.user.save(update_fields=["facility"])

            role, _ = Role.objects.get_or_create(
                  name="facility_admin",
                  defaults={"description": "Facility administrator"},
            )
            already_admin = RoleGrant.objects.filter(
                  user=request.user, facility=self.facility,
                  role=role, revoked_at__isnull=True,
            ).exists()
            if not already_admin:
                  RoleGrant(
                        user=request.user,
                        role=role,
                        facility=self.facility,
                        granted_by=None,
                        reason="system_bootstrap",
                  ).save()

            messages.success(request, f"You are now the admin for {self.facility}.")
            return self._render(request, self.facility, "success")
