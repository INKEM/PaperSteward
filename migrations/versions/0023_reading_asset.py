"""手工资产（收获/问题）三张表：身份、版本、来源；只追加不覆盖。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | Sequence[str] | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reading_asset",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('harvest', 'problem')", name="asset_kind_allowed"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_reading_asset_idempotency",
        "reading_asset",
        ["idempotency_key"],
        unique=True,
    )
    op.create_table(
        "reading_asset_revision",
        sa.Column("asset_id", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("applicability", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.ForeignKeyConstraint(["asset_id"], ["reading_asset.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "status IN ('active', 'archived', 'open', 'resolved')",
            name="asset_status_allowed",
        ),
        sa.PrimaryKeyConstraint("asset_id", "revision"),
    )
    op.create_table(
        "reading_asset_source",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("asset_id", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("paper_id", sa.String(64), nullable=False),
        sa.Column("archive_hash", sa.String(64), nullable=False),
        sa.Column("rating_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["asset_id", "revision"],
            ["reading_asset_revision.asset_id", "reading_asset_revision.revision"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["paper_id"], ["paper.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_reading_asset_source_revision",
        "reading_asset_source",
        ["asset_id", "revision"],
        unique=False,
    )


def downgrade() -> None:
    # 移除这些表会丢弃手工资产的全部版本、来源与正文副本；
    # 归档、评分与阅读时间声明不受影响。
    op.drop_index("ix_reading_asset_source_revision", table_name="reading_asset_source")
    op.drop_table("reading_asset_source")
    op.drop_table("reading_asset_revision")
    op.drop_index("uq_reading_asset_idempotency", table_name="reading_asset")
    op.drop_table("reading_asset")
