"""Persist immutable source metadata versions."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "paper_source",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("paper_id", sa.String(64), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_json", sa.JSON(), nullable=False),
        sa.Column("candidate_json", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_paper_source")),
        sa.ForeignKeyConstraint(
            ["paper_id"], ["paper.id"], name=op.f("fk_paper_source_paper_id_paper")
        ),
    )
    op.create_index("ix_paper_source_identity", "paper_source", ["source", "source_id"])


def downgrade() -> None:
    op.drop_index("ix_paper_source_identity", table_name="paper_source")
    op.drop_table("paper_source")
