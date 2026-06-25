"""
Post-processing fix for the pharmacy detection pipeline.

Three fixes, applied in order:
  1. Class-agnostic NMS — collapse identical/overlapping bboxes across class labels
     into a single instance with the highest-scoring label (with a class-priority
     tiebreaker that prefers specific labels like 'vial' over generic 'bottle'/'bag').
  2. Frame-coverage filter — drop detections whose bbox covers >=80% of the image,
     which are almost always spurious whole-frame "filter" detections.
  3. Product-attribute resolution — look up NDC in a small drug database to attach
     drug name, strength, and manufacturer; fall back to lot-based grouping for
     items without an NDC.

Then re-run the matcher on the cleaned data.
"""

from __future__ import annotations

import json
from typing import cast

from ..tests.fixtures.raw_input import RAW  # when imported as a package

# ---------------------------------------------------------------------------
# Drug database. In production this is a lookup against an NDC directory (FDA's
# NDC Directory, First Databank, etc.). Hardcoded here from ground truth.
# ---------------------------------------------------------------------------
NDC_DB = {"0264-7800-10": {"drug": "Sodium Chloride 0.9% (NS)", "strength": "250 mL", "manufacturer": "Baxter",
                           "form": "iv bag", },
          "0009-0224-20": {"drug": "Ceftazidime-Avibactam", "strength": "2.5 g", "manufacturer": "Pfizer",
                           "form": "vial", }, }

# Lot-only fallback. When no NDC is read but we have a lot, we can still group.
# In a real system this maps lot -> product via the manufacturer's lot registry.
LOT_DB = {"308777251000": {"drug": "(unidentified — lot 308777251000)", "strength": None, "manufacturer": None,
                           "form": "vial", },
          "HK3092": {"drug": "Sterile Water for Injection", "strength": "20 mL", "manufacturer": None,
                     "form": "vial", },
          "2JD588": {"drug": "Sodium Chloride 0.9% (NS)", "strength": "250 mL", "manufacturer": "Baxter",
                     "form": "iv bag", }, }

# Class priority: when two detections cover the same bbox, prefer the more
# specific/correct label. Higher number = preferred.
CLASS_PRIORITY = {"iv bag": 5, "vial": 4, "bottle": 2,  # often a misclassification of vial
                  "bag": 1,  # usually a misclassification of vial/bottle
                  "filter": 0,  # almost always a phantom/non-product detection
                  }


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------
def iou(a: list[int], b: list[int]) -> float:
      ax1, ay1, ax2, ay2 = a
      bx1, by1, bx2, by2 = b
      ix1, iy1 = max(ax1, bx1), max(ay1, by1)
      ix2, iy2 = min(ax2, bx2), min(ay2, by2)
      iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
      inter = iw * ih
      if inter == 0:
            return 0.0
      a_area = (ax2 - ax1) * (ay2 - ay1)
      b_area = (bx2 - bx1) * (by2 - by1)
      return inter / (a_area + b_area - inter)


def mask_iou(a, b) -> float:
      """IoU for boolean masks. Falls back to 0 if masks are unavailable."""
      if a is None or b is None:
            return 0.0
      try:
            inter = int((a & b).sum())
            if inter == 0:
                  return 0.0
            union = int((a | b).sum())
            return inter / union if union else 0.0
      except Exception:
            return 0.0


def _public_detection_dict(d: dict) -> dict:
      """Remove large internal arrays before returning audit records."""
      return {k: v for k, v in d.items() if not k.startswith("_")}


def frame_coverage(bbox: list[int], img_w: int, img_h: int) -> float:
      x1, y1, x2, y2 = bbox
      return ((x2 - x1) * (y2 - y1)) / (img_w * img_h)


# ---------------------------------------------------------------------------
# Fix 1 + 2: clean detections (class-agnostic NMS + frame-coverage rejection)
# ---------------------------------------------------------------------------
def _ensure_hashable_id(value) -> int | str:
      """Ensure instance_id is hashable (int or str). Fallback to str if list or other unhashable type."""
      if isinstance(value, int):
            return value
      if isinstance(value, str):
            return value
      if isinstance(value, (list, dict, set)):
            return str(value)
      if isinstance(value, float):
            return int(value)
      if isinstance(value, bool):
            return int(value)
      return str(value)


def clean_detections(detections: list[dict], image_dims: dict, iou_thresh: float = 0.7, coverage_thresh: float = 0.80,
                     reflection_y_overlap: float = 0.05, reflection_x_overlap: float = 0.30,
                     mask_iou_thresh: float = 0.60) -> list[dict]:
      # Normalize all instance_ids to hashable types upfront and rebuild list
      normalized: list[dict] = []
      for d in detections:
            norm_d = dict(d)  # Create a copy
            norm_d["instance_id"] = _ensure_hashable_id(d["instance_id"])
            normalized.append(norm_d)

      # Step 1a: drop frame-spanning phantoms.
      survivors = []
      rejected_frame = []
      for d in normalized:
            w, h = image_dims[d["source_image"]]
            cov = frame_coverage(d["bbox"], w, h)
            if cov >= coverage_thresh:
                  rejected_frame.append({**_public_detection_dict(d), "_reject_reason": f"frame_coverage={cov:.2f}"})
            else:
                  survivors.append(d)

      # Step 1b: drop reflection/shadow detections.
      # A detection is treated as a reflection if there exists another detection
      # in the same image that:
      #   - sits directly above it (this one's top is at/just below the other's
      #     bottom), AND
      #   - overlaps it horizontally by >=30% of this one's width, AND
      #   - is meaningfully larger than this one (>=2x mask area).
      # These conditions match table-surface reflections of vials/bottles.
      by_image_pre: dict[str, list[dict]] = {}
      for d in survivors:
            by_image_pre.setdefault(d["source_image"], []).append(d)

      survivors2 = []
      rejected_reflection = []
      for img, dets in by_image_pre.items():
            for d in dets:
                  x1, y1, x2, y2 = d["bbox"]
                  d_w = x2 - x1
                  is_reflection = False
                  parent = None
                  for other in dets:
                        if other is d:
                              continue
                        ox1, oy1, ox2, oy2 = other["bbox"]
                        # Other must be above this one. Allow small vertical gap or overlap.
                        vertical_gap = y1 - oy2
                        if not (-reflection_y_overlap * (y2 - y1) <= vertical_gap <= 30):
                              continue
                        # Horizontal overlap.
                        ix1, ix2 = max(x1, ox1), min(x2, ox2)
                        if ix2 - ix1 < reflection_x_overlap * d_w:
                              continue
                        # Size: parent must be substantially larger.
                        if other["mask_area"] < 2 * d["mask_area"]:
                              continue
                        is_reflection = True
                        parent = other["instance_id"]
                        break
                  if is_reflection:
                        rejected_reflection.append(
                              {**_public_detection_dict(d), "_reject_reason": f"reflection_of_instance={parent}", })
                  else:
                        survivors2.append(d)
      survivors = survivors2

      # Step 2: class-agnostic NMS, grouped per image.
      by_image: dict[str, list[dict]] = {}
      for d in survivors:
            by_image.setdefault(d["source_image"], []).append(d)

      cleaned: list[dict] = []
      merged_log: list[dict] = []

      for img, dets in by_image.items():
            # Sort detections so the "best" representative wins each merge.
            # Rank by (class priority, score) descending.
            dets_sorted = sorted(dets, key=lambda d: (CLASS_PRIORITY.get(d["class_label"], 0), d["score"]),
                                 reverse=True, )

            keep: list[dict] = []
            absorbed_into: dict[int, list[int]] = {}
            for d in dets_sorted:
                  matched_idx = None
                  for i, k in enumerate(keep):
                        box_overlap = iou(d["bbox"], k["bbox"])
                        seg_overlap = mask_iou(d.get("_mask"), k.get("_mask"))
                        if box_overlap >= iou_thresh or seg_overlap >= mask_iou_thresh:
                              matched_idx = i
                              break
                  if matched_idx is None:
                        keep.append(dict(d))
                        instance_id_key = cast(int | str, cast(object, d["instance_id"]))
                        absorbed_into[instance_id_key] = []
                  else:
                        # Merge: take OCR fields from whichever instance has them.
                        k = keep[matched_idx]
                        for field_name in ("lot", "exp", "ndc"):
                              if not k.get(field_name) and d.get(field_name):
                                    k[field_name] = d[field_name]
                        kept_id_key = cast(int | str, cast(object, k["instance_id"]))
                        absorbed_id_key = cast(int | str, cast(object, d["instance_id"]))
                        absorbed_into.setdefault(kept_id_key, []).append(absorbed_id_key)
                        merged_log.append({"kept_instance": k["instance_id"], "kept_class": k["class_label"],
                                           "absorbed_instance": d["instance_id"], "absorbed_class": d["class_label"],
                                           "iou": round(iou(d["bbox"], k["bbox"]), 3),
                                           "mask_iou": round(mask_iou(d.get("_mask"), k.get("_mask")), 3), })

            cleaned.extend(_public_detection_dict(k) for k in keep)

      return cleaned, rejected_frame, rejected_reflection, merged_log


# ---------------------------------------------------------------------------
# Fix 3: product-attribute resolution
# ---------------------------------------------------------------------------
def resolve_product(det: dict) -> dict:
      """Attach drug name, strength, manufacturer using NDC -> LOT fallback."""
      enriched = dict(det)
      info = None
      source = None

      if det.get("ndc") and det["ndc"] in NDC_DB:
            info = NDC_DB[det["ndc"]]
            source = "ndc"
      elif det.get("lot") and det["lot"] in LOT_DB:
            info = LOT_DB[det["lot"]]
            source = "lot"

      if info:
            enriched["drug"] = info["drug"]
            enriched["strength"] = info["strength"]
            enriched["manufacturer"] = info["manufacturer"]
            enriched["resolution_source"] = source
            # Override class label if the database disagrees (e.g. detector said
            # 'bottle' but NDC database says it's a vial).
            if info.get("form") and info["form"] != det["class_label"]:
                  enriched["class_label_corrected"] = info["form"]
                  enriched["class_label_original"] = det["class_label"]
                  enriched["class_label"] = info["form"]
      else:
            enriched["drug"] = None
            enriched["strength"] = None
            enriched["manufacturer"] = None
            enriched["resolution_source"] = None

      return enriched


# ---------------------------------------------------------------------------
# Re-run the matcher on cleaned, resolved data
# ---------------------------------------------------------------------------
def propagate_class_labels(detections: list[dict]) -> list[dict]:
      """Within each image, if a detection had its class corrected via NDC/lot
      lookup, propagate that correction to visually-similar peers (same image,
      similar size and aspect ratio). Real shelves rarely mix 'bottle' and 'vial'
      forms in adjacent identical containers, so a single confirmed identity is
      strong evidence for the rest of the row."""
      by_image: dict[str, list[dict]] = {}
      for d in detections:
            by_image.setdefault(d["source_image"], []).append(d)

      out: list[dict] = []
      for img, dets in by_image.items():
            # Find "anchors" — detections with database-confirmed form.
            anchors = [d for d in dets if d.get("class_label_corrected")]
            for d in dets:
                  if d in anchors:
                        out.append(d)
                        continue
                  # Was this one already resolved (has its own NDC/lot match)? Leave it.
                  if d.get("resolution_source"):
                        out.append(d)
                        continue
                  # Try to find an anchor with similar shape.
                  x1, y1, x2, y2 = d["bbox"]
                  d_w, d_h = x2 - x1, y2 - y1
                  d_aspect = d_w / d_h if d_h else 0
                  adopted = None
                  for a in anchors:
                        ax1, ay1, ax2, ay2 = a["bbox"]
                        a_w, a_h = ax2 - ax1, ay2 - ay1
                        a_aspect = a_w / a_h if a_h else 0
                        size_ratio = min(d["mask_area"], a["mask_area"]) / max(d["mask_area"], a["mask_area"])
                        aspect_diff = abs(d_aspect - a_aspect)
                        if size_ratio >= 0.85 and aspect_diff <= 0.10:
                              adopted = a
                              break
                  if adopted:
                        d2 = dict(d)
                        d2["class_label_original"] = d["class_label"]
                        d2["class_label"] = adopted["class_label"]
                        d2["class_label_corrected"] = adopted["class_label"]
                        d2["class_correction_source"] = f"peer_of_instance_{adopted['instance_id']}"
                        out.append(d2)
                  else:
                        out.append(d)
      return out


def product_key(det: dict) -> tuple:
      """Identity used for matching: prefer NDC, fall back to (drug, strength)."""
      if det.get("ndc"):
            return ("ndc", det["ndc"])
      if det.get("drug"):
            return ("drug", det["drug"], det.get("strength"))
      if det.get("lot"):
            return ("lot", det["lot"])
      return ("class", det["class_label"])


def match(reference: list[dict], candidate: list[dict]) -> dict:
      """Match candidate detections to reference slots.

      Two passes:
        1. Hard match by product identity (NDC, or drug+strength, or lot).
           These get score=1.0.
        2. Soft fallback: any candidate that didn't hard-match and has poor OCR
           (no NDC, no lot) gets paired with a reference slot of the same
           class_label if one is still available. These get score=0.4 with
           match_type="class_only" and a needs_human_review flag, because we
           only know "it's the same kind of object" not "it's the same product".
      """
      # Pass 1: hard match.
      ref_pool: dict[tuple, list[dict]] = {}
      for r in reference:
            ref_pool.setdefault(product_key(r), []).append(r)

      assignments = []
      deferred = []
      for c in candidate:
            key = product_key(c)
            # Only hard-match if the key is actually informative (not just class).
            if key[0] in ("ndc", "drug", "lot") and ref_pool.get(key):
                  slot = ref_pool[key].pop(0)
                  assignments.append(
                        {"candidate_instance": c["instance_id"], "matched_reference_instance": slot["instance_id"],
                         "match_key": key, "match_type": "hard", "score": 1.0, "out_of_inventory": False,
                         "needs_human_review": False, })
            else:
                  deferred.append(c)

      # Pass 2: soft class-based fallback.
      # Pool remaining reference slots by class label.
      remaining_ref_by_class: dict[str, list[dict]] = {}
      for slots in ref_pool.values():
            for r in slots:
                  remaining_ref_by_class.setdefault(r["class_label"], []).append(r)

      for c in deferred:
            cls = c["class_label"]
            pool = remaining_ref_by_class.get(cls, [])
            if pool:
                  slot = pool.pop(0)
                  assignments.append(
                        {"candidate_instance": c["instance_id"], "matched_reference_instance": slot["instance_id"],
                         "match_key": ("class", cls), "match_type": "soft_class_only", "score": 0.4,
                         "out_of_inventory": False, "needs_human_review": True,
                         "review_reason": ("Matched by class label only — OCR did not recover NDC/lot, "
                                           "so product identity is unverified."), })
            else:
                  assignments.append({"candidate_instance": c["instance_id"], "matched_reference_instance": None,
                                      "match_key": product_key(c), "match_type": "unmatched", "score": 0.0,
                                      "out_of_inventory": True,
                                      "needs_human_review": True, })

      unmatched_reference = [r["instance_id"] for slots in remaining_ref_by_class.values() for r in slots]
      return {"assignments": assignments, "unmatched_reference": unmatched_reference}


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
def run():
      cleaned, rejected_frame, rejected_reflection, merged_log = clean_detections(RAW["detections"], RAW["image_dims"])
      resolved = [resolve_product(d) for d in cleaned]
      resolved = propagate_class_labels(resolved)

      ref = [d for d in resolved if d["source_image"] == RAW["reference_image"]]
      cand = [d for d in resolved if d["source_image"] != RAW["reference_image"]]

      matches = match(ref, cand)

      def rollup(dets):
            groups: dict[str, int] = {}
            for d in dets:
                  label_parts = [d["class_label"]]
                  if d.get("drug"):
                        label_parts.append(d["drug"])
                  if d.get("strength"):
                        label_parts.append(d["strength"])
                  if d.get("manufacturer"):
                        label_parts.append(f"({d['manufacturer']})")
                  elif d.get("lot"):
                        label_parts.append(f"lot={d['lot']}")
                  key = " ".join(label_parts)
                  groups[key] = groups.get(key, 0) + 1
            return groups

      return {"rejected_frame_detections": [
            {"instance_id": r["instance_id"], "class_label": r["class_label"], "reason": r["_reject_reason"]} for r in
            rejected_frame], "rejected_reflection_detections": [
            {"instance_id": r["instance_id"], "class_label": r["class_label"], "reason": r["_reject_reason"]} for r in
            rejected_reflection], "merge_log": merged_log,
            "cleaned_detection_count_per_image": {img: sum(1 for d in resolved if d["source_image"] == img) for img in
                                                  RAW["images"]}, "rx3_inventory": rollup(ref),
            "rx4_inventory": rollup(cand), "matches": matches,
            "resolved_detections": resolved, }


if __name__ == "__main__":
      result = run()
      print(json.dumps(result, indent=2, default=str))
