"""Test the variant-pool / ensemble-voting path of extract_fields.

Simulates what FieldExtractor.extract() produces when running EasyOCR over
multiple rotations and preprocessings of the same crop. Each pool stands in
for one (rotation, preprocessing) variant.

Run from the bundle root:
    python -m pharmacy_pipeline.tests.test_variant_pools
"""
from __future__ import annotations
import sys, pathlib

HERE = pathlib.Path(__file__).resolve()
ROOT = HERE.parent.parent
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from rxocrpl.ocr.ocr_fields import extract_fields


def show(name, pools):
    flat = [ln for p in pools for ln in p]
    fields, _ = extract_fields(flat, variant_pools=pools, auto_load_directory=False)
    print(f"--- {name} ---")
    for i, p in enumerate(pools):
        print(f"  pool {i}: {p}")
    populated = {k: getattr(fields, k) for k in fields._FIELD_NAMES
                 if getattr(fields, k) is not None}
    confidences = {k: getattr(fields, f"{k}_confidence") for k in populated}
    print(f"  -> {populated}")
    print(f"  conf: {confidences}")
    flagged = [k for k in populated if confidences[k] < 0.4]
    if flagged:
        print(f"  needs review: {flagged}")
    print()


if __name__ == "__main__":
    # All 4 variants agree perfectly. Confidence should be near-max
    # (regex_prior 0.95 × agreement 1.0 = 0.95).
    show("Unanimous agreement across 4 variants", [
        ["LOT 2JD588", "EXP 07/27", "NDC 0264-7800-10"],
        ["LOT 2JD588", "EXP 07/27", "NDC 0264-7800-10"],
        ["LOT 2JD588", "EXP 07/27", "NDC 0264-7800-10"],
        ["LOT 2JD588", "EXP 07/27", "NDC 0264-7800-10"],
    ])

    # 3/4 agree on lot, 1 variant misread it. Lot confidence drops to ~0.71
    # (0.95 × 0.75). Still above review threshold (0.4).
    show("Majority agreement (3/4) on lot", [
        ["LOT 2JD588", "EXP 07/27"],
        ["LOT 2JD588", "EXP 07/27"],
        ["LOT 2JD588", "EXP 07/27"],
        ["LOT 2JD5B8", "EXP 07/27"],   # B for 8: classic OCR confusion
    ])

    # 2 variants saw the lot, 2 saw nothing (dropped a side). Confidence
    # halves: 0.95 × 0.5 = 0.475.
    show("Half the variants missed the lot side", [
        ["LOT 2JD588", "EXP 07/27"],
        ["LOT 2JD588", "EXP 07/27"],
        ["EXP 07/27"],
        ["EXP 07/27"],
    ])

    # Only one variant got it. Confidence collapses to 0.95 × 0.25 = 0.24
    # → below review threshold.
    show("Lone variant — should flag for review", [
        ["LOT 2JD588", "EXP 07/27"],
        ["EXP 07/27"],
        ["EXP 07/27"],
        ["EXP 07/27"],
    ])

    # Two variants tied on different values. Voter picks one but agreement
    # is 0.5; review flag fires for the loser case too.
    show("Disagreement: two variants vs two variants", [
        ["LOT 2JD588"],
        ["LOT 2JD588"],
        ["LOT 2JD5B8"],
        ["LOT 2JD5B8"],
    ])

    # Adjacent-fragment join: EasyOCR often splits 'LOT' from the value.
    show("Adjacent-fragment recovery (LOT and value split)", [
        ["LOT", "2JD588", "EXP 07/27"],
        ["LOT", "2JD588", "EXP 07/27"],
        ["LOT", "2JD588", "EXP 07/27"],
        ["LOT", "2JD588", "EXP 07/27"],
    ])
