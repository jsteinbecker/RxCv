from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import HealthAPIView, LabelerViewSet, ProductViewSet, RxNormConceptViewSet, SummaryAPIView

app_name = "rxocrpl_api"

router = DefaultRouter()
router.register(r"products", ProductViewSet, basename="product")
router.register(r"concepts", RxNormConceptViewSet, basename="concept")
router.register(r"labelers", LabelerViewSet, basename="labeler")

urlpatterns = [
    path("health/", HealthAPIView.as_view(), name="health"),
    path("summary/", SummaryAPIView.as_view(), name="summary"),
]

urlpatterns += router.urls
