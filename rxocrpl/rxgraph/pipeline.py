"""Materialize an RxNorm concept and its BN/IN/PIN/SCD/SCDG/SCDC neighborhood
into the existing ``RxNormConcept`` + ``RxNormConceptRelation`` graph tables.

Design notes
------------
* **Two phases.** All network I/O happens first and builds an in-memory graph;
  only then do we open a single ``transaction.atomic()`` to write. RxNav latency
  never holds a DB transaction open.
* **Edges come from the API, not from a hardcoded family adjacency.** For each
  materialized node we probe ``getRelatedByRelationship`` one rela at a time and
  keep a target only if (a) it is in the fetched family and (b) its TTY is an
  allowed target for that (source_tty, rela) rule. That makes the edge set
  tolerant: an over-broad rule can never invent a wrong edge.
* **Schema mapping is centralized.** The two field-name constants below are the
  only things to adjust if your ``RxNormConceptRelation`` uses different FK
  names. Everything else keys off your migration's documented fields
  (``rxcui``, ``tty``, ``name``, ``active``, ``attributes``, ``synced_at``,
  ``rxnorm_release``).
"""

from __future__ import annotations

import dataclasses
import logging
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Optional

from django.apps import apps as django_apps
from django.db import transaction
from django.utils import timezone

from .client import RxNavClient

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Configuration -- adjust these to match your models if needed.
# --------------------------------------------------------------------------- #
CONCEPT_MODEL = "RxNormConcept"  # model_name within the app
RELATION_MODEL = "RxNormConceptRelation"  # model_name within the app
APP_LABEL: Optional[str] = "rxocrpl"  # None -> auto-detect from CONCEPT_MODEL

# RxNormConceptRelation field names (the one place to rename for your schema).
RELATION_SOURCE_FIELD = "source"
RELATION_TARGET_FIELD = "target"
RELATION_RELA_FIELD = "rela"

# Strength field names on the relation model.
RELATION_NUMERATOR_VALUE_FIELD = "numerator_value"
RELATION_NUMERATOR_UNIT_FIELD = "numerator_unit"
RELATION_DENOMINATOR_VALUE_FIELD = "denominator_value"
RELATION_DENOMINATOR_UNIT_FIELD = "denominator_unit"

# The TTYs this pipeline materializes.  SBD (Semantic Branded Drug) is the
# branded counterpart of SCD and is linked to it by has_tradename/tradename_of;
# without it in the family, that SCD↔SBD edge can never be built.
DEFAULT_TTYS: frozenset[str] = frozenset({"BN", "IN", "PIN", "SCD", "SCDG", "SCDC", "SBD"})

# Outbound edge rules: source TTY -> [(rela, {allowed target TTYs}), ...].
# Each clinically meaningful edge among these TTYs is captured exactly once,
# in a canonical forward direction; inverses (constitutes, ingredient_of,
# tradename_of, ...) are derivable and intentionally not duplicated.
#
#   IN  --has_tradename-->        BN     (Tylenol tradename_of acetaminophen)
#   IN  --has_form-->             PIN    (precise ingredient salt form)
#   SCDC--has_ingredient-->       IN
#   SCDC--has_precise_ingredient->PIN
#   SCD --consists_of-->          SCDC
#   SCD --has_ingredient-->       IN
#   SCD --has_tradename-->        SBD    (branded form; SBD tradename_of SCD)
#   SBD --has_ingredient-->       IN
#   SCDG--inverse_isa-->          SCD    (SCDG sits above its dose-form SCDs)
#   SCDG--has_ingredient-->       IN
#   GPCK--contains-->             SCD    (generic kit's member clinical drugs)
#   BPCK--contains-->             SBD    (branded kit's member branded drugs)
#
# BPCK/GPCK are intentionally NOT in DEFAULT_TTYS: widening the default
# family fetch would pull unrelated packs into every ordinary SCD/SBD
# lookup. Callers resolving a kit pass an explicit
# tty_filter=DEFAULT_TTYS | {"BPCK", "GPCK"} instead (see
# enrichment_service.sync_kit_components).
EDGE_RULES: dict[str, list[tuple[str, frozenset[str]]]] = {
      "IN": [("has_tradename", frozenset({"BN"})),
             ("has_form", frozenset({"PIN"}))],
      "PIN": [],  # reached via SCDC.has_precise_ingredient and IN.has_form
      "BN": [],  # reached via IN.has_tradename
      "SCDC": [("has_ingredient", frozenset({"IN"})),
               ("has_precise_ingredient", frozenset({"PIN"}))],
      "SCD": [("consists_of", frozenset({"SCDC"})),
              ("has_ingredient", frozenset({"IN"})),
              ("has_tradename", frozenset({"SBD"}))],
      "SBD": [("has_ingredient", frozenset({"IN"}))],  # SCD↔SBD via SCD.has_tradename
      "SCDG": [("inverse_isa", frozenset({"SCD"})),
               ("has_ingredient", frozenset({"IN"}))],
      "GPCK": [("contains", frozenset({"SCD"}))],
      "BPCK": [("contains", frozenset({"SBD"}))],
}

# RxNav SUPPRESS values that mean "do not treat as an active concept".
# (Y = suppressible, O = obsolete, E = quantified/expanded form.) N / "" = active.
_SUPPRESSED = {"Y", "O", "E"}

# How long a fully-anchored concept stays "fresh" before ``materialize_concept``
# will re-probe RxNav for it.  RxNorm ships monthly, so a 30-day window means at
# most one refresh per release while still skipping the redundant re-fetch that
# used to happen on every run.  Pass ``force=True`` (or ``refresh_after=None``)
# to always re-fetch.
DEFAULT_REFRESH_AFTER = timedelta(days=30)

# ``attributes`` key recording when a concept was last *fully expanded as an
# anchor* (its whole family + edges fetched).  Distinct from ``synced_at``,
# which also advances when a concept is merely created as a neighbor of some
# other anchor and therefore does not imply its own neighborhood was fetched.
_ANCHORED_AT_KEY = "anchored_at"

# TTYs whose nodes are worth expanding when chaining.  Bare ingredients (IN)
# and brand names (BN) fan out to hundreds of unrelated products, so chaining
# through them is intentionally excluded; the drug-level TTYs (clinical SCD and
# branded SBD included) give a bounded, clinically-coherent neighborhood.
CHAIN_TTYS: frozenset[str] = frozenset({"SCD", "SBD", "SCDG", "SCDC", "PIN"})

_UNSET = object()


# --------------------------------------------------------------------------- #
# In-memory graph
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class Node:
      rxcui: str
      name: str
      tty: str
      suppress: str = ""

      @property
      def active(self) -> bool:
            return self.suppress not in _SUPPRESSED


@dataclasses.dataclass
class Edge:
      source: str  # rxcui
      target: str  # rxcui
      rela: str


@dataclasses.dataclass
class ConceptGraph:
      anchor: str
      nodes: dict[str, Node]
      edges: list[Edge]


@dataclasses.dataclass
class MaterializeResult:
      anchor: object  # RxNormConcept instance
      concepts: dict[str, object]  # rxcui -> RxNormConcept
      relations: list[object]  # RxNormConceptRelation instances
      created_concepts: int
      created_relations: int

      def __str__(self) -> str:  # pragma: no cover - convenience only
            return (f"{getattr(self.anchor, 'rxcui', '?')}: "
                    f"{len(self.concepts)} concepts "
                    f"(+{self.created_concepts} new), "
                    f"{len(self.relations)} relations "
                    f"(+{self.created_relations} new)")


# Optional hook: given the *source* Node of a has_ingredient edge whose source
# is an SCDC/SCD, return a dict of relation defaults (e.g. numerator_value /
# numerator_unit) to store the strength on the edge. Return None to skip.
StrengthResolver = Callable[[Node], Optional[dict]]


# --------------------------------------------------------------------------- #
# Model resolution
# --------------------------------------------------------------------------- #
def _resolve_models():
      if APP_LABEL:
            concept = django_apps.get_model(APP_LABEL, CONCEPT_MODEL)
            relation = django_apps.get_model(APP_LABEL, RELATION_MODEL)
            return concept, relation
      # Auto-detect: find the app that defines a model named CONCEPT_MODEL.
      for model in django_apps.get_models():
            if model._meta.model_name == CONCEPT_MODEL:
                  label = model._meta.app_label
                  return model, django_apps.get_model(label, RELATION_MODEL)
      raise LookupError(
            f"Could not locate model '{CONCEPT_MODEL}'. Set APP_LABEL in "
            f"rxgraph/pipeline.py to your app's label."
      )


# --------------------------------------------------------------------------- #
# Fetch phase (network only -- no DB writes)
# --------------------------------------------------------------------------- #
def fetch_family(
          rxcui: str,
          *,
          client: RxNavClient,
          tty_filter: Iterable[str] = DEFAULT_TTYS,
          build_edges: bool = True,
          max_nodes: int = 2000,
) -> ConceptGraph:
      """Build the in-memory concept graph for ``rxcui`` from RxNav.

      Raises ``ValueError`` if the family exceeds ``max_nodes`` (anchoring on a
      bare ingredient can pull in hundreds of products -- for that, a bulk RRF
      load is more appropriate than per-edge REST calls).
      """
      keep = frozenset(tty_filter)

      nodes: dict[str, Node] = {}

      # 1) The anchor itself (allrelated does not return self).
      props = client.properties(rxcui)
      if props:
            nodes[rxcui] = Node(
                  rxcui=props.get("rxcui", rxcui),
                  name=props.get("name", ""),
                  tty=props.get("tty", ""),
                  suppress=props.get("suppress", "") or "",
            )

      # 2) The whole family, filtered to the TTYs we care about.
      for group in client.all_related(rxcui):
            tty = group.get("tty")
            if tty not in keep:
                  continue
            for cp in group.get("conceptProperties") or []:
                  cui = cp.get("rxcui")
                  if not cui:
                        continue
                  nodes[cui] = Node(
                        rxcui=cui,
                        name=cp.get("name", ""),
                        tty=cp.get("tty", tty),
                        suppress=cp.get("suppress", "") or "",
                  )

      if len(nodes) > max_nodes:
            raise ValueError(
                  f"Family for {rxcui} has {len(nodes)} concepts (> max_nodes="
                  f"{max_nodes}). Anchor on a more specific concept or raise the cap."
            )

      edges: list[Edge] = []
      if build_edges:
            edges = _fetch_edges(rxcui, nodes, client)

      return ConceptGraph(anchor=rxcui, nodes=nodes, edges=edges)


def _fetch_edges(anchor: str, nodes: dict[str, Node], client: RxNavClient) -> list[Edge]:
      seen: set[tuple[str, str, str]] = set()
      edges: list[Edge] = []
      for source in nodes.values():
            for rela, allowed in EDGE_RULES.get(source.tty, ()):
                  for cp in client.related_by_rela(source.rxcui, rela):
                        target_cui = cp.get("rxcui")
                        target_tty = cp.get("tty")
                        # Keep only edges that land inside our materialized family and
                        # match the expected target TTY for this rule.
                        if (
                                  target_cui in nodes
                                  and target_tty in allowed
                                  and target_cui != source.rxcui
                        ):
                              key = (source.rxcui, target_cui, rela)
                              if key not in seen:
                                    seen.add(key)
                                    edges.append(Edge(source.rxcui, target_cui, rela))
      return edges


# --------------------------------------------------------------------------- #
# Write phase (single transaction)
# --------------------------------------------------------------------------- #
def materialize_concept(
          rxcui: str,
          *,
          client: Optional[RxNavClient] = None,
          release: str = "",
          tty_filter: Iterable[str] = DEFAULT_TTYS,
          build_edges: bool = True,
          max_nodes: int = 2000,
          strength_resolver: Optional[StrengthResolver] = None,
          refresh_after: Optional[timedelta] = _UNSET,  # type: ignore[assignment]
          force: bool = False,
) -> MaterializeResult:
      """Fetch and persist ``rxcui``'s concept neighborhood. Idempotent.

      Returns a :class:`MaterializeResult`. Existing rows are updated in place
      (``update_or_create`` on ``rxcui``); existing edges are left untouched.

      Skipping already-done work
      --------------------------
      When ``rxcui`` was already fully expanded as an anchor within
      ``refresh_after`` (default :data:`DEFAULT_REFRESH_AFTER`), the RxNav fetch
      is skipped entirely and the neighborhood is served from the DB.  This is
      the persistent, cross-process counterpart to the in-process ``lru_cache``
      on the low-level client.  Pass ``force=True`` to always re-fetch, or
      ``refresh_after=None`` to disable the freshness window for this call.
      """
      if refresh_after is _UNSET:
            refresh_after = DEFAULT_REFRESH_AFTER

      client = client or RxNavClient()
      concept_model, relation_model = _resolve_models()

      # ---- skip phase (DB only) ----
      # Serve a still-fresh, already-anchored concept from the DB without a
      # single RxNav request.
      if not force and refresh_after is not None:
            existing = concept_model.objects.filter(rxcui=rxcui).first()
            if existing is not None and _anchor_is_fresh(existing, refresh_after):
                  concepts, relations = _load_neighborhood(
                        concept_model, relation_model, existing
                  )
                  return MaterializeResult(
                        anchor=existing,
                        concepts=concepts,
                        relations=relations,
                        created_concepts=0,
                        created_relations=0,
                  )

      # ---- network phase (no DB) ----
      graph = fetch_family(
            rxcui,
            client=client,
            tty_filter=tty_filter,
            build_edges=build_edges,
            max_nodes=max_nodes,
      )

      # ---- write phase (one transaction) ----
      now = timezone.now()
      with transaction.atomic():
            concepts, created_c = _upsert_nodes(concept_model, graph.nodes, now)
            relations, created_r = _upsert_edges(
                  relation_model, concepts, graph.nodes, graph.edges, strength_resolver
            )
            _mark_anchored(concepts.get(rxcui) or concepts.get(graph.anchor), now)

      anchor_obj = concepts.get(rxcui) or concepts.get(graph.anchor)
      return MaterializeResult(
            anchor=anchor_obj,
            concepts=concepts,
            relations=relations,
            created_concepts=created_c,
            created_relations=created_r,
      )


def _anchor_is_fresh(concept, refresh_after: timedelta) -> bool:
      """True if ``concept`` was fully anchored within ``refresh_after``.

      Reads the ``anchored_at`` marker stamped by :func:`_mark_anchored`.  A
      concept that only exists as a neighbor of some other anchor has no marker
      and is therefore never considered fresh, so chaining into it still fetches
      its own family.
      """
      attrs = getattr(concept, "attributes", None) or {}
      stamp = attrs.get(_ANCHORED_AT_KEY)
      if not stamp:
            return False
      try:
            anchored_at = datetime.fromisoformat(stamp)
      except (TypeError, ValueError):
            return False
      return (timezone.now() - anchored_at) < refresh_after


def _mark_anchored(anchor, now: datetime) -> None:
      """Stamp ``anchored_at`` on the anchor so later runs can skip it."""
      if anchor is None:
            return
      attrs = dict(getattr(anchor, "attributes", {}) or {})
      attrs[_ANCHORED_AT_KEY] = now.isoformat()
      anchor.attributes = attrs
      anchor.save(update_fields=["attributes"])


def _load_neighborhood(concept_model, relation_model, anchor):
      """Load ``anchor``'s 1-hop neighborhood from the DB (no network).

      Returns ``(concepts, relations)`` shaped like the write phase's output so
      the freshness-skip path and the chaining driver see a uniform result.
      """
      relations = list(
            relation_model.objects.filter(
                  **{f"{RELATION_SOURCE_FIELD}__rxcui": anchor.rxcui}
            ).select_related(RELATION_SOURCE_FIELD, RELATION_TARGET_FIELD)
      )
      concepts: dict[str, object] = {anchor.rxcui: anchor}
      for rel in relations:
            target = getattr(rel, RELATION_TARGET_FIELD)
            concepts[target.rxcui] = target
      return concepts, relations


def _upsert_nodes(concept_model, nodes, now):
      out: dict[str, object] = {}
      created = 0
      for node in nodes.values():
            defaults = {
                  "tty": node.tty or "",
                  "name": node.name or "",
                  "active": node.active,
                  "synced_at": now,
            }
            obj, was_created = concept_model.objects.update_or_create(
                  rxcui=node.rxcui, defaults=defaults
            )
            # Stash the raw SUPPRESS flag without clobbering other attributes.
            attrs = dict(getattr(obj, "attributes", {}) or {})
            if attrs.get("suppress") != node.suppress:
                  attrs["suppress"] = node.suppress
                  obj.attributes = attrs
                  obj.save(update_fields=["attributes"])
            out[node.rxcui] = obj
            created += int(was_created)
      return out, created


def _upsert_edges(relation_model, concepts, nodes, edges, strength_resolver):
      out: list[object] = []
      created = 0
      for edge in edges:
            source = concepts.get(edge.source)
            target = concepts.get(edge.target)
            if source is None or target is None:
                  continue
            lookup = {
                  RELATION_SOURCE_FIELD: source,
                  RELATION_TARGET_FIELD: target,
                  RELATION_RELA_FIELD: edge.rela,
            }
            defaults: dict = {}
            if strength_resolver is not None and edge.rela == "has_ingredient":
                  src_node = nodes.get(edge.source)
                  if src_node is not None and src_node.tty in ("SCDC", "SCD"):
                        resolved = strength_resolver(src_node)
                        if resolved:
                              # Map resolved strength keys to the model field names.
                              # resolved is expected to have keys like 'numerator_value', etc.
                              # but we use the constants for safety.
                              defaults[RELATION_NUMERATOR_VALUE_FIELD] = resolved.get("numerator_value")
                              defaults[RELATION_NUMERATOR_UNIT_FIELD] = resolved.get("numerator_unit")
                              defaults[RELATION_DENOMINATOR_VALUE_FIELD] = resolved.get("denominator_value")
                              defaults[RELATION_DENOMINATOR_UNIT_FIELD] = resolved.get("denominator_unit")

            obj, was_created = relation_model.objects.update_or_create(
                  defaults=defaults, **lookup
            )
            out.append(obj)
            created += int(was_created)
      return out, created


def materialize_concept_graph(
          rxcui: str,
          *,
          client: Optional[RxNavClient] = None,
          max_depth: int = 1,
          max_total_nodes: int = 500,
          chain_ttys: Iterable[str] = CHAIN_TTYS,
          visited: Optional[set[str]] = None,
          **kwargs: Any,
) -> dict[str, MaterializeResult]:
      """Chain-materialize ``rxcui`` and the concepts connected to it.

      Breadth-first from ``rxcui``: each anchored concept exposes its neighbors
      (restricted to ``chain_ttys``), which are themselves anchored, out to
      ``max_depth`` hops.  Every :func:`materialize_concept` call flows through
      the freshness skip, so a neighbor already anchored recently costs no
      network I/O.

      Loop safety
      -----------
      The RxNorm graph is cyclic (SCD ↔ SCDC ↔ IN …), so the walk is bounded by
      three independent guards, any one of which terminates it:

      * ``visited`` — a concept is anchored at most once per call; re-encounters
        are dropped, which is what actually breaks cycles.
      * ``max_depth`` — hop budget from the origin.
      * ``max_total_nodes`` — hard ceiling on concepts anchored, so a
        pathological family cannot fan out without bound.

      Returns ``{rxcui: MaterializeResult}`` for every concept anchored.
      """
      client = client or RxNavClient()
      keep = frozenset(chain_ttys)
      visited = visited if visited is not None else set()
      results: dict[str, MaterializeResult] = {}

      queue: deque[tuple[str, int]] = deque([(rxcui, 0)])
      while queue:
            current, depth = queue.popleft()
            if current in visited:
                  continue
            if len(visited) >= max_total_nodes:
                  log.info(
                        "materialize_concept_graph: node budget %d reached at %s",
                        max_total_nodes, rxcui,
                  )
                  break
            visited.add(current)

            try:
                  result = materialize_concept(current, client=client, **kwargs)
            except Exception:  # noqa: BLE001 - one bad node must not sink the walk
                  log.exception("Chain materialize failed for rxcui=%s", current)
                  continue

            results[current] = result
            if result.anchor is None or depth >= max_depth:
                  continue

            # Enqueue connected concepts we have not already anchored.
            for neighbor_cui, neighbor in result.concepts.items():
                  if neighbor_cui in visited:
                        continue
                  if getattr(neighbor, "tty", None) in keep:
                        queue.append((neighbor_cui, depth + 1))

      return results


def add_concept(rxcui: str, *, chain: bool = False, **kwargs) -> object:
      """Materialize ``rxcui`` and return the anchor RxNormConcept instance.

      With ``chain=True`` the connected concepts are materialized too (see
      :func:`materialize_concept_graph`); chaining kwargs such as ``max_depth``
      and ``max_total_nodes`` are forwarded.
      """
      if chain:
            chain_keys = ("max_depth", "max_total_nodes", "chain_ttys", "visited")
            chain_kwargs = {k: kwargs.pop(k) for k in chain_keys if k in kwargs}
            results = materialize_concept_graph(rxcui, **chain_kwargs, **kwargs)
            top = results.get(rxcui)
            return top.anchor if top else None
      return materialize_concept(rxcui, **kwargs).anchor


def add_concept_by_name(name: str, **kwargs) -> object:
      """Resolve ``name`` to an RXCUI and materialize its neighborhood.

      Returns the anchor RxNormConcept or ``None`` if the name cannot be resolved.
      """
      client = kwargs.get("client") or RxNavClient()
      rxcui = client.find_rxcui_by_name(name)
      if not rxcui:
            return None
      kwargs["client"] = client
      return add_concept(rxcui, **kwargs)


def add_concept_by_ndc(ndc: str, **kwargs: Any) -> Optional[object]:
      """Resolve ``ndc`` to an RXCUI via ProductRxNormMapping or rxnorm.enrichment and materialize its graph.

      Returns the anchor RxNormConcept or ``None`` if the NDC cannot be resolved.
      """
      concept_model, _ = _resolve_models()
      from rxocrpl.models import ProductRxNormMapping
      mapping = ProductRxNormMapping.objects.filter(product_ndc=ndc).first()
      if mapping:
            return add_concept(mapping.rxcui, **kwargs)

      from rxocrpl.rxnorm.enrichment import get_rxnorm_enrichment
      enrichment = get_rxnorm_enrichment(ndc)
      rxcui = enrichment.concept_rxcui
      if not rxcui:
            return None
      return add_concept(rxcui, **kwargs)
