"""Request ID context plumbing.

Why this module exists
----------------------
Every incoming HTTP request is tagged with a short unique ID (the
"request ID"). That ID is the key to production-grade debuggability: it
appears on the response's `X-Request-ID` header so support can ask the
user for it, and it is stamped onto every log line emitted while that
request is being processed so the operator can `grep <id> app.log` and
get the entire trail for that one request — start, finish, every
service-level log line in between, even the traceback if it failed.

The mechanism is two pieces:

1. A module-level `ContextVar` (`_request_id_var`) that holds the
   current request's ID. `ContextVar` is the right primitive for this
   because FastAPI / Starlette run each request inside its own asyncio
   task, and a `ContextVar` set in a task is automatically visible to
   every coroutine running on that task — including the deep
   service/repository code that never sees the `Request` object. No
   need to thread `request.state.request_id` through every function
   signature.

2. A `logging.Filter` (`RequestIdFilter`) that reads the `ContextVar`
   and stamps the ID onto every `LogRecord` as `record.request_id`.
   The root log handler in `app.core.logging` installs this filter, so
   every `logger.info(...)` / `logger.exception(...)` call in the
   codebase picks up the ID with **zero changes at the call site**.

Celery worker logs (which run in a different process) never have a
`ContextVar` set, so the filter writes an empty string and the log
format omits the `[request_id]` segment. This is intentional — workers
have their own correlation via the Celery task ID and would just be
noised by an empty bracket.

`set_request_id()` / `reset_request_id(token)` are a paired
set/restore. The middleware (see `app.core.middleware`) calls them in
a `try/finally` so a request that raises still restores the previous
value and cannot leak the ID into the next request.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar, Token

# The default value is the empty string, NOT None. The logging filter
# (below) reads this var and writes it onto the LogRecord; a None
# default would make `record.request_id` None and break the log
# format string in Celery / startup code where there is no request.
_request_id_var: ContextVar[str] = ContextVar("request_id", default="")


def get_request_id() -> str:
    """Return the current request's ID, or ``""`` if outside a request.

    Safe to call from anywhere — including Celery workers and startup
    code — because the var's default is the empty string.
    """
    return _request_id_var.get()


def set_request_id(value: str) -> Token[str]:
    """Set the current request's ID. Returns a token for `reset_request_id`.

    The middleware is the only intended caller; it stores the token and
    passes it to `reset_request_id` in a `finally` block.
    """
    return _request_id_var.set(value)


def reset_request_id(token: Token[str]) -> None:
    """Restore the ContextVar to the state captured by `token`.

    This is the only sanctioned way to clear the var — direct `.set("")`
    in a finally block would race against a concurrent request that
    reused the same asyncio task (rare but possible in some test
    harnesses). `ContextVar.reset()` is the documented safe primitive.
    """
    _request_id_var.reset(token)


class RequestIdFilter(logging.Filter):
    """Inject the current request's ID onto every `LogRecord`.

    The root handler in `app.core.logging` installs one of these on its
    formatter pipeline, so by the time the formatter runs, every record
    has a `request_id` attribute — empty string for logs outside a
    request (Celery workers, startup, etc.).

    Why a Filter instead of a LoggerAdapter at every call site?
        A Filter runs once per log record at the handler level and
        requires zero changes to the ~hundreds of `logger.info(...)`
        call sites already in the codebase. Per AGENTS.md: "smallest
        appropriate change."
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # `getattr` with a default keeps the attribute present on every
        # record even if the filter is re-instantiated, so the log
        # format string can always reference %(request_id)s without
        # raising KeyError on records produced before this filter was
        # attached.
        record.request_id = get_request_id() or "-"
        return True


__all__ = [
    "RequestIdFilter",
    "get_request_id",
    "set_request_id",
    "reset_request_id",
]
