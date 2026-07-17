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
from typing import Any, Callable, Iterable, Optional

from django.apps import apps as django_apps
from django.db import transaction
from django.utils import timezone

from .client import RxNavClient

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

# The TTYs this pipeline materializes, exactly as requested.
DEFAULT_TTYS: frozenset[str] = frozenset({"BN", "IN", "PIN", "SCD", "SCDG", "SCDC"})

# Outbound edge rules: source TTY -> [(rela, {allowed target TTYs}), ...].
# Each clinically meaningful edge among the six TTYs is captured exactly once,
# in a canonical forward direction; inverses (constitutes, ingredient_of, ...)
# are derivable and intentionally not duplicated.
#
#   IN  --has_tradename-->        BN     (Tylenol tradename_of acetaminophen)
#   IN  --has_form-->             PIN    (precise ingredient salt form)
#   SCDC--has_ingredient-->       IN
#   SCDC--has_precise_ingredient->PIN
#   SCD --consists_of-->          SCDC
#   SCD --has_ingredient-->       IN
#   SCDG--inverse_isa-->          SCD    (SCDG sits above its dose-form SCDs)
#   SCDG--has_ingredient-->       IN
EDGE_RULES: dict[str, list[tuple[str, frozenset[str]]]] = {
      "IN": [("has_tradename", frozenset({"BN"})),
             ("has_form", frozenset({"PIN"}))],
      "PIN": [],  # reached via SCDC.has_precise_ingredient and IN.has_form
      "BN": [],  # reached via IN.has_tradename
      "SCDC": [("has_ingredient", frozenset({"IN"})),
               ("has_precise_ingredient", frozenset({"PIN"}))],
      "SCD": [("consists_of", frozenset({"SCDC"})),
              ("has_ingredient", frozenset({"IN"}))],
      "SCDG": [("inverse_isa", frozenset({"SCD"})),
               ("has_ingredient", frozenset({"IN"}))],
}

# RxNav SUPPRESS values that mean "do not treat as an active concept".
# (Y = suppressible, O = obsolete, E = quantified/expanded form.) N / "" = active.
_SUPPRESSED = {"Y", "O", "E"}


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
) -> MaterializeResult:
      """Fetch and persist ``rxcui``'s concept neighborhood. Idempotent.

      Returns a :class:`MaterializeResult`. Existing rows are updated in place
      (``update_or_create`` on ``rxcui``); existing edges are left untouched.
      """

      client = client or RxNavClient()
      concept_model, relation_model = _resolve_models()

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
            print(concepts, created_c, relations, created_r)

      anchor_obj = concepts.get(rxcui) or concepts.get(graph.anchor)
      return MaterializeResult(
            anchor=anchor_obj,
            concepts=concepts,
            relations=relations,
            created_concepts=created_c,
            created_relations=created_r,
      )


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


def add_concept(rxcui: str, **kwargs) -> object:
      """Materialize ``rxcui`` and return the anchor RxNormConcept instance."""
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
