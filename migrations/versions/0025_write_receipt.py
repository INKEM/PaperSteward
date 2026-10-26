"""写入回执表：幂等键绑定操作、目标与规范化请求哈希；既有键从首次版本回填。

回填只恢复可核查的请求哈希（阅读时间来自声明字段，资产创建来自首个版本的
内容与来源），``result_json`` 留空——旧键重放退回当前视图，不伪造历史结果。
"""

from collections.abc import Sequence
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

from papersteward.reading.insights.idempotency import (
    ASSET_CREATE,
    READING_TIME_RECORD,
    asset_content_payload,
    reading_time_payload,
    request_hash,
)
from papersteward.storage.mappers import as_utc

revision: str = "0025"
down_revision: str | Sequence[str] | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reading_write_receipt",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("operation", sa.String(32), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "operation IN ('reading_time.record', 'asset.create', 'asset.revise', 'asset.status')",
            name="receipt_operation_allowed",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_reading_write_receipt_key",
        "reading_write_receipt",
        ["idempotency_key"],
        unique=True,
    )
    _backfill(op.get_bind())


def _backfill(connection: sa.Connection) -> None:
    """从既有幂等键恢复请求哈希；无法恢复的行跳过，读取侧按冲突处理。"""
    receipts = sa.table(
        "reading_write_receipt",
        sa.column("id"),
        sa.column("idempotency_key"),
        sa.column("operation"),
        sa.column("target_id"),
        sa.column("request_hash"),
        sa.column("result_json"),
        sa.column("created_at"),
    )
    times = sa.table(
        "reading_time_revision",
        sa.column("paper_id"),
        sa.column("revision"),
        sa.column("recorded_at"),
        sa.column("precision"),
        sa.column("declared_date", sa.Date()),
        sa.column("declared_month"),
        sa.column("idempotency_key"),
    )

    last_paper, last_revision = "", -1
    while True:
        rows = connection.execute(
            times.select()
            .where(
                times.c.idempotency_key.is_not(None),
                times.c.idempotency_key != "",
                sa.tuple_(times.c.paper_id, times.c.revision) > (last_paper, last_revision),
            )
            .order_by(times.c.paper_id, times.c.revision)
            .limit(200)
        ).all()
        if not rows:
            break
        connection.execute(
            receipts.insert(),
            [
                {
                    "id": uuid4().hex,
                    "idempotency_key": row.idempotency_key,
                    "operation": READING_TIME_RECORD,
                    "target_id": row.paper_id,
                    "request_hash": request_hash(
                        READING_TIME_RECORD,
                        row.paper_id,
                        reading_time_payload(
                            paper_id=row.paper_id,
                            precision=row.precision,
                            day=row.declared_date.isoformat() if row.declared_date else None,
                            month=row.declared_month,
                        ),
                    ),
                    "result_json": None,
                    "created_at": row.recorded_at,
                }
                for row in rows
            ],
        )
        last_paper, last_revision = rows[-1].paper_id, rows[-1].revision

    assets = sa.table(
        "reading_asset",
        sa.column("id"),
        sa.column("kind"),
        sa.column("idempotency_key"),
        sa.column("created_at"),
    )
    revisions = sa.table(
        "reading_asset_revision",
        sa.column("asset_id"),
        sa.column("revision"),
        sa.column("title"),
        sa.column("body"),
        sa.column("applicability"),
    )
    sources = sa.table(
        "reading_asset_source",
        sa.column("asset_id"),
        sa.column("revision"),
        sa.column("paper_id"),
        sa.column("rating_at", sa.DateTime(timezone=True)),
    )

    last_asset = ""
    while True:
        rows = connection.execute(
            sa.select(
                assets.c.id.label("asset_id"),
                assets.c.kind,
                assets.c.idempotency_key,
                assets.c.created_at,
                revisions.c.title,
                revisions.c.body,
                revisions.c.applicability,
            )
            .select_from(
                assets.join(
                    revisions,
                    (revisions.c.asset_id == assets.c.id) & (revisions.c.revision == 1),
                )
            )
            .where(assets.c.id > last_asset)
            .order_by(assets.c.id)
            .limit(200)
        ).all()
        if not rows:
            break
        payloads = []
        for row in rows:
            asset_sources = connection.execute(
                sa.select(sources.c.paper_id, sources.c.rating_at).where(
                    sources.c.asset_id == row.asset_id,
                    sources.c.revision == 1,
                )
            ).all()
            payloads.append(
                {
                    "id": uuid4().hex,
                    "idempotency_key": row.idempotency_key,
                    "operation": ASSET_CREATE,
                    "target_id": "",
                    "request_hash": request_hash(
                        ASSET_CREATE,
                        "",
                        asset_content_payload(
                            row.kind,
                            title=row.title,
                            body=row.body,
                            applicability=row.applicability,
                            sources=(
                                (
                                    source.paper_id,
                                    as_utc(source.rating_at).isoformat()
                                    if source.rating_at
                                    else None,
                                )
                                for source in asset_sources
                            ),
                        ),
                    ),
                    "result_json": None,
                    "created_at": row.created_at,
                }
            )
        if payloads:
            connection.execute(receipts.insert(), payloads)
        last_asset = rows[-1].asset_id


def downgrade() -> None:
    # 仅移除写入回执；冻结正文、资产版本与阅读日期不受影响。
    # 日期/创建的幂等键仍在旧列上，可再次升级时回填；修订/状态回执不可恢复。
    op.drop_index("uq_reading_write_receipt_key", table_name="reading_write_receipt")
    op.drop_table("reading_write_receipt")
