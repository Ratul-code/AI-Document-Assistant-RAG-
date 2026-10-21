"""Download Doc2Dial JSON files to a local cache.

Doc2Dial is a multi-turn dialogue dataset grounded on user-facing
documents (DMV / SSA / VA / StudentAid, ~450 docs, ~4,500 dialogues,
~14 turns each). We use a small subset (15–20 docs, 40–50 turns) as
the retrieval-eval corpus.

The eval's data-loading scripts (`ingest_corpus.py` and
`build_dataset.py`) both read the cached JSON; they never hit the
network directly. This keeps the eval hermetic after the first
download (~6 MB total).

Cache layout (after extraction from the official zip)::

    backend/eval/.cache/doc2dial/
        doc2dial_doc.json          # documents + spans
        doc2dial_dial_train.json   # training dialogues
        doc2dial_dial_validation.json
        doc2dial_dial_test.json

Actual JSON shapes:

- `doc2dial_doc.json`: a two-level nested dict
  ``{"doc_data": {<domain>: {<doc_id>: {
      "title":   str,
      "doc_id":  str,        # the same id, repeated
      "domain":  str,        # e.g. "dmv"
      "doc_text": str,       # the full document text (used as the
                              # ingestion body)
      "spans":   {<id>: {    # gold evidence spans, key = id_sp
          "id_sp":   str,
          "start_sp": int,
          "end_sp":   int,
          "text_sp":  str,   # the gold evidence text
          "tag":     "u" | "t",   # u=unit, t=title
          "id_sec":  str,    # section id
          ...
      }},
  }}}}``
  Doc ids are **unique only within a domain** (e.g. `Registrations#3_0`
  appears in `dmv`; the same id can in theory exist in another
  domain). We key spans by `(domain, doc_id, sp_id)`.

- `doc2dial_dial_*.json`: a two-level nested dict of dialogues
  ``{"dial_data": {<domain>: {<doc_id>: [
      {
        "dial_id": str,         # unique across the whole dataset
        "doc_id":  str,         # per-domain doc id
        "domain":  str,
        "turns":   [
          {
            "turn_id":    int,
            "role":       "user" | "agent",
            "da":         str,                # dialogue act
            "utterance":  str,                # the question/answer text
            "references": [{"sp_id": str, "label": str}, ...],
          }
        ],
      },
      ...
  ]}}}``
  Note: `<doc_id>` here is the **per-domain** doc id; the same doc
  can back many dialogues.

Doc2Dial is published under CC-BY-4.0. The cached JSON files are
gitignored under `backend/eval/.cache/`.

Run from `backend/` with the venv active::

    python -m eval.scripts.doc2dial_loader --max-turns 40
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

EVAL_DIR = Path(__file__).resolve().parents[1]  # .../backend/eval
CACHE_ROOT = EVAL_DIR / ".cache"


# Files we expect after unzipping the official release. The
# dialogue file is split into train / validation / test, but for
# the eval we only need the documents + training dialogues.
EXPECTED_FILES: tuple[str, ...] = (
    "doc2dial_doc.json",
    "doc2dial_dial_train.json",
    "doc2dial_dial_validation.json",
    "doc2dial_dial_test.json",
)


# Single canonical source — the official doc2dial.github.io release.
# (Earlier we tried the GitHub release tag and a `/data/` URL, both
# 404; this is the one the upstream `doc2dial.py` TFDS loader uses.)
SOURCE_URLS: tuple[str, ...] = ("https://doc2dial.github.io/file/doc2dial_v1.0.1.zip",)


def cache_dir_for(dataset: str) -> Path:
    """Return the cache directory for `dataset`. Created if missing."""
    path = CACHE_ROOT / dataset
    path.mkdir(parents=True, exist_ok=True)
    return path


def is_cached(dataset: str) -> bool:
    """True iff every expected Doc2Dial JSON file is present on disk."""
    cache = cache_dir_for(dataset)
    return all((cache / name).exists() for name in EXPECTED_FILES)


def _download_and_extract(url: str, dest_dir: Path) -> None:
    """Download a zip from `url` and extract JSON files into `dest_dir`.

    Filters to the expected filenames so the rest of the archive
    (`__MACOSX/`, READMEs, etc.) is ignored.
    """
    print(f"  [download] {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "puku-eval/1.0 (+doc2dial loader)"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = resp.read()
    with zipfile.ZipFile(BytesIO(data)) as zf:
        for member in zf.namelist():
            base = Path(member).name
            if base in EXPECTED_FILES:
                target = dest_dir / base
                with zf.open(member) as src, target.open("wb") as dst:
                    dst.write(src.read())
                print(f"  [extracted] {target}")


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------
# Helpers for the actual JSON structure
# --------------------------------------------------------------------------


def _iter_all_documents(raw_docs: dict) -> list[tuple[str, str, dict]]:
    """Flatten ``{"doc_data": {<domain>: {<doc_id>: body}}}`` into a list.

    Returns a list of ``(domain, doc_id, body)`` tuples. The body
    is the dict the official release ships (title, doc_text, spans, ...).
    """
    out: list[tuple[str, str, dict]] = []
    doc_data = raw_docs.get("doc_data", {})
    for domain, docs_in_domain in doc_data.items():
        if not isinstance(docs_in_domain, dict):
            continue
        for doc_id, body in docs_in_domain.items():
            if not isinstance(body, dict):
                continue
            out.append((str(domain), str(doc_id), body))
    return out


def _iter_all_dialogues(raw_dial: dict, split: str) -> list[dict]:
    """Flatten ``{"dial_data": {<domain>: {<doc_id>: [dial, dial, ...]}}}``
    into a list of dialogue dicts, one per `dial_id`.
    """
    out: list[dict] = []
    dial_data = raw_dial.get("dial_data", {})
    for domain, dials_in_domain in dial_data.items():
        if not isinstance(dials_in_domain, dict):
            continue
        for doc_id, dials in dials_in_domain.items():
            if not isinstance(dials, list):
                continue
            for dial in dials:
                if not isinstance(dial, dict):
                    continue
                out.append(
                    {
                        "dial_id": str(dial.get("dial_id", "")),
                        "doc_id": str(dial.get("doc_id", doc_id)),
                        "domain": str(dial.get("domain", domain)),
                        "turns": dial.get("turns", []),
                        "split": split,
                    }
                )
    return out


# --------------------------------------------------------------------------
# Public API consumed by the other eval scripts
# --------------------------------------------------------------------------


def load_documents(dataset: str) -> list[tuple[str, dict]]:
    """Return ``[(doc_key, {title, doc_id, domain, doc_text, spans, ...})]``
    for every document in the cache.

    `doc_key` is the composite ``"<domain>::<doc_id>"`` — domains
    are disjoint namespaces so this is unique across the dataset.
    """
    cache = cache_dir_for(dataset)
    if not is_cached(dataset):
        raise FileNotFoundError(
            f"Doc2Dial cache missing at {cache}. Run:\n"
            f"    python -m eval.scripts.doc2dial_loader"
        )
    raw = _read_json(cache / "doc2dial_doc.json")
    out: list[tuple[str, dict]] = []
    for domain, doc_id, body in _iter_all_documents(raw):
        out.append((f"{domain}::{doc_id}", body))
    return out


def load_doc_spans(dataset: str) -> dict[tuple[str, str, str], str]:
    """Return ``{(domain, doc_id, sp_id): span_text}`` for every gold
    evidence span in the cache.

    The triple-key accounts for the per-domain doc_id namespace.
    `sp_id` is a string (the source uses `id_sp` strings like `"4"`).
    """
    cache = cache_dir_for(dataset)
    if not is_cached(dataset):
        raise FileNotFoundError(
            f"Doc2Dial cache missing at {cache}. Run:\n"
            f"    python -m eval.scripts.doc2dial_loader"
        )
    raw = _read_json(cache / "doc2dial_doc.json")
    out: dict[tuple[str, str, str], str] = {}
    for domain, doc_id, body in _iter_all_documents(raw):
        spans = body.get("spans", {})
        if not isinstance(spans, dict):
            continue
        for sp_id, span in spans.items():
            if not isinstance(span, dict):
                continue
            text = span.get("text_sp", "")
            if text:
                out[(domain, doc_id, str(sp_id))] = str(text)
    return out


def load_dialogue_turns(dataset: str) -> list[dict]:
    """Return one row per (dialogue, turn) across train/val/test.

    Shape per row (one row == one turn)::

        {
            "id":              "doc2dial-<dial_id>-<turn_id>",
            "dial_id":         "9f44c153...",
            "turn_id":         1,
            "domain":          "dmv",
            "doc_id":          "Top 5 DMV Mistakes and How to Avoid Them#3_0",
            "doc_key":         "dmv::Top 5 DMV Mistakes and How to Avoid Them#3_0",
            "role":            "user" | "agent",
            "question":        "...",
            "answer":          "...",   # for agent turns; "" for user turns
            "utterance":       "...",
            "da":              "query_condition",
            "references":      [{"sp_id": "4", "label": "precondition"}, ...],
            "split":           "train" | "validation" | "test",
            "question_type":   "dialogue-turn",
            "prev_agent_utterance":   "..." or "",   # NEW — the agent's
                # last utterance immediately before this user turn. Used by
                # `build_dataset.py --context-mode previous_agent` to augment
                # the question text.
            "first_user_utterance":   "..." or "",   # NEW — the first user
                # turn of the dialogue (the topic anchor). Used by
                # `build_dataset.py --context-mode topic_plus_previous`.
        }
    """
    cache = cache_dir_for(dataset)
    if not is_cached(dataset):
        raise FileNotFoundError(
            f"Doc2Dial cache missing at {cache}. Run:\n"
            f"    python -m eval.scripts.doc2dial_loader"
        )
    rows: list[dict] = []
    for split, fname in (
        ("train", "doc2dial_dial_train.json"),
        ("validation", "doc2dial_dial_validation.json"),
        ("test", "doc2dial_dial_test.json"),
    ):
        path = cache / fname
        if not path.exists():
            continue
        raw = _read_json(path)
        for dial in _iter_all_dialogues(raw, split):
            dial_id = dial["dial_id"]
            doc_id = dial["doc_id"]
            domain = dial["domain"]
            doc_key = f"{domain}::{doc_id}"
            # Each user turn is paired with the agent's reply that
            # follows; we surface the question and the agent's
            # answer so the dataset row carries a coherent (Q, A)
            # for the future LLM-side eval.
            turns = dial.get("turns", [])
            # First user utterance in this dialogue — the topic
            # anchor. Doc2Dial dialogues are strict user/agent
            # alternation, so the first turn is always a user turn;
            # we still defensively guard.
            first_user_utterance = ""
            for ft in turns:
                if isinstance(ft, dict) and str(ft.get("role", "")) == "user":
                    first_user_utterance = str(ft.get("utterance", "")).strip()
                    break
            for idx, t in enumerate(turns):
                if not isinstance(t, dict):
                    continue
                role = str(t.get("role", ""))
                if role != "user":
                    continue
                turn_id = t.get("turn_id", idx)
                utterance = str(t.get("utterance", "")).strip()
                # Find the next agent turn to use as the gold answer.
                agent_answer = ""
                for nxt in turns[idx + 1 :]:
                    if isinstance(nxt, dict) and nxt.get("role") == "agent":
                        agent_answer = str(nxt.get("utterance", "")).strip()
                        break
                # Find the previous agent turn to use as immediate
                # context. Doc2Dial is strict user→agent→user→agent,
                # so `idx - 1` is always the immediately preceding
                # agent turn (when it exists).
                prev_agent_utterance = ""
                if idx > 0:
                    prev = turns[idx - 1]
                    if isinstance(prev, dict) and str(prev.get("role", "")) == "agent":
                        prev_agent_utterance = str(prev.get("utterance", "")).strip()
                rows.append(
                    {
                        "id": f"doc2dial-{dial_id}-{turn_id}",
                        "dial_id": dial_id,
                        "turn_id": turn_id,
                        "domain": domain,
                        "doc_id": doc_id,
                        "doc_key": doc_key,
                        "role": role,
                        "question": utterance,
                        "answer": agent_answer,
                        "utterance": utterance,
                        "da": str(t.get("da", "")),
                        "references": [
                            {
                                "sp_id": str(ref.get("sp_id", "")),
                                "label": str(ref.get("label", "")),
                            }
                            for ref in t.get("references", [])
                            if isinstance(ref, dict)
                        ],
                        "split": split,
                        "question_type": "dialogue-turn",
                        "prev_agent_utterance": prev_agent_utterance,
                        "first_user_utterance": first_user_utterance,
                    }
                )
    # Sort for stable ordering. Without this the row order is
    # JSON-insertion order — dialogues appear in the dict-iteration
    # order of `doc2dial_dial_*.json`, which is not stable across
    # Python versions and not consistent with the canonical
    # `(dial_id, turn_id)` order `cap_by_turns` applies. Slicing
    # `turns[:N]` from this list MUST yield the same N rows that
    # `cap_by_turns` would have picked; otherwise ingest and
    # build_dataset disagree about which docs to include.
    _SPLIT_ORDER = {"train": 0, "validation": 1, "test": 2}
    rows.sort(
        key=lambda r: (
            _SPLIT_ORDER.get(r.get("split", ""), 99),
            str(r.get("dial_id", "")),
            int(r.get("turn_id", 0) or 0),
        )
    )
    return rows


# --------------------------------------------------------------------------
# Subset selection (caps for small-eval smoke runs)
# --------------------------------------------------------------------------


def cap_by_turns(
    turns: list[dict],
    documents: list[tuple[str, dict]],
    *,
    max_turns: int,
) -> tuple[list[tuple[str, dict]], list[dict]]:
    """Pick a (documents, turns) subset driven by the question cap.

    Strategy (question-first — fixes the silent empty-gold bug the
    doc-first popularity cap produced):

      1. Sort `turns` by `(split_priority, dial_id, turn_id)` to
         match the canonical order `load_dialogue_turns` returns.
         Slicing `turns[:max_turns]` from the loader's already-sorted
         list and `cap_by_turns(turns, ..., max_turns)` from a raw
         list therefore pick the same N.
      2. Take the first `max_turns`. If `max_turns <= 0`, keep all.
      3. Derive the doc set from those turns' `doc_key` field —
         every kept question's source doc is included.
      4. Look up each `doc_key` in `documents` (which is keyed by
         `f"{domain}::{doc_id}"`). A doc referenced by 5 of the first
         40 turns is ingested once.

    Returns:
        documents_kept: every doc referenced by the chosen turns,
            in their original (cache) order.
        turns_kept: the first `max_turns` turns, sorted for stability.

    Why not cap by popular docs: a popularity cap (top-N docs by
    turn-count) can drop a doc that's needed by the question slice the
    user actually wants to score. The seeder then has no chunks for
    that doc, `build_question_row_doc2dial` silently sets
    `scope_document_ids=[]` / `relevant_chunk_keys=[]`, and the
    runner reports a 0.0 recall for what looks like a retrieval bug.
    Driving the cap from the question list keeps the corpus and the
    dataset in lockstep.
    """
    if max_turns <= 0:
        # No cap: keep everything. Equivalent to the pre-cap behaviour.
        return documents, turns

    # Same key as `load_dialogue_turns`. Keep these two in sync —
    # the seeder, the loader preview, and build_dataset must all
    # agree on what "the first N turns" means.
    _SPLIT_ORDER = {"train": 0, "validation": 1, "test": 2}

    def _turn_key(t: dict) -> tuple[int, str, int]:
        return (
            _SPLIT_ORDER.get(str(t.get("split", "")), 99),
            str(t.get("dial_id", "")),
            int(t.get("turn_id", 0) or 0),
        )

    sorted_turns = sorted(turns, key=_turn_key)
    turns_kept = sorted_turns[:max_turns]

    chosen_doc_keys: set[str] = set()
    for t in turns_kept:
        dk = t.get("doc_key", "")
        if dk:
            chosen_doc_keys.add(dk)

    documents_kept = [(d_key, body) for d_key, body in documents if d_key in chosen_doc_keys]
    return documents_kept, turns_kept


# --------------------------------------------------------------------------
# Standalone-question filter (drops turns that depend on prior context)
# --------------------------------------------------------------------------


# Patterns that strongly suggest the user turn is an acknowledgement,
# back-channel, or pure follow-up that needs dialogue context to be
# answerable. Each entry is matched case-insensitively as a prefix
# (after whitespace strip).
_ACK_PREFIXES: tuple[str, ...] = (
    "yes",
    "no",
    "no, ",
    "no.",
    "ok",
    "okay",
    "i see",
    "i understand",
    "i'm sorry",
    "i am sorry",
    "sure",
    "great",
    "right",
    "exactly",
    "correct",
    "thanks",
    "thank you",
    "got it",
    "alright",
    "fine",
)


def is_standalone_question(turn: dict) -> bool:
    """True if the user turn is answerable WITHOUT prior context.

    Heuristic (mirrors the analysis we did on the 600-turn run):

      * Length: at least 6 whitespace-separated tokens. A 1-5 word
        turn is almost always "yes", "ok", "what if X?" — the latter
        sometimes self-contained, but the marginal cases are not
        worth a more elaborate rule.
      * Not an acknowledgement-prefixed utterance. "yes, that's
        right" / "ok thank you" / "I see" are pure back-channels
        with no retrieval-relevant content.
      * Contains a question mark, OR is at least 8 words long. A
        declarative statement like "I need to renew my license" is
        still answerable (it has the topic); a question mark is
        the simplest signal that it's actually a question.

    This filter is intentionally conservative — it drops the
    obvious no-context cases and keeps the ambiguous ones.
    """
    text = (turn.get("question") or turn.get("utterance") or "").strip()
    if not text:
        return False
    tokens = text.split()
    if len(tokens) < 6:
        return False
    lower = text.lower()
    for prefix in _ACK_PREFIXES:
        if lower.startswith(prefix):
            # "no problem with my license" is a real question;
            # "no, thank you" is not. The `startswith` rule above
            # already requires at least 6 tokens, so this only
            # fires on longer acknowledgements like "ok thank you
            # very much for the help".
            if len(tokens) < 8:
                return False
            # Even if 8+ tokens, if it starts with a strong ack
            # prefix and contains no question mark, treat as
            # context-dependent.
            if "?" not in text:
                return False
            break
    return not ("?" not in text and len(tokens) < 8)


def filter_standalone_questions(turns: list[dict]) -> list[dict]:
    """Return only the user turns that are answerable standalone.

    This is the "fair single-turn question" subset. The gold
    chunks and Doc2Dial `references` are unchanged — we're only
    dropping rows where the question text is too thin to be a
    retrieval target.
    """
    return [t for t in turns if is_standalone_question(t)]


# --------------------------------------------------------------------------
# Question augmentation with prior dialogue context
# --------------------------------------------------------------------------


def augment_question(
    turn: dict,
    *,
    mode: str,
) -> str:
    """Rewrite the user-turn question to include prior dialogue context.

    Modes:
      * `"none"` — return `turn["question"]` unchanged. The default.
      * `"previous_agent"` — prepend the immediately preceding
        agent utterance, wrapped in a short prefix so the retriever
        sees it as context rather than as the query itself.
      * `"topic_plus_previous"` — prepend the first user turn of
        the dialogue (the topic anchor) AND the immediately
        preceding agent turn. This is the structured middle-ground
        that matches a typical chat endpoint's "topic + last turn"
        retrieval prompt.

    The gold chunk (Doc2Dial's `references`) is **not** changed.
    Only the string the retriever sees is rewritten.

    Args:
        turn: one row from `load_dialogue_turns()`.
        mode: one of `"none"`, `"previous_agent"`, `"topic_plus_previous"`.

    Returns:
        The augmented question string. Empty string if the row has
        no question text.
    """
    base = (turn.get("question") or turn.get("utterance") or "").strip()
    if not base:
        return base
    if mode == "none":
        return base
    if mode not in ("previous_agent", "topic_plus_previous"):
        raise ValueError(
            f"Unknown context mode {mode!r}; expected 'none', "
            f"'previous_agent', or 'topic_plus_previous'."
        )

    parts: list[str] = []
    if mode == "topic_plus_previous":
        first_user = (turn.get("first_user_utterance") or "").strip()
        # Skip the topic anchor if it equals the current question
        # (e.g. turn_id == 1) or is empty.
        if first_user and first_user != base:
            parts.append(f"[user's original question: {first_user}]")
    prev_agent = (turn.get("prev_agent_utterance") or "").strip()
    if prev_agent:
        parts.append(f"[agent just said: {prev_agent}]")
    if not parts:
        return base
    return " ".join(parts) + " " + base


# --------------------------------------------------------------------------
# CLI entry point
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="doc2dial_loader",
        description=(
            "Download Doc2Dial JSON files to the local cache. "
            "Idempotent — re-running skips files already on disk."
        ),
    )
    parser.add_argument(
        "--dataset",
        default="doc2dial",
        help="Cache directory name. Default: doc2dial.",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=40,
        help=(
            "Cap to N turns (default: 40). The doc set is then derived "
            "from those turns — every kept turn's source doc is "
            "included. The loader itself does NOT ingest; ingest happens "
            "in `ingest_corpus.py` with the same cap."
        ),
    )
    parser.add_argument(
        "--max-docs",
        type=int,
        default=0,
        help=(
            "Soft cap only. The loader warns if the derived doc set "
            "exceeds this number; it does NOT truncate. Default 0 = no "
            "warning."
        ),
    )
    args = parser.parse_args(argv)

    cache = cache_dir_for(args.dataset)
    if is_cached(args.dataset):
        print(
            f"Doc2Dial already cached under {cache} "
            f"({len(EXPECTED_FILES)} files present). "
            f"Delete the files (or the directory) to re-download."
        )
    else:
        last_err: Exception | None = None
        for url in SOURCE_URLS:
            try:
                _download_and_extract(url, cache)
                last_err = None
                break
            except Exception as exc:  # pragma: no cover
                last_err = exc
                print(f"  source {url} failed: {exc}", file=sys.stderr)
        if not is_cached(args.dataset):
            print(
                f"ERROR: failed to download Doc2Dial from any source. " f"Last error: {last_err}",
                file=sys.stderr,
            )
            return 2

    # Subset selection preview — useful so the user sees the cap.
    documents = load_documents(args.dataset)
    turns = load_dialogue_turns(args.dataset)
    documents_kept, turns_kept = cap_by_turns(turns, documents, max_turns=args.max_turns)

    sizes = {
        name: (cache / name).stat().st_size for name in EXPECTED_FILES if (cache / name).exists()
    }
    total = sum(sizes.values())
    print(
        f"Cached {args.dataset} ({total / 1024:.1f} KB total) under {cache}: "
        + ", ".join(f"{k}={v / 1024:.1f}KB" for k, v in sizes.items())
    )
    print(
        f"Subset preview: {len(documents_kept)}/{len(documents)} docs, "
        f"{len(turns_kept)}/{len(turns)} turns (max_turns={args.max_turns})."
    )
    if args.max_docs > 0 and len(documents_kept) > args.max_docs:
        print(
            f"  NOTE: derived doc set ({len(documents_kept)}) exceeds "
            f"--max-docs={args.max_docs}. The seeder will ingest all "
            f"{len(documents_kept)} (the cap is informational only)."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
