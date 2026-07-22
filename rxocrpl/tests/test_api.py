from django.test import TestCase
from rest_framework.test import APIClient

from rxocrpl.models import Labeler, ListedIngredient, PackagedProduct, Product, RxNormConcept


class RxOcrPlApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.labeler = Labeler.objects.create(
            labeler_code="12345",
            name="Acme Pharma",
            verbose_name="Acme Pharmaceutical Company",
            active=True,
        )
        self.product = Product.objects.create(
            product_ndc="12345-6789-10",
            generic_name="Acetaminophen",
            brand_name="Tylenol",
            labeler=self.labeler,
            labeler_name="Acme Pharma",
            dosage_form="tablet",
            route=["oral"],
            active_ingredients=[{"name": "Acetaminophen", "strength": "325", "unit": "mg"}],
            rxcui_mapping={"rxcui": "123"},
            active=True,
        )
        self.packaged_product = PackagedProduct.objects.create(
            product=self.product,
            package_code="10",
            description="100 tablet in 1 bottle",
            active=True,
        )
        ListedIngredient.objects.create(
            product=self.product,
            name="Acetaminophen",
            strength="325",
            unit="mg",
        )
        RxNormConcept.objects.create(
            rxcui="123",
            name="Acetaminophen 325 MG Oral Tablet",
            tty="SCD",
            active=True,
        )

    def test_health_endpoint(self):
        response = self.client.get("/rxocrpl/api/health/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_summary_endpoint(self):
        response = self.client.get("/rxocrpl/api/summary/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["products"], 1)
        self.assertEqual(payload["concepts"], 1)
        self.assertEqual(payload["labelers"], 1)

    def test_product_list_includes_nested_fields(self):
        response = self.client.get("/rxocrpl/api/products/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("results", payload)
        self.assertEqual(len(payload["results"]), 1)

        product = payload["results"][0]
        self.assertEqual(product["product_ndc"], "12345-6789-10")
        self.assertEqual(product["labeler"]["labeler_code"], "12345")
        self.assertEqual(product["ingredients"][0]["name"], "Acetaminophen")
        self.assertEqual(product["substance"], "acetaminophen 325 MG")

    def test_labeler_active_syncs_to_products_and_packages(self):
        self.labeler.active = False
        self.labeler.save(update_fields=["active"])

        self.product.refresh_from_db()
        self.packaged_product.refresh_from_db()
        self.assertFalse(self.product.active)
        self.assertFalse(self.packaged_product.active)

        self.labeler.active = True
        self.labeler.save(update_fields=["active"])

        self.product.refresh_from_db()
        self.packaged_product.refresh_from_db()
        self.assertTrue(self.product.active)
        self.assertTrue(self.packaged_product.active)

    def test_inactive_labeler_leads_new_products_and_packages(self):
        inactive_labeler = Labeler.objects.create(
            labeler_code="99999",
            name="Inactive Pharma",
            active=False,
        )
        product = Product.objects.create(
            product_ndc="99999-1111",
            generic_name="Ibuprofen",
            labeler=inactive_labeler,
            labeler_name="Inactive Pharma",
            dosage_form="tablet",
            route=["oral"],
            active_ingredients=[],
            active=True,
        )
        package = PackagedProduct.objects.create(
            product=product,
            package_code="01",
            description="100 tablet in 1 bottle",
            active=True,
        )

        product.refresh_from_db()
        package.refresh_from_db()
        self.assertFalse(product.active)
        self.assertFalse(package.active)
