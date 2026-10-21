"""create conversations, conversation_documents, conversation_messages

Revision ID: d7e8f9a0b123
Revises: c6d9e0f1a234
Create Date: 2026-09-27 10:00:00.000000

Adds the chat-history layer on top of the existing retrieval pipeline:

* ``conversations`` — one row per chat thread, owned by a user. Carries
  a boolean ``scope_all_documents`` flag plus a join table that pins a
  document-selection scope to the thread.
* ``conversation_documents`` — composite-PK join table
  ``(conversation_id, document_id)`` for the explicit (non-all) scope.
* ``conversation_messages`` — one row per turn (user OR assistant).
  ``citations`` is JSONB on Postgres / JSON on SQLite.

All three tables CASCADE from ``users`` and ``conversations`` /
``documents`` so deleting a user, conversation, or document cleans up
its chat history references.

The ``message_role`` enum lives on Postgres only; SQLite stores it as
VARCHAR (no native enums there).
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "d7e8f9a0b123"
down_revision: Union[str, None] = "c6d9e0f1a234"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_MESSAGE_ROLE_VALUES = ("user", "assistant")

_message_role_enum = postgresql.ENUM(
    *_MESSAGE_ROLE_VALUES,
    name="message_role",
    create_type=True,
)


def upgrade() -> None:
    bind = op.get_bind()

    # ---- conversations -------------------------------------------------
    op.create_table(
        "conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column(
            "scope_all_documents",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # Sidebar sort: most recent chats per user.
    op.create_index(
        op.f("ix_conversations_owner_last_message_at"),
        "conversations",
        ["owner_id", "last_message_at"],
        unique=False,
    )
    # last_message_at defaults to NOW() so brand-new conversations are
    # already ordered consistently even before their first message.
    # Postgres supports `ALTER COLUMN ... SET DEFAULT` directly; SQLite
    # has no such syntax and would need a table-rebuild, so we skip on
    # SQLite — the model sets last_message_at explicitly in app code
    # on insert, and the sidebar's ORDER BY works without a DB default.
    if bind.dialect.name == "postgresql":
        op.execute(
            "ALTER TABLE conversations "
            "ALTER COLUMN last_message_at SET DEFAULT (CURRENT_TIMESTAMP)"
        )

    # ---- conversation_documents ----------------------------------------
    op.create_table(
        "conversation_documents",
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["document_id"], ["documents.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("conversation_id", "document_id"),
    )
    # Reverse lookup: which chats use this document?
    op.create_index(
        op.f("ix_conversation_documents_document_id"),
        "conversation_documents",
        ["document_id"],
        unique=False,
    )

    # ---- conversation_messages -----------------------------------------
    # Postgres gets a native ENUM; SQLite stores the values as plain
    # VARCHAR via ``native_enum=False`` (no DDL ENUM type needed).
    if bind.dialect.name == "postgresql":
        _message_role_enum.create(bind, checkfirst=True)
        role_column_type = postgresql.ENUM(
            *_MESSAGE_ROLE_VALUES,
            name="message_role",
            create_type=False,
        )
    else:
        role_column_type = sa.Enum(
            *_MESSAGE_ROLE_VALUES,
            name="message_role",
            create_type=False,
            native_enum=False,
            length=20,
        )

    # citations is JSONB on Postgres (queryable), plain JSON on SQLite.
    if bind.dialect.name == "postgresql":
        citations_column_type = postgresql.JSONB()
    else:
        citations_column_type = sa.JSON()

    op.create_table(
        "conversation_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("role", role_column_type, nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("citations", citations_column_type, nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # Chat-history fetch: ORDER BY created_at inside a conversation.
    op.create_index(
        op.f("ix_conversation_messages_conversation_created_at"),
        "conversation_messages",
        ["conversation_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_index(
        op.f("ix_conversation_messages_conversation_created_at"),
        table_name="conversation_messages",
    )
    op.drop_table("conversation_messages")
    if bind.dialect.name == "postgresql":
        _message_role_enum.drop(bind, checkfirst=True)

    op.drop_index(
        op.f("ix_conversation_documents_document_id"),
        table_name="conversation_documents",
    )
    op.drop_table("conversation_documents")

    op.drop_index(
        op.f("ix_conversations_owner_last_message_at"),
        table_name="conversations",
    )
    op.drop_table("conversations")
