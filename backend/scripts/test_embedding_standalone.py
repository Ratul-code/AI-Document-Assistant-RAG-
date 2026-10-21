"""Standalone smoke test for the Qwen3-Embedding-0.6B model.

Confirms we can:
  1. Encode a single string.
  2. Encode a batch of strings in one call.
  3. Compute cosine similarity between two related and two unrelated sentences.
  4. Inspect a single vector's value range and length.

Usage (from backend/):
    source .venv/bin/activate
    python scripts/test_embedding_standalone.py
"""

from __future__ import annotations

import numpy as np
from sentence_transformers import SentenceTransformer

MODEL_ID = "Qwen/Qwen3-Embedding-0.6B"


def main() -> None:
    print(f"Loading {MODEL_ID!r} ...")
    model = SentenceTransformer(MODEL_ID, device="cpu")
    print(f"  dim={model.get_embedding_dimension()}\n")

    # --- 1. Single text ---
    print("[1] Single text encode")
    single = model.encode("What is the capital of France?", normalize_embeddings=True)
    print(f"    type={type(single).__name__}  shape={single.shape}  dtype={single.dtype}")
    print(f"    first 5 values: {single[:5].tolist()}")
    print(
        f"    vector norm:   {float(np.linalg.norm(single)):.4f}  (should be ~1.0 after normalize)"
    )
    print()

    # --- 2. Batch encode ---
    print("[2] Batch encode (3 texts in one call)")
    texts = [
        "What is the capital of France?",
        "Paris is the capital of France.",
        "I like pizza.",
    ]
    batch = model.encode(texts, normalize_embeddings=True)
    print(f"    shape={batch.shape}  dtype={batch.dtype}")
    print(f"    row norms: {np.linalg.norm(batch, axis=1).round(4).tolist()}")
    print()

    # --- 3. Similarity check ---
    print("[3] Cosine similarity (since vectors are normalized, dot == cosine)")
    sim_related = float(np.dot(batch[0], batch[1]))
    sim_unrelated = float(np.dot(batch[0], batch[2]))
    sim_self = float(np.dot(batch[0], batch[0]))
    print(f"    Q vs paraphrase    : {sim_related:.4f}   (expect HIGH, > 0.5)")
    print(f"    Q vs pizza         : {sim_unrelated:.4f}   (expect LOW)")
    print(f"    Q vs itself        : {sim_self:.4f}   (must be 1.0)")
    print()

    # --- 4. Sanity ---
    print("[4] Sanity checks")
    finite = np.isfinite(batch).all()
    nonzero = not np.allclose(batch, 0.0)
    print(f"    all values finite  : {finite}")
    print(f"    not all zeros      : {nonzero}")
    print(f"    value range        : [{batch.min():.4f}, {batch.max():.4f}]")

    assert sim_self > 0.999, "self-similarity should be 1.0"
    assert sim_related > sim_unrelated, "related text should beat unrelated"
    assert finite and nonzero, "vectors look broken"
    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
