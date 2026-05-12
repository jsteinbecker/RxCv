"""
Stage 4: Matching (reference-based)
====================================
Assignment problem, not clustering.

Domain assumption (provided by the user):
    The first image contains the complete inventory of components.
    Subsequent images contain SUBSETS of that inventory, possibly mixed
    with out-of-inventory items (syringes, filters, etc.) that should
    NOT be matched to any reference slot.

So matching becomes: for each subsequent image, assign each detection
to either (a) one of the reference slots from image 1, or (b) "out of
inventory". Two detections in the same subsequent image must not claim
the same reference slot — when image 1 has duplicates (e.g., three
identical Pfizer vials) and a subsequent image shows two of them, each
should map to a different slot.

This is exactly the bipartite assignment problem, solved optimally by
the Hungarian algorithm (scipy.optimize.linear_sum_assignment).

Output:
    A ReferenceMatch result with:
        - slot definitions (one per image-1 detection)
        - per-image assignments (which slot each detection maps to)
        - missing slots per subsequent image
        - out-of-inventory detections per subsequent image
        - per-image and overall completeness counts
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Sequence

import numpy as np
from scipy.optimize import linear_sum_assignment

# Imports support both package (python -m) and script (python pipeline.py) invocation.
try:
      from .detection import Detection
      from .embedding import cosine_similarity_matrix
      from ..ocr.ocr_fields import OCRFields
except ImportError:
      from detection.detection import Detection
      from detection.embedding import cosine_similarity_matrix
      from ocr.ocr_fields import OCRFields

# Score weights for the hybrid match metric.
# When a barcode-confirmed NDC match is available it replaces the OCR-NDC term
# at a higher weight (W_BARCODE_NDC) because barcode reads are essentially
# error-free compared to OCR on small label text.
W_VISUAL = 0.35
W_LOT = 0.30
W_NDC = 0.15        # OCR-inferred NDC match
W_BARCODE_NDC = 0.30  # barcode-confirmed NDC match (replaces W_NDC when applicable)
W_EXP = 0.05

# Minimum score for a subsequent detection to be assigned to a reference
# slot. Below this, the detection is classified as out-of-inventory.
# 0.45 is conservative — it lets visual-similarity-only matches through
# (visual=1.0 alone scores 0.40, plus partial text agreement gets it over
# the threshold) but rejects clearly-different objects.
ASSIGNMENT_THRESHOLD = 0.45

# Above this visual-only floor, lot-number agreement alone is sufficient
# evidence to assign even when other text fields disagree or are missing.
# Useful when OCR partially fails on one side of the comparison.
VISUAL_FLOOR_FOR_LOT_OVERRIDE = 0.55


@dataclass
class ReferenceSlot:
      """One unique inventory item, defined by an image-1 detection."""

      slot_id: int
      reference_detection_id: int  # the image-1 Detection.instance_id
      class_label: str | None
      lot: str | None
      ndc: str | None
      exp: str | None
      brand: str | None = None     # proprietary (brand) name from OCR or NDC DB
      strength: str | None = None  # drug strength from OCR or NDC DB

      def describe(self) -> str:
            """Short human-readable summary of what's in this slot."""
            parts = [self.class_label or "object"]
            if self.brand:
                  parts.append(self.brand)
            if self.strength:
                  parts.append(self.strength)
            if self.lot:
                  parts.append(f"lot={self.lot}")
            if self.ndc:
                  parts.append(f"ndc={self.ndc}")
            return " ".join(parts)


@dataclass
class Assignment:
      """One subsequent-image detection's assignment outcome."""

      detection_id: int  # global instance_id of the subsequent detection
      source_image: str
      slot_id: int | None  # None means out-of-inventory (or excess under certification)
      score: float  # the hybrid match score that drove the assignment
      # True when the user certified subset∈inventory but this detection still
      # couldn't be slotted because the image had more detections than the
      # reference has slots. Distinguishes a *capacity* failure from a normal
      # below-threshold OOI rejection.
      excess_under_certification: bool = False
      # True when the assignment was made under certification mode and the raw
      # match score was below the normal threshold. The slot was filled anyway,
      # but a human reviewer should sanity-check the pairing.
      forced: bool = False


@dataclass
class ImageReport:
      """Per-image breakdown of what was found vs. what was expected."""

      source_image: str
      is_reference: bool
      detection_count: int
      assigned_count: int = 0  # detections that mapped to a reference slot
      out_of_inventory_count: int = 0  # detections with no good slot match
      missing_slot_ids: list[int] = field(default_factory=list)
      matched_slot_ids: list[int] = field(default_factory=list)
      # Force-assign mode telemetry
      forced_assignment_count: int = 0  # assignments made under certification
      # whose raw score was below threshold
      excess_count: int = 0  # detections we couldn't slot even with force-assign
      # because the image had more dets than ref slots


@dataclass
class ReferenceMatch:
      """Full output of reference-based matching."""

      slots: list[ReferenceSlot]
      assignments: list[Assignment]
      image_reports: list[ImageReport]

      def slot_by_id(self, slot_id: int) -> ReferenceSlot:
            return self.slots[slot_id]

      def report_for_image(self, source_image: str) -> ImageReport | None:
            for r in self.image_reports:
                  if r.source_image == source_image:
                        return r
            return None


# ---------------------------------------------------------------------------
# Pair scoring (same logic as before, factored out for reuse)
# ---------------------------------------------------------------------------


def _pair_score(
          visual_sim: float,
          fields_a: OCRFields,
          fields_b: OCRFields,
) -> tuple[float, dict[str, bool]]:
      """Hybrid match score between two detections.

    Barcode-confirmed NDC matches are weighted at W_BARCODE_NDC (0.30) rather
    than the OCR-only W_NDC (0.15). A barcode match fires when either side has
    a barcode_ndc and it agrees with the other side's best available NDC.
    """
      lot_match = bool(
            fields_a.lot and fields_b.lot and fields_a.lot == fields_b.lot
      )
      ndc_match = bool(
            fields_a.ndc and fields_b.ndc and fields_a.ndc == fields_b.ndc
      )
      exp_match = bool(
            fields_a.exp and fields_b.exp and fields_a.exp == fields_b.exp
      )

      # Barcode NDC: prefer barcode_ndc when present, fall back to OCR ndc.
      a_ndc = fields_a.barcode_ndc or fields_a.ndc
      b_ndc = fields_b.barcode_ndc or fields_b.ndc
      barcode_ndc_match = bool(
            a_ndc and b_ndc and a_ndc == b_ndc
            and (fields_a.barcode_ndc or fields_b.barcode_ndc)
      )

      ndc_component = (
            W_BARCODE_NDC if barcode_ndc_match
            else W_NDC if ndc_match
            else 0.0
      )

      score = (
                W_VISUAL * visual_sim
                + W_LOT * (1.0 if lot_match else 0.0)
                + ndc_component
                + W_EXP * (1.0 if exp_match else 0.0)
      )
      return score, {
            "lot": lot_match,
            "ndc": ndc_match,
            "barcode_ndc": barcode_ndc_match,
            "exp": exp_match,
      }


def _build_score_matrix(
          sub_indices: Sequence[int],
          ref_indices: Sequence[int],
          embeddings: np.ndarray,
          field_records: Sequence[OCRFields],
) -> tuple[np.ndarray, np.ndarray]:
      """Build the score and visual-similarity matrices for one subsequent image.

    Returns (scores, visual_sims), each shape [n_sub, n_ref].
    """
      n_sub = len(sub_indices)
      n_ref = len(ref_indices)
      scores = np.zeros((n_sub, n_ref), dtype=np.float64)
      visual_sims = np.zeros((n_sub, n_ref), dtype=np.float64)

      # Compute visual similarities in one batched dot-product. Both blocks of
      # embeddings are L2-normalized rows, so the matrix product gives cosine
      # similarities directly.
      if n_sub > 0 and n_ref > 0:
            sub_embs = embeddings[list(sub_indices)]
            ref_embs = embeddings[list(ref_indices)]
            visual_sims = sub_embs @ ref_embs.T

      for i, sub_i in enumerate(sub_indices):
            for j, ref_j in enumerate(ref_indices):
                  score, _ = _pair_score(
                        visual_sims[i, j], field_records[sub_i], field_records[ref_j]
                  )
                  scores[i, j] = score

      return scores, visual_sims


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def match_against_reference(
          detections: Sequence[Detection],
          embeddings: np.ndarray,
          field_records: Sequence[OCRFields],
          reference_image: str,
          threshold: float = ASSIGNMENT_THRESHOLD,
          force_assign: bool = False,
          priority_ndcs: frozenset[str] | None = None,
) -> ReferenceMatch:
      """Assign every subsequent detection to a reference slot, or to out-of-inventory.

    Args:
        detections: All detections across all images, with global instance_ids.
        embeddings: [N, D] L2-normalized embeddings, aligned to detections.
        field_records: OCR fields, aligned to detections.
        reference_image: Path of the image whose detections define the inventory.
            Typically the first image in the batch.
        threshold: Minimum score to count as a valid slot assignment.
        force_assign: When True, the user has certified that every item in the
            subsequent images is contained in the reference inventory. The
            Hungarian solver still picks the optimal pairing, but the score
            threshold is bypassed — every Hungarian-paired detection is assigned
            to its slot regardless of score, with `forced=True` recorded on
            sub-threshold assignments. Detections that *can't* be paired (image
            has more dets than slots) are flagged with `excess_under_certification=True`
            instead of going through the normal OOI path. Use this when shape /
            count priors should override a weak visual+OCR signal.
        priority_ndcs: NDC codes known from an attached Order (components +
            scanned barcodes + expected_components). When a (sub, ref) pair's
            NDCs match AND the NDC is in this set, the pair receives a strong
            score boost so the Hungarian solver prefers it even when visual
            similarity alone is borderline. Requires visual_sim ≥
            VISUAL_FLOOR_FOR_LOT_OVERRIDE to guard against mis-pairings.

    Returns:
        ReferenceMatch with slots, per-detection assignments, and per-image reports.
    """
      n = len(detections)
      assert len(field_records) == n
      assert embeddings.shape[0] == n

      # Group detection indices by source image, preserving original order.
      by_image: dict[str, list[int]] = defaultdict(list)
      image_order: list[str] = []
      for i, det in enumerate(detections):
            if det.source_image not in by_image:
                  image_order.append(det.source_image)
            by_image[det.source_image].append(i)

      if reference_image not in by_image:
            raise ValueError(
                  f"reference_image {reference_image!r} has no detections; "
                  f"known images: {list(by_image)}"
            )

      # 1. Build reference slots from image-1 detections.
      ref_indices = by_image[reference_image]
      slots: list[ReferenceSlot] = []
      for slot_id, det_idx in enumerate(ref_indices):
            det = detections[det_idx]
            f = field_records[det_idx]
            slots.append(
                  ReferenceSlot(
                        slot_id=slot_id,
                        reference_detection_id=det.instance_id,
                        class_label=det.class_label,
                        lot=f.lot,
                        ndc=f.ndc,
                        exp=f.exp,
                        brand=f.brand,
                        strength=f.strength,
                  )
            )

      # 2. Reference image: every detection trivially "assigned" to its own slot.
      assignments: list[Assignment] = []
      image_reports: list[ImageReport] = []

      ref_report = ImageReport(
            source_image=reference_image,
            is_reference=True,
            detection_count=len(ref_indices),
            assigned_count=len(ref_indices),
            matched_slot_ids=list(range(len(slots))),
      )
      for slot_id, det_idx in enumerate(ref_indices):
            assignments.append(
                  Assignment(
                        detection_id=detections[det_idx].instance_id,
                        source_image=reference_image,
                        slot_id=slot_id,
                        score=1.0,  # self-match
                  )
            )
      image_reports.append(ref_report)

      # 3. For each subsequent image, solve the assignment problem.
      for img in image_order:
            if img == reference_image:
                  continue
            sub_indices = by_image[img]
            report = _assign_one_image(
                  img, sub_indices, ref_indices, slots, detections,
                  embeddings, field_records, threshold, assignments,
                  force_assign=force_assign,
                  priority_ndcs=priority_ndcs,
            )
            image_reports.append(report)

      # Restore image_reports to image_order.
      image_reports.sort(key=lambda r: image_order.index(r.source_image))

      return ReferenceMatch(slots=slots, assignments=assignments, image_reports=image_reports)


def _assign_one_image(
          image: str,
          sub_indices: list[int],
          ref_indices: list[int],
          slots: list[ReferenceSlot],
          detections: Sequence[Detection],
          embeddings: np.ndarray,
          field_records: Sequence[OCRFields],
          threshold: float,
          assignments_out: list[Assignment],
          force_assign: bool = False,
          priority_ndcs: frozenset[str] | None = None,
) -> ImageReport:
      """Run Hungarian assignment for one subsequent image. Appends to assignments_out.

    When force_assign is True, every Hungarian-paired detection is assigned to
    its slot regardless of score. Sub-threshold pairings are flagged with
    `forced=True`. Detections that can't be paired at all (n_sub > n_ref) are
    flagged with `excess_under_certification=True` rather than going through
    the normal out-of-inventory path — they represent a contradiction between
    the user's certification and the actual detection count.

    priority_ndcs: when provided, any (sub, ref) pair whose NDCs agree and
    appear in this set gets a strong score boost (threshold + 0.25) if visual
    similarity is at least VISUAL_FLOOR_FOR_LOT_OVERRIDE. This surfaces
    order-level knowledge (expected NDCs, scanned barcodes) into the solver.
    """
      n_sub = len(sub_indices)
      n_ref = len(ref_indices)
      report = ImageReport(
            source_image=image,
            is_reference=False,
            detection_count=n_sub,
      )

      if n_sub == 0:
            report.missing_slot_ids = list(range(n_ref))
            return report

      scores, visual_sims = _build_score_matrix(
            sub_indices, ref_indices, embeddings, field_records
      )

      # Lot-override boost: if a (sub, ref) pair shares a lot number AND has
      # decent visual similarity, lift its score to ensure the assignment
      # picks it even if other channels are missing.
      for i, sub_i in enumerate(sub_indices):
            for j, ref_j in enumerate(ref_indices):
                  f_sub = field_records[sub_i]
                  f_ref = field_records[ref_j]
                  if (
                            f_sub.lot
                            and f_ref.lot
                            and f_sub.lot == f_ref.lot
                            and visual_sims[i, j] >= VISUAL_FLOOR_FOR_LOT_OVERRIDE
                  ):
                        # Force this pair above threshold so the Hungarian solver
                        # treats it as a strong preference. Don't pin to a constant
                        # because we still want differential scoring among multiple
                        # lot-matched pairs (visual sim breaks ties).
                        scores[i, j] = max(scores[i, j], threshold + 0.10 + 0.1 * visual_sims[i, j])

      # Priority NDC boost: when the caller provides expected NDCs from an Order,
      # any (sub, ref) pair whose NDCs agree AND fall in that set receives a
      # stronger boost than the lot-override. Requires minimum visual similarity
      # to avoid pairing visually-different items that happen to share an NDC.
      if priority_ndcs:
            for i, sub_i in enumerate(sub_indices):
                  for j, ref_j in enumerate(ref_indices):
                        f_sub = field_records[sub_i]
                        f_ref = field_records[ref_j]
                        sub_ndc = f_sub.barcode_ndc or f_sub.ndc
                        ref_ndc = f_ref.barcode_ndc or f_ref.ndc
                        if (
                                    sub_ndc and ref_ndc
                                    and sub_ndc == ref_ndc
                                    and sub_ndc in priority_ndcs
                                    and visual_sims[i, j] >= VISUAL_FLOOR_FOR_LOT_OVERRIDE
                        ):
                              scores[i, j] = max(scores[i, j], threshold + 0.25)

      # Hungarian solver minimizes cost; we want to maximize score.
      # scipy's linear_sum_assignment handles rectangular matrices and returns
      # min(n_sub, n_ref) pairs.
      cost = -scores  # negate to convert max-score to min-cost
      row_ind, col_ind = linear_sum_assignment(cost)

      matched_slots: set[int] = set()
      matched_subs: set[int] = set()

      for r, c in zip(row_ind, col_ind):
            score = scores[r, c]
            sub_global_idx = sub_indices[r]
            slot_id = c  # ref_indices index == slot_id by construction
            # In normal mode, only assign if the score clears the threshold.
            # In force_assign mode, the user has certified the subset is in the
            # reference inventory, so we trust the Hungarian solver's optimal
            # pairing even when scores are weak — shape, position, count, and
            # visual cues that didn't produce a confident OCR-backed match are
            # still better than nothing under that certification.
            if score >= threshold or force_assign:
                  assignments_out.append(
                        Assignment(
                              detection_id=detections[sub_global_idx].instance_id,
                              source_image=image,
                              slot_id=int(slot_id),
                              score=float(score),
                              forced=force_assign and score < threshold,
                        )
                  )
                  matched_slots.add(int(slot_id))
                  matched_subs.add(r)
                  report.assigned_count += 1
                  report.matched_slot_ids.append(int(slot_id))
                  if force_assign and score < threshold:
                        report.forced_assignment_count += 1

      # Any sub detection not in matched_subs is either out-of-inventory (normal
      # mode) or excess-under-certification (force_assign mode — image had more
      # detections than reference slots, so the Hungarian solver couldn't pair
      # them and certification can't conjure extra inventory slots out of thin air).
      for r, sub_global_idx in enumerate(sub_indices):
            if r in matched_subs:
                  continue
            # Best score this detection achieved against any slot, even if below threshold.
            best_score = float(scores[r].max()) if scores.shape[1] > 0 else 0.0
            is_excess = force_assign  # under certification, the only way to land
            # here is n_sub > n_ref (Hungarian couldn't pair)
            assignments_out.append(
                  Assignment(
                        detection_id=detections[sub_global_idx].instance_id,
                        source_image=image,
                        slot_id=None,
                        score=best_score,
                        excess_under_certification=is_excess,
                  )
            )
            if is_excess:
                  report.excess_count += 1
            else:
                  report.out_of_inventory_count += 1

      # Any slot not in matched_slots is missing from this image.
      report.missing_slot_ids = [s for s in range(n_ref) if s not in matched_slots]
      return report


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------


def summarize(match: ReferenceMatch) -> dict:
      """Quick stats useful for human-readable reports or JSON output."""
      n_slots = len(match.slots)
      out: dict = {
            "n_reference_slots": n_slots,
            "per_image": [],
      }
      # Group slots by their describe() string to surface duplicate inventory items.
      desc_counts = Counter(s.describe() for s in match.slots)
      out["inventory_grouped"] = [
            {"description": desc, "count": cnt} for desc, cnt in desc_counts.most_common()
      ]
      for report in match.image_reports:
            entry = {
                  "image": report.source_image,
                  "is_reference": report.is_reference,
                  "detections": report.detection_count,
                  "assigned": report.assigned_count,
                  "out_of_inventory": report.out_of_inventory_count,
                  "missing_slot_count": len(report.missing_slot_ids),
                  "forced_assignments": report.forced_assignment_count,
                  "excess_under_certification": report.excess_count,
            }
            if report.missing_slot_ids and not report.is_reference:
                  entry["missing_slots"] = [
                        {
                              "slot_id": sid,
                              "description": match.slots[sid].describe(),
                        }
                        for sid in report.missing_slot_ids
                  ]
            out["per_image"].append(entry)
      return out