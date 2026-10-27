"""结构化阅读理由版本表；写入回执 CHECK 扩展新操作码。

SQLite 不能修改 CHECK 约束：回执表按「删索引→改名→建新表→复制→删旧表→
重建索引」重建，既有回执行逐行原样复制。降级仅删新表并移除新操作码回执
——结构化理由历史不可恢复，降级前需自行备份。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | Sequence[str] | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RECEIPT_COLUMNS = (
    "id",
    "idempotency_key",
    "operation",
    "target_id",
    "request_hash",
    "result_json",
    "created_at",
)
_RECEIPT_OPERATIONS_BASE = "'reading_time.record', 'asset.create', 'asset.revise', 'asset.status'"


def _receipt_check(operations: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"operation IN ({operations})", name="receipt_operation_allowed")


def _rebuild_receipt(bind: sa.Connection, *, extended: bool) -> None:
    """重建回执表以更换 CHECK；数据行经 INSERT…SELECT 逐行原样复制。"""
    operations = (
        f"{_RECEIPT_OPERATIONS_BASE}, 'structured_feedback.revise'"
        if extended
        else _RECEIPT_OPERATIONS_BASE
    )
    bind.execute(sa.text("DROP INDEX IF EXISTS uq_reading_write_receipt_key"))
    bind.execute(sa.text("DROP TABLE IF EXISTS reading_write_receipt_old"))
    bind.execute(sa.text("ALTER TABLE reading_write_receipt RENAME TO reading_write_receipt_old"))
    op.create_table(
        "reading_write_receipt",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("operation", sa.String(32), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        _receipt_check(operations),
        sa.PrimaryKeyConstraint("id"),
    )
    columns = ", ".join(_RECEIPT_COLUMNS)
    bind.execute(
        sa.text(
            f"INSERT INTO reading_write_receipt ({columns}) "
            f"SELECT {columns} FROM reading_write_receipt_old"
        )
    )
    bind.execute(sa.text("DROP TABLE reading_write_receipt_old"))
    op.create_index(
        "uq_reading_write_receipt_key",
        "reading_write_receipt",
        ["idempotency_key"],
        unique=True,
    )


def upgrade() -> None:
    op.create_table(
        "reading_structured_feedback",
        sa.Column("paper_id", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rating_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("value_sources_json", sa.JSON(), nullable=False),
        sa.Column("limited_reasons_json", sa.JSON(), nullable=False),
        sa.Column("context_note", sa.Text(), nullable=True),
        sa.Column("applicability", sa.Text(), nullable=True),
        sa.Column("takeaway", sa.Text(), nullable=True),
        sa.Column("question", sa.Text(), nullable=True),
        sa.Column("usage_result", sa.Text(), nullable=True),
        sa.Column("vocabulary_version", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.ForeignKeyConstraint(["paper_id"], ["paper.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("paper_id", "revision"),
    )
    op.create_index(
        "uq_reading_structured_feedback_key",
        "reading_structured_feedback",
        ["idempotency_key"],
        unique=True,
    )
    _rebuild_receipt(op.get_bind(), extended=True)


def downgrade() -> None:
    # 结构化理由历史随新表删除，不可恢复；新操作码回执一并移除，
    # 其余回执行经重建逐行保留。降级前先备份可完整保留。
    bind = op.get_bind()
    bind.execute(
        sa.text("DELETE FROM reading_write_receipt WHERE operation = 'structured_feedback.revise'")
    )
    _rebuild_receipt(bind, extended=False)
    op.drop_index("uq_reading_structured_feedback_key", table_name="reading_structured_feedback")
    op.drop_table("reading_structured_feedback")
