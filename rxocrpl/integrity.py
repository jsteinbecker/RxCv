"""
Data Integrity Checks for RXOCRPL
"""
from __future__ import annotations

from django.db.models import QuerySet, Count
from rxcv import settings

try:
      from .models import Product
except ImportError as e:
      try:
            from rxocrpl.models import Product
      except ImportError:
            raise ImportError("Could not import required modules. Ensure this script is run within the Django project context.") from e


def check_for_products_without_ingredients () -> QuerySet[Product]:
      """
      Check for products that do not have any associated ingredients.

      Returns:
            QuerySet[Product]: A queryset of products without ingredients.
      """
      return Product.objects.filter(active=True, ingredients__isnull=True)
