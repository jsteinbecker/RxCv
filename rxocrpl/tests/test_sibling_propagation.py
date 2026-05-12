"""Unit tests for _propagate_ocr_to_siblings.

Exercises the case that triggered this fix: 3 identical vials in one image,
detected as 2 bottles + 1 vial, where only the vial gets a successful OCR read.
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
from rxocrpl.ocr.ocr_fields import OCRFields
from rxocrpl.pipeline import _propagate_ocr_to_siblings


IMG_A = "image_a.png"
IMG_B = "image_b.png"


def _make_detection(
    instance_id: int,
    source_image: str,
    class_label: str,
    bbox: tuple[int, int, int, int],
    mask_area: int,
) -> Detection:
    mask = np.zeros((100, 100), dtype=bool)
    return Detection(
        instance_id=instance_id,
        source_image=source_image,
        class_label=class_label,
        score=0.6,
        bbox=bbox,
        mask=mask,
        crop=PILImage.new("RGB", (10, 10)),
    )


def _make_fields(ndc=None, lot=None, exp=None,
                 ndc_conf=0.0, lot_conf=0.0, exp_conf=0.0) -> OCRFields:
    return OCRFields(
        ndc=ndc, lot=lot, exp=exp,
        ndc_confidence=ndc_conf,
        lot_confidence=lot_conf,
        exp_confidence=exp_conf,
    )


def test_propagates_ndc_and_lot_to_blank_siblings() -> None:
    """The exact failure from the report: 3 identical vials, one gets OCR."""
    # Three identical containers (same aspect ratio, same area ~155k px)
    dets = [
        _make_detection(12, IMG_A, "bottle", (232, 11, 533, 615), 156209),
        _make_detection(13, IMG_A, "bottle", (875, 12, 1175, 615), 154085),
        _make_detection(14, IMG_A, "vial",   (556, 14,  855, 614), 155594),
    ]
    fields = [
        _make_fields(),                                      # instance 12 — blank
        _make_fields(),                                      # instance 13 — blank
        _make_fields(ndc="0009-0224-20", lot="ZVLJ3",       # instance 14 — has data
                     ndc_conf=0.70, lot_conf=0.95),
    ]

    _propagate_ocr_to_siblings(dets, fields)

    # All three should now have the same NDC and lot.
    for i, f in enumerate(fields):
        assert f.ndc == "0009-0224-20", f"instance {dets[i].instance_id} missing ndc"
        assert f.lot == "ZVLJ3",        f"instance {dets[i].instance_id} missing lot"

    # Source detection confidence unchanged; propagated ones are 90%.
    assert fields[2].ndc_confidence == 0.70    # source unchanged
    assert fields[0].ndc_confidence == pytest_approx(0.70 * 0.90)
    assert fields[1].ndc_confidence == pytest_approx(0.70 * 0.90)
    print("[OK] propagates ndc+lot to blank siblings")


def test_does_not_overwrite_existing_values() -> None:
    """Existing non-None values must be left intact."""
    dets = [
        _make_detection(0, IMG_A, "vial", (0, 0, 100, 300), 30000),
        _make_detection(1, IMG_A, "vial", (110, 0, 210, 300), 30000),
    ]
    fields = [
        _make_fields(ndc="0009-0224-20", ndc_conf=0.70),
        _make_fields(ndc="0264-7800-10", ndc_conf=0.80),  # different NDC already set
    ]

    _propagate_ocr_to_siblings(dets, fields)

    # Neither should overwrite the other.
    assert fields[0].ndc == "0009-0224-20"
    assert fields[1].ndc == "0264-7800-10"
    print("[OK] does not overwrite existing values")


def test_no_propagation_across_images() -> None:
    """A successful OCR from image A must not bleed into image B."""
    dets = [
        _make_detection(0, IMG_A, "vial", (0, 0, 100, 300), 30000),
        _make_detection(1, IMG_B, "vial", (0, 0, 100, 300), 30000),  # same shape, different image
    ]
    fields = [
        _make_fields(ndc="0009-0224-20", ndc_conf=0.70),
        _make_fields(),
    ]

    _propagate_ocr_to_siblings(dets, fields)

    assert fields[1].ndc is None, "cross-image propagation must not happen"
    print("[OK] no cross-image propagation")


def test_dissimilar_sizes_not_grouped() -> None:
    """A small vial and a large iv bag are not grouped; no propagation."""
    dets = [
        _make_detection(0, IMG_A, "vial",   (0, 0, 100, 300),  30000),   # small
        _make_detection(1, IMG_A, "iv bag", (0, 0, 800, 600), 480000),   # 16× larger
    ]
    fields = [
        _make_fields(ndc="0009-0224-20", ndc_conf=0.70),
        _make_fields(),
    ]

    _propagate_ocr_to_siblings(dets, fields)

    assert fields[1].ndc is None, "dissimilar sizes should not be grouped"
    print("[OK] dissimilar sizes not grouped")


def test_all_blank_no_crash() -> None:
    """If no sibling has any OCR data, nothing should happen (no crash)."""
    dets = [
        _make_detection(0, IMG_A, "vial", (0, 0, 100, 300), 30000),
        _make_detection(1, IMG_A, "vial", (110, 0, 210, 300), 30000),
    ]
    fields = [_make_fields(), _make_fields()]

    _propagate_ocr_to_siblings(dets, fields)  # must not raise

    assert fields[0].ndc is None
    assert fields[1].ndc is None
    print("[OK] all-blank siblings: no crash")


# ---------------------------------------------------------------------------
# Simple approx helper (avoids pytest dependency for float comparison)
# ---------------------------------------------------------------------------
class pytest_approx:
    def __init__(self, expected: float, rel: float = 1e-3):
        self.expected = expected
        self.rel = rel

    def __eq__(self, actual: float) -> bool:  # type: ignore[override]
        return abs(actual - self.expected) <= self.rel * abs(self.expected)

    def __repr__(self) -> str:
        return f"~{self.expected}"


if __name__ == "__main__":
    test_propagates_ndc_and_lot_to_blank_siblings()
    test_does_not_overwrite_existing_values()
    test_no_propagation_across_images()
    test_dissimilar_sizes_not_grouped()
    test_all_blank_no_crash()
    print("\nAll sibling-propagation tests passed.")
