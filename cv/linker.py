"""
Tiered cross-image component linking with partial-information inference.

The matching problem is richer than 1:1 component-to-component pairing:

  1. Two crops within the SAME image may show the same physical unit from
     different angles, OR two interchangeable units (same lot/exp). They
     should be collapsed into an equivalence class before cross-image
     matching, with merged evidence from all members.

  2. Crops carry partial information. A rear-view vial may show only
     Lot/Exp; a front-view of the same vial shows product/NDC/strength.
     Linking should chain through shared evidence: if A and B share Lot
     within Image 1, and B and C share NDC across images, then A and C
     are linked even though they share no directly-visible field.

  3. "Match" is not binary. Three meaningful tiers:
       - IDENTITY: same physical lot (same NDC + Lot + Exp).
       - SKU: same product SKU (same NDC, different/unknown lots).
       - THERAPEUTIC: same drug + strength, different manufacturer.

This module produces all three.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, asdict

import numpy as np


# ============================================================
# CONFIDENCE TIERS
# ============================================================

class LinkTier:
      IDENTITY = "identity"  # same physical lot (NDC + Lot + Exp all match)
      SKU = "sku"  # same product SKU (NDC matches, lots differ or unknown)
      THERAPEUTIC = "therapeutic"  # same drug+strength, different mfg/NDC


CONFIDENCE_LABELS = {
      LinkTier.IDENTITY: "very_high",
      LinkTier.SKU: "high",
      LinkTier.THERAPEUTIC: "medium",
}


# ============================================================
# EQUIVALENCE CLASS
# ============================================================

@dataclass
class EquivClass:
      """
      A group of components from one image that represent the same physical
      unit or interchangeable units. Evidence is merged across members so
      downstream matching benefits from the union of fields.
      """
      image_index: int
      member_indices: list[int]  # component_index values
      member_types: list[str]  # vial / bag

      # Merged evidence (None where no member supplied a value)
      ndc: str | None = None
      lot: str | None = None
      expiration: str | None = None
      manufacturer: str | None = None
      product: str | None = None
      strength: str | None = None
      volume_ml: str | None = None

      # Why members were grouped
      grouping_reason: str | None = None  # "same_lot_ndc", "single", "visual"

      def label(self) -> str:
            n = len(self.member_indices)
            if n == 1:
                  return f"img{self.image_index}#{self.member_indices[0]}"
            return f"img{self.image_index}#{{{','.join(str(i) for i in self.member_indices)}}}"


# ============================================================
# LINKS
# ============================================================

@dataclass
class Link:
      """
      A typed link between two equivalence classes (one per image).
      """
      image1_class_id: int  # index into the per-image EquivClass list
      image2_class_id: int
      image1_components: list[int]
      image2_components: list[int]
      tier: str  # LinkTier.*
      confidence: str  # CONFIDENCE_LABELS[tier]
      score: float  # auxiliary numeric, for sorting/ambiguity
      shared_fields: list[str]  # which fields directly matched
      inferred_fields: list[str]  # fields inferred via class merging
      notes: str | None = None  # free-form caveats (e.g. "lot mismatch")


@dataclass
class UnmatchedClass:
      image_index: int
      class_id: int
      component_indices: list[int]
      reason: str  # "no_candidate", "ambiguous_only"


# ============================================================
# WITHIN-IMAGE EQUIVALENCE-CLASS BUILDING
# ============================================================

def _idlike(v):
      """Normalize an ID-like field for equality comparison."""
      if v is None:
            return None
      s = str(v).strip().upper()
      return s.replace(" ", "").replace("-", "") or None


def _both(a, b):
      return a is not None and b is not None


def _same_physical_lot(c1: dict, c2: dict) -> bool:
      """
      Two components are the same physical lot when their *observed* fields
      are mutually consistent and at minimum share lot+exp OR ndc+lot.
      Missing fields don't disprove identity (they just don't confirm it),
      but observed-but-conflicting fields do.
      """
      n1, n2 = _idlike(c1.get("ndc")), _idlike(c2.get("ndc"))
      l1, l2 = _idlike(c1.get("lot")), _idlike(c2.get("lot"))
      e1, e2 = _idlike(c1.get("expiration")), _idlike(c2.get("expiration"))
      m1, m2 = c1.get("manufacturer"), c2.get("manufacturer")

      # Conflicts on observed fields → not the same lot
      if _both(n1, n2) and n1 != n2:
            return False
      if _both(l1, l2) and l1 != l2:
            return False
      if _both(e1, e2) and e1 != e2:
            return False
      if _both(m1, m2) and m1.lower() != m2.lower():
            return False

      # Need at least one strong shared field plus one corroborating field
      shared_strong = (_both(l1, l2) and l1 == l2)
      shared_ndc = (_both(n1, n2) and n1 == n2)
      shared_exp = (_both(e1, e2) and e1 == e2)
      shared_mfg = (_both(m1, m2) and m1.lower() == m2.lower())

      # Lot+Exp, Lot+NDC, or Lot+Mfg are each sufficient to call same physical lot
      if shared_strong and (shared_exp or shared_ndc or shared_mfg):
            return True
      return False


def _merge_field(a, b):
      """Prefer first non-None; if both present and disagree, keep first (caller policy)."""
      return a if a is not None else b


def _components_must_be_same_type(c1, c2) -> bool:
      return c1.get("component_type") == c2.get("component_type")


def build_equiv_classes(components: list[dict],
                        image_index: int) -> list[EquivClass]:
      """
      Group within-image components into equivalence classes via
      union-find on the same-physical-lot relation.
      """
      n = len(components)
      parent = list(range(n))

      def find(i):
            while parent[i] != i:
                  parent[i] = parent[parent[i]]
                  i = parent[i]
            return i

      def union(i, j):
            ri, rj = find(i), find(j)
            if ri != rj:
                  parent[rj] = ri

      for i in range(n):
            for j in range(i + 1, n):
                  if not _components_must_be_same_type(components[i], components[j]):
                        continue
                  if _same_physical_lot(components[i], components[j]):
                        union(i, j)

      groups: dict[int, list[int]] = defaultdict(list)
      for i in range(n):
            groups[find(i)].append(i)

      classes: list[EquivClass] = []
      for members in groups.values():
            members.sort()
            merged_fields = {
                  "ndc": None, "lot": None, "expiration": None,
                  "manufacturer": None, "product": None,
                  "strength": None, "volume_ml": None,
            }
            for k in members:
                  c = components[k]
                  for field_name in merged_fields:
                        merged_fields[field_name] = _merge_field(
                              merged_fields[field_name], c.get(field_name))

            ec = EquivClass(
                  image_index=image_index,
                  member_indices=[components[k]["component_index"] for k in members],
                  member_types=[components[k]["component_type"] for k in members],
                  grouping_reason="same_lot_ndc" if len(members) > 1 else "single",
                  **merged_fields,
            )
            classes.append(ec)

      return classes


@dataclass
class WithinImageRelation:
      """
      A relationship between two equivalence classes in the SAME image.
      Surfaces e.g. 'these two vials are the same product but different mfg'.
      """
      image_index: int
      class_a_id: int
      class_b_id: int
      class_a_components: list[int]
      class_b_components: list[int]
      tier: str
      confidence: str
      shared_fields: list[str]
      notes: str | None = None


def find_within_image_relations(classes: list[EquivClass],
                                image_index: int) -> list[WithinImageRelation]:
      """
      Look for SKU or therapeutic relationships *within* a single image's
      equivalence classes. (Identity-tier groupings are already handled by
      class-collapse — anything still in separate classes is by definition
      not the same physical lot.)
      """
      out: list[WithinImageRelation] = []
      for i in range(len(classes)):
            for j in range(i + 1, len(classes)):
                  tier, _, shared, notes = _classify_pair(classes[i], classes[j])
                  if tier in (LinkTier.SKU, LinkTier.THERAPEUTIC):
                        out.append(WithinImageRelation(
                              image_index=image_index,
                              class_a_id=i, class_b_id=j,
                              class_a_components=classes[i].member_indices,
                              class_b_components=classes[j].member_indices,
                              tier=tier,
                              confidence=CONFIDENCE_LABELS[tier],
                              shared_fields=shared,
                              notes=notes,
                        ))
      return out


# ============================================================
# CROSS-IMAGE TIER CLASSIFICATION
# ============================================================

def _classify_pair(a: EquivClass, b: EquivClass):
      """
      Decide which tier (if any) connects two equivalence classes.
      Returns (tier, score, shared_fields, notes) or (None, 0.0, [], None).
      """
      if a.member_types and b.member_types:
            # If neither side is fully homogeneous, this is permissive: we
            # only veto when *all* members on both sides disagree on type.
            types_a = set(a.member_types)
            types_b = set(b.member_types)
            if not (types_a & types_b):
                  return None, 0.0, [], "type_mismatch"

      n1, n2 = _idlike(a.ndc), _idlike(b.ndc)
      l1, l2 = _idlike(a.lot), _idlike(b.lot)
      e1, e2 = _idlike(a.expiration), _idlike(b.expiration)
      m1, m2 = a.manufacturer, b.manufacturer
      p1, p2 = a.product, b.product
      s1, s2 = a.strength, b.strength
      v1, v2 = a.volume_ml, b.volume_ml

      shared = []
      notes_parts = []

      ndc_match = _both(n1, n2) and n1 == n2
      ndc_conflict = _both(n1, n2) and n1 != n2
      lot_match = _both(l1, l2) and l1 == l2
      lot_conflict = _both(l1, l2) and l1 != l2
      exp_match = _both(e1, e2) and e1 == e2
      exp_conflict = _both(e1, e2) and e1 != e2
      mfg_match = _both(m1, m2) and m1.lower() == m2.lower()
      mfg_conflict = _both(m1, m2) and m1.lower() != m2.lower()
      prod_match = _both(p1, p2) and p1.lower() == p2.lower()
      prod_conflict = _both(p1, p2) and p1.lower() != p2.lower()
      strength_match = _both(s1, s2) and s1 == s2
      vol_match = _both(v1, v2) and v1 == v2

      if ndc_match: shared.append("ndc")
      if lot_match: shared.append("lot")
      if exp_match: shared.append("expiration")
      if mfg_match: shared.append("manufacturer")
      if prod_match: shared.append("product")
      if strength_match: shared.append("strength")
      if vol_match: shared.append("volume_ml")

      # ---- IDENTITY: same physical lot ----
      # Strong identity: NDC + Lot match, no conflicts.
      # Permissive identity: Lot + Exp match (with NDC unknown on at least
      #   one side and not conflicting), or Lot + Mfg match likewise.
      if not (ndc_conflict or lot_conflict or exp_conflict or mfg_conflict):
            if ndc_match and lot_match:
                  score = 10.0 + (1.0 if exp_match else 0.0) + (0.5 if mfg_match else 0.0)
                  return LinkTier.IDENTITY, score, shared, None
            if lot_match and (exp_match or mfg_match):
                  score = 8.0 + (0.5 if mfg_match else 0.0)
                  note = "identity inferred from lot+exp (NDC not co-observed)"
                  return LinkTier.IDENTITY, score, shared, note

      # If lots conflict, identity is ruled out — fall through to SKU.
      if lot_conflict:
            notes_parts.append("different lots")

      # ---- SKU: same product, possibly different lots ----
      # Same NDC with no fatal conflicts.
      if ndc_match and not (mfg_conflict or prod_conflict):
            score = 6.0
            if strength_match: score += 0.5
            if vol_match: score += 0.5
            note = "; ".join(notes_parts) or None
            return LinkTier.SKU, score, shared, note

      # Same product+strength+manufacturer but NDC unknown on a side.
      if prod_match and strength_match and mfg_match \
                and not (ndc_conflict or lot_conflict):
            return LinkTier.SKU, 5.0, shared, "SKU inferred without NDC co-observation"

      # ---- THERAPEUTIC: same drug+strength, different mfg ----
      if prod_match and strength_match and (mfg_conflict or ndc_conflict):
            return (LinkTier.THERAPEUTIC, 3.5, shared,
                    "same product/strength, different manufacturer")

      if prod_match and (strength_match or vol_match) and not (mfg_conflict or ndc_conflict):
            return (LinkTier.THERAPEUTIC, 2.5, shared,
                    "same product, manufacturer not co-observed")

      return None, 0.0, [], None


# ============================================================
# LINKING
# ============================================================

def link_classes(
          classes_1: list[EquivClass],
          classes_2: list[EquivClass],
          ambiguity_margin: float = 1.0,
) -> tuple[list[Link], list[UnmatchedClass], list[UnmatchedClass]]:
      """
      Score every (class_1, class_2) pair, classify into a tier, and emit
      Link objects. Within each tier, do ambiguity resolution:
      - If a row's best partner is also that partner's best (within margin),
        it's a clean link.
      - If multiple partners are within `ambiguity_margin` of the best,
        emit Links for each but mark them with notes.

      Higher tiers are resolved first; once a class is consumed by an
      IDENTITY link, it's skipped for SKU/THERAPEUTIC consideration.
      """
      n1, n2 = len(classes_1), len(classes_2)
      if n1 == 0 or n2 == 0:
            unmatched1 = [UnmatchedClass(1, i, c.member_indices, "no_candidate")
                          for i, c in enumerate(classes_1)]
            unmatched2 = [UnmatchedClass(2, j, c.member_indices, "no_candidate")
                          for j, c in enumerate(classes_2)]
            return [], unmatched1, unmatched2

      # Score every pair once
      pair_info = {}  # (i,j) -> (tier, score, shared, notes)
      for i, a in enumerate(classes_1):
            for j, b in enumerate(classes_2):
                  tier, score, shared, notes = _classify_pair(a, b)
                  if tier is not None:
                        pair_info[(i, j)] = (tier, score, shared, notes)

      consumed_1: set[int] = set()
      consumed_2: set[int] = set()
      links: list[Link] = []

      # Process tiers in priority order
      for tier in (LinkTier.IDENTITY, LinkTier.SKU, LinkTier.THERAPEUTIC):
            # Build per-row and per-column best scores for this tier
            per_row_best: dict[int, float] = {}
            per_col_best: dict[int, float] = {}
            for (i, j), (t, s, _, _) in pair_info.items():
                  if t != tier or i in consumed_1 or j in consumed_2:
                        continue
                  per_row_best[i] = max(per_row_best.get(i, -np.inf), s)
                  per_col_best[j] = max(per_col_best.get(j, -np.inf), s)

            # Pick all (i,j) within ambiguity margin of both row and col best.
            tier_links: list[tuple[int, int, float, list[str], str | None]] = []
            for (i, j), (t, s, shared, notes) in pair_info.items():
                  if t != tier or i in consumed_1 or j in consumed_2:
                        continue
                  row_best = per_row_best.get(i, -np.inf)
                  col_best = per_col_best.get(j, -np.inf)
                  if s >= row_best - ambiguity_margin and s >= col_best - ambiguity_margin:
                        tier_links.append((i, j, s, shared, notes))

            # Detect ambiguity — multiple partners on either side at this tier
            row_partners: dict[int, list[int]] = defaultdict(list)
            col_partners: dict[int, list[int]] = defaultdict(list)
            for i, j, _, _, _ in tier_links:
                  row_partners[i].append(j)
                  col_partners[j].append(i)

            for i, j, s, shared, notes in tier_links:
                  ambig_left = len(row_partners[i]) > 1
                  ambig_right = len(col_partners[j]) > 1
                  ambig_note_parts = []
                  if ambig_left:
                        others = [classes_2[jj].label() for jj in row_partners[i] if jj != j]
                        ambig_note_parts.append(
                              f"img1 class also matches {', '.join(others)}")
                  if ambig_right:
                        others = [classes_1[ii].label() for ii in col_partners[j] if ii != i]
                        ambig_note_parts.append(
                              f"img2 class also matches {', '.join(others)}")
                  if ambig_note_parts:
                        full_note = "; ".join(filter(None, [notes, *ambig_note_parts]))
                  else:
                        full_note = notes

                  inferred = _inferred_fields(classes_1[i], classes_2[j], shared)

                  links.append(Link(
                        image1_class_id=i,
                        image2_class_id=j,
                        image1_components=classes_1[i].member_indices,
                        image2_components=classes_2[j].member_indices,
                        tier=tier,
                        confidence=CONFIDENCE_LABELS[tier],
                        score=float(s),
                        shared_fields=shared,
                        inferred_fields=inferred,
                        notes=full_note,
                  ))

            # Only consume a class if its tier-link partners are unambiguous on its side.
            for i, j, _, _, _ in tier_links:
                  if len(row_partners[i]) == 1:
                        consumed_1.add(i)
                  if len(col_partners[j]) == 1:
                        consumed_2.add(j)

      unmatched1 = [UnmatchedClass(1, i, c.member_indices, "no_candidate")
                    for i, c in enumerate(classes_1) if i not in consumed_1
                    and not any(lk.image1_class_id == i for lk in links)]
      unmatched2 = [UnmatchedClass(2, j, c.member_indices, "no_candidate")
                    for j, c in enumerate(classes_2) if j not in consumed_2
                    and not any(lk.image2_class_id == j for lk in links)]

      return links, unmatched1, unmatched2


def _inferred_fields(a: EquivClass, b: EquivClass,
                     shared_fields: list[str]) -> list[str]:
      """
      Fields where one side knows the value and the other side's class
      inherits it through within-image merging (i.e. one of its members
      contributed the field that's now matching across images).
      A field is 'inferred' for a class if it came from class merging,
      not from the specific member that's being matched.
      Here we approximate this as: a field is inferred for the link when
      the class has multiple members and the field is in shared_fields.
      """
      inferred = []
      for f in shared_fields:
            if len(a.member_indices) > 1 or len(b.member_indices) > 1:
                  inferred.append(f)
      return inferred


# ============================================================
# SERIALIZATION
# ============================================================

def equiv_class_to_dict(c: EquivClass) -> dict:
      return asdict(c)


def link_to_dict(lk: Link) -> dict:
      return asdict(lk)


def unmatched_to_dict(u: UnmatchedClass) -> dict:
      return asdict(u)


def within_relation_to_dict(r: WithinImageRelation) -> dict:
      return asdict(r)


# ============================================================
# TOP-LEVEL ENTRY
# ============================================================

def link_images(components_1: list[dict],
                components_2: list[dict],
                ambiguity_margin: float = 1.0) -> dict:
      """
      Top-level entry point. Builds equivalence classes within each image,
      then produces tiered links between them.

      Returns a dict suitable for embedding in the pipeline JSON.
      """
      classes_1 = build_equiv_classes(components_1, image_index=1)
      classes_2 = build_equiv_classes(components_2, image_index=2)

      within_1 = find_within_image_relations(classes_1, image_index=1)
      within_2 = find_within_image_relations(classes_2, image_index=2)

      links, unmatched_1, unmatched_2 = link_classes(
            classes_1, classes_2, ambiguity_margin=ambiguity_margin)

      by_tier = defaultdict(list)
      for lk in links:
            by_tier[lk.tier].append(link_to_dict(lk))

      return {
            "equiv_classes_image1": [equiv_class_to_dict(c) for c in classes_1],
            "equiv_classes_image2": [equiv_class_to_dict(c) for c in classes_2],
            "within_image_relations": {
                  "image1": [within_relation_to_dict(r) for r in within_1],
                  "image2": [within_relation_to_dict(r) for r in within_2],
            },
            "links_by_tier": {
                  "identity": by_tier[LinkTier.IDENTITY],
                  "sku": by_tier[LinkTier.SKU],
                  "therapeutic": by_tier[LinkTier.THERAPEUTIC],
            },
            "unmatched_image1": [unmatched_to_dict(u) for u in unmatched_1],
            "unmatched_image2": [unmatched_to_dict(u) for u in unmatched_2],
      }
