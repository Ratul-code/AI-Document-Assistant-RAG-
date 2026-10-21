"""tz-aware document timestamps

Revision ID: a9b8c7d6e5f4
Revises: d7e8f9a0b123
Create Date: 2026-10-10 12:00:00.000000

The ``Document.created_at`` and ``Document.updated_at`` columns were
previously populated exclusively by a DB-side ``server_default=func.now()``
expression. On Postgres that's ``now()`` and returns ``timestamptz`` —
tz-aware, fine. On SQLite it resolves to ``CURRENT_TIMESTAMP``, which
returns a *naive* value: no tz info on the way out, so Pydantic
serializes the JSON without an offset suffix and the browser's
``new Date(iso)`` interprets it as the user's local time. The
relative-time formatter then computes the wrong delta by exactly the
user's UTC offset — e.g. "6 hours ago" right after upload for a user
~UTC-6.

Fix in the model: stamp ``created_at`` / ``updated_at`` in Python
with ``default=lambda: datetime.now(timezone.utc)`` and keep the
DB-side ``server_default=`` as a Postgres fallback. Mirrors the
existing pattern on ``Conversation.last_message_at``.

This migration is a no-op for both engines:

  * Postgres — the columns were already ``timestamptz`` and
    ``CURRENT_TIMESTAMP`` already returned tz-aware values. The
    Python default is belt-and-suspenders; no DDL change needed.
  * SQLite — the columns are stored as naive strings regardless of
    any DEFAULT expression (SQLite has no tz-aware type). The fix
    lives in the application layer (the model's Python default);
    no DDL change can make the stored values tz-aware after the
    fact. Pre-existing rows continue to round-trip as naive on
    SQLite, but the frontend's relative-time formatter is being
    updated in lockstep to coerce naive ISO strings to UTC, so
    pre-existing rows render correctly too.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "a9b8c7d6e5f4"
down_revision: Union[str, None] = "d7e8f9a0b123"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # No DDL change. The fix lives in the model (Python default
    # stamping) and the frontend (naive-ISO → UTC coercion). The
    # migration exists to mark the schema-side intent so future
    # autogenerate runs don't try to "drift" the columns back to
    # ``func.now()``-only and reintroduce the SQLite bug.
    pass


def downgrade() -> None:
    # No DDL change to reverse. The Python default and the frontend
    # coercion are independent of the schema.
    pass
