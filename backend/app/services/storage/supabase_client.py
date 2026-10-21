"""Thin wrapper around the Supabase Python SDK.

Centralises all Supabase calls so route handlers never import the SDK directly.
This makes it easy to:
- mock for tests
- swap providers later (e.g. to S3 or R2)
- lazy-init the client (no network until first call)
"""

from __future__ import annotations

from typing import Any, Optional

from supabase import Client, create_client

from app.core.config import Settings, get_settings


class SupabaseStorage:
    """Wraps a Supabase client and exposes only what we need.

    The underlying `Client` is created lazily on first use so that import
    doesn't fail when env vars are missing (useful for unit tests).
    """

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()
        self._client: Optional[Client] = None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _require_config(self) -> tuple[str, str]:
        """Return (url, key) or raise if either is missing."""
        url = self._settings.supabase_url
        key = self._settings.supabase_service_role_key
        if not url or not key:
            raise RuntimeError(
                "Supabase is not configured. Set SUPABASE_URL and "
                "SUPABASE_SERVICE_ROLE_KEY in your .env file."
            )
        return url, key

    def _get_client(self) -> Client:
        """Lazy-init the Supabase client."""
        if self._client is None:
            url, key = self._require_config()
            self._client = create_client(url, key)
        return self._client

    # ------------------------------------------------------------------
    # Public API (the only methods routes are allowed to call)
    # ------------------------------------------------------------------

    def create_signed_upload_url(
        self,
        bucket: str,
        storage_path: str,
    ) -> dict[str, Any]:
        """Generate a presigned URL the client can PUT a file to.

        Returns a dict like:
            {"signed_url": "https://...?token=...", "storage_path": "user/abc.pdf"}

        NOTE: Supabase's `create_signed_upload_url` does not accept a
        custom expiry — the returned signed URL is hardcoded to be valid
        for 2 hours by Supabase. If you need a different app-level TTL,
        enforce it server-side (e.g. via an `expires_at` column on the
        document row) rather than relying on the Supabase URL expiry.
        """
        client = self._get_client()
        # The supabase-py SDK exposes create_signed_upload_url on the
        # storage client. It returns a dict with `signedURL` (uppercase U).
        # No expires_in kwarg is accepted (and none is needed).
        response = client.storage.from_(bucket).create_signed_upload_url(
            storage_path,
        )
        # Normalise key name (Supabase returns "signedURL")
        signed = response.get("signedURL") or response.get("signed_url") or response.get("signedUrl")
        if not signed:
            raise RuntimeError(
                f"Supabase did not return a signed URL. Got: {response!r}"
            )
        return {"signed_url": signed, "storage_path": storage_path}

    def delete_file(self, bucket: str, storage_path: str) -> None:
        """Remove an object from a bucket. Best-effort; logs but doesn't
        raise on failure so callers can ignore cleanup errors."""
        client = self._get_client()
        try:
            client.storage.from_(bucket).remove([storage_path])
        except Exception:
            # Cleanup is best-effort; we don't want to mask the real error.
            pass

    def download_file(self, bucket: str, storage_path: str) -> bytes:
        """Download an object from a bucket as raw bytes.

        Used by the Celery `process_document` task after the client has
        finished the Supabase PUT. Raises if the object is missing or the
        network call fails — callers should treat that as a processing
        failure (mark the document FAILED, surface the error).
        """
        client = self._get_client()
        # `download()` returns bytes directly in supabase-py >= 2.x.
        data = client.storage.from_(bucket).download(storage_path)
        if not data:
            raise RuntimeError(
                f"Supabase returned empty body for {bucket}/{storage_path}"
            )
        return data

    def get_public_url(self, bucket: str, storage_path: str) -> str:
        """Return the public URL for an object (only works for public buckets)."""
        client = self._get_client()
        return client.storage.from_(bucket).get_public_url(storage_path)


# Module-level singleton (matches the pattern of get_settings()).
_supabase_storage: Optional[SupabaseStorage] = None


def get_supabase_storage() -> SupabaseStorage:
    """Return the shared SupabaseStorage instance."""
    global _supabase_storage
    if _supabase_storage is None:
        _supabase_storage = SupabaseStorage()
    return _supabase_storage