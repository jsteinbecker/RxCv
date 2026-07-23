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

      def __repr__(self):
            return f"rxcui<{self.rxcui},({self.tty})>"


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
      labeler
          The resolved :class:`Labeler` for the NDC's labeler segment, or
          ``None``.  Populated independently of RxNorm resolution — an NDC with
          no RxCUI still has a labeler.
      """
      rxcui: dict[RelatedLevel, list[NdcRxcui]] = field(default_factory=dict)
      concept_rxcui: str | None = None
      concept: RxConcept | None = None
      status: ConceptStatus | None = None
      scdc_group: tuple[str, ...] = ()
      volume_group_key: VolumeGroupKey | None = None
      labeler: Labeler | None = None

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
                  "labeler": self.labeler.to_dict() if self.labeler else None,
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
            items = fetch_ndc_rxcui(ndc, level.value)
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

      labeler = get_labeler(ndc)

      # Skip RxNorm network calls that are unlikely to enrich.  A labeler
      # resolved from the local curation file with ``in_rxnorm=False`` has no
      # products in RxNorm, so NDC→RxCUI resolution will almost certainly miss.
      # Return early with just the labeler (which resolved offline) rather than
      # spending three ``relatedndc`` requests to confirm the miss.  openFDA-
      # sourced labelers carry ``in_rxnorm=None`` and are *not* skipped.
      if labeler is not None and labeler.in_rxnorm is False:
            return RxNormEnrichment(labeler=labeler)

      rxcui_map = get_all_rxcui(ndc)
      concept_items = rxcui_map.get(RelatedLevel.CONCEPT) or []
      concept_rxcui = _select_concept_rxcui(concept_items)

      if not concept_rxcui:
            return RxNormEnrichment(rxcui=rxcui_map, concept_rxcui=concept_rxcui, labeler=labeler)

      try:
            concept = get_concept(concept_rxcui)
      except RxNavError:
            return RxNormEnrichment(rxcui=rxcui_map, concept_rxcui=concept_rxcui, labeler=labeler)

      if not concept:
            return RxNormEnrichment(rxcui=rxcui_map, concept_rxcui=concept_rxcui, labeler=labeler)

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
            labeler=labeler,
      )


NDC_10_FORMAT = re.compile(r"^\d{4}-\d{4}-\d{2}$|^\d{5}-\d{3}-\d{2}$|^\d{5}-\d{4}-\d{1}$|^\d{10}$")
NDC_11_FORMAT = re.compile(r"^\d{5}-\d{4}-\d{2}$|^\d{11}$")


def valid_ndc_format(ndc: str) -> bool:
      """Check if the NDC is in a valid 10-digit or 11-digit format."""
      return bool(NDC_10_FORMAT.match(ndc) or NDC_11_FORMAT.match(ndc))


# ---------------------------------------------------------------------------
# Labeler resolution
# ---------------------------------------------------------------------------
#
# The labeler code is the first segment of an NDC.  Resolution escalates:
#
#   1. ``Labelers.active.json`` — a curated local file, hit first because it is
#      free, offline, and carries product counts that openFDA does not.
#   2. openFDA ``/drug/ndc.json`` — the network fallback, queried only when the
#      local file misses.  Its answer is memoized in ``_OPENFDA_CACHE`` so a
#      miss costs one request per labeler code, not one per NDC.
#
# A labeler code has no fixed width: it is 4, 5, or 6 digits depending on the
# NDC's segment configuration.  ``labeler_code_candidates`` therefore yields
# every plausible reading rather than guessing one, and lookup tries each in
# turn.  This is why an unpadded 10-digit NDC is ambiguous and a hyphenated one
# is not.

import json
import os
from pathlib import Path

_LABELERS_PATH = Path(
      os.environ.get("LABELERS_JSON", Path(__file__).with_name("Labelers.active.json"))
)
_OPENFDA_NDC_URL = "https://api.fda.gov/drug/ndc.json"


@dataclass(frozen=True)
class Labeler:
      """A resolved NDC labeler.

      Attributes
      ----------
      code
          The labeler segment of the NDC, as matched (zero-padding preserved).
      name
          Short/common name (the JSON key locally; the openFDA
          ``labeler_name`` otherwise).
      full_name
          Registered corporate name, when known.
      source
          ``"local"`` or ``"openfda"`` — which tier answered.
      codes
          Every labeler code owned by this labeler.  Only populated from the
          local file; openFDA answers for one code at a time.
      active_rx_product_count, active_ndc_product_count, in_rxnorm
          Curation metadata, local file only.
      """

      code: str
      name: str
      full_name: str | None = None
      source: str = "local"
      codes: tuple[str, ...] = ()
      active_rx_product_count: int | None = None
      active_ndc_product_count: int | None = None
      in_rxnorm: bool | None = None

      @property
      def has_active_ndc_products(self) -> bool:
            return bool(self.active_ndc_product_count)

      def to_dict(self) -> dict[str, Any]:
            return {
                  "code": self.code,
                  "name": self.name,
                  "full_name": self.full_name,
                  "source": self.source,
                  "codes": list(self.codes),
                  "active_rx_product_count": self.active_rx_product_count,
                  "active_ndc_product_count": self.active_ndc_product_count,
                  "in_rxnorm": self.in_rxnorm,
            }

      def __str__(self) -> str:
            return f"{self.name} <{self.code}>"


def labeler_code_candidates(ndc: str) -> tuple[str, ...]:
      """Every plausible labeler segment for *ndc*, most-likely first.

      Hyphenated NDCs are unambiguous — the first segment *is* the code, and it
      is returned alone.  Bare digit strings are not: an 11-digit NDC is 5-4-2
      by convention, but a 10-digit one may be 4-4-2, 5-3-2, or 5-4-1, so the
      5- and 4-digit readings are both returned.  Returns ``()`` for input that
      cannot be an NDC.
      """
      if not ndc:
            return ()

      if "-" in ndc:
            head = ndc.split("-", 1)[0]
            return (head,) if head.isdigit() else ()

      digits = "".join(ch for ch in ndc if ch.isdigit())
      if len(digits) == 11:
            return (digits[:5],)
      if len(digits) == 10:
            # 5-3-2 / 5-4-1 both start with 5; 4-4-2 starts with 4.
            return (digits[:5], digits[:4])
      if len(digits) >= 4:
            return (digits[:5], digits[:4]) if len(digits) >= 5 else (digits[:4],)
      return ()


@lru_cache(maxsize=1)
def _load_labelers() -> dict[str, Labeler]:
      """Index ``Labelers.active.json`` by labeler code.

      The file is a list of single-key objects (``[{"Eli Lilly": {...}}, ...]``);
      this flattens it into ``{code: Labeler}`` so lookup is O(1).  A labeler
      owning several codes is indexed under each.  Returns ``{}`` when the file
      is absent or unreadable — a missing curation file degrades to the openFDA
      tier rather than raising.
      """
      try:
            raw = json.loads(_LABELERS_PATH.read_text())
      except (OSError, json.JSONDecodeError):
            return {}

      index: dict[str, Labeler] = {}
      for entry in raw or []:
            if not isinstance(entry, dict):
                  continue
            for name, meta in entry.items():
                  meta = meta or {}
                  codes = tuple(str(c) for c in (meta.get("codes") or []))
                  for code in codes:
                        index[code] = Labeler(
                              code=code,
                              name=name,
                              full_name=meta.get("full_name"),
                              source="local",
                              codes=codes,
                              active_rx_product_count=meta.get("active_rx_product_count"),
                              active_ndc_product_count=meta.get("active_ndc_product_count"),
                              in_rxnorm=meta.get("in_rxnorm"),
                        )
      return index


@lru_cache(maxsize=512)
def fetch_openfda_labeler(code: str) -> Labeler | None:
      """Resolve a labeler *code* via openFDA, or ``None`` if unknown.

      Queries ``product_ndc`` on the code prefix and reads ``labeler_name`` off
      the first result.  A 404 from openFDA means "no such labeler" and yields
      ``None``; any other transport failure also yields ``None`` so that a flaky
      network degrades the enrichment rather than aborting it.
      """
      if not code:
            return None
      try:
            res = requests.get(
                  _OPENFDA_NDC_URL,
                  params={"search": f'product_ndc:"{code}"*'},
                  timeout=100,
            )
      except requests.RequestException:
            return None

      if res.status_code == 404:  # openFDA's "no matches"
            return None
      if res.status_code != 200:
            return None

      results = (res.json() or {}).get("results") or []
      if not results:
            return None

      name = results[0].get("labeler_name")
      if not name:
            return None
      return Labeler(code=code, name=name, full_name=name, source="openfda")


@lru_cache(maxsize=512)
def get_labeler(ndc: str) -> Labeler | None:
      """Resolve the labeler for *ndc*, escalating local → openFDA.

      Every candidate code is tried against the local index before *any*
      network call is made, so an ambiguous 10-digit NDC cannot fall through to
      openFDA merely because its first candidate reading missed locally.
      Returns ``None`` when no tier resolves.
      """
      candidates = labeler_code_candidates(ndc)
      if not candidates:
            return None

      local = _load_labelers()
      for code in candidates:
            if code in local:
                  return local[code]

      for code in candidates:
            found = fetch_openfda_labeler(code)
            if found:
                  return found

      for code in (ndc, ndc.replace("-", "")):
            found = fetch_openfda_labeler(code)
            if found:
                  return found

      return None


if __name__ == "__main__":
      from rxocrpl.rxnorm._ansi import (
            BOLD, CYAN, DIM, GREEN, MAGENTA, RED, RESET, YELLOW,
            concept, cui, error, field, hdr, listing, ok, status, tty,
      )

      hdr("1. allrelated — every concept in a drug's neighborhood")
      for rxcui, term in (("221124", "SCDC"), ("1370474", "PIN"),
                          ("1370474", "BN"), ("1370474", "SCD"),
                          ("1370474", "SCDC")):
            listing(f"{rxcui} → {term}", get_rxcui_related(rxcui, term))

      hdr("2. Concept + status resolution")
      for rxcui in ("1370474", "1791700", "221124"):
            c = get_concept(rxcui)
            field(rxcui, concept(c))
            field("", status(get_concept_status(rxcui)), pad=22)
            if c:
                  ok(c.is_quantified, f"is_quantified = {c.is_quantified}")

      hdr("3. Volume-variant grouping: base → children")
      base = "1791701"
      variants = get_quantified_forms(base)
      listing(f"quantified forms of {base}", variants)
      ok(bool(variants), "historystatus exposes the base → children direction")

      hdr("4. …but children → base is the hard direction")
      child = "1791700"
      base_concept = get_unquantified_form(child)
      field("direct rela", f"{DIM}(SUPPRESS=\"E\" — expect nothing){RESET}")
      field("name fallback", concept(base_concept))
      ok(base_concept is not None,
         "recovered via name-strip + allsrc=1" if base_concept
         else f"{RED}both strategies failed → fall back to SCDC{RESET}")

      hdr("5. SCDC group key — the suppression-proof proxy")
      for rxcui in ("1791700", "1791702"):
            key = get_scdc_group_key(rxcui)
            field(rxcui, f"{YELLOW}{sorted(key)}{RESET}")
      a, b = get_scdc_group_key("1791700"), get_scdc_group_key("1791702")
      ok(a == b and bool(a),
         "two volume variants share one SCDC key" if a == b
         else "keys diverge — not the same drug")
      print(f"  {DIM}NB: SCDC encodes ingredient+strength but NOT dose form.{RESET}")

      hdr("6. get_volume_group_key — best available handle")
      for rxcui in ("1791700", "1370474"):
            k = get_volume_group_key(rxcui)
            kind_color = GREEN if k.kind == "scd" else YELLOW
            field(rxcui, f"{BOLD}{kind_color}{k.kind}{RESET} {DIM}{k}{RESET}",
                  "clean base" if k.kind == "scd" else "SCDC fallback")

      hdr("7. Full enrichment for an NDC")
      enr = get_rxnorm_enrichment("10019-653-64")
      field("concept", concept(enr.concept))
      field("concept_rxcui", cui(enr.concept_rxcui))
      field("status", status(enr.status))
      field("scdc_group", f"{YELLOW}{list(enr.scdc_group)}{RESET}")
      field("volume_group_key", f"{DIM}{enr.volume_group_key}{RESET}")
      print()
      for level, items in enr.rxcui.items():
            listing(level.value, items,
                    render=lambda i: f"{cui(i.rxcui)} [{tty(i.tty)}]")

      hdr("8. TTY preference in _select_concept_rxcui")
      cases = [
            ("SCSD wins over SCD", [NdcRxcui("111", "SCD"), NdcRxcui("222", "SCSD")], "222"),
            ("SCD when no SCSD", [NdcRxcui("111", "SBD"), NdcRxcui("222", "SCD")], "222"),
            ("first when neither", [NdcRxcui("111", "BPCK"), NdcRxcui("222", "GPCK")], "111"),
            ("None when empty", [], None),
      ]
      for label, items, expected in cases:
            got = _select_concept_rxcui(items)
            ok(got == expected, f"{label:<22} → {cui(got)}")

      hdr("9. NDC format validation")
      for ndc, expect in (("10019-653-64", True), ("10019-653-64-1", False),
                          ("1001965364", True), ("10019065364", True),
                          ("0002-7510-01", True), ("abc-def-gh", False)):
            got = valid_ndc_format(ndc)
            mark = f"{GREEN}valid{RESET}" if got else f"{RED}invalid{RESET}"
            ok(got == expect, f"{YELLOW}{ndc:<16}{RESET} {mark}")

      hdr("10. Empty-input short circuits")
      ok(get_all_rxcui("") == {}, "get_all_rxcui('') → {}")
      empty = get_rxnorm_enrichment("")
      ok(empty.concept is None and not empty.rxcui,
         "get_rxnorm_enrichment('') → empty RxNormEnrichment")
      field("to_dict()", f"{DIM}{empty.to_dict()}{RESET}")

      hdr("11. Enrichment cache")
      info = get_rxnorm_enrichment.cache_info()
      print(f"  {CYAN}{'get_rxnorm_enrichment':<24}{RESET} "
            f"{GREEN}{info.hits} hits{RESET} {DIM}/{RESET} "
            f"{YELLOW}{info.misses} misses{RESET} "
            f"{DIM}(size {info.currsize}/{info.maxsize}){RESET}")

      hdr("12. Labeler lookup — local index, then openFDA")
      _ansi_src = {"local": GREEN, "openfda": YELLOW}


      def show_labeler(ndc: str) -> None:
            lab = get_labeler(ndc)
            if not lab:
                  field(ndc, f"{RED}unresolved{RESET}",
                        f"candidates={list(labeler_code_candidates(ndc))}")
                  return
            color = _ansi_src.get(lab.source, DIM)
            field(ndc, f"{BOLD}{color}{lab.source:<7}{RESET} {cui(lab.code)} {lab.name}")
            if lab.full_name and lab.full_name != lab.name:
                  field("", f"{DIM}{lab.full_name}{RESET}")
            if lab.source == "local":
                  field("", f"{DIM}rx={lab.active_rx_product_count} "
                            f"ndc={lab.active_ndc_product_count} "
                            f"in_rxnorm={lab.in_rxnorm} "
                            f"codes={list(lab.codes)}{RESET}")


      print(f"  {DIM}local index: {len(_load_labelers())} codes "
            f"from {_LABELERS_PATH.name}{RESET}\n")
      for ndc in ("0002-7510-01", "10019-653-64", "00002751001",
                  "99999-999-99", "not-an-ndc"):
            show_labeler(ndc)

      hdr("13. Labeler code candidates — where ambiguity comes from")
      for ndc, note in (
                  ("0002-7510-01", "hyphenated → unambiguous"),
                  ("00002751001", "11 digits → 5-4-2 by convention"),
                  ("1001965364", "10 digits → 4-4-2 or 5-3-2/5-4-1"),
                  ("", "empty → no candidates"),
      ):
            field(ndc or "(empty)",
                  f"{YELLOW}{list(labeler_code_candidates(ndc))}{RESET}", note)

      hdr("14. Labeler rides along in the enrichment record")
      enr = get_rxnorm_enrichment("0002-7510-01")
      field("labeler", f"{GREEN}{enr.labeler}{RESET}" if enr.labeler
      else f"{DIM}None{RESET}")
      field("concept", concept(enr.concept))
      print(f"\n  {DIM}An NDC RxNorm cannot resolve still keeps its labeler:{RESET}")
      orphan = get_rxnorm_enrichment("0002-9999-99")
      field("concept", concept(orphan.concept))
      field("labeler", f"{GREEN}{orphan.labeler}{RESET}" if orphan.labeler
      else f"{DIM}None{RESET}")
      ok(orphan.labeler is not None and orphan.concept is None,
         "labeler resolves independently of RxNorm")

      hdr("15. Labeler Lookup from NDC")
      for ndc in ("65145-0129-01", "65145-129-25"):
            enr = get_rxnorm_enrichment(ndc)
            field(ndc, f"{GREEN}{enr.labeler}{RESET}" if enr.labeler
            else f"{DIM}None{RESET}")
