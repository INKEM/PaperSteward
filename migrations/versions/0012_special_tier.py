"""Persist append-only special-bucket tier assessments and overrides."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "special_tier_snapshot",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("paper_id", sa.String(length=64), nullable=False),
        sa.Column("nomination_id", sa.String(length=64), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("tier", sa.String(length=16), nullable=False),
        sa.Column("dimensions_json", sa.JSON(), nullable=False),
        sa.Column("weights_json", sa.JSON(), nullable=False),
        sa.Column("reasons_json", sa.JSON(), nullable=False),
        sa.Column("explanation_json", sa.JSON(), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("breakdown_json", sa.JSON(), nullable=False),
        sa.Column("rubric_version", sa.String(length=64), nullable=False),
        sa.Column("assigned_by", sa.String(length=16), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('review', 'distant', 'classic')",
            name=op.f("ck_special_tier_snapshot_kind_values"),
        ),
        sa.CheckConstraint(
            "tier IN ('high', 'medium', 'low')",
            name=op.f("ck_special_tier_snapshot_tier_values"),
        ),
        sa.CheckConstraint(
            "assigned_by IN ('agent', 'user', 'system')",
            name=op.f("ck_special_tier_snapshot_assigned_by_values"),
        ),
        sa.CheckConstraint(
            "value >= 0.0 AND value <= 100.0",
            name=op.f("ck_special_tier_snapshot_value_range"),
        ),
        sa.ForeignKeyConstraint(
            ["nomination_id"],
            ["special_nomination.id"],
            name=op.f("fk_special_tier_snapshot_nomination_id_special_nomination"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["paper_id"],
            ["paper.id"],
            name=op.f("fk_special_tier_snapshot_paper_id_paper"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_special_tier_snapshot")),
    )
    op.create_index(
        "ix_special_tier_paper_created",
        "special_tier_snapshot",
        ["paper_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_special_tier_kind_tier",
        "special_tier_snapshot",
        ["kind", "tier"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_special_tier_kind_tier", table_name="special_tier_snapshot")
    op.drop_index("ix_special_tier_paper_created", table_name="special_tier_snapshot")
    op.drop_table("special_tier_snapshot")
