"""Tests for the reranker module (ADR-019).

Two surfaces:

1. ``NoneReranker`` — pure identity. No I/O, no deps.
2. ``get_default_reranker`` — provider dispatch via Settings + the
   process-level singleton.

The real ``CrossEncoderReranker`` requires sentence-transformers + a
~300 MB checkpoint; we exercise it indirectly by feeding the factory
a provider config and asserting the singleton is set. The detailed
forward-pass behaviour is covered by the analyser rerank tests
(``test_analyser_rerank.py``), which use a stub ``Reranker`` to drive
the orchestrator deterministically without touching torch.
"""

from __future__ import annotations

import pytest

from app.services.ai.rerankers import (
    NoneReranker,
    Reranker,
    get_default_reranker,
    reset_reranker_singleton,
)

# ---------------------------------------------------------------------
# NoneReranker
# ---------------------------------------------------------------------


def test_none_reranker_returns_input_order_with_zero_score() -> None:
    rr = NoneReranker()
    assert rr.name == "none"
    out = rr.rerank("q", ["a", "b", "c", "d"], top_k=2)
    assert out == [(0, 0.0), (1, 0.0)]


def test_none_reranker_top_k_larger_than_pool() -> None:
    out = NoneReranker().rerank("q", ["only"], top_k=5)
    assert out == [(0, 0.0)]


def test_none_reranker_empty_input() -> None:
    assert NoneReranker().rerank("q", [], top_k=5) == []


def test_none_reranker_top_k_zero() -> None:
    assert NoneReranker().rerank("q", ["a"], top_k=0) == []


# ---------------------------------------------------------------------
# Factory: provider dispatch via Settings
# ---------------------------------------------------------------------


def test_factory_returns_none_when_provider_is_none(monkeypatch) -> None:
    monkeypatch.setenv("ANALYSIS_RERANK_PROVIDER", "none")
    reset_reranker_singleton()
    rr = get_default_reranker()
    assert isinstance(rr, NoneReranker)
    assert rr.name == "none"


def test_factory_caches_singleton(monkeypatch) -> None:
    monkeypatch.setenv("ANALYSIS_RERANK_PROVIDER", "none")
    reset_reranker_singleton()
    a = get_default_reranker()
    b = get_default_reranker()
    assert a is b


def test_factory_resets_on_reset_hook(monkeypatch) -> None:
    monkeypatch.setenv("ANALYSIS_RERANK_PROVIDER", "none")
    reset_reranker_singleton()
    a = get_default_reranker()
    reset_reranker_singleton()
    b = get_default_reranker()
    assert a is not b


def test_reranker_protocol_is_a_protocol() -> None:
    """``Reranker`` is a typing Protocol — sanity check that the
    declared methods exist on a concrete implementation."""
    rr = NoneReranker()
    assert isinstance(rr, Reranker)  # structural check via runtime
    # And the methods take/return the documented shapes.
    out = rr.rerank("q", ["a", "b"], top_k=1)
    assert isinstance(out, list)
    assert all(isinstance(item, tuple) and len(item) == 2 for item in out)
    idx, score = out[0]
    assert isinstance(idx, int)
    assert isinstance(score, float)


def test_factory_falls_back_to_none_reranker_when_cross_encoder_construction_fails(
    monkeypatch, caplog
) -> None:
    """When the cross-encoder can't be constructed (e.g. sentence-
    transformers not installed in this Python), the factory MUST
    fall back to NoneReranker AND log a loud warning — never raise
    and never silently return a no-op.

    This was the bug behind "rerank gave identical numbers to
    hybrid" in the dev environment: the constructor raised
    ModuleNotFoundError, the eval helper's broad `except Exception`
    caught it, and every question silently fell back to the
    retriever's pre-rerank order.
    """
    monkeypatch.setenv("ANALYSIS_RERANK_PROVIDER", "cross_encoder")
    reset_reranker_singleton()

    import app.services.ai.rerankers as rerankers_pkg

    def _boom(*args, **kwargs):
        raise ModuleNotFoundError("No module named 'sentence_transformers' (simulated)")

    monkeypatch.setattr(rerankers_pkg, "CrossEncoderReranker", _boom)

    with caplog.at_level("WARNING", logger="app.services.ai.rerankers"):
        rr = get_default_reranker()

    # Factory did NOT raise. It returned a NoneReranker.
    assert isinstance(rr, NoneReranker)
    assert rr.name == "none"
    # And a loud warning was logged so the operator knows.
    assert any(
        "CrossEncoderReranker" in rec.message and "Falling back" in rec.message
        for rec in caplog.records
    ), f"Expected loud fallback warning, got: {[r.message for r in caplog.records]}"
