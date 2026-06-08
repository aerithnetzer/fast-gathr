"""Database models.

Schema is defined to match the docx ``Database fields`` reference.

Conventions:

* All domain primary keys are client-supplied strings (e.g. ``P-123``,
  ``L-456``). The AI agent / ingestion pipeline picks these IDs.
* Polymorphic references like ``[X-ID] where X can be P, SI, L, ...`` are
  modelled as a plain ``*_id: str`` column (no FK constraint) plus a
  sibling ``*_type`` discriminator.
* Cross-references to "tag-types" that don't have their own master table
  (``I``, ``T``, ``INT``, ``TE``, ``Concept``, ``E``, ``R``, ``A``, ``Q``)
  point at :class:`MasterVocabularyList`.
* ``vector_embedding`` columns use ``pgvector`` (1536 dims — sized for
  OpenAI ``text-embedding-3-small`` / ``ada-002``).
* Schema is owned by Alembic; ``create_all`` is intentionally NOT called
  here.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from pydantic import field_serializer
from sqlalchemy import Column
from sqlmodel import Field, SQLModel, create_engine

EMBEDDING_DIM = 1536


def _embedding_to_list(v: Any) -> list[float] | None:
    """Coerce pgvector's numpy ndarray (or any iterable) into a plain
    Python list so Pydantic can serialise it to JSON."""
    if v is None:
        return None
    if isinstance(v, list):
        return v
    return list(v)


# ── Auth tables ─────────────────────────────────────────────────────────────

class User(SQLModel, table=True):
    """Authenticated user."""
    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(unique=True, index=True, max_length=64)
    hashed_password: str = Field(max_length=256)
    is_active: bool = Field(default=True)
    is_admin: bool = Field(default=False)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ApiToken(SQLModel, table=True):
    """Long-lived API token. Plaintext shown once; only hash is retained."""
    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    token_hash: str = Field(unique=True, index=True, max_length=128)
    name: str = Field(max_length=128)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_used_at: datetime | None = Field(default=None)
    revoked_at: datetime | None = Field(default=None)


# ── Document / clause backbone ──────────────────────────────────────────────

class DocumentMetadata(SQLModel, table=True):
    __tablename__ = "documentmetadata"
    id: str = Field(max_length=32, primary_key=True)  # DocID
    record_id: str | None = Field(default=None, max_length=64)
    archival_reference: str | None = Field(default=None, max_length=256)
    archive_or_library: str | None = Field(default=None, max_length=256)
    record_title: str | None = Field(default=None, max_length=512)
    document_title: str | None = Field(default=None, max_length=512)
    document_type: str | None = Field(default=None, max_length=128)
    originating_body: str | None = Field(default=None, max_length=256)
    plaintiff: str | None = Field(default=None, max_length=256)
    defendant: str | None = Field(default=None, max_length=256)
    normalized_date: date | None = Field(default=None)
    start_date: date | None = Field(default=None)
    end_date: date | None = Field(default=None)
    regnal_year: str | None = Field(default=None, max_length=64)
    court_term: str | None = Field(default=None, max_length=64)
    reading_order: int | None = Field(default=None)
    topic: str | None = Field(default=None, max_length=512)


class Clause(SQLModel, table=True):
    id: str = Field(max_length=32, primary_key=True)  # ClauseID
    doc_id: str = Field(max_length=32, foreign_key="documentmetadata.id", index=True)
    clause_text: str = Field(default="")


# ── Master vocabulary ───────────────────────────────────────────────────────

class MasterVocabularyList(SQLModel, table=True):
    __tablename__ = "mastervocabularylist"
    id: str = Field(max_length=16, primary_key=True)
    headword: str = Field(max_length=256)
    trigger: str = Field(max_length=256)
    form: str | None = Field(default=None, max_length=256)
    classifications: str | None = Field(default=None, max_length=256)
    notes: str | None = Field(default=None, max_length=2048)
    definition: str | None = Field(default=None)
    vector_embedding: list[float] | None = Field(
        default=None, sa_column=Column(Vector(EMBEDDING_DIM))
    )

    @field_serializer("vector_embedding")
    def _ser_embedding(self, v: Any) -> list[float] | None:
        return _embedding_to_list(v)


# ── Locations / Persons / Vessels / SocialIdentity / Relationship masters ──

class Locations(SQLModel, table=True):
    id: str = Field(max_length=16, primary_key=True)
    headword: str = Field(max_length=256)
    trigger: str | None = Field(default=None, max_length=256)
    located_in_id: str | None = Field(
        default=None, max_length=16, foreign_key="locations.id"
    )
    notes: str | None = Field(default=None, max_length=2048)


class Person(SQLModel, table=True):
    id: str = Field(max_length=16, primary_key=True)
    headword: str = Field(max_length=256)
    trigger: str | None = Field(default=None, max_length=256)
    surname: str | None = Field(default=None, max_length=120)
    suffix: str | None = Field(default=None, max_length=80)
    alias: str | None = Field(default=None, max_length=120)
    sex: str = Field(default="X", max_length=2)
    imputed_birth_year: int | None = Field(default=None)
    identity_id: str | None = Field(
        default=None, max_length=16, foreign_key="socialidentity.id"
    )
    affiliation_id: str | None = Field(
        default=None, max_length=16, foreign_key="mastervocabularylist.id"
    )
    residence_id: str | None = Field(
        default=None, max_length=16, foreign_key="locations.id"
    )
    work_location_id: str | None = Field(
        default=None, max_length=16, foreign_key="locations.id"
    )
    subscription: str | None = Field(default=None, max_length=4)
    notes: str | None = Field(default=None)


class SocialIdentity(SQLModel, table=True):
    __tablename__ = "socialidentity"
    id: str = Field(max_length=16, primary_key=True)
    headword: str = Field(max_length=256)
    form: str | None = Field(default=None, max_length=1024)
    function: str | None = Field(default=None, max_length=128)
    classification: str | None = Field(default=None, max_length=128)
    notes: str | None = Field(default=None, max_length=2048)


class VesselNames(SQLModel, table=True):
    __tablename__ = "vesselnames"
    id: str = Field(max_length=16, primary_key=True)
    headword: str = Field(max_length=256)
    trigger: str | None = Field(default=None, max_length=256)
    home_port_id: str | None = Field(
        default=None, max_length=16, foreign_key="locations.id"
    )
    shipmaster_id: str | None = Field(
        default=None, max_length=16, foreign_key="person.id"
    )
    shipowner_id: str | None = Field(
        default=None, max_length=16, foreign_key="person.id"
    )


class Relationship(SQLModel, table=True):
    id: str = Field(max_length=16, primary_key=True)
    headword: str = Field(max_length=256)
    form: str | None = Field(default=None, max_length=1024)
    inverse_exists: str | None = Field(default=None, max_length=32)
    inverse_relationship: str | None = Field(default=None, max_length=32)


# ── Mentions table ──────────────────────────────────────────────────────────

class Mentions(SQLModel, table=True):
    id: str = Field(max_length=32, primary_key=True)
    clause_id: str = Field(max_length=32, foreign_key="clause.id", index=True)
    item_id: str = Field(max_length=16, foreign_key="mastervocabularylist.id", index=True)
    trigger: str | None = Field(default=None, max_length=256)


# ── Occurrences tables ──────────────────────────────────────────────────────

class PersonOccurrence(SQLModel, table=True):
    __tablename__ = "personoccurrence"
    id: str = Field(max_length=32, primary_key=True)  # P-OccurrenceID
    clause_id: str = Field(max_length=32, foreign_key="clause.id", index=True)
    person_id: str = Field(max_length=16, foreign_key="person.id", index=True)
    trigger: str | None = Field(default=None, max_length=256)
    suffix: str | None = Field(default=None, max_length=80)
    alias: str | None = Field(default=None, max_length=120)
    imputed_birth_year: int | None = Field(default=None)
    identity_id: str | None = Field(
        default=None, max_length=16, foreign_key="socialidentity.id"
    )
    affiliation_id: str | None = Field(
        default=None, max_length=16, foreign_key="mastervocabularylist.id"
    )
    residence_id: str | None = Field(
        default=None, max_length=16, foreign_key="locations.id"
    )
    work_location_id: str | None = Field(
        default=None, max_length=16, foreign_key="locations.id"
    )
    subscription: str | None = Field(default=None, max_length=4)


class VesselOccurrence(SQLModel, table=True):
    __tablename__ = "vesseloccurrence"
    id: str = Field(max_length=32, primary_key=True)  # V-OccurrenceID
    clause_id: str = Field(max_length=32, foreign_key="clause.id", index=True)
    vessel_id: str = Field(max_length=16, foreign_key="vesselnames.id", index=True)
    trigger: str | None = Field(default=None, max_length=256)
    home_port_id: str | None = Field(
        default=None, max_length=16, foreign_key="locations.id"
    )
    shipmaster_id: str | None = Field(
        default=None, max_length=16, foreign_key="person.id"
    )
    shipowner_id: str | None = Field(
        default=None, max_length=16, foreign_key="person.id"
    )


class SocialIdentityOccurrence(SQLModel, table=True):
    __tablename__ = "socialidentityoccurrence"
    id: str = Field(max_length=32, primary_key=True)  # SI-OccurrenceID
    clause_id: str = Field(max_length=32, foreign_key="clause.id", index=True)
    social_identity_id: str = Field(
        max_length=16, foreign_key="socialidentity.id", index=True
    )
    trigger: str | None = Field(default=None, max_length=256)
    sex: str | None = Field(default=None, max_length=2)
    secondary_identity_id: str | None = Field(
        default=None, max_length=16, foreign_key="socialidentity.id"
    )
    affiliation_id: str | None = Field(
        default=None, max_length=16, foreign_key="mastervocabularylist.id"
    )
    attribute_id: str | None = Field(
        default=None, max_length=16, foreign_key="mastervocabularylist.id"
    )
    residence_id: str | None = Field(
        default=None, max_length=16, foreign_key="locations.id"
    )
    work_location_id: str | None = Field(
        default=None, max_length=16, foreign_key="locations.id"
    )
    related_to_id: str | None = Field(default=None, max_length=16)
    related_to_type: str | None = Field(default=None, max_length=8)
    plural: str | None = Field(default=None, max_length=8)


class EventOccurrence(SQLModel, table=True):
    __tablename__ = "eventoccurrence"
    id: str = Field(max_length=32, primary_key=True)  # E-OccurrenceID
    clause_id: str = Field(max_length=32, foreign_key="clause.id", index=True)
    event_id: str = Field(
        max_length=16, foreign_key="mastervocabularylist.id", index=True
    )
    trigger: str | None = Field(default=None, max_length=256)
    actor_id: str | None = Field(default=None, max_length=16)
    actor_type: str | None = Field(default=None, max_length=8)
    counterparty_id: str | None = Field(default=None, max_length=16)
    counterparty_type: str | None = Field(default=None, max_length=8)
    object_id: str | None = Field(default=None, max_length=16)
    object_type: str | None = Field(default=None, max_length=8)
    means_id: str | None = Field(default=None, max_length=16)
    means_type: str | None = Field(default=None, max_length=8)
    attribute_id: str | None = Field(
        default=None, max_length=16, foreign_key="mastervocabularylist.id"
    )
    place_where_id: str | None = Field(
        default=None, max_length=16, foreign_key="locations.id"
    )
    place_from_id: str | None = Field(
        default=None, max_length=16, foreign_key="locations.id"
    )
    place_to_id: str | None = Field(
        default=None, max_length=16, foreign_key="locations.id"
    )
    when_: str | None = Field(default=None, max_length=128)
    modality: str | None = Field(default=None, max_length=64)


class RelationshipOccurrence(SQLModel, table=True):
    __tablename__ = "relationshipoccurrence"
    id: str = Field(max_length=32, primary_key=True)  # R-OccurrenceID
    clause_id: str = Field(max_length=32, foreign_key="clause.id", index=True)
    relationship_id: str = Field(
        max_length=16, foreign_key="relationship.id", index=True
    )
    trigger: str | None = Field(default=None, max_length=256)
    subject_id: str | None = Field(default=None, max_length=16)
    subject_type: str | None = Field(default=None, max_length=8)
    object_id: str | None = Field(default=None, max_length=16)
    object_type: str | None = Field(default=None, max_length=8)
    inverse: str | None = Field(default=None, max_length=64)


class AttributeOccurrence(SQLModel, table=True):
    __tablename__ = "attributeoccurrence"
    id: str = Field(max_length=32, primary_key=True)  # A-OccurrenceID
    clause_id: str = Field(max_length=32, foreign_key="clause.id", index=True)
    attribute_id: str = Field(
        max_length=16, foreign_key="mastervocabularylist.id", index=True
    )
    trigger: str | None = Field(default=None, max_length=256)
    object_id: str | None = Field(default=None, max_length=16)
    object_type: str | None = Field(default=None, max_length=8)


class QuantifiedStatementOccurrence(SQLModel, table=True):
    __tablename__ = "quantifiedstatementoccurrence"
    id: str = Field(max_length=32, primary_key=True)  # Q-OccurrenceID
    clause_id: str = Field(max_length=32, foreign_key="clause.id", index=True)
    q_id: str = Field(
        max_length=16, foreign_key="mastervocabularylist.id", index=True
    )
    trigger: str | None = Field(default=None, max_length=256)
    object_id: str | None = Field(default=None, max_length=16)
    object_type: str | None = Field(default=None, max_length=8)
    quantity: str | None = Field(default=None, max_length=64)
    unit_id: str | None = Field(
        default=None, max_length=16, foreign_key="mastervocabularylist.id"
    )
    per_quantity: str | None = Field(default=None, max_length=64)
    per_unit_id: str | None = Field(
        default=None, max_length=16, foreign_key="mastervocabularylist.id"
    )
    rate_period: str | None = Field(default=None, max_length=64)


# ── Document-attached: summaries, keywords, chat ────────────────────────────

class Summary(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    doc_id: str = Field(max_length=32, foreign_key="documentmetadata.id", index=True)
    summary_text: str = Field(default="")
    created_date: datetime = Field(default_factory=datetime.utcnow)


class Keyword(SQLModel, table=True):
    id: str = Field(max_length=32, primary_key=True)  # KeywordID
    keyword: str = Field(max_length=256)
    definition: str | None = Field(default=None)
    vector_embedding: list[float] | None = Field(
        default=None, sa_column=Column(Vector(EMBEDDING_DIM))
    )

    @field_serializer("vector_embedding")
    def _ser_embedding(self, v: Any) -> list[float] | None:
        return _embedding_to_list(v)


class KeywordOccurrence(SQLModel, table=True):
    __tablename__ = "keywordoccurrence"
    id: str = Field(max_length=32, primary_key=True)  # KeywordOccurrenceID
    doc_id: str = Field(max_length=32, foreign_key="documentmetadata.id", index=True)
    keyword_id: str = Field(max_length=32, foreign_key="keyword.id", index=True)
    trigger: str | None = Field(default=None, max_length=256)


class ChatRecord(SQLModel, table=True):
    __tablename__ = "chatrecord"
    id: int | None = Field(default=None, primary_key=True)
    doc_id: str = Field(max_length=32, foreign_key="documentmetadata.id", index=True)
    chat_text: str = Field(default="")


# ── Engine ──────────────────────────────────────────────────────────────────

postgres_url = os.getenv(
    "DATABASE_URL", "postgresql://postgres:postgres@db:5432/postgres"
)
engine = create_engine(postgres_url, echo=False)
