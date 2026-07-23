"""Static (server-rendered) layout builder for the RxNorm concept hierarchy.

Unlike :mod:`rxocrpl.views.concept_graph_view` (which ships an interactive
Cytoscape graph to the browser), this module computes a fully positioned,
click-through diagram on the server so the admin can render it as plain SVG
with ``<a>`` links -- no JavaScript, no client-side graph engine.

The layout is a centred *hub* (the anchor concept) with relationship
*branches* fanning out to either side:

    inbound relations  <--  [ ANCHOR ]  -->  outbound relations
    (what points here)                        (what this consists of)

Connected NDC products (via :class:`ProductRxNormMapping`) hang off the hub as
their own branch. All labels are drawn horizontally; only the thin connector
lines curve, which keeps the radial-infographic feel without sacrificing
legibility.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from math import ceil
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from django.urls import reverse

if TYPE_CHECKING:
      from rxocrpl.models import RxNormConcept

# --- palette (kept in sync with the TTY colours used in concept_graph.html) ---
TTY_COLORS = {
      "IN": "#059669",
      "PIN": "#10b981",
      "SCDC": "#c026d3",
      "SBDC": "#86198f",
      "SCD": "#3a7bd5",
      "SBD": "#1e40af",
      "BN": "#ea580c",
      "SCDG": "#d97706",
      "SBDG": "#92400e",
}
DEFAULT_COLOR = "#64748b"
PRODUCT_COLOR = "#0f766e"

TTY_LABELS = {
      "IN": "Ingredient",
      "PIN": "Precise Ingredient",
      "SCDC": "Clinical Drug Component",
      "SBDC": "Branded Drug Component",
      "SCD": "Clinical Drug",
      "SBD": "Branded Drug",
      "BN": "Branded Name",
      "SCDG": "Clinical Dose Form Group",
      "SBDG": "Branded Dose Form Group",
}

# Rank is used only to order branches so the diagram reads roughly
# ingredient -> component -> drug -> group from the inside out.
TTY_RANK = {
      "IN": 0, "PIN": 0,
      "SCDC": 1, "SBDC": 1,
      "SCD": 2, "SBD": 2,
      "SCDG": 3, "SBDG": 3,
      "BN": 4,
}

# --- geometry (all values in SVG user units / px) ---
CARD_W = 224
CARD_H = 42
CARD_VGAP = 12          # vertical gap between cards within a branch
GROUP_HEADER_H = 26     # height reserved for a branch's rela heading
GROUP_GAP = 30          # vertical gap between branches on the same side
HUB_W = 260
HUB_H = 74
BRANCH_GAP_X = 96       # horizontal gap between the hub edge and its cards
MARGIN_X = 44
MARGIN_TOP = 96         # room for the title / legend band
MARGIN_BOTTOM = 44
MAX_PER_BRANCH = 9      # cap cards per branch; the rest collapse to a "more" link


def _color_for(tty: str | None) -> str:
      return TTY_COLORS.get(tty or "", DEFAULT_COLOR)


def _short(name: str | None, limit: int = 46) -> str:
      if not name:
            return ""
      name = name.strip()
      return name if len(name) <= limit else name[: limit - 1].rstrip() + "…"


@dataclass
class Card:
      """A positioned, clickable box in the diagram."""

      label: str
      sub: str
      color: str
      url: str
      x: float = 0.0
      y: float = 0.0
      w: float = CARD_W
      h: float = CARD_H
      accent: str = ""          # optional right-aligned badge (e.g. TTY)
      is_more: bool = False

      @property
      def cx(self) -> float:
            return self.x + self.w / 2

      @property
      def cy(self) -> float:
            return self.y + self.h / 2

      # --- text anchor helpers (keep the template arithmetic-free) ---
      @property
      def text_x(self) -> float:
            return self.x + 18

      @property
      def label_y(self) -> float:
            return self.y + 18

      @property
      def sub_y(self) -> float:
            return self.y + 33

      @property
      def badge_x(self) -> float:
            return self.x + self.w - 12

      @property
      def badge_y(self) -> float:
            return self.y + self.h / 2 + 4


@dataclass
class Branch:
      """A relationship group rendered as a labelled column of cards."""

      heading: str
      rank: int
      side: str                 # "left" or "right"
      cards: list[Card] = field(default_factory=list)
      hx: float = 0.0           # heading anchor x
      hy: float = 0.0           # heading anchor y

      def height(self) -> float:
            n = len(self.cards)
            return GROUP_HEADER_H + n * CARD_H + max(0, n - 1) * CARD_VGAP


@dataclass
class Connector:
      """A cubic-bezier link drawn from the hub to a card (or between cards)."""

      path: str
      color: str
      to_hub: bool              # True => arrow points at the hub (inbound)


def _concept_url(rxcui: str) -> str:
      return reverse("admin:rxocrpl_rxnormconcept_hierarchy", args=[rxcui])


def _product_url(ndc: str) -> str:
      return reverse("admin:rxocrpl_product_change", args=[ndc])


# --- per-branch pagination (keeps overflow on this page instead of the ------
#     interactive graph; each branch pages independently via ?pg_<key>=N) ---
def _param_for(key: str) -> str:
      """URL-safe query-param name uniquely identifying a branch."""
      return "pg_" + re.sub(r"[^0-9a-zA-Z]+", "_", key)


def _clamp_page(query: dict[str, str] | None, key: str, page_count: int) -> int:
      raw = (query or {}).get(_param_for(key), "0")
      try:
            page = int(raw)
      except (TypeError, ValueError):
            page = 0
      return max(0, min(page, page_count - 1))


def _paged_url(
          base: str, query: dict[str, str] | None, key: str, page: int
) -> str:
      """URL for the hierarchy page with just ``key``'s page changed.

      Every *other* branch's page is preserved, so paging one group does not
      reset the others.
      """
      param = _param_for(key)
      params = {
            k: v
            for k, v in (query or {}).items()
            if k.startswith("pg_") and k != param
      }
      if page > 0:
            params[param] = str(page)
      qs = urlencode(sorted(params.items()))
      return f"{base}?{qs}" if qs else base


def _pager(
          *,
          key: str,
          page: int,
          page_count: int,
          remaining: int,
          noun: str,
          base: str,
          query: dict[str, str] | None,
) -> tuple["Card | None", "Card | None"]:
      """Build the previous / next navigation cards for a paginated branch."""
      prev_card = next_card = None
      if page > 0:
            prev_card = Card(
                  label=f"← previous {noun}",
                  sub=f"page {page} of {page_count}",
                  color=DEFAULT_COLOR,
                  url=_paged_url(base, query, key, page - 1),
                  is_more=True,
            )
      if page < page_count - 1:
            next_card = Card(
                  label=f"+{remaining} more {noun}",
                  sub=f"page {page + 2} of {page_count}",
                  color=DEFAULT_COLOR,
                  url=_paged_url(base, query, key, page + 1),
                  is_more=True,
            )
      return prev_card, next_card


def _collect_branches(
          concept: "RxNormConcept", query: dict[str, str] | None = None
) -> tuple[list[Branch], set[str]]:
      """Group the anchor's direct relations into per-``rela`` branches.

      Returns the branches plus the set of every rxcui appearing in the ego
      graph (used to find connected products). ``query`` carries the per-branch
      page state (``pg_<key>`` params) so overflow can be paged in place.
      """
      from rxocrpl.models import RxNormConceptRelation

      outbound = list(
            RxNormConceptRelation.objects.filter(source=concept).select_related("target")
      )
      inbound = list(
            RxNormConceptRelation.objects.filter(target=concept).select_related("source")
      )

      ego_rxcuis: set[str] = {concept.rxcui}
      base = _concept_url(concept.rxcui)

      def build(relations, *, direction: str) -> list[Branch]:
            # rela -> {rxcui -> concept}
            grouped: dict[str, dict[str, "RxNormConcept"]] = {}
            for rel in relations:
                  neighbor = rel.target if direction == "out" else rel.source
                  ego_rxcuis.add(neighbor.rxcui)
                  grouped.setdefault(rel.rela, {})[neighbor.rxcui] = neighbor

            branches: list[Branch] = []
            side = "right" if direction == "out" else "left"
            arrow = "→" if direction == "out" else "←"
            for rela, members in grouped.items():
                  concepts = sorted(
                        members.values(),
                        key=lambda c: (TTY_RANK.get(c.tty or "", 2), (c.name or "").lower()),
                  )
                  rank = min((TTY_RANK.get(c.tty or "", 2) for c in concepts), default=2)
                  noun = rela.replace("_", " ")
                  key = f"{direction}:{rela}"
                  page_count = max(1, ceil(len(concepts) / MAX_PER_BRANCH))
                  page = _clamp_page(query, key, page_count)
                  start = page * MAX_PER_BRANCH
                  window = concepts[start : start + MAX_PER_BRANCH]

                  base_heading = (
                        f"{arrow} {noun}" if direction == "out" else f"{noun} {arrow}"
                  )
                  heading = (
                        f"{base_heading} · {page + 1}/{page_count}"
                        if page_count > 1
                        else base_heading
                  )

                  prev_card, next_card = _pager(
                        key=key,
                        page=page,
                        page_count=page_count,
                        remaining=len(concepts) - (start + len(window)),
                        noun=noun,
                        base=base,
                        query=query,
                  )
                  cards: list[Card] = []
                  if prev_card:
                        cards.append(prev_card)
                  for c in window:
                        cards.append(
                              Card(
                                    label=_short(c.name),
                                    sub=f"{c.rxcui}",
                                    color=_color_for(c.tty),
                                    url=_concept_url(c.rxcui),
                                    accent=c.tty or "",
                              )
                        )
                  if next_card:
                        cards.append(next_card)
                  branches.append(Branch(heading=heading, rank=rank, side=side, cards=cards))
            return branches

      left = build(inbound, direction="in")
      right = build(outbound, direction="out")
      # Read inside-out: lowest TTY rank nearest the top of each column.
      left.sort(key=lambda b: (b.rank, b.heading))
      right.sort(key=lambda b: (b.rank, b.heading))
      return left + right, ego_rxcuis


def _collect_product_branch(
          concept: "RxNormConcept",
          ego_rxcuis: set[str],
          query: dict[str, str] | None = None,
) -> tuple[Branch | None, dict[str, str], int]:
      """Build the "NDC products" branch from confirmed RxCUI->NDC mappings.

      Returns the branch, a mapping of product-NDC -> the rxcui it is tied to
      (so the caller can route each product's connector to the right concept
      card), and the true total number of distinct connected products. Overflow
      is paged in place via the ``pg_products`` query param.
      """
      from rxocrpl.models import Product, ProductRxNormMapping

      mappings = list(
            ProductRxNormMapping.objects.filter(rxcui__in=ego_rxcuis).order_by(
                  "rxcui", "product_ndc"
            )
      )
      if not mappings:
            return None, {}, 0

      products = Product.objects.in_bulk(
            [m.product_ndc for m in mappings], field_name="product_ndc"
      )

      # De-dupe to one entry per NDC, preserving the (rxcui, ndc) ordering.
      ndc_to_rxcui: dict[str, str] = {}
      items: list[tuple[str, str, str]] = []  # (ndc, label, tie)
      seen: set[str] = set()
      for m in mappings:
            if m.product_ndc in seen:
                  continue
            seen.add(m.product_ndc)
            ndc_to_rxcui[m.product_ndc] = m.rxcui
            product = products.get(m.product_ndc)
            label = (
                  (product.brand_name or product.generic_name)
                  if product
                  else (m.name or m.product_ndc)
            )
            tie = "anchor" if m.rxcui == concept.rxcui else m.rxcui
            items.append((m.product_ndc, label, tie))

      total_products = len(items)
      key = "products"
      page_count = max(1, ceil(total_products / MAX_PER_BRANCH))
      page = _clamp_page(query, key, page_count)
      start = page * MAX_PER_BRANCH
      window = items[start : start + MAX_PER_BRANCH]

      prev_card, next_card = _pager(
            key=key,
            page=page,
            page_count=page_count,
            remaining=total_products - (start + len(window)),
            noun="products",
            base=_concept_url(concept.rxcui),
            query=query,
      )
      cards: list[Card] = []
      if prev_card:
            cards.append(prev_card)
      for ndc, label, tie in window:
            cards.append(
                  Card(
                        label=_short(label, 40),
                        sub=f"{ndc}  ·  {tie}",
                        color=PRODUCT_COLOR,
                        url=_product_url(ndc),
                        accent="NDC",
                  )
            )
      if next_card:
            cards.append(next_card)

      base_heading = "→ NDC products"
      heading = (
            f"{base_heading} · {page + 1}/{page_count}"
            if page_count > 1
            else base_heading
      )
      branch = Branch(heading=heading, rank=5, side="right", cards=cards)
      return branch, ndc_to_rxcui, total_products


def _bezier(sx: float, sy: float, ex: float, ey: float) -> str:
      """Horizontal-ish cubic bezier between two points."""
      dx = max(40.0, abs(ex - sx) * 0.5)
      c1x = sx + (dx if ex >= sx else -dx)
      c2x = ex + (-dx if ex >= sx else dx)
      return f"M {sx:.1f} {sy:.1f} C {c1x:.1f} {sy:.1f} {c2x:.1f} {ey:.1f} {ex:.1f} {ey:.1f}"


def build_hierarchy(
          concept: "RxNormConcept", query: dict[str, str] | None = None
) -> dict:
      """Compute the full positioned diagram for ``concept``.

      Returns a context dict ready to hand to the template: SVG dimensions, the
      hub, positioned cards, connectors, a legend and summary counts. ``query``
      (typically ``request.GET``) supplies each branch's ``pg_<key>`` page so
      overflow nodes are paged in place rather than punting to the interactive
      graph.
      """
      branches, ego_rxcuis = _collect_branches(concept, query)
      product_branch, ndc_to_rxcui, total_products = _collect_product_branch(
            concept, ego_rxcuis, query
      )
      if product_branch is not None:
            branches.append(product_branch)

      left = [b for b in branches if b.side == "left"]
      right = [b for b in branches if b.side == "right"]

      def side_height(side: list[Branch]) -> float:
            if not side:
                  return 0.0
            return sum(b.height() for b in side) + (len(side) - 1) * GROUP_GAP

      content_h = max(side_height(left), side_height(right), HUB_H)
      total_h = MARGIN_TOP + content_h + MARGIN_BOTTOM
      center_y = MARGIN_TOP + content_h / 2

      # Horizontal extents: hub in the middle, cards BRANCH_GAP_X beyond each edge.
      left_extent = HUB_W / 2 + BRANCH_GAP_X + CARD_W if left else HUB_W / 2
      right_extent = HUB_W / 2 + BRANCH_GAP_X + CARD_W if right else HUB_W / 2
      center_x = MARGIN_X + left_extent
      total_w = center_x + right_extent + MARGIN_X

      hub = {
            "x": center_x - HUB_W / 2,
            "y": center_y - HUB_H / 2,
            "w": HUB_W,
            "h": HUB_H,
            "cx": center_x,
            "cy": center_y,
            "color": _color_for(concept.tty),
            "name": _short(concept.name, 52),
            "rxcui": concept.rxcui,
            "tty": concept.tty or "",
            "tty_label": TTY_LABELS.get(concept.tty or "", "Concept"),
            "name_y": center_y - 6,
            "meta_y": center_y + 15,
      }

      connectors: list[Connector] = []
      rxcui_card_pos: dict[str, tuple[float, float]] = {}

      def place_side(side: list[Branch], is_left: bool) -> None:
            start_y = center_y - side_height(side) / 2
            y = start_y
            for branch in side:
                  branch.hy = y + GROUP_HEADER_H - 8
                  if is_left:
                        card_x = center_x - HUB_W / 2 - BRANCH_GAP_X - CARD_W
                        branch.hx = card_x + CARD_W  # right-align heading
                        hub_edge_x = center_x - HUB_W / 2
                  else:
                        card_x = center_x + HUB_W / 2 + BRANCH_GAP_X
                        branch.hx = card_x
                        hub_edge_x = center_x + HUB_W / 2

                  cy = y + GROUP_HEADER_H
                  for card in branch.cards:
                        card.x = card_x
                        card.y = cy
                        inner_x = card.x + card.w if is_left else card.x
                        path = _bezier(hub_edge_x, center_y, inner_x, card.cy)
                        connectors.append(
                              Connector(path=path, color=card.color, to_hub=is_left)
                        )
                        cy += CARD_H + CARD_VGAP
                  y += branch.height() + GROUP_GAP

      place_side(left, is_left=True)
      place_side(right, is_left=False)

      # Record concept-card positions so product connectors can target them.
      for branch in left + right:
            for card in branch.cards:
                  if not card.is_more and card.accent != "NDC":
                        rxcui_card_pos[card.sub] = (card.x, card.cy)

      # Re-route product connectors to the concept they map to (when shown).
      if product_branch is not None:
            for card in product_branch.cards:
                  if card.is_more:
                        continue
                  ndc = card.sub.split(" ")[0]
                  target_rxcui = ndc_to_rxcui.get(ndc)
                  pos = rxcui_card_pos.get(target_rxcui or "")
                  if pos:
                        # link product -> its concept card instead of the hub
                        tx, ty = pos
                        connectors.append(
                              Connector(
                                    path=_bezier(card.x, card.cy, tx, ty),
                                    color=PRODUCT_COLOR,
                                    to_hub=False,
                              )
                        )

      legend_ttys = sorted(
            {t for b in branches for c in b.cards for t in [c.accent] if t and t != "NDC"}
            | ({concept.tty} if concept.tty else set())
      )
      legend = [
            {"tty": t, "color": _color_for(t), "label": TTY_LABELS.get(t, t)}
            for t in legend_ttys
      ]

      # True totals (before per-branch capping) so the header isn't misleading.
      total_concepts = len(ego_rxcuis) - 1  # exclude the anchor itself
      shown_concepts = sum(
            len([c for c in b.cards if not c.is_more])
            for b in branches
            if b is not product_branch
      )
      shown_products = (
            len([c for c in product_branch.cards if not c.is_more])
            if product_branch
            else 0
      )
      right_relation_branches = len([b for b in right if b is not product_branch])

      return {
            "concept": concept,
            "svg_w": round(total_w),
            "svg_h": round(total_h),
            "hub": hub,
            "branches": branches,
            "connectors": connectors,
            "legend": legend,
            "summary": {
                  "branch_count": len(branches),
                  "concept_count": total_concepts,
                  "shown_concepts": shown_concepts,
                  "concepts_truncated": shown_concepts < total_concepts,
                  "product_count": total_products,
                  "shown_products": shown_products,
                  "inbound_count": len(left),
                  "outbound_count": right_relation_branches,
            },
      }
