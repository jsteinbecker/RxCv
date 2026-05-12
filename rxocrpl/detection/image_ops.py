"""Image normalization helpers for detection crops."""
from __future__ import annotations

import hashlib
from typing import Tuple

import numpy as np
from PIL import Image, ImageOps


def image_fingerprint(image: Image.Image, size: tuple[int, int] = (96, 96)) -> str:
    """Stable perceptual-ish hash for caching expensive crop operations.

    This is intentionally dependency-free: normalize orientation, downsample,
    grayscale, and hash bytes. It is not meant for cryptographic identity; it is
    meant to catch exact or near-identical repeated product crops in a run.
    """
    img = ImageOps.exif_transpose(image).convert("L").resize(size, Image.Resampling.BILINEAR)
    return hashlib.blake2b(img.tobytes(), digest_size=16).hexdigest()


def canonicalize_crop(
    source_image: Image.Image,
    bbox: tuple[int, int, int, int],
    mask: np.ndarray | None = None,
    *,
    pad_fraction: float = 0.06,
    rotate: bool = True,
) -> Image.Image:
    """Return a mask-aware, approximately upright crop for OCR/embedding.

    Uses the instance mask's principal axis to rotate elongated objects into a
    stable orientation. Falls back to a padded bbox crop when the mask is absent
    or too small. This improves cross-view matching and OCR on horizontal vials.
    """
    image = ImageOps.exif_transpose(source_image).convert("RGB")
    W, H = image.size
    x1, y1, x2, y2 = bbox
    bw, bh = max(1, x2 - x1), max(1, y2 - y1)
    pad = int(round(max(bw, bh) * pad_fraction))
    x1p, y1p = max(0, x1 - pad), max(0, y1 - pad)
    x2p, y2p = min(W, x2 + pad), min(H, y2 + pad)
    crop = image.crop((x1p, y1p, x2p, y2p))

    if not rotate or mask is None or mask.shape[0] != H or mask.shape[1] != W:
        return crop

    ys, xs = np.nonzero(mask[y1p:y2p, x1p:x2p])
    if len(xs) < 25:
        return crop

    coords = np.column_stack([xs, ys]).astype(np.float32)
    coords -= coords.mean(axis=0, keepdims=True)
    cov = np.cov(coords, rowvar=False)
    vals, vecs = np.linalg.eigh(cov)
    axis = vecs[:, int(np.argmax(vals))]
    angle = float(np.degrees(np.arctan2(axis[1], axis[0])))

    # Rotate elongated objects to horizontal; leave near-square objects alone.
    elongation = float(np.sqrt(max(vals) / max(min(vals), 1e-6)))
    if elongation < 1.25:
        return crop

    rotated = crop.rotate(-angle, expand=True, resample=Image.Resampling.BICUBIC)
    return rotated
