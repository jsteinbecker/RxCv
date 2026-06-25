import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'rxcv.settings')
django.setup()

from rxocrpl.rxgraph.product_pipeline import materialize_product
from rxocrpl.models import Product


def run_test():
      # Acetaminophen RXCUI
      rxcui = "161"
      ndc = "12345678901"

      print(f"Materializing product for RXCUI={rxcui}, NDC={ndc}...")
      try:
            product = materialize_product(rxcui, ndc)
            print(f"Product created/updated: {product}")
            print(f"Generic Name: {product.generic_name}")
            print(f"Ingredients: {product.active_ingredients}")
      except Exception as e:
            print(f"Error: {e}")


if __name__ == "__main__":
      run_test()
