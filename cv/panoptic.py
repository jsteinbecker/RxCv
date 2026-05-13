"""
Panoptic-style drop-in replacement for segment_components().

Inspired by panoptic segmentation: a semantic pass classifies every pixel as
"background stuff" or "foreground things," then an instance pass separates
individual objects within the things class.

Both passes are pure OpenCV — no deep-learning backend required.

Stage 1 — Semantic (stuff vs. things):
  K-means clusters the image by HSV colour.  Each cluster is labelled
  "background" if it accounts for a disproportionate share of the border-ring
  pixels (the table/surface), otherwise "foreground."  Foreground cluster
  pixels form the "things" mask.  The mask is then morphologically cleaned to
  fill holes inside opaque objects and remove speckle.

Stage 2 — Instance (individual object separation within things):
  Connected components on the things mask yield coarse instances.  Any
  component large enough to plausibly contain two objects is re-split via
  distance-transform watershed — the same technique used in watershed.py —
  so touching vials or overlapping labels are correctly separated.

Returns (boxes, panoptic_mask) where panoptic_mask encodes both passes:
  pixel value 0          → background
  pixel value 255 / 127  → things boundary / interior  (for debug rendering)
Same type contract as segment.py and watershed.py.
"""

from __future__ import annotations

import cv2
import numpy as np
from scipy.ndimage import maximum_filter

# ---- tuning ---------------------------------------------------------------
K_CLUSTERS      = 6      # colour clusters for semantic stage
BG_BORDER_FRAC  = 0.25   # a cluster is "background" if this fraction of its
                          # pixels sit in the border ring
MIN_AREA_FRAC   = 0.004
MAX_AREA_FRAC   = 0.55
MIN_DIM         = 40
PAD             = 10
IOU_SUPPRESS    = 0.20
CONTAIN_FRAC    = 0.75
BORDER          = 20
SPLIT_RATIO     = 3.5    # re-split a component if its longest axis / expected
                          # object size exceeds this (heuristic for two-pack)
DT_PEAK_FRAC    = 0.40
LOCAL_MAX_SIZE  = 30
# ---------------------------------------------------------------------------


# ============================================================
# Stage 1: semantic segmentation via colour clustering
# ============================================================

def _semantic_mask(img: np.ndarray) -> np.ndarray:
    """
    Return a binary "things" mask via K-means colour clustering.

    Pixels whose cluster is dominated by border-ring pixels are labelled
    background; all others are labelled foreground (things).
    """
    H, W = img.shape[:2]

    # Smooth before clustering to reduce texture noise
    smooth = cv2.bilateralFilter(img, d=9, sigmaColor=75, sigmaSpace=75)
    hsv = cv2.cvtColor(smooth, cv2.COLOR_BGR2HSV).astype(np.float32)

    pixels = hsv.reshape(-1, 3)
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 1.0)
    _, labels, _ = cv2.kmeans(
        pixels, K_CLUSTERS, None, criteria,
        attempts=3, flags=cv2.KMEANS_PP_CENTERS,
    )
    label_map = labels.reshape(H, W).astype(np.int32)

    # Build border-ring pixel set
    border_mask = np.zeros((H, W), dtype=bool)
    border_mask[:BORDER, :] = True
    border_mask[-BORDER:, :] = True
    border_mask[:, :BORDER] = True
    border_mask[:, -BORDER:] = True

    border_counts = np.bincount(label_map[border_mask], minlength=K_CLUSTERS)
    total_counts  = np.bincount(label_map.ravel(), minlength=K_CLUSTERS)

    bg_labels = set()
    for k in range(K_CLUSTERS):
        if total_counts[k] == 0:
            continue
        if border_counts[k] / total_counts[k] >= BG_BORDER_FRAC:
            bg_labels.add(k)

    # Build things mask: pixels NOT in any background cluster
    things = np.ones((H, W), dtype=np.uint8)
    for k in bg_labels:
        things[label_map == k] = 0

    # Zero out border ring to suppress edge artefacts
    things[:BORDER, :] = things[-BORDER:, :] = 0
    things[:, :BORDER] = things[:, -BORDER:] = 0

    # Morphological clean-up: close holes, open speckle
    close_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (13, 13))
    things = cv2.morphologyEx(things, cv2.MORPH_CLOSE, close_k, iterations=2)
    open_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    things = cv2.morphologyEx(things, cv2.MORPH_OPEN, open_k, iterations=1)

    return things


# ============================================================
# Stage 2: instance separation via watershed
# ============================================================

def _watershed_split(component_mask: np.ndarray) -> list[np.ndarray]:
    """
    Run distance-transform watershed on a single binary component mask.
    Returns a list of per-instance binary masks.
    """
    dist = cv2.distanceTransform(component_mask, cv2.DIST_L2, 5)
    if dist.max() == 0:
        return [component_mask]

    norm = dist / dist.max()
    local_max = (maximum_filter(norm, size=LOCAL_MAX_SIZE) == norm)
    peaks = (local_max & (norm >= DT_PEAK_FRAC)).astype(np.uint8)

    n_seeds, seed_map = cv2.connectedComponents(peaks)
    if n_seeds <= 1:
        return [component_mask]

    markers = seed_map.astype(np.int32) + 1  # seeds get IDs ≥2; bg=1
    sure_bg_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (21, 21))
    sure_bg = cv2.dilate(component_mask, sure_bg_k, iterations=2)
    unknown = cv2.subtract(sure_bg, peaks)
    markers[unknown == 1] = 0

    # Watershed needs a 3-channel image; we synthesise one from the mask
    vis = cv2.cvtColor(component_mask * 255, cv2.COLOR_GRAY2BGR)
    cv2.watershed(vis, markers)

    instances = []
    for lbl in range(2, n_seeds + 1):
        m = (markers == lbl).astype(np.uint8)
        if m.any():
            instances.append(m)
    return instances if instances else [component_mask]


def _maybe_split(component_mask: np.ndarray, img_area: int) -> list[np.ndarray]:
    """
    Decide whether a connected component should be split.  Returns a list of
    sub-masks (length 1 means no split).
    """
    ys, xs = np.where(component_mask)
    if len(xs) == 0:
        return []
    w = int(xs.max() - xs.min() + 1)
    h = int(ys.max() - ys.min() + 1)
    longest = max(w, h)

    # Rough expected single-object size: sqrt(component area / pi) * 2
    area = int(component_mask.sum())
    expected_dim = 2 * np.sqrt(area / np.pi)

    if longest > SPLIT_RATIO * expected_dim:
        return _watershed_split(component_mask)
    return [component_mask]


# ============================================================
# Shared helpers (NMS, padding)
# ============================================================

def _iou(a, b) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    inter = max(0, ix2-ix1) * max(0, iy2-iy1)
    if inter == 0:
        return 0.0
    return inter / float((ax2-ax1)*(ay2-ay1) + (bx2-bx1)*(by2-by1) - inter)


def _contained_fraction(inner, outer) -> float:
    ax1, ay1, ax2, ay2 = inner
    bx1, by1, bx2, by2 = outer
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    inter = max(0, ix2-ix1) * max(0, iy2-iy1)
    return inter / max(1, (ax2-ax1)*(ay2-ay1))


def _suppress_overlaps(boxes: list) -> list:
    if not boxes:
        return []
    indexed = sorted(enumerate(boxes),
                     key=lambda ib: (ib[1][2]-ib[1][0]) * (ib[1][3]-ib[1][1]))
    keep = [True] * len(indexed)
    for i in range(len(indexed)):
        if not keep[i]:
            continue
        _, bi = indexed[i]
        ai = (bi[2]-bi[0]) * (bi[3]-bi[1])
        for j in range(i+1, len(indexed)):
            if not keep[j]:
                continue
            _, bj = indexed[j]
            aj = (bj[2]-bj[0]) * (bj[3]-bj[1])
            if aj > 1.5 * ai and _contained_fraction(bi, bj) >= CONTAIN_FRAC:
                keep[j] = False
            elif _iou(bi, bj) >= IOU_SUPPRESS:
                keep[j] = False
    result = [box for (_, box), k in zip(indexed, keep) if k]
    result.sort(key=lambda b: b[0])
    return result


def _pad(x1, y1, x2, y2, W, H):
    return max(0, x1 - PAD), max(0, y1 - PAD), min(W, x2 + PAD), min(H, y2 + PAD)


# ============================================================
# Public entry point
# ============================================================

def segment_components(img: np.ndarray):
    """
    Panoptic-style segmentation. Drop-in for pipeline.segment_components().
    Returns (boxes, panoptic_mask).
    """
    H, W = img.shape[:2]
    img_area = H * W

    # --- Stage 1: semantic things mask ---
    things_mask = _semantic_mask(img)

    # --- Stage 2: connected components → optional instance splitting ---
    n_cc, cc_map, stats, _ = cv2.connectedComponentsWithStats(
        things_mask, connectivity=8
    )

    # Accumulate instance masks after splitting; collect boxes
    panoptic_mask = np.zeros((H, W), dtype=np.uint8)
    boxes = []

    for lbl in range(1, n_cc):
        x, y, wb, hb, area = stats[lbl]
        if area < MIN_AREA_FRAC * img_area or area > MAX_AREA_FRAC * img_area:
            continue
        if wb < MIN_DIM or hb < MIN_DIM:
            continue

        component_bin = (cc_map == lbl).astype(np.uint8)
        sub_instances = _maybe_split(component_bin, img_area)

        for inst_mask in sub_instances:
            ys, xs = np.where(inst_mask)
            if len(xs) == 0:
                continue
            x1, y1_i, x2, y2_i = int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())
            wb_i, hb_i = x2 - x1, y2_i - y1_i
            area_i = int(inst_mask.sum())
            if area_i < MIN_AREA_FRAC * img_area or area_i > MAX_AREA_FRAC * img_area:
                continue
            if wb_i < MIN_DIM or hb_i < MIN_DIM:
                continue
            panoptic_mask[inst_mask > 0] = 255
            boxes.append(_pad(x1, y1_i, x2, y2_i, W, H))

    boxes = _suppress_overlaps(boxes)
    return boxes, panoptic_mask
