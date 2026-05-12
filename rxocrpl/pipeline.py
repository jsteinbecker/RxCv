"""
Pipeline Orchestrator
=====================
Wire detection -> cleanup -> OCR -> NDC enrichment -> embedding -> reference
matching into one entry point.

Domain assumption:
    The first image in the input list is the COMPLETE INVENTORY.
    Subsequent images contain subsets of that inventory, optionally mixed
    with out-of-inventory items (syringes, filters, etc.) that should
    be flagged but not matched to any reference component.

Stages:
    1. Detect across all images (Grounding DINO + SAM 2).
    2. Clean: class-agnostic NMS, frame-coverage filter, reflection filter.
       (See detection/clean_and_match.py)
    3. OCR each surviving crop -> Lot/NDC/Exp + brand/mfg/product (FieldExtractor).
    4. Enrich via FDA NDC Directory (attaches drug/strength/manufacturer).
    5. Embed each crop (DINOv2).
    6. Reference matching via Hungarian assignment over embeddings + OCR fields.

Usage (CLI):
    python -m pharmacy_pipeline.pipeline reference.png subsequent1.png ...

The first positional argument is treated as the reference image.

Usage (Python):
    from pharmacy_pipeline.pipeline import Pipeline
    p = Pipeline()
    result = p.process(["reference.png", "subsequent1.png"])
    print(result.summary())
"""

from __future__ import annotations

import base64
import io
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    try:
        from .order import Order
    except ImportError:
        from order import Order  # type: ignore[assignment]

import numpy as np
from PIL import Image as PILImage
from PIL import ImageDraw, ImageFont

try:
      from .detection.clean_and_match import clean_detections, resolve_product
      from .detection.detection import Detection, Detector
      from .detection.embedding import Embedder
      from .detection.image_ops import canonicalize_crop
      from .detection.matching import ReferenceMatch, match_against_reference, summarize
      from .ocr.ndc_directory import NDCDirectory, get_directory
      from .ocr.ocr_fields import FieldExtractor, OCRFields, extract_ndc
except ImportError:
      from detection.clean_and_match import clean_detections, resolve_product
      from detection.detection import Detection, Detector
      from detection.embedding import Embedder
      from detection.image_ops import canonicalize_crop
      from detection.matching import ReferenceMatch, match_against_reference, summarize
      from ocr.ndc_directory import NDCDirectory, get_directory
      from ocr.ocr_fields import FieldExtractor, OCRFields, extract_ndc

# Best-effort font for annotation labels — falls back to PIL's bitmap default.
try:
      _LABEL_FONT = ImageFont.load_default(size=9)
except TypeError:
      _LABEL_FONT = ImageFont.load_default()

# Colors keyed by detection role.
_COLOR_REF = (30, 120, 255)  # blue — reference image slot
_COLOR_MATCH = (20, 190, 60)  # green — matched to inventory
_COLOR_OOI = (220, 40, 40)  # red — out-of-inventory
_COLOR_EXCESS = (255, 140, 0)  # orange — excess under certification
_COLOR_FORCED = (180, 130, 30)  # amber — assigned but sub-threshold (forced)
_COLOR_UNKNOWN = (160, 160, 160)  # gray — no assignment info


def _det_color(
          det: Detection,
          assignment,  # Assignment | None
          reference_image: str,
) -> tuple[int, int, int]:
      if assignment is None:
            return _COLOR_UNKNOWN
      if det.source_image == reference_image:
            return _COLOR_REF
      if assignment.slot_id is None:
            # Excess-under-certification looks different from normal OOI: it's a
            # *contradiction* with what the user certified rather than an expected
            # foreign object, and reviewers should triage it differently.
            return _COLOR_EXCESS if assignment.excess_under_certification else _COLOR_OOI
      if assignment.forced:
            return _COLOR_FORCED
      return _COLOR_MATCH


@dataclass
class PipelineResult:
      """Everything the pipeline produced for one batch of images.
    :var detections: Detections produced by the detector.
    :var fields: OCRFields extracted from the images.
    :var embeddings: Embeddings of the images.
    :var match: ReferenceMatch between the images and the reference inventory.
    :var images_processed: List of image paths that were processed.
    :var reference_image: Path to the reference image.
    :var rejected_frame: List of frame-coverage rejects.
    :var rejected_reflection: List of reflection rejects.
    :var nms_merge_log: Audit trail for non-maximum suppression merges.
    :var enrichment: NDC-enrichment side data, keyed by instance_id.
    :var certified_subset_in_inventory: True when the run was made under the
        user's certification that every item in the subsequent images is
        contained in the reference inventory image. This relaxes the matcher's
        score threshold so that shape/visual/count cues can fill slots that
        weak OCR alone wouldn't justify.
    """

      detections: list[Detection]
      fields: list[OCRFields]
      embeddings: np.ndarray
      match: ReferenceMatch
      images_processed: list[str] = field(default_factory=list)
      reference_image: str = ""
      # Cleanup audit trail
      rejected_frame: list[dict] = field(default_factory=list)
      rejected_reflection: list[dict] = field(default_factory=list)
      nms_merge_log: list[dict] = field(default_factory=list)
      # NDC-enrichment side data, keyed by instance_id
      enrichment: dict[int, dict] = field(default_factory=dict)
      # Certification flag (see class docstring)
      certified_subset_in_inventory: bool = False

      def detection_by_id(self, detection_id: int) -> Detection:
            for d in self.detections:
                  if d.instance_id == detection_id:
                        return d
            raise KeyError(detection_id)

      def fields_by_id(self, detection_id: int) -> OCRFields:
            for d, f in zip(self.detections, self.fields):
                  if d.instance_id == detection_id:
                        return f
            raise KeyError(detection_id)

      def counts_by_image(self) -> dict[str, dict[str, int]]:
            """Per-image instance counts, broken down by detected class."""
            out: dict[str, dict[str, int]] = {}
            for d in self.detections:
                  per_image = out.setdefault(d.source_image, {})
                  per_image[d.class_label] = per_image.get(d.class_label, 0) + 1
            return out

      def to_dict(self) -> dict:
            """Serializable summary. Excludes mask/crop/embedding arrays."""
            return {
                  "reference_image": self.reference_image,
                  "images": self.images_processed,
                  "n_detections": len(self.detections),
                  "certified_subset_in_inventory": self.certified_subset_in_inventory,
                  "counts_by_image": self.counts_by_image(),
                  "cleanup": {
                        "rejected_frame": self.rejected_frame,
                        "rejected_reflection": self.rejected_reflection,
                        "nms_merge_log": self.nms_merge_log,
                  },
                  "reference_inventory": [
                        {
                              "slot_id": s.slot_id,
                              "class_label": s.class_label,
                              "brand": s.brand,
                              "strength": s.strength,
                              "lot": s.lot,
                              "ndc": s.ndc,
                              "exp": s.exp,
                              "description": s.describe(),
                        }
                        for s in self.match.slots
                  ],
                  "assignments": [
                        {
                              "detection_id": a.detection_id,
                              "source_image": a.source_image,
                              "slot_id": a.slot_id,
                              "score": round(a.score, 3),
                              "out_of_inventory": a.slot_id is None and not a.excess_under_certification,
                              "excess_under_certification": a.excess_under_certification,
                              "forced": a.forced,
                        }
                        for a in self.match.assignments
                  ],
                  "summary": summarize(self.match),
                  "detections": [
                        {
                              "instance_id": d.instance_id,
                              "source_image": d.source_image,
                              "class_label": d.class_label,
                              "score": round(d.score, 3),
                              "bbox": d.bbox,
                              "mask_area": d.mask_area(),
                              "lot": f.lot,
                              "exp": f.exp,
                              "ndc": f.ndc,
                              "barcode_ndc": f.barcode_ndc,
                              "brand": f.brand,
                              "drug": f.product,
                              "strength": f.strength,
                              "ocr_needs_review": f.needs_review(),
                              "enrichment": self.enrichment.get(d.instance_id, {}),
                        }
                        for d, f in zip(self.detections, self.fields)
                  ],
            }

      # ------------------------------------------------------------------
      # Markdown report
      # ------------------------------------------------------------------

      def _render_annotated_image(self, source_image: str) -> str:
            """Return a base64-encoded PNG of *source_image* with segmentation
        mask overlays (semi-transparent fills) and labelled bounding boxes.

        Colour legend:
            blue  — reference-image slot
            green — subsequent detection matched to an inventory slot
            red   — subsequent detection flagged out-of-inventory
        """
            img = PILImage.open(source_image).convert("RGB")
            img_arr = np.array(img, dtype=np.float32)
            H, W = img_arr.shape[:2]

            assignment_by_det = {a.detection_id: a for a in self.match.assignments}

            # --- Pass 1: semi-transparent mask fill (alpha composite via numpy) ---
            overlay = np.zeros((H, W, 4), dtype=np.float32)
            for d in self.detections:
                  if d.source_image != source_image:
                        continue
                  if d.mask.shape != (H, W):
                        continue
                  a = assignment_by_det.get(d.instance_id)
                  r, g, b = _det_color(d, a, self.reference_image)
                  overlay[d.mask, 0] = r
                  overlay[d.mask, 1] = g
                  overlay[d.mask, 2] = b
                  overlay[d.mask, 3] = 70.0  # ~27% opacity

            alpha = overlay[:, :, 3:4] / 255.0
            blended = (img_arr * (1 - alpha) + overlay[:, :, :3] * alpha).clip(0, 255).astype(np.uint8)
            img = PILImage.fromarray(blended)

            # --- Pass 2: bounding boxes + labels ---
            draw = ImageDraw.Draw(img)
            lw = max(2, min(H, W) // 400)

            for d in self.detections:
                  if d.source_image != source_image:
                        continue
                  a = assignment_by_det.get(d.instance_id)
                  color = _det_color(d, a, self.reference_image)

                  x1, y1, x2, y2 = d.bbox
                  draw.rectangle([x1, y1, x2, y2], outline=color, width=lw)

                  if a is not None and d.source_image == self.reference_image:
                        label = f"#{d.instance_id} s{a.slot_id}"
                  elif a is not None and a.slot_id is not None:
                        label = f"#{d.instance_id} s{a.slot_id} {a.score:.2f}"
                  elif a is not None:
                        label = f"#{d.instance_id} OOI {a.score:.2f}"
                  else:
                        label = f"#{d.instance_id} {d.class_label}"

                  # Measure label to size the background pill.
                  try:
                        bbox_t = draw.textbbox((0, 0), label, font=_LABEL_FONT)
                        tw, th = bbox_t[2] - bbox_t[0], bbox_t[3] - bbox_t[1]
                  except AttributeError:
                        tw, th = len(label) * 7, 13

                  pad = 2
                  bg_x1 = x1
                  bg_y1 = max(0, y1 - th - pad * 2)
                  bg_x2 = min(W, x1 + tw + pad * 2)
                  bg_y2 = y1
                  draw.rectangle([bg_x1, bg_y1, bg_x2, bg_y2], fill=color)
                  draw.text((bg_x1 + pad, bg_y1 + pad), label, fill=(255, 255, 255), font=_LABEL_FONT)

            # Downscale very large images to keep the .md file manageable.
            max_width = 1200
            if img.width > max_width:
                  ratio = max_width / img.width
                  img = img.resize((max_width, int(img.height * ratio)), PILImage.LANCZOS)

            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=True)
            return base64.b64encode(buf.getvalue()).decode()

      def to_markdown(self, run_time: Optional[datetime] = None) -> str:
            """Dense Markdown report with annotated segmentation visualisations.

        Embeds annotated images as base64 data URIs so the .md is self-contained.
        """
            ts = (run_time or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
            assignment_by_det = {a.detection_id: a for a in self.match.assignments}
            n_slots = len(self.match.slots)

            lines: list[str] = []

            # ── Header ────────────────────────────────────────────────────────────
            cert_row = (
                      "| **Mode** | "
                      + ("**CERTIFIED SUBSET** (force-assign)" if self.certified_subset_in_inventory
                         else "Standard (threshold-gated)")
                      + " |"
            )
            lines += [
                  "# RxCV Pipeline Report",
                  "",
                  f"| | |",
                  f"|---|---|",
                  f"| **Run** | {ts} |",
                  f"| **Reference** | `{Path(self.reference_image).name}` |",
                  f"| **Images processed** | {len(self.images_processed)} |",
                  cert_row,
                  f"| **Total detections (post-cleanup)** | {len(self.detections)} |",
                  f"| **NMS merges** | {len(self.nms_merge_log)} |",
                  f"| **Frame-coverage rejects** | {len(self.rejected_frame)} |",
                  f"| **Reflection rejects** | {len(self.rejected_reflection)} |",
                  f"| **Inventory slots** | {n_slots} |",
                  "",
            ]
            if self.certified_subset_in_inventory:
                  lines += [
                        "> **Certification mode is ON.** The user has certified that every "
                        "item in the subsequent images is contained in the reference "
                        "inventory. The matcher's score threshold is bypassed — every "
                        "Hungarian-paired detection is assigned to its slot regardless "
                        "of OCR confidence. Sub-threshold pairings are flagged as "
                        "`forced`, and any detections beyond the reference slot count "
                        "are flagged as `excess_under_certification` (a contradiction "
                        "with the certification that warrants re-imaging or re-counting).",
                        "",
                  ]
            lines += [
                  "---",
                  "",
            ]

            # ── Reference Inventory ───────────────────────────────────────────────
            lines += [
                  f"## Reference Inventory ({n_slots} slots)",
                  "",
                  "| Slot | Class | Brand | Strength | Lot | NDC | Exp |",
                  "|-----:|-------|-------|----------|-----|-----|-----|",
            ]
            for s in self.match.slots:
                  lines.append(
                        f"| {s.slot_id} | {s.class_label or '—'} | "
                        f"{s.brand or '—'} | {s.strength or '—'} | "
                        f"`{s.lot or '—'}` | `{s.ndc or '—'}` | {s.exp or '—'} |"
                  )
            lines += ["", "---", ""]

            # ── Per-image sections ────────────────────────────────────────────────
            lines += ["## Results by Image", ""]

            for report in self.match.image_reports:
                  img_name = Path(report.source_image).name

                  if report.is_reference:
                        lines.append(f"### [REF] {img_name}")
                        lines.append(
                              f"{report.detection_count} detections — defines inventory"
                        )
                  else:
                        n_miss = len(report.missing_slot_ids)
                        lines.append(f"### {img_name}")
                        base_line = (
                              f"{report.detection_count} detections — "
                              f"**{report.assigned_count} matched** / "
                              f"{report.out_of_inventory_count} out-of-inventory / "
                              f"{n_miss} missing"
                        )
                        # Surface certification-mode telemetry only when it's non-zero
                        # to keep the standard-mode report uncluttered.
                        extras = []
                        if report.forced_assignment_count:
                              extras.append(f"{report.forced_assignment_count} forced")
                        if report.excess_count:
                              extras.append(f"{report.excess_count} excess")
                        if extras:
                              base_line += " (" + ", ".join(extras) + ")"
                        lines.append(base_line)
                  lines.append("")

                  # Annotated image
                  b64 = self._render_annotated_image(report.source_image)
                  lines.append(f"![{img_name}](data:image/png;base64,{b64})")
                  lines.append("")

                  # Detection table
                  img_dets = [
                        (d, f)
                        for d, f in zip(self.detections, self.fields)
                        if d.source_image == report.source_image
                  ]
                  if img_dets:
                        if report.is_reference:
                              lines += [
                                    "| ID | Class | Det score | Brand | Strength | Lot | NDC | Exp | OCR |",
                                    "|----|-------|----------:|-------|----------|-----|-----|-----|-----|",
                              ]
                              for d, f in img_dets:
                                    ocr_flag = "!" if f.needs_review() else "ok"
                                    lines.append(
                                          f"| {d.instance_id} | {d.class_label} | {d.score:.2f} | "
                                          f"{f.brand or '—'} | {f.strength or '—'} | "
                                          f"`{f.lot or '—'}` | `{f.ndc or '—'}` | {f.exp or '—'} | {ocr_flag} |"
                                    )
                        else:
                              lines += [
                                    "| ID | Class | Det score | Slot | Match score | Brand | Strength | Lot | NDC | Exp | OCR |",
                                    "|----|-------|----------:|:----:|------------:|-------|----------|-----|-----|-----|-----|",
                              ]
                              for d, f in img_dets:
                                    a = assignment_by_det.get(d.instance_id)
                                    slot_str = f"s{a.slot_id}" if (a and a.slot_id is not None) else "**OOI**"
                                    mscore = f"{a.score:.2f}" if a else "—"
                                    ocr_flag = "!" if f.needs_review() else "ok"
                                    lines.append(
                                          f"| {d.instance_id} | {d.class_label} | {d.score:.2f} | "
                                          f"{slot_str} | {mscore} | "
                                          f"{f.brand or '—'} | {f.strength or '—'} | "
                                          f"`{f.lot or '—'}` | `{f.ndc or '—'}` | {f.exp or '—'} | {ocr_flag} |"
                                    )

                  # Missing-slot summary for subsequent images
                  if not report.is_reference and report.missing_slot_ids:
                        miss_desc = Counter(
                              self.match.slots[sid].describe() for sid in report.missing_slot_ids
                        )
                        miss_str = ", ".join(
                              f"{desc}" + (f" x{cnt}" if cnt > 1 else "")
                              for desc, cnt in miss_desc.most_common()
                        )
                        lines.append("")
                        lines.append(
                              f"> **Missing ({len(report.missing_slot_ids)} slots):** {miss_str}"
                        )

                  lines += ["", "---", ""]

            # ── OCR review flags ──────────────────────────────────────────────────
            flagged = [
                  (d, f) for d, f in zip(self.detections, self.fields) if f.needs_review()
            ]
            if flagged:
                  lines += [
                        "## OCR Review Flags",
                        "",
                        "| ID | Image | Lot | Lot conf | NDC | NDC conf | Exp | Exp conf |",
                        "|----|-------|-----|:--------:|-----|:--------:|-----|:--------:|",
                  ]
                  for d, f in flagged:
                        lines.append(
                              f"| {d.instance_id} | {Path(d.source_image).name} | "
                              f"`{f.lot or '—'}` | {f.lot_confidence:.2f} | "
                              f"`{f.ndc or '—'}` | {f.ndc_confidence:.2f} | "
                              f"{f.exp or '—'} | {f.exp_confidence:.2f} |"
                        )
                  lines.append("")

            # ── Legend ────────────────────────────────────────────────────────────
            lines += [
                  "---",
                  "",
                  "**Annotation legend:**  "
                  "blue = reference slot · green = inventory match · red = out-of-inventory  ",
                  "OCR column: `ok` = all fields confident · `!` = low-confidence field, review needed",
                  "",
            ]

            return "\n".join(lines)

      def summary(self) -> str:
            """Human-readable text summary."""
            lines = []
            lines.append(f"Reference image: {Path(self.reference_image).name}")
            lines.append(f"Subsequent images: {len(self.images_processed) - 1}")
            if self.certified_subset_in_inventory:
                  lines.append(
                        "Mode: CERTIFIED SUBSET (force-assign — score threshold bypassed)"
                  )
            if self.rejected_frame or self.rejected_reflection or self.nms_merge_log:
                  lines.append(
                        f"Cleanup: {len(self.nms_merge_log)} NMS merges, "
                        f"{len(self.rejected_frame)} frame-spanning rejects, "
                        f"{len(self.rejected_reflection)} reflection rejects"
                  )
            lines.append("")

            # Inventory listing.
            lines.append(f"Reference inventory ({len(self.match.slots)} slots):")
            desc_counter = Counter(s.describe() for s in self.match.slots)
            for desc, cnt in desc_counter.most_common():
                  suffix = f" x{cnt}" if cnt > 1 else ""
                  lines.append(f"  - {desc}{suffix}")
            lines.append("")

            # Per-image breakdown.
            for report in self.match.image_reports:
                  name = Path(report.source_image).name
                  if report.is_reference:
                        lines.append(f"[REF] {name}: {report.detection_count} detections (defines inventory)")
                        continue
                  line = (
                        f"      {name}: {report.detection_count} detections -> "
                        f"{report.assigned_count} matched to inventory, "
                        f"{report.out_of_inventory_count} out-of-inventory"
                  )
                  extras = []
                  if report.forced_assignment_count:
                        extras.append(f"{report.forced_assignment_count} forced")
                  if report.excess_count:
                        extras.append(f"{report.excess_count} excess (over inventory count)")
                  if extras:
                        line += " [" + ", ".join(extras) + "]"
                  lines.append(line)
                  if report.missing_slot_ids:
                        lines.append(
                              f"        Missing from this image: {len(report.missing_slot_ids)} slot(s)"
                        )
                        # Group missing by description for readability.
                        missing_descs = Counter(
                              self.match.slots[sid].describe() for sid in report.missing_slot_ids
                        )
                        for desc, cnt in missing_descs.most_common():
                              suffix = f" x{cnt}" if cnt > 1 else ""
                              lines.append(f"          - {desc}{suffix}")

            # OCR review flags.
            flagged = [
                  (d.instance_id, d.source_image)
                  for d, f in zip(self.detections, self.fields)
                  if f.needs_review()
            ]
            if flagged:
                  lines.append("")
                  lines.append("Flagged for OCR review:")
                  for inst_id, src in flagged:
                        lines.append(f"  Instance #{inst_id} from {Path(src).name}")

            return "\n".join(lines)


def _detection_to_clean_dict(d: Detection, fields: OCRFields) -> dict:
      """Project a Detection + its OCR fields into the dict shape clean_detections expects."""
      return {
            "instance_id": d.instance_id,
            "source_image": d.source_image,
            "class_label": d.class_label,
            "score": d.score,
            "bbox": list(d.bbox),
            "mask_area": int(d.mask_area()),
            "lot": fields.lot,
            "exp": fields.exp,
            "ndc": fields.ndc,
            "_mask": d.mask,
      }


def _uf_find(parent: list[int], x: int) -> int:
      """Path-compressed union-find root lookup."""
      while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
      return x


def _propagate_ocr_to_siblings(
          detections: list[Detection],
          fields: list[OCRFields],
          size_ratio_thresh: float = 0.85,
          aspect_diff_thresh: float = 0.15,
) -> None:
      """Share lot/ndc/exp from the richest OCR read to visually similar peers.

    When multiple containers in the same image are segmented at similar scale
    and aspect ratio, but OCR only succeeds on a subset, this fills the blanks.
    Handles the common case where identical vials are misclassified as bottles
    on some instances but OCR still reads the label on one of them.

    Similarity is based on mask-area ratio and bounding-box aspect ratio;
    class label is deliberately ignored so bottle/vial confusions don't block
    propagation between obviously identical containers.

    Mutates fields in-place. Does not overwrite existing non-None values.
    Propagated confidence is 90 % of the source's to flag inferred origin.
    """
      by_image: dict[str, list[int]] = {}
      for i, d in enumerate(detections):
            by_image.setdefault(d.source_image, []).append(i)

      for img_indices in by_image.values():
            n = len(img_indices)
            if n < 2:
                  continue

            parent = list(range(n))

            for a in range(n):
                  da = detections[img_indices[a]]
                  ax1, ay1, ax2, ay2 = da.bbox
                  a_h = ay2 - ay1
                  if a_h == 0:
                        continue
                  a_aspect = (ax2 - ax1) / a_h
                  a_area = da.mask_area() or (ax2 - ax1) * a_h
                  if a_area == 0:
                        continue
                  for b in range(a + 1, n):
                        db = detections[img_indices[b]]
                        bx1, by1, bx2, by2 = db.bbox
                        b_h = by2 - by1
                        if b_h == 0:
                              continue
                        b_aspect = (bx2 - bx1) / b_h
                        b_area = db.mask_area() or (bx2 - bx1) * b_h
                        if b_area == 0:
                              continue
                        if min(a_area, b_area) / max(a_area, b_area) < size_ratio_thresh:
                              continue
                        if abs(a_aspect - b_aspect) > aspect_diff_thresh:
                              continue
                        ra, rb = _uf_find(parent, a), _uf_find(parent, b)
                        if ra != rb:
                              parent[ra] = rb

            groups: dict[int, list[int]] = {}
            for pos in range(n):
                  groups.setdefault(_uf_find(parent, pos), []).append(img_indices[pos])

            for group_idxs in groups.values():
                  if len(group_idxs) < 2:
                        continue
                  gf = [fields[i] for i in group_idxs]
                  # Rank by completeness: NDC is most valuable, then lot, then exp.
                  best = max(
                        gf,
                        key=lambda f: (f.ndc is not None) * 4 + (f.lot is not None) * 2 + (f.exp is not None),
                  )
                  if best.ndc is None and best.lot is None and best.exp is None:
                        continue
                  for f in gf:
                        if f is best:
                              continue
                        if f.ndc is None and best.ndc:
                              f.ndc = best.ndc
                              f.ndc_confidence = round(best.ndc_confidence * 0.90, 3)
                        if f.lot is None and best.lot:
                              f.lot = best.lot
                              f.lot_confidence = round(best.lot_confidence * 0.90, 3)
                        if f.exp is None and best.exp:
                              f.exp = best.exp
                              f.exp_confidence = round(best.exp_confidence * 0.90, 3)


class Pipeline:
      """End-to-end pipeline. Loads all four models once."""

      def __init__(
                self,
                detection_prompt: str = "vial . iv bag . bottle . syringe . filter .",
                device: str | None = None,
                ndc_directory: NDCDirectory | None = None,
                certified_subset_in_inventory: bool = False,
      ) -> None:
            # Note the broader default prompt — subsequent images may contain
            # syringes/filters that we *want* to detect (so we can correctly
            # label them out-of-inventory) rather than miss entirely.
            self.detection_prompt = detection_prompt
            self.detector = Detector(device=device)
            self.extractor = FieldExtractor()
            self.embedder = Embedder(device=device)
            # Lazily resolved on first use; can be overridden.
            self._ndc_directory = ndc_directory
            # Default certification mode for every process() call. The user can
            # override on a per-call basis. See Pipeline.process() for what this
            # actually changes.
            self.certified_subset_in_inventory = certified_subset_in_inventory

      @property
      def ndc_directory(self) -> NDCDirectory | None:
            """Lazy-load the FDA NDC Directory for OCR enrichment.

        Falls back to None (graceful skip) if the CSV isn't present.
        """
            if self._ndc_directory is None:
                  try:
                        self._ndc_directory = get_directory()
                  except (FileNotFoundError, OSError):
                        self._ndc_directory = None
            return self._ndc_directory

      def process(
                self,
                image_paths: list[str | Path],
                certified_subset_in_inventory: bool | None = None,
                order: Order | None = None,
      ) -> PipelineResult:
            """Run all stages on a batch of images.

        The FIRST image is treated as the reference inventory; subsequent
        images are matched against it.

        Args:
            image_paths: First entry is the reference inventory image; the rest
                are subsequent images to verify.
            certified_subset_in_inventory: Per-call override of the constructor
                default. When True, the user certifies that every item in the
                subsequent images is contained in the reference inventory; the
                matcher's score threshold is bypassed and the Hungarian solver's
                optimal pairing is trusted regardless of OCR confidence. Pass
                None (the default) to inherit the value set on the Pipeline
                instance.
            order: Optional Order object. When provided, the pipeline extracts
                expected NDC codes from order.components, order.scanned_barcodes,
                and order.expected_components (resolved via the NDC directory).
                These are used as *priority_ndcs* in the matcher: any (sub, ref)
                detection pair whose NDCs agree AND appear in the expected set
                receives a strong score boost, improving accuracy when the OCR
                signal is weak.
        """
            if not image_paths:
                  raise ValueError("at least one image (the reference) is required")

            # Resolve the per-call override against the constructor default.
            certified = (
                  self.certified_subset_in_inventory
                  if certified_subset_in_inventory is None
                  else certified_subset_in_inventory
            )

            # Collect priority NDCs from the attached Order (if any).
            # These come from three sources (in descending confidence):
            #   1. component.package_ndc / component.product.product_ndc
            #   2. scanned_barcodes (physically scanned before imaging)
            #   3. expected_components strings resolved via the NDC directory
            priority_ndcs: frozenset[str] = frozenset()
            if order is not None:
                  _ndc_set: set[str] = set()
                  for comp in getattr(order, "components", []):
                        pkg = getattr(comp, "package_ndc", None)
                        if pkg:
                              _ndc_set.add(pkg.strip())
                        product = getattr(comp, "product", None)
                        if product:
                              prod = getattr(product, "product_ndc", None)
                              if prod:
                                    _ndc_set.add(prod.strip())
                  for bc in getattr(order, "scanned_barcodes", []):
                        if bc:
                              # Try to extract a formatted NDC from the raw barcode string.
                              ext = extract_ndc(f"NDC {bc.strip()}")
                              if ext is not None:
                                    _ndc_set.add(ext.value)
                              else:
                                    _ndc_set.add(bc.strip())
                  resolve_fn = getattr(order, "resolve_expected_ndcs", None)
                  if resolve_fn is not None:
                        _ndc_set.update(resolve_fn(self.ndc_directory))
                  priority_ndcs = frozenset(_ndc_set)

            image_paths = [str(p) for p in image_paths]
            reference_image = image_paths[0]

            # Stage 1: detect across all images, accumulating into one flat list.
            # Reassign instance_ids globally so they're unique across the batch.
            all_detections: list[Detection] = []
            image_dims: dict[str, tuple[int, int]] = {}
            for img_path in image_paths:
                  per_image = self.detector.detect(img_path, prompt=self.detection_prompt)
                  for d in per_image:
                        d.instance_id = len(all_detections)
                        all_detections.append(d)
                  # Record image dimensions for the cleanup pass. Detector outputs
                  # masks shaped (H, W); sample any one detection from this image.
                  for d in per_image:
                        if d.mask is not None:
                              h, w = d.mask.shape[:2]
                              image_dims[img_path] = (w, h)
                              break

            if not all_detections:
                  return self._empty_result(image_paths, reference_image, certified=certified)

            # Stage 2a: build mask-aware canonical crops, then OCR each crop.
            # We need OCR fields *before* cleanup so that NMS can merge OCR fields
            # from absorbed duplicates. Canonical crops are also reused by the
            # embedding stage to stabilize rotated/front-back views.
            source_cache: dict[str, PILImage.Image] = {}
            for d in all_detections:
                  img = source_cache.get(d.source_image)
                  if img is None:
                        img = PILImage.open(d.source_image).convert("RGB")
                        source_cache[d.source_image] = img
                  d.metadata["canonical_crop"] = canonicalize_crop(img, d.bbox, d.mask)

            all_fields = [self.extractor.extract(d.metadata.get("canonical_crop", d.crop)) for d in all_detections]

            # Stage 2b: cleanup — NMS, frame-coverage filter, reflection filter.
            # Operates on dicts; survivors are filtered back to Detection list by id.
            det_dicts = [
                  _detection_to_clean_dict(d, f)
                  for d, f in zip(all_detections, all_fields)
            ]
            cleaned, rejected_frame, rejected_reflection, merge_log = clean_detections(
                  det_dicts, image_dims
            )
            survivor_ids = {d["instance_id"] for d in cleaned}

            # NMS may have merged OCR fields from absorbed peers into the survivor.
            # Apply those merges back onto the Detection-side OCRFields.
            cleaned_by_id = {d["instance_id"]: d for d in cleaned}
            survivors: list[Detection] = []
            survivor_fields: list[OCRFields] = []
            for d, f in zip(all_detections, all_fields):
                  if d.instance_id not in survivor_ids:
                        continue
                  merged = cleaned_by_id[d.instance_id]
                  # Backfill OCR fields if the cleanup pass merged in a peer's value
                  # (clean_detections fills missing lot/ndc/exp from absorbed dets).
                  if f.lot is None and merged.get("lot"):
                        f.lot = merged["lot"]
                  if f.ndc is None and merged.get("ndc"):
                        f.ndc = merged["ndc"]
                  if f.exp is None and merged.get("exp"):
                        f.exp = merged["exp"]
                  survivors.append(d)
                  survivor_fields.append(f)

            all_detections = survivors
            all_fields = survivor_fields

            if not all_detections:
                  return self._empty_result(
                        image_paths,
                        reference_image,
                        rejected_frame=rejected_frame,
                        rejected_reflection=rejected_reflection,
                        merge_log=merge_log,
                        certified=certified,
                  )

            # Stage 2c: propagate OCR fields to visually similar siblings.
            # Fills lot/ndc/exp on detections where OCR failed but an identical
            # container in the same image was read successfully. Must run before
            # enrichment so that propagated NDCs benefit from database lookups.
            _propagate_ocr_to_siblings(all_detections, all_fields)

            # Stage 3: NDC-directory enrichment (drug, strength, manufacturer, form).
            # Falls through to the legacy hardcoded NDC_DB if directory is unavailable.
            enrichment: dict[int, dict] = {}
            directory = self.ndc_directory
            for d, f in zip(all_detections, all_fields):
                  entry = None
                  if f.ndc and directory is not None:
                        entry = directory.lookup_ndc(f.ndc)
                  if entry is not None:
                        enrichment[d.instance_id] = {
                              "source": "ndc_directory",
                              "ndc": f.ndc,
                              "brand": entry.proprietary_name,
                              "generic": entry.nonproprietary_name,
                              "strength": entry.display_strength(),
                              "manufacturer": entry.labeler,
                              "dosage_form": entry.dosage_form,
                        }
                  else:
                        # Legacy resolution_source path — uses the hardcoded NDC_DB/LOT_DB
                        # in clean_and_match. This is a graceful fallback for offline use.
                        d_dict = _detection_to_clean_dict(d, f)
                        resolved = resolve_product(d_dict)
                        if resolved.get("resolution_source"):
                              enrichment[d.instance_id] = {
                                    "source": resolved["resolution_source"],
                                    "ndc": f.ndc,
                                    "drug": resolved.get("drug"),
                                    "strength": resolved.get("strength"),
                                    "manufacturer": resolved.get("manufacturer"),
                              }
                              # If the database disagrees with the detector on form
                              # (e.g. detector said 'bottle' but NDC says it's a vial),
                              # apply the correction the same way clean_and_match would.
                              if resolved.get("class_label_corrected"):
                                    d.class_label = resolved["class_label"]

            # Stage 4: embed each canonical crop in one batched call.
            embeddings = self.embedder.embed_batch([
                  d.metadata.get("canonical_crop", d.crop) for d in all_detections
            ])

            # Stage 5: reference-based matching.
            match = match_against_reference(
                  detections=all_detections,
                  embeddings=embeddings,
                  field_records=all_fields,
                  reference_image=reference_image,
                  force_assign=certified,
                  priority_ndcs=priority_ndcs or None,
            )

            return PipelineResult(
                  detections=all_detections,
                  fields=all_fields,
                  embeddings=embeddings,
                  match=match,
                  images_processed=image_paths,
                  reference_image=reference_image,
                  rejected_frame=rejected_frame,
                  rejected_reflection=rejected_reflection,
                  nms_merge_log=merge_log,
                  enrichment=enrichment,
                  certified_subset_in_inventory=certified,
            )

      @staticmethod
      def _empty_result(
                image_paths: list[str],
                reference_image: str,
                rejected_frame: list[dict] | None = None,
                rejected_reflection: list[dict] | None = None,
                merge_log: list[dict] | None = None,
                certified: bool = False,
      ) -> PipelineResult:
            empty_match = ReferenceMatch(slots=[], assignments=[], image_reports=[])
            return PipelineResult(
                  detections=[],
                  fields=[],
                  embeddings=np.zeros((0, 0), dtype=np.float32),
                  match=empty_match,
                  images_processed=image_paths,
                  reference_image=reference_image,
                  rejected_frame=rejected_frame or [],
                  rejected_reflection=rejected_reflection or [],
                  nms_merge_log=merge_log or [],
                  certified_subset_in_inventory=certified,
            )


if __name__ == "__main__":
      import argparse

      parser = argparse.ArgumentParser(
            prog="rxocrpl.pipeline",
            description=(
                  "RxCV pipeline: detect, OCR, embed, and match pharmacy components "
                  "across a batch of images. The FIRST image is the reference "
                  "inventory; subsequent images are matched against it."
            ),
      )
      parser.add_argument(
            "images",
            nargs="+",
            help="Reference image followed by one or more subsequent images.",
      )
      parser.add_argument(
            "--certified", "--certified-subset",
            dest="certified",
            action="store_true",
            help=(
                  "Certify that every item in the subsequent images is contained in "
                  "the reference inventory. Bypasses the matcher's score threshold "
                  "so shape/visual/count cues can fill slots that weak OCR alone "
                  "wouldn't justify. Sub-threshold pairings are flagged as 'forced' "
                  "and any over-count detections as 'excess_under_certification'."
            ),
      )
      parser.add_argument(
            "--expect", "-e",
            dest="expect",
            nargs="+",
            metavar="COMPONENT",
            help=(
                  'Expected components on the tray, as human-readable strings. '
                  'Each string is parsed for drug name, strength, and form, then '
                  'resolved to NDC codes via the FDA NDC Directory. Those NDCs '
                  'are used as priority hints in the matcher, boosting accuracy '
                  'when OCR is weak. Repeat as needed. Example: '
                  '--expect "VIAL, FUROSEMIDE 100mg/10mL, #1" "NS IVPB 100ML, #1"'
            ),
      )
      parser.add_argument(
            "--barcode", "-b",
            dest="barcodes",
            nargs="+",
            metavar="BARCODE",
            help=(
                  "Raw barcode strings physically scanned from the components "
                  "before imaging (e.g. from a handheld scanner). Each value is "
                  "parsed as an NDC and added to the priority set alongside "
                  "--expect. Example: --barcode 0409-6537-10"
            ),
      )
      args = parser.parse_args()

      # Build a lightweight order-like object when --expect or --barcode are used.
      # Importing Order here (inside __main__) is safe — no circular import risk.
      _order = None
      if args.expect or args.barcodes:
            try:
                  try:
                        from .order import Order
                  except ImportError:
                        from order import Order
                  _order = Order(
                        id=0,
                        components=[],
                        expected_components=list(args.expect or []),
                        scanned_barcodes=list(args.barcodes or []),
                  )
            except Exception as _e:
                  print(f"[warn] could not build Order for --expect/--barcode hints: {_e}")

      pipeline = Pipeline(certified_subset_in_inventory=args.certified)
      result = pipeline.process(args.images, order=_order)

      now = datetime.now()
      stamp = now.strftime("%Y%m%d_%H%M%S")

      Path("out").mkdir(exist_ok=True)

      json_path = f"out/result_{stamp}.json"
      md_path = f"out/result_{stamp}.md"

      output = json.dumps(result.to_dict(), indent=2, default=str)
      with open(json_path, "w") as f:
            f.write(output)
      print(f"JSON  -> {json_path}")

      md_content = result.to_markdown(run_time=now)
      with open(md_path, "w") as f:
            f.write(md_content)
      print(f"Report -> {md_path}")

      print()
      print(result.summary())