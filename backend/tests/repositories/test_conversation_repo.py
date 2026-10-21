"""Unit tests for the conversation repository.

Uses an in-memory SQLite session so the tests are independent of the
environment's Postgres / SQLite config. Models are created fresh per
session via the SQLAlchemy test base.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.models.conversation import Conversation, MessageRole
from app.models.conversation_document import ConversationDocument
from app.models.conversation_message import ConversationMessage
from app.models.document import Document, DocumentStatus
from app.models.user import User
from app.repositories import conversation as conversation_repo

# ---------------------------------------------------------------------
# Test scaffolding — in-memory SQLite, fresh schema per test.
# ---------------------------------------------------------------------


@pytest_asyncio.fixture
async def db():
    # aiosqlite + StaticPool so we share a single in-memory connection
    # across all awaits in the test (otherwise each await gets a fresh
    # empty DB and our inserts vanish).
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: sync_conn.exec_driver_sql(
                # SQLAlchemy's create_all on SQLite is fine — there's no enum types
                # to create in the dev path; the message_role column is just VARCHAR.
                ""
            )
        )
        # Manually create only the tables we need (keeps the test
        # hermetic — no Redis, no Supabase tables).
        from sqlalchemy import MetaData

        from app.db.base import Base

        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with session_factory() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def owner(db: AsyncSession) -> User:
    user = User(
        id=uuid.uuid4(),
        name="alice",
        email=f"alice-{uuid.uuid4().hex}@example.com",
        hashed_password="x",
    )
    db.add(user)
    await db.flush()
    return user


@pytest_asyncio.fixture
async def other_user(db: AsyncSession) -> User:
    user = User(
        id=uuid.uuid4(),
        name="bob",
        email=f"bob-{uuid.uuid4().hex}@example.com",
        hashed_password="x",
    )
    db.add(user)
    await db.flush()
    return user


@pytest_asyncio.fixture
async def document(db: AsyncSession, owner: User) -> Document:
    doc = Document(
        id=uuid.uuid4(),
        owner_id=owner.id,
        title="Chunker Design Notes",
        filename="notes.pdf",
        mime_type="application/pdf",
        file_size_bytes=1024,
        storage_bucket="docs",
        storage_path="x/y",
        status=DocumentStatus.ready,
    )
    db.add(doc)
    await db.flush()
    return doc


# ---------------------------------------------------------------------
# Conversation CRUD
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_get_conversation(db: AsyncSession, owner: User):
    conv = await conversation_repo.create_conversation(db, owner_id=owner.id, title="Hi")
    fetched = await conversation_repo.get_conversation(db, conv.id, owner_id=owner.id)
    assert fetched is not None
    assert fetched.id == conv.id
    assert fetched.title == "Hi"
    assert fetched.scope_all_documents is False
    assert fetched.last_message_at == conv.last_message_at


@pytest.mark.asyncio
async def test_get_conversation_other_owner_returns_none(
    db: AsyncSession, owner: User, other_user: User
):
    conv = await conversation_repo.create_conversation(db, owner_id=owner.id)
    fetched = await conversation_repo.get_conversation(db, conv.id, owner_id=other_user.id)
    assert fetched is None  # cross-user reads return None, not raising


@pytest.mark.asyncio
async def test_list_conversations_orders_by_last_message_at(db: AsyncSession, owner: User):
    a = await conversation_repo.create_conversation(db, owner_id=owner.id, title="A")
    # B and C are intentionally created only to give "A" sibling rows for the
    # last_message_at ordering test — the Python bindings are unused.
    await conversation_repo.create_conversation(db, owner_id=owner.id, title="B")
    await conversation_repo.create_conversation(db, owner_id=owner.id, title="C")

    # bump a to be newest
    msg = ConversationMessage(conversation_id=a.id, role=MessageRole.user, content="hi")
    db.add(msg)
    a.last_message_at = msg.created_at = __import__("datetime").datetime.now(
        tz=__import__("datetime").timezone.utc
    )
    await db.flush()

    items = await conversation_repo.list_conversations(db, owner_id=owner.id)
    titles = [i.title for i in items]
    assert titles[0] == "A"  # most recent
    assert set(titles) == {"A", "B", "C"}


@pytest.mark.asyncio
async def test_delete_conversation(db: AsyncSession, owner: User):
    conv = await conversation_repo.create_conversation(db, owner_id=owner.id)
    assert await conversation_repo.delete_conversation(db, conv.id, owner_id=owner.id)
    assert await conversation_repo.get_conversation(db, conv.id, owner_id=owner.id) is None


@pytest.mark.asyncio
async def test_delete_conversation_wrong_owner_returns_false(
    db: AsyncSession, owner: User, other_user: User
):
    conv = await conversation_repo.create_conversation(db, owner_id=owner.id)
    assert await conversation_repo.delete_conversation(db, conv.id, owner_id=other_user.id) is False
    # still exists
    assert await conversation_repo.get_conversation(db, conv.id, owner_id=owner.id) is not None


# ---------------------------------------------------------------------
# Doc selection
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_and_list_conversation_documents(
    db: AsyncSession, owner: User, document: Document
):
    conv = await conversation_repo.create_conversation(db, owner_id=owner.id)
    d2 = Document(
        id=uuid.uuid4(),
        owner_id=owner.id,
        title="Other",
        filename="o.pdf",
        mime_type="application/pdf",
        file_size_bytes=10,
        storage_bucket="b",
        storage_path="x",
        status=DocumentStatus.ready,
    )
    db.add(d2)
    await db.flush()

    await conversation_repo.set_conversation_documents(
        db,
        conversation_id=conv.id,
        owner_id=owner.id,
        document_ids=[document.id, d2.id],
    )
    ids = await conversation_repo.list_conversation_document_ids(
        db, conversation_id=conv.id, owner_id=owner.id
    )
    assert set(ids) == {document.id, d2.id}


@pytest.mark.asyncio
async def test_set_conversation_documents_replaces_previous(
    db: AsyncSession, owner: User, document: Document
):
    conv = await conversation_repo.create_conversation(db, owner_id=owner.id)
    await conversation_repo.set_conversation_documents(
        db, conversation_id=conv.id, owner_id=owner.id, document_ids=[document.id]
    )
    # Replace with empty selection.
    await conversation_repo.set_conversation_documents(
        db, conversation_id=conv.id, owner_id=owner.id, document_ids=[]
    )
    ids = await conversation_repo.list_conversation_document_ids(
        db, conversation_id=conv.id, owner_id=owner.id
    )
    assert ids == []


# ---------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insert_message_bumps_last_message_at(db: AsyncSession, owner: User):
    conv = await conversation_repo.create_conversation(db, owner_id=owner.id)
    before = conv.last_message_at
    msg = await conversation_repo.insert_message(
        db,
        conversation_id=conv.id,
        owner_id=owner.id,
        role=MessageRole.user,
        content="hi",
    )
    assert msg is not None
    assert msg.role == MessageRole.user
    assert msg.content == "hi"
    await db.refresh(conv)
    # Bumped to "now" — strictly >= the row's default
    assert conv.last_message_at >= before


@pytest.mark.asyncio
async def test_insert_message_unknown_conv_returns_none(db: AsyncSession, owner: User):
    msg = await conversation_repo.insert_message(
        db,
        conversation_id=uuid.uuid4(),
        owner_id=owner.id,
        role=MessageRole.user,
        content="x",
    )
    assert msg is None


@pytest.mark.asyncio
async def test_list_messages_ordered_and_owner_scoped(
    db: AsyncSession, owner: User, other_user: User
):
    conv = await conversation_repo.create_conversation(db, owner_id=owner.id)
    await conversation_repo.insert_message(
        db,
        conversation_id=conv.id,
        owner_id=owner.id,
        role=MessageRole.user,
        content="q1",
    )
    await conversation_repo.insert_message(
        db,
        conversation_id=conv.id,
        owner_id=owner.id,
        role=MessageRole.assistant,
        content="a1",
        error_code=None,
    )

    msgs = await conversation_repo.list_messages(db, conversation_id=conv.id, owner_id=owner.id)
    assert [m.role for m in msgs] == [MessageRole.user, MessageRole.assistant]
    assert [m.content for m in msgs] == ["q1", "a1"]

    # Wrong owner → empty (no cross-user leak).
    other_msgs = await conversation_repo.list_messages(
        db, conversation_id=conv.id, owner_id=other_user.id
    )
    assert other_msgs == []


@pytest.mark.asyncio
async def test_list_messages_desc_returns_newest_first(db: AsyncSession, owner: User):
    """``order="desc"`` reverses the result order — the first page of
    a paginated chat UI uses this so the newest turns render immediately
    and older turns are loaded on scroll.
    """
    conv = await conversation_repo.create_conversation(db, owner_id=owner.id)
    for content in ("q1", "a1", "q2", "a2", "q3"):
        await conversation_repo.insert_message(
            db,
            conversation_id=conv.id,
            owner_id=owner.id,
            role=(MessageRole.user if content.startswith("q") else MessageRole.assistant),
            content=content,
        )

    desc_msgs = await conversation_repo.list_messages(
        db,
        conversation_id=conv.id,
        owner_id=owner.id,
        order="desc",
    )
    assert [m.content for m in desc_msgs] == ["q3", "a2", "q2", "a1", "q1"]

    asc_msgs = await conversation_repo.list_messages(
        db,
        conversation_id=conv.id,
        owner_id=owner.id,
        order="asc",
    )
    assert [m.content for m in asc_msgs] == ["q1", "a1", "q2", "a2", "q3"]


@pytest.mark.asyncio
async def test_list_messages_desc_respects_limit_and_offset(db: AsyncSession, owner: User):
    """Pagination math should work the same way in DESC mode: offset
    skips from the newest end of the result set."""
    conv = await conversation_repo.create_conversation(db, owner_id=owner.id)
    for i in range(5):
        await conversation_repo.insert_message(
            db,
            conversation_id=conv.id,
            owner_id=owner.id,
            role=MessageRole.user,
            content=f"m{i}",
        )

    page1 = await conversation_repo.list_messages(
        db,
        conversation_id=conv.id,
        owner_id=owner.id,
        limit=2,
        offset=0,
        order="desc",
    )
    assert [m.content for m in page1] == ["m4", "m3"]

    page2 = await conversation_repo.list_messages(
        db,
        conversation_id=conv.id,
        owner_id=owner.id,
        limit=2,
        offset=2,
        order="desc",
    )
    assert [m.content for m in page2] == ["m2", "m1"]

    # And ASC mode (the legacy behaviour) keeps using offset from the
    # oldest end — used by other clients.
    page_asc = await conversation_repo.list_messages(
        db,
        conversation_id=conv.id,
        owner_id=owner.id,
        limit=2,
        offset=2,
        order="asc",
    )
    assert [m.content for m in page_asc] == ["m2", "m3"]
