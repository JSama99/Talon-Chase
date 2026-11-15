"""contact.email_undeliverable_at_ms

When mail to a contact's address bounces for good, the time is recorded here and
the address is no longer emailed. Existing contacts have never bounced: null.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("contact") as batch:
        batch.add_column(sa.Column("email_undeliverable_at_ms", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("contact") as batch:
        batch.drop_column("email_undeliverable_at_ms")
