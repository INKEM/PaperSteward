"""阅读时间声明版本表；阅读回顾第一阶段第一笔存储变更。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | Sequence[str] | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reading_time_revision",
        sa.Column("paper_id", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("precision", sa.String(8), nullable=False),
        sa.Column("declared_date", sa.Date(), nullable=True),
        sa.Column("declared_month", sa.String(7), nullable=True),
        sa.Column("basis", sa.String(16), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.ForeignKeyConstraint(["paper_id"], ["paper.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("paper_id", "revision"),
        sa.CheckConstraint("precision IN ('day', 'month', 'unknown')", name="precision_allowed"),
        sa.CheckConstraint(
            "(precision = 'day' AND declared_date IS NOT NULL AND declared_month IS NULL)"
            " OR (precision = 'month' AND declared_date IS NULL AND declared_month IS NOT NULL)"
            " OR (precision = 'unknown' AND declared_date IS NULL AND declared_month IS NULL)",
            name="precision_value_coherent",
        ),
        sa.CheckConstraint(
            "basis IN ('system_recorded', 'declared', 'unknown')", name="basis_allowed"
        ),
    )
    op.create_index(
        "uq_reading_time_idempotency",
        "reading_time_revision",
        ["idempotency_key"],
        unique=True,
    )


def downgrade() -> None:
    # 移除本表会丢弃已积累的日期声明；归档、评分与事件不受影响。
    op.drop_index("uq_reading_time_idempotency", table_name="reading_time_revision")
    op.drop_table("reading_time_revision")
