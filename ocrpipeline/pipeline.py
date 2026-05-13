"""
Pipeline Orchestrator
=====================
Wire detection -> OCR -> embedding -> reference matching into one entry point.

Domain assumption:
    The first image in the input list is the COMPLETE INVENTORY.
    Subsequent images contain subsets of that inventory, optionally mixed
    with out-of-inventory items (syringes, filters, etc.) that should
    be flagged but not matched to any reference component.

Usage (CLI):
    python pipeline.py reference.png subsequent1.png subsequent2.png ...

The first positional argument is treated as the reference image.

Usage (Python):
    from pipeline import Pipeline
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
from typing import Optional

import numpy as np
from PIL import Image as PILImage
from PIL import ImageDraw, ImageFont

from detection import Detection, Detector
from embedding import Embedder
from matching import ReferenceMatch, match_against_reference, summarize
from ocr import FieldExtractor, OCRFields

# Best-effort font for annotation labels — falls back to PIL's bitmap default.
try:
    _LABEL_FONT = ImageFont.load_default(size=9)
except TypeError:
    _LABEL_FONT = ImageFont.load_default()

# Colours keyed by detection role.
_COLOR_REF = (30, 120, 255)    # blue   — reference image slot
_COLOR_MATCH = (20, 190, 60)   # green  — matched to inventory
_COLOR_OOI = (220, 40, 40)     # red    — out-of-inventory
_COLOR_UNKNOWN = (160, 160, 160)  # grey — no assignment info


def _det_color(
    det: Detection,
    assignment,  # Assignment | None
    reference_image: str,
) -> tuple[int, int, int]:
    if assignment is None:
        return _COLOR_UNKNOWN
    if det.source_image == reference_image:
        return _COLOR_REF
    return _COLOR_MATCH if assignment.slot_id is not None else _COLOR_OOI


@dataclass
class PipelineResult:
    """Everything the pipeline produced for one batch of images."""

    detections: list[Detection]
    fields: list[OCRFields]
    embeddings: np.ndarray
    match: ReferenceMatch
    images_processed: list[str] = field(default_factory=list)
    reference_image: str = ""

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
            "counts_by_image": self.counts_by_image(),
            "reference_inventory": [
                {
                    "slot_id": s.slot_id,
                    "class_label": s.class_label,
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
                    "out_of_inventory": a.slot_id is None,
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
                    "ocr_needs_review": f.needs_review(),
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
        lines += [
            "# RxCV Pipeline Report",
            "",
            f"| | |",
            f"|---|---|",
            f"| **Run** | {ts} |",
            f"| **Reference** | `{Path(self.reference_image).name}` |",
            f"| **Images processed** | {len(self.images_processed)} |",
            f"| **Total detections** | {len(self.detections)} |",
            f"| **Inventory slots** | {n_slots} |",
            "",
            "---",
            "",
        ]

        # ── Reference Inventory ───────────────────────────────────────────────
        lines += [
            f"## Reference Inventory ({n_slots} slots)",
            "",
            "| Slot | Class | Lot | NDC | Exp |",
            "|-----:|-------|-----|-----|-----|",
        ]
        for s in self.match.slots:
            lines.append(
                f"| {s.slot_id} | {s.class_label or '—'} | "
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
                lines.append(
                    f"{report.detection_count} detections — "
                    f"**{report.assigned_count} matched** / "
                    f"{report.out_of_inventory_count} out-of-inventory / "
                    f"{n_miss} missing"
                )
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
                        "| ID | Class | Det score | Lot | NDC | Exp | OCR |",
                        "|----|-------|----------:|-----|-----|-----|-----|",
                    ]
                    for d, f in img_dets:
                        ocr_flag = "!" if f.needs_review() else "ok"
                        lines.append(
                            f"| {d.instance_id} | {d.class_label} | {d.score:.2f} | "
                            f"`{f.lot or '—'}` | `{f.ndc or '—'}` | {f.exp or '—'} | {ocr_flag} |"
                        )
                else:
                    lines += [
                        "| ID | Class | Det score | Slot | Match score | Lot | NDC | Exp | OCR |",
                        "|----|-------|----------:|:----:|------------:|-----|-----|-----|-----|",
                    ]
                    for d, f in img_dets:
                        a = assignment_by_det.get(d.instance_id)
                        slot_str = f"s{a.slot_id}" if (a and a.slot_id is not None) else "**OOI**"
                        mscore = f"{a.score:.2f}" if a else "—"
                        ocr_flag = "!" if f.needs_review() else "ok"
                        lines.append(
                            f"| {d.instance_id} | {d.class_label} | {d.score:.2f} | "
                            f"{slot_str} | {mscore} | "
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
            lines.append(
                f"      {name}: {report.detection_count} detections -> "
                f"{report.assigned_count} matched to inventory, "
                f"{report.out_of_inventory_count} out-of-inventory"
            )
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


class Pipeline:
    """End-to-end pipeline. Loads all four models once."""

    def __init__(
        self,
        detection_prompt: str = "vial . iv bag . bottle . syringe . filter .",
        device: str | None = None,
    ) -> None:
        # Note the broader default prompt — subsequent images may contain
        # syringes/filters that we *want* to detect (so we can correctly
        # label them out-of-inventory) rather than miss entirely.
        self.detection_prompt = detection_prompt
        self.detector = Detector(device=device)
        self.extractor = FieldExtractor()
        self.embedder = Embedder(device=device)

    def process(self, image_paths: list[str | Path]) -> PipelineResult:
        """Run all four stages on a batch of images.

        The FIRST image is treated as the reference inventory; subsequent
        images are matched against it.
        """
        if not image_paths:
            raise ValueError("at least one image (the reference) is required")

        image_paths = [str(p) for p in image_paths]
        reference_image = image_paths[0]

        # Stage 1: detect across all images, accumulating into one flat list.
        # Reassign instance_ids globally so they're unique across the batch.
        all_detections: list[Detection] = []
        for img_path in image_paths:
            per_image = self.detector.detect(img_path, prompt=self.detection_prompt)
            for d in per_image:
                d.instance_id = len(all_detections)
                all_detections.append(d)

        if not all_detections:
            empty_match = ReferenceMatch(slots=[], assignments=[], image_reports=[])
            return PipelineResult(
                detections=[],
                fields=[],
                embeddings=np.zeros((0, 0), dtype=np.float32),
                match=empty_match,
                images_processed=image_paths,
                reference_image=reference_image,
            )

        # Stage 2: OCR each crop.
        all_fields = [self.extractor.extract(d.crop) for d in all_detections]

        # Stage 3: embed each crop in one batched call.
        embeddings = self.embedder.embed_batch([d.crop for d in all_detections])

        # Stage 4: reference-based matching.
        match = match_against_reference(
            detections=all_detections,
            embeddings=embeddings,
            field_records=all_fields,
            reference_image=reference_image,
        )

        return PipelineResult(
            detections=all_detections,
            fields=all_fields,
            embeddings=embeddings,
            match=match,
            images_processed=image_paths,
            reference_image=reference_image,
        )


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python pipeline.py <reference_image> [subsequent1] [subsequent2] ...")
        print("       The FIRST image defines the complete inventory.")
        sys.exit(1)

    pipeline = Pipeline()
    result = pipeline.process(sys.argv[1:])

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
