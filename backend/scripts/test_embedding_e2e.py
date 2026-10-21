"""End-to-end smoke test for the embedding wiring.

Exercises:
  - get_default_embedding_model() with EMBEDDING_PROVIDER=qwen3
  - embed_batch() returning EmbeddingResult instances
  - dim == settings.embedding_dim
  - cosine similarity ranking (paraphrase > unrelated)

Run from backend/ with the venv active:
    EMBEDDING_PROVIDER=qwen3 python scripts/test_embedding_e2e.py
"""

from __future__ import annotations

import numpy as np

from app.core.config import get_settings
from app.services.ai.embeddings import get_default_embedding_model


def main() -> None:
    settings = get_settings()
    print(f"settings.embedding_provider  : {settings.embedding_provider}")
    print(f"settings.embedding_model_name: {settings.embedding_model_name}")
    print(f"settings.embedding_dim       : {settings.embedding_dim}")

    model = get_default_embedding_model()
    print(f"\nmodel class : {type(model).__name__}")
    print(f"model.dim   : {model.dim}")
    print(f"model.model_name: {model.model_name}")

    chunks = [
        "The cat sat on the mat.",
        "A kitten was resting on the rug.",  # paraphrase of [0]
        "Quantum mechanics describes subatomic particles.",  # unrelated
        "Stocks rallied after the Fed announcement.",  # unrelated
    ]
    results = model.embed_batch(chunks)
    print(f"\nembedded {len(results)} chunks; each dim={len(results[0].vector)}")
    print(f"first vector[0..3]: {[round(x, 4) for x in results[0].vector[:4]]}")
    print(f"distance_metric   : {results[0].distance_metric}")

    # Stash vectors as a matrix for similarity math.
    mat = np.array([r.vector for r in results])
    norms = np.linalg.norm(mat, axis=1)
    print(f"\nvector norms (should be ~1.0): {norms.round(4).tolist()}")

    # Cosine similarity = dot product when vectors are normalized.
    sim_related = float(np.dot(mat[0], mat[1]))
    sim_unrelated = max(float(np.dot(mat[0], mat[2])), float(np.dot(mat[0], mat[3])))
    print(f"\nsimilarity cat-vs-paraphrase : {sim_related:.4f}")
    print(f"similarity cat-vs-unrelated  : {sim_unrelated:.4f}")

    assert model.dim == settings.embedding_dim, "dim mismatch with settings"
    assert all(len(r.vector) == model.dim for r in results), "vector dim drift"
    assert sim_related > sim_unrelated, "semantic ranking broken"
    assert all(0.99 < n < 1.01 for n in norms), "vectors should be unit-length"
    print("\nAll wiring checks passed.")


if __name__ == "__main__":
    main()
