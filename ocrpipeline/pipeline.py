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

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from detection import Detection, Detector
from embedding import Embedder
from matching import ReferenceMatch, match_against_reference, summarize
from ocr import FieldExtractor, OCRFields


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

    def summary(self) -> str:
        """Human-readable text summary."""
        lines = []
        lines.append(f"Reference image: {Path(self.reference_image).name}")
        lines.append(f"Subsequent images: {len(self.images_processed) - 1}")
        lines.append("")

        # Inventory listing.
        lines.append(f"Reference inventory ({len(self.match.slots)} slots):")
        from collections import Counter
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
    print(result.summary())
    print()
    print("Full JSON:")
    print(json.dumps(result.to_dict(), indent=2, default=str))
