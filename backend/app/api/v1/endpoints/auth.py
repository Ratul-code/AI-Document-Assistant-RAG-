"""Authentication endpoints: register, login, refresh, logout, me.

Both the access token and refresh token are delivered as HttpOnly cookies.
JavaScript cannot read either — the frontend just calls authenticated routes
and the browser sends the cookies automatically.
"""

# NOTE: this module intentionally OMITS `from __future__ import annotations`.
# slowapi's `@limiter.limit(...)` wraps the route handler, and FastAPI's
# `lenient_issubclass` check (which detects `Request` / `Response` /
# `BackgroundTasks` special parameters) requires real class objects, not
# stringified forward references. With `from __future__ import
# annotations` enabled, the wrapper's signature has string annotations
# and FastAPI treats every parameter as a query param, which 422s on
# POSTs. Dropping the future import keeps the wrapped signature's
# annotations as live class objects.

from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUserDep, DbDep
from app.core.config import get_settings
from app.core.rate_limit import limiter
from app.schemas.token import AuthResponse
from app.schemas.user import UserCreate, UserRead, UserCredential
from app.services.auth.service import (
    AuthError,
    IssuedTokens,
    login,
    logout as auth_logout,
    refresh as auth_refresh,
    register_user,
)

router = APIRouter()
_settings = get_settings()
_REFRESH_COOKIE_NAME = _settings.refresh_cookie_name
_ACCESS_COOKIE_NAME = _settings.access_cookie_name


# --- Cookie helpers --------------------------------------------------------

def _set_token_cookies(response: Response, issued: IssuedTokens) -> None:
    """Attach both tokens as HttpOnly cookies."""
    response.set_cookie(
        key=_ACCESS_COOKIE_NAME,
        value=issued.tokens.access_token,
        max_age=issued.access_max_age_seconds,
        httponly=True,
        secure=_settings.cookie_secure,
        samesite=_settings.cookie_samesite,
        path="/",
    )
    response.set_cookie(
        key=_REFRESH_COOKIE_NAME,
        value=issued.tokens.refresh_token,
        max_age=issued.refresh_max_age_seconds,
        httponly=True,
        secure=_settings.cookie_secure,
        samesite=_settings.cookie_samesite,
        path="/",
    )


def _clear_token_cookies(response: Response) -> None:
    for name in (_ACCESS_COOKIE_NAME, _REFRESH_COOKIE_NAME):
        response.delete_cookie(
            key=name,
            path="/",
            httponly=True,
            secure=_settings.cookie_secure,
            samesite=_settings.cookie_samesite,
        )


def _auth_response(issued: IssuedTokens, user_id) -> AuthResponse:
    return AuthResponse(
        user_id=user_id,
        access_token_expires_in=issued.access_max_age_seconds,
        refresh_token_expires_in=issued.refresh_max_age_seconds,
    )


# --- Register --------------------------------------------------------------

@router.post(
    "/register",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    tags=["auth"],
)
@limiter.limit("5/hour")
async def register(
    request: Request,
    response: Response,
    payload: UserCreate,
    db: DbDep,
) -> UserRead:
    """Create a new user account. Does NOT issue tokens — call /login next."""
    try:
        user = await register_user(
            db,
            name=payload.name,
            email=payload.email,
            password=payload.password,
        )
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=exc.detail
        )
    return UserRead.model_validate(user)


# --- Login -----------------------------------------------------------------

@router.post("/login", response_model=AuthResponse, tags=["auth"])
@limiter.limit("5/minute")
async def login_endpoint(
    request: Request,
    response: Response,
    payload: UserCredential,
    db: DbDep,
) -> AuthResponse:
    print("payload")
    """Verify email/password; set BOTH tokens as HttpOnly cookies."""
    try:
        user, issued = await login(
            db, email=payload.email, password=payload.password
        )
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=exc.detail
        )

    _set_token_cookies(response, issued)
    return _auth_response(issued, user.id)


# --- Refresh ---------------------------------------------------------------

@router.post("/refresh", response_model=AuthResponse, tags=["auth"])
@limiter.limit("30/minute")
async def refresh_endpoint(
    request: Request,
    response: Response,
    db: DbDep,
    refresh_token: Annotated[
        str | None, Cookie(alias=_REFRESH_COOKIE_NAME)
    ] = None,
) -> AuthResponse:
    """Rotate the refresh token. Sets new access + refresh cookies AND returns
    the new tokens in the response body so non-browser clients (e.g. Next.js
    server components) can use them directly."""
    if not refresh_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing refresh token cookie",
        )
    try:
        issued = await auth_refresh(db, raw_refresh_token=refresh_token)
    except AuthError as exc:
        _clear_token_cookies(response)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=exc.detail
        )

    _set_token_cookies(response, issued)
    return _auth_response(issued, issued.user_id)


# --- Logout ----------------------------------------------------------------

@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, tags=["auth"])
@limiter.limit("10/minute")
async def logout_endpoint(
    request: Request,
    response: Response,
    db: DbDep,
    refresh_token: Annotated[
        str | None, Cookie(alias=_REFRESH_COOKIE_NAME)
    ] = None,
) -> Response:
    """Revoke the refresh token and clear both cookies."""
    if refresh_token:
        await auth_logout(db, raw_refresh_token=refresh_token)
    _clear_token_cookies(response)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


# --- Me (current user) -----------------------------------------------------

@router.get("/me", response_model=UserRead, tags=["auth"])
@limiter.limit("60/minute")
async def me(
    request: Request,
    response: Response,
    current_user: CurrentUserDep,
) -> UserRead:
    """Return the currently authenticated user's profile.

    Authentication is resolved from the standard `Cookie:` request header.
    Works for both browser clients (cookies auto-attached) and Next.js
    server components / API routes — just forward the incoming request's
    `Cookie` header when calling this endpoint from server-side code:

        const cookieHeader = request.headers.get('cookie') ?? '';
        await fetch(`${API}/auth/me`, { headers: { Cookie: cookieHeader } });

    Returns 401 when no valid token is present.
    """
    return UserRead.model_validate(current_user)


# --- Session check (lightweight) -------------------------------------------

@router.get("/check", tags=["auth"])
async def check(
    request: Request,
    response: Response,
    access_token: Annotated[
        str | None, Cookie(alias=_ACCESS_COOKIE_NAME)
    ] = None,
) -> dict:
    """Cheap "is the access-token cookie valid?" probe for the Next.js proxy.

    Why a separate endpoint instead of /auth/me:
      - /auth/me fetches the full user row from the DB on every call
        (so the UI can render the profile). That's 200-500ms and adds
        load to the DB on every page navigation.
      - The proxy only needs a yes/no on auth — it doesn't need the
        profile. So this endpoint just verifies the JWT signature
        (no DB hit) and returns 200/401.
      - /auth/me is rate-limited at 60/min per user. The proxy calls
        this check on every matched request (every page nav, every
        RSC fetch, every prefetch, every HMR ping), which easily
        blows past 60/min and triggers 429s — which the proxy
        treats as "not authenticated" and bounces the user to
        /login. This endpoint is intentionally NOT rate-limited
        because the work it does is so cheap (a single HMAC
        verification) and the cost of a bad answer is "user gets
        redirected to /login" rather than anything security-relevant
        (the JWT signature still has to be valid; we just don't
        double-check that the user hasn't been deleted, which is
        an acceptable trade for proxy perf).

    Returns:
        200 {"authenticated": true, "user_id": "..."}  when the JWT
            signature is valid and not expired.
        401 {"authenticated": false}                   otherwise.
    """
    # Import here to avoid a circular import at module load time.
    import jwt as _jwt
    from app.core.security import decode_access_token

    if not access_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    try:
        payload = decode_access_token(access_token)
    except (_jwt.ExpiredSignatureError, _jwt.PyJWTError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )
    sub = payload.get("sub")
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
        )
    return {"authenticated": True, "user_id": sub}
