"""FastAPI application entry point.

Authentication model:

* ``GET /health`` is public.
* All domain endpoints (POST + GET) require a bearer token in
  ``Authorization: Bearer ...``.
* Bearer tokens may be either a JWT (from ``POST /token``) or an API token
  (from ``POST /tokens``). API tokens enable users to delegate read+write
  access to AI agents and other automation.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import Annotated, Any, Type, TypeVar

from fastapi import Body, Depends, FastAPI, HTTPException, Path, Query, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, ConfigDict
from sqlmodel import Session, SQLModel, select

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
from db import (
    ApiToken,
    AttributeOccurrence,
    ChatRecord,
    Clause,
    DocumentMetadata,
    EventOccurrence,
    Keyword,
    KeywordOccurrence,
    Locations,
    MasterVocabularyList,
    Mentions,
    Person,
    PersonOccurrence,
    QuantifiedStatementOccurrence,
    Relationship,
    RelationshipOccurrence,
    SocialIdentity,
    SocialIdentityOccurrence,
    Summary,
    User,
    VesselNames,
    VesselOccurrence,
    engine,
)


# ── App lifecycle ───────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    bootstrap_admin()
    yield


app = FastAPI(lifespan=lifespan, title="fast-gathr API")


# ── Session dependency ──────────────────────────────────────────────────────

def get_session():
    with Session(engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]
CurrentUser = Annotated[User, Depends(get_current_user)]
CurrentAdmin = Annotated[User, Depends(get_current_admin)]


# ── Auth-specific schemas ───────────────────────────────────────────────────

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


# ── API tokens (delegate access to agents) ──────────────────────────────────

@app.post(
    "/tokens",
    response_model=ApiTokenCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if row.revoked_at is None:
        row.revoked_at = datetime.now(tz=timezone.utc)
        session.add(row)
        session.commit()


# ── Generic CRUD factory ────────────────────────────────────────────────────

ModelT = TypeVar("ModelT", bound=SQLModel)


def register_crud(
    *,
    prefix: str,
    model: Type[ModelT],
    tag: str,
    id_type: type = str,
) -> None:
    """Register ``POST /<prefix>/``, ``GET /<prefix>/{id}`` and
    ``GET /<prefix>/`` endpoints for the given SQLModel.

    Both endpoints require an authenticated user.

    The request body and response schemas are derived from the model
    itself. Vector columns are intentionally not excluded — agents may
    legitimately want to write embeddings via POST.
    """

    not_found = HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"{model.__name__} not found",
    )
    safe_name = prefix.replace("-", "_")

    # Bind ``model`` / ``id_type`` to defaults so FastAPI sees concrete
    # types at decoration time (using them as annotations would leave
    # them as forward refs that Pydantic can't resolve).
    def _create(
        session: SessionDep,
        _user: CurrentUser,
        body: ModelT = Body(...),
    ):
        pk = getattr(body, "id", None)
        if pk is not None and session.get(model, pk) is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"{model.__name__} with id={pk!r} already exists",
            )
        session.add(body)
        try:
            session.commit()
        except Exception as exc:  # pragma: no cover - defensive
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc.__cause__ or exc),
            )
        session.refresh(body)
        return body

    # Patch the body parameter's annotation to the concrete model class.
    _create.__annotations__["body"] = model
    _create.__name__ = f"create_{safe_name}"

    app.post(
        f"/{prefix}/",
        response_model=model,
        status_code=status.HTTP_201_CREATED,
        tags=[tag],
        name=f"create_{safe_name}",
    )(_create)

    def _list(
        session: SessionDep,
        _user: CurrentUser,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ):
        return session.exec(select(model).offset(offset).limit(limit)).all()

    _list.__name__ = f"list_{safe_name}"

    app.get(
        f"/{prefix}/",
        response_model=list[model],
        tags=[tag],
        name=f"list_{safe_name}",
    )(_list)

    def _get(
        session: SessionDep,
        _user: CurrentUser,
        item_id: Any = Path(...),
    ):
        row = session.get(model, item_id)
        if row is None:
            raise not_found
        return row

    _get.__annotations__["item_id"] = id_type
    _get.__name__ = f"get_{safe_name}"

    app.get(
        f"/{prefix}/{{item_id}}",
        response_model=model,
        tags=[tag],
        name=f"get_{safe_name}",
    )(_get)


# ── Register every docx-defined table ───────────────────────────────────────

register_crud(prefix="vocabulary", model=MasterVocabularyList, tag="Vocabulary")
register_crud(prefix="mentions", model=Mentions, tag="Mentions")
register_crud(prefix="locations", model=Locations, tag="Locations")
register_crud(prefix="persons", model=Person, tag="Persons")
register_crud(prefix="persons-occurrences", model=PersonOccurrence, tag="Persons")
register_crud(prefix="vessels", model=VesselNames, tag="Vessels")
register_crud(prefix="vessels-occurrences", model=VesselOccurrence, tag="Vessels")
register_crud(prefix="social-identities", model=SocialIdentity, tag="SocialIdentity")
register_crud(
    prefix="social-identities-occurrences",
    model=SocialIdentityOccurrence,
    tag="SocialIdentity",
)
register_crud(prefix="relationships", model=Relationship, tag="Relationships")
register_crud(
    prefix="relationships-occurrences",
    model=RelationshipOccurrence,
    tag="Relationships",
)
register_crud(prefix="events-occurrences", model=EventOccurrence, tag="Events")
register_crud(
    prefix="attributes-occurrences", model=AttributeOccurrence, tag="Attributes"
)
register_crud(
    prefix="quantified-statements-occurrences",
    model=QuantifiedStatementOccurrence,
    tag="QuantifiedStatements",
)
register_crud(prefix="clauses", model=Clause, tag="Documents")
register_crud(prefix="documents", model=DocumentMetadata, tag="Documents")
register_crud(prefix="summaries", model=Summary, tag="Documents", id_type=int)
register_crud(prefix="keywords", model=Keyword, tag="Keywords")
register_crud(
    prefix="keywords-occurrences", model=KeywordOccurrence, tag="Keywords"
)
register_crud(prefix="chat-records", model=ChatRecord, tag="Documents", id_type=int)
