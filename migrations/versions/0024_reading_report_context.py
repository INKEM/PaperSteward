"""报告独立范围上下文；旧报告保守恢复，冻结正文与哈希保持不变。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from papersteward.reading.insights.legacy_context import legacy_report_context

revision: str = "0024"
down_revision: str | Sequence[str] | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reading_report_context",
        sa.Column("report_id", sa.String(64), nullable=False),
        sa.Column("context_json", sa.JSON(), nullable=False),
        sa.Column("series_key", sa.String(64), nullable=True),
        sa.ForeignKeyConstraint(["report_id"], ["reading_report.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("report_id"),
    )
    op.create_index("ix_reading_report_context_series", "reading_report_context", ["series_key"])
    connection = op.get_bind()
    reports = sa.table(
        "reading_report",
        sa.column("id"),
        sa.column("run_id"),
        sa.column("timezone"),
        sa.column("content_json", sa.JSON()),
    )
    runs = sa.table("run", sa.column("id"), sa.column("stats_json", sa.JSON()))
    contexts = sa.table(
        "reading_report_context",
        sa.column("report_id"),
        sa.column("context_json", sa.JSON()),
        sa.column("series_key"),
    )
    # 主键批次遍历；不把完整私人报告整库载入内存。
    last_id = ""
    while True:
        rows = connection.execute(
            sa.select(reports.c.id, reports.c.timezone, reports.c.content_json, runs.c.stats_json)
            .select_from(reports.outerjoin(runs, reports.c.run_id == runs.c.id))
            .where(reports.c.id > last_id)
            .order_by(reports.c.id)
            .limit(200)
        ).all()
        if not rows:
            break
        payloads = []
        for row in rows:
            stats = row.stats_json if isinstance(row.stats_json, dict) else {}
            try:
                context = legacy_report_context(
                    row.content_json, stats.get("request"), timezone=row.timezone
                )
            except (ValueError, TypeError, KeyError):
                # 原始报告已坏时不阻断迁移；读取仍由旧哈希/契约校验拒绝，不能制造可信上下文。
                continue
            payloads.append(
                {
                    "report_id": row.id,
                    "context_json": context.model_dump(mode="json"),
                    "series_key": None,
                }
            )
        if payloads:
            connection.execute(contexts.insert(), payloads)
        last_id = rows[-1].id


def downgrade() -> None:
    # 仅移除范围与输入版本清单；旧/新报告冻结正文、日期和资产均保留。
    op.drop_index("ix_reading_report_context_series", table_name="reading_report_context")
    op.drop_table("reading_report_context")
