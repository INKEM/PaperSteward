"""Persist frozen static score snapshots."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "static_score_snapshot",
        sa.Column("paper_id", sa.String(64), nullable=False),
        sa.Column("score_date", sa.Date(), nullable=False),
        sa.Column("relevance", sa.Float(), nullable=False),
        sa.Column("authority", sa.Float(), nullable=False),
        sa.Column("extra_value", sa.Float(), nullable=False),
        sa.Column("static_score", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("config_version", sa.String(32), nullable=False),
        sa.Column("profile_version", sa.String(32), nullable=False),
        sa.Column("taxonomy_version", sa.String(32), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("breakdown_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("paper_id", "score_date", name=op.f("pk_static_score_snapshot")),
        sa.ForeignKeyConstraint(
            ["paper_id"],
            ["paper.id"],
            name=op.f("fk_static_score_snapshot_paper_id_paper"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "relevance >= 0.0 AND relevance <= 100.0",
            name=op.f("ck_static_score_snapshot_relevance_range"),
        ),
        sa.CheckConstraint(
            "authority >= 0.0 AND authority <= 100.0",
            name=op.f("ck_static_score_snapshot_authority_range"),
        ),
        sa.CheckConstraint(
            "extra_value >= 0.0 AND extra_value <= 100.0",
            name=op.f("ck_static_score_snapshot_extra_value_range"),
        ),
        sa.CheckConstraint(
            "static_score >= 0.0 AND static_score <= 100.0",
            name=op.f("ck_static_score_snapshot_static_range"),
        ),
        sa.CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name=op.f("ck_static_score_snapshot_confidence_range"),
        ),
    )


def downgrade() -> None:
    op.drop_table("static_score_snapshot")
