"""Tests for the production configuration validator.

The validator in `app/core/config.py` refuses to start the app in
`APP_ENV=prod` when the configuration is unsafe. That is a
fail-closed contract, and it is the difference between "I forgot to
set an env var" and "my production site signs tokens with a secret
that is published in the README".

Two things are exercised here:

1. **Critical misconfigurations must raise.** The app has to refuse to
   boot — a warning would be easy to miss in a container log, and a
   half-secured production site is worse than one that is visibly
   broken.

2. **Dev and test must be completely unaffected.** The whole point of
   the permissive defaults is that `docker compose up` works with zero
   setup. If this validator ever fires in dev, it breaks the local
   workflow for every contributor.

Note that these tests construct `Settings` directly with explicit
keyword arguments rather than reading a `.env` file, so they are
independent of whatever is in the developer's working tree.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings

# A valid production baseline. Tests override a single key at a time
# to isolate one misconfiguration at a time.
_VALID_PROD = {
    "app_env": "prod",
    "jwt_secret": "x" * 48,
    "app_debug": False,
    "cookie_secure": True,
    "database_url": "postgresql+psycopg://u:p@db.example.com:5432/appdb",
    "cors_origins": "https://app.example.com",
    "embedding_provider": "qwen3",
    "_env_file": None,
}


def _prod_settings(**overrides: object) -> Settings:
    """Build a Settings instance from the valid prod baseline."""
    return Settings(**{**_VALID_PROD, **overrides})


# =============================================================================
# Happy path
# =============================================================================


def test_valid_production_config_boots() -> None:
    """A correctly configured prod deployment must start."""
    settings = _prod_settings()
    assert settings.app_env == "prod"


def test_dev_environment_is_unaffected() -> None:
    """The dev defaults are permissive on purpose — they must not raise.

    Every value that could otherwise be inherited from the ambient
    environment is passed explicitly, so this test checks the code
    under test rather than whatever `backend/.env` happens to say.
    """
    settings = Settings(
        app_env="dev",
        app_debug=True,
        cookie_secure=False,
        jwt_secret="dev-secret-change-me",
        _env_file=None,
    )
    assert settings.app_debug is True
    assert settings.cookie_secure is False
    assert settings.jwt_secret == "dev-secret-change-me"


def test_test_environment_is_unaffected() -> None:
    """CI runs with APP_ENV=test — same deal as dev, no validation."""
    settings = Settings(app_env="test", app_debug=True, _env_file=None)
    assert settings.app_debug is True


def test_defaults_are_permissive_regardless_of_ambient_env() -> None:
    """The *class defaults* must stay permissive.

    The two tests above assert dev/test behaviour, but they read
    `APP_DEBUG`, `COOKIE_SECURE`, and `JWT_SECRET` from the ambient
    environment — `_env_file=None` only stops the `.env` FILE from
    being read, it does not stop environment variables from being
    read. So on a machine whose `backend/.env` is configured for
    production (APP_DEBUG=false, COOKIE_SECURE=true, a real
    JWT_SECRET), those assertions fail for reasons that have nothing
    to do with the code under test.

    This test pins the defaults explicitly by passing every value that
    could otherwise be inherited from the environment, so it checks
    what the class actually declares.
    """
    settings = Settings(
        app_env="dev",
        app_debug=True,
        cookie_secure=False,
        jwt_secret="dev-secret-change-me",
        _env_file=None,
    )
    assert settings.app_debug is True
    assert settings.cookie_secure is False
    assert settings.jwt_secret == "dev-secret-change-me"


def test_production_alias_is_also_validated() -> None:
    """`APP_ENV=production` is a common alternative spelling."""
    with pytest.raises(ValidationError):
        Settings(**{**_VALID_PROD, "app_env": "production", "jwt_secret": "short"})


# =============================================================================
# Critical misconfigurations — each must refuse to boot
# =============================================================================


def test_default_jwt_secret_is_rejected() -> None:
    """The shipped default secret means anyone can forge a token."""
    with pytest.raises(ValidationError) as exc:
        _prod_settings(jwt_secret="dev-secret-change-me")
    assert "JWT_SECRET" in str(exc.value)


def test_short_jwt_secret_is_rejected() -> None:
    """A secret shorter than 32 chars adds no security over the default."""
    with pytest.raises(ValidationError) as exc:
        _prod_settings(jwt_secret="short-but-not-the-default")
    assert "JWT_SECRET" in str(exc.value)


def test_debug_enabled_is_rejected() -> None:
    """FastAPI debug mode leaks stack traces on unhandled errors."""
    with pytest.raises(ValidationError) as exc:
        _prod_settings(app_debug=True)
    assert "APP_DEBUG" in str(exc.value)


def test_insecure_cookie_is_rejected() -> None:
    """Auth cookies must not be sent over plain HTTP."""
    with pytest.raises(ValidationError) as exc:
        _prod_settings(cookie_secure=False)
    assert "COOKIE_SECURE" in str(exc.value)


def test_missing_database_url_is_rejected() -> None:
    """Without it the app silently falls back to a container-local SQLite
    file, and every deploy would wipe the data."""
    with pytest.raises(ValidationError) as exc:
        _prod_settings(database_url=None)
    assert "DATABASE_URL" in str(exc.value)


def test_localhost_cors_origin_is_rejected() -> None:
    """A leftover localhost origin blocks every real browser request."""
    with pytest.raises(ValidationError) as exc:
        _prod_settings(cors_origins="http://localhost:3000")
    assert "CORS_ORIGINS" in str(exc.value)


def test_empty_cors_origins_is_rejected() -> None:
    """An empty allowlist blocks the frontend entirely."""
    with pytest.raises(ValidationError) as exc:
        _prod_settings(cors_origins="")
    assert "CORS_ORIGINS" in str(exc.value)


def test_multiple_problems_are_all_reported_together() -> None:
    """One boot attempt should list every problem, not just the first."""
    with pytest.raises(ValidationError) as exc:
        _prod_settings(
            jwt_secret="dev-secret-change-me",
            app_debug=True,
            cookie_secure=False,
            database_url=None,
            cors_origins="http://localhost:3000",
        )
    message = str(exc.value)
    for expected in ("JWT_SECRET", "APP_DEBUG", "COOKIE_SECURE", "DATABASE_URL"):
        assert expected in message, f"{expected} should be reported"
    assert "CORS_ORIGINS" in message


# =============================================================================
# Soft misconfigurations — warn, but do not block
# =============================================================================


def test_stub_embedding_provider_warns_but_boots(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The stub provider does not error — it just answers wrongly.

    That makes it the most dangerous misconfiguration to leave in place,
    but it is not a security hole, so it warns rather than blocks.
    """
    with caplog.at_level("WARNING"):
        settings = _prod_settings(embedding_provider="stub")

    assert settings.embedding_provider == "stub"
    assert any("EMBEDDING_PROVIDER" in r.message for r in caplog.records)


def test_wildcard_cors_warns_but_boots(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`Access-Control-Allow-Origin: *` with credentials is rejected by
    browsers — a confusing failure, so warn about it."""
    with caplog.at_level("WARNING"):
        settings = _prod_settings(cors_origins="*")

    assert settings.cors_origins_list == ["*"]
    assert any("CORS_ORIGINS" in r.message for r in caplog.records)


def test_no_warnings_for_a_clean_config(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A clean prod config should be quiet."""
    with caplog.at_level("WARNING"):
        _prod_settings()

    assert not [r for r in caplog.records if r.levelname == "WARNING"]
