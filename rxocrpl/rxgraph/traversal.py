"""Read-side convenience helpers for walking the materialized RxNorm graph.

These give you the typed-accessor ergonomics of per-TTY models without giving
up the single concept+relation schema. Import the functions, or mix
``RxConceptTraversalMixin`` into your RxNormConcept model for attribute access::

    scd.ingredients()      # -> QuerySet[RxNormConcept] of TTY=IN
    scd.components()       # -> SCDC concepts that constitute this SCD
    scd.brand_names()      # -> BN concepts (via IN.has_tradename)
    scd.dose_form_group()  # -> SCDG concept(s) this SCD rolls up into
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional, cast

if TYPE_CHECKING:
      from django.db.models import QuerySet
      from rxocrpl.models import RxNormConcept

from .pipeline import (
      RELATION_RELA_FIELD,
      RELATION_SOURCE_FIELD,
      RELATION_TARGET_FIELD,
      _resolve_models,
)


def _related(
          concept: RxNormConcept,
          *,
          rela: Optional[str] = None,
          outgoing: bool = True,
          target_tty: Optional[str] = None,
) -> QuerySet[RxNormConcept]:
      """Return a QuerySet of RxNormConcepts related to ``concept``.

    ``outgoing=True`` follows edges where ``concept`` is the source; otherwise
    where it is the target. Filter by ``rela`` and/or the neighbor's ``tty``.
    """
      concept_model, relation_model = _resolve_models()

      edge_filter: dict[str, Any] = {}

      if outgoing:
            edge_filter[RELATION_SOURCE_FIELD] = concept
            neighbor_attr = RELATION_TARGET_FIELD
      else:
            edge_filter[RELATION_TARGET_FIELD] = concept
            neighbor_attr = RELATION_SOURCE_FIELD

      if rela is not None:
            edge_filter[RELATION_RELA_FIELD] = rela

      neighbor_ids = (
            relation_model.objects.filter(**edge_filter)
            .values_list(f"{neighbor_attr}_id", flat=True)
      )

      qs = cast("QuerySet[RxNormConcept]", concept_model.objects.filter(pk__in=neighbor_ids))

      if target_tty is not None:
            qs = qs.filter(tty=target_tty)

      return qs


def ingredients(concept: RxNormConcept) -> QuerySet[RxNormConcept]:
      """IN concepts directly tied to this concept (SCD/SCDC has_ingredient IN)."""
      return _related(concept, rela="has_ingredient", outgoing=True, target_tty="IN")


def precise_ingredients(concept: RxNormConcept) -> QuerySet[RxNormConcept]:
      return _related(concept, rela="has_precise_ingredient", outgoing=True, target_tty="PIN")


def components(concept: RxNormConcept) -> QuerySet[RxNormConcept]:
      """SCDC components of an SCD (SCD consists_of SCDC)."""
      return _related(concept, rela="consists_of", outgoing=True, target_tty="SCDC")


def clinical_drugs(scdc_concept: RxNormConcept) -> QuerySet[RxNormConcept]:
      """SCDs that this SCDC constitutes (inverse of consists_of)."""
      return _related(scdc_concept, rela="consists_of", outgoing=False, target_tty="SCD")


def brand_names(concept: RxNormConcept) -> QuerySet[RxNormConcept]:
      """BN concepts reachable from this concept's ingredient(s)."""
      concept_model, _ = _resolve_models()

      in_ids: list[Any] = list(ingredients(concept).values_list("pk", flat=True)) or [concept.pk]
      bn_ids: set[Any] = set()

      for in_pk in in_ids:
            in_obj = cast("RxNormConcept", concept_model.objects.get(pk=in_pk))

            bn_ids.update(
                  _related(in_obj, rela="has_tradename", outgoing=True, target_tty="BN")
                  .values_list("pk", flat=True)
            )
      return cast("QuerySet[RxNormConcept]", concept_model.objects.filter(pk__in=bn_ids))


def dose_form_group(concept: RxNormConcept) -> QuerySet[RxNormConcept]:
      """SCDG concept(s) that this SCD rolls up into (SCDG inverse_isa SCD)."""
      return _related(concept, rela="inverse_isa", outgoing=False, target_tty="SCDG")


def branded_drugs(concept: RxNormConcept) -> QuerySet[RxNormConcept]:
      """SBD branded forms of this SCD (SCD has_tradename SBD)."""
      return _related(concept, rela="has_tradename", outgoing=True, target_tty="SBD")


def clinical_drug(concept: RxNormConcept) -> QuerySet[RxNormConcept]:
      """The SCD this SBD is a tradename of (inverse of SCD has_tradename)."""
      return _related(concept, rela="has_tradename", outgoing=False, target_tty="SCD")


class RxConceptTraversalMixin:
      """Mix into your RxNormConcept model for ``concept.ingredients()`` access."""

      def ingredients(self: Any) -> QuerySet[RxNormConcept]:
            return ingredients(self)

      def precise_ingredients(self: Any) -> QuerySet[RxNormConcept]:
            return precise_ingredients(self)

      def components(self: Any) -> QuerySet[RxNormConcept]:
            return components(self)

      def clinical_drugs(self: Any) -> QuerySet[RxNormConcept]:
            return clinical_drugs(self)

      def brand_names(self: Any) -> QuerySet[RxNormConcept]:
            return brand_names(self)

      def dose_form_group(self: Any) -> QuerySet[RxNormConcept]:
            return dose_form_group(self)

      def branded_drugs(self: Any) -> QuerySet[RxNormConcept]:
            return branded_drugs(self)

      def clinical_drug(self: Any) -> QuerySet[RxNormConcept]:
            return clinical_drug(self)
