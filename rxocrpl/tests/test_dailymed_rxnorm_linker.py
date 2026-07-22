from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import TestCase

from rxocrpl.dailymed_rxnorm_linker import (
      SetidRxNormEntry,
      link_product_from_setid_map,
      load_setid_map,
      select_best_entry,
)
from rxocrpl.models import Product, ProductRxNormMapping


def _make_product (
          *,
          product_ndc: str,
          generic_name: str,
          brand_name: str | None = None,
          dosage_form: str = "tablet",
) -> Product:
      return Product.objects.create(
            product_ndc=product_ndc,
            generic_name=generic_name,
            brand_name=brand_name,
            labeler_name="Test Labeler",
            dosage_form=dosage_form,
            route=["oral"],
            active_ingredients=[{"name": generic_name, "strength": "1", "unit": "MG"}],
            rxcui_mapping={},
            active=True,
      )


class DailyMedRxNormLinkerTests(TestCase):
      def test_select_best_entry_prefers_brand_for_branded_products (self):
            product = Product(
                  product_ndc="0000-0000-00",
                  generic_name="trametinib",
                  brand_name="Mekinist",
                  labeler_name="Test",
                  dosage_form="tablet",
                  route=[],
                  active_ingredients=[],
                  rxcui_mapping={},
                  active=True,
            )
            entries = [
                  SetidRxNormEntry(
                        setid="setid",
                        spl_version=35,
                        rxcui="1425104",
                        rxstring="trametinib 0.5 MG Oral Tablet",
                        tty="SCD",
                  ),
                  SetidRxNormEntry(
                        setid="setid",
                        spl_version=35,
                        rxcui="1425110",
                        rxstring="trametinib 0.5 MG Oral Tablet [Mekinist]",
                        tty="SBD",
                  ),
            ]

            best = select_best_entry(product, entries)

            self.assertIsNotNone(best)
            self.assertEqual(best.rxcui, "1425110")

      def test_select_best_entry_prefers_generic_for_unbranded_products (self):
            product = Product(
                  product_ndc="0000-0000-01",
                  generic_name="trametinib",
                  brand_name=None,
                  labeler_name="Test",
                  dosage_form="tablet",
                  route=[],
                  active_ingredients=[],
                  rxcui_mapping={},
                  active=True,
            )
            entries = [
                  SetidRxNormEntry(
                        setid="setid",
                        spl_version=35,
                        rxcui="1425110",
                        rxstring="trametinib 0.5 MG Oral Tablet [Mekinist]",
                        tty="SBD",
                  ),
                  SetidRxNormEntry(
                        setid="setid",
                        spl_version=35,
                        rxcui="1425104",
                        rxstring="trametinib 0.5 MG Oral Tablet",
                        tty="SCD",
                  ),
            ]

            best = select_best_entry(product, entries)

            self.assertIsNotNone(best)
            self.assertEqual(best.rxcui, "1425104")

      def test_load_and_link_product_from_setid_map (self):
            with TemporaryDirectory() as tmpdir:
                  map_path = Path(tmpdir) / "rxnorm-dailymed_setid_map.txt"
                  map_path.write_text(
                        "\n".join(
                              [
                                    "SETID|SPL_VERSION|RXCUI|RXSTRING|RXTTY",
                                    "set-123|1|111111|trametinib 0.5 MG Oral Tablet|SCD",
                                    "set-123|2|111111|trametinib 0.5 MG Oral Tablet|SCD",
                                    "set-123|2|222222|trametinib 0.5 MG Oral Tablet [Mekinist]|SBD",
                              ]
                        ),
                        encoding="utf-8",
                  )

                  setid_index = load_setid_map(map_path)
                  self.assertEqual(len(setid_index["set-123"]), 2)
                  self.assertEqual(
                        {entry.rxcui for entry in setid_index["set-123"]},
                        {"111111", "222222"},
                  )

                  product = _make_product(
                        product_ndc="12345-6789-10",
                        generic_name="trametinib",
                        brand_name="Mekinist",
                  )

                  with patch(
                            "rxocrpl.dailymed_rxnorm_linker.find_setid",
                            return_value="set-123",
                  ):
                        best = link_product_from_setid_map(product, setid_index)

                  self.assertIsNotNone(best)
                  self.assertEqual(best.rxcui, "222222")

                  mapping = ProductRxNormMapping.objects.get(
                        product_ndc=product.product_ndc
                  )
                  self.assertEqual(mapping.rxcui, "222222")
                  self.assertEqual(mapping.tty, "SBD")
                  self.assertEqual(mapping.name, "trametinib 0.5 MG Oral Tablet [Mekinist]")
