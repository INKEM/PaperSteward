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
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from papersteward.storage.db import Base, utc_now


class OnboardingRow(Base):
    """单用户初始化流程；门槛证据与确认者保存在追加写事件中。"""

    __tablename__ = "onboarding"
    __table_args__ = (
        CheckConstraint("id = 1", name="singleton"),
        CheckConstraint(
            "(stage = 'profile' AND revision = 1) OR "
            "(stage = 'taxonomy' AND revision = 2) OR "
            "(stage = 'calibration' AND revision = 3) OR "
            "(stage = 'dry_run' AND revision = 4) OR "
            "(stage = 'active' AND revision = 5)",
            name="stage_matches_revision",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stage: Mapped[str] = mapped_column(String(16), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CalibrationRoundRow(Base):
    """P2 独立校准轮次；双方标注仅可各自写入一次。"""

    __tablename__ = "onboarding_calibration"
    __table_args__ = (
        CheckConstraint("round_number IN (1, 2)", name="round_limit"),
        CheckConstraint("sample_kind IN ('user', 'synthetic')", name="sample_kind_values"),
        CheckConstraint(
            "report_json IS NULL OR (agent_json IS NOT NULL AND user_json IS NOT NULL)",
            name="report_requires_labels",
        ),
    )
    round_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    g2_event_id: Mapped[str] = mapped_column(String(64), ForeignKey("event.id"), nullable=False)
    binding_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    cohort_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    rubric_version: Mapped[str] = mapped_column(String(64), nullable=False)
    sample_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    agent_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    user_json: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    report_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


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
    primary_tag: Mapped[str] = mapped_column(String(64), nullable=False)
    filing_bucket_id: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_version: Mapped[str] = mapped_column(String(32), nullable=False)
    taxonomy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    config_version: Mapped[str] = mapped_column(String(32), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    secondary_tags_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    score_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    priority: Mapped[float | None] = mapped_column(Float, nullable=True)


__all__ = [
    "ArchiveSnapshotRow",
    "BucketRow",
    "ConfigRow",
    "CalibrationRoundRow",
    "EventRow",
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
