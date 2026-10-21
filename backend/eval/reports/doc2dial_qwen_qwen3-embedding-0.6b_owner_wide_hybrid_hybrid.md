# Retrieval Eval — doc2dial v1

- Embedder: `Qwen/Qwen3-Embedding-0.6B`  (171 docs, 482 chunks in corpus; 1500 questions in dataset)
- Started: 2026-10-07T18:30:04+00:00  Finished: 2026-10-07T19:02:22+00:00

## Settings

| knob | value |
|------|-------|
| scope_mode | `owner_wide` |
| retrieval_mode | `hybrid` |
| passage_mapping | `fuzzy_overlap_0.5` |
| top_k (pool cap) | `10` |
| hybrid.dense_k | `15` |
| hybrid.bm25_k | `15` |
| hybrid.rrf_k | `60` |
| min_cosine | `0.0` |
| snippet_chars | `1000` |
| rerank.enabled | `true` |
| rerank.provider | `cross_encoder:BAAI/bge-reranker-base` |
| rerank.model | `BAAI/bge-reranker-base` |
| rerank.device | `mps` |
| rerank.top_k (LLM input) | `10` |
| rerank.pool_multiplier | `3` (pre-rerank pool ≈ `10 × 3` = `30`) |
| rerank.max_length | `512` |
| rerank.batch_size | `16` |
| rerank.timeout_s | `30.0` |

## Overall

- Retriever: R@1=0.512  R@5=0.822  R@10=0.883  MRR=0.777  (scored 1500/1500)

- **With rerank** (`cross_encoder:BAAI/bge-reranker-base`): R@1=0.532  R@5=0.829  R@10=0.883  MRR=0.797  (scored 1500/1500)

- **Δ** (rerank − retriever): R@1 +0.020  ·  R@5 +0.008  ·  R@10 +0.000  ·  MRR +0.020

## Miss analysis (retriever only)

Base: **1500** scored questions (questions with non-empty gold). A 'miss' is a question whose gold set has zero overlap with the top-K retrieved ids.

| K | misses | % of scored |
|---|--------|-------------|
| @ 1 | 500 | 33.3% |
| @ 5 | 121 | 8.1% |
| @ 10 | 70 | 4.7% |

## Miss analysis (retriever + rerank)

Base: **1500** scored questions (matched by id to the retriever-only run).

| K | misses | % of scored |
|---|--------|-------------|
| @ 1 | 455 | 30.3% |
| @ 5 | 102 | 6.8% |
| @ 10 | 70 | 4.7% |

### Rerank effect: rescued vs lost (per K)

A question is *rescued* if R@K went from 0 (retriever) to >0 (rerank). *Lost* is the opposite. *Unchanged hit* means the rerank kept it as a hit. (Questions where the retriever already had a hit and the rerank preserved it are not counted as 'lost' even if the exact position changed.)

| K | rescued | lost | unchanged hit |
|---|---------|------|---------------|
| @ 1 | 176 | 131 | 869 |
| @ 5 | 42 | 23 | 1356 |
| @ 10 | 0 | 0 | 1430 |

## Per question type

| type | n | R@1 | R@5 | R@10 | MRR |
|------|---|-----|-----|------|-----|
| dialogue-turn | 1500 | 0.512 | 0.822 | 0.883 | 0.777 |
