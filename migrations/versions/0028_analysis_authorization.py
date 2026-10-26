"""阅读分析授权与材料清单：授权、撤回事件、清单、预览令牌与用户摘录。

全部为新增表；降级整体删除（授权/清单/令牌/摘录历史不可恢复，降级前需
自行备份）。审计与清单只存 ID、哈希与类别，不存私人正文。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: str | Sequence[str] | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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
        sa.CheckConstraint("purpose IN ('reading_reflection')", name="grant_purpose_allowed"),
        sa.CheckConstraint("scope_mode IN ('once', 'workspace')", name="grant_scope_allowed"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_reading_analysis_grant_purpose",
        "reading_analysis_grant",
        ["purpose", "revoked_at"],
    )
    op.create_table(
        "reading_analysis_revoke_event",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("grant_id", sa.String(64), nullable=False),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("reason IN ('user')", name="revoke_reason_allowed"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_reading_analysis_revoke_grant",
        "reading_analysis_revoke_event",
        ["grant_id"],
    )
    op.create_table(
        "reading_analysis_manifest",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("content_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_reading_analysis_manifest",
        "reading_analysis_manifest",
        ["purpose", "manifest_hash"],
        unique=True,
    )
    op.create_table(
        "reading_analysis_preview",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("token", sa.String(64), nullable=False),
        sa.Column("manifest_id", sa.String(64), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("model_config_version", sa.String(32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "consumed_at IS NULL OR consumed_at >= created_at",
            name="preview_consumed_after_created",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_reading_analysis_preview_token",
        "reading_analysis_preview",
        ["token"],
        unique=True,
    )
    op.create_index(
        "ix_reading_analysis_preview_manifest",
        "reading_analysis_preview",
        ["manifest_id"],
    )
    op.create_table(
        "reading_analysis_excerpt",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("paper_id", sa.String(64), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source_note", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["paper_id"], ["paper.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_reading_analysis_excerpt",
        "reading_analysis_excerpt",
        ["paper_id", "content_hash"],
        unique=True,
    )


def downgrade() -> None:
    # 授权、撤回记录、材料清单、预览令牌与用户摘录随表删除，不可恢复；
    # 降级前先备份可完整保留。
    op.drop_index("uq_reading_analysis_excerpt", table_name="reading_analysis_excerpt")
    op.drop_table("reading_analysis_excerpt")
    op.drop_index("ix_reading_analysis_preview_manifest", table_name="reading_analysis_preview")
    op.drop_index("uq_reading_analysis_preview_token", table_name="reading_analysis_preview")
    op.drop_table("reading_analysis_preview")
    op.drop_index("uq_reading_analysis_manifest", table_name="reading_analysis_manifest")
    op.drop_table("reading_analysis_manifest")
    op.drop_index("ix_reading_analysis_revoke_grant", table_name="reading_analysis_revoke_event")
    op.drop_table("reading_analysis_revoke_event")
    op.drop_index("ix_reading_analysis_grant_purpose", table_name="reading_analysis_grant")
    op.drop_table("reading_analysis_grant")
