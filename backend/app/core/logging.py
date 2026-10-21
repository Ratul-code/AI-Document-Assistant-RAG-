"""Centralized logging configuration.

Why this exists
---------------
uvicorn attaches its own `StreamHandler` to the root logger (and to
`uvicorn`, `uvicorn.error`, `uvicorn.access`). Without intervention,
app-level `logger = logging.getLogger(__name__)` would inherit that
handler but with the default WARNING level — so `logger.info(...)` in
our own code is silently dropped.

This module installs ONE clear handler on the root logger, wired to
the level the user configured, and quiets known-chatty third-party
loggers (sqlalchemy.engine, httpx) so they don't drown out real
messages.

NOTE: SQLAlchemy's `create_async_engine(..., echo=True)` attaches its
own private handler to the `sqlalchemy.engine` logger. That handler
ignores everything we do here and will print SQL lines directly. Keep
`echo=False` in `app/db/session.py` — use the engine log level to
toggle SQL tracing instead.

Idempotent: safe to call multiple times (e.g. on test reload).
"""

from __future__ import annotations

import logging
import sys

from app.core.config import get_settings
from app.core.request_id import RequestIdFilter

# Third-party loggers we want at WARNING unless the user explicitly asks
# for DEBUG via LOG_LEVEL.
_QUIET_BY_DEFAULT: dict[str, int] = {
    "sqlalchemy.engine": logging.WARNING,
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
}

# Single shared filter instance. `Filter.filter()` is called once per
# record by every handler it's attached to, so one instance is
# enough — there's no per-handler state.
_REQUEST_ID_FILTER = RequestIdFilter()


def configure_logging() -> None:
    """Install the root-logger handler and apply per-logger levels.

    Safe to call repeatedly — guards against double-handler setup.
    """
    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    root = logging.getLogger()

    # Idempotency: if our handler is already attached, just re-apply
    # levels and return.
    if any(getattr(h, "_puku_app_handler", False) for h in root.handlers):
        root.setLevel(level)
        for name, lvl in _QUIET_BY_DEFAULT.items():
            logging.getLogger(name).setLevel(lvl)
        return

    # Remove pre-existing root handlers (e.g. uvicorn's default) so we
    # control exactly one path to stderr. uvicorn's own loggers keep
    # working — they have their own handlers attached.
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(stream=sys.stderr)
    # The `[%(request_id)s]` segment is the entire point of this
    # change. `RequestIdFilter` writes either the real ID or `"-"` (a
    # dash, not empty) onto every record so the format string never
    # fails on a missing attribute. The dash sentinel keeps Celery
    # worker logs and startup logs (which never have a request ID)
    # self-explanatory instead of collapsing to `[]`.
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s [%(request_id)s] %(name)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    # Attach the filter so every record has a `request_id` attribute
    # by the time the formatter runs. Filters are the documented way
    # to inject context into the format pipeline without touching
    # every call site.
    handler.addFilter(_REQUEST_ID_FILTER)
    handler._puku_app_handler = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level)

    for name, lvl in _QUIET_BY_DEFAULT.items():
        logging.getLogger(name).setLevel(lvl)