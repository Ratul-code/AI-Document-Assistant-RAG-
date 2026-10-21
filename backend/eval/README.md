# Eval — Retrieval evaluation harness

Retrieval-only eval for the AI Document Assistant. Drives the
**real production retrieval path** (`fetch_candidate_rows` +
`rank_top_rows` / `hybrid_retrieve`) against a persisted SQLite
corpus built from the Doc2Dial dataset.

| Dataset | Source | Size |
|---|---|---|
| `doc2dial` | Doc2Dial official release (~85 MB) | question-capped subset (default: 40 turns → 6 docs → ~13 chunks) |

The runner is dataset-scoped to Doc2Dial; the `name` field in
`dataset.json` is just the tag written into the report filename.

---

## TL;DR — the 4 scripts you run, in order

```bash
cd backend
source .venv/bin/activate

# One-time setup (downloads the Doc2Dial release)
pip install -r requirements-eval.txt
python -m eval.scripts.doc2dial_loader --max-turns 40

# ── SCRIPT 1: INGEST ─────────────────────────────────────────────
# Drives the prod parse → chunk → embed pipeline for every doc
# in the dataset's question-capped subset.
# Writes:
#   backend/eval/.cache/doc2dial/corpus.db
#   backend/eval/.cache/doc2dial/manifest.json
#   backend/eval/datasets/doc2dial/owner_id.txt
# ────────────────────────────────────────────────────────────────
python -m eval.scripts.ingest_corpus \
    --dataset doc2dial \
    --embedding-provider stub \                  # or "qwen3" for real
    --max-questions 40                           # the only cap knob

# ── SCRIPT 2: BUILD DATASET ──────────────────────────────────────
# Maps gold passages → chunks via fuzzy LCS overlap.
# Writes:
#   backend/eval/datasets/doc2dial/dataset.json         (gold keys)
#   backend/eval/datasets/doc2dial/passage_mapping.json (audit)
# ────────────────────────────────────────────────────────────────
python -m eval.scripts.build_dataset --dataset doc2dial --max-questions 40

# ── SCRIPT 3: SCORE ──────────────────────────────────────────────
# Reads persisted corpus + dataset.json, calls the real prod
# retriever for every question, writes:
#   backend/eval/results/<dataset>_<embedder>_<scope>_<mode>_<ts>.json
#   backend/eval/reports/<dataset>_<embedder>_<scope>_<mode>_<ts>.md
# ────────────────────────────────────────────────────────────────
python -m eval.scripts.run_retrieval_eval \
    --dataset doc2dial \
    --embedding-provider stub \
    --scope-mode owner_wide \
    --retrieval-mode hybrid \
    --top-k 3 \
    --max-questions 40
```

Re-ingesting with a different embedder is a one-liner with `--force`:

```bash
python -m eval.scripts.ingest_corpus --dataset doc2dial --embedding-provider qwen3 --force --max-questions 40
python -m eval.scripts.build_dataset --dataset doc2dial --max-questions 40
python -m eval.scripts.run_retrieval_eval --dataset doc2dial --embedding-provider qwen3 --top-k 3 --max-questions 40
```

The runner refuses to score against a corpus whose `model_name`
doesn't match the active embedder — model-mismatch guard driven by
`manifest.json`.

---

## All CLI variables you can set

### `doc2dial_loader` — `python -m eval.scripts.doc2dial_loader`

Downloads the Doc2Dial JSON release to `eval/.cache/doc2dial/`.
Idempotent (re-runs skip files already on disk).

| Flag | Default | Effect |
|---|---|---|
| `--max-turns` | 40 | Preview cap: take the first N turns (sorted by `(split, dial_id, turn_id)`), derive the doc set. The loader does NOT ingest — that's `ingest_corpus.py`'s job. |
| `--max-docs` | 0 (no warn) | If > 0 and the derived doc set is larger than this, print a NOTE. The cap is informational only; it does NOT truncate. |

### `ingest_corpus` — `python -m eval.scripts.ingest_corpus`

Drives the real production parse → chunk → embed pipeline for every
doc in the question-capped subset, persists to `corpus.db`.

| Flag | Default | Effect |
|---|---|---|
| `--dataset` | `doc2dial` | Doc2Dial only. |
| `--embedding-provider` | `stub` | `stub` (16-dim SHA-256, fast) or `qwen3` (1024-dim, slow, real). |
| `--max-questions` | 40 | Cap to N user turns; derive and ingest the docs they reference. |
| `--force` | off | Drop existing `corpus.db` + `manifest.json` before re-ingesting. Required when switching embedders. |

### `build_dataset` — `python -m eval.scripts.build_dataset`

Maps gold passages → chunks, writes `dataset.json` + `passage_mapping.json`.

| Flag | Default | Effect |
|---|---|---|
| `--dataset` | `doc2dial` | Doc2Dial only. |
| `--threshold` | 0.5 | LCS overlap threshold for passage→chunk mapping. Higher = stricter (fewer chunks marked gold). |
| `--max-questions` | 0 (no cap) | Cap the question list. The slice must match what `ingest_corpus` used so the corpus and dataset stay in lockstep. |
| `--filter-context-dependent` | off | Drop short / acknowledgement turns before scoring. Produces a fair single-turn baseline. Gold chunks unchanged. |
| `--context-mode` | `none` | Rewrite the question with prior dialogue context. `none` = raw user turn, `previous_agent` = 1 turn back, `topic_plus_previous` = first user turn + 1 back. Gold chunks unchanged. |

### `run_retrieval_eval` — `python -m eval.scripts.run_retrieval_eval`

Scores the persisted corpus + dataset with the production retriever,
writes results JSON + report Markdown.

| Flag | Default | Effect |
|---|---|---|
| `--dataset` | `doc2dial` | Doc2Dial only. |
| `--embedding-provider` | `stub` | Must match the corpus's `manifest.json` `model_name` (the runner refuses otherwise). |
| `--scope-mode` | `owner_wide` | `owner_wide` (real baseline — every chunk in the corpus) or `per_question` (debug only — narrows candidates to the question's `scope_document_ids`). |
| `--retrieval-mode` | `hybrid` | `hybrid` (dense top-K + BM25 top-K + RRF, default) or `dense` (cosine-only, legacy v1). |
| `--top-k` | from `dataset.json` (`metrics_config.top_k`, normally 5) | Recall target. Lower for tiny corpora (e.g. `--top-k 3` for Doc2Dial). |
| `--hybrid-dense-k` | 15 | Hybrid only: dense-leg candidates before RRF. |
| `--hybrid-bm25-k` | 15 | Hybrid only: BM25-leg candidates before RRF. |
| `--hybrid-rrf-k` | 60 | Hybrid only: RRF constant. Cormack 2009 standard — do not change without a reason. |
| `--max-questions` | 0 (no cap) | Cap the question list for smoke runs. |
| `--output-suffix` | timestamp | Custom filename suffix for results/report (default is `YYYYMMDDTHHMMSS`). |

---

## All scopes (axes of variation)

The "scope" of an experiment is the combination of `(scope_mode, retrieval_mode, embedder, top_k)`. Here is the full set worth running for a comparison.

### Doc2Dial (cap-driven, default 40 turns → 6 docs → ~13 chunks)

| # | Scope mode | Retrieval | Embedder | Top-K | Max-Q | Why |
|---|---|---|---|---|---|---|
| D1 | `owner_wide` | `hybrid` | `stub` | 3 | 40 | Fast smoke |
| D2 | `owner_wide` | `hybrid` | `qwen3` | 3 | 40 | **Headline real baseline** |
| D3 | `owner_wide` | `dense` | `stub` | 3 | 40 | A/B vs D1 |
| D4 | `owner_wide` | `dense` | `qwen3` | 3 | 40 | A/B vs D2 |
| D5 | `per_question` | `dense` | `stub` | 3 | 40 | With 1–3 chunks per question, should be near-perfect |
| D6 | `owner_wide` | `hybrid` | `qwen3` | 5 | 200 | Larger slice for stability |

---

## What is "owner_wide" vs "per_question" scope mode?

Both come straight from the production analyser. The only
difference is what we pass as `document_ids` to
`fetch_candidate_rows`:

| Scope mode | `document_ids` arg | What gets ranked | When to use |
|---|---|---|---|
| **`owner_wide`** (default) | `None` | Every chunk in every READY doc owned by the eval owner — i.e. **the whole corpus**. This is what the production chat path does when the user hasn't filtered to a specific conversation. | **The real baseline.** This is what the eval is designed to measure. |
| `per_question` | The question's `scope_document_ids` (the doc_ids the dataset lists as the question's source docs) | Only chunks from those specific docs. | Debugging only — checks whether retrieval works *given* a known-good scope. Inflates scores because it pre-filters the candidate set. |

In production, `fetch_candidate_rows` is owner-scoped **for
security**, not for ranking. The candidate set is always
`WHERE documents.owner_id = :owner_id AND documents.status = 'ready'
AND document_embeddings.status = 'succeeded'` — every returned
row is guaranteed to belong to the calling user. `document_ids`
is an optional narrowing filter on top of that. The eval harness
holds the same invariant via the per-dataset synthetic
`owner_id.txt`.

---

## What is "hybrid" vs "dense" retrieval mode?

The runner exposes `--retrieval-mode {dense, hybrid}`. Both modes
call the **same production retrieval primitives**
(`fetch_candidate_rows` + `rank_top_rows` for the dense leg) so the
scores are apples-to-apples A/B comparisons on the same questions.

### `dense` (legacy v1)

```
for each question:
  candidates = fetch_candidate_rows(db, owner_id, document_ids)
  query_vec  = embedder.embed(question).vector
  ranked     = rank_top_rows(candidates, query_vec, top_k=top_k)
  return top_k chunk_ids
```

### `hybrid` (default)

```
# Once at startup, only in owner_wide mode:
all_candidates = fetch_candidate_rows(db, owner_id, document_ids=None)
bm25_index     = build_bm25_index(all_candidates)   # Okapi, k1=1.5, b=0.75

for each question:
  candidates = fetch_candidate_rows(db, owner_id, document_ids)
  query_vec  = embedder.embed(question).vector

  dense_top = [r.chunk_id for r, _ in
               rank_top_rows(candidates, query_vec, top_k=15)]      # --hybrid-dense-k
  bm25_top  = [r.chunk_id for r, _ in
               bm25_top_rows(bm25_index, question, top_k=15)]      # --hybrid-bm25-k

  fused     = reciprocal_rank_fusion([dense_top, bm25_top], k=60)  # --hybrid-rrf-k
  return top_k chunk_ids from `fused`
```

Why hybrid helps: dense retrieval alone misses questions whose
query vocabulary doesn't semantically match the chunk text
(acronyms, technical terms, paraphrases). BM25 catches exact-token
matches the embedder misses. Reciprocal Rank Fusion combines the
two rankings without score-calibration issues (RRF uses ranks, not
raw scores), so the two legs don't need to be on the same scale.

In `per_question` mode the candidate set changes per question, so
the BM25 index is rebuilt inside each call (no shared state). In
`owner_wide` mode the candidate set is identical across all
questions, so the BM25 index is built **once** at startup and
reused — saves ~100 ms × N_questions.

---

## End-to-end flow

```
                 ┌─────────────────────────────────────────┐
                 │ Dataset source (doc2dial zip)            │
                 └────────────────┬────────────────────────┘
                                  │ loader
                                  ▼
        ┌──────────────────────────────────────────────────────┐
        │  backend/eval/.cache/doc2dial/*.json                 │
        └─────┬─────────────────────────────┬──────────────────┘
              │                             │
              │ ingest_corpus.py            │ build_dataset.py
              │ (SCRIPT 1)                  │ (SCRIPT 2)
              ▼                             ▼
┌──────────────────────────────┐  ┌──────────────────────────────┐
│ Persisted corpus.db +        │  │ dataset.json (canonical gold)│
│   manifest.json              │  │ passage_mapping.json (audit) │
│   • N docs                   │  │   • M questions              │
│   • K chunks                 │  │   • S with ≥1 gold chunk     │
│   • K embeddings             │  │   • scope_mode = owner_wide  │
│   • 1 owner (deterministic   │  └──────────────┬───────────────┘
│     UUID in owner_id.txt)    │                 │
└──────────────┬───────────────┘                 │
               │                                 │
               └─────────────────┬───────────────┘
                                 │ run_retrieval_eval.py (SCRIPT 3)
                                 ▼
        ┌─────────────────────────────────────────────────┐
        │ For every question:                             │
        │   1. embed(question)              ← active EmbeddingModel │
        │   2. fetch_candidate_rows(        ← prod repo, owner-scoped │
        │        db, owner_id,             READY + succeeded only     │
        │        document_ids=scope_ids)   None = owner_wide          │
        │   3. rank_top_rows OR hybrid_retrieve           │
        │   4. resolve (doc_id, chunk_index) gold keys → chunk UUIDs │
        │   5. recall_at_k + MRR + aggregate                          │
        │   6. exclude rows with empty gold from the metric average  │
        └────────────┬────────────────────────────────────────────────┘
             ▼
        ┌─────────────────────────────────────────────────┐
        │ results/doc2dial_<embedder>_<scope>_<mode>_*.json │
        │ reports/doc2dial_<embedder>_<scope>_<mode>_*.md  │
        │   • recall_at_1/5/10, mrr                        │
        │   • questions_total vs questions_scored          │
        │   • empty_gold_skipped                           │
        └─────────────────────────────────────────────────┘
```

---

## How the corpus is ingested (script 1)

`ingest_corpus.py` runs the **same pipeline a real upload runs**,
just bypassing the HTTP layer:

```
1. load_unique_documents_doc2dial()    # loader helper
       doc2dial: takes first --max-questions turns, derives their docs

2. For each doc:
     a. write doc text to a temp .md file
     b. parse_document()                    ← app pipeline (real)
     c. document_task._persist_pages()      ← app pipeline (real)
     d. document_task._chunk_pages_for_document() ← app pipeline (real)
     e. embedding_task._process_one_document()    ← app pipeline (real)
            └─ calls the active EmbeddingModel.embed_batch
               on every chunk and writes DocumentEmbedding rows

3. write manifest.json (model_name, dim, doc_count, chunk_count,
                        question_count, completed_at, db_path)
```

The corpus lives at `backend/eval/.cache/doc2dial/corpus.db`. The
schema is the prod schema — same `documents`, `document_pages`,
`document_chunks`, `document_embeddings`, `users` tables — built
by `Base.metadata.create_all` on the same model classes the prod
app uses. That is the property that makes the prod retrieval SQL
work unchanged.

**Why the deterministic `doc_id` matters:** chunker-assigned
`chunk_id` UUIDs are `uuid4()` and change every re-seed. Document
IDs would too — unless we pin them. `ingest_corpus.py` passes
`doc_id=deterministic_doc_id(owner_id, doc_digest)` (UUID5) into
`seed_one_document`, so the same `(owner, doc)` always produces
the same `documents.id`. This is what lets the dataset's gold
keys (`relevant_chunk_keys` and `scope_document_ids`) line up
with the rows in the persisted DB across re-ingests. **Without
this, every score is 0**.

For Doc2Dial the digest is the composite `doc_key = "<domain>::<doc_id>"` because doc_ids are namespaced by domain (e.g. `Registrations#3_0` exists in `dmv`; the same string may also exist in another domain).

---

## How the dataset is built (script 2)

`build_dataset.py` reads the persisted corpus and produces two
files:

### `dataset.json` — canonical gold (runner reads it)

For every question, we need to know which of *our* chunks are
gold. Each Doc2Dial turn references gold evidence at the *span*
level; our chunker splits docs at 500/800-token tiktoken
boundaries which don't align with the dataset's span boundaries.
So:

```
For each question Q with gold evidence spans P_1, P_2, …:

  resolve Q's source doc → Document.id (UUID5 of owner+doc_digest)
  for each gold span P_j of Q:
    for each chunk C of Q's source doc:
      overlap = lcs_ratio(P_j, C)
        = longest_common_substring(P_j, C) / len(P_j)
      if overlap ≥ 0.5 (--threshold):
        mark C as a gold chunk for Q

→ Q.relevant_chunk_keys = [(doc_id, chunk_index), …]
  Q.scope_document_ids  = [doc_id, …]    (per-question scope)
  Q.relevant_passage_ids = [...]         (raw dataset ids, audit only)
```

Chunk UUIDs are **not** stored in the dataset — they're
non-deterministic across re-seeds. Instead we store the stable
key `(document_id, chunk_index)` — `document_id` is UUID5
(deterministic) and `chunk_index` within a doc is
content-deterministic. The runner resolves `(doc_id, chunk_index)`
→ `chunk_id` UUID via a single SQL query against the persisted
DB at score time.

`build_dataset.py` prints `Wrote dataset.json (M questions,
S/M scored)` — "scored" means "had ≥1 chunk mapping above the
0.5 threshold". The S/M ratio is the **upper bound on recall**
regardless of retriever quality. For Doc2Dial with the
question-first cap, this is normally 100% — every question's
source doc is in the corpus.

### `passage_mapping.json` — audit (runner ignores it)

Every `(passage, chunk)` pair with `overlap ≥ threshold`, plus
the ratio. Reviewers use this to audit why a chunk was or wasn't
marked relevant. The runner doesn't read it.

---

## How retrieval scoring works (script 3)

For every question, `run_retrieval_eval._retrieve_for_question`
does (dense mode):

```python
from app.repositories.embedding_search import fetch_candidate_rows
from app.services.document.analyser import rank_top_rows

candidates = fetch_candidate_rows(
    db,
    owner_id=owner_id,
    document_ids=None,        # owner_wide: every READY doc of this owner
)
query_vec = embedder.embed(question).vector
ranked = rank_top_rows(
    candidates, query_vec,
    top_k=top_k,
    min_cosine=0.0,
)
return [row.chunk_id for row, _score in ranked]
```

The candidate fetch is the same SQL the prod analyser uses:

```sql
SELECT dc.id, d.id, d.title, dp.id, dp.page_number,
       dc.chunk_index, dc.section_path, dc.text,
       de.vector, de.model_name, de.dim
FROM document_chunks dc
JOIN document_pages dp ON dp.id = dc.page_id
JOIN documents d ON d.id = dp.document_id
JOIN document_embeddings de ON de.chunk_id = dc.id
WHERE d.owner_id = :owner_id
  AND d.status = 'ready'
  AND de.status = 'succeeded'
[AND d.id IN (:scope_ids)]    -- only when document_ids is provided
ORDER BY d.id, dp.page_number, dc.chunk_index;
```

For hybrid mode the runner additionally builds a BM25 index once
(`build_bm25_index(all_candidates)`), calls both legs per
question, and fuses with RRF.

After scoring, the runner splits `per_question` rows into rows
that have gold (counted in the average) and rows that don't
(excluded from the average). The JSON output reports both
`questions_scored` and `empty_gold_skipped` so you can tell
whether a low score is a retriever problem or a mapping problem.

---

## Datasets in detail

### `doc2dial` — Doc2Dial

The official Doc2Dial release (DMV / SSA / VA / StudentAid
domains, 488 docs / 30,653 user turns across train/val/test).
Loaded from `https://doc2dial.github.io/file/doc2dial_v1.0.1.zip`.

The eval uses a **question-first cap** (`--max-questions N`,
default 40). The loader takes the first N turns in canonical
order (`(split, dial_id, turn_id)`), derives the unique
`doc_key` set those turns reference, and ingests exactly that
doc set. There is no popularity filter — every kept turn's
source doc is in the corpus. This guarantees that every
scored question's source doc is present.

Gold per turn: each user turn carries
`references: [{"sp_id": str, "label": str}, ...]`. Spans live
inside each `doc2dial_doc.json` document body, keyed by
`(domain, doc_id, sp_id)`. The mapping step resolves each
reference to its `text_sp` and runs the same LCS-overlap
heuristic.

---

## Does the eval really use the production retrieval path?

**Yes — the same functions, called the same way.** No
parallel retriever, no shortcut:

1. **`app.repositories.embedding_search.fetch_candidate_rows`**
   is called verbatim from `eval.scripts.run_retrieval_eval`.
   Same SQLAlchemy select. Same owner/status/embedding-status
   joins. Same `CandidateRow` dataclass. Same
   `(document_id, page_number, chunk_index)` ordering.

2. **`app.services.document.analyser.rank_top_rows`** is called
   verbatim. Same NumPy cosine. Same `top_k` + `min_cosine`
   signature. Same tie-breaker (`doc_id`, `page_number`,
   `chunk_index`).

3. **`app.services.document.retrieval.hybrid_retrieve`** is called
   for hybrid mode. Same Okapi BM25 + RRF as the prod hybrid
   chat endpoint.

The differences from a real production call:

| Difference | Why |
|---|---|
| The embedder. Eval uses `EMBEDDING_PROVIDER=stub` or `qwen3` from env. | Lets us swap embedders without code edits. The active model is what `get_default_embedding_model()` returns — same singleton used by Celery workers in prod. |
| The DB. Eval reads the persisted `.cache/doc2dial/corpus.db`; prod reads the real `app.db` (or whatever `DATABASE_URL` points at). | The persisted DB is hermetic — no Celery, no Redis, no Supabase. Same schema (`Base.metadata.create_all` on the same model classes). |
| `pubsub` is patched to a no-op (`patch_pubsub_noop()`). | The Celery embedding path's status helper calls `publish_event` indirectly. Without Redis that logs warnings and slows the run. |
| `analyse_query` (the LLM half) is **not** called. | V1 is retrieval-only per ADR-015. No LLM call, no answer scoring, no faithfulness check. |

Everything between "embed query" and "rank top-k" is identical.

---

## Layout

```
backend/eval/
├── schema.py                       # Pydantic models for dataset.json / mapping
├── scripts/
│   ├── _db.py                      # Hermetic in-memory SQLite + pubsub no-op
│   │                               # + persisted_engine() for the file DB
│   ├── doc2dial_loader.py          # Downloads Doc2Dial, sorts turns, cap_by_turns
│   ├── seed_corpus.py              # Per-doc parse + chunk + embed helpers
│   ├── ingest_corpus.py            # SCRIPT 1 — drives the prod pipeline,
│   │                               # writes corpus.db + manifest.json
│   ├── build_dataset.py            # SCRIPT 2 — passage→chunk mapping,
│   │                               # writes dataset.json + passage_mapping.json
│   ├── mapping.py                  # LCS fuzzy passage→chunk match
│   ├── score.py                    # Recall@k, MRR primitives (pure)
│   └── run_retrieval_eval.py       # SCRIPT 3 — CLI runner + report writer
├── datasets/
│   └── doc2dial/                   # owner_id.txt (committed),
│                                   # dataset.json + passage_mapping.json (gitignored)
├── .cache/
│   └── doc2dial/                   # *.json (gitignored) + corpus.db + manifest.json
├── results/                        # Per-run JSON (gitignored)
├── reports/                        # Per-run Markdown (gitignored)
└── tests/                          # Pytest suite
```

## Tests

```bash
cd backend
pytest eval/tests/ -v
```

## Scope (v1)

- **Retrieval-only:** Recall@1, Recall@5, Recall@10, MRR.
- **No LLM answer scoring.** LLM faithfulness and exact-match are
  deferred (ADR-015).
- **Hybrid by default.** `--retrieval-mode hybrid` is the default
  (`dense` is opt-in). Both modes share the same scoring primitives.
- **One dataset:** `doc2dial`, with its own owner scope so
  `owner_wide` retrieval cannot cross-contaminate.
- **No CI integration.** Per ADR-015, recommended only after ≥10
  real questions and a stable baseline.