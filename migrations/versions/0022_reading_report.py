"""阅读回顾报告快照表；冻结正文 + 校验哈希 + 输入指纹。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | Sequence[str] | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reading_report",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("report_kind", sa.String(16), nullable=False),
        sa.Column("period_label", sa.String(32), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("review_mode", sa.String(16), nullable=False),
        sa.Column("knowledge_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("contract", sa.String(32), nullable=False),
        sa.Column("input_fingerprint", sa.String(64), nullable=False),
        sa.Column("content_json", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "report_kind IN ('week', 'month', 'all', 'custom')", name="report_kind_allowed"
        ),
        sa.CheckConstraint(
            "review_mode IN ('as_recorded', 'revised_now')", name="review_mode_allowed"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_reading_report_run", "reading_report", ["run_id"], unique=True)
    op.create_index(
        "uq_reading_report_fingerprint",
        "reading_report",
        ["input_fingerprint"],
        unique=True,
    )
    op.create_index("ix_reading_report_generated", "reading_report", ["generated_at"], unique=False)


def downgrade() -> None:
    # 移除本表会丢弃已保存的报告快照；归档、评分与阅读时间声明不受影响。
    op.drop_index("ix_reading_report_generated", table_name="reading_report")
    op.drop_index("uq_reading_report_fingerprint", table_name="reading_report")
    op.drop_index("uq_reading_report_run", table_name="reading_report")
    op.drop_table("reading_report")
