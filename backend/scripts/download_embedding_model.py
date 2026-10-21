"""Download the Qwen3-Embedding-0.6B model into the local Hugging Face cache.

First run: downloads ~1.2 GB to ~/.cache/huggingface/hub/
Later runs: instant load from cache.

Usage (from backend/):
    source .venv/bin/activate
    python scripts/download_embedding_model.py
"""

from __future__ import annotations

from sentence_transformers import SentenceTransformer

MODEL_ID = "Qwen/Qwen3-Embedding-0.6B"


def main() -> None:
    print(f"Loading {MODEL_ID!r} ... (first run downloads ~1.2 GB)")
    model = SentenceTransformer(MODEL_ID, device="cpu")
    print("Loaded.")
    print("  model_name        :", MODEL_ID)
    print("  embedding dim     :", model.get_embedding_dimension())
    print("  max seq length    :", model.get_max_seq_length())
    print("  cache location    : ~/.cache/huggingface/hub/")


if __name__ == "__main__":
    main()
