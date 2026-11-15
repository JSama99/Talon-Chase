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
        world.tick()
        world.send_pod(load)
        assert world.chase(load).state is ChaseState.CLOSED

        command.downgrade(config, "base")
        assert sa.inspect(engine).get_table_names() == ["alembic_version"]
    finally:
        METADATA.drop_all(engine)
        with engine.begin() as conn:
            conn.execute(sa.text("DROP TABLE IF EXISTS alembic_version"))
        engine.dispose()


def test_outbox_migration_keeps_sent_history_and_reverses(monkeypatch):
    """A database with attempts sent before the outbox existed upgrades without loss."""
    monkeypatch.setenv("DATABASE_URL", URL)
    engine = sa.create_engine(URL)
    METADATA.drop_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE IF EXISTS alembic_version"))
    config = Config("alembic.ini")
    try:
        command.upgrade(config, "0001")
        rows = {
            "broker": {
                "id": "b1", "name": "B", "chase_mailbox": "pods@b.example",
                "rep_email": "o@b.example", "sms_number": "+14045550100",
                "adapter": "packet_email", "outbound_enabled": True, "config": "{}",
            },
            "carrier": {
                "id": "c1", "broker_id": "b1", "name": "C", "mc_number": "MC1",
                "timezone": "America/Denver",
            },
            "load": {
                "id": "l1", "broker_id": "b1", "carrier_id": "c1", "load_number": "TC-1",
                "delivery_appt_ms": 1, "bol_number": "", "po_number": "",
                "expected_pages": 1, "delivered_at_ms": 2, "contact_ids": "[]",
                "customer_name": "", "consignee_name": "", "consignee_address": "",
                "pickup_number": "", "rate_cents": 0, "ready_to_invoice_at_ms": None,
            },
            "chase": {
                "id": "ch1", "load_id": "l1", "broker_id": "b1", "requirement_id": "r1",
                "opened_at_ms": 2, "state": "ASKED", "next_step": "SECOND",
                "next_action_at_ms": 9, "attempts_sent": 1, "reasks_sent": 0,
                "plan": "{}", "fail_reasons": "[]", "reply_to_contact_id": "",
                "passed_document_id": "", "writeback_id": "", "closed_at_ms": None,
                "closed_reason": "", "kill_noted": False,
            },
            "attempt": {
                "id": "att-1", "chase_id": "ch1", "step": "FIRST_EMAIL", "channel": "EMAIL",
                "to": '["d@c.example"]', "cc": "[]", "subject": "Please send the POD",
                "body": "Hello", "sent_at_ms": 1_794_340_800_000, "provider_id": "ses-123",
            },
        }  # fmt: skip
        with engine.begin() as conn:
            for table, row in rows.items():
                cols = ", ".join(f'"{k}"' for k in row)
                vals = ", ".join(f":{k}" for k in row)
                conn.execute(sa.text(f'INSERT INTO "{table}" ({cols}) VALUES ({vals})'), row)

        command.upgrade(config, "head")
        store = SqlStore(URL)
        from chase.domain.models import Attempt, OutboxMessage, OutboxStatus

        (attempt,) = store.find(Attempt)
        (message,) = store.find(OutboxMessage)
        assert attempt.queued_at_ms == 1_794_340_800_000 and attempt.outbox_id == message.id
        assert message.status is OutboxStatus.SENT and message.provider_id == "ses-123"
        assert (message.subject, message.body, message.to) == (
            "Please send the POD",
            "Hello",
            ["d@c.example"],
        )
        assert message.from_addr == "pods@b.example" and message.quiet_tz == "America/Denver"
        assert message.idempotency_key == "attempt:att-1" and message.load_id == "l1"
        store.engine.dispose()

        command.downgrade(config, "0001")
        with engine.connect() as conn:
            row = conn.execute(sa.text("SELECT * FROM attempt")).mappings().one()
        assert (row["subject"], row["body"], row["provider_id"], row["sent_at_ms"]) == (
            "Please send the POD",
            "Hello",
            "ses-123",
            1_794_340_800_000,
        )
        assert "outbox_message" not in sa.inspect(engine).get_table_names()
    finally:
        METADATA.drop_all(engine)
        with engine.begin() as conn:
            conn.execute(sa.text("DROP TABLE IF EXISTS alembic_version"))
            conn.execute(sa.text("DROP TABLE IF EXISTS outbox_message"))
        engine.dispose()
