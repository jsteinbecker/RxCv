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
import torch
import torch.nn.functional as F
from PIL import Image
from transformers import AutoImageProcessor, AutoModel


# ViT-L/14 is the size/quality sweet spot. ViT-g is better but 4x slower
# and rarely worth it for label-level discrimination.
DEFAULT_MODEL = "facebook/dinov2-large"


class Embedder:
    """DINOv2 wrapper. Load once, embed many crops."""

    def __init__(self, model_name: str = DEFAULT_MODEL, device: str | None = None) -> None:
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.processor = AutoImageProcessor.from_pretrained(model_name)
        self.model = AutoModel.from_pretrained(model_name).to(self.device)
        self.model.eval()

    @torch.no_grad()
    def embed(self, image: Image.Image | str | Path) -> np.ndarray:
        """Embed one image. Returns L2-normalized 1D numpy array."""
        if isinstance(image, (str, Path)):
            image = Image.open(image).convert("RGB")
        elif image.mode != "RGB":
            image = image.convert("RGB")

        inputs = self.processor(images=image, return_tensors="pt").to(self.device)
        outputs = self.model(**inputs)
        # Use the CLS token (pooler output) — it aggregates global structure.
        # The patch tokens (last_hidden_state[:, 1:]) are useful if you want
        # spatial features, but for whole-crop similarity the CLS is right.
        emb = outputs.pooler_output[0]
        emb = F.normalize(emb, p=2, dim=0)
        return emb.cpu().numpy()

    @torch.no_grad()
    def embed_batch(self, images: list[Image.Image]) -> np.ndarray:
        """Embed a batch. Returns [N, D] L2-normalized array.

        Faster than calling embed() in a loop when N > ~4 because the GPU
        amortizes kernel launch overhead across the batch.
        """
        if not images:
            return np.zeros((0, self.model.config.hidden_size), dtype=np.float32)

        rgb_images = [img if img.mode == "RGB" else img.convert("RGB") for img in images]
        inputs = self.processor(images=rgb_images, return_tensors="pt").to(self.device)
        outputs = self.model(**inputs)
        embs = outputs.pooler_output  # [N, D]
        embs = F.normalize(embs, p=2, dim=1)
        return embs.cpu().numpy()


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

    if len(sys.argv) < 2:
        print("Usage: python embedding.py <image1> [image2] [image3] ...")
        sys.exit(1)

    paths = sys.argv[1:]
    embedder = Embedder()
    embs = embedder.embed_batch([Image.open(p).convert("RGB") for p in paths])
    sims = cosine_similarity_matrix(embs)

    print(f"Embedding dim: {embs.shape[1]}")
    print("\nPairwise cosine similarity:")
    print(" " * 12 + "  ".join(f"{Path(p).stem[:8]:>8s}" for p in paths))
    for i, p in enumerate(paths):
        row = "  ".join(f"{sims[i, j]:8.3f}" for j in range(len(paths)))
        print(f"{Path(p).stem[:10]:10s}  {row}")
