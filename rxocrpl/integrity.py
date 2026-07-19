"""
Data Integrity Checks for RXOCRPL
"""
from __future__ import annotations

from django.db.models import QuerySet, Count

try:
      from .consoleprint import _hdr, _sub, _val
      from .models import Product
except ImportError as e:
      try:
            from rxocrpl.consoleprint import _hdr, _sub, _val
            from rxocrpl.models import Product
      except ImportError:
            raise ImportError("Could not import required modules. Ensure this script is run within the Django project context.") from e


def check_for_products_without_ingredients() -> QuerySet[Product]:
      """
      Check for products that do not have any associated ingredients.

      Returns:
            QuerySet[Product]: A queryset of products without ingredients.
      """
      return Product.objects.annotate(ingredient_ct=Count('ingredients')).filter(ingredient_ct=0)


if __name__ == "__main__":
      _hdr("Data Integrity Checks for RXOCRPL")
      _sub("Checking for products without ingredients...")
      products_without_ingredients = check_for_products_without_ingredients()
      if products_without_ingredients.exists():
            _val(f"Found {products_without_ingredients.count()} products without ingredients:")
            for product in products_without_ingredients:
                  _val(f"- {product.product_ndc} ({product.generic_name or product.labeler.name})")
      else:
            _val("No products without ingredients found.")
