"""Preserve complete reading-time content without changing P1 archive fields."""

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "archive_snapshot",
        sa.Column("content_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )


def downgrade() -> None:
    with op.batch_alter_table("archive_snapshot") as batch:
        batch.drop_column("content_json")
