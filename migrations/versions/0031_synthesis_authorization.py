"""跨文献专题综合：授权用途扩展与私人综合派生登记。

「阅读反思」与「跨文献专题综合」是独立用途，授权表 CHECK 必须同时接受
``reading_reflection`` 与 ``reading_synthesis``；知识版报告的私人归纳在
``reading_derived_copy`` 以 ``field_kind='synthesis'`` 登记来源链。两处都是
SQLite CHECK 扩展，按「改名→建新→复制→删旧」重建，既有行逐条保留。

降级遇到综合授权或综合派生登记时明确拒绝：旧 schema 无法表示这些新值，
静默删除等于丢掉来源链与授权历史（阶段计划 §11）。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: str | Sequence[str] | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GRANT_COLUMNS = (
    "id",
    "purpose",
    "scope_mode",
    "endpoint_identity",
    "model_config_version",
    "model_name",
    "material_classes_json",
    "manifest_hash",
    "consent_version",
    "created_at",
    "revoked_at",
)
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

_PURPOSES_EXTENDED = "'reading_reflection', 'reading_synthesis'"
_PURPOSES_BASE = "'reading_reflection'"
_FIELDS_EXTENDED = "'title', 'body', 'sanitized', 'statement', 'quote', 'synthesis'"
_FIELDS_BASE = "'title', 'body', 'sanitized', 'statement', 'quote'"
_SOURCES_EXTENDED = "'asset', 'report', 'paper', 'candidate'"
_SOURCES_BASE = "'asset', 'report', 'paper'"


def _rebuild_grant(*, extended: bool) -> None:
    purposes = _PURPOSES_EXTENDED if extended else _PURPOSES_BASE
    bind = op.get_bind()
    bind.execute(sa.text("DROP INDEX IF EXISTS ix_reading_analysis_grant_purpose"))
    bind.execute(sa.text("DROP TABLE IF EXISTS reading_analysis_grant_old"))
    bind.execute(sa.text("ALTER TABLE reading_analysis_grant RENAME TO reading_analysis_grant_old"))
    op.create_table(
        "reading_analysis_grant",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("scope_mode", sa.String(16), nullable=False),
        sa.Column("endpoint_identity", sa.String(512), nullable=False),
        sa.Column("model_config_version", sa.String(32), nullable=False),
        sa.Column("model_name", sa.String(128), nullable=True),
        sa.Column("material_classes_json", sa.JSON(), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=True),
        sa.Column("consent_version", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(f"purpose IN ({purposes})", name="grant_purpose_allowed"),
        sa.CheckConstraint("scope_mode IN ('once', 'workspace')", name="grant_scope_allowed"),
        sa.PrimaryKeyConstraint("id"),
    )
    columns = ", ".join(_GRANT_COLUMNS)
    bind.execute(
        sa.text(
            f"INSERT INTO reading_analysis_grant ({columns}) "
            f"SELECT {columns} FROM reading_analysis_grant_old"
        )
    )
    bind.execute(sa.text("DROP TABLE reading_analysis_grant_old"))
    op.create_index(
        "ix_reading_analysis_grant_purpose",
        "reading_analysis_grant",
        ["purpose", "revoked_at"],
    )


def _rebuild_derived_copy(*, extended: bool) -> None:
    fields = _FIELDS_EXTENDED if extended else _FIELDS_BASE
    sources = _SOURCES_EXTENDED if extended else _SOURCES_BASE
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
        sa.CheckConstraint(f"source_kind IN ({sources})", name="derived_source_allowed"),
        sa.CheckConstraint(
            "derived_kind IN ('report', 'candidate')", name="derived_target_allowed"
        ),
        sa.CheckConstraint(f"field_kind IN ({fields})", name="derived_field_allowed"),
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
    _rebuild_grant(extended=True)
    _rebuild_derived_copy(extended=True)


def downgrade() -> None:
    bind = op.get_bind()
    has_synthesis_grant = bind.execute(
        sa.text("SELECT 1 FROM reading_analysis_grant WHERE purpose = 'reading_synthesis' LIMIT 1")
    ).first()
    if has_synthesis_grant:
        raise RuntimeError("无法回退迁移 0031：存在跨文献专题综合授权，请先撤回或导出这些授权记录")
    has_synthesis_copy = bind.execute(
        sa.text(
            "SELECT 1 FROM reading_derived_copy WHERE field_kind = 'synthesis'"
            " OR source_kind = 'candidate' LIMIT 1"
        )
    ).first()
    if has_synthesis_copy:
        raise RuntimeError(
            "无法回退迁移 0031：存在知识版报告的私人综合登记，请先删除或导出这些报告"
        )
    _rebuild_derived_copy(extended=False)
    _rebuild_grant(extended=False)
