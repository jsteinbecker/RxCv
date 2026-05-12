"""
Stage 1: Detection
==================
Open-vocabulary detection (Grounding DINO) + instance segmentation (SAM 2).

One pass produces bounding boxes, masks, class labels, and per-object crops.
Everything downstream (counting, OCR, matching) consumes these records.

Models (HuggingFace):
    - IDEA-Research/grounding-dino-base
    - facebook/sam2-hiera-large

Install:
    pip install torch torchvision transformers pillow numpy
    pip install git+https://github.com/facebookresearch/sam2.git
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

# torch and transformers are heavy and only the Detector class (not the
# Detection dataclass) actually uses them. Try to import at module load so
# the @torch.no_grad() decorator is available, but fall back gracefully on
# torch-less machines so consumers that only need the Detection dataclass
# (e.g. unit tests for the matcher) can still import this module.
try:
      import torch
      from transformers import AutoModelForZeroShotObjectDetection, AutoProcessor

      _TORCH_AVAILABLE = True
except ImportError:
      torch = None  # type: ignore[assignment]
      AutoModelForZeroShotObjectDetection = None  # type: ignore[assignment]
      AutoProcessor = None  # type: ignore[assignment]
      _TORCH_AVAILABLE = False

# SAM 2 import is deferred — it's heavier and we want detection.py to be
# importable for type hints even on machines without SAM 2 installed.
try:
      from sam2.sam2_image_predictor import SAM2ImagePredictor

      _SAM2_AVAILABLE = True
except ImportError:
      SAM2ImagePredictor = None  # type: ignore[misc,assignment]
      _SAM2_AVAILABLE = False


def _no_grad_decorator(func):
      """Wrap a method with torch.no_grad() if torch is available; otherwise
      pass through unchanged. The unchanged path is only ever hit when torch
      isn't installed, in which case the wrapped method also can't run — but
      having the decorator be a no-op lets the module import."""
      if _TORCH_AVAILABLE:
            return torch.no_grad()(func)
      return func

# Default text prompt for Grounding DINO. Period-separated phrases is the
# format the model expects — each phrase becomes a candidate class.
# Lowercase + trailing period per the model card's examples.
DEFAULT_PROMPT = "vial . iv bag . wrapper . large vial . cadd cassette ."

# Detection thresholds. Box threshold is intentionally low because some
# vials in the reference images are partially occluded or knocked over.
# Text threshold is the per-token confidence for matching the prompt class.
BOX_THRESHOLD = 0.30
TEXT_THRESHOLD = 0.25

# Minimum mask area (pixels) to keep a detection. Filters specular highlights
# and label fragments that the detector occasionally picks up as objects.
MIN_MASK_AREA = 2000
MIN_MASK_AREA_FRACTION = 0.00075


@dataclass
class Detection:
      """One detected object with everything downstream stages need."""

      instance_id: int
      class_label: str
      score: float
      bbox: tuple[int, int, int, int]  # x1, y1, x2, y2 in pixel coords
      mask: np.ndarray  # bool array, same H×W as source image
      crop: Image.Image  # tight RGB crop (bbox-cropped, not mask-cropped)
      source_image: str  # path or identifier of the source image
      metadata: dict[str, Any] = field(default_factory=dict)

      def mask_area(self) -> int:
            return int(self.mask.sum())


class Detector:
      """Grounding DINO + SAM 2 wrapper.

    Load once, reuse across many images. Both models are large (~1GB+ each)
    so instantiating per-image is wasteful.
    """

      def __init__(
                self,
                device: str | None = None,
                gdino_model: str = "IDEA-Research/grounding-dino-base",
                sam2_model: str = "facebook/sam2-hiera-large",
      ) -> None:
            if not _TORCH_AVAILABLE:
                  raise ImportError(
                        "torch and transformers are not installed. Run: "
                        "pip install torch torchvision transformers"
                  )
            if not _SAM2_AVAILABLE:
                  raise ImportError(
                        "SAM 2 is not installed. Run: "
                        "pip install git+https://github.com/facebookresearch/sam2.git"
                  )

            self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")

            # Grounding DINO — open-vocab detector. Outputs boxes from text prompt.
            self.gdino_processor = AutoProcessor.from_pretrained(gdino_model)
            self.gdino_model = AutoModelForZeroShotObjectDetection.from_pretrained(
                  gdino_model
            ).to(self.device)
            self.gdino_model.eval()

            # SAM 2 — segmentation. Takes boxes as prompts, returns masks.
            self.sam2 = SAM2ImagePredictor.from_pretrained(sam2_model, device=self.device)

      @_no_grad_decorator
      def detect(
                self,
                image_path: str | Path,
                prompt: str = DEFAULT_PROMPT,
                box_threshold: float = BOX_THRESHOLD,
                text_threshold: float = TEXT_THRESHOLD,
                min_mask_area: int = MIN_MASK_AREA,
                min_mask_area_fraction: float = MIN_MASK_AREA_FRACTION,
      ) -> list[Detection]:
            """Run detection + segmentation on one image.

        Returns a list of Detection records, one per surviving instance.
        """
            image_path = str(image_path)
            image = Image.open(image_path).convert("RGB")
            image_np = np.array(image)

            # --- Grounding DINO: text prompt -> bounding boxes -----------------
            inputs = self.gdino_processor(
                  images=image, text=prompt, return_tensors="pt"
            ).to(self.device)
            outputs = self.gdino_model(**inputs)

            # post_process_grounded_object_detection handles the threshold
            # filtering and maps token confidences back to phrase labels.
            # API note: transformers v4.51+ renamed `box_threshold` -> `threshold`
            # and the output key `labels` (string class names) -> `text_labels`
            # (the new `labels` returns integer indices). We use the current names.
            results = self.gdino_processor.post_process_grounded_object_detection(
                  outputs,
                  inputs.input_ids,
                  threshold=box_threshold,
                  text_threshold=text_threshold,
                  target_sizes=[image.size[::-1]],  # (H, W)
            )[0]

            boxes = results["boxes"].cpu().numpy()  # [N, 4] xyxy in pixel coords
            scores = results["scores"].cpu().numpy()
            # Prefer text_labels (string class names). Fall back to labels for
            # transformers <4.51 where labels was still the string field.
            labels = results.get("text_labels", results.get("labels"))

            if len(boxes) == 0:
                  return []

            # --- SAM 2: boxes -> masks -----------------------------------------
            self.sam2.set_image(image_np)
            masks, mask_scores, _ = self.sam2.predict(
                  box=boxes,
                  multimask_output=False,  # one mask per box; we trust GDino's localization
            )
            # SAM 2 returns shape [N, 1, H, W] when multimask_output=False; squeeze.
            if masks.ndim == 4:
                  masks = masks.squeeze(1)
            masks = masks.astype(bool)

            # --- Build Detection records, filtering by mask area ----------------
            detections: list[Detection] = []
            next_id = 0
            for box, score, label, mask in zip(boxes, scores, labels, masks):
                  H, W = image_np.shape[:2]
                  effective_min_area = max(min_mask_area, int(H * W * min_mask_area_fraction))
                  if int(mask.sum()) < effective_min_area:
                        continue

                  x1, y1, x2, y2 = (int(round(v)) for v in box)
                  # Clamp to image bounds — GDino occasionally emits boxes that
                  # extend a pixel or two outside the frame.
                  x1, y1 = max(0, x1), max(0, y1)
                  x2, y2 = min(W, x2), min(H, y2)
                  if x2 <= x1 or y2 <= y1:
                        continue

                  crop = image.crop((x1, y1, x2, y2))

                  detections.append(
                        Detection(
                              instance_id=next_id,
                              class_label=label,
                              score=float(score),
                              bbox=(x1, y1, x2, y2),
                              mask=mask,
                              crop=crop,
                              source_image=image_path,
                        )
                  )
                  next_id += 1

            return detections


def detect_image(
          image_path: str | Path,
          detector: Detector | None = None,
          prompt: str = DEFAULT_PROMPT,
) -> list[Detection]:
      """Convenience wrapper. Builds a Detector if one isn't passed.

    For batch processing, build the Detector once and pass it explicitly.
    """
      if detector is None:
            detector = Detector()
      return detector.detect(image_path, prompt=prompt)


if __name__ == "__main__":
      import sys

      if len(sys.argv) < 2:
            print("Usage: python detection.py <image_path> [prompt]")
            sys.exit(1)

      img_path = sys.argv[1]
      prompt = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_PROMPT

      det = Detector()
      results = det.detect(img_path, prompt=prompt)
      print(f"Found {len(results)} objects in {img_path}")
      for r in results:
            print(
                  f"  #{r.instance_id} {r.class_label:10s} "
                  f"score={r.score:.3f} bbox={r.bbox} area={r.mask_area()}"
            )