"""
Drop-in replacement for segment_components().

Key changes vs. the column-projection approach:
  1. Build a foreground mask from background subtraction (table is ~uniform),
     unioned with strong edges. This gives us actual object support, not just
     printed-text edge density.
  2. Use 2D connected components on the mask, not 1D column projection.
     This is what fixes the front/back vial fusion: two objects that overlap
     in x but not in y end up as separate components.
  3. Filter components by size and aspect ratio.
  4. Final NMS-style suppression: drop a box if it's >IOU_THRESHOLD with
     another, prefer the smaller (tighter) box; also drop any box that
     fully contains another smaller box.

Returns the same (boxes, edge_mask) tuple as the original so nothing
downstream needs to change.
"""

from __future__ import annotations

import cv2
import numpy as np

MIN_AREA_FRAC = 0.01  # min component area as fraction of image area
MAX_AREA_FRAC = 0.55  # reject if a single component is most of the frame
MIN_DIM = 40  # min width/height in pixels
PAD = 10  # smaller pad than the original 28
IOU_SUPPRESS = 0.10  # drop overlapping boxes above this IoU
CONTAIN_FRAC = 0.75  # drop a box if >= this fraction of it sits in another
BORDER = 20  # margin to ignore at image edges


def _foreground_mask(img: np.ndarray) -> np.ndarray:
      """
      Build a binary mask of likely-foreground pixels.

      Strategy: the table background is roughly uniform gray. We estimate
      its color as the median of the image border, then mark pixels whose
      color distance from that exceeds a threshold. We also union in strong
      edges, since transparent objects (the saline bag) may be close to
      background color in their interior but have a strong outline.
      """
      H, W = img.shape[:2]

      # Background color estimate from a thin border ring
      ring = np.concatenate([
            img[:BORDER, :].reshape(-1, 3),
            img[-BORDER:, :].reshape(-1, 3),
            img[:, :BORDER].reshape(-1, 3),
            img[:, -BORDER:].reshape(-1, 3),
      ], axis=0)
      bg_color = np.median(ring, axis=0).astype(np.float32)

      # Per-pixel distance from background color
      diff = np.linalg.norm(img.astype(np.float32) - bg_color, axis=2)
      # Adaptive threshold: anything notably different from background
      fg_color = (diff > max(20.0, np.percentile(diff, 60))).astype(np.uint8)

      # Edge support — picks up transparent-object outlines and printed text
      gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
      gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
      gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
      mag = cv2.magnitude(gx, gy)
      edges = (mag > np.percentile(mag, 92)).astype(np.uint8)

      mask = ((fg_color | edges) * 255).astype(np.uint8)

      # Zero out a border ring so edge artifacts don't seed components
      mask[:BORDER, :] = 0
      mask[-BORDER:, :] = 0
      mask[:, :BORDER] = 0
      mask[:, -BORDER:] = 0

      # Close gaps inside transparent objects, then open to drop speckle
      close_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
      mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, close_k, iterations=2)
      open_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
      mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, open_k, iterations=1)

      return mask


def _iou(a, b) -> float:
      ax1, ay1, ax2, ay2 = a
      bx1, by1, bx2, by2 = b
      ix1, iy1 = max(ax1, bx1), max(ay1, by1)
      ix2, iy2 = min(ax2, bx2), min(ay2, by2)
      iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
      inter = iw * ih
      if inter == 0:
            return 0.0
      area_a = (ax2 - ax1) * (ay2 - ay1)
      area_b = (bx2 - bx1) * (by2 - by1)
      return inter / float(area_a + area_b - inter)


def _contained_fraction(inner, outer) -> float:
      """Fraction of `inner`'s area that lies inside `outer`."""
      ax1, ay1, ax2, ay2 = inner
      bx1, by1, bx2, by2 = outer
      ix1, iy1 = max(ax1, bx1), max(ay1, by1)
      ix2, iy2 = min(ax2, bx2), min(ay2, by2)
      iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
      inter = iw * ih
      inner_area = max(1, (ax2 - ax1) * (ay2 - ay1))
      return inter / float(inner_area)


def _suppress_overlaps(boxes: list[tuple[int, int, int, int]]
                       ) -> list[tuple[int, int, int, int]]:
      """
      Drop boxes that substantially overlap another. Prefer the smaller
      (tighter) box when two are nearly the same — this is the opposite of
      standard NMS, and it's what we want here because the failure mode is
      a giant box swallowing two real objects.
      """
      if not boxes:
            return []

      # Sort by area ascending so smaller boxes get priority
      indexed = sorted(enumerate(boxes),
                       key=lambda ib: (ib[1][2] - ib[1][0]) * (ib[1][3] - ib[1][1]))

      keep_flags = [True] * len(indexed)
      for i in range(len(indexed)):
            if not keep_flags[i]:
                  continue
            _, box_i = indexed[i]
            area_i = (box_i[2] - box_i[0]) * (box_i[3] - box_i[1])
            for j in range(i + 1, len(indexed)):
                  if not keep_flags[j]:
                        continue
                  _, box_j = indexed[j]
                  area_j = (box_j[2] - box_j[0]) * (box_j[3] - box_j[1])

                  iou = _iou(box_i, box_j)
                  # If j is much larger and largely covers i, drop j (the big swallower)
                  cf_i_in_j = _contained_fraction(box_i, box_j)
                  if area_j > 1.5 * area_i and cf_i_in_j >= CONTAIN_FRAC:
                        keep_flags[j] = False
                        continue
                  # Otherwise standard high-IoU suppression: drop the larger
                  if iou >= IOU_SUPPRESS:
                        keep_flags[j] = False

      kept = [box for (_, box), keep in zip(indexed, keep_flags) if keep]
      # Restore left-to-right ordering for downstream stability
      kept.sort(key=lambda b: b[0])
      return kept


def _pad_box(x1, y1, x2, y2, pad, w, h):
      return (
            max(0, x1 - pad),
            max(0, y1 - pad),
            min(w, x2 + pad),
            min(h, y2 + pad),
      )


def segment_components(img: np.ndarray):
      """
      Drop-in replacement. Returns (boxes, edge_mask) — `edge_mask` here is
      actually the foreground mask, but the caller only writes it to disk
      for debugging, so the rename is harmless.
      """
      H, W = img.shape[:2]
      img_area = H * W

      mask = _foreground_mask(img)

      # 2D connected components on the foreground mask
      n_labels, labels, stats, _ = cv2.connectedComponentsWithStats(
            (mask > 0).astype(np.uint8), connectivity=8
      )

      boxes = []
      for lbl in range(1, n_labels):  # 0 is background
            x, y, w_box, h_box, area = stats[lbl]

            if area < MIN_AREA_FRAC * img_area:
                  continue
            if area > MAX_AREA_FRAC * img_area:
                  # A single component covering most of the frame means our
                  # background subtraction failed to separate things — skip it
                  # rather than emit a frame-sized box.
                  continue
            if w_box < MIN_DIM or h_box < MIN_DIM:
                  continue

            x1, y1 = x, y
            x2, y2 = x + w_box, y + h_box
            boxes.append(_pad_box(x1, y1, x2, y2, PAD, W, H))

      boxes = _suppress_overlaps(boxes)

      # The downstream code calls cv2.imwrite on edge_mask*255, so return
      # something with the right dtype and value range.
      return boxes, (mask > 0).astype(np.uint8)
