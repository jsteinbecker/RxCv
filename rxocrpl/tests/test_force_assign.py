"""Unit tests for the matcher's certified-subset / force-assign mode.

Builds synthetic Detection + OCRFields inputs so the matcher can be exercised
without invoking the heavy CV models (Grounding DINO, SAM 2, DINOv2, EasyOCR).

Run from the bundle root:
    python -m rxocrpl.tests.test_force_assign
"""
from __future__ import annotations

import sys
import pathlib

HERE = pathlib.Path(__file__).resolve()
ROOT = HERE.parent.parent
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

import numpy as np
from PIL import Image as PILImage

from rxocrpl.detection.detection import Detection
from rxocrpl.detection.matching import match_against_reference, ASSIGNMENT_THRESHOLD
from rxocrpl.ocr.ocr_fields import OCRFields


REF_IMG = "ref.png"
SUB_IMG = "sub.png"


def _make_detection(instance_id: int, source_image: str, class_label: str) -> Detection:
    """Build a minimal Detection. mask/crop are not used by the matcher."""
    return Detection(
        instance_id=instance_id,
        source_image=source_image,
        class_label=class_label,
        score=0.9,
        bbox=(0, 0, 10, 10),
        mask=np.ones((10, 10), dtype=bool),
        crop=PILImage.new("RGB", (10, 10), color=(0, 0, 0)),
    )


def _orthogonal_embeddings(n: int, dim: int = 8) -> np.ndarray:
    """Return n L2-normalized rows that are mutually near-orthogonal.

    Using one-hot vectors over `dim` dimensions: visual_sim between distinct
    detections is exactly 0, which makes _pair_score = W_VISUAL*0 = 0 for
    pairs with no OCR agreement. That deliberately puts every score below
    ASSIGNMENT_THRESHOLD so we can verify force_assign actually flips the
    assignment behavior.
    """
    assert n <= dim, "increase dim for more orthogonal rows"
    e = np.zeros((n, dim), dtype=np.float64)
    for i in range(n):
        e[i, i] = 1.0
    return e


def _summarize_assignments(match) -> str:
    out = []
    for r in match.image_reports:
        out.append(
            f"  {r.source_image}: detections={r.detection_count} "
            f"assigned={r.assigned_count} ooi={r.out_of_inventory_count} "
            f"forced={r.forced_assignment_count} "
            f"excess={r.excess_count} "
            f"missing={len(r.missing_slot_ids)}"
        )
    return "\n".join(out)


def test_standard_mode_rejects_weak_matches() -> None:
    """Baseline: with orthogonal embeddings and no OCR agreement, every
    sub detection should land in out-of-inventory in standard mode."""
    detections = [
        _make_detection(0, REF_IMG, "vial"),
        _make_detection(1, REF_IMG, "vial"),
        _make_detection(2, SUB_IMG, "vial"),
        _make_detection(3, SUB_IMG, "vial"),
    ]
    fields = [OCRFields() for _ in range(4)]  # all blank
    embeddings = _orthogonal_embeddings(4)

    match = match_against_reference(
        detections=detections,
        embeddings=embeddings,
        field_records=fields,
        reference_image=REF_IMG,
        force_assign=False,
    )

    sub_report = match.report_for_image(SUB_IMG)
    assert sub_report is not None
    assert sub_report.assigned_count == 0, (
        f"expected 0 assignments under standard mode (threshold {ASSIGNMENT_THRESHOLD}), "
        f"got {sub_report.assigned_count}"
    )
    assert sub_report.out_of_inventory_count == 2
    assert sub_report.excess_count == 0
    assert sub_report.forced_assignment_count == 0
    assert len(sub_report.missing_slot_ids) == 2  # both ref slots unfilled
    print("[OK] standard mode rejects weak matches")
    print(_summarize_assignments(match))


def test_force_assign_fills_slots_despite_weak_scores() -> None:
    """Same inputs as above, but with force_assign=True. The Hungarian solver
    should slot both subs into the two reference positions; both assignments
    should be flagged as forced (since their scores are well below threshold)."""
    detections = [
        _make_detection(0, REF_IMG, "vial"),
        _make_detection(1, REF_IMG, "vial"),
        _make_detection(2, SUB_IMG, "vial"),
        _make_detection(3, SUB_IMG, "vial"),
    ]
    fields = [OCRFields() for _ in range(4)]
    embeddings = _orthogonal_embeddings(4)

    match = match_against_reference(
        detections=detections,
        embeddings=embeddings,
        field_records=fields,
        reference_image=REF_IMG,
        force_assign=True,
    )

    sub_report = match.report_for_image(SUB_IMG)
    assert sub_report is not None
    assert sub_report.assigned_count == 2, (
        f"force_assign should slot both subs; got {sub_report.assigned_count}"
    )
    assert sub_report.out_of_inventory_count == 0
    assert sub_report.excess_count == 0
    assert sub_report.forced_assignment_count == 2, (
        f"both assignments should be flagged forced; got {sub_report.forced_assignment_count}"
    )
    assert len(sub_report.missing_slot_ids) == 0

    # Verify the per-Assignment flag is set on both sub assignments.
    sub_assigns = [a for a in match.assignments if a.source_image == SUB_IMG]
    assert all(a.forced for a in sub_assigns)
    assert all(a.slot_id is not None for a in sub_assigns)
    print("[OK] force_assign fills slots despite weak scores")
    print(_summarize_assignments(match))


def test_excess_under_certification() -> None:
    """If the subsequent image has more detections than the reference has slots,
    force_assign can't help the leftovers — they should be flagged with
    excess_under_certification rather than counted as normal OOI."""
    detections = [
        _make_detection(0, REF_IMG, "vial"),  # 1 ref slot
        _make_detection(1, SUB_IMG, "vial"),
        _make_detection(2, SUB_IMG, "vial"),
        _make_detection(3, SUB_IMG, "vial"),  # 3 subs, only 1 slot
    ]
    fields = [OCRFields() for _ in range(4)]
    embeddings = _orthogonal_embeddings(4)

    match = match_against_reference(
        detections=detections,
        embeddings=embeddings,
        field_records=fields,
        reference_image=REF_IMG,
        force_assign=True,
    )

    sub_report = match.report_for_image(SUB_IMG)
    assert sub_report is not None
    assert sub_report.assigned_count == 1, "Hungarian pairs as many as there are slots"
    assert sub_report.excess_count == 2, (
        f"2 subs should be flagged as excess; got {sub_report.excess_count}"
    )
    assert sub_report.out_of_inventory_count == 0, (
        "under certification, leftovers go to excess, not OOI"
    )

    # Verify the per-Assignment flag.
    excess = [a for a in match.assignments if a.excess_under_certification]
    assert len(excess) == 2
    assert all(a.slot_id is None for a in excess)
    print("[OK] excess_under_certification flagged for over-count")
    print(_summarize_assignments(match))


def test_strong_matches_unchanged_by_force_assign() -> None:
    """When OCR gives a strong signal, force_assign mode should produce the
    same assignment as standard mode — the threshold is bypassed but the
    Hungarian solver still optimizes for the best pairing, and strong matches
    aren't flagged as forced."""
    detections = [
        _make_detection(0, REF_IMG, "vial"),
        _make_detection(1, REF_IMG, "vial"),
        _make_detection(2, SUB_IMG, "vial"),
        _make_detection(3, SUB_IMG, "vial"),
    ]
    fields = [
        OCRFields(lot="ABC123", ndc="0009-0224-20"),  # ref 0
        OCRFields(lot="XYZ789", ndc="0264-7800-10"),  # ref 1
        OCRFields(lot="ABC123", ndc="0009-0224-20"),  # sub matches ref 0
        OCRFields(lot="XYZ789", ndc="0264-7800-10"),  # sub matches ref 1
    ]
    # Use orthogonal embeddings so visual sim is 0; OCR agreement does the work.
    embeddings = _orthogonal_embeddings(4)

    match_std = match_against_reference(
        detections=detections, embeddings=embeddings,
        field_records=fields, reference_image=REF_IMG, force_assign=False,
    )
    match_forced = match_against_reference(
        detections=detections, embeddings=embeddings,
        field_records=fields, reference_image=REF_IMG, force_assign=True,
    )

    std_pairs = {(a.detection_id, a.slot_id)
                 for a in match_std.assignments if a.source_image == SUB_IMG}
    forced_pairs = {(a.detection_id, a.slot_id)
                    for a in match_forced.assignments if a.source_image == SUB_IMG}
    assert std_pairs == forced_pairs, (
        f"strong-match assignments should be identical between modes; "
        f"std={std_pairs} forced={forced_pairs}"
    )

    # And none should be flagged as forced — scores were over threshold.
    forced_count = sum(
        1 for a in match_forced.assignments
        if a.source_image == SUB_IMG and a.forced
    )
    assert forced_count == 0, (
        f"strong matches should not be flagged forced; got {forced_count}"
    )
    print("[OK] strong matches identical between standard and force_assign modes")
    print(_summarize_assignments(match_forced))


if __name__ == "__main__":
    print("=== test_standard_mode_rejects_weak_matches ===")
    test_standard_mode_rejects_weak_matches()
    print()
    print("=== test_force_assign_fills_slots_despite_weak_scores ===")
    test_force_assign_fills_slots_despite_weak_scores()
    print()
    print("=== test_excess_under_certification ===")
    test_excess_under_certification()
    print()
    print("=== test_strong_matches_unchanged_by_force_assign ===")
    test_strong_matches_unchanged_by_force_assign()
    print()
    print("All force-assign tests passed.")
