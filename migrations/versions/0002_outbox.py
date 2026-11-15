"""outbox

Moves sending out of the engine's transaction. Adds ``outbox_message``; an
``attempt`` keeps the decision (who, which step, when it was queued) and points at
the message that delivers it.

Existing attempts were sent synchronously before this change, so each one becomes
an outbox message with status SENT carrying its subject, body, provider id and
send time. Nothing is lost, and ``downgrade`` puts it back.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "outbox_message",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("broker_id", sa.Text(), nullable=False),
        sa.Column("load_id", sa.Text(), nullable=False),
        sa.Column("chase_id", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("from_addr", sa.Text(), nullable=False),
        sa.Column("to", sa.JSON(), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("next_attempt_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("cc", sa.JSON(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("attachments", sa.JSON(), nullable=False),
        sa.Column("quiet_tz", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("tries", sa.BigInteger(), nullable=False),
        sa.Column("claim_token", sa.Text(), nullable=False),
        sa.Column("lease_until_ms", sa.BigInteger(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("provider_id", sa.Text(), nullable=False),
        sa.Column("sent_at_ms", sa.BigInteger(), nullable=True),
        sa.Column("finished_at_ms", sa.BigInteger(), nullable=True),
        sa.Column("deferred_noted", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    for column in ("broker_id", "chase_id", "load_id", "status"):
        op.create_index(
            op.f(f"ix_outbox_message_{column}"), "outbox_message", [column], unique=False
        )

    # New attempt columns, filled from the old ones before they become required.
    op.add_column("attempt", sa.Column("queued_at_ms", sa.BigInteger(), nullable=True))
    op.add_column("attempt", sa.Column("outbox_id", sa.Text(), nullable=False, server_default=""))
    op.execute("UPDATE attempt SET queued_at_ms = sent_at_ms")
    with op.batch_alter_table("attempt") as batch:  # batch: SQLite cannot ALTER COLUMN
        batch.alter_column("queued_at_ms", nullable=False)

    # Every existing attempt was already delivered: record it as a sent message.
    op.execute(
        """
        INSERT INTO outbox_message (
            id, broker_id, load_id, chase_id, purpose, channel, idempotency_key,
            from_addr, "to", created_at_ms, next_attempt_at_ms, cc, subject, body,
            attachments, quiet_tz, status, tries, claim_token, lease_until_ms,
            last_error, provider_id, sent_at_ms, finished_at_ms, deferred_noted
        )
        SELECT
            'msg-legacy-' || a.id, c.broker_id, c.load_id, a.chase_id,
            'CARRIER_REQUEST', a.channel, 'attempt:' || a.id,
            CASE WHEN a.channel = 'SMS' THEN b.sms_number ELSE b.chase_mailbox END,
            a."to", a.sent_at_ms, a.sent_at_ms, a.cc, a.subject, a.body,
            '[]', COALESCE(ca.timezone, ''), 'SENT', 1, '', 0,
            '', a.provider_id, a.sent_at_ms, a.sent_at_ms, false
        FROM attempt a
        JOIN chase c ON c.id = a.chase_id
        JOIN broker b ON b.id = c.broker_id
        LEFT JOIN "load" l ON l.id = c.load_id
        LEFT JOIN carrier ca ON ca.id = l.carrier_id
        """
    )
    op.execute(
        """
        UPDATE attempt SET outbox_id = 'msg-legacy-' || id
        WHERE EXISTS (SELECT 1 FROM outbox_message m WHERE m.id = 'msg-legacy-' || attempt.id)
        """
    )
    with op.batch_alter_table("attempt") as batch:
        batch.alter_column("outbox_id", server_default=None)
        for column in ("body", "provider_id", "sent_at_ms", "subject"):
            batch.drop_column(column)


def downgrade() -> None:
    op.add_column("attempt", sa.Column("subject", sa.Text(), nullable=False, server_default=""))
    op.add_column("attempt", sa.Column("body", sa.Text(), nullable=False, server_default=""))
    op.add_column("attempt", sa.Column("provider_id", sa.Text(), nullable=False, server_default=""))
    op.add_column("attempt", sa.Column("sent_at_ms", sa.BigInteger(), nullable=True))
    op.execute(
        """
        UPDATE attempt SET
            subject = m.subject,
            body = m.body,
            provider_id = m.provider_id,
            sent_at_ms = COALESCE(m.sent_at_ms, attempt.queued_at_ms)
        FROM outbox_message m
        WHERE m.id = attempt.outbox_id
        """
    )
    op.execute("UPDATE attempt SET sent_at_ms = queued_at_ms WHERE sent_at_ms IS NULL")
    with op.batch_alter_table("attempt") as batch:
        batch.alter_column("sent_at_ms", nullable=False)
        for column in ("subject", "body", "provider_id"):
            batch.alter_column(column, server_default=None)
        batch.drop_column("outbox_id")
        batch.drop_column("queued_at_ms")
    for column in ("status", "load_id", "chase_id", "broker_id"):
        op.drop_index(op.f(f"ix_outbox_message_{column}"), table_name="outbox_message")
    op.drop_table("outbox_message")
