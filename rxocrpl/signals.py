from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Labeler, PackagedProduct, Product


def _sync_labeler_products(labeler):
      Product.objects.filter(labeler=labeler).update(active=labeler.active)
      PackagedProduct.objects.filter(product__labeler=labeler).update(active=labeler.active)


@receiver(post_save, sender=Labeler)
def sync_products_with_labeler_active(sender, instance, update_fields=None, **kwargs):
      if update_fields is not None and "active" not in update_fields:
            return

      _sync_labeler_products(instance)


@receiver(post_save, sender=Product)
def sync_product_with_labeler_active(sender, instance, **kwargs):
      if not instance.labeler_id:
            PackagedProduct.objects.filter(product=instance).update(active=instance.active)
            return

      labeler_active = instance.labeler.active
      if instance.active != labeler_active:
            Product.objects.filter(pk=instance.pk).update(active=labeler_active)
            instance.active = labeler_active

      PackagedProduct.objects.filter(product=instance).update(active=labeler_active)


@receiver(post_save, sender=PackagedProduct)
def sync_package_with_labeler_active(sender, instance, **kwargs):
      product = instance.product
      if not product.labeler_id:
            return

      labeler_active = product.labeler.active
      if instance.active != labeler_active:
            PackagedProduct.objects.filter(pk=instance.pk).update(active=labeler_active)
            instance.active = labeler_active
