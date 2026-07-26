from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone


class User(AbstractUser):
      """Pharmacy staff. is_rph distinguishes pharmacists (who can verify) from techs."""
      USERNAME_FIELD = "username"
      is_rph = models.BooleanField(default=False)
      facility = models.ForeignKey(
            "rxocrpl.Facility",
            on_delete=models.SET_NULL,
            null=True,
            blank=True,
            related_name="users",
      )
      user_type = models.CharField(max_length=100, null=True, blank=True)

      def has_role (self, role_name: str, facility=None, organization=None) -> bool:
            """
            Check if user holds an active grant for the role at the given scope,
            considering hierarchy inheritance.
            """
            from rxocrpl.models import RoleGrant

            now = timezone.now()
            base_qs = RoleGrant.objects.filter(
                  user=self, role__name=role_name, revoked_at__isnull=True
            ).exclude(expires_at__lt=now)

            if organization:
                  if base_qs.filter(organization=organization).exists():
                        return True

            if facility:
                  if base_qs.filter(facility=facility).exists():
                        return True

                  curr = facility.parent
                  while curr:
                        if base_qs.filter(facility=curr).exists():
                              return True
                        curr = curr.parent

                  if facility.organization:
                        if base_qs.filter(organization=facility.organization).exists():
                              return True

            return False

      @property
      def is_admin (self) -> bool:
            """Returns True if the user has any active admin grant."""
            from rxocrpl.models import RoleGrant

            return (
                  RoleGrant.objects.filter(
                        user=self,
                        role__name__in=["org_admin", "facility_admin"],
                        revoked_at__isnull=True,
                  )
                  .exclude(expires_at__lt=timezone.now())
                  .exists()
            )


class TimeStampedModel(models.Model):
      """Shared created/updated timestamps for auditability and run provenance."""
      created_at = models.DateTimeField(auto_now_add=True)
      updated_at = models.DateTimeField(auto_now=True)

      class Meta:
            abstract = True


class Manufacturer(TimeStampedModel):
      name = models.CharField(max_length=255, verbose_name="Canonical Name", unique=True)

      def __str__ (self): return self.name


class ManufacturerAlias(TimeStampedModel):
      """Alternately names a manufacturer appears under on labels (e.g. 'Pfizer Inc.' vs 'Pfizer')."""
      name = models.CharField(max_length=255, unique=True)
      manufacturer = models.ForeignKey(Manufacturer, on_delete=models.CASCADE, related_name="aliases")
      verifier = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="verified_aliases", )

      def is_verified (self): return self.verifier is not None


class LabelerCode(TimeStampedModel):
      """The 4-5 digit FDA labeler code, first segment of an NDC."""
      code = models.CharField(max_length=5)
      manufacturer = models.ForeignKey(Manufacturer, on_delete=models.CASCADE, related_name="labeler_codes", )
      verifier = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="verified_labeler_codes", )

      class Meta:
            constraints = [
                  models.UniqueConstraint(fields=["manufacturer", "code"], name="unique_mfr_labeler_code"),
                  #                  models.CheckConstraint(condition=Q(code__regex=r"^\d{4,5}$"), name="labeler_code_format", ),
            ]

      def is_verified (self): return self.verifier is not None

      def __str__ (self): return self.code


class ComponentLibraryEntry(TimeStampedModel):
      """
      The canonical record for a type of container — e.g. 'Pfizer lidocaine 1% 50mL vial'.
      Identified by (manufacturer, code) where `code` is the product+package portion of the NDC.
      `data` holds parsed label fields (drug name, strength, form, NDC, etc.).
      """
      manufacturer = models.ForeignKey(Manufacturer, on_delete=models.CASCADE, related_name="library_entries")
      code = models.CharField(max_length=10)
      data = models.JSONField(default=dict)
      verifier = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="verified_library_entries", limit_choices_to={"is_rph": True})

      class Meta:
            constraints = [models.UniqueConstraint(fields=["manufacturer", "code"], name="unique_mfr_code"), ]

      def is_verified (self):
            return self.verifier is not None


class CompoundedSterileProduct(TimeStampedModel):
      """
      A single compounding event — the traceability record.
      Ties a pharmacist + timestamp + set of physical containers used.
      """
      preparer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                   related_name="prepared_compounds")
      verifier = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                   related_name="compounds",
                                   limit_choices_to={"is_rph": True})
      prepared_at = models.DateTimeField(auto_now_add=True)
      notes = models.TextField(blank=True)
      # Optional: formula/recipe FK, lot number assigned to the finished compound, BUD, etc.


class ComponentInstance(TimeStampedModel):
      """
      A specific physical bottle used in a compound. Captures the lot-level
      facts that the library entry (which describes the type) cannot.
      """
      component = models.ForeignKey(ComponentLibraryEntry, on_delete=models.CASCADE, related_name="instances")
      compound = models.ForeignKey(CompoundedSterileProduct, on_delete=models.CASCADE, related_name="components",
                                   null=True, blank=True)
      lot_number = models.CharField(max_length=64, blank=True)
      expiration = models.DateField(null=True, blank=True)
      volume_ml = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True,
                                      help_text="Volume drawn from this container in mL")


class ImageSet(TimeStampedModel):
      """A batch of label scans captured for one compounding event."""
      timestamp = models.DateTimeField(auto_now_add=True)
      compound = models.OneToOneField(
            CompoundedSterileProduct, on_delete=models.CASCADE, related_name="image_set",
            null=True, blank=True,
      )
      captured_by = models.ForeignKey(
            settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
            related_name="captured_image_sets",
      )


class Image(TimeStampedModel):
      image_set = models.ForeignKey(ImageSet, on_delete=models.CASCADE, related_name="images")
      path = models.CharField(max_length=1024)
      width = models.PositiveIntegerField(null=True, blank=True)
      height = models.PositiveIntegerField(null=True, blank=True)

      class Meta:
            constraints = [
                  models.UniqueConstraint(fields=["image_set", "path"], name="unique_imageset_path"),
            ]


class SegmentationAlgorithmChoices(models.TextChoices):
      PANOPTIC = "panoptic", "Panoptic"
      WATERSHED = "watershed", "Watershed"


class RunStatusChoices(models.TextChoices):
      PENDING = "pending", "Pending"
      RUNNING = "running", "Running"
      SUCCEEDED = "succeeded", "Succeeded"
      FAILED = "failed", "Failed"


class PipelineRun(TimeStampedModel):
      """A single reproducible segmentation run over an ImageSet."""

      image_set = models.ForeignKey(ImageSet, on_delete=models.CASCADE, related_name="pipeline_runs")
      initiated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                       related_name="pipeline_runs")
      algorithm = models.CharField(max_length=32, choices=SegmentationAlgorithmChoices.choices)
      status = models.CharField(max_length=16, choices=RunStatusChoices.choices, default=RunStatusChoices.PENDING)
      model_name = models.CharField(max_length=128, blank=True)
      model_version = models.CharField(max_length=128, blank=True)
      code_ref = models.CharField(max_length=64, blank=True, help_text="Git SHA or release tag")
      parameters = models.JSONField(default=dict, blank=True)
      metrics = models.JSONField(default=dict, blank=True)
      started_at = models.DateTimeField(auto_now_add=True)
      finished_at = models.DateTimeField(null=True, blank=True)
      notes = models.TextField(blank=True)

      class Meta:
            indexes = [
                  models.Index(fields=["image_set", "algorithm", "started_at"]),
                  models.Index(fields=["status"]),
            ]
            constraints = [
                  #                  models.CheckConstraint(
                  #                        condition=Q(finished_at__isnull=True) | Q(finished_at__gte=F("started_at")),
                  #                        name="run_finished_after_started",
                  #                  ),
            ]


class SegmentationResult(TimeStampedModel):
      """Per-image artifacts and summary stats emitted by a PipelineRun."""

      run = models.ForeignKey(PipelineRun, on_delete=models.CASCADE, related_name="results")
      image = models.ForeignKey(Image, on_delete=models.CASCADE, related_name="segmentation_results")
      label_map_path = models.CharField(max_length=1024, blank=True)
      edge_mask_path = models.CharField(max_length=1024, blank=True)
      panoptic_map_path = models.CharField(max_length=1024, blank=True)
      marker_map_path = models.CharField(max_length=1024, blank=True)
      num_regions = models.PositiveIntegerField(default=0)
      stats = models.JSONField(default=dict, blank=True)

      class Meta:
            constraints = [
                  models.UniqueConstraint(fields=["run", "image"], name="unique_run_image_result"),
            ]


class SegmentKindChoices(models.TextChoices):
      INSTANCE = "instance", "Instance"
      STUFF = "stuff", "Stuff"
      WATERSHED_BASIN = "watershed_basin", "Watershed basin"


class ImageSegment(TimeStampedModel):
      """
      A bounding box on a label image identifying one container.
      Links the visual evidence to the ComponentInstance it depicts.
      """
      image = models.ForeignKey(Image, on_delete=models.CASCADE, related_name="segments")
      result = models.ForeignKey(SegmentationResult, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name="segments", )
      box = models.JSONField(default=dict)  # expected: {"x": int, "y": int, "w": int, "h": int}
      polygon = models.JSONField(default=list, blank=True)
      component = models.ForeignKey(ComponentInstance, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="segments", )
      kind = models.CharField(max_length=24, choices=SegmentKindChoices.choices, default=SegmentKindChoices.INSTANCE)
      class_label = models.CharField(max_length=128, blank=True)
      instance_id = models.PositiveIntegerField(null=True, blank=True)
      score = models.DecimalField(max_digits=5, decimal_places=3, null=True, blank=True,
                                  validators=[MinValueValidator(0), MaxValueValidator(1)], )
      area_px = models.PositiveIntegerField(null=True, blank=True)
      centroid = models.JSONField(default=dict, blank=True)

      class Meta:
            indexes = [
                  models.Index(fields=["image", "kind"]),
                  models.Index(fields=["result", "kind"]),
            ]
            constraints = [
                  models.UniqueConstraint(
                        fields=["result", "instance_id"],
                        condition=Q(result__isnull=False, instance_id__isnull=False),
                        name="unique_instance_per_result",
                  ),
            ]


class OcrRoleChoices(models.IntegerChoices):
      TEXT = 1, "Text"
      COMPONENT_NAME = 2, "Component name"
      MFG = 3, "Manufacturer"
      STRENGTH = 4, "Strength"
      ROUTE = 5, "Route"
      EXPIRATION = 6, "Expiration"
      LOT_NUMBER = 7, "Lot number"
      NDC = 8, "NDC"
      PACKAGE = 9, "Package"


class OcrToken(TimeStampedModel):
      token = models.TextField()
      segment = models.ForeignKey(ImageSegment, on_delete=models.CASCADE, related_name="tokens")
      source_run = models.ForeignKey(PipelineRun, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name="ocr_tokens", )
      confidence = models.DecimalField(max_digits=5, decimal_places=3,
                                       validators=[MinValueValidator(0), MaxValueValidator(1)], )
      role = models.IntegerField(choices=OcrRoleChoices.choices, default=OcrRoleChoices.TEXT)
      role_confidence = models.DecimalField(max_digits=5, decimal_places=3,
                                            validators=[MinValueValidator(0), MaxValueValidator(1)], )
      bbox = models.JSONField(default=dict, blank=True)
      metadata = models.JSONField(default=dict, blank=True)

      class Meta:
            indexes = [
                  models.Index(fields=["segment", "role"]),
                  models.Index(fields=["source_run", "role"]),
            ]
