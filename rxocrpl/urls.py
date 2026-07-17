from django.urls import path
from . import views

app_name = 'rxocrpl'

urlpatterns = [
      path('', views.index, name='index'),
      path('stats/', views.stats, name='stats'),
      path('concept/<str:rxcui>/graph/', views.concept_graph_view, name='concept_graph'),
      path('tty/<str:tty>/', views.concept_tty_list_view, name='concept_tty_list'),
      path('ndc/data/loadall/', views.import_all_ndc_products, name='import_all_ndc_products'),
      path('ndc/data/loadpkg/', views.import_all_packages, name='import_all_packages'),
      path('ndc/data/loadingredients/', views.ingredients_dict_to_model, name='import_all_ingredients'),
      path('ndc/', views.ndc_product_list_view, name='ndc_product_list'),
      path('ndc/<str:ndc>/', views.ndc_product_detail_view, name='ndc_product_detail'),
      path('labeler/', views.labeler_list_view, name='labeler_list'),
      path('labeler/<str:labeler_code>/', views.labeler_detail_view, name='labeler_detail'),
]
