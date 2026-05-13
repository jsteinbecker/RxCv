"""
Marker-based watershed drop-in replacement for segment_components().

Approach:
  1. Build a foreground mask (background-subtraction + strong-edge union),
     identical to the strategy in segment.py so both modules stay comparable.
  2. Distance-transform the mask.  Local maxima of the transform are the
     farthest-from-background pixels inside each object — reliable single-
     object interior seeds even when objects are touching.
  3. Label those seeds as watershed markers (one integer per seed cluster).
  4. Run cv2.watershed to grow each marker outward until regions meet at
     boundaries, cleanly splitting touching objects.
  5. Extract a bounding box for every watershed region, filter by size, and
     suppress overlapping boxes with the same NMS logic used in segment.py.

Returns (boxes, foreground_mask) — same type contract as segment.py.
"""

from __future__ import annotations

import cv2
import numpy as np
from scipy.ndimage import maximum_filter

# ---- tuning ---------------------------------------------------------------
MIN_AREA_FRAC  = 0.004   # min component as fraction of image area
MAX_AREA_FRAC  = 0.75    # drop if a single region swamps the frame
MIN_DIM        = 40      # min width/height in pixels
PAD            = 10
IOU_SUPPRESS   = 0.30    # drop overlapping boxes above this IoU
CONTAIN_FRAC   = 0.85
BORDER         = 20
DT_PEAK_FRAC   = 0.66    # seed threshold: fraction of the DT global max
LOCAL_MAX_SIZE = 1      # neighbourhood (pixels) for local-max search
# ---------------------------------------------------------------------------


def _foreground_mask(img: np.ndarray) -> np.ndarray:
    """Background-subtraction + edge-union foreground mask (same as segment.py)."""
    ring = np.concatenate([
        img[:BORDER, :].reshape(-1, 3),
        img[-BORDER:, :].reshape(-1, 3),
        img[:, :BORDER].reshape(-1, 3),
        img[:, -BORDER:].reshape(-1, 3),
    ], axis=0)
    bg_color = np.median(ring, axis=0).astype(np.float32)
    diff = np.linalg.norm(img.astype(np.float32) - bg_color, axis=2)
    fg_color = (diff > max(20.0, np.percentile(diff, 60))).astype(np.uint8)

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    edges = (cv2.magnitude(gx, gy) > np.percentile(
        cv2.magnitude(gx, gy), 92)).astype(np.uint8)

    mask = ((fg_color | edges) * 255).astype(np.uint8)
    mask[:BORDER, :] = mask[-BORDER:, :] = 0
    mask[:, :BORDER] = mask[:, -BORDER:] = 0

    close_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, close_k, iterations=2)
    open_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, open_k, iterations=1)
    return mask


def _seed_markers(fg_bin: np.ndarray) -> tuple[int, np.ndarray]:
    """
    Distance-transform the fg mask, find local maxima above DT_PEAK_FRAC of
    the global max, then label connected peak clusters as individual markers.

    Returns (n_markers, marker_map) where marker IDs run [1 … n_markers].
    Label 0 means "unknown / to be assigned by watershed."
    """
    dist = cv2.distanceTransform(fg_bin, cv2.DIST_L2, 5)
    if dist.max() == 0:
        return 0, np.zeros(fg_bin.shape, dtype=np.int32)

    norm_dist = dist / dist.max()
    local_max = (maximum_filter(norm_dist, size=LOCAL_MAX_SIZE) == norm_dist)
    peaks = (local_max & (norm_dist >= DT_PEAK_FRAC)).astype(np.uint8)

    n_markers, marker_map = cv2.connectedComponents(peaks)
    return n_markers, marker_map.astype(np.int32)


def _iou(a, b) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    if inter == 0:
        return 0.0
    return inter / float((ax2-ax1)*(ay2-ay1) + (bx2-bx1)*(by2-by1) - inter)


def _contained_fraction(inner, outer) -> float:
    ax1, ay1, ax2, ay2 = inner
    bx1, by1, bx2, by2 = outer
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
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
        for j in range(i + 1, len(indexed)):
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
    return (max(0, x1-PAD), max(0, y1-PAD), min(W, x2+PAD), min(H, y2+PAD))


def segment_components(img: np.ndarray):
    """
    Watershed-based segmentation. Drop-in for pipeline.segment_components().
    Returns (boxes, foreground_mask).
    """
    H, W = img.shape[:2]
    img_area = H * W

    mask = _foreground_mask(img)
    fg_bin = (mask > 0).astype(np.uint8)

    n_markers, marker_map = _seed_markers(fg_bin)
    if n_markers < 2:
        # No seeds found — fall back to connected-components on the fg mask
        n_cc, cc_map, stats, _ = cv2.connectedComponentsWithStats(fg_bin, connectivity=8)
        boxes = []
        for lbl in range(1, n_cc):
            x, y, wb, hb, area = stats[lbl]
            if area < MIN_AREA_FRAC * img_area or area > MAX_AREA_FRAC * img_area:
                continue
            if wb < MIN_DIM or hb < MIN_DIM:
                continue
            boxes.append(_pad(x, y, x+wb, y+hb, W, H))
        return _suppress_overlaps(boxes), fg_bin

    # Markers: seed clusters get IDs 1…n; shift up so background = 1
    marker_map = marker_map + 1   # seed clusters now start at 2

    # Pixels outside sure-foreground but inside sure-background → unknown (0)
    sure_bg_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (21, 21))
    sure_bg = cv2.dilate(fg_bin, sure_bg_k, iterations=3)
    peak_bin = (marker_map > 1).astype(np.uint8)
    unknown = cv2.subtract(sure_bg, peak_bin)
    marker_map[unknown == 1] = 0

    img3 = img if img.ndim == 3 else cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    cv2.watershed(img3.copy(), marker_map)
    # After watershed: 1 = background, -1 = boundaries, ≥2 = object regions

    boxes = []
    for lbl in range(2, n_markers + 1):
        ys, xs = np.where(marker_map == lbl)
        if len(xs) == 0:
            continue
        x1, y1, x2, y2 = int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())
        wb, hb = x2 - x1, y2 - y1
        area = wb * hb
        if area < MIN_AREA_FRAC * img_area or area > MAX_AREA_FRAC * img_area:
            continue
        if wb < MIN_DIM or hb < MIN_DIM:
            continue
        boxes.append(_pad(x1, y1, x2, y2, W, H))

    return _suppress_overlaps(boxes), fg_bin
