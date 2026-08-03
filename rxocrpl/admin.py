from django import forms
from django.contrib import admin, messages
from django.db.models import Count
from django.urls import reverse

from .enrichment_service import sync_product_from_external_sources
from .models import (
      User,
      ApprovedProductReconstitutionScheme,
      Component,
      CspOrder,
      Facility,
      Labeler,
      ListedIngredient,
      OCRFields,
      Organization,
      PackagedProduct,
      Product,
      ProductComponent,
      ProductRxNormMapping,
      Role,
      RoleGrant,
      RoleGrantEvent,
      RxNormConcept,
      RxNormConceptRelation,
      VerificationImage,
)

admin.site.register(RxNormConceptRelation)
admin.site.register(Role)
admin.site.register(OCRFields)
admin.site.register(ProductRxNormMapping)


class FacilityInline(admin.TabularInline):
      model = Facility
      extra = 0
      fields = ["name", "facility_type"]


class OrganizationRoleGrantInline(admin.TabularInline):
      model = RoleGrant
      extra = 0
      fk_name = "organization"
      readonly_fields = ["granted_at"]
      fields = ["user", "role", "granted_by", "reason", "granted_at", "revoked_at", "expires_at"]


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
      list_display = ["name"]
      inlines = [FacilityInline, OrganizationRoleGrantInline]


class FacilityRoleGrantInline(admin.TabularInline):
      model = RoleGrant
      extra = 0
      fk_name = "facility"
      readonly_fields = ["granted_at"]
      fields = ["user", "role", "granted_by", "reason", "granted_at", "revoked_at", "expires_at"]


@admin.register(Facility)
class FacilityAdmin(admin.ModelAdmin):
      list_display = ["name", "organization", "parent", "facility_type", "admin_count", "has_admins"]
      list_filter = ["organization", "facility_type"]
      search_fields = ["name"]
      inlines = [FacilityRoleGrantInline]
      actions = ["claim_as_admin"]

      @admin.display(description="Admins", ordering="name")
      def admin_count (self, obj):
            return obj.current_admins.count()

      @admin.display(boolean=True, description="Has Admin")
      def has_admins (self, obj):
            return obj.current_admins.exists()

      @admin.action(description="Claim selected facilities as admin (bootstrap)")
      def claim_as_admin (self, request, queryset):
            claimed, skipped = [], []
            for facility in queryset:
                  if facility.current_admins.exists():
                        skipped.append(f"{facility} (already has admins)")
                        continue
                  if request.user.facility_id and request.user.facility_id != facility.pk:
                        skipped.append(f"{facility} (you are assigned to a different facility)")
                        continue
                  if not request.user.facility_id:
                        request.user.facility = facility
                        request.user.save(update_fields=["facility"])
                  role, _ = Role.objects.get_or_create(
                        name="facility_admin",
                        defaults={"description": "Facility administrator"},
                  )
                  already = RoleGrant.objects.filter(
                        user=request.user, facility=facility, role=role, revoked_at__isnull=True
                  ).exists()
                  if already:
                        claimed.append(f"{facility} (already your facility)")
                        continue
                  grant = RoleGrant(
                        user=request.user,
                        role=role,
                        facility=facility,
                        granted_by=None,
                        reason="system_bootstrap",
                  )
                  grant.save()
                  claimed.append(str(facility))

            if claimed:
                  self.message_user(request, f"Claimed admin for: {', '.join(claimed)}")
            if skipped:
                  self.message_user(request, f"Skipped: {', '.join(skipped)}", level=messages.WARNING)


class RoleGrantInline(admin.TabularInline):
      model = RoleGrant
      extra = 0
      fk_name = "user"
      readonly_fields = ["granted_at"]
      fields = [
            "role",
            "organization",
            "facility",
            "granted_by",
            "reason",
            "granted_at",
            "revoked_at",
            "expires_at",
      ]


# @admin.register(User)
# class UserAdmin(admin.ModelAdmin):
#       list_display = ["name", "auth_user", "facility", "user_type", "is_admin"]
#       list_filter = ["facility", "user_type"]
#       search_fields = ["name", "auth_user__username", "auth_user__email"]
#       raw_id_fields = ["auth_user"]
#       inlines = [RoleGrantInline]
#
#       @admin.display(boolean=True, description="Admin")
#       def is_admin (self, obj) -> bool:
#             return obj.is_admin


class RoleGrantEventInline(admin.TabularInline):
      model = RoleGrantEvent
      extra = 0
      readonly_fields = ["event_type", "timestamp", "actor", "notes"]
      can_delete = False

      def has_add_permission (self, request, obj=None):
            return False


@admin.register(RoleGrant)
class RoleGrantAdmin(admin.ModelAdmin):
      list_display = [
            "user",
            "role",
            "scope",
            "granted_by",
            "granted_at",
            "revoked_at",
            "is_active",
      ]
      list_filter = ["role", "granted_at", "revoked_at", "organization", "facility"]
      search_fields = ["user__name", "role__name"]
      readonly_fields = ["granted_at"]
      inlines = [RoleGrantEventInline]

      def scope (self, obj):
            return obj.organization or obj.facility

      def is_active (self, obj):
            return obj.is_active

      is_active.boolean = True  # ty:ignore[unresolved-attribute]


@admin.register(RoleGrantEvent)
class RoleGrantEventAdmin(admin.ModelAdmin):
      list_display = ["grant", "event_type", "timestamp", "actor"]
      list_filter = ["event_type", "timestamp"]
      readonly_fields = ["grant", "event_type", "timestamp", "actor", "notes"]

      def has_add_permission (self, request):
            return False

      def has_change_permission (self, request, obj=None):
            return False

      def has_delete_permission (self, request, obj=None):
            return False


class ComponentInline(admin.TabularInline):
      model = Component
      extra = 0
      autocomplete_fields = ["product"]


class VerificationImageInline(admin.TabularInline):
      model = VerificationImage
      extra = 0


@admin.register(CspOrder)
class CspOrderAdmin(admin.ModelAdmin):
      list_display = ["id", "created_at", "certified_subset"]
      list_filter = ["created_at", "certified_subset"]
      inlines = [ComponentInline, VerificationImageInline]


@admin.register(ApprovedProductReconstitutionScheme)
class ApprovedProductReconstitutionSchemeAdmin(admin.ModelAdmin):
      list_display = ["facility", "user", "whole_product_strength"]
      list_filter = ["facility", "user"]


class InlineIngredient(admin.TabularInline):
      model = ListedIngredient
      extra = 0


class ProductComponentInline(admin.TabularInline):
      model = ProductComponent
      extra = 0


class InlinePackages(admin.TabularInline):
      model = PackagedProduct
      extra = 0
      fields = ["package_ndc", "description", "active"]
      readonly_fields = ["package_ndc", "description"]


def _distinct_routes ():
      """Collect the distinct route strings already stored across products."""
      routes = set()
      for values in Product.objects.values_list("route", flat=True):
            if isinstance(values, list):
                  routes.update(v for v in values if v)
      return routes


class RouteTagWidget(forms.SelectMultiple):
      """A select2 tag-style multi-select for the Product.route JSON list."""

      class Media:
            css = {
                  "screen": (
                        "admin/css/vendor/select2/select2.min.css",
                        "admin/css/autocomplete.css",
                  )
            }
            js = (
                  "admin/js/vendor/jquery/jquery.min.js",
                  "admin/js/vendor/select2/select2.full.min.js",
                  "admin/js/jquery.init.js",
                  "rxocrpl/admin/route_tag_widget.js",
            )

      def __init__ (self, attrs=None, choices=()):
            default_attrs = {
                  "class": "route-tag-widget",
                  "data-placeholder": "Search or add routes…",
            }
            if attrs:
                  default_attrs.update(attrs)
            super().__init__(default_attrs, choices)


class RouteTagField(forms.MultipleChoiceField):
      """MultipleChoiceField that accepts arbitrary (newly-typed) tag values."""

      def valid_value (self, value):
            return True


class ProductAdminForm(forms.ModelForm):
      route = RouteTagField(
            required=False,
            widget=RouteTagWidget,
            help_text="Type to search existing routes or add new ones.",
      )

      class Meta:
            model = Product
            fields = "__all__"

      def __init__ (self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            current = self.instance.route if isinstance(getattr(self.instance, "route", None), list) else []
            values = sorted({*_distinct_routes(), *current})
            self.fields["route"].choices = [(v, v) for v in values]
            self.initial["route"] = current


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
      form = ProductAdminForm
      autocomplete_fields = ["concepts"]
      list_display = [
            "brand_name",
            "generic_name",
            "as_substance",
            "labeler",
            "product_ndc",
            "dosage_form",
            "route",
            "ingredient_count",
      ]
      filter_horizontal = ["concepts"]
      list_filter = ["labeler", "dosage_form", "route"]

      def get_queryset (self, request):
            return super().get_queryset(request).filter(active=True)

      fieldsets = (
            (None, {"fields": (("brand_name", "generic_name", "product_ndc"),
                               "dosage_form", "route",
                               ("as_substance", "sync_button"))}),
            ("Labeler", {"fields": (("labeler", "labeler_name"),)}),
            ("Stats", {"fields": ("ingredient_count", "package_count")}),
            ("Concepts", {"fields": ("concepts",)}),
      )
      readonly_fields = [
            "ingredient_count", "package_count", "as_substance", "sync_button", "labeler"
      ]

      inlines = [
            InlineIngredient, ProductComponentInline, InlinePackages
      ]
      search_fields = ["brand_name", "generic_name", "product_ndc"]
      actions = ["enrich_from_outside_sources"]

      class Media:
            js = ("rxocrpl/admin/concept_autocomplete_links.js",)

      def formfield_for_manytomany (self, db_field, request, **kwargs):
            formfield = super().formfield_for_manytomany(db_field, request, **kwargs)
            if db_field.name == "concepts" and formfield:
                  formfield.required = False
                  formfield.widget.attrs["data-change-url-template"] = reverse(
                        "admin:rxocrpl_rxnormconcept_change",
                        args=["__value__"],
                  )
            return formfield

      @admin.action(description="Enrich from Outside Sources")
      def enrich_from_outside_sources (self, request, queryset):
            succeeded = 0
            failed = []
            for product in queryset:
                  try:
                        sync_product_from_external_sources(product.product_ndc)
                        succeeded += 1
                  except Exception as exc:
                        failed.append(f"{product.product_ndc} ({exc})")

            if succeeded:
                  self.message_user(
                        request, f"Successfully enriched {succeeded} product(s)."
                  )
            if failed:
                  self.message_user(
                        request,
                        f"Failed to enrich {len(failed)} product(s): {', '.join(failed)}",
                        level=messages.ERROR,
                  )

      def view_on_site (self, obj):
            return reverse("rxocrpl:ndc_product_detail", args=[obj.product_ndc])

      def get_urls (self):
            from django.urls import path

            urls = super().get_urls()
            custom_urls = [
                  path(
                        "<path:object_id>/sync/",
                        self.admin_site.admin_view(self.sync_view),
                        name="rxocrpl_product_sync",
                  ),
            ]
            return custom_urls + urls

      def sync_view (self, request, object_id):
            from django.shortcuts import redirect

            obj = self.get_object(request, object_id)
            if obj:
                  sync_product_from_external_sources(obj.product_ndc)
                  self.message_user(request, f"Successfully enriched {obj.product_ndc}")
            return redirect("admin:rxocrpl_product_change", object_id)

      def sync_button (self, obj):
            from django.utils.html import format_html

            if obj.pk:
                  url = reverse("admin:rxocrpl_product_sync", args=[obj.pk])
                  return format_html(
                        '<a class="button" href="{}">Sync from External Sources</a>', url
                  )
            return ""

      sync_button.short_description = "Sync"  # ty:ignore[unresolved-attribute]

      @staticmethod
      def ingredient_count (obj):
            return obj.listedingredient_set.count()

      @staticmethod
      def package_count (obj):
            return obj.packagedproduct_set.count()


@admin.register(PackagedProduct)
class PackagedProductAdmin(admin.ModelAdmin):
      list_display = ["product", "package_ndc", "description"]
      search_fields = [
            "product__brand_name",
            "product__generic_name",
            "product__product_ndc",
            "package_ndc",
      ]

      def get_queryset (self, request):
            return super().get_queryset(request).filter(active=True)


@admin.register(ListedIngredient)
class ListedIngredientAdmin(admin.ModelAdmin):
      list_display = ["product", "name", "strength", "unit"]
      search_fields = ["product__brand_name", "product__generic_name"]
      readonly_fields = ["product"]

      def get_queryset (self, request):
            return super().get_queryset(request).filter(product__active=True)


class InlineProducts(admin.TabularInline):
      model = Product
      extra = 0
      fields = [
            "brand_name",
            "generic_name",
            "product_ndc",
            "dosage_form",
            "as_substance",
      ]
      readonly_fields = [
            "brand_name",
            "generic_name",
            "product_ndc",
            "dosage_form",
            "as_substance",
      ]
      can_delete = False
      show_change_link = True


@admin.register(Labeler)
class LabelerAdmin(admin.ModelAdmin):
      list_display = ["verbose_name", "labeler_code", "name", "product_count"]
      search_fields = ["verbose_name", "labeler_code", "name"]
      list_filter = ["active"]
      sortable_by = ["verbose_name", "labeler_code", "name", "product_count"]

      inlines = [InlineProducts]

      def view_on_site (self, obj):
            return reverse("rxocrpl:labeler_detail", args=[obj.labeler_code])

      def get_queryset (self, request):
            return super().get_queryset(request).filter(active=True)

      @staticmethod
      def product_count (obj):
            return obj.product_set.count()


@admin.register(RxNormConcept)
class RxNormConceptAdmin(admin.ModelAdmin):
      list_display = ["rxid", "name", "tty", "hierarchy_link"]
      search_fields = ["rxcui", "name", "tty"]
      list_filter = ["tty"]
      sortable_by = ["rxid", "name", "tty"]
      readonly_fields = ["hierarchy_link"]
      change_form_template = "admin/rxocrpl/rxnormconcept/change_form.html"

      def view_on_site (self, obj):
            return reverse("admin:rxocrpl_rxnormconcept_hierarchy", args=[obj.rxcui])

      @staticmethod
      def _parse_pages (request):
            import json

            raw = request.GET.get("pages")
            if not raw:
                  return {}
            try:
                  data = json.loads(raw)
            except (ValueError, TypeError):
                  return {}
            if not isinstance(data, dict):
                  return {}
            return {
                  str(k): int(v)
                  for k, v in data.items()
                  if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit())
            }

      @staticmethod
      def _hierarchy_context (concept, pages=None):
            from .rxgraph.hierarchy import build_hierarchy

            return {
                  **build_hierarchy(concept, pages),
                  "cytoscape_url": reverse("rxocrpl:concept_graph", args=[concept.rxcui]),
                  "change_url": reverse(
                        "admin:rxocrpl_rxnormconcept_change", args=[concept.rxcui]
                  ),
            }

      def change_view (self, request, object_id, form_url="", extra_context=None):
            concept = self.get_object(request, object_id)
            extra_context = extra_context or {}
            if concept:
                  extra_context["hierarchy"] = self._hierarchy_context(concept)
            return super().change_view(request, object_id, form_url, extra_context)

      def get_urls (self):
            from django.urls import path

            urls = super().get_urls()
            custom_urls = [
                  path(
                        "<path:rxcui>/hierarchy/",
                        self.admin_site.admin_view(self.hierarchy_view),
                        name="rxocrpl_rxnormconcept_hierarchy",
                  ),
            ]
            return custom_urls + urls

      def hierarchy_view (self, request, rxcui):
            from django.shortcuts import get_object_or_404, render

            concept = get_object_or_404(RxNormConcept, rxcui=rxcui)
            pages = self._parse_pages(request)
            hierarchy = self._hierarchy_context(concept, pages)

            # In-place paging: return just the diagram body so the client can
            # swap it without a full page reload (progressive fallback renders
            # the whole page when ``partial`` is absent).
            if request.GET.get("partial"):
                  return render(
                        request,
                        "admin/rxocrpl/rxnormconcept/_hierarchy_body.html",
                        hierarchy,
                  )

            context = {
                  **self.admin_site.each_context(request),
                  **hierarchy,
                  "title": f"Hierarchy · {concept.name or concept.rxcui}",
                  "opts": self.model._meta,
            }
            return render(request, "admin/rxocrpl/rxnormconcept/hierarchy.html", context)

      @admin.display(description="Hierarchy")
      def hierarchy_link (self, obj):
            from django.utils.html import format_html

            if not obj.pk:
                  return ""
            url = reverse("admin:rxocrpl_rxnormconcept_hierarchy", args=[obj.pk])
            return format_html('<a class="button" href="{}">View hierarchy</a>', url)
