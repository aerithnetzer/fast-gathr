from typing import Any

from pydantic.types import Json
from sqlmodel import JSON, Column, Field, SQLModel, create_engine
from datetime import date, datetime


class User(SQLModel, table=True):
    """Authenticated user. Created either via the bootstrap mechanism on first
    startup or via POST /users/ by an admin."""
    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(unique=True, index=True, max_length=64)
    hashed_password: str = Field(max_length=256)
    is_active: bool = Field(default=True)
    is_admin: bool = Field(default=False)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ApiToken(SQLModel, table=True):
    """Long-lived API token issued by a user, typically delegated to an AI
    agent or other service. The plaintext token is shown only once at
    creation; only its hash is stored."""
    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    token_hash: str = Field(unique=True, index=True, max_length=128)
    name: str = Field(max_length=128)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_used_at: datetime | None = Field(default=None)
    revoked_at: datetime | None = Field(default=None)


class MasterVocabularyList(SQLModel, table=True):
    id: str = Field(max_length=8, primary_key=True)
    headword: str = Field(max_length=256)
    trigger: str = Field()
    form: str = Field(max_length=256)
    classifications: str = Field(max_length=256)
    notes: str = Field(max_length=1024)

class Mentions(SQLModel, table=True):
    id: str = Field(max_length=8, primary_key=True)
    headword: str = Field(max_length=256)
    form: str = Field(max_length=256)
    classifications: str = Field(max_length=256)
    notes: str = Field(max_length=1024)

class Ocurrences(SQLModel, table=True):
    id: str = Field(max_length=8, primary_key=True)
    headword: str = Field(max_length=256)
    form: str = Field(max_length=256)
    classifications: str = Field(max_length=256)
    notes: str = Field(max_length=1024)

class Occupation(SQLModel, table=True):
    id: str = Field(max_length=8, primary_key=True)


class Office(SQLModel, table=True):
    id: str = Field(max_length=8, primary_key=True)


class Status(SQLModel, table=True):
    id: str = Field(max_length=8, primary_key=True)


class Residence(SQLModel, table=True):
    id: str = Field(max_length=8, primary_key=True)


class WorkLocation(SQLModel, table=True):
    id: str = Field(max_length=8, primary_key=True)


class Person(SQLModel, table=True):
    id: str = Field(max_length=8, primary_key=True)
    headword: str = Field(max_length=256)
    surname: str = Field(max_length=50)
    form: str = Field(max_length=120)
    qualifier: str = Field(max_length=80)
    alias: str = Field(max_length=80)
    sex: str = Field(max_length=2, default="X")
    imputed_birth_year: date
    occupation_id: str = Field(max_length=8, foreign_key="occupation.id")
    office_id: str = Field(max_length=8, foreign_key="office.id")
    status_id: str = Field(max_length=8, foreign_key="status.id")
    residence_id: str = Field(max_length=8, foreign_key="residence.id")
    work_location_id: str = Field(max_length=8, foreign_key="worklocation.id")
    notes: str = Field(default="")
    subscription: str = Field(max_length=1)

class Entity(SQLModel, table=True):
    id: str = Field(max_length=8, primary_key=True)
    headword: str = Field(max_length=256)
    form: str = Field(max_length=120)
    extra_data: dict[str, Any] = Field(sa_column=Column(JSON))

class Relationship(SQLModel, table=True):
    id: str = Field(max_length=8, primary_key=True)
    headword: str = Field(max_length=256)
    form: str = Field(max_length=1024)
    inverse_exists: str = Field(max_length=32)
    inverse_relationship: str = Field(max_length=32)


class SocialIdentity(SQLModel, table=True):
    id: str = Field(max_length=8, primary_key=True)
    headword: str = Field(max_length=256)
    form: str = Field(max_length=1024)
    function: str = Field(max_length=128)
    classification: str = Field(max_length=128)
    notes: str = Field(max_length=2048)


class Locations(SQLModel, table=True):
    id: str = Field(max_length=8, primary_key=True)
    headword: str = Field(max_length=256)
    form: str = Field(max_length=1024)
    located_in: str = Field(max_length=128)
    classification: str = Field(max_length=128)


class Things(SQLModel, table=True):
    id: str = Field(max_length=8, primary_key=True)
    headword: str = Field(max_length=256)
    form: str = Field(max_length=1024)
    classification: str = Field(max_length=128)


class Intangibles(SQLModel, table=True):
    id: str = Field(max_length=8, primary_key=True)
    headword: str = Field(max_length=256)
    form: str = Field(max_length=1024)
    classification: str = Field(max_length=128)


class VesselNames(SQLModel, table=True):
    id: str = Field(max_length=8, primary_key=True)
    headword: str = Field(max_length=256)
    form: str = Field(max_length=1024)
    home_port_id: str = Field(max_length=128, foreign_key="locations.id")


class Institutions(SQLModel, table=True):
    id: str = Field(max_length=8, primary_key=True)
    headword: str = Field(max_length=256)
    form: str = Field(max_length=1024)
    classification: str = Field(max_length=128)


class TemporalExpressions(SQLModel, table=True):
    id: str = Field(max_length=8, primary_key=True)
    headword: str = Field(max_length=256)
    form: str = Field(max_length=1024)
    classification: str = Field(max_length=128)


class Concepts(SQLModel, table=True):
    id: str = Field(max_length=8, primary_key=True)
    headword: str = Field(max_length=256)
    form: str = Field(max_length=1024)
    included_concepts: str = Field(max_length=128)


# class OccurrenceTable(SQLModel, table=True):
#     pass
#
#
# class ChatDumpTable(SQLModel, table=True):
#     pass
#
#
# class MetadataTable(SQLModel, table=True):
#     pass


import os

postgres_url = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@db:5432/postgres")
engine = create_engine(postgres_url, echo=True)
SQLModel.metadata.create_all(engine)
