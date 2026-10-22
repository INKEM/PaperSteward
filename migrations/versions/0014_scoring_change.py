"""Persist global scoring-weight previews and confirmed activations."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scoring_change",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("base_version", sa.String(length=64), nullable=False),
        sa.Column("config_version", sa.String(length=64), nullable=False),
        sa.Column("preview_day", sa.Date(), nullable=False),
        sa.Column("effective_on", sa.Date(), nullable=False),
        sa.Column("proposed_json", sa.JSON(), nullable=False),
        sa.Column("preview_json", sa.JSON(), nullable=False),
        sa.Column("preview_hash", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_by", sa.String(length=128), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('previewed', 'confirmed')",
            name=op.f("ck_scoring_change_status_values"),
        ),
        sa.CheckConstraint(
            "(status = 'previewed' AND confirmed_at IS NULL AND confirmed_by IS NULL) "
            "OR (status = 'confirmed' AND confirmed_at IS NOT NULL "
            "AND confirmed_by IS NOT NULL)",
            name=op.f("ck_scoring_change_confirmed_fields"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scoring_change")),
    )
    op.create_index(
        "ix_scoring_change_status_effective",
        "scoring_change",
        ["status", "effective_on"],
        unique=False,
    )
    op.create_index(
        "ix_scoring_change_config_version",
        "scoring_change",
        ["config_version"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_scoring_change_config_version", table_name="scoring_change")
    op.drop_index("ix_scoring_change_status_effective", table_name="scoring_change")
    op.drop_table("scoring_change")
