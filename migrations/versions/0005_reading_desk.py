"""reading desk

Moves reading out of the engine's transaction. Adds ``reading``: one row per file
to be read, written with its document and worked off by the reading desk. A document
gains ``read_at_ms`` and ``reader``; a chase gains ``reading_inbound_id``, the
message whose files it is waiting on while RECEIVED.

Existing documents were read at the moment they arrived, by the stand-in: each gets
``read_at_ms = received_at_ms`` and ``reader = 'stand-in'``, and its stored
extraction gains the new ``doubts`` list, empty. No reading rows are made for them:
there is nothing left to read. ``downgrade`` drops what was added and takes
``doubts`` back out.

Evidence is not rewritten. Chains keep their ``document.received`` events; that kind
is retired in ontology 2.0.0 and new files record ``document.arrived`` and
``document.read`` instead.

Revision ID: 0005
Revises: 0004
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def _extractions(conn) -> list[tuple[str, dict]]:
    rows = conn.execute(sa.text("SELECT id, extraction FROM document")).all()
    out = []
    for doc_id, raw in rows:
        value = json.loads(raw) if isinstance(raw, str | bytes) else raw
        if isinstance(value, dict):
            out.append((doc_id, value))
    return out


def _save(conn, doc_id: str, extraction: dict) -> None:
    conn.execute(
        sa.text("UPDATE document SET extraction = :e WHERE id = :id").bindparams(
            sa.bindparam("e", type_=sa.JSON())
        ),
        {"e": extraction, "id": doc_id},
    )


def upgrade() -> None:
    op.create_table(
        "reading",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("broker_id", sa.Text(), nullable=False),
        sa.Column("load_id", sa.Text(), nullable=False),
        sa.Column("document_id", sa.Text(), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("next_attempt_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("tries", sa.BigInteger(), nullable=False),
        sa.Column("claim_token", sa.Text(), nullable=False),
        sa.Column("lease_until_ms", sa.BigInteger(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("finished_at_ms", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("broker_id", "load_id", "status"):
        op.create_index(op.f(f"ix_reading_{column}"), "reading", [column], unique=False)

    with op.batch_alter_table("chase") as batch:
        batch.add_column(
            sa.Column("reading_inbound_id", sa.Text(), nullable=False, server_default="")
        )
    with op.batch_alter_table("chase") as batch:
        batch.alter_column("reading_inbound_id", server_default=None)

    with op.batch_alter_table("document") as batch:
        batch.add_column(sa.Column("read_at_ms", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("reader", sa.Text(), nullable=False, server_default=""))
    op.execute("UPDATE document SET read_at_ms = received_at_ms, reader = 'stand-in'")
    with op.batch_alter_table("document") as batch:
        batch.alter_column("reader", server_default=None)

    conn = op.get_bind()
    for doc_id, extraction in _extractions(conn):
        if "doubts" not in extraction:
            _save(conn, doc_id, {**extraction, "doubts": []})


def downgrade() -> None:
    conn = op.get_bind()
    for doc_id, extraction in _extractions(conn):
        if "doubts" in extraction:
            _save(conn, doc_id, {k: v for k, v in extraction.items() if k != "doubts"})
    with op.batch_alter_table("document") as batch:
        batch.drop_column("reader")
        batch.drop_column("read_at_ms")
    with op.batch_alter_table("chase") as batch:
        batch.drop_column("reading_inbound_id")
    for column in ("status", "load_id", "broker_id"):
        op.drop_index(op.f(f"ix_reading_{column}"), table_name="reading")
    op.drop_table("reading")
