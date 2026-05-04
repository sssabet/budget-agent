"""Authentication and household authorization for the FastAPI surface.

Two ways to authenticate:

1. **Session cookie (browser, default)** — after the user signs in via Firebase
   client-side, the JS posts the Firebase ID token to /session, which mints a
   long-lived JWT cookie (HttpOnly, Secure, SameSite=Lax). Subsequent requests
   ride on the cookie. This survives iOS Safari ITP / privacy modes that wipe
   localStorage and IndexedDB out from under Firebase's client persistence —
   which is the actual fix for "I keep getting kicked back to login."

2. **Bearer ID token** — kept as a fallback for non-browser clients (CLI, the
   manual-token textarea, future mobile callers).

On first sign-in, any verified Google account is auto-provisioned:
  - a User row keyed by email (display name from the token's `name` claim,
    falling back to the email's local part)
  - a personal household named "<display name>'s budget" with the standard
    set of default categories, so the dashboard isn't blank.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, Request, status
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from app.config import settings
from app.db.init_db import DEFAULT_CATEGORIES
from app.db.repository import (
    ensure_personal_household,
    get_or_create_user,
    get_user_by_id,
    list_user_households,
)
from app.db.session import session_scope

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "budget_session"
_DEV_SECRET_WARNED = False


@dataclass(frozen=True)
class AuthenticatedUser:
    id: uuid.UUID
    email: str
    display_name: str
    households: tuple[tuple[uuid.UUID, str], ...]


def _session_secret() -> str:
    global _DEV_SECRET_WARNED
    secret = settings().session_secret
    if secret == "dev-only-not-for-production" and not _DEV_SECRET_WARNED:
        logger.warning(
            "BUDGET_AGENT_SESSION_SECRET not set — using dev fallback. "
            "Anyone who knows the fallback can mint cookies. Set the env var."
        )
        _DEV_SECRET_WARNED = True
    return secret


def make_session_token(user_id: uuid.UUID, email: str) -> str:
    """Mint a JWT used as the session cookie's value."""
    cfg = settings()
    now = int(time.time())
    payload = {
        "sub": str(user_id),
        "email": email,
        "iat": now,
        "exp": now + cfg.session_ttl_seconds,
    }
    return jwt.encode(payload, _session_secret(), algorithm="HS256")


def _verify_session_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, _session_secret(), algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


def _bearer_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token


def verify_firebase_id_token(token: str) -> dict:
    """Verify a Google/Firebase ID token and return its decoded claims.
    Raises HTTPException(401) on invalid signature/audience/expiry, 403 on
    unverified email or missing email claim. Used by both the bearer-token
    path and the /session cookie-issuing endpoint.
    """
    cfg = settings()
    verifier = google_requests.Request()
    try:
        if cfg.auth_mode == "firebase":
            if not cfg.firebase_project_id:
                raise RuntimeError("FIREBASE_PROJECT_ID is required for AUTH_MODE=firebase")
            claims = id_token.verify_firebase_token(
                token, verifier, audience=cfg.firebase_project_id
            )
        elif cfg.auth_mode == "google":
            if not cfg.google_oauth_client_id:
                raise RuntimeError("GOOGLE_OAUTH_CLIENT_ID is required for AUTH_MODE=google")
            claims = id_token.verify_oauth2_token(
                token, verifier, audience=cfg.google_oauth_client_id
            )
        else:
            raise RuntimeError(f"unsupported AUTH_MODE={cfg.auth_mode}")
    except RuntimeError:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    if claims.get("email_verified") is False:
        raise HTTPException(status_code=403, detail="email is not verified")
    if not str(claims.get("email", "")).strip():
        raise HTTPException(status_code=403, detail="token has no email claim")
    return claims


def _claims_from_token(request: Request) -> dict:
    """Verify the bearer token and return its decoded claims."""
    return verify_firebase_id_token(_bearer_token(request))


def _display_name_from_claims(claims: dict, email: str) -> str:
    raw = claims.get("name") or claims.get("given_name") or ""
    name = str(raw).strip()
    if name:
        return name[:120]
    # Fall back to the email's local part — better than blank.
    return email.split("@", 1)[0][:120] or email[:120]


def _user_from_session_cookie(request: Request) -> AuthenticatedUser | None:
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie:
        return None
    payload = _verify_session_token(cookie)
    if payload is None:
        return None
    try:
        user_id = uuid.UUID(str(payload.get("sub", "")))
    except ValueError:
        return None

    with session_scope() as s:
        user = get_user_by_id(s, user_id)
        if user is None:
            # Cookie references a user that's gone; force a fresh sign-in.
            return None
        households = list_user_households(s, user.id)
        if not households:
            household = ensure_personal_household(
                s, user, default_categories=DEFAULT_CATEGORIES
            )
            households = [household]
        return AuthenticatedUser(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            households=tuple((h.id, h.name) for h in households),
        )


def _user_from_bearer(request: Request) -> AuthenticatedUser:
    claims = _claims_from_token(request)
    email = str(claims["email"]).strip().lower()
    display_name = _display_name_from_claims(claims, email)

    with session_scope() as s:
        user, created = get_or_create_user(s, email=email, display_name=display_name)
        if created:
            logger.info("provisioned new user email=%s display=%s", email, display_name)

        # Refresh display_name on later sign-ins so a Google-side rename
        # propagates, but never blank out a name with empty input.
        if not created and display_name and user.display_name != display_name:
            user.display_name = display_name

        households = list_user_households(s, user.id)
        if not households:
            household = ensure_personal_household(
                s, user, default_categories=DEFAULT_CATEGORIES
            )
            households = [household]
            logger.info(
                "provisioned personal household for user=%s household=%s",
                email,
                household.name,
            )

        return AuthenticatedUser(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            households=tuple((h.id, h.name) for h in households),
        )


def get_current_user(request: Request) -> AuthenticatedUser:
    # Cookie first: silent, fast, survives ITP. If absent or invalid, fall
    # back to a Bearer token (CLI / mobile / manual-token escape hatch).
    cookied = _user_from_session_cookie(request)
    if cookied is not None:
        return cookied
    return _user_from_bearer(request)


def authorized_household(
    user: AuthenticatedUser, household_name: str | None
) -> tuple[uuid.UUID, str]:
    if household_name is None:
        return user.households[0]
    for household_id, name in user.households:
        if name == household_name:
            return household_id, name
    raise HTTPException(status_code=403, detail="user cannot access this household")


CurrentUser = Depends(get_current_user)
