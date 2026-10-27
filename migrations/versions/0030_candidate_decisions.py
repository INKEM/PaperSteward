"""候选决定与资产类型扩展：方法卡/证据组、类型化版本内容、候选决定。

``reading_asset`` 是父表——SQLite 的 RENAME 会重写子表外键引用，因此按
「改名→建新表→复制→重建子表→最后清旧」顺序重建三张资产表（子表跟随
重建以把外键指回新父表），无需关闭外键。降级遇 method/evidence 资产行
或非 pending 候选明确拒绝（无法在旧 schema 表示）。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: str | Sequence[str] | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ASSET_COLUMNS = (
    "id",
    "kind",
    "idempotency_key",
    "created_at",
)
_REVISION_COLUMNS = (
    "asset_id",
    "revision",
    "recorded_at",
    "title",
    "body",
    "applicability",
    "status",
)
_SOURCE_COLUMNS = (
    "id",
    "asset_id",
    "revision",
    "paper_id",
    "archive_hash",
    "rating_at",
)
_CANDIDATE_COLUMNS = (
    "id",
    "run_id",
    "manifest_id",
    "manifest_hash",
    "grant_id",
    "model_config_version",
    "paper_id",
    "kind",
    "nature",
    "statement",
    "conditions",
    "limitations",
    "quote",
    "quote_source_id",
    "source_refs_json",
    "verification",
    "stale",
    "created_at",
)

_ASSET_KINDS_EXTENDED = "'harvest', 'problem', 'method', 'evidence'"
_ASSET_KINDS_BASE = "'harvest', 'problem'"
_CANDIDATE_VERIFICATION_EXTENDED = "'pending_user', 'confirmed', 'dismissed', 'merged'"
_CANDIDATE_VERIFICATION_BASE = "'pending_user'"


def _rebuild_asset_tables(*, extended: bool) -> None:
    """三张资产表按子表跟随重建的顺序换 kind/状态 CHECK；数据逐行复制。"""
    bind = op.get_bind()
    kinds = _ASSET_KINDS_EXTENDED if extended else _ASSET_KINDS_BASE

    # 1) reading_asset：改名后子表外键自动改指 _old；先删唯一索引避免重名。
    bind.execute(sa.text("DROP INDEX IF EXISTS uq_reading_asset_idempotency"))
    bind.execute(sa.text("ALTER TABLE reading_asset RENAME TO reading_asset_old"))
    op.create_table(
        "reading_asset",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"kind IN ({kinds})", name="asset_kind_allowed"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_reading_asset_idempotency", "reading_asset", ["idempotency_key"], unique=True
    )
    asset_columns = ", ".join(_ASSET_COLUMNS)
    bind.execute(
        sa.text(
            f"INSERT INTO reading_asset ({asset_columns}) "
            f"SELECT {asset_columns} FROM reading_asset_old"
        )
    )

    # 2) reading_asset_revision：重建以把外键指回新父表。
    bind.execute(sa.text("ALTER TABLE reading_asset_revision RENAME TO reading_asset_revision_old"))
    op.create_table(
        "reading_asset_revision",
        sa.Column("asset_id", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("applicability", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.ForeignKeyConstraint(
            ["asset_id"], ["reading_asset.id"], name="fk_asset_revision_asset", ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'archived', 'open', 'resolved')",
            name="asset_status_allowed",
        ),
        sa.PrimaryKeyConstraint("asset_id", "revision"),
    )
    revision_columns = ", ".join(_REVISION_COLUMNS)
    bind.execute(
        sa.text(
            f"INSERT INTO reading_asset_revision ({revision_columns}) "
            f"SELECT {revision_columns} FROM reading_asset_revision_old"
        )
    )

    # 3) reading_asset_source：重建以把复合外键指回新 revision 表。
    bind.execute(sa.text("DROP INDEX IF EXISTS ix_reading_asset_source_revision"))
    bind.execute(sa.text("ALTER TABLE reading_asset_source RENAME TO reading_asset_source_old"))
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
            name="fk_asset_source_revision",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["paper_id"], ["paper.id"], name="fk_asset_source_paper", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_reading_asset_source_revision",
        "reading_asset_source",
        ["asset_id", "revision"],
    )
    source_columns = ", ".join(_SOURCE_COLUMNS)
    bind.execute(
        sa.text(
            f"INSERT INTO reading_asset_source ({source_columns}) "
            f"SELECT {source_columns} FROM reading_asset_source_old"
        )
    )

    # 4) 先清子表旧表，最后清父表旧表（避免外键隐式删除冲突）。
    bind.execute(sa.text("DROP TABLE reading_asset_source_old"))
    bind.execute(sa.text("DROP TABLE reading_asset_revision_old"))
    bind.execute(sa.text("DROP TABLE reading_asset_old"))


def _rebuild_candidate(*, extended: bool) -> None:
    verification = _CANDIDATE_VERIFICATION_EXTENDED if extended else _CANDIDATE_VERIFICATION_BASE
    bind = op.get_bind()
    for name in (
        "ix_reading_analysis_candidate_run",
        "ix_reading_analysis_candidate_manifest",
        "ix_reading_analysis_candidate_paper",
    ):
        bind.execute(sa.text(f"DROP INDEX IF EXISTS {name}"))
    bind.execute(sa.text("DROP TABLE IF EXISTS reading_analysis_candidate_old"))
    bind.execute(
        sa.text("ALTER TABLE reading_analysis_candidate RENAME TO reading_analysis_candidate_old")
    )
    op.create_table(
        "reading_analysis_candidate",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("manifest_id", sa.String(64), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("grant_id", sa.String(64), nullable=False),
        sa.Column("model_config_version", sa.String(32), nullable=False),
        sa.Column("paper_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("nature", sa.String(24), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("conditions", sa.Text(), nullable=True),
        sa.Column("limitations", sa.Text(), nullable=True),
        sa.Column("quote", sa.Text(), nullable=True),
        sa.Column("quote_source_id", sa.String(128), nullable=True),
        sa.Column("source_refs_json", sa.JSON(), nullable=False),
        sa.Column("verification", sa.String(16), nullable=False),
        sa.Column("stale", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('harvest', 'question', 'method')", name="candidate_kind_allowed"
        ),
        sa.CheckConstraint(
            "nature IN ('paper_statement', 'user_expression', 'model_inference')",
            name="candidate_nature_allowed",
        ),
        sa.CheckConstraint(
            f"verification IN ({verification})", name="candidate_verification_allowed"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    candidate_columns = ", ".join(_CANDIDATE_COLUMNS)
    bind.execute(
        sa.text(
            f"INSERT INTO reading_analysis_candidate ({candidate_columns}) "
            f"SELECT {candidate_columns} FROM reading_analysis_candidate_old"
        )
    )
    bind.execute(sa.text("DROP TABLE reading_analysis_candidate_old"))
    op.create_index("ix_reading_analysis_candidate_run", "reading_analysis_candidate", ["run_id"])
    op.create_index(
        "ix_reading_analysis_candidate_manifest",
        "reading_analysis_candidate",
        ["manifest_hash", "paper_id"],
    )
    op.create_index(
        "ix_reading_analysis_candidate_paper", "reading_analysis_candidate", ["paper_id"]
    )


def upgrade() -> None:
    _rebuild_asset_tables(extended=True)
    op.create_table(
        "reading_asset_typed",
        sa.Column("asset_id", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("content_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["asset_id", "revision"],
            ["reading_asset_revision.asset_id", "reading_asset_revision.revision"],
            name="fk_asset_typed_revision",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("asset_id", "revision"),
    )
    op.create_table(
        "reading_analysis_decision",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("candidate_id", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("asset_id", sa.String(64), nullable=True),
        sa.Column("asset_revision", sa.Integer(), nullable=True),
        sa.Column("paper_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("statement_hash", sa.String(64), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "decision IN ('confirmed', 'dismissed', 'merged')",
            name="candidate_decision_allowed",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_reading_analysis_decision_candidate",
        "reading_analysis_decision",
        ["candidate_id"],
        unique=True,
    )
    op.create_index(
        "ix_reading_analysis_decision_memory",
        "reading_analysis_decision",
        ["paper_id", "kind", "statement_hash"],
    )
    op.create_index("ix_reading_analysis_decision_asset", "reading_analysis_decision", ["asset_id"])
    _rebuild_candidate(extended=True)


def downgrade() -> None:
    bind = op.get_bind()
    has_new_kind = bind.execute(
        sa.text("SELECT 1 FROM reading_asset WHERE kind IN ('method', 'evidence') LIMIT 1")
    ).first()
    if has_new_kind:
        raise RuntimeError("无法回退迁移 0030：存在方法卡/证据组资产，请先导出或删除这些资产")
    has_decided = bind.execute(
        sa.text(
            "SELECT 1 FROM reading_analysis_candidate WHERE verification != 'pending_user' LIMIT 1"
        )
    ).first()
    if has_decided:
        raise RuntimeError("无法回退迁移 0030：存在已处理候选，请先恢复候选核对状态")
    op.drop_index("ix_reading_analysis_decision_asset", table_name="reading_analysis_decision")
    op.drop_index("ix_reading_analysis_decision_memory", table_name="reading_analysis_decision")
    op.drop_index("uq_reading_analysis_decision_candidate", table_name="reading_analysis_decision")
    op.drop_table("reading_analysis_decision")
    op.drop_table("reading_asset_typed")
    _rebuild_candidate(extended=False)
    _rebuild_asset_tables(extended=False)
