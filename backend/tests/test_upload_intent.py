"""End-to-end smoke test for the upload-intent flow.

What we verify (with Supabase unconfigured so we don't need real creds):
1. POST /upload-intent without auth -> 401
2. POST /upload-intent with bad MIME -> 415
3. POST /upload-intent with oversize file -> 413
4. POST /upload-intent with valid input -> 503 (Supabase not configured)
   AND no document rows left behind in the DB (rollback works).
"""

from __future__ import annotations

import asyncio
import os
import uuid

# Force "no Supabase" so this test never accidentally hits a real bucket.
os.environ["SUPABASE_URL"] = ""
os.environ["SUPABASE_SERVICE_ROLE_KEY"] = ""

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import get_engine
from app.main import create_app

client = TestClient(create_app())


def _register_and_login() -> str:
    email = f"upload-test-{uuid.uuid4().hex[:8]}@example.com"
    password = "CorrectHorseBatteryStaple!1"

    r = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "name": "Upload Tester"},
    )
    assert r.status_code == 201, f"register failed: {r.status_code} {r.text}"

    r = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"

    cookie = client.cookies.get(get_settings().access_cookie_name)
    assert cookie, "no access_token cookie after login"
    return cookie


def _count_documents() -> int:
    """Synchronous helper: count rows in `documents` table."""

    async def _inner() -> int:
        engine = get_engine()
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT COUNT(*) FROM documents"))
            return int(result.scalar_one())

    return asyncio.run(_inner())


def test_upload_intent_requires_auth() -> None:
    r = client.post(
        "/api/v1/documents/upload-intent",
        json={
            "title": "t",
            "filename": "f.pdf",
            "mime_type": "application/pdf",
            "file_size_bytes": 1024,
        },
    )
    assert r.status_code == 401, f"expected 401, got {r.status_code}: {r.text}"


def test_upload_intent_rejects_bad_mime() -> None:
    _register_and_login()
    r = client.post(
        "/api/v1/documents/upload-intent",
        json={
            "title": "t",
            "filename": "evil.exe",
            "mime_type": "application/x-msdownload",
            "file_size_bytes": 1024,
        },
    )
    assert r.status_code == 415, f"expected 415, got {r.status_code}: {r.text}"


def test_upload_intent_rejects_oversize() -> None:
    _register_and_login()
    r = client.post(
        "/api/v1/documents/upload-intent",
        json={
            "title": "t",
            "filename": "huge.pdf",
            "mime_type": "application/pdf",
            "file_size_bytes": 60 * 1024 * 1024,  # over the 50 MB cap
        },
    )
    assert r.status_code == 413, f"expected 413, got {r.status_code}: {r.text}"


def test_upload_intent_503_rolls_back_db_row() -> None:
    """Valid input but Supabase unconfigured → 503, with no DB row left behind."""
    baseline = _count_documents()
    _register_and_login()

    r = client.post(
        "/api/v1/documents/upload-intent",
        json={
            "title": "Quarterly report",
            "filename": "q3.pdf",
            "mime_type": "application/pdf",
            "file_size_bytes": 200 * 1024,  # 200 KB
        },
    )
    assert r.status_code == 503, f"expected 503, got {r.status_code}: {r.text}"
    body = r.json()
    assert "Storage backend" in body["detail"], body

    after = _count_documents()
    assert after == baseline, f"DB row leaked after rollback: baseline={baseline} after={after}"


if __name__ == "__main__":
    test_upload_intent_requires_auth()
    print("OK: auth required (401)")
    test_upload_intent_rejects_bad_mime()
    print("OK: bad mime rejected (415)")
    test_upload_intent_rejects_oversize()
    print("OK: oversize rejected (413)")
    test_upload_intent_503_rolls_back_db_row()
    print("OK: 503 returned and DB row rolled back")
    print("\nALL UPLOAD-INTENT TESTS PASSED")
