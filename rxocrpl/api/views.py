from rest_framework import filters, viewsets
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from rxocrpl.integrity import check_for_products_without_ingredients
from rxocrpl.models import Labeler, Product, RxNormConcept

from .serializers import LabelerSerializer, ProductSerializer, RxNormConceptSerializer


class HealthAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, *args, **kwargs):
        return Response({"status": "ok", "service": "rxocrpl-api"})


class SummaryAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, *args, **kwargs):
        return Response(
            {
                "products": Product.objects.count(),
                "concepts": RxNormConcept.objects.count(),
                "labelers": Labeler.objects.count(),
                "products_without_ingredients": check_for_products_without_ingredients().count(),
            }
        )


class ProductViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = ProductSerializer
    permission_classes = [AllowAny]
    lookup_field = "product_ndc"
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ("product_ndc", "generic_name", "brand_name", "labeler_name")
    ordering_fields = ("product_ndc", "generic_name", "brand_name", "labeler_name", "ingredient_count")
    ordering = ("generic_name", "product_ndc")

    def get_queryset(self):
        queryset = (
            Product.objects.select_related("labeler")
            .prefetch_related("ingredients", "packaged_products")
            .all()
        )

        active = self.request.query_params.get("active")
        if active is not None:
            normalized = active.lower()
            if normalized in {"1", "true", "yes", "on"}:
                queryset = queryset.filter(active=True)
            elif normalized in {"0", "false", "no", "off"}:
                queryset = queryset.filter(active=False)

        labeler = self.request.query_params.get("labeler")
        if labeler:
            queryset = queryset.filter(labeler__labeler_code=labeler)

        dosage_form = self.request.query_params.get("dosage_form")
        if dosage_form:
            queryset = queryset.filter(dosage_form__iexact=dosage_form)

        ndc = self.request.query_params.get("ndc")
        if ndc:
            queryset = queryset.filter(product_ndc=ndc)

        return queryset


class RxNormConceptViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = RxNormConceptSerializer
    permission_classes = [AllowAny]
    lookup_field = "rxcui"
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ("rxcui", "name", "tty")
    ordering_fields = ("rxcui", "name", "tty", "synced_at")
    ordering = ("name", "rxcui")

    def get_queryset(self):
        queryset = RxNormConcept.objects.all()

        active = self.request.query_params.get("active")
        if active is not None:
            normalized = active.lower()
            if normalized in {"1", "true", "yes", "on"}:
                queryset = queryset.filter(active=True)
            elif normalized in {"0", "false", "no", "off"}:
                queryset = queryset.filter(active=False)

        tty = self.request.query_params.get("tty")
        if tty:
            queryset = queryset.filter(tty__iexact=tty)

        return queryset


class LabelerViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = LabelerSerializer
    permission_classes = [AllowAny]
    lookup_field = "labeler_code"
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ("labeler_code", "name", "verbose_name")
    ordering_fields = ("labeler_code", "name", "active")
    ordering = ("name", "labeler_code")

    def get_queryset(self):
        queryset = Labeler.objects.all()

        active = self.request.query_params.get("active")
        if active is not None:
            normalized = active.lower()
            if normalized in {"1", "true", "yes", "on"}:
                queryset = queryset.filter(active=True)
            elif normalized in {"0", "false", "no", "off"}:
                queryset = queryset.filter(active=False)

        return queryset
