"""
Stage 2: OCR
============
Extract Lot, Expiration, and NDC fields from per-vial crops.

Strategy:
    1. Run OCR on multiple preprocessed/rotated versions of each crop.
    2. Pool all detected text strings.
    3. Apply field-specific regex patterns to extract candidates.
    4. Vote on the most common candidate per field (cheap ensemble).
    5. Validate against the field's expected format; flag uncertain ones.

This is the cheapest robustness lever for pharmaceutical label OCR. A single
OCR pass will miss fields when the label is rotated, partially occluded by a
vial cap, or has low contrast against the glass. Voting across 4-8 variants
costs ~5x the inference time but typically halves the field-extraction error.

Backend:
    EasyOCR (pure PyTorch, no PaddlePaddle dependency). On the same hardware
    you've already provisioned for SAM 2 + DINOv2, this avoids adding an
    entire second deep-learning framework just for OCR.

Install:
    pip install easyocr pillow numpy
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable

import numpy as np
from PIL import Image, ImageEnhance, ImageOps

try:
      import easyocr

      _EASYOCR_AVAILABLE = True
except ImportError:
      easyocr = None  # type: ignore[assignment]
      _EASYOCR_AVAILABLE = False

# ----- Regex patterns ------------------------------------------------------
# Lot numbers are alphanumeric, typically 4-12 chars. Pharma lots use a mix
# of letters and digits; pure-digit lots also exist (e.g., 308777251000 from
# the Pfizer reference image, or 30612234).
LOT_RE = re.compile(r"(?:LOT|Lot|lot)\s*[:#]?\s*([A-Z0-9]{4,14})", re.IGNORECASE)

# Expiration formats seen in the wild:
#   MM/YY      e.g. 12/26
#   MM/YYYY    e.g. 04/27
#   MM/DD/YYYY e.g. 05/30/2028
#   YYYY-MM-DD (less common on US labels but allowed)
EXP_RE = re.compile(r"(?:EXP|Exp|exp)\s*[:.]?\s*"
                    r"(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}"  # MM/DD/YY(YY)
                    r"|\d{1,2}[/\-]\d{2,4}"  # MM/YY(YY)
                    r"|\d{4}[/\-]\d{1,2}[/\-]\d{1,2})",  # YYYY-MM-DD
      re.IGNORECASE, )

# NDC: National Drug Code. Three formats: 4-4-2, 5-3-2, 5-4-1, plus the
# 11-digit billing format (5-4-2 with leading zeros). Hyphens or spaces.
NDC_RE = re.compile(r"NDC\s*"
                    r"(\d{4,5}[\-\s]\d{3,4}[\-\s]\d{1,2})", re.IGNORECASE, )


@dataclass
class OCRFields:
      """Extracted fields for one vial crop."""
      lot: str | None = None
      exp: str | None = None
      ndc: str | None = None
      mfg: str | None = None
      product: str | None = None
      strength: str | None = None
      vol_ml: str | None = None
      brand: str | None = None
      mdv: str | None = None
      instructions: str | None = None

      # Per-field confidence: how many of the OCR variants agreed on this value.
      # Range [0, 1]. Low confidence => flag for human review.
      lot_confidence: float = 0.0
      exp_confidence: float = 0.0
      ndc_confidence: float = 0.0
      # Raw OCR strings from all variants, for debugging.
      raw_texts: list[str] = field(default_factory=list)

      def needs_review(self, threshold: float = 0.4) -> bool:
            """True if any extracted field had low ensemble agreement."""
            for value, conf in [(self.lot, self.lot_confidence), (self.exp, self.exp_confidence),
                  (self.ndc, self.ndc_confidence), ]:
                  if value is not None and conf < threshold:
                        return True
            return False


# ----- Image preprocessing variants ----------------------------------------
def _preprocess_variants(crop: Image.Image) -> list[Image.Image]:
      """Generate a small set of preprocessed versions of one crop.

    Each variant targets a different OCR failure mode:
      - original: baseline
      - contrast-stretched: faded printing on white labels
      - sharpened: small text below the legibility threshold
      - grayscale-autocontrast: colored labels (purple caps, orange accents)
        where color is misleading the detector
    """
      variants = [crop]

      # Contrast stretch
      contrast = ImageEnhance.Contrast(crop).enhance(1.6)
      variants.append(contrast)

      # Sharpen
      sharp = ImageEnhance.Sharpness(crop).enhance(2.5)
      variants.append(sharp)

      # Grayscale + autocontrast (good for labels with colored backgrounds)
      gray = ImageOps.autocontrast(ImageOps.grayscale(crop)).convert("RGB")
      variants.append(gray)

      return variants


def _rotation_variants(crop: Image.Image) -> list[tuple[int, Image.Image]]:
      """Return (angle, rotated_image) pairs for 0/90/180/270 degrees.

    Knocked-over vials (image 4 in the reference set) need this. EasyOCR's
    built-in rotation handling covers small angles but not full quarter turns.
    """
      return [(angle, crop.rotate(angle, expand=True)) for angle in (0, 90, 180, 270)]


class FieldExtractor:
      """OCR + regex extraction wrapper.
      Initialize once, reuse across many crops.
    """

      def __init__(self, lang: str = "en", use_gpu: bool | None = None) -> None:
            if not _EASYOCR_AVAILABLE:
                  raise ImportError("EasyOCR not installed. Run: pip install easyocr")
            # EasyOCR auto-detects GPU when gpu=None, but explicit is clearer.
            if use_gpu is None:
                  try:
                        import torch
                        use_gpu = torch.cuda.is_available()
                  except ImportError:
                        use_gpu = False

            # languages is a list — keep 'en' only for English labels. Add other
            # codes (e.g., 'fr', 'de') if you process multilingual packaging.
            self.reader = easyocr.Reader([lang], gpu=use_gpu, verbose=False)

      def extract(self, crop: Image.Image, try_rotations: bool = True, ) -> OCRFields:
            """Extract Lot/Exp/NDC from one vial crop using the variant ensemble."""
            # Build the full set of (image variant) inputs.
            if try_rotations:
                  base_inputs = [img for _, img in _rotation_variants(crop)]
            else:
                  base_inputs = [crop]

            all_variants: list[Image.Image] = []
            for img in base_inputs:
                  all_variants.extend(_preprocess_variants(img))

            # Run OCR on each variant, collect every string.
            raw_texts: list[str] = []
            for img in all_variants:
                  texts = self._ocr_one(img)
                  raw_texts.extend(texts)

            # Apply regex extraction across the pooled text.
            lot_candidates = self._extract_with(LOT_RE, raw_texts)
            exp_candidates = self._extract_with(EXP_RE, raw_texts)
            ndc_candidates = self._extract_with(NDC_RE, raw_texts)

            lot, lot_conf = _vote(lot_candidates)
            exp, exp_conf = _vote(exp_candidates)
            ndc, ndc_conf = _vote(ndc_candidates)

            return OCRFields(lot=lot, exp=exp, ndc=_normalize_ndc(ndc) if ndc else None, lot_confidence=lot_conf,
                  exp_confidence=exp_conf, ndc_confidence=ndc_conf, raw_texts=raw_texts, )

      def _ocr_one(self, img: Image.Image) -> list[str]:
            """Run EasyOCR on a single PIL image, return list of detected strings.

        EasyOCR's readtext() returns a list of (bbox, text, confidence) tuples.
        We discard bbox/confidence here because the voting ensemble across
        variants gives us a more robust confidence signal than per-detection
        OCR confidence (which is overconfident on hallucinated reads).
        """
            arr = np.array(img)
            results = self.reader.readtext(arr, detail=1)
            if not results:
                  return []
            return [text for (_, text, _) in results]

      @staticmethod
      def _extract_with(pattern: re.Pattern[str], texts: Iterable[str]) -> list[str]:
            """Apply a regex to every text fragment, return all matches.

        Also try concatenating adjacent fragments — EasyOCR sometimes splits
        'LOT' and the lot number itself into separate detections, breaking
        regexes that look for both in one string.
        """
            out = []
            text_list = list(texts)

            # First pass: each text in isolation.
            for t in text_list:
                  for m in pattern.finditer(t):
                        out.append(m.group(1).strip().upper())

            # Second pass: pairs of consecutive fragments joined by space.
            # Catches the "LOT" / "ABC123" split case.
            for i in range(len(text_list) - 1):
                  joined = f"{text_list[i]} {text_list[i + 1]}"
                  for m in pattern.finditer(joined):
                        out.append(m.group(1).strip().upper())

            return out


def _vote(candidates: list[str]) -> tuple[str | None, float]:
      """Return the most common candidate and its agreement fraction."""
      if not candidates:
            return None, 0.0
      counter = Counter(candidates)
      top, top_count = counter.most_common(1)[0]
      confidence = top_count / len(candidates)
      return top, confidence


def _normalize_ndc(ndc: str) -> str:
      """Standardize NDC formatting to hyphens-only."""
      return re.sub(r"\s+", "-", ndc.strip())


def extract_fields(crop: Image.Image, extractor: FieldExtractor | None = None) -> OCRFields:
      """Convenience wrapper. Builds a FieldExtractor if one isn't passed."""
      if extractor is None:
            extractor = FieldExtractor()
      return extractor.extract(crop)


if __name__ == "__main__":
      import sys

      if len(sys.argv) < 2:
            print("Usage: python ocr.py <crop_image_path>")
            sys.exit(1)

      img = Image.open(sys.argv[1]).convert("RGB")
      fields = extract_fields(img)
      print(f"Lot: {fields.lot} (conf={fields.lot_confidence:.2f})")
      print(f"Exp: {fields.exp} (conf={fields.exp_confidence:.2f})")
      print(f"NDC: {fields.ndc} (conf={fields.ndc_confidence:.2f})")
      if fields.needs_review():
            print("[FLAGGED FOR REVIEW — low confidence on at least one field]")
