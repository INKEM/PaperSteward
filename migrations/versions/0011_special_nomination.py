"""Persist special-bucket nominations and human decisions."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "special_nomination",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("paper_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("review_layers_json", sa.JSON(), nullable=False),
        sa.Column("classic_basis_json", sa.JSON(), nullable=False),
        sa.Column("bridge_memo_json", sa.JSON(), nullable=True),
        sa.Column("reasons_json", sa.JSON(), nullable=False),
        sa.Column("provenance_model", sa.String(length=128), nullable=False),
        sa.Column("provenance_prompt_version", sa.String(length=64), nullable=False),
        sa.Column("provenance_generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provenance_input_hash", sa.String(length=128), nullable=False),
        sa.Column("nominated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.String(length=128), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "kind IN ('review', 'distant', 'classic')",
            name=op.f("ck_special_nomination_kind_values"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'rejected')",
            name=op.f("ck_special_nomination_status_values"),
        ),
        sa.CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name=op.f("ck_special_nomination_confidence_range"),
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND decided_at IS NULL AND decided_by IS NULL) "
            "OR (status IN ('accepted', 'rejected') AND decided_at IS NOT NULL "
            "AND decided_by IS NOT NULL)",
            name=op.f("ck_special_nomination_decided_fields"),
        ),
        sa.ForeignKeyConstraint(
            ["paper_id"],
            ["paper.id"],
            name=op.f("fk_special_nomination_paper_id_paper"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_special_nomination")),
        sa.UniqueConstraint("paper_id", "kind", name=op.f("uq_special_nomination_paper_kind")),
    )
    op.create_index(
        "ix_special_nomination_status_kind",
        "special_nomination",
        ["status", "kind"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_special_nomination_status_kind", table_name="special_nomination")
    op.drop_table("special_nomination")
