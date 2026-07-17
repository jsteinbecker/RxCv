"""
Stage 3: Visual Embeddings
==========================
Compute DINOv2 embeddings for vial crops. These power the visual side of
front/back matching: two crops of the same vial type will have high cosine
similarity, regardless of viewing angle.

DINOv2 over CLIP for this task:
    CLIP is trained on image-text pairs; its embedding space is organized
    around what an image is "about" semantically. DINOv2 is trained with
    pure self-supervision (no text), so its space is organized around
    visual structure — exactly what tells two pharmaceutical labels apart.

Install:
    pip install torch transformers pillow numpy
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

# torch and transformers are heavy and only the Embedder class actually uses
# them. Defer them so consumers that only need cosine_similarity_matrix /
# cosine_similarity (pure numpy) — including unit tests for the matcher —
# can import this module on machines without torch installed.
try:
      import torch
      import torch.nn.functional as F
      from transformers import AutoImageProcessor, AutoModel

      _TORCH_AVAILABLE = True
except ImportError:
      _TORCH_AVAILABLE = False


def _no_grad_decorator(func):
      """Wrap a method with torch.no_grad() if torch is available; otherwise
      pass through unchanged. The unchanged path is only ever hit when torch
      isn't installed, in which case the wrapped method also can't run — but
      having the decorator be a no-op lets the module import."""
      if _TORCH_AVAILABLE and torch is not None:
            return torch.no_grad()(func)
      return func


# ViT-L/14 is the size/quality sweet spot. ViT-g is better but 4x slower
# and rarely worth it for label-level discrimination.
DEFAULT_MODEL = "facebook/dinov2-large"


class Embedder:
      """DINOv2 wrapper. Load once, embed many crops."""

      def __init__(self, model_name: str = DEFAULT_MODEL, device: str | None = None) -> None:
            if not _TORCH_AVAILABLE:
                  raise ImportError(
                        "torch and transformers are not installed. Run: "
                        "pip install torch transformers"
                  )
            self.device = device or ("cuda" if torch is not None and torch.cuda.is_available() else "cpu")
            self.processor = AutoImageProcessor.from_pretrained(model_name) if model_name is not None else None
            self.model = AutoModel.from_pretrained(model_name).to(self.device) if model_name is not None else None
            self.model.eval() if self.model is not None else None
            self._cache: dict[str, np.ndarray] = {}

      @_no_grad_decorator
      def embed(self, image: Image.Image | str | Path) -> np.ndarray:
            """Embed one image. Returns L2-normalized 1D numpy array."""
            if self.model is None or self.processor is None:
                  raise RuntimeError("Embedder model is not loaded. Please provide a valid model_name.")

            if isinstance(image, (str, Path)):
                  image = Image.open(image).convert("RGB")
            elif image.mode != "RGB":
                  image = image.convert("RGB")

            from .image_ops import image_fingerprint
            key = image_fingerprint(image)
            cached = self._cache.get(key)
            if cached is not None:
                  return cached.copy()

            inputs = self.processor(images=image, return_tensors="pt").to(self.device)
            outputs = self.model(**inputs)
            # Use the CLS token (pooler output) — it aggregates global structure.
            # The patch tokens (last_hidden_state[:, 1:]) are useful if you want
            # spatial features, but for whole-crop similarity the CLS is right.
            emb = outputs.pooler_output[0]
            emb = F.normalize(emb, p=2, dim=0)
            arr = emb.cpu().numpy()
            self._cache[key] = arr
            return arr.copy()

      @_no_grad_decorator
      def embed_batch(self, images: list[Image.Image]) -> np.ndarray:
            """Embed a batch. Returns [N, D] L2-normalized array.

            Faster than calling embed() in a loop when N > ~4 because the GPU
            amortizes kernel launch overhead across the batch.
            """
            if self.model is None or self.processor is None:
                  raise RuntimeError("Embedder model is not loaded. Please provide a valid model_name.")

            if not images:
                  return np.zeros((0, self.model.config.hidden_size), dtype=np.float32)

            try:
                  from rxocrpl.detection.image_ops import image_fingerprint
            except ImportError:
                  raise ImportError(
                        "rxocrpl.detection.image_ops is not available. Please ensure the module is installed.")

            rgb_images = [img if img.mode == "RGB" else img.convert("RGB") for img in images]
            keys = [image_fingerprint(img) for img in rgb_images]
            out: list[np.ndarray | None] = [self._cache.get(k) for k in keys]
            missing_indices = [i for i, val in enumerate(out) if val is None]

            if missing_indices:
                  batch = [rgb_images[i] for i in missing_indices]
                  inputs = self.processor(images=batch, return_tensors="pt").to(self.device)
                  outputs = self.model(**inputs)
                  embs = F.normalize(outputs.pooler_output, p=2, dim=1).cpu().numpy()
                  for idx, emb in zip(missing_indices, embs):
                        self._cache[keys[idx]] = emb
                        out[idx] = emb

            return np.stack([val for val in out if val is not None]).astype(np.float32, copy=True)


def cosine_similarity_matrix(embeddings: np.ndarray) -> np.ndarray:
      """Pairwise cosine similarity for an [N, D] L2-normalized matrix.

      Since rows are unit-norm, cosine similarity == dot product.
      Returns [N, N] symmetric matrix with 1.0 on the diagonal.
      """
      if embeddings.shape[0] == 0:
            return np.zeros((0, 0), dtype=np.float32)
      return embeddings @ embeddings.T


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
      """Cosine similarity between two L2-normalized 1D vectors."""
      return float(np.dot(a, b))


if __name__ == "__main__":
      import sys
      from pathlib import Path

      if len(sys.argv) > 1:
            # Run with provided system arguments
            paths = sys.argv[1:]
            images = [Image.open(p).convert("RGB") for p in paths]
            labels = [Path(p).stem[:8] for p in paths]
      else:
            img1_path = "/Users/jts/PycharmProjects/RxCv/rxocrpl/detection/static/img-example-1.png"
            img2a_path = "/Users/jts/PycharmProjects/RxCv/rxocrpl/detection/static/img-example-2a.jpeg"
            img2b_path = "/Users/jts/PycharmProjects/RxCv/rxocrpl/detection/static/img-example-2b.jpeg"
            img3_path = "/Users/jts/PycharmProjects/RxCv/rxocrpl/detection/static/img-example-3.jpeg"

            paths = [img1_path, img2a_path, img2b_path, img3_path]
            for p in paths:
                  print(Path(p).exists(), p)

            images = [Image.open(p).convert("RGB") for p in paths]
            labels = ["1", "2A", "2B", "3"]

      embedder = Embedder()
      embs = embedder.embed_batch(images)
      sims = cosine_similarity_matrix(embs)

      print(f"Embedding dim: {embs.shape[1]}")
      print("\nPairwise cosine similarity:")
      print(" " * 12 + "  ".join(f"{lbl:>8s}" for lbl in labels))
      for i, lbl in enumerate(labels):
            row = "  ".join(f"{sims[i, j]:8.3f}" for j in range(len(labels)))
            print(f"{lbl[:10]:10s}  {row}")
