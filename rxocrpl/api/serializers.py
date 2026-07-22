from rest_framework import serializers

from rxocrpl.models import Labeler, ListedIngredient, PackagedProduct, Product, RxNormConcept


class LabelerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Labeler
        fields = ("labeler_code", "name", "verbose_name", "active")


class ListedIngredientSerializer(serializers.ModelSerializer):
    class Meta:
        model = ListedIngredient
        fields = ("name", "strength", "unit")


class PackagedProductSerializer(serializers.ModelSerializer):
    class Meta:
        model = PackagedProduct
        fields = ("package_ndc", "package_code", "description", "active")


class ProductSerializer(serializers.ModelSerializer):
    labeler = LabelerSerializer(read_only=True)
    ingredients = ListedIngredientSerializer(many=True, read_only=True)
    packaged_products = PackagedProductSerializer(many=True, read_only=True)
    description = serializers.SerializerMethodField()
    substance = serializers.SerializerMethodField()
    dailymed_url = serializers.CharField(read_only=True)

    class Meta:
        model = Product
        fields = (
            "product_ndc",
            "generic_name",
            "brand_name",
            "labeler_name",
            "labeler",
            "dosage_form",
            "route",
            "active_ingredients",
            "ingredient_count",
            "rxcui_mapping",
            "active",
            "description",
            "substance",
            "dailymed_url",
            "ingredients",
            "packaged_products",
        )

    def get_description(self, obj):
        return obj.describe()

    def get_substance(self, obj):
        return obj.as_substance()


class RxNormConceptSerializer(serializers.ModelSerializer):
    class Meta:
        model = RxNormConcept
        fields = ("rxcui", "name", "tty", "active", "attributes", "rxnorm_release", "synced_at")
