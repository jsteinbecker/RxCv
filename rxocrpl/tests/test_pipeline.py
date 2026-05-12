"""End-to-end test: run the cleanup pipeline on the raw rx3/rx4 detections
and print the inventory + matching summary.

Run from the bundle root:
    python -m pharmacy_pipeline.tests.test_pipeline
"""
from __future__ import annotations
import sys, pathlib

HERE = pathlib.Path(__file__).resolve()
ROOT = HERE.parent.parent
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from rxocrpl.detection.clean_and_match import run


if __name__ == "__main__":
    r = run()

    print("=== REJECTED (frame-spanning) ===")
    for x in r["rejected_frame_detections"]:
        print(" ", x)

    print("\n=== REJECTED (reflections) ===")
    for x in r["rejected_reflection_detections"]:
        print(" ", x)

    print("\n=== MERGED (NMS) ===")
    for x in r["merge_log"]:
        print(" ", x)

    print("\n=== CLEANED COUNTS ===")
    for k, v in r["cleaned_detection_count_per_image"].items():
        print(f"  {k}: {v}")

    print("\n=== rx3 INVENTORY ===")
    for k, v in r["rx3_inventory"].items():
        print(f"  {v}x  {k}")

    print("\n=== rx4 INVENTORY ===")
    for k, v in r["rx4_inventory"].items():
        print(f"  {v}x  {k}")

    print("\n=== MATCHES ===")
    for a in r["matches"]["assignments"]:
        print(
            f"  cand#{a['candidate_instance']:>2}  ->  ref#{a['matched_reference_instance']}  "
            f"[{a['match_type']}, score={a['score']}, review={a['needs_human_review']}]"
        )
    print(f"  unmatched_reference_slots: {r['matches']['unmatched_reference']}")
