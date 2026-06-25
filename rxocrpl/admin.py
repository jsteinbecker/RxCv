from django.contrib import admin
from .models import RxNormConcept, RxNormConceptRelation, Product

admin.site.register(RxNormConcept)
admin.site.register(RxNormConceptRelation)
admin.site.register(Product)
