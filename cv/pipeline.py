"""
Computer-vision pipeline: segmentation, OCR, field extraction, and
cross-image component matching.

Run directly to analyze IMAGE_PATHS and produce:
    - per-component crops + segmentation overviews
    - linked_components.json (full pipeline output)

The reporting layer (PDF generation) lives in `report.py` and consumes
the JSON + crops written here.

Manufacturer recognition is driven by the registry parsed from
/ref/mfg-data.txt — see mfg_lookup.py.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections import defaultdict, deque
from dataclasses import dataclass, asdict, field
from pathlib import Path

import cv2
import numpy as np
import pytesseract
from scipy.ndimage import gaussian_filter1d

from linker import link_images
from mfg_lookup import get_registry, ManufacturerRegistry
from panoptic import segment_components as po_segment
from watershed import segment_components as ws_segment

# ============================================================
# CONFIG
# ============================================================

IMAGE_PATHS = [
      "/Users/jts/Documents/ocr-rx1.png",
      "/Users/jts/Documents/ocr-rx2.png",
]

SEGMENTATION_METHODS = ("legacy", "watershed", "panoptic")
DEFAULT_SEGMENTATION_METHOD = "watershed"

OUT_DIR = Path("output") / dt.datetime.now().strftime("%Y-%m-%d_%H%M")
OUT_DIR.mkdir()

MFG_REGISTRY_PATH = Path("ref/mfg-data.txt")

ROTATIONS = [0, 90, 180, 270]
FINE_DESKEW_RANGE = range(-8, 9, 2)

GENERIC_STOPWORDS = {
      "for", "the", "and", "usp", "injection", "single", "dose", "container",
      "contains", "store", "made", "usa", "lot", "exp", "ml", "ndc", "of",
      "to", "is", "on", "in", "with", "use", "only", "each",
}

PRODUCT_PATTERNS = [
      ("0.9% Sodium Chloride Injection, USP",
       r"(0\.9\s*%|sodium\s+chlor[il]de)"),
      ("Thiamine HCl Injection",
       r"th[il]am[il]ne"),
      ("Dextrose 5% Injection, USP",
       r"\bdextrose\b"),
      ("Lactated Ringer's Injection, USP",
       r"lactated\s+ringer"),
      ("Heparin Sodium Injection",
       r"\bhepar[il]n\b"),
      ("Potassium Chloride Injection",
       r"potass[il]um\s+chlor[il]de"),
      ("Magnesium Sulfate Injection",
       r"magnes[il]um\s+sulf"),
]

FONT = cv2.FONT_HERSHEY_SIMPLEX


# ============================================================
# DATA STRUCTURES
# ============================================================

@dataclass
class OcrCandidate:
      text: str
      rotation: int
      deskew: float
      psm: int
      preprocess: str
      score: float
      word_conf_mean: float
      word_count: int


@dataclass
class Component:
      image_index: int
      component_index: int
      bbox_xyxy: tuple
      crop_path: str
      component_type: str

      text: str
      best_rotation: int
      best_deskew: float
      best_psm: int
      best_preprocess: str
      ocr_confidence: float
      tokens: list

      product: str | None
      manufacturer: str | None
      manufacturer_source: str | None  # "ndc" | "name" | None
      ndc: str | None
      lot: str | None
      expiration: str | None
      volume_ml: str | None
      strength: str | None

      aspect_ratio: float
      area: int

      color_hist: np.ndarray | None = field(repr=False, default=None)
      orb_des: np.ndarray | None = field(repr=False, default=None)
      all_ocr_candidates: list = field(default_factory=list, repr=False)


# ============================================================
# UTILITIES
# ============================================================

def normalize_spaces(text: str) -> str:
      text = text.replace("\x0c", "")
      text = re.sub(r"[ \t]+", " ", text)
      text = re.sub(r"\n{2,}", "\n", text)
      return text.strip()


def normalize_idlike(text):
      if not text:
            return None
      return re.sub(r"[^A-Za-z0-9]", "", text).upper()


def rotate_image(img: np.ndarray, angle: int) -> np.ndarray:
      if angle == 0:
            return img
      if angle == 90:
            return cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
      if angle == 180:
            return cv2.rotate(img, cv2.ROTATE_180)
      if angle == 270:
            return cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
      raise ValueError(f"Unsupported angle: {angle}")


def affine_rotate(img: np.ndarray, angle_deg: float) -> np.ndarray:
      if abs(angle_deg) < 0.01:
            return img
      h, w = img.shape[:2]
      M = cv2.getRotationMatrix2D((w / 2, h / 2), angle_deg, 1.0)
      cos = abs(M[0, 0]);
      sin = abs(M[0, 1])
      new_w = int(h * sin + w * cos)
      new_h = int(h * cos + w * sin)
      M[0, 2] += (new_w - w) / 2
      M[1, 2] += (new_h - h) / 2
      return cv2.warpAffine(
            img, M, (new_w, new_h),
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_REPLICATE,
      )


def pad_box(x1, y1, x2, y2, pad, w, h):
      return (
            max(0, x1 - pad),
            max(0, y1 - pad),
            min(w, x2 + pad),
            min(h, y2 + pad),
      )


def jaccard(a: set, b: set) -> float:
      if not a and not b:
            return 0.0
      inter = len(a & b)
      union = len(a | b)
      return inter / union if union else 0.0


def ensure_color(img: np.ndarray) -> np.ndarray:
      if len(img.shape) == 2:
            return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
      return img


# ============================================================
# OCR PREPROCESSING
# ============================================================

def upscale(img: np.ndarray, scale: float = 3.0) -> np.ndarray:
      h, w = img.shape[:2]
      return cv2.resize(img, (int(w * scale), int(h * scale)),
                        interpolation=cv2.INTER_CUBIC)


def preprocess_adaptive(crop: np.ndarray) -> np.ndarray:
      up = upscale(crop, 3.0)
      gray = cv2.cvtColor(up, cv2.COLOR_BGR2GRAY) if up.ndim == 3 else up
      gray = cv2.bilateralFilter(gray, 7, 50, 50)
      clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(10, 10))
      gray = clahe.apply(gray)
      return cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY, 41, 11
      )


def preprocess_otsu(crop: np.ndarray) -> np.ndarray:
      up = upscale(crop, 3.0)
      gray = cv2.cvtColor(up, cv2.COLOR_BGR2GRAY) if up.ndim == 3 else up
      gray = cv2.GaussianBlur(gray, (3, 3), 0)
      _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
      return binary


def preprocess_inverted_otsu(crop: np.ndarray) -> np.ndarray:
      up = upscale(crop, 3.0)
      gray = cv2.cvtColor(up, cv2.COLOR_BGR2GRAY) if up.ndim == 3 else up
      gray = cv2.GaussianBlur(gray, (3, 3), 0)
      _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
      return binary


def preprocess_morph(crop: np.ndarray) -> np.ndarray:
      bin_img = preprocess_adaptive(crop)
      inv = cv2.bitwise_not(bin_img)
      kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
      inv = cv2.morphologyEx(inv, cv2.MORPH_CLOSE, kernel)
      return cv2.bitwise_not(inv)


PREPROCESSORS = {
      "adaptive": preprocess_adaptive,
      "otsu": preprocess_otsu,
      "inv_otsu": preprocess_inverted_otsu,
      "morph": preprocess_morph,
}

# ============================================================
# OCR
# ============================================================

PSM_MODES = [6, 4, 11, 3]
OCR_BASE_CONFIG = "-c preserve_interword_spaces=1 --oem 3"


def ocr_with_confidence(processed: np.ndarray, psm: int):
      config = f"{OCR_BASE_CONFIG} --psm {psm}"
      try:
            data = pytesseract.image_to_data(
                  processed, config=config,
                  output_type=pytesseract.Output.DICT
            )
      except pytesseract.TesseractError:
            return "", 0.0, 0

      words, confs = [], []
      n = len(data["text"])
      for i in range(n):
            w = data["text"][i].strip()
            try:
                  c = float(data["conf"][i])
            except (ValueError, TypeError):
                  c = -1.0
            if w and c > 0:
                  words.append(w)
                  confs.append(c)

      if not words:
            return "", 0.0, 0

      lines = defaultdict(list)
      for i in range(n):
            w = data["text"][i].strip()
            try:
                  c = float(data["conf"][i])
            except (ValueError, TypeError):
                  c = -1.0
            if w and c > 0:
                  key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
                  lines[key].append(w)
      text = "\n".join(" ".join(ws) for _, ws in sorted(lines.items()))
      text = normalize_spaces(text)

      return text, float(np.mean(confs)), len(words)


def candidate_score(text: str, mean_conf: float, word_count: int) -> float:
      if word_count == 0:
            return 0.0
      keyword_bonus = 0
      low = text.lower()
      for kw in ("ndc", "lot", "exp", "mfg", "rx only", "usp", "mg/", "mcg/", "ml"):
            if kw in low:
                  keyword_bonus += 1
      base = mean_conf * (1.0 + np.log1p(word_count) / 4.0)
      return base + 3.0 * keyword_bonus


def detect_skew_angle(crop: np.ndarray) -> float:
      gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
      edges = cv2.Canny(gray, 50, 150)
      best_angle = 0.0
      best_var = -1.0
      for a in np.arange(-6, 6.5, 1.0):
            rot = affine_rotate(edges, a)
            proj = rot.sum(axis=1).astype(np.float32)
            v = float(np.var(proj))
            if v > best_var:
                  best_var = v
                  best_angle = a
      return best_angle


def run_robust_ocr(crop: np.ndarray) -> tuple[str, OcrCandidate, list]:
      candidates: list[OcrCandidate] = []
      for rot in ROTATIONS:
            rotated = rotate_image(crop, rot)
            skew = detect_skew_angle(rotated)
            deskewed = affine_rotate(rotated, -skew) if abs(skew) > 0.5 else rotated

            for prep_name, prep_fn in PREPROCESSORS.items():
                  try:
                        processed = prep_fn(deskewed)
                  except cv2.error:
                        continue
                  for psm in PSM_MODES:
                        text, mean_conf, wc = ocr_with_confidence(processed, psm)
                        if wc == 0:
                              continue
                        score = candidate_score(text, mean_conf, wc)
                        candidates.append(OcrCandidate(
                              text=text, rotation=rot, deskew=-skew, psm=psm,
                              preprocess=prep_name, score=score,
                              word_conf_mean=mean_conf, word_count=wc,
                        ))

      if not candidates:
            empty = OcrCandidate("", 0, 0.0, 6, "adaptive", 0.0, 0.0, 0)
            return "", empty, []

      candidates.sort(key=lambda c: c.score, reverse=True)
      best = candidates[0]
      return best.text, best, candidates[:8]


# ============================================================
# FIELD EXTRACTION
# ============================================================

def fix_ocr_digits(s: str) -> str:
      if s is None:
            return s
      return (s.replace("O", "0").replace("o", "0")
              .replace("I", "1").replace("l", "1")
              .replace("S", "5").replace("B", "8")
              .replace("Z", "2").replace("Q", "0"))


NDC_RE = re.compile(
      r"\bNDC\s*[:#]?\s*([0-9OIlS]{4,5}[-\s]?[0-9OIlS]{3,4}[-\s]?[0-9OIlS]{1,2})\b",
      re.I,
)
LOT_RE = re.compile(
      r"\b(?:LOT|Lot)\s*(?:No\.?|#|:)?\s*([A-Z0-9]{4,12})\b"
)
EXP_RE = re.compile(
      r"\b(?:EXP|Exp(?:ires)?|Expiration|EXP\.?)\s*(?:DATE)?\s*[:.]?\s*"
      r"([0-9]{1,2}[/\-\.][0-9]{2,4}(?:[/\-\.][0-9]{2,4})?|"
      r"[A-Z]{3}\s?[0-9]{2,4}|[0-9]{4}-[0-9]{2}(?:-[0-9]{2})?)",
      re.I,
)
VOL_RE = re.compile(r"\b([0-9]+(?:\.[0-9]+)?)\s*m[lL]\b")
STRENGTH_RE = re.compile(
      r"\b([0-9]+(?:\.[0-9]+)?)\s*(mg|mcg|g|units?|U)\s*/\s*"
      r"([0-9]+(?:\.[0-9]+)?)\s*m[lL]\b",
      re.I,
)
STRENGTH_SOLO_RE = re.compile(r"\b([0-9]+(?:\.[0-9]+)?)\s*(mg|mcg|g)\b", re.I)


def _search_one(regex, text):
      m = regex.search(text)
      return m.group(1) if m else None


def _resolve_manufacturer(ndc: str | None, full_text: str,
                          registry: ManufacturerRegistry):
      """
      Returns (canonical_name, source) where source is 'ndc' | 'name' | None.
      NDC-based resolution wins over name-based when both succeed.
      """
      if ndc:
            m = registry.lookup_by_ndc(ndc)
            if m:
                  return m.canonical, "ndc"
      m = registry.lookup_by_name(full_text)
      if m:
            return m.canonical, "name"
      return None, None


def extract_fields_from_text(text: str,
                             registry: ManufacturerRegistry | None = None) -> dict:
      one = text.replace("\n", " ")

      ndc_raw = _search_one(NDC_RE, one)
      ndc = fix_ocr_digits(ndc_raw) if ndc_raw else None
      if ndc:
            digits = re.sub(r"[^0-9]", "", ndc)
            if len(digits) in (10, 11):
                  if len(digits) == 10:
                        digits = "0" + digits
                  ndc = f"{digits[0:5]}-{digits[5:9]}-{digits[9:11]}"

      lot = _search_one(LOT_RE, one)
      exp = _search_one(EXP_RE, one)
      vol = _search_one(VOL_RE, one)

      strength_m = STRENGTH_RE.search(one)
      if strength_m:
            strength = (f"{strength_m.group(1)} "
                        f"{strength_m.group(2).lower()}/"
                        f"{strength_m.group(3)} mL")
      else:
            solo = STRENGTH_SOLO_RE.search(one)
            strength = f"{solo.group(1)} {solo.group(2).lower()}" if solo else None

      if registry is None:
            registry = get_registry()
      manufacturer, mfg_source = _resolve_manufacturer(ndc, one, registry)

      product = None
      for canon, pattern in PRODUCT_PATTERNS:
            if re.search(pattern, one, re.I):
                  product = canon
                  break

      return {
            "product": product,
            "manufacturer": manufacturer,
            "manufacturer_source": mfg_source,
            "ndc": ndc,
            "lot": lot,
            "expiration": exp,
            "volume_ml": vol,
            "strength": strength,
      }


def extract_fields_with_salvage(candidates: list,
                                registry: ManufacturerRegistry | None = None) -> dict:
      """
      Run extraction against the winning candidate first, then fill any
      missing field by scanning lower-ranked candidates.

      Manufacturer is upgraded if a later candidate yields an NDC-sourced
      match where the winner only had a name-sourced (or no) match — the
      NDC code is the more reliable signal.
      """
      field_keys = ("product", "manufacturer", "manufacturer_source", "ndc",
                    "lot", "expiration", "volume_ml", "strength")

      if not candidates:
            return {k: None for k in field_keys}

      if registry is None:
            registry = get_registry()

      merged = extract_fields_from_text(candidates[0].text, registry)

      def needs_more(d):
            check_keys = [k for k in field_keys if k != "manufacturer_source"]
            return any(d.get(k) is None for k in check_keys)

      def mfg_upgrade(current_src, new_src):
            # name → ndc is an upgrade; everything else holds.
            return current_src != "ndc" and new_src == "ndc"

      for cand in candidates[1:]:
            if not needs_more(merged) and merged.get("manufacturer_source") == "ndc":
                  break
            more = extract_fields_from_text(cand.text, registry)
            for k, v in more.items():
                  if k == "manufacturer":
                        if v is None:
                              continue
                        if (merged.get("manufacturer") is None
                                  or mfg_upgrade(merged.get("manufacturer_source"),
                                                 more.get("manufacturer_source"))):
                              merged["manufacturer"] = v
                              merged["manufacturer_source"] = more.get("manufacturer_source")
                  elif k == "manufacturer_source":
                        continue  # handled above
                  else:
                        if merged.get(k) is None and v is not None:
                              merged[k] = v
      return merged


def tokenize(text: str) -> list:
      toks = re.findall(r"[A-Za-z0-9]+", text.lower())
      return [t for t in toks if len(t) > 1 and t not in GENERIC_STOPWORDS]


# ============================================================
# VISUAL FEATURES
# ============================================================

def compute_color_hist(crop: np.ndarray) -> np.ndarray:
      hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
      hist = cv2.calcHist([hsv], [0, 1], None, [16, 16], [0, 180, 0, 256])
      hist = cv2.normalize(hist, hist).flatten()
      return hist


_orb = cv2.ORB_create(nfeatures=500)  # module-level, created once


def compute_orb_descriptors(crop: np.ndarray):
      if crop.size == 0:
            return None
      gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
      _, des = _orb.detectAndCompute(gray, None)
      return des  # may still be None if no keypoints found —


def hist_similarity(h1, h2) -> float:
      if h1 is None or h2 is None:
            return 0.0
      sim = cv2.compareHist(h1.astype(np.float32), h2.astype(np.float32), cv2.HISTCMP_CORREL)
      return float((sim + 1.0) / 2.0)


def orb_similarity(d1, d2) -> float:
      if d1 is None or d2 is None or len(d1) == 0 or len(d2) == 0:
            return 0.0
      bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
      matches = bf.match(d1, d2)
      if not matches:
            return 0.0
      good = [m for m in matches if m.distance < 64]
      denom = max(1, min(len(d1), len(d2)))
      return min(1.0, len(good) / denom)


# ============================================================
# SEGMENTATION
# ============================================================

def _split_at_valleys(col_smooth: np.ndarray, x1: int, x2: int,
                      global_thresh: float) -> list[tuple[int, int]]:
      """
      Within a detected x_region [x1, x2], find internal valleys deep enough to
      indicate a gap between adjacent objects and return sub-regions.  A valley
      between two local maxima is treated as a split point when it drops below
      55% of the average of its flanking peaks — shallow enough to catch touching
      vials while ignoring noise on a single object's edge profile.
      """
      seg = col_smooth[x1:x2]
      n = len(seg)
      if n == 0:
            return []

      peaks = [i for i in range(1, n - 1) if seg[i] > seg[i - 1] and seg[i] >= seg[i + 1]]
      if len(peaks) < 2:
            return [(x1, x2)]

      split_pts = [0]
      for i in range(len(peaks) - 1):
            p1, p2 = peaks[i], peaks[i + 1]
            valley_pos = p1 + int(np.argmin(seg[p1:p2 + 1]))
            valley_val = seg[valley_pos]
            avg_peak = (seg[p1] + seg[p2]) / 2.0
            if avg_peak > global_thresh and valley_val < 0.55 * avg_peak:
                  split_pts.append(valley_pos)

      split_pts.append(n)
      result = []
      for i in range(len(split_pts) - 1):
            s, e = x1 + split_pts[i], x1 + split_pts[i + 1]
            if e - s > 35:
                  result.append((s, e))
      return result if result else [(x1, x2)]


def segment_components(img: np.ndarray):
      H, W = img.shape[:2]
      gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
      gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
      gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
      mag = cv2.magnitude(gx, gy)

      edge_mask = (mag > np.percentile(mag, 95)).astype(np.uint8)
      border = 30
      edge_mask[:border, :] = 0
      edge_mask[-border:, :] = 0
      edge_mask[:, :border] = 0
      edge_mask[:, -border:] = 0

      col_density = edge_mask.sum(axis=0).astype(float)
      # sigma=7 (down from 18) so the inter-vial gap isn't smoothed over
      col_smooth = gaussian_filter1d(col_density, sigma=7)
      col_thresh = max(col_smooth.max() * 0.08, 5)

      x_regions = []
      inside, start = False, 0
      for x, on in enumerate(col_smooth > col_thresh):
            if on and not inside:
                  start, inside = x, True
            elif inside and (not on or x == W - 1):
                  end = x
                  inside = False
                  if end - start > 35:
                        x_regions.append((start, end))

      # Secondary pass: split any region containing internal valleys deep enough
      # to indicate two adjacent objects that weren't separated by thresholding alone
      refined_regions = []
      for x1, x2 in x_regions:
            refined_regions.extend(_split_at_valleys(col_smooth, x1, x2, col_thresh))

      boxes = []
      for x1, x2 in refined_regions:
            submask = edge_mask[:, x1:x2]
            row_density = submask.sum(axis=1).astype(float)
            row_smooth = gaussian_filter1d(row_density, sigma=8)
            row_thresh = max(row_smooth.max() * 0.06, 2)
            ys = np.where(row_smooth > row_thresh)[0]
            if len(ys) == 0:
                  continue
            y1, y2 = int(ys[0]), int(ys[-1])
            px1, py1, px2, py2 = pad_box(x1, y1, x2, y2, pad=28, w=W, h=H)
            boxes.append((px1, py1, px2, py2))

      boxes.sort(key=lambda b: b[0])
      return boxes, edge_mask


def _normalize_segmentation_method(method: str | None) -> str:
      chosen = (method or DEFAULT_SEGMENTATION_METHOD).strip().lower()
      if chosen not in SEGMENTATION_METHODS:
            allowed = ", ".join(SEGMENTATION_METHODS)
            raise ValueError(f"Unknown segmentation method '{method}'. Choose one of: {allowed}")
      return chosen


def segment_with_method(img: np.ndarray, method: str | None = None):
      chosen = _normalize_segmentation_method(method)
      segmenters = {
            "legacy": segment_components,
            "watershed": ws_segment,
            "panoptic": po_segment,
      }
      return segmenters[chosen](img)


# ============================================================
# ANALYSIS
# ============================================================

LEGACY_BAG_AREA_RATIO = 0.10
SHAPE_MIN_CONTOUR_AREA = 450.0
SHAPE_MIN_CONFIDENCE = 1.25


def _legacy_component_type(bbox, image_shape) -> str:
      x1, y1, x2, y2 = bbox
      H, W = image_shape[:2]
      area = (x2 - x1) * (y2 - y1)
      return "bag" if area / (H * W) > LEGACY_BAG_AREA_RATIO else "vial"


def _largest_contour_from_crop(crop: np.ndarray):
      gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
      gray = cv2.GaussianBlur(gray, (5, 5), 0)

      best = None
      for mode in (cv2.THRESH_BINARY_INV, cv2.THRESH_BINARY):
            _, bw = cv2.threshold(gray, 0, 255, mode + cv2.THRESH_OTSU)
            bw = cv2.morphologyEx(
                  bw,
                  cv2.MORPH_CLOSE,
                  cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5)),
                  iterations=1,
            )
            contours, _ = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            if not contours:
                  continue
            cand = max(contours, key=cv2.contourArea)
            area = cv2.contourArea(cand)
            if best is None or area > best[0]:
                  best = (area, cand)
      return best[1] if best is not None else None


def _contour_span(points: np.ndarray) -> float:
      if points.shape[0] < 2:
            return 0.0
      xs = points[:, 0]
      return float(xs.max() - xs.min() + 1)


def infer_component_type(crop: np.ndarray, bbox, image_shape) -> str:
      contour = _largest_contour_from_crop(crop)
      if contour is None:
            return _legacy_component_type(bbox, image_shape)

      area = float(cv2.contourArea(contour))
      if area < SHAPE_MIN_CONTOUR_AREA:
            return _legacy_component_type(bbox, image_shape)

      x, y, w, h = cv2.boundingRect(contour)
      perim = max(cv2.arcLength(contour, True), 1e-6)
      hull_area = max(cv2.contourArea(cv2.convexHull(contour)), 1e-6)

      (rw, rh) = cv2.minAreaRect(contour)[1]
      rect_area = max(rw * rh, 1e-6)

      aspect = min(w, h) / max(w, h, 1)
      extent = area / max(w * h, 1)
      rectangularity = area / rect_area
      solidity = area / hull_area
      circularity = (4.0 * np.pi * area) / (perim * perim)

      pts = contour.reshape(-1, 2)
      y_min = float(pts[:, 1].min())
      y_max = float(pts[:, 1].max())
      h_span = max(1.0, y_max - y_min)
      top_cut = y_min + 0.25 * h_span
      mid_lo = y_min + 0.40 * h_span
      mid_hi = y_min + 0.70 * h_span

      top_pts = pts[pts[:, 1] <= top_cut]
      mid_pts = pts[(pts[:, 1] >= mid_lo) & (pts[:, 1] <= mid_hi)]
      top_width = _contour_span(top_pts)
      mid_width = _contour_span(mid_pts)
      neck_ratio = top_width / max(mid_width, 1.0)

      bag_score = 0.0
      vial_score = 0.0

      if aspect < 0.55:
            vial_score += 2.0
      elif aspect > 0.75:
            bag_score += 1.5

      if extent > 0.58:
            bag_score += 1.5
      elif extent < 0.48:
            vial_score += 1.0

      if rectangularity > 0.75:
            bag_score += 1.5
      elif rectangularity < 0.62:
            vial_score += 1.0

      if neck_ratio < 0.82:
            vial_score += 2.0
      elif neck_ratio > 0.93:
            bag_score += 1.0

      if solidity > 0.93 and rectangularity > 0.78:
            bag_score += 0.5
      if circularity > 0.70:
            vial_score += 0.5

      confidence = abs(bag_score - vial_score)
      if confidence < SHAPE_MIN_CONFIDENCE:
            return _legacy_component_type(bbox, image_shape)

      return "bag" if bag_score > vial_score else "vial"


def analyze_image(image_path: str, image_index: int,
                  registry: ManufacturerRegistry | None = None,
                  segmentation_method: str = DEFAULT_SEGMENTATION_METHOD) -> list:
      print(f"[pipeline] image {image_index}: loading {image_path}")
      img = cv2.imread(image_path)
      if img is None:
            raise FileNotFoundError(image_path)

      if registry is None:
            registry = get_registry()

      chosen_segmentation = _normalize_segmentation_method(segmentation_method)
      print(f"[pipeline] image {image_index}: segmenting ({chosen_segmentation})...")
      boxes, edge_mask = segment_with_method(img, chosen_segmentation)
      print(f"[pipeline] image {image_index}: found {len(boxes)} components")
      components = []
      annotated = img.copy()

      for i, box in enumerate(boxes, start=1):
            print(f"[pipeline] image {image_index}: OCR component "
                  f"{i}/{len(boxes)}...", flush=True)
            x1, y1, x2, y2 = box
            crop = img[y1:y2, x1:x2]
            crop_path = OUT_DIR / f"img{image_index}_component_{i:02d}.png"
            cv2.imwrite(str(crop_path), crop)

            text, best_cand, all_cands = run_robust_ocr(crop)
            fields = extract_fields_with_salvage(all_cands, registry=registry)
            tokens = tokenize(text)

            ctype = infer_component_type(crop, box, img.shape)
            h, w = crop.shape[:2]

            comp = Component(
                  image_index=image_index,
                  component_index=i,
                  bbox_xyxy=box,
                  crop_path=str(crop_path),
                  component_type=ctype,
                  text=text,
                  best_rotation=best_cand.rotation,
                  best_deskew=best_cand.deskew,
                  best_psm=best_cand.psm,
                  best_preprocess=best_cand.preprocess,
                  ocr_confidence=best_cand.word_conf_mean,
                  tokens=tokens,
                  product=fields["product"],
                  manufacturer=fields["manufacturer"],
                  manufacturer_source=fields["manufacturer_source"],
                  ndc=fields["ndc"],
                  lot=fields["lot"],
                  expiration=fields["expiration"],
                  volume_ml=fields["volume_ml"],
                  strength=fields["strength"],
                  aspect_ratio=w / max(h, 1),
                  area=w * h,
                  color_hist=compute_color_hist(crop),
                  orb_des=compute_orb_descriptors(crop),
                  all_ocr_candidates=all_cands,
            )
            components.append(comp)

            # Compact one-line summary so the progress log is also a useful audit trail
            ndc_str = comp.ndc or "-"
            lot_str = comp.lot or "-"
            mfg_str = comp.manufacturer or "-"
            print(f"[pipeline]   → {ctype}  ndc={ndc_str}  lot={lot_str}  "
                  f"mfg={mfg_str}  conf={comp.ocr_confidence:.0f}",
                  flush=True)

            cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 255, 255), 3)
            cv2.putText(annotated, f"img{image_index}_obj{i}",
                        (x1, max(20, y1 - 10)),
                        FONT, 0.8, (0, 255, 255), 2, cv2.LINE_AA)

      cv2.imwrite(str(OUT_DIR / f"img{image_index}_segmentation.png"), annotated)
      cv2.imwrite(str(OUT_DIR / f"img{image_index}_edge_mask.png"), edge_mask * 255)
      print(f"[pipeline] image {image_index}: done "
            f"({len(components)} components processed)")
      return components


# ============================================================
# MATCHING
# ============================================================

def field_score(a: Component, b: Component) -> float:
      if a.component_type != b.component_type:
            return -999.0
      score = 0.0

      def exact(va, vb, w):
            nonlocal score
            na, nb = normalize_idlike(va), normalize_idlike(vb)
            if na and nb:
                  score += w if na == nb else -1.5 * w

      exact(a.ndc, b.ndc, 8.0)
      exact(a.lot, b.lot, 7.0)
      exact(a.expiration, b.expiration, 5.0)
      exact(a.volume_ml, b.volume_ml, 2.0)
      exact(a.strength, b.strength, 3.0)

      if a.manufacturer and b.manufacturer:
            score += 4.0 if a.manufacturer.lower() == b.manufacturer.lower() else -6.0
      if a.product and b.product:
            score += 4.0 if a.product.lower() == b.product.lower() else -4.0
      return score


def visual_score(a: Component, b: Component) -> float:
      hsim = hist_similarity(a.color_hist, b.color_hist)
      osim = orb_similarity(a.orb_des, b.orb_des)
      size_sim = 1.0 - min(1.0, abs(a.aspect_ratio - b.aspect_ratio) /
                           max(a.aspect_ratio, b.aspect_ratio, 1e-6))
      return 3.0 * hsim + 3.0 * osim + 1.0 * size_sim


def token_score(a: Component, b: Component) -> float:
      return 4.0 * jaccard(set(a.tokens), set(b.tokens))


def pair_score(a: Component, b: Component) -> float:
      fs = field_score(a, b)
      if fs < -100:
            return fs
      return fs + token_score(a, b) + visual_score(a, b)


def build_score_matrix(comps_a, comps_b) -> np.ndarray:
      S = np.zeros((len(comps_a), len(comps_b)), dtype=np.float32)
      for i, a in enumerate(comps_a):
            for j, b in enumerate(comps_b):
                  S[i, j] = pair_score(a, b)
      return S


def find_link_groups(comps_a, comps_b, scores,
                     min_score=3.0, ambiguity_margin=1.0):
      if len(comps_a) == 0 or len(comps_b) == 0:
            return [], []

      row_best = scores.max(axis=1)
      col_best = scores.max(axis=0)

      graph = defaultdict(set)
      nodes = set()
      for i in range(len(comps_a)):
            for j in range(len(comps_b)):
                  s = float(scores[i, j])
                  if s < min_score:
                        continue
                  if s >= row_best[i] - ambiguity_margin or s >= col_best[j] - ambiguity_margin:
                        ai, bj = f"A{i}", f"B{j}"
                        graph[ai].add(bj);
                        graph[bj].add(ai)
                        nodes.update([ai, bj])

      visited = set()
      unique_links, ambiguous_groups = [], []

      for start in nodes:
            if start in visited:
                  continue
            q = deque([start]);
            visited.add(start);
            comp_nodes = []
            while q:
                  cur = q.popleft();
                  comp_nodes.append(cur)
                  for nxt in graph[cur]:
                        if nxt not in visited:
                              visited.add(nxt);
                              q.append(nxt)

            a_ids = sorted(int(n[1:]) for n in comp_nodes if n.startswith("A"))
            b_ids = sorted(int(n[1:]) for n in comp_nodes if n.startswith("B"))

            if len(a_ids) == 1 and len(b_ids) == 1:
                  i, j = a_ids[0], b_ids[0]
                  unique_links.append({
                        "image1_component_index": comps_a[i].component_index,
                        "image2_component_index": comps_b[j].component_index,
                        "score": float(scores[i, j]),
                        "status": "unique",
                  })
            else:
                  ambiguous_groups.append({
                        "image1_component_indices": [comps_a[i].component_index for i in a_ids],
                        "image2_component_indices": [comps_b[j].component_index for j in b_ids],
                        "scores": {
                              f"{comps_a[i].component_index}->{comps_b[j].component_index}":
                                    float(scores[i, j])
                              for i in a_ids for j in b_ids
                        },
                        "status": "ambiguous_group",
                  })

      return unique_links, ambiguous_groups


def find_unmatched(comps_a, comps_b, unique_links, ambiguous_groups):
      matched_a, matched_b = set(), set()
      for lk in unique_links:
            matched_a.add(lk["image1_component_index"])
            matched_b.add(lk["image2_component_index"])
      for grp in ambiguous_groups:
            matched_a.update(grp["image1_component_indices"])
            matched_b.update(grp["image2_component_indices"])
      unmatched_a = [c.component_index for c in comps_a if c.component_index not in matched_a]
      unmatched_b = [c.component_index for c in comps_b if c.component_index not in matched_b]
      return unmatched_a, unmatched_b


# ============================================================
# JSON EXPORT
# ============================================================

def component_to_dict(c: Component) -> dict:
      d = asdict(c)
      d.pop("color_hist", None)
      d.pop("orb_des", None)
      d.pop("all_ocr_candidates", None)
      return d


def run_pipeline(image_paths: list[str] | None = None,
                 out_json_name: str = "linked_components.json",
                 mfg_registry_path: str | Path | None = None,
                 render_pdf: bool = True,
                 out_pdf_name: str = "linked_components_report.pdf",
                 segmentation_method: str = DEFAULT_SEGMENTATION_METHOD,
                 ) -> dict:
      """
      End-to-end CV pipeline. Returns the result dict and writes JSON to disk.
      When `render_pdf` is True (default), also runs the report.py renderer
      immediately after JSON serialization.
      """
      import time
      t0 = time.monotonic()

      paths = image_paths or IMAGE_PATHS
      chosen_segmentation = _normalize_segmentation_method(segmentation_method)
      print(f"[pipeline] starting (images: {len(paths)})")
      print(f"[pipeline] segmentation method: {chosen_segmentation}")
      registry = get_registry(mfg_registry_path or MFG_REGISTRY_PATH)
      print(f"[pipeline] manufacturer registry: {registry.summary()}")

      all_components = []
      for idx, path in enumerate(paths, start=1):
            comps = analyze_image(
                  path,
                  image_index=idx,
                  registry=registry,
                  segmentation_method=chosen_segmentation,
            )
            all_components.append(comps)

      comps1, comps2 = all_components

      print(f"[pipeline] linking phase: serializing {len(comps1)}+{len(comps2)} "
            f"components")
      comps1_dicts = [component_to_dict(c) for c in comps1]
      comps2_dicts = [component_to_dict(c) for c in comps2]

      print(f"[pipeline] linking phase: building equivalence classes and "
            f"tiered links...")
      linkage = link_images(comps1_dicts, comps2_dicts, ambiguity_margin=1.0)
      print(f"[pipeline] linking phase: "
            f"{len(linkage['equiv_classes_image1'])}+"
            f"{len(linkage['equiv_classes_image2'])} classes, "
            f"{len(linkage['links_by_tier']['identity'])} identity, "
            f"{len(linkage['links_by_tier']['sku'])} sku, "
            f"{len(linkage['links_by_tier']['therapeutic'])} therapeutic")

      result = {
            "segmentation": {"method": chosen_segmentation},
            "images": [
                  {"image_index": 1, "path": paths[0],
                   "components": comps1_dicts},
                  {"image_index": 2, "path": paths[1],
                   "components": comps2_dicts},
            ],
            "linkage": linkage,
      }

      out_json = OUT_DIR / out_json_name
      print(f"[pipeline] writing JSON: {out_json}")
      with open(out_json, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)

      if render_pdf:
            # Lazy import so a missing reportlab dep doesn't block JSON-only runs
            try:
                  from report import build_report
            except ImportError as e:
                  print(f"[pipeline] WARN: cannot import report.py ({e}); "
                        f"skipping PDF render")
            else:
                  out_pdf = OUT_DIR / out_pdf_name
                  print(f"[pipeline] rendering PDF: {out_pdf}")
                  build_report(result, out_pdf, out_dir=OUT_DIR)
                  print(f"[pipeline] PDF done")

      elapsed = time.monotonic() - t0
      print(f"[pipeline] complete in {elapsed:.1f}s")
      return result


# ============================================================
# CLI
# ============================================================

def main():
      import argparse
      import time

      start_time = time.monotonic()

      ap = argparse.ArgumentParser(
            description="Run the CV pipeline (segment → OCR → link → render).")
      ap.add_argument("--no-pdf", action="store_true",
                      help="Skip PDF rendering; write JSON only.")
      ap.add_argument("--mfg-registry",
                      default=str(MFG_REGISTRY_PATH),
                      help=f"Path to manufacturer registry "
                           f"(default: {MFG_REGISTRY_PATH})")
      ap.add_argument(
            "--segmentation-method",
            choices=SEGMENTATION_METHODS,
            default=DEFAULT_SEGMENTATION_METHOD,
            help=("Segmentation backend to use "
                  f"(default: {DEFAULT_SEGMENTATION_METHOD})."),
      )
      args = ap.parse_args()

      result = run_pipeline(
            render_pdf=not args.no_pdf,
            mfg_registry_path=args.mfg_registry,
            segmentation_method=args.segmentation_method,
      )
      linkage = result["linkage"]

      print()
      print("=" * 60)
      print("SUMMARY")
      print("=" * 60)
      print(f"  components:        image1={len(result['images'][0]['components'])}, "
            f"image2={len(result['images'][1]['components'])}")
      print(f"  equiv classes:     image1={len(linkage['equiv_classes_image1'])}, "
            f"image2={len(linkage['equiv_classes_image2'])}")
      print(f"  identity links:    {len(linkage['links_by_tier']['identity'])}")
      print(f"  sku links:         {len(linkage['links_by_tier']['sku'])}")
      print(f"  therapeutic links: {len(linkage['links_by_tier']['therapeutic'])}")
      print(f"  unmatched img1:    {len(linkage['unmatched_image1'])}")
      print(f"  unmatched img2:    {len(linkage['unmatched_image2'])}")
      print()
      print(f"  JSON: {OUT_DIR / 'linked_components.json'}")
      if not args.no_pdf:
            print(f"  PDF:  {OUT_DIR / 'linked_components_report.pdf'}")

      elapsed = time.monotonic() - start_time
      print(f"Done in {elapsed:.1f}s")


if __name__ == "__main__":
      main()
