# Retrieval Eval — doc2dial v1

- Embedder: `Qwen/Qwen3-Embedding-0.6B`  (171 docs, 482 chunks in corpus; 1500 questions in dataset)
- Started: 2026-10-07T19:07:57+00:00  Finished: 2026-10-07T19:13:48+00:00

## Settings

| knob | value |
|------|-------|
| scope_mode | `owner_wide` |
| retrieval_mode | `dense` |
| passage_mapping | `fuzzy_overlap_0.5` |
| top_k (pool cap) | `10` |
| min_cosine | `0.0` |
| snippet_chars | `1000` |

## Overall

- Retriever: R@1=0.457  R@5=0.794  R@10=0.878  MRR=0.726  (scored 1500/1500)

## Miss analysis (retriever only)

Base: **1500** scored questions (questions with non-empty gold). A 'miss' is a question whose gold set has zero overlap with the top-K retrieved ids.

| K | misses | % of scored |
|---|--------|-------------|
| @ 1 | 599 | 39.9% |
| @ 5 | 167 | 11.1% |
| @ 10 | 87 | 5.8% |

## Per question type

| type | n | R@1 | R@5 | R@10 | MRR |
|------|---|-----|-----|------|-----|
| dialogue-turn | 1500 | 0.457 | 0.794 | 0.878 | 0.726 |
