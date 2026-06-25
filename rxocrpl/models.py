from django.db import models
from django.core.validators import MinValueValidator

from .rxgraph.traversal import RxConceptTraversalMixin


class TermType(models.TextChoices):
      """Type of term extracted from an OCR result."""
      IN = 'IN', 'Ingredient'
      PIN = 'PIN', 'Precise Ingredient'
      SCDC = 'SCDC', 'Semantic Clinical Drug Component'
      SCD = 'SCD', 'Semantic Clinical Drug'
      SCDG = 'SCDG', 'Semantic Clinical Drug Dose Form Group'
      BN = 'BN', 'Branded Name'
      SBDC = 'SBDC', 'Semantic Branded Drug Component'
      SBD = 'SBD', 'Semantic Branded Drug'


class RouteOfAdministration(models.TextChoices):
      """Route of administration for a clinical drug."""
      ORAL = 'oral', 'Oral'
      INTRAVENOUS = 'intravenous', 'Intravenous'
      INTRAMUSCULAR = 'intramuscular', 'Intramuscular'
      SUBCUTANEOUS = 'subcutaneous', 'Subcutaneous'
      TOPICAL = 'topical', 'Topical'
      INHALATION = 'inhalation', 'Inhalation'
      RECTAL = 'rectal', 'Rectal'
      VAGINAL = 'vaginal', 'Vaginal'
      NASAL = 'nasal', 'Nasal'
      OPHTHALMIC = 'ophthalmic', 'Ophthalmic'
      OTIC = 'otic', 'Otic'


class DoseForm(models.TextChoices):
      """Dose form for a clinical drug."""
      PILL = 'pill', 'Pill'
      TABLET = 'tablet', 'Tablet'
      CAPSULE = 'capsule', 'Capsule'
      INJECTION = 'injection', 'Injection'
      SOLUTION = 'solution', 'Solution'
      SUSPENSION = 'suspension', 'Suspension'
      CREAM = 'cream', 'Cream'
      OINTMENT = 'ointment', 'Ointment'
      GEL = 'gel', 'Gel'
      PATCH = 'patch', 'Patch'
      DROPS = 'drops', 'Drops'
      SPRAY = 'spray', 'Spray'
      POWDER = 'powder', 'Powder'
      SUPPOSITORY = 'suppository', 'Suppository'


class RxNormConcept(models.Model, RxConceptTraversalMixin):
      """RxNorm concept mapping."""
      rxcui = models.CharField(max_length=20, primary_key=True)
      name = models.CharField(max_length=255, null=True, blank=True)
      tty = models.CharField(max_length=10, choices=TermType.choices)
      active = models.BooleanField(default=True)
      attributes = models.JSONField(default=dict, blank=True)
      synced_at = models.DateTimeField(auto_now=True)
      rxnorm_release = models.CharField(max_length=20, null=True, blank=True)

      def __str__(self):
            return f"[{self.tty}.{self.rxcui}] {self.name}"


class RxNormConceptRelation(models.Model):
      """Edges in the RxNorm graph (e.g. SCD --consists_of--> SCDC)."""
      source = models.ForeignKey(RxNormConcept, related_name='outbound_relations', on_delete=models.CASCADE)
      target = models.ForeignKey(RxNormConcept, related_name='inbound_relations', on_delete=models.CASCADE)
      rela = models.CharField(max_length=50)
      # Optional strength attributes (materialized from SCDC/SCD has_ingredient edges)
      numerator_value = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
      numerator_unit = models.CharField(max_length=20, null=True, blank=True)
      denominator_value = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
      denominator_unit = models.CharField(max_length=20, null=True, blank=True)

      class Meta:
            unique_together = ('source', 'target', 'rela')

      def __str__(self):
            return f"{self.source_id} -[{self.rela}]-> {self.target_id}"


class ClinicalDrug(models.Model):
      """RxNorm SCD/SCDC concept details."""
      concept = models.OneToOneField(RxNormConcept, on_delete=models.CASCADE, primary_key=True,
                                     limit_choices_to={'tty': 'SCD'})
      roa = models.CharField(max_length=20, choices=RouteOfAdministration.choices)
      dose_form = models.CharField(max_length=20, choices=DoseForm.choices)

      def __str__(self):
            return f"{self.concept.name} ({self.concept.rxcui})"


class Product(models.Model):
      """FDA drug product details."""
      product_ndc = models.CharField(max_length=20, primary_key=True)
      generic_name = models.CharField(max_length=100)
      brand_name = models.CharField(max_length=100, null=True, blank=True)
      labeler_name = models.CharField(max_length=100)
      dosage_form = models.CharField(max_length=100)
      route = models.JSONField(default=list)  # List of route strings
      active_ingredients = models.JSONField(default=list)  # List of ingredient dicts
      rxcui_mapping = models.JSONField(default=dict)  # Mapping of NDC to RxCUI info

      def __str__(self):
            return f"{self.generic_name} ({self.product_ndc})"


class CspOrder(models.Model):
      """Pharmacy preparation order."""
      id = models.AutoField(primary_key=True)
      reference_image = models.FileField(upload_to='orders/reference/', null=True, blank=True)
      certified_subset = models.BooleanField(default=False)
      created_at = models.DateTimeField(auto_now_add=True)

      def __str__(self):
            return f"Order {self.id} ({self.created_at})"


class Component(models.Model):
      """A specific drug component within an order."""
      order = models.ForeignKey(CspOrder, related_name='components', on_delete=models.CASCADE)
      product = models.ForeignKey(Product, on_delete=models.PROTECT)

      # Quantities stored as magnitude + unit string (matching quantities.py logic)
      numerator_mag = models.DecimalField(max_digits=12, decimal_places=4)
      numerator_unit = models.CharField(max_length=20)
      denominator_mag = models.DecimalField(max_digits=12, decimal_places=4)
      denominator_unit = models.CharField(max_length=20)

      quantity = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)])
      lot = models.CharField(max_length=100, null=True, blank=True)
      exp = models.CharField(max_length=100, null=True, blank=True)
      package_ndc = models.CharField(max_length=20, null=True, blank=True)

      def __str__(self):
            return f"{self.product.generic_name} in Order {self.order_id}"


class OCRFields(models.Model):
      """Fields extracted from an image crop via the OCR pipeline."""
      # This might be linked to a specific detection in a PipelineResult,
      # but as a base model it stores the extracted drug attributes.
      lot = models.CharField(max_length=100, null=True, blank=True)
      exp = models.CharField(max_length=100, null=True, blank=True)
      ndc = models.CharField(max_length=20, null=True, blank=True)
      barcode_ndc = models.CharField(max_length=20, null=True, blank=True)
      mfg = models.CharField(max_length=100, null=True, blank=True)
      product = models.CharField(max_length=100, null=True, blank=True)
      brand = models.CharField(max_length=100, null=True, blank=True)
      strength = models.CharField(max_length=100, null=True, blank=True)
      vol_ml = models.CharField(max_length=50, null=True, blank=True)
      mdv = models.CharField(max_length=10, null=True, blank=True)
      instructions = models.CharField(max_length=100, null=True, blank=True)
      # Confidences
      lot_confidence = models.FloatField(default=0.0)
      exp_confidence = models.FloatField(default=0.0)
      ndc_confidence = models.FloatField(default=0.0)
      barcode_ndc_confidence = models.FloatField(default=0.0)
      mfg_confidence = models.FloatField(default=0.0)
      product_confidence = models.FloatField(default=0.0)
      brand_confidence = models.FloatField(default=0.0)
      strength_confidence = models.FloatField(default=0.0)
      vol_ml_confidence = models.FloatField(default=0.0)
      mdv_confidence = models.FloatField(default=0.0)
      instructions_confidence = models.FloatField(default=0.0)

      raw_texts = models.JSONField(default=list)

      def __str__(self):
            return f"OCR results (NDC: {self.ndc or self.barcode_ndc})"
