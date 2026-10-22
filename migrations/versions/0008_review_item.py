"""Persist human review items for low-confidence and boundary cases."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "review_item",
        sa.Column("paper_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reasons_json", sa.JSON(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.String(128), nullable=True),
        sa.Column("resolution", sa.String(16), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("paper_id", name=op.f("pk_review_item")),
        sa.ForeignKeyConstraint(
            ["paper_id"],
            ["paper.id"],
            name=op.f("fk_review_item_paper_id_paper"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'resolved')", name=op.f("ck_review_item_status_values")
        ),
        sa.CheckConstraint(
            "resolution IS NULL OR resolution IN ('approved', 'relabeled')",
            name=op.f("ck_review_item_resolution_values"),
        ),
        sa.CheckConstraint(
            "(status = 'open' AND resolved_at IS NULL AND resolved_by IS NULL "
            "AND resolution IS NULL) OR (status = 'resolved' AND resolved_at IS NOT NULL "
            "AND resolved_by IS NOT NULL AND resolution IS NOT NULL)",
            name=op.f("ck_review_item_resolved_fields"),
        ),
    )


def downgrade() -> None:
    op.drop_table("review_item")
