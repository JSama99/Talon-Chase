"""ontology

Brings the tables in line with ontology 1.0.0 (docs/ONTOLOGY.md):

* ``party``: the customer and consignee of a load become records. The load's
  ``customer_name``, ``consignee_name`` and ``consignee_address`` move there and the
  load points at them. One party per broker, role and name (case-insensitive), with
  the same id the API gives it.
* ``consent``: permission to text becomes a history. ``contact.sms_consent`` becomes
  a GRANTED record from the broker's system; ``contact.sms_stopped`` becomes the
  STOP_REPLY withdrawal that the old ``sms.stopped`` evidence event names, with that
  event's time and message.
* ``operator``: the free-text names in ``escalation.acknowledged_by`` become STAFF
  operators of that broker, and the escalation points at them.
* ``escalation.resolution``: escalations acknowledged before resolutions existed
  become OTHER, with a note saying so when they had none.
* ``requirement.status``, ``met_by_document_id``, ``closed_at_ms``: worked out from the
  chase. A chase closed as filed met its requirement with its passed document; one
  closed by a person resolved it.

Evidence is never rewritten. Old chains keep their ``sms.stopped`` events, which the
ontology lists as a retired kind. ``downgrade`` puts every value back where it was.

Revision ID: 0003
Revises: 0002
"""

import hashlib
import json

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

CARRIED_NOTE = "Acknowledged before resolutions were recorded."
CONSENT_NOTE = "Carried over from contact.sms_consent by migration 0003."
STOP_NOTE = "Carried over from contact.sms_stopped by migration 0003."


def _digest(value: dict) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def party_id(broker_id: str, role: str, name: str) -> str:
    """Same as the API's ``party_for``: one party per broker, role and name."""
    return (
        "party-" + _digest({"broker": broker_id, "name": name.strip().lower(), "role": role})[:20]
    )


def operator_id(broker_id: str, name: str) -> str:
    return "op-" + _digest({"broker": broker_id, "name": name.strip().lower()})[:16]


def _rows(conn, sql: str, **params) -> list[dict]:
    return [dict(r) for r in conn.execute(sa.text(sql), params).mappings()]


def upgrade() -> None:
    op.create_table(
        "party",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("broker_id", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("address", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_party_broker_id"), "party", ["broker_id"], unique=False)
    op.create_table(
        "operator",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("broker_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_operator_broker_id"), "operator", ["broker_id"], unique=False)
    op.create_table(
        "consent",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("contact_id", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("recorded_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("recorded_by", sa.Text(), nullable=False),
        sa.Column("inbound_id", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_consent_status"), "consent", ["status"], unique=False)

    with op.batch_alter_table("load") as batch:
        batch.add_column(
            sa.Column("customer_party_id", sa.Text(), nullable=False, server_default="")
        )
        batch.add_column(
            sa.Column("consignee_party_id", sa.Text(), nullable=False, server_default="")
        )
    with op.batch_alter_table("escalation") as batch:
        batch.add_column(sa.Column("resolution", sa.Text(), nullable=True))
    with op.batch_alter_table("requirement") as batch:
        batch.add_column(sa.Column("status", sa.Text(), nullable=False, server_default="OPEN"))
        batch.add_column(
            sa.Column("met_by_document_id", sa.Text(), nullable=False, server_default="")
        )
        batch.add_column(sa.Column("closed_at_ms", sa.BigInteger(), nullable=True))
        batch.create_index(op.f("ix_requirement_status"), ["status"], unique=False)

    conn = op.get_bind()
    _parties_up(conn)
    _consent_up(conn)
    _operators_up(conn)
    _requirements_up(conn)

    with op.batch_alter_table("load") as batch:
        batch.alter_column("customer_party_id", server_default=None)
        batch.alter_column("consignee_party_id", server_default=None)
        batch.drop_column("customer_name")
        batch.drop_column("consignee_name")
        batch.drop_column("consignee_address")
    with op.batch_alter_table("contact") as batch:
        batch.drop_column("sms_consent")
        batch.drop_column("sms_stopped")
    with op.batch_alter_table("requirement") as batch:
        batch.alter_column("status", server_default=None)
        batch.alter_column("met_by_document_id", server_default=None)


def _parties_up(conn) -> None:
    parties: dict[str, dict] = {}
    for load in _rows(conn, 'SELECT * FROM "load" ORDER BY id'):
        links = {"customer_party_id": "", "consignee_party_id": ""}
        for column, role, name, address in (
            ("customer_party_id", "CUSTOMER", load["customer_name"], ""),
            ("consignee_party_id", "CONSIGNEE", load["consignee_name"], load["consignee_address"]),
        ):
            if not name.strip():
                continue
            pid = party_id(load["broker_id"], role, name)
            known = parties.setdefault(
                pid,
                {"id": pid, "broker_id": load["broker_id"], "role": role, "name": name.strip(),
                 "address": ""},
            )  # fmt: skip
            known["address"] = known["address"] or address.strip()
            links[column] = pid
        conn.execute(
            sa.text(
                'UPDATE "load" SET customer_party_id = :customer_party_id, '
                "consignee_party_id = :consignee_party_id WHERE id = :id"
            ),
            links | {"id": load["id"]},
        )
    for party in parties.values():
        conn.execute(
            sa.text(
                "INSERT INTO party (id, broker_id, role, name, address) "
                "VALUES (:id, :broker_id, :role, :name, :address)"
            ),
            party,
        )


def _consent_up(conn) -> None:
    stops: dict[str, dict] = {}
    for event in _rows(
        conn, "SELECT at_ms, payload FROM evidence_event WHERE kind = 'sms.stopped' ORDER BY at_ms"
    ):
        payload = json.loads(event["payload"])
        stops[payload["contact_id"]] = {
            "at_ms": event["at_ms"],
            "inbound_id": payload["inbound_id"],
        }
    insert = sa.text(
        "INSERT INTO consent (id, contact_id, channel, status, source, recorded_at_ms, "
        "recorded_by, inbound_id, note) VALUES (:id, :contact_id, 'SMS', :status, :source, "
        ":recorded_at_ms, '', :inbound_id, :note)"
    )
    for contact in _rows(conn, "SELECT id, sms_consent, sms_stopped FROM contact ORDER BY id"):
        if contact["sms_consent"]:
            conn.execute(
                insert,
                {"id": f"cns-{contact['id']}-granted", "contact_id": contact["id"],
                 "status": "GRANTED", "source": "TMS_SYNC", "recorded_at_ms": 0,
                 "inbound_id": "", "note": CONSENT_NOTE},
            )  # fmt: skip
        if contact["sms_stopped"]:
            stop = stops.get(contact["id"])
            conn.execute(
                insert,
                {"id": f"cns-{contact['id']}-stopped", "contact_id": contact["id"],
                 "status": "WITHDRAWN",
                 # Without the STOP message on record, the withdrawal still holds, but
                 # it comes from the broker's system; a later sync could re-grant it.
                 "source": "STOP_REPLY" if stop else "TMS_SYNC",
                 "recorded_at_ms": stop["at_ms"] if stop else 1,
                 "inbound_id": stop["inbound_id"] if stop else "", "note": STOP_NOTE},
            )  # fmt: skip


def _operators_up(conn) -> None:
    named = _rows(
        conn,
        "SELECT id, broker_id, acknowledged_by, note FROM escalation "
        "WHERE acknowledged_at_ms IS NOT NULL ORDER BY id",
    )
    made: set[str] = set()
    for escalation in named:
        name = escalation["acknowledged_by"].strip() or "Unknown"
        oid = operator_id(escalation["broker_id"], name)
        if oid not in made:
            made.add(oid)
            conn.execute(
                sa.text(
                    "INSERT INTO operator (id, broker_id, name, role, email, active) "
                    "VALUES (:id, :broker_id, :name, 'STAFF', '', :active)"
                ),
                {"id": oid, "broker_id": escalation["broker_id"], "name": name, "active": True},
            )
        conn.execute(
            sa.text(
                "UPDATE escalation SET acknowledged_by = :by, resolution = 'OTHER', "
                "note = :note WHERE id = :id"
            ),
            {"by": oid, "note": escalation["note"].strip() or CARRIED_NOTE, "id": escalation["id"]},
        )


def _requirements_up(conn) -> None:
    for chase in _rows(
        conn,
        "SELECT requirement_id, closed_reason, closed_at_ms, passed_document_id FROM chase "
        "WHERE state = 'CLOSED' ORDER BY id",
    ):
        filed = chase["closed_reason"] == "filed"
        conn.execute(
            sa.text(
                "UPDATE requirement SET status = :status, met_by_document_id = :doc, "
                "closed_at_ms = :at WHERE id = :id"
            ),
            {"status": "MET" if filed else "RESOLVED_BY_PERSON",
             "doc": chase["passed_document_id"] if filed else "",
             "at": chase["closed_at_ms"], "id": chase["requirement_id"]},
        )  # fmt: skip


def downgrade() -> None:
    with op.batch_alter_table("load") as batch:
        batch.add_column(sa.Column("customer_name", sa.Text(), nullable=False, server_default=""))
        batch.add_column(sa.Column("consignee_name", sa.Text(), nullable=False, server_default=""))
        batch.add_column(
            sa.Column("consignee_address", sa.Text(), nullable=False, server_default="")
        )
    with op.batch_alter_table("contact") as batch:
        batch.add_column(
            sa.Column("sms_consent", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.add_column(
            sa.Column("sms_stopped", sa.Boolean(), nullable=False, server_default=sa.false())
        )

    conn = op.get_bind()
    parties = {p["id"]: p for p in _rows(conn, "SELECT * FROM party")}
    for load in _rows(conn, 'SELECT id, customer_party_id, consignee_party_id FROM "load"'):
        customer = parties.get(load["customer_party_id"], {})
        consignee = parties.get(load["consignee_party_id"], {})
        conn.execute(
            sa.text(
                'UPDATE "load" SET customer_name = :c, consignee_name = :n, '
                "consignee_address = :a WHERE id = :id"
            ),
            {"c": customer.get("name", ""), "n": consignee.get("name", ""),
             "a": consignee.get("address", ""), "id": load["id"]},
        )  # fmt: skip

    history: dict[str, list[dict]] = {}
    for consent in _rows(conn, "SELECT * FROM consent ORDER BY recorded_at_ms, id"):
        history.setdefault(consent["contact_id"], []).append(consent)
    for contact_id, records in history.items():
        stopped = False
        for record in records:
            if record["source"] == "STOP_REPLY" or record["note"] == STOP_NOTE:
                stopped = True
            elif record["status"] == "GRANTED" and record["source"] == "WRITTEN":
                stopped = False
        # A STOP never changed the old consent flag; only the broker's own records did.
        others = [r for r in records if r["source"] != "STOP_REPLY" and r["note"] != STOP_NOTE]
        granted = bool(others) and others[-1]["status"] == "GRANTED"
        conn.execute(
            sa.text("UPDATE contact SET sms_consent = :g, sms_stopped = :s WHERE id = :id"),
            {"g": granted, "s": stopped, "id": contact_id},
        )

    operators = {o["id"]: o["name"] for o in _rows(conn, "SELECT id, name FROM operator")}
    for escalation in _rows(conn, "SELECT id, acknowledged_by, note FROM escalation"):
        conn.execute(
            sa.text("UPDATE escalation SET acknowledged_by = :by, note = :note WHERE id = :id"),
            {"by": operators.get(escalation["acknowledged_by"], escalation["acknowledged_by"]),
             "note": "" if escalation["note"] == CARRIED_NOTE else escalation["note"],
             "id": escalation["id"]},
        )  # fmt: skip

    with op.batch_alter_table("requirement") as batch:
        batch.drop_index(op.f("ix_requirement_status"))
        batch.drop_column("closed_at_ms")
        batch.drop_column("met_by_document_id")
        batch.drop_column("status")
    with op.batch_alter_table("escalation") as batch:
        batch.drop_column("resolution")
    with op.batch_alter_table("load") as batch:
        batch.drop_column("consignee_party_id")
        batch.drop_column("customer_party_id")
        for column in ("customer_name", "consignee_name", "consignee_address"):
            batch.alter_column(column, server_default=None)
    with op.batch_alter_table("contact") as batch:
        batch.alter_column("sms_consent", server_default=None)
        batch.alter_column("sms_stopped", server_default=None)
    op.drop_index(op.f("ix_consent_status"), table_name="consent")
    op.drop_table("consent")
    op.drop_index(op.f("ix_operator_broker_id"), table_name="operator")
    op.drop_table("operator")
    op.drop_index(op.f("ix_party_broker_id"), table_name="party")
    op.drop_table("party")
