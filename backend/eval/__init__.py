"""Retrieval evaluation harness.

Ships a versioned Doc2Dial dataset schema, a hermetic seeded-corpus
builder, and a CLI runner that drives the same `fetch_candidate_rows` +
`rank_top_rows` pair the production analyser uses (ADR-013).

The v1 scope is **retrieval-only** — Recall@1/5/10 + MRR. LLM
answer quality, faithfulness, and exact-match-of-generated-answer
are deferred (see ADR-015 / ADR-016).
"""