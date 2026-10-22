"""Immutable daily digests and durable delivery reservations."""

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "digest",
        sa.Column("digest_date", sa.Date(), primary_key=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "delivery",
        sa.Column("digest_date", sa.Date(), sa.ForeignKey("digest.digest_date"), primary_key=True),
        sa.Column("channel", sa.String(32), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("destination_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "channel IN ('local_markdown', 'smtp')", name=op.f("ck_delivery_channel_values")
        ),
        sa.CheckConstraint(
            "status IN ('reserved', 'sent', 'failed', 'uncertain')",
            name=op.f("ck_delivery_status_values"),
        ),
    )


def downgrade() -> None:
    op.drop_table("delivery")
    op.drop_table("digest")
