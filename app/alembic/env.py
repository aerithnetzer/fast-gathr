"""Alembic migration environment.

Reads ``DATABASE_URL`` from the environment so configuration matches the
running app exactly. Imports SQLModel metadata for ``--autogenerate``
support, although our migrations are hand-written.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make ``app/`` importable so ``import db`` resolves the same way as it does
# under ``uv run fastapi``.
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_APP_DIR = _HERE.parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

from sqlmodel import SQLModel  # noqa: E402

import db  # noqa: F401, E402  (registers all tables on SQLModel.metadata)


config = context.config

# Inject DATABASE_URL into the alembic config.
database_url = os.getenv("DATABASE_URL")
if database_url:
    config.set_main_option("sqlalchemy.url", database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _maybe_autostamp_baseline(connectable) -> None:
    """If the legacy ``user`` table exists but ``alembic_version`` does
    not, stamp ``0001_baseline`` so ``upgrade head`` skips re-creating
    User/ApiToken on a database created by the pre-Alembic
    ``SQLModel.metadata.create_all`` path.
    """
    from sqlalchemy import text

    with connectable.connect() as conn:
        has_alembic = conn.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() "
                "AND table_name = 'alembic_version'"
            )
        ).first()
        has_user = conn.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() "
                "AND table_name = 'user'"
            )
        ).first()
        if not has_alembic and has_user:
            conn.execute(
                text(
                    "CREATE TABLE alembic_version ("
                    "version_num VARCHAR(32) NOT NULL, "
                    "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO alembic_version (version_num) "
                    "VALUES ('0001_baseline')"
                )
            )
            conn.commit()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    _maybe_autostamp_baseline(connectable)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
