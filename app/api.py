"""FastAPI application entry point.

Authentication model:
- ``GET`` endpoints (data reads + ``/health``) are public.
- ``POST`` endpoints require a bearer token in ``Authorization: Bearer ...``.
- Bearer tokens may be either a JWT (from ``POST /token``) or an API token
  (from ``POST /tokens``). API tokens enable users to delegate write access to
  AI agents and other automation.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlmodel import Session, select

from auth import (
    bootstrap_admin,
    create_access_token,
    generate_api_token,
    get_current_admin,
    get_current_user,
    hash_api_token,
    hash_password,
    verify_password,
)
from db import ApiToken, Person, User, engine


# ── App lifecycle ───────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Bootstrap the initial admin user from env vars if no admin exists yet.
    bootstrap_admin()
    yield


app = FastAPI(lifespan=lifespan)


# ── Session dependency ──────────────────────────────────────────────────────

def get_session():
    with Session(engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]
CurrentUser = Annotated[User, Depends(get_current_user)]
CurrentAdmin = Annotated[User, Depends(get_current_admin)]


# ── Pydantic request / response shapes ──────────────────────────────────────

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserCreateRequest(BaseModel):
    username: str
    password: str
    is_admin: bool = False


class UserPublic(BaseModel):
    id: int
    username: str
    is_active: bool
    is_admin: bool


class ApiTokenCreateRequest(BaseModel):
    name: str


class ApiTokenCreateResponse(BaseModel):
    """Returned ONCE on creation. The plaintext ``token`` value cannot be
    retrieved later."""
    id: int
    name: str
    token: str


class ApiTokenPublic(BaseModel):
    id: int
    name: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


# ── Health (public) ─────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}


# ── Auth: /token (public — issues JWTs) ─────────────────────────────────────

@app.post("/token", response_model=TokenResponse)
def login(
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
    session: SessionDep,
):
    user = session.exec(
        select(User).where(User.username == form_data.username)
    ).first()
    if (
        user is None
        or not user.is_active
        or not verify_password(form_data.password, user.hashed_password)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return TokenResponse(access_token=create_access_token(subject=user.username))


# ── Users ───────────────────────────────────────────────────────────────────

@app.get("/users/me", response_model=UserPublic)
def read_current_user(user: CurrentUser):
    return UserPublic(
        id=user.id,
        username=user.username,
        is_active=user.is_active,
        is_admin=user.is_admin,
    )


@app.post("/users/", response_model=UserPublic, status_code=status.HTTP_201_CREATED)
def create_user(
    body: UserCreateRequest,
    session: SessionDep,
    _admin: CurrentAdmin,
):
    existing = session.exec(
        select(User).where(User.username == body.username)
    ).first()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username already exists",
        )
    new_user = User(
        username=body.username,
        hashed_password=hash_password(body.password),
        is_active=True,
        is_admin=body.is_admin,
    )
    session.add(new_user)
    session.commit()
    session.refresh(new_user)
    return UserPublic(
        id=new_user.id,
        username=new_user.username,
        is_active=new_user.is_active,
        is_admin=new_user.is_admin,
    )


# ── API tokens (delegate POST access to agents) ─────────────────────────────

@app.post("/tokens", response_model=ApiTokenCreateResponse, status_code=status.HTTP_201_CREATED)
def create_api_token(
    body: ApiTokenCreateRequest,
    session: SessionDep,
    user: CurrentUser,
):
    """Create a long-lived API token tied to the current user.

    The plaintext token is returned exactly once. Store it immediately —
    only its hash is retained server-side.
    """
    plaintext = generate_api_token()
    row = ApiToken(
        user_id=user.id,
        token_hash=hash_api_token(plaintext),
        name=body.name,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return ApiTokenCreateResponse(id=row.id, name=row.name, token=plaintext)


@app.get("/tokens", response_model=list[ApiTokenPublic])
def list_api_tokens(session: SessionDep, user: CurrentUser):
    rows = session.exec(
        select(ApiToken).where(ApiToken.user_id == user.id)
    ).all()
    return [
        ApiTokenPublic(
            id=r.id,
            name=r.name,
            created_at=r.created_at,
            last_used_at=r.last_used_at,
            revoked_at=r.revoked_at,
        )
        for r in rows
    ]


@app.delete("/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_api_token(
    token_id: int,
    session: SessionDep,
    user: CurrentUser,
):
    row = session.get(ApiToken, token_id)
    if row is None or row.user_id != user.id:
        # Same response whether it doesn't exist or belongs to another user —
        # avoids leaking which token IDs are valid.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if row.revoked_at is None:
        row.revoked_at = datetime.now(tz=timezone.utc)
        session.add(row)
        session.commit()


# ── Domain endpoints ────────────────────────────────────────────────────────

@app.post("/persons/")
def create_person(
    person: Person,
    session: SessionDep,
    _user: CurrentUser,
):
    session.add(person)
    session.commit()
    session.refresh(person)
    return person


@app.get("/persons/{person_id}")
def get_person(person_id: str, session: SessionDep) -> Person:
    person = session.get(Person, person_id)
    if person is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Person not found",
        )
    return person
