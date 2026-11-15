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
from tests.conftest import World, check_worlds

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
        check_worlds()

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


def test_ontology_migration_carries_every_value_over_and_back(monkeypatch):
    """0003 turns flat fields into parties, consent history, operators, resolutions
    and requirement status, and ``downgrade`` restores the old columns exactly."""
    monkeypatch.setenv("DATABASE_URL", URL)
    engine = sa.create_engine(URL)
    METADATA.drop_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE IF EXISTS alembic_version"))
    config = Config("alembic.ini")
    load = {
        "broker_id": "b1",
        "carrier_id": "c1",
        "delivery_appt_ms": 1,
        "bol_number": "",
        "po_number": "",
        "expected_pages": 1,
        "delivered_at_ms": 2,
        "contact_ids": "[]",
        "pickup_number": "PU-1",
        "rate_cents": 100,
        "ready_to_invoice_at_ms": None,
    }
    chase = {
        "broker_id": "b1",
        "opened_at_ms": 2,
        "next_action_at_ms": 0,
        "attempts_sent": 1,
        "reasks_sent": 0,
        "plan": "{}",
        "fail_reasons": "[]",
        "reply_to_contact_id": "",
        "writeback_id": "",
        "kill_noted": False,
    }
    contact = {
        "carrier_id": "c1",
        "role": "DRIVER",
        "name": "",
        "email": "",
        "verified": True,
        "source": "tms",
    }
    rows = [
        ("broker", {"id": "b1", "name": "B", "chase_mailbox": "pods@b.example",
                    "rep_email": "o@b.example", "sms_number": "", "adapter": "packet_email",
                    "outbound_enabled": True, "config": "{}"}),
        ("carrier", {"id": "c1", "broker_id": "b1", "name": "C", "mc_number": "MC1",
                     "timezone": "America/Denver"}),
        # Two loads for the same customer, spelled differently: one party.
        ("load", load | {"id": "l1", "load_number": "TC-1", "customer_name": "Acme Foods",
                         "consignee_name": "Riverside", "consignee_address": "1 Dock Rd"}),
        ("load", load | {"id": "l2", "load_number": "TC-2", "customer_name": " acme foods",
                         "consignee_name": "", "consignee_address": ""}),
        ("requirement", {"id": "r1", "load_id": "l1", "doc_type": "POD"}),
        ("requirement", {"id": "r2", "load_id": "l2", "doc_type": "POD"}),
        ("chase", chase | {"id": "ch1", "load_id": "l1", "requirement_id": "r1",
                           "state": "CLOSED", "next_step": "NONE", "passed_document_id": "d1",
                           "closed_at_ms": 50, "closed_reason": "filed"}),
        ("chase", chase | {"id": "ch2", "load_id": "l2", "requirement_id": "r2",
                           "state": "CLOSED", "next_step": "NONE", "passed_document_id": "",
                           "closed_at_ms": 60, "closed_reason": "escalation_acknowledged"}),
        ("escalation", {"id": "e1", "chase_id": "ch2", "load_id": "l2", "broker_id": "b1",
                        "reason": "NO_RESPONSE", "detail": "", "created_at_ms": 40,
                        "acknowledged_by": "Jermaine", "acknowledged_at_ms": 60, "note": ""}),
        ("contact", contact | {"id": "k1", "phone": "+13125550101", "sms_consent": True,
                               "sms_stopped": False}),
        ("contact", contact | {"id": "k2", "phone": "+13125550102", "sms_consent": True,
                               "sms_stopped": True}),
        ("evidence_event", {"chain_id": "broker:b1", "seq": 0, "kind": "sms.stopped",
                            "at_ms": 30, "payload": '{"contact_id":"k2","inbound_id":"in-7"}',
                            "payload_digest": "x", "prev_hash": "0" * 64, "hash": "y"}),
    ]  # fmt: skip
    try:
        command.upgrade(config, "0002")
        with engine.begin() as conn:
            for table, row in rows:
                cols = ", ".join(f'"{k}"' for k in row)
                vals = ", ".join(f":{k}" for k in row)
                conn.execute(sa.text(f'INSERT INTO "{table}" ({cols}) VALUES ({vals})'), row)

        command.upgrade(config, "head")
        command.check(config)
        from chase.domain.models import (
            Consent,
            ConsentSource,
            ConsentStatus,
            Escalation,
            EscalationResolution,
            Load,
            Operator,
            Party,
            PartyRole,
            Requirement,
            RequirementStatus,
        )

        store = SqlStore(URL)
        parties = {p.id: p for p in store.find(Party)}
        l1, l2 = store.get(Load, "l1"), store.get(Load, "l2")
        assert l1.customer_party_id == l2.customer_party_id and len(parties) == 2
        assert parties[l1.customer_party_id].name == "Acme Foods"
        consignee = parties[l1.consignee_party_id]
        assert (consignee.role, consignee.name, consignee.address) == (
            PartyRole.CONSIGNEE, "Riverside", "1 Dock Rd",
        )  # fmt: skip
        assert l2.consignee_party_id == ""

        consents = sorted(store.find(Consent), key=lambda c: (c.contact_id, c.recorded_at_ms))
        assert [(c.contact_id, c.status, c.source, c.inbound_id) for c in consents] == [
            ("k1", ConsentStatus.GRANTED, ConsentSource.TMS_SYNC, ""),
            ("k2", ConsentStatus.GRANTED, ConsentSource.TMS_SYNC, ""),
            ("k2", ConsentStatus.WITHDRAWN, ConsentSource.STOP_REPLY, "in-7"),
        ]
        assert consents[-1].recorded_at_ms == 30

        (operator,) = store.find(Operator)
        escalation = store.get(Escalation, "e1")
        assert (operator.name, operator.broker_id) == ("Jermaine", "b1")
        assert escalation.acknowledged_by == operator.id
        assert escalation.resolution is EscalationResolution.OTHER and escalation.note

        met, settled = store.get(Requirement, "r1"), store.get(Requirement, "r2")
        assert (met.status, met.met_by_document_id, met.closed_at_ms) == (
            RequirementStatus.MET, "d1", 50,
        )  # fmt: skip
        assert (settled.status, settled.met_by_document_id, settled.closed_at_ms) == (
            RequirementStatus.RESOLVED_BY_PERSON, "", 60,
        )  # fmt: skip
        store.engine.dispose()

        command.downgrade(config, "0002")
        with engine.connect() as conn:

            def one(sql):
                return dict(conn.execute(sa.text(sql)).mappings().one())

            assert {
                k: v for k, v in one("SELECT * FROM \"load\" WHERE id = 'l1'").items()
                if k.startswith(("customer", "consignee"))
            } == {"customer_name": "Acme Foods", "consignee_name": "Riverside",
                  "consignee_address": "1 Dock Rd"}  # fmt: skip
            assert one("SELECT sms_consent, sms_stopped FROM contact WHERE id = 'k1'") == {
                "sms_consent": True, "sms_stopped": False,
            }  # fmt: skip
            assert one("SELECT sms_consent, sms_stopped FROM contact WHERE id = 'k2'") == {
                "sms_consent": True, "sms_stopped": True,
            }  # fmt: skip
            assert one("SELECT acknowledged_by, note FROM escalation WHERE id = 'e1'") == {
                "acknowledged_by": "Jermaine", "note": "",
            }  # fmt: skip
            assert set(one("SELECT * FROM requirement WHERE id = 'r1'")) == {
                "id", "load_id", "doc_type",
            }  # fmt: skip
        tables = set(sa.inspect(engine).get_table_names())
        assert not tables & {"party", "consent", "operator"}
    finally:
        METADATA.drop_all(engine)
        with engine.begin() as conn:
            for table in ("alembic_version", "outbox_message", "party", "consent", "operator"):
                conn.execute(sa.text(f'DROP TABLE IF EXISTS "{table}"'))
        engine.dispose()
