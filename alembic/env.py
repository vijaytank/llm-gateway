"""
alembic/env.py — Alembic environment for LLM Gateway

Reads DATABASE_URL from the environment (docker-compose / wizard set this)
and points migrations at the schemas.db metadata.
"""

import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
import sys
from pathlib import Path

# Make project root importable when alembic runs from anywhere
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alembic import context
from schemas.db import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Export it (e.g. "
            "postgresql://llm_gateway:pass@localhost:5432/llm_gateway) before running migrations."
        )
    # SQLAlchemy 2.0 prefers postgresql+psycopg2:// explicit driver
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL to stdout)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (connect to the DB)."""
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _database_url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
