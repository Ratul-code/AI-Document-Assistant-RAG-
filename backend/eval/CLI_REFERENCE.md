# Eval CLI Reference — quick copy-paste

All scripts live under `backend/eval/scripts/`. Run from `backend/`
with the venv active.

```bash
cd backend
source .venv/bin/activate
```

The single supported dataset is `doc2dial`:

| Name | Loader | Shape |
|---|---|---|
| `doc2dial` | `doc2dial_loader` (official JSON release, ~85 MB) | 15–20 docs × 40–50 dialogue turns (corpus = 488 docs / 30,653 user turns across train/val/test) |

The dataset gets its own:
- persisted corpus at `eval/.cache/doc2dial/corpus.db`
- manifest at `eval/.cache/doc2dial/manifest.json`
- synthetic owner at `eval/datasets/doc2dial/owner_id.txt`
- dataset + audit files at `eval/datasets/doc2dial/{dataset.json, passage_mapping.json}`

---

## The 4 scripts at a glance

```text
doc2dial_loader.py        ─ one-shot download + cache subset selection
ingest_corpus.py          ─ runs prod parse → chunk → embed for every doc
build_dataset.py          ─ maps gold passages → chunk keys (fuzzy LCS)
run_retrieval_eval.py     ─ scores R@1/5/10 + MRR, writes report
```

Run them in that order, then re-run `run_retrieval_eval.py` freely.

---

## 1. `doc2dial_loader` — Doc2Dial only

Download the doc2Dial JSON release to the local cache. Idempotent.
The loader itself does NOT pick a subset — the question cap is
applied later by `ingest_corpus.py` and `build_dataset.py` (which
derive the doc set from the chosen turns so the corpus and the
dataset stay in lockstep).

```bash
# Default — just downloads the release; no subset yet
python -m eval.scripts.doc2dial_loader

# Preview: pick first 40 turns, derive docs, show counts
python -m eval.scripts.doc2dial_loader --max-turns 40
```

Cache lives at `backend/eval/.cache/doc2dial/`. Re-running skips files
already on disk. Delete that directory to re-download.

---

## 2. `ingest_corpus` — build the persisted corpus

Drives the **same pipeline a production upload runs**:
`parse_document` → `_persist_pages` → `_chunk_pages_for_document` →
`_process_one_document` (embedder). No shortcuts.

Re-runs are no-ops unless `--force` is passed. Different embedders
require `--force` to re-seed.

### Doc2Dial

```bash
# Stub (fast — recommended for first smoke run)
python -m eval.scripts.ingest_corpus \
    --dataset doc2dial \
    --embedding-provider stub

# Real embedder
python -m eval.scripts.ingest_corpus \
    --dataset doc2dial \
    --embedding-provider qwen3 --force

# Custom question cap (default 40). The first N turns are taken in
# stable order; their source docs are then ingested. There is no
# `--max-docs` knob anymore — the doc set is always "the docs that
# back the chosen turns".
python -m eval.scripts.ingest_corpus \
    --dataset doc2dial \
    --embedding-provider stub \
    --max-questions 40
```

Outputs:
- `backend/eval/.cache/doc2dial/corpus.db` — full prod schema
- `backend/eval/.cache/doc2dial/manifest.json` — `model_name`, `embedding_dim`, `doc_count`, `chunk_count`, `question_count`
- `backend/eval/datasets/doc2dial/owner_id.txt` — written on first ingest

---

## 3. `build_dataset` — map gold passages → chunks

Reads the persisted corpus and produces two JSON files. The mapping
is `build_passage_mapping` (LCS overlap ≥ 0.5) — Doc2Dial supplies
its gold span text via the embedded spans dict.

```bash
# Doc2Dial — default behaviour
python -m eval.scripts.build_dataset --dataset doc2dial

# Common knobs:
#   --threshold 0.5                       change the LCS overlap threshold
#   --max-questions N                     cap the dataset for fast smoke runs
#   --filter-context-dependent            drop turns that depend on prior
#                                          dialogue context (short turns,
#                                          acknowledgements). Gold chunks
#                                          are unchanged.
#   --context-mode previous_agent         rewrite the question to prepend
#                                          the immediately preceding agent
#                                          turn. Use to measure retrieval
#                                          quality under context-aware
#                                          queries.
#   --context-mode topic_plus_previous    also prepend the first user turn
#                                          of the dialogue (the topic anchor)
#                                          plus the immediately preceding
#                                          agent turn.
```

#### `--filter-context-dependent` and `--context-mode` — what they change

Doc2Dial is a multi-turn dialogue dataset. Many user turns are
short acknowledgements or follow-ups ("yes", "ok", "thank you",
"what about X?") that are unanswerable as standalone queries. By
default the runner scores every turn, which depresses the
headline numbers for reasons unrelated to retrieval quality.

The two flags address it from different angles:

| Flag | Effect on rows | Effect on query string | When to use |
|---|---|---|---|
| `--filter-context-dependent` | Drops the obvious no-context turns before writing `dataset.json`. Gold chunks are unchanged. | n/a | **Standalone-question baseline.** Use to measure how your retriever performs on fair single-turn questions. |
| `--context-mode previous_agent` | No rows dropped. | Prepends the agent's last turn as bracketed context before the user turn. | Simulates a chat endpoint that always feeds the previous turn into the retrieval query. |
| `--context-mode topic_plus_previous` | No rows dropped. | Prepends the dialogue's first user turn (the topic anchor) AND the most recent agent turn. | Simulates a chat endpoint that sees the whole conversation summarised as "topic + last turn". |

Gold chunks are NEVER changed by either flag — only the query
string the retriever sees, and (for `--filter-context-dependent`)
which rows are scored.

Outputs:
- `backend/eval/datasets/doc2dial/dataset.json` — committable gold (runner reads this)
- `backend/eval/datasets/doc2dial/passage_mapping.json` — audit only (runner ignores)

---

## 4. `run_retrieval_eval` — score, write JSON + Markdown report

Drives the **real production retriever** (`fetch_candidate_rows` +
`rank_top_rows`, plus optional hybrid BM25+RRF). No parallel retriever.

The runner refuses to start unless the persisted corpus's
`model_name` matches the active embedder — catches "wrong provider
selected" and "stale DB from a different embedder" in one check.

### Scope modes (`--scope-mode`)

| Mode | What gets ranked | When |
|---|---:|---|
| **`owner_wide`** (default) | Every chunk in every doc owned by the eval owner — i.e. the **whole corpus**. | The real baseline. What the eval is designed to measure. |
| `per_question` | Only chunks from the question's `scope_document_ids`. | Debugging only — checks whether retrieval works *given* a known-good scope. Inflates scores. |

For doc2dial (15–20 docs × ~4 chunks), `owner_wide` ≈ `per_question`.
Use `per_question` only when validating the mapping/eval plumbing.

### Retrieval modes (`--retrieval-mode`)

| Mode | What it does |
|---|---|
| **`hybrid`** (default) | dense top-`--hybrid-dense-k` (default 15) + BM25 top-`--hybrid-bm25-k` (default 15) → Reciprocal Rank Fusion (`--hybrid-rrf-k` = 60). Same BM25 index reused in `owner_wide` mode. |
| `dense` | Plain cosine top-K. Legacy v1 behaviour. |

### Doc2Dial

```bash
# Default — owner_wide hybrid, top_k=5
python -m eval.scripts.run_retrieval_eval \
    --dataset doc2dial \
    --embedding-provider stub

# Top_k=3 is more informative for 1-4-chunk docs
python -m eval.scripts.run_retrieval_eval \
    --dataset doc2dial \
    --embedding-provider stub \
    --top-k 3

# Real embedder
python -m eval.scripts.run_retrieval_eval \
    --dataset doc2dial \
    --embedding-provider qwen3 \
    --top-k 3
```

### Outputs

```
backend/eval/results/doc2dial_<embedder>_<scope>_<mode>_<ts>.json   # machine-readable
backend/eval/reports/doc2dial_<embedder>_<scope>_<mode>_<ts>.md    # human-readable
```

Example:

```
backend/eval/results/doc2dial_stub_owner_wide_hybrid_20261005T140749.json
backend/eval/reports/doc2dial_stub_owner_wide_hybrid_20261005T140749.md
```

---

## Common knobs — reference

| Flag | Default | Effect |
|---|---|---|
| `--dataset` | `doc2dial` | Doc2Dial only. |
| `--embedding-provider` | `stub` | `stub` (16-dim SHA-256, fast) or `qwen3` (1024-dim, slow, real) |
| `--top-k` | dataset's `metrics_config.top_k` (5) | Recall target. Lower for tiny-doc corpora. |
| `--scope-mode` | `owner_wide` | `owner_wide` (real baseline) or `per_question` (debug only) |
| `--retrieval-mode` | `hybrid` | `hybrid` (dense + BM25 + RRF) or `dense` (cosine only) |
| `--hybrid-dense-k` | 15 | Dense leg candidates before RRF |
| `--hybrid-bm25-k` | 15 | BM25 leg candidates before RRF |
| `--hybrid-rrf-k` | 60 | RRF constant (Cormack 2009 standard — don't tune) |
| `--max-questions` | 40 (doc2dial) | Cap the question/turn list. Also the cap on ingested docs (derived from the chosen turns). |
| `--max-turns` | 40 (loader only) | Loader-only preview cap. The actual eval cap lives in `--max-questions` on `ingest_corpus` / `build_dataset` / `run_retrieval_eval`. |
| `--force` | off | Drop persisted corpus + manifest, re-seed |
| `--output-suffix` | timestamp | Custom filename suffix for results/report |
| `--threshold` | 0.5 (`build_dataset` only) | LCS overlap threshold |
| `--filter-context-dependent` | off (`build_dataset` only) | Drop short / acknowledgement turns before scoring. Produces a fair single-turn baseline. |
| `--context-mode` | `none` (`build_dataset` only) | Rewrite the question with prior dialogue context. Values: `none` (raw), `previous_agent` (1 turn back), `topic_plus_previous` (first user turn + 1 back). |

---

## Typical workflow — Doc2Dial end-to-end (40 turns)

The doc2dial cap is **question-first**: you give `--max-questions N`,
the first N turns are picked in stable order, and the doc set to
ingest is derived from those turns (every kept turn's source doc is
included, no popularity ranking). This keeps the corpus and the
dataset in lockstep — every question the runner scores has its
source doc in the corpus.

```bash
cd backend && source .venv/bin/activate

# 1. Download the release (no subset yet — caps live in ingest/build/eval)
python -m eval.scripts.doc2dial_loader

# 2. Ingest via the production pipeline. Takes the first 40 turns,
#    derives their docs, ingests exactly those.
python -m eval.scripts.ingest_corpus \
    --dataset doc2dial \
    --embedding-provider stub \
    --max-questions 40

# 3. Build the gold dataset for the same 40 turns
python -m eval.scripts.build_dataset --dataset doc2dial --max-questions 40

# 4. Score (smoke — same 40 turns, top_k=3)
python -m eval.scripts.run_retrieval_eval \
    --dataset doc2dial \
    --embedding-provider stub \
    --top-k 3 \
    --max-questions 40

# 5. Re-score with the real embedder
python -m eval.scripts.ingest_corpus \
    --dataset doc2dial \
    --embedding-provider qwen3 --force \
    --max-questions 40
python -m eval.scripts.build_dataset --dataset doc2dial --max-questions 40
python -m eval.scripts.run_retrieval_eval \
    --dataset doc2dial \
    --embedding-provider qwen3 \
    --top-k 3 \
    --max-questions 40
```

> Doc2Dial facts worth knowing before you run the runner:
> - The full corpus is **488 docs / 30,653 user turns** across
>   train/val/test (domains: dmv / ssa / va / studentaid). `--max-questions`
>   caps the *ingested* subset, and the doc set is whatever those
>   turns reference (no popularity filter).
> - Doc ids are only unique **within a domain** (e.g. `Registrations#3_0`
>   in `dmv`). The seeder hashes the composite `"<domain>::<doc_id>"`
>   into the deterministic doc UUID, so the same composite key must
>   reach `build_dataset.py` — which is exactly what
>   `load_all_turns_doc2dial` emits via `doc_key`.
> - Gold evidence is resolved from the spans dict embedded inside each
>   `doc2dial_doc.json` document body, keyed by
>   `(domain, doc_id, sp_id)`. There is no `doc2dial_doc_span.json`
>   file in the official release.
> - The runner splits the question set into rows with gold (the
>   metric average is computed over these) and rows with no gold
>   (mapping failed — excluded from the average). The JSON output
>   reports both `questions_scored` and `empty_gold_skipped` so
>   you can see whether a 0.0 score is a retrieval problem or a
>   mapping problem.

---

## What lives where

```
backend/eval/
├── scripts/
│   ├── doc2dial_loader.py        # Doc2Dial download + subset selection
│   ├── ingest_corpus.py          # drives the prod pipeline
│   ├── build_dataset.py          # passage → chunk fuzzy LCS mapping
│   ├── run_retrieval_eval.py     # CLI runner + report writer
│   ├── seed_corpus.py            # per-doc pipeline helpers
│   ├── _db.py                    # hermetic + persisted SQLite engines
│   └── mapping.py / score.py     # scoring primitives
├── datasets/
│   └── doc2dial/                 # created at runtime
│       ├── owner_id.txt
│       ├── dataset.json
│       └── passage_mapping.json
└── .cache/
    └── doc2dial/
        ├── doc2dial_doc.json
        ├── doc2dial_dial_train.json
        ├── doc2dial_dial_validation.json
        ├── doc2dial_dial_test.json
        ├── corpus.db
        └── manifest.json
```

All `.cache/` and `dataset.json` / `passage_mapping.json` files are
gitignored. The only committed eval artefacts are `owner_id.txt`
files (one per dataset) and the canonical schemas in `eval/schema.py`.