"""Alembic environment. The database address comes from DATABASE_URL."""

import os

from alembic import context
from sqlalchemy import create_engine

from chase.store.sql import METADATA, engine_options, normalize_database_url

target_metadata = METADATA


def run() -> None:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("set DATABASE_URL before running alembic")
    url = normalize_database_url(url)
    if context.is_offline_mode():
        context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = create_engine(url, **engine_options(url))
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


run()
