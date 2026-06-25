"""RxNorm concept traversal and enrichment.

This module builds on the raw API calls in :mod:`rxnorm.client` to provide
higher-level operations: resolving concepts, grouping volume variants, and
assembling the full enrichment record for an NDC.

Volume-variant grouping background
-----------------------------------
Quantified SCDs (e.g. *5 ML Sodium Chloride 0.9% Injection*) share an
unquantified SCD base (*Sodium Chloride 0.9% Injection*) that carries
``SUPPRESS="E"`` in RxNorm.  Because active-scoped endpoints silently drop
``SUPPRESS="E"`` atoms:

  * ``/related?rela=quantified_form_of`` almost always returns nothing for an
    active quantified child — the base is invisible to that endpoint.
  * ``historystatus`` (current+historical scope) *is* aware of the base, but
    only exposes it in the *base → children* direction via ``quantifiedConcept``.

There is therefore no clean active-API path from a quantified child up to its
suppressed base; ``RXNREL.RRF`` is authoritative for that direction.

As a practical workaround :func:`get_unquantified_form` strips the ``<n> ML``
prefix from the child's name and resolves the remainder with ``allsrc=1``.
When that also fails, :func:`get_scdc_group_key` returns a frozenset of
ingredient+strength (SCDC) CUIs shared by all volume variants — a reliable
active-concept proxy that avoids the suppressed base entirely.
"""
from __future__ import annotations
from typing import Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, NamedTuple
import re

import requests

from rxocrpl.rxnorm.client import (
      fetch_history_status,
      fetch_ndc_rxcui,
      fetch_related_by_rela,
      fetch_related_by_tty,
      fetch_rxcui_by_name,
      fetch_rxcui_name,
)
from rxocrpl.rxnorm.dataclasses import (
      ConceptStatus,
      RelatedLevel,
      RxConcept,
      RxNavError,
      VolumeGroupKey,
)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class NdcRxcui(NamedTuple):
      """A single ``(rxcui, tty)`` match returned by an NDC look-up.

      A :class:`~typing.NamedTuple` rather than a dataclass so that legacy
      positional access (``item[0]`` / ``item[1]``) keeps working and the value
      still serializes to a 2-element JSON array unchanged.
      """

      rxcui: str
      tty: str


@dataclass(frozen=True)
class RxNormEnrichment:
      """All relevant RxNorm data collected for a single NDC.

      Holds the rich typed objects (:class:`RxConcept`, :class:`ConceptStatus`,
      :class:`VolumeGroupKey`); call :meth:`to_dict` for the JSON-serializable
      view.  Being frozen, an instance returned from the cached
      :func:`get_rxnorm_enrichment` cannot be mutated by one caller in a way that
      corrupts the value seen by the next.

      Attributes
      ----------
      rxcui
          NDC resolved at all three relation scopes (concept / drug / product),
          mapping each scope to its list of ``(rxcui, tty)`` matches.
      concept_rxcui
          The primary concept-level RxCUI string, or ``None``.  May be populated
          even when ``concept`` is ``None`` (the CUI was selected but could not
          be resolved to a full concept).
      concept
          The resolved primary :class:`RxConcept`, or ``None``.
      status
          The concept's :class:`ConceptStatus`, or ``None``.
      scdc_group
          Sorted SCDC CUIs (ingredient+strength identity).
      volume_group_key
          The :class:`VolumeGroupKey` when the NDC resolves to a quantified
          form; ``None`` otherwise.
      """
      rxcui: dict[RelatedLevel, list[NdcRxcui]] = field(default_factory=dict)
      concept_rxcui: str | None = None
      concept: RxConcept | None = None
      status: ConceptStatus | None = None
      scdc_group: tuple[str, ...] = ()
      volume_group_key: VolumeGroupKey | None = None

      def to_dict(self) -> dict[str, Any]:
            """Render as the JSON-serializable schema callers previously received."""
            return {
                  "rxcui": {
                        level.value: [list(item) for item in items]
                        for level, items in self.rxcui.items()
                  },
                  "concept": str(self.concept) if self.concept else None,
                  "concept_rxcui": self.concept_rxcui,
                  "status": self.status.value if self.status else None,
                  "scdc_group": list(self.scdc_group),
                  "volume_group_key": (
                        str(self.volume_group_key) if self.volume_group_key else None
                  ),
            }

      def get(self, key: str, default: Any = None) -> Any:
            """Allow dict-like access to attributes."""
            return getattr(self, key, default)

      def items(self) -> Iterable[tuple[str, Any]]:
            """Allow dict-like iteration over attributes."""
            for key in self.__dataclass_fields__:
                  yield key, getattr(self, key)


def get_concept(rxcui: str) -> RxConcept | None:
      """Resolve *rxcui* to a fully-populated :class:`~rxnorm.models.RxConcept`.

      Resolution strategy:
      1. Check the local database (`RxNormConcept` model) if available.
      2. Fall back to `historystatus.minConcept` (API) because it carries the `tty` field.
      3. Fall back to a name-only look-up (`/rxcui` endpoint).

      Returns ``None`` when the concept cannot be resolved at all.
      """
      # 1. Local database check
      try:
            from rxocrpl.models import RxNormConcept
            local = RxNormConcept.objects.filter(rxcui=rxcui).first()
            if local:
                  return RxConcept(rxcui=local.rxcui, name=local.name, tty=local.tty)
      except (ImportError, Exception):
            # Django might not be configured or model might not exist
            pass

      # 2. API fallback
      mc = fetch_history_status(rxcui).get("minConcept") or {}
      if mc.get("rxcui"):
            return RxConcept.from_props(mc)

      name = fetch_rxcui_name(rxcui)
      return RxConcept(rxcui, name, None) if name else None


def get_concept_status(rxcui: str) -> ConceptStatus | None:
      """Return the :class:`~rxnorm.models.ConceptStatus` for *rxcui*, or ``None``."""
      raw = fetch_history_status(rxcui).get("status")
      return ConceptStatus(raw) if raw else None


def get_all_rxcui(ndc: str) -> dict[RelatedLevel, list[NdcRxcui]]:
      """Resolve *ndc* at all three relation scopes (concept / drug / product).

      Returns a dict mapping each :class:`RelatedLevel` scope to a list of
      :class:`NdcRxcui` matches, omitting scopes where no match was found.
      Returns ``{}`` for an empty NDC.
      """
      if not ndc:
            return {}
      result: dict[RelatedLevel, list[NdcRxcui]] = {}
      for level in RelatedLevel:
            items = fetch_ndc_rxcui(ndc, level)
            if items:
                  result[level] = [NdcRxcui(item[0], item[1]) for item in items]
      return result


def get_rxcui_related(rxcui: str, rela: str) -> list[RxConcept]:
      """List all related concepts for *rxcui* via *rela*."""
      url = f"https://rxnav.nlm.nih.gov/REST/Prescribe/rxcui/{rxcui}/allrelated.json"
      res = requests.get(url)
      if res.status_code != 200:
            raise RxNavError(f"RxNav returned {res.status_code} for {url}: {res.text}")
      body = res.json()
      results: list[RxConcept] = []
      for opt in (body.get("allRelatedGroup") or {}).get("conceptGroup") or []:
            if opt.get("tty") == rela:
                  for choice in opt.get("conceptProperties") or []:
                        results.append(RxConcept.from_props(choice, default_tty=rela))
      return results


def get_quantified_forms(base_rxcui: str) -> list[RxConcept]:
      """List the quantified (volume) variants of an unquantified SCD base.

      Uses the ``has_quantified_form`` direction exposed by ``historystatus``.
      Returns ``[]`` when *base_rxcui* is not an unquantified SCD base.
      """
      status = fetch_history_status(base_rxcui)
      return [RxConcept.from_props(c) for c in (status.get("quantifiedConcept") or [])]


def get_unquantified_form(rxcui: str) -> RxConcept | None:
      """The unquantified SCD that groups every volume variant of a quantified SCD.

      Resolution strategy (in order):

      1. **Direct rela** — ``quantified_form_of`` via ``/related``.  Normally
         returns nothing because the base is ``SUPPRESS="E"``; included for
         completeness and future NLM changes.
      2. **Name fallback** — strip the leading ``<n> ML`` prefix from the child's
         name, then resolve the remainder with ``allsrc=1``.

      Returns ``None`` when both strategies fail.  In that case callers should
      fall back to :func:`get_scdc_group_key`.
      """
      for concept in fetch_related_by_rela(rxcui, "quantified_form_of"):
            if concept.tty == "SCD":
                  return concept

      name = fetch_rxcui_name(rxcui)
      if not name:
            return None
      base_name = RxConcept(rxcui, name).unquantified_name
      if base_name == name:
            return None  # no volume prefix → already unquantified

      cui = fetch_rxcui_by_name(base_name, allsrc=True)
      return RxConcept(cui, base_name, "SCD") if cui else None


def get_scdc_group_key(rxcui: str) -> frozenset[str]:
      """Ingredient+strength identity key for *rxcui* expressed as a CUI frozenset.

      Every volume variant of the same drug reduces to the same set of SCDC
      (ingredient+strength component) CUIs, making this frozenset a reliable
      proxy for the suppressed SCD base.

      .. note::
          SCDC identity encodes ingredient and strength but **not** dose form.
          An Injection and an oral solution at identical strengths will share the
          same key.
      """
      return frozenset(c.rxcui for c in fetch_related_by_tty(rxcui, "SCDC") if c.rxcui)


def get_volume_group_key(rxcui: str) -> VolumeGroupKey:
      """Best available handle linking the volume variants of *rxcui*'s drug.

      Returns a :class:`~rxnorm.models.VolumeGroupKey` of kind ``"scd"`` when
      the unquantified SCD base resolves via the API; otherwise kind ``"scdc"``
      with the shared SCDC component CUIs.
      """
      base = get_unquantified_form(rxcui)
      if base:
            return VolumeGroupKey(kind="scd", base=base)
      return VolumeGroupKey(kind="scdc", components=get_scdc_group_key(rxcui))


def _select_concept_rxcui(items: list[NdcRxcui]) -> str | None:
      """Pick the best concept-level RxCUI, preferring SCSD then SCD.

      Falls back to the first match when neither preferred TTY is present, and
      returns ``None`` for an empty list.
      """
      if not items:
            return None
      for preferred_tty in ("SCSD", "SCD"):
            for item in items:
                  if item.tty == preferred_tty:
                        return item.rxcui
      return items[0].rxcui


@lru_cache(maxsize=256)
def get_rxnorm_enrichment(ndc: str) -> RxNormEnrichment:
      """Collect all relevant RxNorm data for *ndc* into an :class:`RxNormEnrichment`.

      Orchestrates :func:`get_all_rxcui` → :func:`get_concept` →
      :func:`get_concept_status` → :func:`get_scdc_group_key` →
      :func:`get_volume_group_key`, all individually cached, so repeated calls
      for the same NDC are free after the first fetch.

      Returns an empty :class:`RxNormEnrichment` for an empty NDC.  Use
      :meth:`RxNormEnrichment.to_dict` for the JSON-serializable form.
      """
      if not ndc:
            return RxNormEnrichment()

      rxcui_map = get_all_rxcui(ndc)
      concept_items = rxcui_map.get(RelatedLevel.CONCEPT) or []
      concept_rxcui = _select_concept_rxcui(concept_items)

      if not concept_rxcui:
            return RxNormEnrichment(rxcui=rxcui_map, concept_rxcui=concept_rxcui)

      try:
            concept = get_concept(concept_rxcui)
      except RxNavError:
            return RxNormEnrichment(rxcui=rxcui_map, concept_rxcui=concept_rxcui)

      if not concept:
            return RxNormEnrichment(rxcui=rxcui_map, concept_rxcui=concept_rxcui)

      status: ConceptStatus | None = None
      try:
            status = get_concept_status(concept_rxcui)
      except RxNavError:
            pass

      scdc_group: tuple[str, ...] = ()
      try:
            scdc_group = tuple(sorted(get_scdc_group_key(concept_rxcui)))
      except RxNavError:
            pass

      volume_group_key: VolumeGroupKey | None = None
      if concept.is_quantified:
            try:
                  volume_group_key = get_volume_group_key(concept_rxcui)
            except RxNavError:
                  pass

      return RxNormEnrichment(
            rxcui=rxcui_map,
            concept_rxcui=concept_rxcui,
            concept=concept,
            status=status,
            scdc_group=scdc_group,
            volume_group_key=volume_group_key,
      )


NDC_10_FORMAT = re.compile(r"^\d{4}-\d{4}-\d{2}$|^\d{5}-\d{3}-\d{2}$|^\d{5}-\d{4}-\d{1}$|^\d{10}$")
NDC_11_FORMAT = re.compile(r"^\d{5}-\d{4}-\d{2}$|^\d{11}$")


def valid_ndc_format(ndc: str) -> bool:
      """Check if the NDC is in a valid 10-digit or 11-digit format."""
      return bool(NDC_10_FORMAT.match(ndc) or NDC_11_FORMAT.match(ndc))


if __name__ == "__main__":
      print(get_rxcui_related("221124", "SCDC"))
      print(get_rxcui_related("1370474", "PIN"))
      print(get_rxcui_related("1370474", "BN"))
      print(get_rxcui_related("1370474", "SCD"))
      print(get_rxcui_related("1370474", "SCDC"))

      print(get_rxnorm_enrichment("10019-653-64"))

      print(valid_ndc_format("10019-653-64"))
      print(valid_ndc_format("10019-653-64-1"))
      print(valid_ndc_format("1001965364"))
      print(valid_ndc_format("10019065364"))
