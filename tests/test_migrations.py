"""The Alembic migration must build exactly the schema the store expects.

Needs a Postgres database: set CHASE_TEST_POSTGRES_URL. Skipped otherwise.
"""

from __future__ import annotations

import os

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from chase.domain.models import ChaseState
from chase.store.sql import METADATA, SqlStore
from tests.conftest import World

URL = os.environ.get("CHASE_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not URL, reason="CHASE_TEST_POSTGRES_URL is not set")


def test_migration_matches_models_and_runs_the_engine(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", URL)
    engine = sa.create_engine(URL)
    METADATA.drop_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE IF EXISTS alembic_version"))
    config = Config("alembic.ini")
    try:
        command.upgrade(config, "head")
        command.check(config)  # raises if the models and the migration differ

        world = World(store=SqlStore(URL))
        load = world.make_load()
        world.engine.tick()
        world.send_pod(load)
        assert world.chase(load).state is ChaseState.CLOSED

        command.downgrade(config, "base")
        assert sa.inspect(engine).get_table_names() == ["alembic_version"]
    finally:
        METADATA.drop_all(engine)
        with engine.begin() as conn:
            conn.execute(sa.text("DROP TABLE IF EXISTS alembic_version"))
        engine.dispose()
