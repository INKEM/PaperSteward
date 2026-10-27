"""独立阅读研究记忆：身份、追加版本与用户决定；扩展用途与派生登记。

保留旧授权与来源链。降级遇研究记忆或其授权/来源登记明确拒绝，避免静默丢失。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: str | Sequence[str] | None = "0031"
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

_PURPOSES_EXTENDED = "'reading_reflection', 'reading_synthesis', 'reading_profile_inference'"
_PURPOSES_BASE = "'reading_reflection', 'reading_synthesis'"
_FIELDS_EXTENDED = "'title', 'body', 'sanitized', 'statement', 'quote', 'synthesis', 'memory'"
_FIELDS_BASE = "'title', 'body', 'sanitized', 'statement', 'quote', 'synthesis'"
_SOURCES_EXTENDED = "'asset', 'report', 'paper', 'candidate'"
_SOURCES_BASE = _SOURCES_EXTENDED


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
    targets = "'report', 'candidate', 'memory'" if extended else "'report', 'candidate'"
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
        sa.CheckConstraint(f"derived_kind IN ({targets})", name="derived_target_allowed"),
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
    op.create_table(
        "reading_memory",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("group_key", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("uq_reading_memory_group", "reading_memory", ["group_key"], unique=True)
    op.create_table(
        "reading_memory_revision",
        sa.Column(
            "memory_id",
            sa.String(64),
            sa.ForeignKey("reading_memory.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("group_json", sa.JSON(), nullable=False),
        sa.Column("manifest_id", sa.String(64), nullable=False),
        sa.Column("grant_id", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("provenance_json", sa.JSON(), nullable=False),
        sa.Column("request_key", sa.String(128), nullable=True),
        sa.Column("request_hash", sa.String(64), nullable=True),
        sa.CheckConstraint("kind IN ('interest', 'value_standard')", name="memory_kind_allowed"),
        sa.CheckConstraint(
            "status IN ('pending', 'confirmed', 'deferred', "
            "'not_preference', 'disabled', 'needs_review')",
            name="memory_status_allowed",
        ),
        sa.CheckConstraint("scope IN ('general', 'task_only')", name="memory_scope_allowed"),
        sa.CheckConstraint(
            "origin IN ('model', 'user', 'source_change')", name="memory_origin_allowed"
        ),
    )
    op.create_index(
        "uq_reading_memory_request", "reading_memory_revision", ["request_key"], unique=True
    )


def downgrade() -> None:
    bind = op.get_bind()
    for query in (
        "SELECT 1 FROM reading_memory LIMIT 1",
        "SELECT 1 FROM reading_analysis_grant WHERE purpose='reading_profile_inference' LIMIT 1",
        "SELECT 1 FROM reading_derived_copy WHERE derived_kind='memory' "
        "OR field_kind='memory' LIMIT 1",
    ):
        if bind.execute(sa.text(query)).first():
            raise RuntimeError(
                "无法回退迁移 0032：存在研究记忆、用途授权或派生副本；请先导出并删除这些记录"
            )
    op.drop_index("uq_reading_memory_request", table_name="reading_memory_revision")
    op.drop_table("reading_memory_revision")
    op.drop_index("uq_reading_memory_group", table_name="reading_memory")
    op.drop_table("reading_memory")
    _rebuild_derived_copy(extended=False)
    _rebuild_grant(extended=False)
