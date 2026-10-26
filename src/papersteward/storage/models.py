"""正式领域表模型（P1-S2）。

ORM 类统一使用 ``*Row`` 后缀，避免与 ``papersteward.core.entities`` 中的不可变
领域实体混淆。持久化层负责：

- 枚举以字符串存储，由 ``storage.mappers`` 转回枚举；
- SQLite 的 naive datetime 在读取时补成 UTC；
- JSON 字段使用 SQLAlchemy ``JSON``，保持 ``*_json`` 命名；
- 约束尽量下沉到 DB，业务不变量仍在服务层校验（双重校验）。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from papersteward.storage.db import Base, utc_now


class OnboardingRow(Base):
    """单用户初始化流程；阶段迁移与确认者保存在追加写事件中。"""

    __tablename__ = "onboarding"
    __table_args__ = (
        CheckConstraint("id = 1", name="singleton"),
        CheckConstraint(
            "(stage = 'setup' AND revision = 0) OR "
            "(stage = 'draft' AND revision = 1) OR "
            "(stage = 'active' AND revision = 2)",
            name="stage_matches_revision",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stage: Mapped[str] = mapped_column(String(16), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IntakeMaterialRow(Base):
    """用户上传的研究方向材料；Agent 抽取草案的输入。"""

    __tablename__ = "intake_material"
    __table_args__ = (CheckConstraint("kind IN ('text', 'file')", name="kind_values"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class ResearchProfileRow(Base):
    """研究方向档案版本；``status='confirmed'`` 每个 id 只能有一条。"""

    __tablename__ = "research_profile"
    __table_args__ = (
        CheckConstraint(
            "(status = 'draft' AND confirmed_at IS NULL) "
            "OR (status <> 'draft' AND confirmed_at IS NOT NULL)",
            name="confirmed_at_matches_status",
        ),
        Index(
            "uq_research_profile_confirmed",
            "id",
            unique=True,
            sqlite_where=text("status = 'confirmed'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[str] = mapped_column(String(32), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    content_md: Mapped[str] = mapped_column(Text, nullable=False)
    parsed_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    change_log_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)


class TaxonomyRow(Base):
    """分类法版本；标签定义同时写入 ``tag`` 表与 ``tags_json`` 快照。"""

    __tablename__ = "taxonomy"
    __table_args__ = (
        CheckConstraint(
            "(status = 'draft' AND confirmed_at IS NULL) "
            "OR (status <> 'draft' AND confirmed_at IS NOT NULL)",
            name="confirmed_at_matches_status",
        ),
        Index(
            "uq_taxonomy_confirmed",
            "id",
            unique=True,
            sqlite_where=text("status = 'confirmed'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[str] = mapped_column(String(32), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    content_md: Mapped[str] = mapped_column(Text, nullable=False)
    tags_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    tags: Mapped[list[TagRow]] = relationship(
        back_populates="taxonomy",
        cascade="all, delete-orphan",
        order_by="TagRow.code",
        lazy="selectin",
    )


class TagRow(Base):
    """分类法版本内的主/副标签定义。"""

    __tablename__ = "tag"
    __table_args__ = (
        ForeignKeyConstraint(
            ["taxonomy_id", "taxonomy_version"],
            ["taxonomy.id", "taxonomy.version"],
            ondelete="CASCADE",
        ),
        CheckConstraint("level IN ('primary', 'secondary')", name="level_values"),
        Index("ix_tag_taxonomy_level", "taxonomy_id", "taxonomy_version", "level"),
    )

    taxonomy_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    taxonomy_version: Mapped[str] = mapped_column(String(32), primary_key=True)
    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    definition: Mapped[str] = mapped_column(Text, nullable=False, default="")
    parent_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    include_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    exclude_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    examples_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    aliases_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    taxonomy: Mapped[TaxonomyRow] = relationship(back_populates="tags")


class BucketRow(Base):
    """主题桶与特殊桶目录。"""

    __tablename__ = "bucket"
    __table_args__ = (
        CheckConstraint(
            "(bucket_type = 'topic' AND tag_code IS NOT NULL AND special_kind IS NULL) "
            "OR (bucket_type = 'special' AND tag_code IS NULL AND special_kind IS NOT NULL)",
            name="bucket_type_matches_owner",
        ),
        CheckConstraint("quota IS NULL OR quota >= 0", name="quota_non_negative"),
        Index(
            "uq_bucket_tag_code",
            "tag_code",
            unique=True,
            sqlite_where=text("tag_code IS NOT NULL"),
        ),
        Index(
            "uq_bucket_special_kind",
            "special_kind",
            unique=True,
            sqlite_where=text("special_kind IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    bucket_type: Mapped[str] = mapped_column(String(16), nullable=False)
    tag_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    special_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    quota: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class PaperRow(Base):
    """文献主记录；``created_at`` 是 P0 API 的兼容别名。"""

    __tablename__ = "paper"
    __table_args__ = (
        Index("ix_paper_status", "status"),
        Index("ix_paper_canonical_id", "canonical_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="discovered")
    authors_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    venue: Mapped[str | None] = mapped_column(String(256), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    abstract: Mapped[str | None] = mapped_column(Text, nullable=True)
    doi: Mapped[str | None] = mapped_column(String(256), nullable=True)
    arxiv_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    pdf_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    oa_status: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    canonical_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    @property
    def created_at(self) -> datetime:
        """兼容 P0 ``/papers`` 响应的字段名。"""
        return self.first_seen_at


class PaperSourceRow(Base):
    """不可变来源版本；同一来源ID的新内容追加，不覆盖历史。"""

    __tablename__ = "paper_source"
    __table_args__ = (Index("ix_paper_source_identity", "source", "source_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    paper_id: Mapped[str] = mapped_column(String(64), ForeignKey("paper.id"), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    candidate_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class PaperTagRow(Base):
    """文献标签分配；部分唯一索引保证每篇最多一条主标签。"""

    __tablename__ = "paper_tag"
    __table_args__ = (
        CheckConstraint("role IN ('primary', 'secondary')", name="role_values"),
        CheckConstraint("confidence >= 0.0 AND confidence <= 1.0", name="confidence_range"),
        CheckConstraint(
            "(provenance_model IS NULL AND provenance_prompt_version IS NULL "
            "AND provenance_generated_at IS NULL AND provenance_input_hash IS NULL) "
            "OR (provenance_model IS NOT NULL AND provenance_prompt_version IS NOT NULL "
            "AND provenance_generated_at IS NOT NULL AND provenance_input_hash IS NOT NULL)",
            name="provenance_all_or_none",
        ),
        Index(
            "uq_paper_tag_primary",
            "paper_id",
            unique=True,
            sqlite_where=text("role = 'primary'"),
        ),
        Index("ix_paper_tag_paper_role", "paper_id", "role"),
    )

    paper_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("paper.id", ondelete="CASCADE"), primary_key=True
    )
    tag_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), primary_key=True)
    taxonomy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    assigned_by: Mapped[str] = mapped_column(String(16), nullable=False, default="system")
    provenance_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provenance_prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provenance_generated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    provenance_input_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)


class PaperBucketRow(Base):
    """文献与桶的关系；部分唯一索引保证每篇最多一条归属桶。"""

    __tablename__ = "paper_bucket"
    __table_args__ = (
        CheckConstraint("role IN ('filing', 'membership')", name="role_values"),
        Index(
            "uq_paper_bucket_filing",
            "paper_id",
            unique=True,
            sqlite_where=text("role = 'filing'"),
        ),
        Index("ix_paper_bucket_paper_role", "paper_id", "role"),
    )

    paper_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("paper.id", ondelete="CASCADE"), primary_key=True
    )
    bucket_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("bucket.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(16), primary_key=True)
    assigned_by: Mapped[str] = mapped_column(String(16), nullable=False, default="system")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class SummaryRow(Base):
    """每篇文献一条 100 字概括。"""

    __tablename__ = "summary"
    __table_args__ = (
        CheckConstraint(
            "(provenance_model IS NULL AND provenance_prompt_version IS NULL "
            "AND provenance_generated_at IS NULL AND provenance_input_hash IS NULL) "
            "OR (provenance_model IS NOT NULL AND provenance_prompt_version IS NOT NULL "
            "AND provenance_generated_at IS NOT NULL AND provenance_input_hash IS NOT NULL)",
            name="provenance_all_or_none",
        ),
    )

    paper_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("paper.id", ondelete="CASCADE"), primary_key=True
    )
    summary_100: Mapped[str] = mapped_column(Text, nullable=False)
    what: Mapped[str] = mapped_column(Text, nullable=False)
    how: Mapped[str] = mapped_column(Text, nullable=False)
    finding: Mapped[str] = mapped_column(Text, nullable=False)
    limitation: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    validation_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    provenance_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provenance_prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provenance_generated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    provenance_input_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)


class EnrichmentRow(Base):
    """每篇文献一条富化信号；缺失字段保持中性值，来源与置信度可审计。"""

    __tablename__ = "enrichment"
    __table_args__ = (
        CheckConstraint("confidence >= 0.0 AND confidence <= 1.0", name="confidence_range"),
        CheckConstraint(
            "citation_count IS NULL OR citation_count >= 0", name="citation_count_non_negative"
        ),
        CheckConstraint(
            "citation_velocity IS NULL OR citation_velocity >= 0.0",
            name="citation_velocity_non_negative",
        ),
        CheckConstraint("oa_status IN ('open', 'closed', 'unknown')", name="oa_status_values"),
        CheckConstraint(
            "(provenance_model IS NULL AND provenance_prompt_version IS NULL "
            "AND provenance_generated_at IS NULL AND provenance_input_hash IS NULL) "
            "OR (provenance_model IS NOT NULL AND provenance_prompt_version IS NOT NULL "
            "AND provenance_generated_at IS NOT NULL AND provenance_input_hash IS NOT NULL)",
            name="provenance_all_or_none",
        ),
    )

    paper_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("paper.id", ondelete="CASCADE"), primary_key=True
    )
    teams_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    venue_level: Mapped[str | None] = mapped_column(Text, nullable=True)
    citation_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    citation_velocity: Mapped[float | None] = mapped_column(Float, nullable=True)
    code_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    data_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    oa_status: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    experiment_quality_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="neutral")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    provenance_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provenance_prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provenance_generated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    provenance_input_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)


class ScoreSnapshotRow(Base):
    """每日评分快照；主键保证同一天只有一条。"""

    __tablename__ = "score_snapshot"
    __table_args__ = (
        CheckConstraint("relevance >= 0.0 AND relevance <= 100.0", name="relevance_range"),
        CheckConstraint("authority >= 0.0 AND authority <= 100.0", name="authority_range"),
        CheckConstraint("extra_value >= 0.0 AND extra_value <= 100.0", name="extra_value_range"),
        CheckConstraint("static_score >= 0.0 AND static_score <= 100.0", name="static_range"),
        CheckConstraint("freshness >= 0.0 AND freshness <= 100.0", name="freshness_range"),
        CheckConstraint("hunger >= 0.0 AND hunger <= 100.0", name="hunger_range"),
        CheckConstraint("dynamic_score >= 0.0 AND dynamic_score <= 100.0", name="dynamic_range"),
        CheckConstraint("preference >= 0.0 AND preference <= 100.0", name="preference_range"),
        CheckConstraint(
            "priority IS NULL OR (priority >= 0.0 AND priority <= 100.0)", name="priority_range"
        ),
        CheckConstraint(
            "(priority IS NULL AND tier IS NOT NULL) OR (priority IS NOT NULL AND tier IS NULL)",
            name="priority_or_tier",
        ),
        CheckConstraint(
            "(provenance_model IS NULL AND provenance_prompt_version IS NULL "
            "AND provenance_generated_at IS NULL AND provenance_input_hash IS NULL) "
            "OR (provenance_model IS NOT NULL AND provenance_prompt_version IS NOT NULL "
            "AND provenance_generated_at IS NOT NULL AND provenance_input_hash IS NOT NULL)",
            name="provenance_all_or_none",
        ),
    )

    paper_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("paper.id", ondelete="CASCADE"), primary_key=True
    )
    score_date: Mapped[date] = mapped_column(Date, primary_key=True)
    relevance: Mapped[float] = mapped_column(Float, nullable=False)
    authority: Mapped[float] = mapped_column(Float, nullable=False)
    extra_value: Mapped[float] = mapped_column(Float, nullable=False)
    static_score: Mapped[float] = mapped_column(Float, nullable=False)
    freshness: Mapped[float] = mapped_column(Float, nullable=False)
    hunger: Mapped[float] = mapped_column(Float, nullable=False)
    dynamic_score: Mapped[float] = mapped_column(Float, nullable=False)
    preference: Mapped[float] = mapped_column(Float, nullable=False)
    priority: Mapped[float | None] = mapped_column(Float, nullable=True)
    tier: Mapped[str | None] = mapped_column(String(16), nullable=True)
    breakdown_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    config_version: Mapped[str] = mapped_column(String(32), nullable=False)
    profile_version: Mapped[str] = mapped_column(String(32), nullable=False)
    taxonomy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    provenance_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provenance_prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provenance_generated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    provenance_input_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)


class StaticScoreSnapshotRow(Base):
    """P4 静态分快照；与每日完整 score_snapshot 分离。"""

    __tablename__ = "static_score_snapshot"
    __table_args__ = (
        CheckConstraint("relevance >= 0.0 AND relevance <= 100.0", name="relevance_range"),
        CheckConstraint("authority >= 0.0 AND authority <= 100.0", name="authority_range"),
        CheckConstraint("extra_value >= 0.0 AND extra_value <= 100.0", name="extra_value_range"),
        CheckConstraint("static_score >= 0.0 AND static_score <= 100.0", name="static_range"),
        CheckConstraint("confidence >= 0.0 AND confidence <= 1.0", name="confidence_range"),
    )

    paper_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("paper.id", ondelete="CASCADE"), primary_key=True
    )
    score_date: Mapped[date] = mapped_column(Date, primary_key=True)
    relevance: Mapped[float] = mapped_column(Float, nullable=False)
    authority: Mapped[float] = mapped_column(Float, nullable=False)
    extra_value: Mapped[float] = mapped_column(Float, nullable=False)
    static_score: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    config_version: Mapped[str] = mapped_column(String(32), nullable=False)
    profile_version: Mapped[str] = mapped_column(String(32), nullable=False)
    taxonomy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    breakdown_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class PaperAnalysisRow(Base):
    """处理分析与质量标记；独立于历史审核记录。"""

    __tablename__ = "paper_analysis"
    paper_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("paper.id", ondelete="CASCADE"), primary_key=True
    )
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    quality_flags_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ReviewItemRow(Base):
    """人工审核项；每篇一条，开放/已处理状态可复用。"""

    __tablename__ = "review_item"
    __table_args__ = (
        CheckConstraint("status IN ('open', 'resolved')", name="status_values"),
        CheckConstraint(
            "resolution IS NULL OR resolution IN ('approved', 'relabeled')",
            name="resolution_values",
        ),
        CheckConstraint(
            "(status = 'open' AND resolved_at IS NULL AND resolved_by IS NULL "
            "AND resolution IS NULL) OR (status = 'resolved' AND resolved_at IS NOT NULL "
            "AND resolved_by IS NOT NULL AND resolution IS NOT NULL)",
            name="resolved_fields",
        ),
    )

    paper_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("paper.id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reasons_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    resolution: Mapped[str | None] = mapped_column(String(16), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class SpecialNominationRow(Base):
    """特殊桶提名与人工决策；每篇文献每个特殊类型一行。"""

    __tablename__ = "special_nomination"
    __table_args__ = (
        CheckConstraint("kind IN ('review', 'distant', 'classic')", name="kind_values"),
        CheckConstraint("status IN ('pending', 'accepted', 'rejected')", name="status_values"),
        CheckConstraint("confidence >= 0.0 AND confidence <= 1.0", name="confidence_range"),
        CheckConstraint(
            "(status = 'pending' AND decided_at IS NULL AND decided_by IS NULL) "
            "OR (status IN ('accepted', 'rejected') AND decided_at IS NOT NULL "
            "AND decided_by IS NOT NULL)",
            name="decided_fields",
        ),
        UniqueConstraint("paper_id", "kind", name="uq_special_nomination_paper_kind"),
        Index("ix_special_nomination_status_kind", "status", "kind"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    paper_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("paper.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    review_layers_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    classic_basis_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    bridge_memo_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    reasons_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    provenance_model: Mapped[str] = mapped_column(String(128), nullable=False)
    provenance_prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    provenance_generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    provenance_input_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    nominated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class SpecialTierSnapshotRow(Base):
    """特殊桶档位评估/人工覆盖；追加写，最新一条为当前档位。"""

    __tablename__ = "special_tier_snapshot"
    __table_args__ = (
        CheckConstraint("kind IN ('review', 'distant', 'classic')", name="kind_values"),
        CheckConstraint("tier IN ('high', 'medium', 'low')", name="tier_values"),
        CheckConstraint("assigned_by IN ('agent', 'user', 'system')", name="assigned_by_values"),
        CheckConstraint("value >= 0.0 AND value <= 100.0", name="value_range"),
        Index("ix_special_tier_paper_created", "paper_id", "created_at"),
        Index("ix_special_tier_kind_tier", "kind", "tier"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    paper_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("paper.id", ondelete="CASCADE"), nullable=False
    )
    nomination_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("special_nomination.id", ondelete="SET NULL"),
        nullable=True,
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    tier: Mapped[str] = mapped_column(String(16), nullable=False)
    dimensions_json: Mapped[dict[str, float]] = mapped_column(JSON, nullable=False)
    weights_json: Mapped[dict[str, float]] = mapped_column(JSON, nullable=False)
    reasons_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    explanation_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    evidence_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    breakdown_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    rubric_version: Mapped[str] = mapped_column(String(64), nullable=False)
    assigned_by: Mapped[str] = mapped_column(String(16), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ScoringChangeRow(Base):
    """全局权重变更预览/确认；只有 confirmed 行参与后续快照配置选择。"""

    __tablename__ = "scoring_change"
    __table_args__ = (
        CheckConstraint("status IN ('previewed', 'confirmed')", name="status_values"),
        CheckConstraint(
            "(status = 'previewed' AND confirmed_at IS NULL AND confirmed_by IS NULL) "
            "OR (status = 'confirmed' AND confirmed_at IS NOT NULL "
            "AND confirmed_by IS NOT NULL)",
            name="confirmed_fields",
        ),
        Index("ix_scoring_change_status_effective", "status", "effective_on"),
        Index("ix_scoring_change_config_version", "config_version"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    base_version: Mapped[str] = mapped_column(String(64), nullable=False)
    config_version: Mapped[str] = mapped_column(String(64), nullable=False)
    preview_day: Mapped[date] = mapped_column(Date, nullable=False)
    effective_on: Mapped[date] = mapped_column(Date, nullable=False)
    proposed_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    preview_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    preview_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class ViewConfigRow(Base):
    """用户保存的自定义排序视图；不影响全局评分配置。"""

    __tablename__ = "view_config"
    __table_args__ = (UniqueConstraint("name", name="uq_view_config_name"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    weights_json: Mapped[dict[str, float]] = mapped_column(JSON, nullable=False)
    preset: Mapped[str | None] = mapped_column(String(32), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EventRow(Base):
    """追加写事件日志；仓储层不提供 update/delete。"""

    __tablename__ = "event"
    __table_args__ = (
        Index("ix_event_created_at", "created_at"),
        Index("ix_event_paper_created", "paper_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    paper_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("paper.id", ondelete="SET NULL"), nullable=True
    )
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class RatingRow(Base):
    """阅读完成后的主观评分；允许同一篇多次修订。"""

    __tablename__ = "rating"
    __table_args__ = (
        CheckConstraint("score IS NULL OR (score >= 1 AND score <= 5)", name="score_range"),
    )

    paper_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("paper.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, default=utc_now
    )
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags_at_read_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    read_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)


class ReadingAssetRow(Base):
    """手工资产（收获/问题）的稳定身份；当前状态按最大版本读取。"""

    __tablename__ = "reading_asset"
    __table_args__ = (
        CheckConstraint("kind IN ('harvest', 'problem')", name="asset_kind_allowed"),
        Index("uq_reading_asset_idempotency", "idempotency_key", unique=True),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class ReadingAssetRevisionRow(Base):
    """资产的一个内容/状态版本；只追加，旧版本与当时来源保留。"""

    __tablename__ = "reading_asset_revision"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'archived', 'open', 'resolved')",
            name="asset_status_allowed",
        ),
    )

    asset_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("reading_asset.id", ondelete="CASCADE"), primary_key=True
    )
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    applicability: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)


class ReadingAssetSourceRow(Base):
    """资产版本的来源引用；指向归档哈希，可选引用具体反馈版本。"""

    __tablename__ = "reading_asset_source"
    __table_args__ = (
        ForeignKeyConstraint(
            ["asset_id", "revision"],
            ["reading_asset_revision.asset_id", "reading_asset_revision.revision"],
            ondelete="CASCADE",
        ),
        Index("ix_reading_asset_source_revision", "asset_id", "revision"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    asset_id: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    paper_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("paper.id", ondelete="CASCADE"), nullable=False
    )
    archive_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    rating_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReadingReportRow(Base):
    """已保存的阅读回顾报告快照；正文冻结，读取时校验哈希。

    运行 ID 与输入指纹均唯一：前者保证同 run 重试不重复建报告，
    后者让相同规范化请求与数据去重到既有快照（保留其原截止时间）。
    """

    __tablename__ = "reading_report"
    __table_args__ = (
        CheckConstraint(
            "report_kind IN ('week', 'month', 'all', 'custom')", name="report_kind_allowed"
        ),
        CheckConstraint(
            "review_mode IN ('as_recorded', 'revised_now')", name="review_mode_allowed"
        ),
        Index("uq_reading_report_run", "run_id", unique=True),
        Index("uq_reading_report_fingerprint", "input_fingerprint", unique=True),
        Index("ix_reading_report_generated", "generated_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    report_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    period_label: Mapped[str] = mapped_column(String(32), nullable=False)
    period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    review_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    knowledge_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    contract: Mapped[str] = mapped_column(String(32), nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    content_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)


class ReadingReportContextRow(Base):
    """报告的独立范围、规范化请求和有效输入清单；不依赖运行记录存续。"""

    __tablename__ = "reading_report_context"
    __table_args__ = (Index("ix_reading_report_context_series", "series_key"),)

    report_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("reading_report.id", ondelete="CASCADE"), primary_key=True
    )
    context_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    series_key: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ReadingTimeRevisionRow(Base):
    """阅读时间声明版本；只追加，不改归档快照/评分/事件。

    精度三值互斥由 CHECK 下沉到数据库；``(paper_id, revision)`` 主键提供
    乐观并发的最终兜底，幂等键唯一索引支撑重复请求重放。
    """

    __tablename__ = "reading_time_revision"
    __table_args__ = (
        CheckConstraint("precision IN ('day', 'month', 'unknown')", name="precision_allowed"),
        CheckConstraint(
            "(precision = 'day' AND declared_date IS NOT NULL AND declared_month IS NULL)"
            " OR (precision = 'month' AND declared_date IS NULL AND declared_month IS NOT NULL)"
            " OR (precision = 'unknown' AND declared_date IS NULL AND declared_month IS NULL)",
            name="precision_value_coherent",
        ),
        CheckConstraint(
            "basis IN ('system_recorded', 'declared', 'unknown')", name="basis_allowed"
        ),
        Index("uq_reading_time_idempotency", "idempotency_key", unique=True),
    )

    paper_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("paper.id", ondelete="CASCADE"), primary_key=True
    )
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    precision: Mapped[str] = mapped_column(String(8), nullable=False)
    declared_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    declared_month: Mapped[str | None] = mapped_column(String(7), nullable=True)
    basis: Mapped[str] = mapped_column(String(16), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)


class ReadingWriteReceiptRow(Base):
    """写入回执：幂等键绑定操作、目标与规范化请求哈希，可重放原结果。

    身份是幂等键（工作区内唯一，唯一索引兜底并发）；``request_hash`` 为空
    表示无法证明原始请求内容，命中即冲突。无外键：回执比目标存得久，
    目标删除后回执仍能解释「同键重放」而不是变成一次新写入。
    """

    __tablename__ = "reading_write_receipt"
    __table_args__ = (
        CheckConstraint(
            "operation IN ('reading_time.record', 'asset.create', 'asset.revise', 'asset.status',"
            " 'structured_feedback.revise')",
            name="receipt_operation_allowed",
        ),
        Index("uq_reading_write_receipt_key", "idempotency_key", unique=True),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class ReadingDerivedCopyRow(Base):
    """私人派生副本登记：来源（资产版本/报告）到含其内容的持久对象。

    只登记 ID 与字段类别，不保存私人原文；按来源版本可找出全部副本，
    供删除预览与后续授权撤回复用。目标侧外键级联：报告删除即清登记；
    来源侧无外键——资产删除后其副本仍留在报告中，登记必须保留。
    """

    __tablename__ = "reading_derived_copy"
    __table_args__ = (
        CheckConstraint("source_kind IN ('asset', 'report')", name="derived_source_allowed"),
        CheckConstraint("derived_kind IN ('report')", name="derived_target_allowed"),
        CheckConstraint(
            "field_kind IN ('title', 'body', 'sanitized')", name="derived_field_allowed"
        ),
        Index("ix_reading_derived_copy_source", "source_kind", "source_id", "source_revision"),
        Index("ix_reading_derived_copy_target", "derived_kind", "derived_id"),
        Index(
            "uq_reading_derived_copy_entry",
            "source_kind",
            "source_id",
            "source_revision",
            "derived_kind",
            "derived_id",
            "field_kind",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    derived_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    derived_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("reading_report.id", ondelete="CASCADE"), nullable=False
    )
    field_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class ReadingStructuredFeedbackRow(Base):
    """结构化阅读理由版本；只追加，不写评分/备注/归档。

    理由码与自由文本只决定写入结果，哈希进回执；``rating_at`` 指向具体评分
    版本（``rating.created_at``），后续评分修订不移动该关联。``(paper_id,
    revision)`` 主键提供乐观并发兜底，幂等键唯一索引支撑重放。
    """

    __tablename__ = "reading_structured_feedback"
    __table_args__ = (Index("uq_reading_structured_feedback_key", "idempotency_key", unique=True),)

    paper_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("paper.id", ondelete="CASCADE"), primary_key=True
    )
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    rating_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    value_sources_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    limited_reasons_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    context_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    applicability: Mapped[str | None] = mapped_column(Text, nullable=True)
    takeaway: Mapped[str | None] = mapped_column(Text, nullable=True)
    question: Mapped[str | None] = mapped_column(Text, nullable=True)
    usage_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    vocabulary_version: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)


class LLMCacheRow(Base):
    """LLM 结果缓存；键为 model+prompt 版本+输入哈希，命中即跳过 provider 调用。

    只缓存最终通过 schema 校验的输出；档案/分类法等输入变化会改变渲染后的
    input_hash，因此缓存随内容自然失效，不做 TTL。
    """

    __tablename__ = "llm_cache"
    __table_args__ = (
        Index("uq_llm_cache_key", "model", "prompt_version", "input_hash", unique=True),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    repair_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class ConfigRow(Base):
    """版本化配置；``(key, version)`` 唯一。"""

    __tablename__ = "config"
    __table_args__ = (Index("ix_config_key_updated", "key", "updated_at"),)

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    version: Mapped[str] = mapped_column(String(32), primary_key=True)
    value_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    updated_by: Mapped[str] = mapped_column(String(16), nullable=False, default="system")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class RunRow(Base):
    """一次任务运行记录；终态必须有结束时间。"""

    __tablename__ = "run"
    __table_args__ = (
        CheckConstraint(
            "(status = 'running' AND finished_at IS NULL) "
            "OR (status <> 'running' AND finished_at IS NOT NULL "
            "AND finished_at >= started_at)",
            name="finished_at_matches_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stats_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    errors_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    cost_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class DigestRow(Base):
    __tablename__ = "digest"

    digest_date: Mapped[date] = mapped_column(Date, primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DeliveryRow(Base):
    __tablename__ = "delivery"
    __table_args__ = (
        CheckConstraint("channel IN ('local_markdown', 'smtp')", name="channel_values"),
        CheckConstraint(
            "status IN ('reserved', 'sent', 'failed', 'uncertain')", name="status_values"
        ),
    )

    digest_date: Mapped[date] = mapped_column(
        Date, ForeignKey("digest.digest_date"), primary_key=True
    )
    channel: Mapped[str] = mapped_column(String(32), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    destination_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ArchiveSnapshotRow(Base):
    """不可变归档快照；仓储只提供 insert/get。"""

    __tablename__ = "archive_snapshot"
    __table_args__ = (
        CheckConstraint("rating IS NULL OR (rating >= 1 AND rating <= 5)", name="rating_range"),
        CheckConstraint(
            "priority IS NULL OR (priority >= 0.0 AND priority <= 100.0)",
            name="priority_range",
        ),
    )

    paper_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("paper.id", ondelete="CASCADE"), primary_key=True
    )
    archived_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # 导入归档允许无主标签/归属桶（用户未选标签时），列可空；既有 done 归档仍必有值。
    primary_tag: Mapped[str | None] = mapped_column(String(64), nullable=True)
    filing_bucket_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    profile_version: Mapped[str] = mapped_column(String(32), nullable=False)
    taxonomy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    config_version: Mapped[str] = mapped_column(String(32), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    content_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'")
    )
    secondary_tags_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    score_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    priority: Mapped[float | None] = mapped_column(Float, nullable=True)


__all__ = [
    "ArchiveSnapshotRow",
    "BucketRow",
    "ConfigRow",
    "EventRow",
    "IntakeMaterialRow",
    "PaperBucketRow",
    "PaperRow",
    "PaperTagRow",
    "RatingRow",
    "ResearchProfileRow",
    "RunRow",
    "ScoreSnapshotRow",
    "SummaryRow",
    "TagRow",
    "TaxonomyRow",
]
