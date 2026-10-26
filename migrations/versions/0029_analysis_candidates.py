"""分析候选表与派生副本登记扩展：候选写入派生链，报告登记改为显式清理。

``reading_derived_copy`` 按「删索引→改名→建新表→复制→删旧表→重建索引」
重建：``derived_kind``/``source_kind``/``field_kind`` CHECK 扩展候选类型，
``derived_id`` 去掉指向报告的外键（同列无法指向两张父表）——删报告的级联
语义改由 ``delete_report`` 服务在同一事务显式清理。降级遇到候选登记行时
明确拒绝（无法表示的新类型），须先处理候选再降级；候选表随降级删除。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: str | Sequence[str] | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DERIVED_COLUMNS = (
    "id",
    "source_kind",
    "source_id",
    "source_revision",
    "derived_kind",
    "derived_id",
    "field_kind",
    "created_at",
)


def _derived_checks(*, with_candidate: bool) -> list[sa.CheckConstraint]:
    source_kinds = "'asset', 'report'" + (", 'paper'" if with_candidate else "")
    derived_kinds = "'report'" + (", 'candidate'" if with_candidate else "")
    field_kinds = "'title', 'body', 'sanitized'" + (
        ", 'statement', 'quote'" if with_candidate else ""
    )
    return [
        sa.CheckConstraint(f"source_kind IN ({source_kinds})", name="derived_source_allowed"),
        sa.CheckConstraint(f"derived_kind IN ({derived_kinds})", name="derived_target_allowed"),
        sa.CheckConstraint(f"field_kind IN ({field_kinds})", name="derived_field_allowed"),
    ]


def _rebuild_derived_copy(*, with_candidate: bool) -> None:
    bind = op.get_bind()
    for name in (
        "ix_reading_derived_copy_source",
        "ix_reading_derived_copy_target",
        "uq_reading_derived_copy_entry",
    ):
        bind.execute(sa.text(f"DROP INDEX IF EXISTS {name}"))
    bind.execute(sa.text("DROP TABLE IF EXISTS reading_derived_copy_old"))
    bind.execute(sa.text("ALTER TABLE reading_derived_copy RENAME TO reading_derived_copy_old"))
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
        *_derived_checks(with_candidate=with_candidate),
        sa.PrimaryKeyConstraint("id"),
    )
    columns = ", ".join(_DERIVED_COLUMNS)
    bind.execute(
        sa.text(
            f"INSERT INTO reading_derived_copy ({columns}) "
            f"SELECT {columns} FROM reading_derived_copy_old"
        )
    )
    bind.execute(sa.text("DROP TABLE reading_derived_copy_old"))
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
        [
            "source_kind",
            "source_id",
            "source_revision",
            "derived_kind",
            "derived_id",
            "field_kind",
        ],
        unique=True,
    )


def upgrade() -> None:
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
            "verification IN ('pending_user')", name="candidate_verification_allowed"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_reading_analysis_candidate_run", "reading_analysis_candidate", ["run_id"])
    op.create_index(
        "ix_reading_analysis_candidate_manifest",
        "reading_analysis_candidate",
        ["manifest_hash", "paper_id"],
    )
    op.create_index(
        "ix_reading_analysis_candidate_paper", "reading_analysis_candidate", ["paper_id"]
    )
    _rebuild_derived_copy(with_candidate=True)


def downgrade() -> None:
    # 候选与其派生登记无法在旧 schema 表示：存在即拒绝降级，须先清理候选。
    bind = op.get_bind()
    has_candidates = bind.execute(
        sa.text("SELECT 1 FROM reading_derived_copy WHERE derived_kind = 'candidate' LIMIT 1")
    ).first()
    if has_candidates:
        raise RuntimeError("无法回退迁移 0029：存在分析候选的派生登记，请先确认或删除分析候选")
    op.drop_index("ix_reading_analysis_candidate_paper", table_name="reading_analysis_candidate")
    op.drop_index("ix_reading_analysis_candidate_manifest", table_name="reading_analysis_candidate")
    op.drop_index("ix_reading_analysis_candidate_run", table_name="reading_analysis_candidate")
    op.drop_table("reading_analysis_candidate")
    _rebuild_derived_copy(with_candidate=False)
