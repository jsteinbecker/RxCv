"""
URL configuration for rxcv project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include
from . import views

app_name = "aux"

urlpatterns = [
      path("labeler-classifications/", views.labeler_classifier_view, name="labeler_classifier"),
      path("labeler-classifications/update/", views.create_and_update_labeler_classifications, name="update_labeler_classifications"),
      path('<labeler_id>/deactivate-labeler/', views.deactivate_labeler, name="deactivate_labeler"),
]
