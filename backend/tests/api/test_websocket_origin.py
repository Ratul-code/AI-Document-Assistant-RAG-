"""Tests for the WebSocket Origin check (CSWSH protection).

Background
----------
A WebSocket upgrade is not subject to the browser's same-origin policy
the way a normal `fetch` is. The browser never inspects an
`Access-Control-Allow-Origin` header on a WS handshake, and it sends
ambient cookies — including our `xs` auth cookie — automatically.

That makes `/ws` vulnerable to Cross-Site WebSocket Hijacking: any
page the user visits can open `wss://our-host/ws`, the browser will
attach the session cookie, and the attacker gets an authenticated
channel as that user.

The defence is to check the handshake's `Origin` header against the
same `CORS_ORIGINS` allowlist that guards the REST API.

Policy under test:
  * no `Origin` header          -> ALLOW (non-browser clients)
  * `Origin` in the allowlist   -> ALLOW
  * `Origin` not in allowlist   -> REJECT
  * `*` in the allowlist        -> ALLOW (explicitly disables the check)

These tests exercise `_origin_is_allowed` directly with a stub
websocket, which keeps them free of DB / Redis / Celery dependencies.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.websockets.routes import _origin_is_allowed


class _FakeWebSocket:
    """Minimal stand-in for a Starlette WebSocket.

    `_origin_is_allowed` only reads `headers.get("origin")` and
    `cookies`, so a tiny stub is enough — and keeps the test fast and
    dependency-free.
    """

    def __init__(self, origin: str | None = None) -> None:
        self.headers: dict[str, str] = {}
        if origin is not None:
            self.headers["origin"] = origin
        self.cookies: dict[str, str] = {}


def _with_origins(origins: str):
    """Patch the settings so `cors_origins_list` returns `origins`."""
    from app.core.config import get_settings

    settings = get_settings()
    return patch.object(
        type(settings),
        "cors_origins_list",
        property(lambda self: settings._to_list(origins)),
    )


# =============================================================================
# Allowed cases
# =============================================================================


def test_missing_origin_is_allowed() -> None:
    """Non-browser clients (mobile apps, websocat, server-to-server) do
    not send an Origin header. Blocking them buys no security, since a
    non-browser client is not driven by a third-party web page."""
    ws = _FakeWebSocket(origin=None)
    with _with_origins("https://app.example.com"):
        assert _origin_is_allowed(ws) is True


def test_matching_origin_is_allowed() -> None:
    """The normal case: the app calls the API from its own origin."""
    ws = _FakeWebSocket(origin="https://app.example.com")
    with _with_origins("https://app.example.com"):
        assert _origin_is_allowed(ws) is True


def test_matching_origin_in_a_multi_origin_allowlist_is_allowed() -> None:
    """Several frontends can share one backend."""
    ws = _FakeWebSocket(origin="https://beta.example.com")
    with _with_origins("https://app.example.com,https://beta.example.com"):
        assert _origin_is_allowed(ws) is True


def test_trailing_slash_is_normalised() -> None:
    """`https://app.example.com/` and `https://app.example.com` are the
    same origin to a browser, so they must compare equal."""
    ws = _FakeWebSocket(origin="https://app.example.com/")
    with _with_origins("https://app.example.com"):
        assert _origin_is_allowed(ws) is True


def test_origin_case_is_normalised() -> None:
    """Host names are case-insensitive."""
    ws = _FakeWebSocket(origin="https://APP.example.com")
    with _with_origins("https://app.example.com"):
        assert _origin_is_allowed(ws) is True


def test_wildcard_allowlist_allows_everything() -> None:
    """`*` means "any origin" — it also disables the CSWSH check, which
    is why production should not use it."""
    ws = _FakeWebSocket(origin="https://evil.example.com")
    with _with_origins("*"):
        assert _origin_is_allowed(ws) is True


# =============================================================================
# Rejected cases
# =============================================================================


def test_mismatched_origin_is_rejected() -> None:
    """The actual CSWSH attack: a third-party page tries to open a
    socket using the victim's session cookie."""
    ws = _FakeWebSocket(origin="https://evil.example.com")
    with _with_origins("https://app.example.com"):
        assert _origin_is_allowed(ws) is False


def test_lookalike_origin_is_rejected() -> None:
    """A prefix of the real origin must not be accepted — this is the
    classic `app.example.com.evil.com` bypass attempt."""
    ws = _FakeWebSocket(origin="https://app.example.com.evil.com")
    with _with_origins("https://app.example.com"):
        assert _origin_is_allowed(ws) is False


def test_http_origin_is_rejected_against_https_allowlist() -> None:
    """Scheme is part of the origin. An http:// origin on a site whose
    allowlist is https:// is a different origin and must not match."""
    ws = _FakeWebSocket(origin="http://app.example.com")
    with _with_origins("https://app.example.com"):
        assert _origin_is_allowed(ws) is False


def test_empty_allowlist_rejects_a_present_origin() -> None:
    """With no allowlist configured there is nothing to match against.
    Refusing is safer than assuming the operator meant "allow all"."""
    ws = _FakeWebSocket(origin="https://app.example.com")
    with _with_origins(""):
        assert _origin_is_allowed(ws) is False
