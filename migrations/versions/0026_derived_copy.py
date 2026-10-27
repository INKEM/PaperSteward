"""私人派生副本登记表：来源（资产版本/报告）到含其内容的持久对象。

回填从既有报告的冻结正文扫描资产引用——只登记 ID 与字段类别，
不复制私人原文；正文损坏的报告跳过，读取侧由哈希校验拒绝。
"""

from collections.abc import Sequence
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | Sequence[str] | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reading_derived_copy",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("source_kind", sa.String(16), nullable=False),
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column("source_revision", sa.Integer(), nullable=True),
        sa.Column("derived_kind", sa.String(16), nullable=False),
        sa.Column("derived_id", sa.String(64), nullable=False),
        sa.Column("field_kind", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("source_kind IN ('asset', 'report')", name="derived_source_allowed"),
        sa.CheckConstraint("derived_kind IN ('report')", name="derived_target_allowed"),
        sa.CheckConstraint(
            "field_kind IN ('title', 'body', 'sanitized')", name="derived_field_allowed"
        ),
        sa.ForeignKeyConstraint(["derived_id"], ["reading_report.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_reading_derived_copy_source",
        "reading_derived_copy",
        ["source_kind", "source_id", "source_revision"],
    )
    op.create_index(
        "ix_reading_derived_copy_target",
        "reading_derived_copy",
        ["derived_kind", "derived_id"],
    )
    op.create_index(
        "uq_reading_derived_copy_entry",
        "reading_derived_copy",
        ["source_kind", "source_id", "source_revision", "derived_kind", "derived_id", "field_kind"],
        unique=True,
    )
    _backfill(op.get_bind())


def _backfill(connection: sa.Connection) -> None:
    """从既有报告冻结正文登记私人副本；正文缺失或损坏的行跳过。"""
    reports = sa.table(
        "reading_report",
        sa.column("id"),
        sa.column("generated_at"),
        sa.column("content_json", sa.JSON()),
    )
    copies = sa.table(
        "reading_derived_copy",
        sa.column("id"),
        sa.column("source_kind"),
        sa.column("source_id"),
        sa.column("source_revision"),
        sa.column("derived_kind"),
        sa.column("derived_id"),
        sa.column("field_kind"),
        sa.column("created_at"),
    )
    last_id = ""
    while True:
        rows = connection.execute(
            sa.select(reports.c.id, reports.c.generated_at, reports.c.content_json)
            .where(reports.c.id > last_id)
            .order_by(reports.c.id)
            .limit(200)
        ).all()
        if not rows:
            break
        payloads: list[dict] = []
        for row in rows:
            content = row.content_json if isinstance(row.content_json, dict) else {}
            refs = content.get("assets")
            if not isinstance(refs, list):
                continue
            for ref in refs:
                if not isinstance(ref, dict) or not isinstance(ref.get("asset_id"), str):
                    continue
                revision = ref.get("revision")
                revision_value = revision if isinstance(revision, int) else None
                payloads.append(
                    {
                        "id": uuid4().hex,
                        "source_kind": "asset",
                        "source_id": ref["asset_id"],
                        "source_revision": revision_value,
                        "derived_kind": "report",
                        "derived_id": row.id,
                        "field_kind": "title",
                        "created_at": row.generated_at,
                    }
                )
                if ref.get("body") is not None:
                    payloads.append(
                        {
                            "id": uuid4().hex,
                            "source_kind": "asset",
                            "source_id": ref["asset_id"],
                            "source_revision": revision_value,
                            "derived_kind": "report",
                            "derived_id": row.id,
                            "field_kind": "body",
                            "created_at": row.generated_at,
                        }
                    )
        if payloads:
            connection.execute(copies.insert(), payloads)
        last_id = rows[-1].id


def downgrade() -> None:
    # 仅移除派生副本登记；冻结报告、资产与回执不受影响。
    op.drop_index("uq_reading_derived_copy_entry", table_name="reading_derived_copy")
    op.drop_index("ix_reading_derived_copy_target", table_name="reading_derived_copy")
    op.drop_index("ix_reading_derived_copy_source", table_name="reading_derived_copy")
    op.drop_table("reading_derived_copy")
