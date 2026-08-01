"""
rxocrpl/base_views.py

Reusable view "primitives" for the rxocrpl app.

These are small, composable class-based views/mixins that capture the
patterns repeated throughout views.py:

  - SearchableListView      querystring search (?q=) + pagination + template
  - StatsView               declarative "count these models" dashboard pages
  - QuerySetActionView      run a callable against a queryset, flash a message,
                            redirect (e.g. repair jobs)
  - JsonActionView          run a callable, return JsonResponse (GET or POST)
  - CsvImportView           stream a CSV/TSV file into update_or_create calls
  - AjaxTemplateResponseMixin  return JSON for XHR, HTML otherwise
"""

import csv

from django.contrib import messages
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from django.views.generic import DetailView, ListView, TemplateView


# ---------------------------------------------------------------------------
# Mixins
# ---------------------------------------------------------------------------

class SearchQueryMixin:
      """Reads ?q= and applies icontains OR-search across `search_fields`."""

      search_fields: tuple[str, ...] = ()
      search_param = "q"

      @property
      def query (self):
            return self.request.GET.get(self.search_param, "").strip()

      def apply_search (self, queryset):
            if self.query and self.search_fields:
                  q = Q()
                  for field in self.search_fields:
                        q |= Q(**{f"{field}__icontains": self.query})
                  queryset = queryset.filter(q)
            return queryset

      def get_context_data (self, **kwargs):
            context = super().get_context_data(**kwargs)
            context["query"] = self.query
            return context


class AjaxTemplateResponseMixin:
      """Return `get_ajax_payload()` as JSON when the request is XHR."""

      def render_to_response (self, context, **response_kwargs):
            if self.request.headers.get("x-requested-with") == "XMLHttpRequest":
                  return JsonResponse(self.get_ajax_payload(context))
            return super().render_to_response(context, **response_kwargs)

      def get_ajax_payload (self, context):
            raise NotImplementedError


# ---------------------------------------------------------------------------
# List / detail primitives
# ---------------------------------------------------------------------------

class SearchableListView(SearchQueryMixin, ListView):
      """
      ListView with ?q= search, pagination, and a `page_obj` in context
      (ListView provides page_obj automatically when paginate_by is set).

      Subclasses set: model or queryset, search_fields, paginate_by,
      template_name, ordering, and optionally base_filters (dict).
      """

      paginate_by = 50
      base_filters: dict = {}

      def get_queryset (self):
            qs = super().get_queryset()
            if self.base_filters:
                  qs = qs.filter(**self.base_filters)
            return self.apply_search(qs)


class SlugDetailView(DetailView):
      """DetailView keyed on an arbitrary unique field instead of pk."""

      lookup_field = "pk"
      lookup_url_kwarg = None  # defaults to lookup_field

      def get_object (self, queryset=None):
            queryset = queryset or self.get_queryset()
            kwarg = self.lookup_url_kwarg or self.lookup_field
            return get_object_or_404(
                  queryset, **{self.lookup_field: self.kwargs[kwarg]}
            )


# ---------------------------------------------------------------------------
# Dashboard / stats primitive
# ---------------------------------------------------------------------------

class StatsView(TemplateView):
      """
      Declarative count dashboard.

      stats = {"total_concepts": RxNormConcept, "products_missing": callable}

      Values may be a Model class (counted via .objects.count()), a queryset,
      or a zero-arg callable returning a queryset or an int.
      """

      stats: dict = {}

      @staticmethod
      def _resolve (value):
            if callable(value) and not hasattr(value, "objects"):
                  value = value()
            if hasattr(value, "objects"):          # model class
                  return value.objects.count()
            if hasattr(value, "count"):            # queryset
                  return value.count()
            return value                            # int

      def get_context_data (self, **kwargs):
            context = super().get_context_data(**kwargs)
            context.update({name: self._resolve(v) for name, v in self.stats.items()})
            return context


# ---------------------------------------------------------------------------
# Action primitives
# ---------------------------------------------------------------------------

class QuerySetActionView(View):
      """
      Run `perform_action()`, flash `get_message(result)`, redirect to
      `success_url_name`. Good for maintenance/repair endpoints.
      """

      success_url_name: str = ""

      def get (self, request, *args, **kwargs):
            return self._run(request)

      def post (self, request, *args, **kwargs):
            return self._run(request)

      def _run (self, request):
            result = self.perform_action()
            msg = self.get_message(result)
            if msg:
                  messages.info(request, msg)
            return redirect(self.success_url_name)

      def perform_action (self):
            raise NotImplementedError

      def get_message (self, result):
            return None


class JsonActionView(View):
      """
      Run `perform_action(**url_kwargs)` and return its dict as JSON.
      Set http_method_names = ["post"] on subclasses for POST-only actions.
      Exceptions become a 500 JSON payload.
      """

      def dispatch (self, request, *args, **kwargs):
            if request.method.lower() not in self.http_method_names:
                  return self.http_method_not_allowed(request, *args, **kwargs)
            try:
                  payload = self.perform_action(**kwargs)
            except Exception as exc:
                  return JsonResponse(
                        {"status": "error", "message": str(exc)}, status=500
                  )
            return JsonResponse(payload)

      def perform_action (self, **kwargs):
            raise NotImplementedError


class CsvImportView(JsonActionView):
      """
      Stream a delimited file and hand each row to `handle_row(row)`.

      Subclasses set: path, delimiter (default ","), success_message.
      """

      path: str = ""
      delimiter = ","
      success_message = "Import completed successfully."

      def perform_action (self, **kwargs):
            with open(self.path, newline="") as f:
                  reader = csv.DictReader(f, delimiter=self.delimiter)
                  count = 0
                  for row in reader:
                        if self.handle_row(row) is not False:
                              count += 1
            return {"status": "success", "message": self.success_message, "rows": count}

      def handle_row (self, row):
            raise NotImplementedError


# ---------------------------------------------------------------------------
# Simple template render helper
# ---------------------------------------------------------------------------

class StaticContextView(TemplateView):
      """TemplateView with a class-level `extra_context`-style static payload."""
      static_context: dict = {}

      def get_context_data (self, **kwargs):
            context = super().get_context_data(**kwargs)
            context.update(self.static_context)
            return context
