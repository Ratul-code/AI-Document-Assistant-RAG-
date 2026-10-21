"""LLM provider layer for the RAG (analysis) pipeline.

The analysis handler talks only to `LLMProvider` so swapping providers
is a single-file change. Today only the OpenAI-compatible chat
endpoint is wired — the protocol covers streaming and non-streaming;
a non-async provider can be wrapped in `asyncio.to_thread` later if
someone needs it.

Wire format: OpenAI's `chat/completions` is the de-facto compatibility
target (OpenAI, Together, Anyscale, vLLM, etc. all speak it).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import AsyncIterator, Literal, Protocol

import httpx

from app.core.config import get_llm_api_key, get_settings

logger = logging.getLogger(__name__)

LLMRole = Literal["system", "user", "assistant"]


@dataclass(frozen=True)
class LLMMessage:
    """One chat message in a request to the model."""

    role: LLMRole
    content: str


class LLMProvider(Protocol):
    """Anything the analyser can call to talk to an LLM.

    Implementations MUST honour `asyncio.CancelledError` — the analyser
    cancels the iterator when the client disconnects, and a non-
    cooperative provider will leak its upstream `httpx` request and
    waste tokens.
    """

    async def stream(self, messages: list[LLMMessage]) -> AsyncIterator[str]:
        """Yield token deltas as they arrive."""
        ...

    async def complete(self, messages: list[LLMMessage]) -> str:
        """Drain `stream()` and return the concatenated answer."""
        ...


class LLMProviderError(RuntimeError):
    """Raised by providers on transport / HTTP / parse failures."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _strip_sse_data_lines(raw: str) -> str | None:
    """Pull out the JSON payload of one SSE `data:` line.

    Returns None for SSE comments, empty lines, or the literal `[DONE]`
    sentinel. The caller yields nothing for `None`.
    """
    line = raw.strip()
    if not line:
        return None
    if line.startswith(":"):
        # SSE comment; ignore.
        return None
    if not line.startswith("data:"):
        # Some providers (e.g. proxies) prepend other events; skip them.
        return None
    payload = line[len("data:"):].strip()
    if payload == "[DONE]" or not payload:
        return None
    return payload


def _extract_delta_content(payload: str) -> str:
    """Pull `delta.content` out of one OpenAI-compatible SSE JSON payload.

    Empty / missing `content` returns "" so the caller can skip empty
    tokens without branching.
    """
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return ""
    choices = obj.get("choices") or []
    if not choices:
        return ""
    delta = choices[0].get("delta") or {}
    content = delta.get("content")
    return content if isinstance(content, str) else ""


class OpenAICompatibleProvider:
    """OpenAI-compatible chat-completions provider.

    Defaults to `https://api.openai.com/v1`. Override `base_url` for
    Together / Anyscale / vLLM / etc. The key is read at call time via
    `get_llm_api_key()`; a missing key surfaces as `LLMProviderError`
    with `code="PROVIDER_ERROR"`.
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        base_url: str | None = None,
        timeout_s: float | None = None,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
        api_key: str | None = None,
        api_key_env: str | None = None,
        http_referer: str | None = None,
        x_title: str | None = None,
    ) -> None:
        # Lazy read of Settings so tests that only import the dataclass
        # don't pay pydantic-settings import cost.
        settings = get_settings()
        self._model = model or settings.analysis_llm_model
        self._base_url = (base_url or settings.analysis_llm_base_url).rstrip("/")
        self._timeout_s = timeout_s or settings.analysis_llm_request_timeout_s
        self._max_output_tokens = (
            max_output_tokens or settings.analysis_llm_max_output_tokens
        )
        self._temperature = (
            temperature if temperature is not None else settings.analysis_llm_temperature
        )
        self._api_key_override = api_key
        # Name of the env var to read the key from. `None` → fall
        # back to the default (settings.analysis_llm_api_key_env).
        # This lets a single provider instance be pointed at a
        # different secret — e.g. for the query-rewrite LLM.
        self._api_key_env = api_key_env
        # Optional attribution headers (OpenRouter uses these to rank
        # apps on the leaderboard). `None` → header omitted entirely.
        self._http_referer = (
            http_referer if http_referer is not None
            else settings.analysis_llm_http_referer
        )
        self._x_title = (
            x_title if x_title is not None
            else settings.analysis_llm_x_title
        )

    def _resolve_api_key(self) -> str | None:
        if self._api_key_override is not None:
            return self._api_key_override
        # If the constructor was told to read from a non-default env
        # var, honour it. Otherwise use the configured default
        # (settings.analysis_llm_api_key_env), looked up via
        # os.environ directly so the secret never lands in Settings.
        if self._api_key_env is not None:
            import os
            return os.environ.get(self._api_key_env)
        return get_llm_api_key()

    def _build_payload(self, messages: list[LLMMessage]) -> dict:
        return {
            "model": self._model,
            "messages": [
                {"role": m.role, "content": m.content} for m in messages
            ],
            "max_tokens": self._max_output_tokens,
            "temperature": self._temperature,
            "stream": True,
        }

    async def stream(self, messages: list[LLMMessage]) -> AsyncIterator[str]:
        """Yield token deltas. Cancellation tears down the upstream call."""
        api_key = self._resolve_api_key()
        if not api_key:
            raise LLMProviderError(
                code="PROVIDER_ERROR",
                message=(
                    "LLM provider is not configured: missing API key in "
                    "ANALYSIS_LLM_API_KEY env var."
                ),
            )

        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        # OpenRouter (and some other gateways) recommend these for
        # attribution. Adding them is a no-op for plain OpenAI.
        if self._http_referer:
            headers["HTTP-Referer"] = self._http_referer
        if self._x_title:
            headers["X-Title"] = self._x_title
        payload = self._build_payload(messages)

        # The `with httpx.AsyncClient(...) as client` block ensures the
        # client is closed even on CancelledError or any other exception.
        timeout = httpx.Timeout(self._timeout_s)
        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                async with client.stream(
                    "POST",
                    url,
                    headers=headers,
                    json=payload,
                ) as response:
                    if response.status_code >= 400:
                        # Drain a small body for the error message; don't
                        # raise mid-stream because the headers/body code
                        # paths of httpx differ.
                        try:
                            body = await response.aread()
                            detail = body[:500].decode("utf-8", errors="replace")
                        except Exception:  # noqa: BLE001
                            detail = f"status={response.status_code}"
                        raise LLMProviderError(
                            code="PROVIDER_ERROR",
                            message=(
                                f"LLM returned {response.status_code}: {detail}"
                            ),
                        )

                    async for raw_line in response.aiter_lines():
                        # Check for cancellation between lines so a
                        # disconnect tears the request down promptly.
                        if asyncio.current_task() and asyncio.current_task().cancelled():
                            return
                        data = _strip_sse_data_lines(raw_line)
                        if data is None:
                            continue
                        text = _extract_delta_content(data)
                        if text:
                            yield text
            except asyncio.CancelledError:
                # Propagate cancellation — the `async with` blocks above
                # close the httpx response + client cleanly.
                logger.info("llm: stream cancelled by client")
                raise
            except LLMProviderError:
                raise
            except httpx.HTTPError as exc:
                raise LLMProviderError(
                    code="PROVIDER_ERROR",
                    message=f"LLM transport error: {exc}",
                ) from exc

    async def complete(self, messages: list[LLMMessage]) -> str:
        """Drain `stream()` into one string. Errors are propagated."""
        chunks: list[str] = []
        async for token in self.stream(messages):
            chunks.append(token)
        return "".join(chunks)


def get_default_llm_provider() -> LLMProvider:
    """Build the configured LLM provider.

    Currently always returns `OpenAICompatibleProvider`. Kept as a
    factory so swapping providers (or per-feature providers) is a
    one-line change.
    """
    return OpenAICompatibleProvider()


def get_rewrite_llm_provider() -> LLMProvider:
    """Build a second OpenAI-compatible provider for the query-rewrite
    step (ADR-021).

    Reads from ``analysis_query_rewrite_*`` settings instead of the
    main ``analysis_llm_*`` ones so operators can point the rewrite
    at a cheaper model (e.g. Gemini Flash-Lite) through OpenRouter
    while keeping the answer-generation model on a heavier /
    different provider.

    The wire protocol is identical — same ``OpenAICompatibleProvider``
    class, only the constructor arguments differ. Operators can
    optionally use a separate API key by setting
    ``ANALYSIS_QUERY_REWRITE_API_KEY_ENV`` to a different env-var
    name; the default reuses the same ``ANALYSIS_LLM_API_KEY``.
    """
    settings = get_settings()
    return OpenAICompatibleProvider(
        model=settings.analysis_query_rewrite_model,
        base_url=settings.analysis_query_rewrite_base_url,
        timeout_s=settings.analysis_query_rewrite_timeout_s,
        max_output_tokens=settings.analysis_query_rewrite_max_assistant_tokens,
        temperature=settings.analysis_query_rewrite_temperature,
        api_key_env=settings.analysis_query_rewrite_api_key_env,
        http_referer=settings.analysis_query_rewrite_http_referer,
        x_title=settings.analysis_query_rewrite_x_title,
    )


__all__ = [
    "LLMMessage",
    "LLMProvider",
    "LLMProviderError",
    "OpenAICompatibleProvider",
    "get_default_llm_provider",
    "get_rewrite_llm_provider",
]
