"""Persist per-paper enrichment signals with confidence and provenance."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "enrichment",
        sa.Column("paper_id", sa.String(64), nullable=False),
        sa.Column("teams_json", sa.JSON(), nullable=False),
        sa.Column("venue_level", sa.Text(), nullable=True),
        sa.Column("citation_count", sa.Integer(), nullable=True),
        sa.Column("citation_velocity", sa.Float(), nullable=True),
        sa.Column("code_url", sa.Text(), nullable=True),
        sa.Column("data_url", sa.Text(), nullable=True),
        sa.Column("oa_status", sa.String(16), nullable=False),
        sa.Column("experiment_quality_json", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provenance_model", sa.String(128), nullable=True),
        sa.Column("provenance_prompt_version", sa.String(64), nullable=True),
        sa.Column("provenance_generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provenance_input_hash", sa.String(128), nullable=True),
        sa.PrimaryKeyConstraint("paper_id", name=op.f("pk_enrichment")),
        sa.ForeignKeyConstraint(
            ["paper_id"],
            ["paper.id"],
            name=op.f("fk_enrichment_paper_id_paper"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name=op.f("ck_enrichment_confidence_range"),
        ),
        sa.CheckConstraint(
            "citation_count IS NULL OR citation_count >= 0",
            name=op.f("ck_enrichment_citation_count_non_negative"),
        ),
        sa.CheckConstraint(
            "citation_velocity IS NULL OR citation_velocity >= 0.0",
            name=op.f("ck_enrichment_citation_velocity_non_negative"),
        ),
        sa.CheckConstraint(
            "oa_status IN ('open', 'closed', 'unknown')",
            name=op.f("ck_enrichment_oa_status_values"),
        ),
        sa.CheckConstraint(
            "(provenance_model IS NULL AND provenance_prompt_version IS NULL "
            "AND provenance_generated_at IS NULL AND provenance_input_hash IS NULL) "
            "OR (provenance_model IS NOT NULL AND provenance_prompt_version IS NOT NULL "
            "AND provenance_generated_at IS NOT NULL AND provenance_input_hash IS NOT NULL)",
            name=op.f("ck_enrichment_provenance_all_or_none"),
        ),
    )


def downgrade() -> None:
    op.drop_table("enrichment")
