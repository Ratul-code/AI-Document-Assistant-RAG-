"""Tests for the OpenAICompatibleProvider SSE stream parser and async iterator.

These tests focus on parser correctness, error handling, and the
end-to-end stream() call. They use httpx.MockTransport to avoid
hitting a real provider.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

os.environ.setdefault("ANALYSIS_LLM_API_KEY", "sk-test")

from app.services.ai.llm import (  # noqa: E402
    LLMMessage,
    LLMProviderError,
    OpenAICompatibleProvider,
    _extract_delta_content,
    _strip_sse_data_lines,
)


# ---------------------------------------------------------------------
# Parser unit tests (no I/O)
# ---------------------------------------------------------------------


def test_strip_sse_skips_empty_lines() -> None:
    assert _strip_sse_data_lines("") is None
    assert _strip_sse_data_lines("   ") is None


def test_strip_sse_skips_comments() -> None:
    assert _strip_sse_data_lines(": keepalive") is None


def test_strip_sse_skips_event_lines() -> None:
    assert _strip_sse_data_lines("event: ping") is None


def test_strip_sse_skips_done_sentinel() -> None:
    assert _strip_sse_data_lines("data: [DONE]") is None
    assert _strip_sse_data_lines("data:") is None


def test_strip_sse_returns_payload() -> None:
    out = _strip_sse_data_lines('data: {"choices":[{"delta":{"content":"hi"}}]}')
    assert out == '{"choices":[{"delta":{"content":"hi"}}]}'


def test_extract_delta_content_returns_text() -> None:
    payload = '{"choices":[{"delta":{"content":"hello"}}]}'
    assert _extract_delta_content(payload) == "hello"


def test_extract_delta_content_empty_when_absent() -> None:
    assert _extract_delta_content('{"choices":[{"delta":{}}]}') == ""
    assert _extract_delta_content("{}") == ""
    assert _extract_delta_content("not-json") == ""


# ---------------------------------------------------------------------
# End-to-end: httpx MockTransport
# ---------------------------------------------------------------------


def _sse_payload(parts: list[str]) -> bytes:
    """Build an SSE body with one data: line per token + final [DONE]."""
    out = []
    for p in parts:
        # Encode the string as a JSON string literal (double quotes,
        # with escapes) — `repr` would emit single quotes and break
        # the JSON payload.
        content_json = json.dumps(p)
        out.append(
            f'data: {{"choices":[{{"delta":{{"content":{content_json}}}}}]}}\n\n'
        )
    out.append("data: [DONE]\n\n")
    return "".join(out).encode("utf-8")


@pytest.mark.asyncio
async def test_stream_yields_each_token_then_completes(monkeypatch) -> None:
    sse = _sse_payload(["Hi", ", ", "world", "!"])

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})

    transport = httpx.MockTransport(handler)

    # Patch httpx.AsyncClient inside the provider module to inject our mock transport.
    from app.services.ai import llm as llm_mod
    orig_client = llm_mod.httpx.AsyncClient

    def _patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return orig_client(*args, **kwargs)

    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _patched_client)
    provider = OpenAICompatibleProvider()
    tokens: list[str] = []
    async for tok in provider.stream([
        LLMMessage(role="user", content="Say hi"),
    ]):
        tokens.append(tok)
    assert tokens == ["Hi", ", ", "world", "!"]


@pytest.mark.asyncio
async def test_complete_concatenates_tokens(monkeypatch) -> None:
    sse = _sse_payload(["abc", "def"])

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})

    transport = httpx.MockTransport(handler)
    from app.services.ai import llm as llm_mod
    orig_client = llm_mod.httpx.AsyncClient

    def _patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return orig_client(*args, **kwargs)

    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _patched_client)
    provider = OpenAICompatibleProvider()
    answer = await provider.complete([LLMMessage(role="user", content="hi")])
    assert answer == "abcdef"


@pytest.mark.asyncio
async def test_stream_surfaces_4xx_as_provider_error(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, content=b"rate limited")

    transport = httpx.MockTransport(handler)
    from app.services.ai import llm as llm_mod
    orig_client = llm_mod.httpx.AsyncClient

    def _patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return orig_client(*args, **kwargs)

    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _patched_client)
    provider = OpenAICompatibleProvider()
    with pytest.raises(LLMProviderError) as exc:
        async for _ in provider.stream([LLMMessage(role="user", content="x")]):
            pass
    assert exc.value.code == "PROVIDER_ERROR"
    assert "429" in exc.value.message


@pytest.mark.asyncio
async def test_stream_surfaces_missing_key_without_network(monkeypatch, caplog) -> None:
    """When ANALYSIS_LLM_API_KEY is unset, the provider refuses with
    a clear error and never opens a network connection."""
    monkeypatch.delenv("ANALYSIS_LLM_API_KEY", raising=False)
    provider = OpenAICompatibleProvider(api_key=None)
    with pytest.raises(LLMProviderError) as exc:
        async for _ in provider.stream([LLMMessage(role="user", content="x")]):
            pass
    assert "API key" in exc.value.message


@pytest.mark.asyncio
async def test_stream_propagates_cancellation(monkeypatch) -> None:
    """Cancellation should close the upstream connection cleanly."""
    started = asyncio.Event()
    finished = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        started.set()
        # Hang the body so the consumer can cancel.
        await asyncio.sleep(30)
        finished.set()
        return httpx.Response(200, content=b"")

    transport = httpx.MockTransport(handler)
    from app.services.ai import llm as llm_mod
    orig_client = llm_mod.httpx.AsyncClient

    def _patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return orig_client(*args, **kwargs)

    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _patched_client)
    provider = OpenAICompatibleProvider()

    async def runner():
        async for _ in provider.stream([LLMMessage(role="user", content="x")]):
            pass

    task = asyncio.create_task(runner())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
