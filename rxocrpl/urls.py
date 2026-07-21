from django.urls import path
from django.urls import include
from . import views

app_name = 'rxocrpl'

urlpatterns = [
      path('', views.index, name='index'),
      path('api/', include(('rxocrpl.api.urls', 'rxocrpl_api'), namespace='rxocrpl_api')),
      path('stats/', views.stats, name='stats'),
      path('products-without-ingredients/', views.products_without_ingredients_view, name='products_without_ingredients'),
      path('concept/<str:rxcui>/graph/', views.concept_graph_view, name='concept_graph'),
      path('concept/<str:rxcui>/sync/', views.sync_concept_from_rxnorm, name='sync_concept'),
      path('tty/<str:tty>/', views.concept_tty_list_view, name='concept_tty_list'),
      path('ndc/data/loadall/', views.import_all_ndc_products, name='import_all_ndc_products'),
      path('ndc/data/loadpkg/', views.import_all_packages, name='import_all_packages'),
      path('ndc/data/loadingredients/', views.ingredients_dict_to_model, name='import_all_ingredients'),
      path('ndc/', views.ndc_product_list_view, name='ndc_product_list'),
      path('ndc/<str:ndc>/', views.ndc_product_detail_view, name='ndc_product_detail'),
      path('labeler/', views.labeler_list_view, name='labeler_list'),
      path('labeler/<str:labeler_code>/', views.labeler_detail_view, name='labeler_detail'),
      path('facility/', views.facility_list_view, name='facility_list'),
      path('facility/<int:pk>/claim/', views.claim_facility_view, name='facility_claim'),
]
