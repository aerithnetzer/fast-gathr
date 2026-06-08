"""baseline: User + ApiToken (matches pre-Alembic prod schema)

This migration represents the schema as it existed before Alembic was
introduced. On environments where ``user`` and ``apitoken`` were already
created by ``SQLModel.metadata.create_all``, run::

    alembic stamp 0001_baseline

once to mark this revision as applied without re-creating the tables.
On fresh databases, ``alembic upgrade head`` will run this normally.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-06-08
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("hashed_password", sa.String(length=256), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_admin", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_user_username", "user", ["username"], unique=True)

    op.create_table(
        "apitoken",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_apitoken_user_id", "apitoken", ["user_id"])
    op.create_index(
        "ix_apitoken_token_hash", "apitoken", ["token_hash"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_apitoken_token_hash", table_name="apitoken")
    op.drop_index("ix_apitoken_user_id", table_name="apitoken")
    op.drop_table("apitoken")
    op.drop_index("ix_user_username", table_name="user")
    op.drop_table("user")
