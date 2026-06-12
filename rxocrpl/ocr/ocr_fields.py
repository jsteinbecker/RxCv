"""
Stage 2: OCR + structured field extraction.

Merged module covering both halves of the OCR pipeline:

  Layer A (image -> text):  FieldExtractor + image-variant ensemble
                            (rotations × preprocessings, EasyOCR per variant,
                            adjacent-fragment joining)

  Layer B (text -> fields): per-field extractors + NDC directory enrichment

Two entry points:

    extractor = FieldExtractor()
    fields = extractor.extract(pil_crop)            # full pipeline from image

    fields, audit = extract_fields(ocr_text_lines)  # from already-OCR'd text

Confidence semantics
--------------------
Each populated field carries a confidence in [0, 1] computed as:

    confidence  =  regex_prior  ×  variant_agreement

regex_prior reflects the strength of the match (a labeled "LOT 2JD588" is
worth more than a bare alphanumeric token), and variant_agreement is the
fraction of OCR variants whose pooled text yielded the same value. When the
caller invokes the text-only API (extract_fields), variant_agreement is 1.0
and confidence collapses to the regex prior alone.

Backend
-------
EasyOCR for the image layer; pure regex + an FDA NDC Directory CSV for the
text layer. Install:

    pip install easyocr pillow numpy
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, asdict, field
from typing import Callable, Iterable, Optional

import numpy as np
from PIL import Image, ImageEnhance, ImageOps

try:
    from .ndc_directory import NDCDirectory, NDCEntry, get_directory, get_labeler_directory
    from .ndc_directory import _is_descriptive_proprietary
except ImportError:
    from ndc_directory import NDCDirectory, NDCEntry, get_directory, get_labeler_directory
    from ndc_directory import _is_descriptive_proprietary

try:
    import easyocr
    _EASYOCR_AVAILABLE = True
except ImportError:
    easyocr = None  # type: ignore[assignment]
    _EASYOCR_AVAILABLE = False

try:
    from pyzbar.pyzbar import decode as _decode_barcodes
    _BARCODE_AVAILABLE = True
except Exception:
    _decode_barcodes = None  # type: ignore[assignment]
    _BARCODE_AVAILABLE = False


# ===========================================================================
# Output schema
# ===========================================================================
@dataclass
class OCRFields:
    """Extracted fields for one component crop.

    Each field has a parallel *_confidence in [0, 1]. needs_review() returns
    True when any populated field falls below a threshold; the upstream
    pipeline uses this to decide whether to route the detection for human
    review.

    barcode_ndc is populated when a machine-readable barcode (GS1-128, Code 128,
    DataMatrix, etc.) was decoded from the crop. It is always high-confidence
    and is used by the matcher at a higher weight than OCR-inferred ndc."""
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
    # Barcode-sourced NDC — separate from OCR-inferred ndc so the matcher can
    # weight it higher. Not included in needs_review() since it's always ≥0.99.
    barcode_ndc: str | None = None

    lot_confidence: float = 0.0
    exp_confidence: float = 0.0
    ndc_confidence: float = 0.0
    mfg_confidence: float = 0.0
    product_confidence: float = 0.0
    strength_confidence: float = 0.0
    vol_ml_confidence: float = 0.0
    brand_confidence: float = 0.0
    mdv_confidence: float = 0.0
    instructions_confidence: float = 0.0
    barcode_ndc_confidence: float = 0.0

    raw_texts: list[str] = field(default_factory=list)

    _FIELD_NAMES = (
        "lot", "exp", "ndc", "mfg", "product", "strength",
        "vol_ml", "brand", "mdv", "instructions",
        # barcode_ndc intentionally excluded — always high-confidence, not user-reviewable
    )

    def needs_review(self, threshold: float = 0.4) -> bool:
        """True if any extracted field has confidence below `threshold`."""
        for name in self._FIELD_NAMES:
            value = getattr(self, name)
            conf = getattr(self, f"{name}_confidence")
            if value is not None and conf < threshold:
                return True
        return False


@dataclass
class FieldExtraction:
    """Internal: one candidate value with its provenance."""
    value: str
    regex_prior: float       # how strong was the regex match (label vs bare)
    source_line: str
    source: str              # which extractor produced it


# ===========================================================================
# Multi-dose / single-dose phrasing — label language, kept hardcoded
# ===========================================================================
MDV_PATTERNS: list[tuple[str, str]] = [
    (r"\bMULTI[\s-]*DOSE\b",        "MDV"),
    (r"\bMULTIPLE[\s-]*DOSE\b",     "MDV"),
    (r"\bSINGLE[\s-]*DOSE\b",       "SDV"),
    (r"\bSINGLE[\s-]*USE\b",        "SDV"),
    (r"\bPRESERVATIVE[\s-]*FREE\b", "SDV"),
]

INSTRUCTION_KEYWORDS = (
    "for intravenous use only",
    "for iv use only",
    "for intramuscular use only",
    "for im use only",
    "single use only",
    "discard unused portion",
    "shake well",
    "refrigerate after reconstitution",
    "do not use if",
    "store at",
    "protect from light",
    "must be diluted",
    "rx only",
)


# ===========================================================================
# Text normalization
# ===========================================================================
_ZERO_LIKE = str.maketrans({"O": "0", "o": "0", "Q": "0", "D": "0"})
_ONE_LIKE  = str.maketrans({"I": "1", "l": "1", "|": "1"})


def normalize_for_digits(s: str) -> str:
    """Repair OCR confusions inside what's expected to be a digit run."""
    return s.translate(_ZERO_LIKE).translate(_ONE_LIKE)


def normalize_line(s: str) -> str:
    """Light cleanup applied to every OCR line before regex matching."""
    s = s.replace("\u2013", "-").replace("\u2014", "-")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _normalize_lot_value(raw: str) -> str:
    """Apply digit-confusable repair to a lot, but only when it's mostly
    digits (preserves real letter content like 'HK3092')."""
    real_letters = [ch for ch in raw if ch.isalpha() and ch not in "OIL"]
    digits       = [ch for ch in raw if ch.isdigit()]
    confusables  = [ch for ch in raw if ch in "OIL"]
    if real_letters:
        return raw
    if confusables and digits and len(digits) >= len(confusables):
        return raw.translate(_ZERO_LIKE).translate(_ONE_LIKE)
    return raw


# ===========================================================================
# Per-field extractors (regex / heuristic layer)
# ===========================================================================
def extract_ndc(line: str) -> Optional[FieldExtraction]:
    """NDC: 10/11-digit code in 4-4-2, 5-3-2, 5-4-1, or 5-4-2 segmentation."""
    digits_only = normalize_for_digits(line)
    has_ndc_prefix = bool(re.search(r"\bNDC\b", line, re.IGNORECASE))

    pattern_segmented = re.compile(
        r"(?:NDC[\s:#]*)?"
        r"(\d{4,5})[\s\-](\d{3,4})[\s\-](\d{1,2})"
        r"(?!\d)",
        re.IGNORECASE,
    )
    for m in pattern_segmented.finditer(digits_only):
        a, b, c = m.groups()
        if len(a) + len(b) + len(c) not in (10, 11):
            continue
        start = m.start(1)
        if start > 0 and digits_only[start - 1].isdigit():
            continue
        value = f"{a}-{b}-{c}"
        prior = 0.95 if has_ndc_prefix else 0.70
        return FieldExtraction(value, prior, line, "extract_ndc:segmented")

    # Bare 10/11-digit run — only accepted when 'NDC' label is present.
    pattern_run = re.compile(r"(?<!\d)(\d{10,11})(?!\d)")
    for m in pattern_run.finditer(digits_only):
        run = m.group(1)
        if not has_ndc_prefix:
            continue
        if len(run) == 11:
            value = f"{run[:5]}-{run[5:9]}-{run[9:]}"
        else:
            value = f"{run[:5]}-{run[5:8]}-{run[8:]}"
        return FieldExtraction(value, 0.85, line, "extract_ndc:run")
    return None


def extract_lot(line: str) -> Optional[FieldExtraction]:
    """Lot/batch numbers. Tolerates LOT/BATCH/BN with or without a word
    boundary on the left (handles concatenated OCR output like 'LOTHK3092')."""
    # Labeled form, with or without a left word boundary — many OCR engines
    # join 'LOT' to the value when the printed gap is narrow.
    m = re.search(
        r"(?:^|\W)(?:LOT|BATCH|BN)[\s:#.]*([A-Z0-9]{4,15})",
        line, re.IGNORECASE,
    )
    if m:
        raw = m.group(1).upper()
        return FieldExtraction(_normalize_lot_value(raw), 0.95,
                               line, "extract_lot:labeled")

    # Unlabeled fallback: alphanumeric tokens that aren't dates or NDCs.
    # Intentionally conservative — a bare token with no surrounding label context
    # is weakly evidenced. Requirements: ≥6 chars, ≥2 digits (facility codes like
    # "8A432" have too few digits to be a reliable lot read). Prior is set below
    # the needs_review() threshold (0.4) so these always get flagged for human
    # review rather than silently accepted.
    for tok in re.findall(r"[A-Z0-9]{6,15}", line):
        if re.fullmatch(r"\d{1,2}/\d{1,2}/?\d{0,4}", tok):
            continue
        digit_count = sum(1 for ch in tok if ch.isdigit())
        if digit_count < 2:
            continue
        if re.fullmatch(r"\d{10,11}", tok):
            return FieldExtraction(tok, 0.40, line, "extract_lot:bare_digits")
        if any(ch.isalpha() for ch in tok):
            return FieldExtraction(_normalize_lot_value(tok.upper()), 0.35,
                                   line, "extract_lot:bare_alnum")
    return None


def extract_exp(line: str) -> Optional[FieldExtraction]:
    """Expiration date in any of the common formats."""
    src = normalize_for_digits(line)
    has_exp = bool(re.search(
        r"\b(?:EXP|EXPIRE[SD]?|EXPIR(?:ATION|Y)|USE\s*BY)\b",
        line, re.IGNORECASE,
    ))
    patterns = [
        (r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b",  "MM/DD/YYYY"),
        (r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b",  "YYYY-MM-DD"),
        (r"\b(\d{1,2})[/-](\d{4})\b",         "MM/YYYY"),
        (r"\b(\d{1,2})/(\d{2})\b",            "MM/YY"),
        (r"\b(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*[\s\-]*(\d{2,4})\b",
         "MMM YYYY"),
    ]
    for pat, kind in patterns:
        m = re.search(pat, src, re.IGNORECASE)
        if not m:
            continue
        value = m.group(0)
        prior = 0.95 if has_exp else 0.55
        return FieldExtraction(value, prior, line, f"extract_exp:{kind}")
    return None


def extract_strength(line: str) -> Optional[FieldExtraction]:
    """Drug strength: number + mass/activity/percentage unit. Distinguishes
    total strength from concentration (X/mL gets a lower prior)."""
    pattern = re.compile(
        r"(\d+(?:\.\d+)?)\s*"
        r"(g|mg|mcg|\u00b5g|ug|units?|U|mEq|%)"
        r"\b",
        re.IGNORECASE,
    )
    matches = list(pattern.finditer(line))
    if not matches:
        return None
    for m in matches:
        amount, unit = m.groups()
        tail = line[m.end():m.end()+5]
        is_concentration = bool(re.match(r"\s*/\s*mL", tail, re.IGNORECASE))
        unit_l = unit.lower()
        if unit_l == "u":
            unit_l = "units"
        if unit_l in ("ug", "\u00b5g"):
            unit_l = "mcg"
        value = f"{amount} {unit_l}"
        prior = 0.70 if is_concentration else 0.90
        return FieldExtraction(value, prior, line, "extract_strength")
    return None


def extract_vol_ml(line: str) -> Optional[FieldExtraction]:
    """Volume in mL. Returns the numeric portion only."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*mL\b", line, re.IGNORECASE)
    if m:
        return FieldExtraction(m.group(1), 0.90, line, "extract_vol_ml")
    return None


def extract_mdv(line: str) -> Optional[FieldExtraction]:
    """Multi-dose / single-dose / preservative-free phrasing."""
    upper = line.upper()
    for pat, canonical in MDV_PATTERNS:
        if re.search(pat, upper):
            return FieldExtraction(canonical, 0.90, line, "extract_mdv")
    return None


def extract_instructions(line: str) -> Optional[FieldExtraction]:
    """Free-text directives like 'for intravenous use only'."""
    low = line.lower()
    for kw in INSTRUCTION_KEYWORDS:
        if kw in low:
            return FieldExtraction(line, 0.85, line, f"extract_instructions:{kw}")
    return None


# Directory-backed extractors.
def extract_mfg(line: str, directory: Optional[NDCDirectory]) -> Optional[FieldExtraction]:
    if directory is None:
        return None
    name = directory.find_labeler(line)
    if name:
        return FieldExtraction(name, 0.90, line, "extract_mfg:directory")
    return None


def extract_brand(line: str, directory: Optional[NDCDirectory]) -> Optional[FieldExtraction]:
    if directory is None:
        return None
    name = directory.find_brand(line)
    if name:
        return FieldExtraction(name, 0.95, line, "extract_brand:directory")
    return None


def extract_product(line: str, directory: Optional[NDCDirectory]) -> Optional[FieldExtraction]:
    if directory is None:
        return None
    name = directory.find_generic(line)
    if name:
        return FieldExtraction(name, 0.90, line, "extract_product:directory")
    return None


SIMPLE_EXTRACTORS: dict[str, Callable[[str], Optional[FieldExtraction]]] = {
    "ndc": extract_ndc,
    "lot": extract_lot,
    "exp": extract_exp,
    "strength": extract_strength,
    "vol_ml": extract_vol_ml,
    "mdv": extract_mdv,
}
DIRECTORY_EXTRACTORS: dict[str, Callable[[str, Optional[NDCDirectory]], Optional[FieldExtraction]]] = {
    "mfg": extract_mfg,
    "brand": extract_brand,
    "product": extract_product,
}


# ===========================================================================
# Layer B: text -> structured fields (with optional ensemble pooling)
# ===========================================================================
def _expand_with_adjacents(lines: list[str]) -> list[str]:
    """Add concatenations of adjacent fragments so regexes that span 'LOT'
    and the lot value (split into separate detections) still match."""
    out = list(lines)
    for i in range(len(lines) - 1):
        out.append(f"{lines[i]} {lines[i + 1]}")
    return out


def _aggregate_field(
    field_name: str,
    extractor_fn: Callable[..., Optional[FieldExtraction]],
    pooled_lines: list[list[str]],
    *args,
) -> tuple[Optional[FieldExtraction], float]:
    """Run a single-line extractor across every line in every variant pool.

    Returns (best_extraction, ensemble_agreement) where ensemble_agreement is
    the fraction of variant pools whose best candidate matched the global
    winner. If only one pool exists (text-only API), ensemble_agreement=1.0.
    """
    # For each variant pool, find its best candidate (highest regex_prior).
    per_variant_best: list[FieldExtraction] = []
    for pool in pooled_lines:
        best_in_pool: Optional[FieldExtraction] = None
        for line in _expand_with_adjacents(pool):
            cand = extractor_fn(line, *args)
            if cand is None:
                continue
            if best_in_pool is None or cand.regex_prior > best_in_pool.regex_prior:
                best_in_pool = cand
        if best_in_pool is not None:
            per_variant_best.append(best_in_pool)

    if not per_variant_best:
        return None, 0.0

    # Vote across variants on the value string.
    votes = Counter(c.value for c in per_variant_best)
    winning_value, winning_count = votes.most_common(1)[0]
    agreement = winning_count / max(len(pooled_lines), 1)
    # Pick the FieldExtraction with the highest regex_prior among those that
    # voted for the winning value, so we surface the cleanest provenance.
    representative = max(
        (c for c in per_variant_best if c.value == winning_value),
        key=lambda c: c.regex_prior,
    )
    return representative, agreement


def _aggregate_instructions(
    pooled_lines: list[list[str]],
) -> tuple[Optional[FieldExtraction], float]:
    """Instructions accumulate (deduped, joined). Confidence is the fraction
    of variant pools that contributed at least one matching line."""
    contributing_pools = 0
    seen = set()
    ordered: list[str] = []
    for pool in pooled_lines:
        had_one = False
        for line in pool:
            cand = extract_instructions(line)
            if cand and cand.value not in seen:
                seen.add(cand.value)
                ordered.append(cand.value)
                had_one = True
            elif cand:
                had_one = True
        if had_one:
            contributing_pools += 1
    if not ordered:
        return None, 0.0
    joined = " | ".join(ordered)
    agreement = contributing_pools / max(len(pooled_lines), 1)
    return FieldExtraction(joined, 0.85, joined, "extract_instructions"), agreement


def _enrich_from_entry(
    fields: dict[str, FieldExtraction],
    confidences: dict[str, float],
    entry: NDCEntry,
) -> None:
    """Fill blank fields from an FDA NDC Directory entry. Never overwrites
    OCR finds — printed label always beats database guess. DB-derived fields
    get prior=0.85 and agreement=1.0 (the directory is deterministic)."""
    src = f"NDC_DB[{entry.product_ndc}]"

    if "brand" not in fields and entry.proprietary_name:
        if not _is_descriptive_proprietary(
            entry.proprietary_name, entry.nonproprietary_name
        ):
            fields["brand"] = FieldExtraction(
                entry.proprietary_name, 0.85, src, "ndc_db_enrichment:brand",
            )
            confidences["brand"] = 1.0
    if "product" not in fields and entry.nonproprietary_name:
        fields["product"] = FieldExtraction(
            entry.nonproprietary_name, 0.85, src, "ndc_db_enrichment:product",
        )
        confidences["product"] = 1.0
    if "mfg" not in fields:
        short = entry.normalized_manufacturer()
        if short:
            fields["mfg"] = FieldExtraction(
                short, 0.85, src, "ndc_db_enrichment:mfg",
            )
            confidences["mfg"] = 1.0
    if "strength" not in fields:
        s = entry.display_strength()
        if s:
            fields["strength"] = FieldExtraction(
                s, 0.85, src, "ndc_db_enrichment:strength",
            )
            confidences["strength"] = 1.0


def _build_ocrfields(
    fields: dict[str, FieldExtraction],
    agreements: dict[str, float],
    raw_texts: list[str],
) -> tuple[OCRFields, dict[str, FieldExtraction]]:
    """Materialize the OCRFields dataclass. Final per-field confidence is
    regex_prior × ensemble_agreement, clipped to [0, 1]."""
    kwargs: dict = {"raw_texts": raw_texts}
    for name, extraction in fields.items():
        agreement = agreements.get(name, 1.0)
        confidence = min(1.0, extraction.regex_prior * agreement)
        kwargs[name] = extraction.value
        kwargs[f"{name}_confidence"] = round(confidence, 3)
    return OCRFields(**kwargs), fields


def extract_fields(
    lines: Iterable[str],
    *,
    variant_pools: Optional[Iterable[list[str]]] = None,
    directory: Optional[NDCDirectory] = None,
    enrich_from_ndc: bool = True,
    auto_load_directory: bool = True,
) -> tuple[OCRFields, dict[str, FieldExtraction]]:
    """Extract structured fields from OCR text.

    Args:
        lines: pooled OCR text from all variants — used as the baseline text
            for regex extraction. Pass an empty iterable if you're using
            variant_pools and have no flat fallback.
        variant_pools: optional list of per-variant text-line lists. When
            supplied, ensemble voting is performed across pools and the
            agreement fraction multiplies into each field's confidence.
            When None, lines is treated as a single pool (agreement=1.0).
        directory: pre-loaded NDCDirectory for mfg/brand/product extraction
            and NDC enrichment. If None and auto_load_directory=True, the
            module-level singleton is loaded lazily.
        enrich_from_ndc: when True, fill blank fields from the NDC database
            using the OCR'd NDC as the key.
        auto_load_directory: lazy-load the singleton on first use; set False
            in environments without the CSV.

    Returns:
        (fields, audit) where audit maps each populated field to its
        FieldExtraction (value, regex_prior, source_line, source).
    """
    if directory is None and auto_load_directory:
        try:
            directory = get_directory()
        except (FileNotFoundError, OSError):
            directory = None

    flat_lines = [normalize_line(ln) for ln in lines if ln and ln.strip()]
    if variant_pools is None:
        pools = [flat_lines]
    else:
        pools = [
            [normalize_line(ln) for ln in pool if ln and ln.strip()]
            for pool in variant_pools
        ]
        # Ensure flat_lines reflects everything the caller saw.
        if not flat_lines:
            flat_lines = [ln for pool in pools for ln in pool]

    best: dict[str, FieldExtraction] = {}
    agreements: dict[str, float] = {}

    # Pass 1: simple extractors with ensemble voting.
    for fname, fn in SIMPLE_EXTRACTORS.items():
        cand, agreement = _aggregate_field(fname, fn, pools)
        if cand is not None:
            best[fname] = cand
            agreements[fname] = agreement

    # Pass 2: directory-backed extractors with ensemble voting.
    for fname, fn in DIRECTORY_EXTRACTORS.items():
        cand, agreement = _aggregate_field(fname, fn, pools, directory)
        if cand is not None:
            best[fname] = cand
            agreements[fname] = agreement

    # Pass 3: instructions (accumulate).
    instr_cand, instr_agreement = _aggregate_instructions(pools)
    if instr_cand is not None:
        best["instructions"] = instr_cand
        agreements["instructions"] = instr_agreement

    # Pass 4: NDC database enrichment.
    if enrich_from_ndc and directory is not None and "ndc" in best:
        # Only enrich when the ndc final confidence (prior × agreement) >= 0.7,
        # consistent with v1 behavior.
        ndc_conf = best["ndc"].regex_prior * agreements.get("ndc", 1.0)
        if ndc_conf >= 0.70:
            entry = directory.lookup_ndc(best["ndc"].value)
            if entry is not None:
                _enrich_from_entry(best, agreements, entry)

    # Pass 5: labeler-code enrichment — populate mfg from the NDC prefix when
    # the full NDC-database lookup didn't yield a manufacturer. labelers.json
    # covers ~1 800 labelers by their 4/5-digit code and loads in milliseconds,
    # so this fallback fires even when the large FDA CSV is unavailable.
    if "ndc" in best and "mfg" not in best:
        try:
            mfg_name = get_labeler_directory().lookup_from_ndc(best["ndc"].value)
            if mfg_name:
                best["mfg"] = FieldExtraction(
                    mfg_name, 0.85, best["ndc"].value, "labeler_json:mfg",
                )
                agreements["mfg"] = 1.0
        except Exception:
            pass

    return _build_ocrfields(best, agreements, flat_lines)


# ===========================================================================
# Layer A: image -> text (variant ensemble)
# ===========================================================================
def _preprocess_variants(crop: Image.Image) -> list[Image.Image]:
    """Generate a small set of preprocessed versions targeting different OCR
    failure modes: faded printing, sub-legibility small text, colored labels."""
    variants = [crop]
    variants.append(ImageEnhance.Contrast(crop).enhance(1.6))
    variants.append(ImageEnhance.Sharpness(crop).enhance(2.5))
    gray = ImageOps.autocontrast(ImageOps.grayscale(crop)).convert("RGB")
    variants.append(gray)
    return variants


def _rotation_variants(crop: Image.Image) -> list[tuple[int, Image.Image]]:
    """0/90/180/270 degree rotations for knocked-over vials."""
    return [(angle, crop.rotate(angle, expand=True)) for angle in (0, 90, 180, 270)]


def _parse_gs1_payload(payload: str) -> list[str]:
    """Parse GS1-128 application identifiers into labeled text lines.

    Handles the parenthesized AI format: "(01)00312345678906(17)261231(10)ABC123"
    AI 01 = GTIN-14 (contains NDC for US drugs as digits 2-13 in 5-4-2 format)
    AI 10 = lot/batch number
    AI 17 = expiry date YYMMDD
    """
    matches = re.findall(r'\((\d{2,4})\)([^(]+)', payload)
    if not matches:
        return []
    lines: list[str] = []
    for ai, val in matches:
        val = val.strip()
        if ai == '01':  # GTIN-14 -> NDC (5-4-2 from digits 2-13, drop check digit)
            digits = re.sub(r'\D', '', val)
            if len(digits) == 14:
                ndc = f"{digits[1:6]}-{digits[6:10]}-{digits[10:12]}"
                lines.append(f"NDC {ndc}")
        elif ai == '10':  # Lot / batch
            lines.append(f"LOT {val}")
        elif ai == '17' and len(val.strip()) == 6:  # Expiry YYMMDD
            v = val.strip()
            lines.append(f"EXP {v[2:4]}/20{v[:2]}")  # MM/YYYY
    return lines


def _barcode_lines(crop: Image.Image) -> list[str]:
    """Best-effort barcode decode. Optional dependency: pyzbar + zbar.

    Decodes GS1-128 / Code 128 / DataMatrix barcodes and returns labeled text
    lines ("NDC …", "LOT …", "EXP …") so the existing field extractors pick
    them up without a separate code path. Falls back to a raw "BARCODE …" line
    for payloads that don't match the GS1 AI format.
    """
    if not _BARCODE_AVAILABLE or _decode_barcodes is None:
        return []
    try:
        decoded = _decode_barcodes(crop.convert("RGB"))
    except Exception:
        return []
    lines: list[str] = []
    for item in decoded:
        try:
            payload = item.data.decode("utf-8", errors="ignore").strip()
        except Exception:
            continue
        if not payload:
            continue
        gs1 = _parse_gs1_payload(payload)
        if gs1:
            lines.extend(gs1)
        else:
            lines.append(f"BARCODE {payload}")
    return lines


class FieldExtractor:
    """Image -> structured fields via OCR variant ensemble.

    Initialize once, reuse across many crops. The reader is the expensive
    object; extracting one crop is ~16 OCR passes (4 rotations × 4
    preprocessings) plus negligible regex work.
    """

    def __init__(
        self,
        lang: str = "en",
        use_gpu: bool | None = None,
        directory: Optional[NDCDirectory] = None,
        auto_load_directory: bool = True,
    ) -> None:
        if not _EASYOCR_AVAILABLE:
            raise ImportError("EasyOCR not installed. Run: pip install easyocr")
        if use_gpu is None:
            try:
                import torch
                use_gpu = torch.cuda.is_available()
            except ImportError:
                use_gpu = False
        self.reader = easyocr.Reader([lang], gpu=use_gpu, verbose=False)
        self._cache: dict[tuple[str, bool, bool], OCRFields] = {}

        if directory is None and auto_load_directory:
            try:
                directory = get_directory()
            except (FileNotFoundError, OSError):
                directory = None
        self.directory = directory

    def extract(
        self,
        crop: Image.Image,
        try_rotations: bool = True,
        enrich_from_ndc: bool = True,
    ) -> OCRFields:
        """Run the full image -> structured fields pipeline on one crop.

        Each (rotation × preprocessing) variant becomes its own pool of OCR
        lines, and the structured-field aggregator votes across pools. A small
        in-memory cache avoids re-OCRing repeated identical crops in one run."""
        try:
            from detection.image_ops import image_fingerprint
        except ImportError:
            try:
                from ..detection.image_ops import image_fingerprint
            except ImportError:
                image_fingerprint = None  # type: ignore[assignment]

        cache_key = None
        if image_fingerprint is not None:
            cache_key = (image_fingerprint(crop), try_rotations, enrich_from_ndc)
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached

        barcode_pool = _barcode_lines(crop)

        # Pre-extract NDC from barcode data before the general ensemble runs.
        # This gives us a high-confidence barcode_ndc field that the matcher
        # weights more heavily than OCR-inferred NDC.
        barcode_ndc_val: str | None = None
        if barcode_pool:
            for line in barcode_pool:
                ndc_ext = extract_ndc(line)
                if ndc_ext is not None:
                    barcode_ndc_val = ndc_ext.value
                    break

        if try_rotations:
            base_inputs = [img for _, img in _rotation_variants(crop)]
        else:
            base_inputs = [crop]

        # One pool per variant.
        pools: list[list[str]] = []
        for img in base_inputs:
            for variant in _preprocess_variants(img):
                pools.append(self._ocr_one(variant))

        if barcode_pool:
            pools.insert(0, barcode_pool)

        flat_lines = [ln for pool in pools for ln in pool]
        fields, _ = extract_fields(
            flat_lines,
            variant_pools=pools,
            directory=self.directory,
            enrich_from_ndc=enrich_from_ndc,
            auto_load_directory=False,
        )

        # Attach barcode_ndc. When the OCR-inferred NDC is absent or weak,
        # adopt the barcode value for the ndc field too.
        if barcode_ndc_val is not None:
            fields.barcode_ndc = barcode_ndc_val
            fields.barcode_ndc_confidence = 0.99
            if fields.ndc is None or fields.ndc_confidence < 0.75:
                fields.ndc = barcode_ndc_val
                fields.ndc_confidence = 0.99

        if cache_key is not None:
            self._cache[cache_key] = fields
        return fields

    def _ocr_one(self, img: Image.Image) -> list[str]:
        """OCR a single PIL image; return detected text strings in reading order.

        EasyOCR returns (bbox, text, confidence) tuples. We sort by top-left
        corner so that _expand_with_adjacents correctly joins fragments that
        are physically adjacent on the label (e.g. "2.5" + "g" → "2.5 g").
        Per-detection confidence is discarded — the variant-ensemble vote is a
        more reliable signal than EasyOCR's overconfident per-box numbers."""
        arr = np.array(img)
        results = self.reader.readtext(arr, detail=1)
        if not results:
            return []
        # Sort top-to-bottom then left-to-right using the minimum y and x of
        # the four bbox corner points so rotated/skewed boxes sort correctly.
        results.sort(key=lambda r: (min(p[1] for p in r[0]), min(p[0] for p in r[0])))
        return [text for (_, text, _) in results]


# ===========================================================================
# Glue for the upstream detection pipeline
# ===========================================================================
def extract_fields_for_detection(
    detection: dict,
    ocr_lines: Iterable[str],
    *,
    variant_pools: Optional[Iterable[list[str]]] = None,
    directory: Optional[NDCDirectory] = None,
    review_threshold: float = 0.4,
) -> dict:
    """Attach structured OCR results to a detection dict from the upstream
    cleanup pipeline."""
    fields, audit = extract_fields(
        ocr_lines,
        variant_pools=variant_pools,
        directory=directory,
    )
    out = dict(detection)
    out["ocr_fields"] = asdict(fields)
    out["ocr_audit"] = {
        k: {
            "value": v.value,
            "regex_prior": v.regex_prior,
            "source_line": v.source_line,
            "source": v.source,
        }
        for k, v in audit.items()
    }
    out["ocr_needs_review"] = fields.needs_review(review_threshold)
    return out


# ===========================================================================
# CLI for one-shot crop testing
# ===========================================================================
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m pharmacy_pipeline.ocr.ocr_fields <crop_image_path>")
        sys.exit(1)

    img = Image.open(sys.argv[1]).convert("RGB")
    fields = FieldExtractor().extract(img)
    print(f"Lot:          {fields.lot} (conf={fields.lot_confidence:.2f})")
    print(f"Exp:          {fields.exp} (conf={fields.exp_confidence:.2f})")
    print(f"NDC:          {fields.ndc} (conf={fields.ndc_confidence:.2f})")
    print(f"Mfg:          {fields.mfg} (conf={fields.mfg_confidence:.2f})")
    print(f"Product:      {fields.product} (conf={fields.product_confidence:.2f})")
    print(f"Brand:        {fields.brand} (conf={fields.brand_confidence:.2f})")
    print(f"Strength:     {fields.strength} (conf={fields.strength_confidence:.2f})")
    print(f"Volume (mL):  {fields.vol_ml} (conf={fields.vol_ml_confidence:.2f})")
    print(f"MDV/SDV:      {fields.mdv} (conf={fields.mdv_confidence:.2f})")
    print(f"Instructions: {fields.instructions} "
          f"(conf={fields.instructions_confidence:.2f})")
    if fields.needs_review():
        print("[FLAGGED FOR REVIEW — low confidence on at least one field]")
