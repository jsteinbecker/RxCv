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

import json
import re
from dataclasses import dataclass, field
from textwrap import wrap
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from django.db.models import Q
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
      "GPCK": "#7c3aed",
      "BPCK": "#5b21b6",
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
      "GPCK": "Generic Pack",
      "BPCK": "Brand Name Pack",
}

# Rank is used only to order branches so the diagram reads roughly
# ingredient -> component -> drug -> group from the inside out.
TTY_RANK = {
      "IN": 0, "PIN": 0,
      "SCDC": 1, "SBDC": 1,
      "SCD": 2, "SBD": 2,
      "SCDG": 3, "SBDG": 3,
      "BN": 4,
      "GPCK": 5, "BPCK": 5,
}

# --- geometry (all values in SVG user units / px) ---
CARD_W = 224
CARD_H = 58
CARD_VGAP = 12          # vertical gap between cards within a branch
GROUP_HEADER_H = 26     # height reserved for a branch's rela heading
GROUP_GAP = 30          # vertical gap between branches on the same side
HUB_W = 260
HUB_H = 74
BRANCH_GAP_X = 96       # horizontal gap between the hub edge and its cards
BRANCH_COLUMN_GAP_X = 52
MARGIN_X = 44
MARGIN_TOP = 96         # room for the title / legend band
MARGIN_BOTTOM = 44
MAX_PER_BRANCH = 9      # cap cards per branch; the rest collapse to a "more" link
MAX_HOPS = 2            # hops walked out from the hub on each side (hub + 2 + 2 = 5 columns wide)
PRIMITIVE_W = 172       # compact size for dose-form-group (SCDG/SBDG) "primitive" cards
PRIMITIVE_H = 40        # minimum; grows to fit a fully-wrapped, untruncated title
TERMINAL_W = 196        # compact size for NDC product "terminal" (leaf) cards
TERMINAL_H = 54         # minimum; grows to fit name + dosage form + labeler/NDC
COMPACT_TOP_PAD = 15    # first label line's baseline, from the card's top edge
COMPACT_LINE_H = 13     # vertical step between wrapped label lines
COMPACT_ROW_GAP = 14    # vertical step from one text row to the next
COMPACT_BOTTOM_PAD = 9  # padding below the last text row


def _color_for(tty: str | None) -> str:
      return TTY_COLORS.get(tty or "", DEFAULT_COLOR)


def _short(name: str | None, limit: int = 46) -> str:
      if not name:
            return ""
      name = name.strip()
      return name if len(name) <= limit else name[: limit - 1].rstrip() + "…"


def _wrap_text(value: str | None, width: int, max_lines: int | None) -> list[str]:
      """Word-wrap ``value``. With ``max_lines=None`` every line is kept (no
      truncation); otherwise lines beyond ``max_lines`` collapse into an
      ellipsis on the last kept line."""
      if not value:
            return [""]
      lines = wrap(value.strip(), width=width, break_long_words=False, break_on_hyphens=False)
      if not lines:
            return [""]
      if max_lines is None or len(lines) <= max_lines:
            return lines
      kept = lines[:max_lines]
      kept[-1] = _short(kept[-1], width)
      return kept


@dataclass(eq=False)
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
      column: int = 1           # hop distance from the hub; each hop is its own column
      hop: int = 1
      parent_rxcuis: list[str] = field(default_factory=list)  # cards this was reached through
      sub2: str = ""            # second identifying line (terminal/product cards only)
      ndc: str = ""             # product NDC, kept separate from `sub` for connector routing

      @property
      def cx(self) -> float:
            return self.x + self.w / 2

      @property
      def cy(self) -> float:
            return self.y + self.h / 2

      # --- appearance variants ---
      @property
      def is_primitive(self) -> bool:
            """Dose-form-group (SCDG/SBDG) cards get a compact "primitive" look."""
            return self.accent in ("SCDG", "SBDG")

      @property
      def is_terminal(self) -> bool:
            """NDC product cards get a compact "terminal" (leaf) look."""
            return self.accent == "NDC"

      @property
      def is_compact(self) -> bool:
            return self.is_primitive or self.is_terminal

      @property
      def node_id(self) -> str:
            """Stable id used to wire up hover highlighting; "" for cards that
            aren't a real graph node (e.g. pagination "more" cards)."""
            if self.is_more:
                  return ""
            if self.is_terminal:
                  return f"ndc:{self.ndc}"
            return f"rx:{self.sub}"

      # --- text anchor helpers (keep the template arithmetic-free) ---
      @property
      def text_x(self) -> float:
            return self.x + (12 if self.is_compact else 18)

      @property
      def label_y(self) -> float:
            if self.is_compact:
                  return self.y + COMPACT_TOP_PAD
            return self.y + 17

      @property
      def sub_y(self) -> float:
            if self.is_compact:
                  n = len(self.label_lines)
                  return self.y + COMPACT_TOP_PAD + (n - 1) * COMPACT_LINE_H + COMPACT_ROW_GAP
            return self.y + 43

      @property
      def sub2_y(self) -> float:
            return self.sub_y + COMPACT_ROW_GAP

      @property
      def badge_x(self) -> float:
            return self.x + self.w - 12

      @property
      def badge_y(self) -> float:
            return self.y + (11 if self.is_compact else 15)

      @property
      def label_lines(self) -> list[str]:
            # Compact cards wrap the full title across as many lines as it
            # takes rather than truncating it -- see _fit_compact_height,
            # which grows the card to match.
            if self.is_primitive:
                  return _wrap_text(self.label, width=16, max_lines=None)
            if self.is_terminal:
                  return _wrap_text(self.label, width=24, max_lines=None)
            return _wrap_text(self.label, width=30, max_lines=2)

      @property
      def sub_lines(self) -> list[str]:
            if self.is_compact:
                  return [_short(self.sub, 28)] if self.sub else [""]
            return _wrap_text(self.sub, width=34, max_lines=1)

      @property
      def sub2_lines(self) -> list[str]:
            return [_short(self.sub2, 28)] if self.sub2 else [""]


@dataclass
class Branch:
      """A relationship group rendered as a labelled column of cards."""

      heading: str
      rank: int
      side: str                 # "left" or "right"
      cards: list[Card] = field(default_factory=list)
      key: str = ""             # stable id for pagination ("<dir>:<rela>")
      column: int = 1
      hx: float = 0.0           # heading anchor x
      hy: float = 0.0           # heading anchor y


@dataclass
class Connector:
      """A cubic-bezier link drawn from the hub to a card (or between cards)."""

      path: str
      color: str
      to_hub: bool              # True => arrow points at the hub (inbound)
      from_id: str = ""         # node_id of the edge's origin (hub uses "rx:<rxcui>")
      to_id: str = ""           # node_id of the edge's destination


def _hierarchy_url(rxcui: str) -> str:
      return reverse("admin:rxocrpl_rxnormconcept_hierarchy", args=[rxcui])


def _concept_url(rxcui: str) -> str:
      return _hierarchy_url(rxcui)


def _product_url(ndc: str) -> str:
      return reverse("admin:rxocrpl_product_change", args=[ndc])


def _more_url(concept: "RxNormConcept", next_pages: dict[str, int]) -> str:
      """URL that re-renders this same hierarchy with an advanced page cursor.

      The full paging state is carried in the query string so the link works
      as a plain navigation (progressive fallback) *and* as an in-place fetch
      target (the client appends ``&partial=1`` and swaps the returned SVG).
      """
      base = _hierarchy_url(concept.rxcui)
      pages = {k: v for k, v in next_pages.items() if v}
      if not pages:
            return base
      return f"{base}?{urlencode({'pages': json.dumps(pages, separators=(',', ':'))})}"


def _paginate(items: list, page: int) -> tuple[list, int, int]:
      """Return ``(page_items, clamped_page, total_pages)`` for ``items``."""
      total_pages = max(1, (len(items) + MAX_PER_BRANCH - 1) // MAX_PER_BRANCH)
      if page < 0 or page >= total_pages:
            page = 0
      start = page * MAX_PER_BRANCH
      return items[start : start + MAX_PER_BRANCH], page, total_pages


def _fit_compact_height(card: Card, minimum: float) -> float:
      """Grow a compact card's height so its (untruncated, possibly
      multi-line) title and its trailing sub line(s) all fit without
      overlapping -- called once the card's final label/sub/sub2 are set."""
      last_row_y = card.sub2_y if card.sub2 else card.sub_y
      return max(minimum, last_row_y - card.y + COMPACT_BOTTOM_PAD)


def _more_card(
          concept: "RxNormConcept",
          pages: dict[str, int],
          key: str,
          page: int,
          total_pages: int,
          remaining: int,
          noun: str,
) -> Card:
      """Build the dashed "next page" card for a paginated branch."""
      if remaining > 0:
            next_page = page + 1
            label = f"+{remaining} more {noun}"
            sub = f"page {page + 2} of {total_pages} →"
      else:
            # On the last page: loop the cursor back to the first page.
            next_page = 0
            label = "↺ back to start"
            sub = f"page {total_pages} of {total_pages}"
      return Card(
            label=label,
            sub=sub,
            color=DEFAULT_COLOR,
            url=_more_url(concept, {**pages, key: next_page}),
            is_more=True,
      )


_WORD_RE = re.compile(r"[a-z0-9]+")


def _apply_implied_specificity(cards: list[Card]) -> None:
      """Infer a narrower/broader relationship among sibling dose-form-group
      cards from name-token containment, and push the more specific siblings
      further out so the fan-out reflects the hierarchy the data model itself
      doesn't encode.

      RxNorm models SCDG/SBDG siblings as flat peers, but e.g. "Omeprazole
      Oral Product" and "Omeprazole Paste Product" are each broader than
      "Omeprazole Oral Paste Product", which combines both dose forms. We
      detect that by stripping the tokens common to every sibling (typically
      the ingredient name) and checking set containment on what's left: a
      proper subset is a broader/closer sibling that the superset implicitly
      depends on.
      """
      by_accent: dict[str, list[Card]] = {}
      for c in cards:
            if c.is_primitive:
                  by_accent.setdefault(c.accent, []).append(c)

      for group in by_accent.values():
            if len(group) < 2:
                  continue

            token_lists = {c: _WORD_RE.findall(c.label.lower()) for c in group}
            shortest = min(len(t) for t in token_lists.values())
            prefix_len = 0
            for i in range(shortest):
                  if len({t[i] for t in token_lists.values()}) == 1:
                        prefix_len += 1
                  else:
                        break
            token_sets = {c: frozenset(t[prefix_len:]) for c, t in token_lists.items()}

            def parents_of(card: Card) -> list[Card]:
                  ts = token_sets[card]
                  if not ts:
                        return []
                  candidates = [
                        o for o in group if o is not card and token_sets[o] and token_sets[o] < ts
                  ]
                  # keep only the immediate (maximal) covering siblings
                  return [
                        o for o in candidates
                        if not any(token_sets[o] < token_sets[p] for p in candidates if p is not o)
                  ]

            depth_memo: dict[str, int] = {}

            def depth_of(card: Card) -> int:
                  if card.sub in depth_memo:
                        return depth_memo[card.sub]
                  parents = parents_of(card)
                  d = 0 if not parents else 1 + max(depth_of(p) for p in parents)
                  depth_memo[card.sub] = d
                  return d

            for c in group:
                  parents = parents_of(c)
                  depth = depth_of(c)
                  if depth > 0:
                        c.column += depth
                        c.hop += depth
                        c.parent_rxcuis = [p.sub for p in parents]

      if by_accent:
            depths = {c.sub: c.column for group in by_accent.values() for c in group}
            cards.sort(key=lambda c: (depths.get(c.sub, c.column), c.label.lower()))


def _collect_branches(
          concept: "RxNormConcept", pages: dict[str, int]
) -> tuple[list[Branch], set[str]]:
      """Walk the anchor's relation graph outward into per-``rela`` branches.

      Each side (inbound / outbound) is walked breadth-first for up to
      ``MAX_HOPS`` hops, so the whole connected branch is shown -- not just the
      anchor's direct neighbours -- while staying at most ``2 * MAX_HOPS + 1``
      (hub included) columns wide. Only cards actually displayed on a hop
      (after per-branch paging) are expanded into the next hop, since a
      paged-away card has no visible position to draw a connector from.

      Each branch shows one page (``MAX_PER_BRANCH`` cards) of its members; the
      current page per branch is taken from ``pages`` (keyed by
      ``"<direction>:<rela>:<hop>"``). Returns the branches plus the set of
      every rxcui appearing in the ego graph (used to find connected products).
      """
      from rxocrpl.models import RxNormConceptRelation

      ego_rxcuis: set[str] = {concept.rxcui}
      branches: list[Branch] = []

      for direction in ("in", "out"):
            side = "right" if direction == "out" else "left"
            arrow = "→" if direction == "out" else "←"
            visited: set[str] = {concept.rxcui}
            frontier = [concept.rxcui]

            for hop in range(1, MAX_HOPS + 1):
                  if not frontier:
                        break
                  if direction == "out":
                        relations = list(
                              RxNormConceptRelation.objects.filter(
                                    source__rxcui__in=frontier
                              ).select_related("source", "target")
                        )
                  else:
                        relations = list(
                              RxNormConceptRelation.objects.filter(
                                    target__rxcui__in=frontier
                              ).select_related("source", "target")
                        )
                  if not relations:
                        break

                  # rela -> {rxcui -> (concept, parent_rxcui)}
                  grouped: dict[str, dict[str, tuple["RxNormConcept", str]]] = {}
                  for rel in relations:
                        neighbor = rel.target if direction == "out" else rel.source
                        parent = rel.source if direction == "out" else rel.target
                        if neighbor.rxcui in visited:
                              continue
                        members = grouped.setdefault(rel.rela, {})
                        if neighbor.rxcui not in members:
                              members[neighbor.rxcui] = (neighbor, parent.rxcui)
                  if not grouped:
                        break

                  next_frontier: list[str] = []
                  for rela, members in grouped.items():
                        entries = sorted(
                              members.values(),
                              key=lambda t: (
                                    TTY_RANK.get(t[0].tty or "", 2),
                                    (t[0].name or "").lower(),
                              ),
                        )
                        rank = min(
                              (TTY_RANK.get(c.tty or "", 2) for c, _ in entries), default=2
                        )
                        rela_disp = rela.replace("_", " ")
                        heading = (
                              f"{arrow} {rela_disp}"
                              if direction == "out"
                              else f"{rela_disp} {arrow}"
                        )
                        key = f"{direction}:{rela}:{hop}"
                        page_entries, page, total_pages = _paginate(
                              entries, pages.get(key, 0)
                        )
                        cards: list[Card] = []
                        for c, parent_rxcui in page_entries:
                              ego_rxcuis.add(c.rxcui)
                              next_frontier.append(c.rxcui)
                              is_dose_form_group = c.tty in ("SCDG", "SBDG")
                              card = Card(
                                    label=c.name or c.rxcui,
                                    sub=f"{c.rxcui}",
                                    color=_color_for(c.tty),
                                    url=_concept_url(c.rxcui),
                                    accent=c.tty or "",
                                    column=hop,
                                    hop=hop,
                                    parent_rxcuis=[parent_rxcui],
                                    w=PRIMITIVE_W if is_dose_form_group else CARD_W,
                                    h=PRIMITIVE_H if is_dose_form_group else CARD_H,
                              )
                              if is_dose_form_group:
                                    card.h = _fit_compact_height(card, PRIMITIVE_H)
                              cards.append(card)
                        _apply_implied_specificity(cards)
                        if total_pages > 1:
                              remaining = len(entries) - (
                                    page * MAX_PER_BRANCH + len(page_entries)
                              )
                              cards.append(
                                    _more_card(
                                          concept, pages, key, page, total_pages,
                                          remaining, rela_disp,
                                    )
                              )
                        branches.append(
                              Branch(
                                    heading=heading, rank=rank, side=side, cards=cards,
                                    key=key, column=hop,
                              )
                        )
                  visited.update(next_frontier)
                  frontier = next_frontier

      # Read inside-out: lowest TTY rank nearest the top, hop-1 columns before hop-2.
      left = [b for b in branches if b.side == "left"]
      right = [b for b in branches if b.side == "right"]
      left.sort(key=lambda b: (b.column, b.rank, b.heading))
      right.sort(key=lambda b: (b.column, b.rank, b.heading))
      return left + right, ego_rxcuis


def _collect_product_branch(
          concept: "RxNormConcept", ego_rxcuis: set[str], pages: dict[str, int]
) -> tuple[Branch | None, dict[str, str], int]:
      """Build the "NDC products" branch from confirmed RxCUI->NDC mappings.

      Shows one page (``MAX_PER_BRANCH`` cards) of the connected products; the
      current page is taken from ``pages`` (keyed by ``"products"``). Returns
      the branch, a mapping of product-NDC -> the rxcui it is tied to (so the
      caller can route each product's connector to the right concept card), and
      the true total number of distinct connected products.
      """
      from rxocrpl.models import Product, ProductRxNormMapping

      mappings = list(
            ProductRxNormMapping.objects.filter(rxcui__in=ego_rxcuis).order_by(
                  "rxcui", "product_ndc"
            )
      )
      total_products = len({m.product_ndc for m in mappings})
      if not mappings:
            return None, {}, 0

      products = Product.objects.in_bulk(
            [m.product_ndc for m in mappings], field_name="product_ndc"
      )

      # Build one card per distinct product (deduped, order preserved).
      all_cards: list[Card] = []
      ndc_to_rxcui: dict[str, str] = {}
      seen: set[str] = set()
      for m in mappings:
            if m.product_ndc in seen:
                  continue
            seen.add(m.product_ndc)
            ndc_to_rxcui[m.product_ndc] = m.rxcui
            product = products.get(m.product_ndc)
            if product:
                  label = product.brand_name or product.generic_name or m.product_ndc
                  dosage_form = product.dosage_form or ""
                  labeler = product.labeler_name or ""
            else:
                  label = m.name or m.product_ndc
                  dosage_form = ""
                  labeler = ""
            sub2 = " · ".join(p for p in (labeler, m.product_ndc) if p)
            card = Card(
                  label=label,
                  sub=dosage_form,
                  sub2=sub2,
                  color=PRODUCT_COLOR,
                  url=_product_url(m.product_ndc),
                  accent="NDC",
                  w=TERMINAL_W,
                  h=TERMINAL_H,
                  ndc=m.product_ndc,
            )
            card.h = _fit_compact_height(card, TERMINAL_H)
            all_cards.append(card)

      key = "products"
      cards, page, total_pages = _paginate(all_cards, pages.get(key, 0))
      cards = list(cards)
      if total_pages > 1:
            remaining = len(all_cards) - (page * MAX_PER_BRANCH + len(cards))
            cards.append(
                  _more_card(
                        concept, pages, key, page, total_pages, remaining, "products"
                  )
            )

      branch = Branch(
            heading="→ NDC products", rank=5, side="right", cards=cards, key=key
      )
      return branch, ndc_to_rxcui, total_products


def _branch_column_runs(branch: Branch) -> list[tuple[int, list[Card]]]:
      """Split a branch's cards into contiguous same-column runs.

      Cards are pre-sorted by column (see ``_apply_implied_specificity``), so
      a branch that has some cards pushed to a deeper column by an implied
      sub-hierarchy yields one run per column, in order.
      """
      runs: list[tuple[int, list[Card]]] = []
      current_col: int | None = None
      current: list[Card] = []
      for card in branch.cards:
            if card.column != current_col:
                  if current:
                        runs.append((current_col, current))
                  current_col = card.column
                  current = []
            current.append(card)
      if current:
            runs.append((current_col, current))
      return runs


def _run_height(cards: list[Card], has_heading: bool) -> float:
      n = len(cards)
      if n == 0:
            return GROUP_HEADER_H if has_heading else 0.0
      return (
            (GROUP_HEADER_H if has_heading else 0.0)
            + sum(card.h for card in cards)
            + (n - 1) * CARD_VGAP
      )


def _side_columns(
          side: list[Branch]
) -> dict[int, list[tuple[Branch, list[Card]]]]:
      """Group a side's branch content by rendered column.

      Each column then lays out and packs independently instead of every
      branch on a side sharing one vertical cursor -- otherwise a column
      further from the hub gets needlessly pushed down below the full height
      of a nearer column it has no vertical relationship with.
      """
      columns: dict[int, list[tuple[Branch, list[Card]]]] = {}
      for branch in side:
            for col, cards_in_run in _branch_column_runs(branch):
                  columns.setdefault(col, []).append((branch, cards_in_run))
      return columns


def _column_height(runs: list[tuple[Branch, list[Card]]]) -> float:
      if not runs:
            return 0.0
      total = 0.0
      for i, (branch, cards_in_run) in enumerate(runs):
            has_heading = cards_in_run[0].column == branch.column
            total += _run_height(cards_in_run, has_heading)
            if i < len(runs) - 1:
                  total += GROUP_GAP
      return total


def _bezier(sx: float, sy: float, ex: float, ey: float) -> str:
      """Horizontal-ish cubic bezier between two points."""
      dx = max(40.0, abs(ex - sx) * 0.5)
      c1x = sx + (dx if ex >= sx else -dx)
      c2x = ex + (-dx if ex >= sx else dx)
      return f"M {sx:.1f} {sy:.1f} C {c1x:.1f} {sy:.1f} {c2x:.1f} {ey:.1f} {ex:.1f} {ey:.1f}"


def build_hierarchy(
          concept: "RxNormConcept", pages: dict[str, int] | None = None
) -> dict:
      """Compute the full positioned diagram for ``concept``.

      ``pages`` maps a branch key (``"<direction>:<rela>"`` or ``"products"``)
      to the 0-based page of that branch to display; branches cap at
      ``MAX_PER_BRANCH`` cards each and expose a "more" card that advances the
      cursor. Returns a context dict ready to hand to the template: SVG
      dimensions, the hub, positioned cards, connectors, a legend and summary
      counts.
      """
      pages = pages or {}
      branches, ego_rxcuis = _collect_branches(concept, pages)
      right_concept_columns = max(
            (card.column for b in branches if b.side == "right" for card in b.cards),
            default=0,
      )
      product_branch, ndc_to_rxcui, total_products = _collect_product_branch(
            concept, ego_rxcuis, pages
      )
      if product_branch is not None:
            product_branch.column = right_concept_columns + 1 if right_concept_columns else 1
            for card in product_branch.cards:
                  card.column = product_branch.column
            branches.append(product_branch)

      left = [b for b in branches if b.side == "left"]
      right = [b for b in branches if b.side == "right"]

      def side_height(side: list[Branch]) -> float:
            columns = _side_columns(side)
            if not columns:
                  return 0.0
            return max(_column_height(runs) for runs in columns.values())

      content_h = max(side_height(left), side_height(right), HUB_H)
      total_h = MARGIN_TOP + content_h + MARGIN_BOTTOM
      center_y = MARGIN_TOP + content_h / 2

      # Horizontal extents: hub in the middle, columns of cards fanning out
      # BRANCH_GAP_X beyond each edge, one column per hop walked (a card's own
      # column can run deeper than its branch's base column when an implied
      # sub-hierarchy pushed it further out -- see _apply_implied_specificity).
      def side_extent(side: list[Branch]) -> float:
            columns = max(
                  (card.column for b in side for card in b.cards), default=0
            )
            if not columns:
                  return HUB_W / 2
            return (
                  HUB_W / 2
                  + BRANCH_GAP_X
                  + columns * CARD_W
                  + max(0, columns - 1) * BRANCH_COLUMN_GAP_X
            )

      left_extent = side_extent(left)
      right_extent = side_extent(right)
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
            "name_lines": _wrap_text(concept.name, width=34, max_lines=2),
            "rxcui": concept.rxcui,
            "node_id": f"rx:{concept.rxcui}",
            "tty": concept.tty or "",
            "tty_label": TTY_LABELS.get(concept.tty or "", "Concept"),
            "name_y": center_y - 13,
            "meta_y": center_y + 22,
      }

      connectors: list[Connector] = []
      rxcui_card_pos: dict[str, tuple[float, float]] = {}

      def place_side(side: list[Branch], is_left: bool) -> None:
            # Each column gets its own vertical cursor, centred independently,
            # so a column further from the hub packs tightly instead of
            # starting below the unrelated content of a nearer column.
            for col, runs in _side_columns(side).items():
                  y = center_y - _column_height(runs) / 2
                  for branch, cards_in_run in runs:
                        has_heading = cards_in_run[0].column == branch.column
                        if has_heading:
                              branch.hy = y + GROUP_HEADER_H - 8
                              if is_left:
                                    branch.hx = (
                                          center_x
                                          - HUB_W / 2
                                          - BRANCH_GAP_X
                                          - (branch.column - 1) * (CARD_W + BRANCH_COLUMN_GAP_X)
                                    )  # right-align heading against its base column
                              else:
                                    branch.hx = (
                                          center_x
                                          + HUB_W / 2
                                          + BRANCH_GAP_X
                                          + (branch.column - 1) * (CARD_W + BRANCH_COLUMN_GAP_X)
                                    )
                              cy = y + GROUP_HEADER_H
                        else:
                              cy = y

                        for card in cards_in_run:
                              if is_left:
                                    slot_x = (
                                          center_x
                                          - HUB_W / 2
                                          - BRANCH_GAP_X
                                          - col * CARD_W
                                          - (col - 1) * BRANCH_COLUMN_GAP_X
                                    )
                                    # keep the inner (hub-facing) edge flush
                                    # regardless of the card's own (possibly
                                    # compact) width
                                    card.x = slot_x + (CARD_W - card.w)
                              else:
                                    card.x = (
                                          center_x
                                          + HUB_W / 2
                                          + BRANCH_GAP_X
                                          + (col - 1) * (CARD_W + BRANCH_COLUMN_GAP_X)
                                    )
                              card.y = cy
                              cy += card.h + CARD_VGAP
                        y += _run_height(cards_in_run, has_heading) + GROUP_GAP

      place_side(left, is_left=True)
      place_side(right, is_left=False)

      # Record concept-card positions so product/deeper-hop connectors can
      # target them.
      for branch in left + right:
            for card in branch.cards:
                  if not card.is_more and card.accent != "NDC":
                        rxcui_card_pos[card.sub] = (card.x, card.cy)

      # Connect every concept card to the card(s) it was reached through,
      # falling back to the hub when its parent isn't itself shown (e.g. a
      # hop-1 card whose "parent" is the anchor, or a paged-away parent).
      hub_edge = {
            True: (center_x - HUB_W / 2, center_y),
            False: (center_x + HUB_W / 2, center_y),
      }
      for branch in left + right:
            if branch is product_branch:
                  continue
            is_left = branch.side == "left"
            for card in branch.cards:
                  if card.is_more:
                        continue
                  inner_x = card.x + card.w if is_left else card.x
                  parent_hits = [
                        (r, rxcui_card_pos[r])
                        for r in card.parent_rxcuis
                        if r in rxcui_card_pos
                  ]
                  if parent_hits:
                        for r, (px, py) in parent_hits:
                              sx = px if is_left else px + CARD_W
                              connectors.append(
                                    Connector(
                                          path=_bezier(sx, py, inner_x, card.cy),
                                          color=card.color, to_hub=is_left,
                                          from_id=f"rx:{r}", to_id=card.node_id,
                                    )
                              )
                  else:
                        sx, sy = hub_edge[is_left]
                        connectors.append(
                              Connector(
                                    path=_bezier(sx, sy, inner_x, card.cy),
                                    color=card.color, to_hub=is_left,
                                    from_id=hub["node_id"], to_id=card.node_id,
                              )
                        )

      # Re-route product connectors to the concept they map to (when shown).
      if product_branch is not None:
            for card in product_branch.cards:
                  if card.is_more:
                        continue
                  target_rxcui = ndc_to_rxcui.get(card.ndc)
                  pos = rxcui_card_pos.get(target_rxcui or "")
                  if pos:
                        # link product -> its concept card instead of the hub
                        tx, ty = pos
                        connectors.append(
                              Connector(
                                    path=_bezier(card.x, card.cy, tx, ty),
                                    color=PRODUCT_COLOR,
                                    to_hub=False,
                                    from_id=f"rx:{target_rxcui}", to_id=card.node_id,
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
            "partial_url": _hierarchy_url(concept.rxcui),
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
