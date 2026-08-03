from django.urls import include, path

from . import views

app_name = 'rxocrpl'

urlpatterns = [
      path('', views.IndexView.as_view(), name='index'),
      path('api/', include(('rxocrpl.api.urls', 'rxocrpl_api'), namespace='rxocrpl_api')),
      path('stats/', views.SiteStatsView.as_view(), name='stats'),

      path('products-without-ingredients/', views.ProductsWithoutIngredientsView.as_view(), name='products_without_ingredients'),
      path('products-without-ingredients/repair/', views.RepairProductsWithoutIngredientsView.as_view(),
           name='repair_products_without_ingredients'),

      path('concept/<str:rxcui>/graph/', views.ConceptGraphView.as_view(), name='concept_graph'),
      path('concept/<str:rxcui>/sync/', views.SyncConceptFromRxNormView.as_view(), name='sync_concept'),
      path('concept/link-quantified-scds/', views.QuantifiedFormEnrichmentView.as_view(), name='quantified_form_enrichment'),
      path('concept/update_collected_concept_data/', views.ConceptCollectedDataUpdateView.as_view(), name='update_collected_concept_data'),
      path('tty/<str:tty>/', views.ConceptTtyListView.as_view(), name='concept_tty_list'),

      path('ndc/data/loadall/', views.ImportNdcProductsView.as_view(), name='import_all_ndc_products'),
      path('ndc/data/loadpkg/', views.ImportPackagesView.as_view(), name='import_all_packages'),
      path('ndc/data/loadingredients/', views.IngredientsDictToModelView.as_view(), name='import_all_ingredients'),
      path('ndc/', views.NdcProductListView.as_view(), name='ndc_product_list'),
      path('ndc/<str:ndc>/', views.NdcProductDetailView.as_view(), name='ndc_product_detail'),

      path('labeler/', views.LabelerListView.as_view(), name='labeler_list'),
      path('labeler/classify/', views.LabelerClassifierView.as_view(), name='labeler_classifier'),
      path('labeler/<str:labeler_code>/', views.LabelerDetailView.as_view(), name='labeler_detail'),

      path('facility/', views.FacilityListView.as_view(), name='facility_list'),
      path('facility/<int:pk>/claim/', views.ClaimFacilityView.as_view(), name='facility_claim'),
]
