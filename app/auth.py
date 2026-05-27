"""Authentication and authorization utilities.

Two credential types are supported, both via the standard
``Authorization: Bearer <value>`` header:

1. **JWT access tokens** — short-lived (60 min), issued by ``POST /token`` after
   username/password login. Used by humans / interactive clients.
2. **API tokens** — long-lived, opaque tokens issued by ``POST /tokens`` to a
   logged-in user. Used by AI agents and other automation that act on behalf
   of a single user. Revocable via ``DELETE /tokens/{id}``.

The ``get_current_user`` dependency accepts either credential type
transparently — it tries to decode the value as a JWT first, then falls back
to looking it up as an API token.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from passlib.context import CryptContext
from sqlmodel import Session, select

from db import ApiToken, User, engine

# ── Configuration ───────────────────────────────────────────────────────────

JWT_SECRET_KEY = os.environ["JWT_SECRET_KEY"]
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_MINUTES = 60

# API tokens are prefixed so we can distinguish them from JWTs at a glance and
# at validation time. JWTs always start with ``eyJ`` (base64-encoded ``{"``).
API_TOKEN_PREFIX = "fgk_"  # "fast-gathr key"
API_TOKEN_RANDOM_BYTES = 32

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")


# ── Password hashing ────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


# ── JWT issuing / decoding ──────────────────────────────────────────────────

def create_access_token(subject: str) -> str:
    expire = datetime.now(tz=timezone.utc) + timedelta(minutes=JWT_EXPIRE_MINUTES)
    payload = {"sub": subject, "exp": expire}
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def _decode_jwt(token: str) -> dict:
    """Decode a JWT or raise jwt exceptions. Caller handles errors."""
    return jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])


# ── API tokens ──────────────────────────────────────────────────────────────

def generate_api_token() -> str:
    """Return a fresh plaintext API token. Show this to the user once."""
    return API_TOKEN_PREFIX + secrets.token_urlsafe(API_TOKEN_RANDOM_BYTES)


def hash_api_token(plain: str) -> str:
    """Return the stored representation of an API token.

    SHA-256 is used (not bcrypt) because API tokens are high-entropy random
    strings, so a fast hash is sufficient and keeps lookup O(1) — we can
    indexed-lookup by hash rather than scanning every row.
    """
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


# ── Current-user dependency ─────────────────────────────────────────────────

_unauthenticated = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
) -> User:
    """Resolve the bearer token to a ``User``.

    Accepts either a JWT (issued by ``/token``) or an API token (issued by
    ``/tokens``). Raises 401 on any failure.
    """
    with Session(engine) as session:
        # API tokens have a known prefix — try that first.
        if token.startswith(API_TOKEN_PREFIX):
            token_row = session.exec(
                select(ApiToken).where(ApiToken.token_hash == hash_api_token(token))
            ).first()
            if token_row is None or token_row.revoked_at is not None:
                raise _unauthenticated
            user = session.get(User, token_row.user_id)
            if user is None or not user.is_active:
                raise _unauthenticated
            # Best-effort last-used update; failures here don't block the
            # request.
            try:
                token_row.last_used_at = datetime.now(tz=timezone.utc)
                session.add(token_row)
                session.commit()
            except Exception:
                session.rollback()
            return user

        # Otherwise treat as a JWT.
        try:
            payload = _decode_jwt(token)
            username = payload.get("sub")
            if not username:
                raise _unauthenticated
        except jwt.PyJWTError:
            raise _unauthenticated

        user = session.exec(select(User).where(User.username == username)).first()
        if user is None or not user.is_active:
            raise _unauthenticated
        return user


def get_current_admin(
    user: Annotated[User, Depends(get_current_user)],
) -> User:
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required",
        )
    return user


# ── Bootstrap admin ─────────────────────────────────────────────────────────

def bootstrap_admin() -> None:
    """Create the initial admin user from environment variables, if needed.

    Idempotent: if any admin already exists, this is a no-op. If either env
    var is missing, this is also a no-op (so the function is safe to call
    unconditionally on every startup).
    """
    username = os.getenv("BOOTSTRAP_ADMIN_USERNAME")
    password = os.getenv("BOOTSTRAP_ADMIN_PASSWORD")
    if not username or not password:
        return

    with Session(engine) as session:
        existing_admin = session.exec(
            select(User).where(User.is_admin == True)  # noqa: E712
        ).first()
        if existing_admin is not None:
            return

        admin = User(
            username=username,
            hashed_password=hash_password(password),
            is_active=True,
            is_admin=True,
        )
        session.add(admin)
        session.commit()
