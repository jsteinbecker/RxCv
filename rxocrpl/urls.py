from django.urls import path
from . import views

app_name = 'rxocrpl'

urlpatterns = [
      path('concept/<str:rxcui>/graph/', views.concept_graph_view, name='concept_graph'),
      path('tty/<str:tty>/', views.concept_tty_list_view, name='concept_tty_list'),
]
