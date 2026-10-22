"""P1 领域实体。

实体是不可变 dataclass，不依赖 ORM 或 FastAPI，可被状态机、仓储映射、文档解析器
与 schema 生成共同复用。字段命名遵循设计文档 §3.2；JSON 类字段在领域层使用
``Mapping``，由持久化层负责序列化。

聚合级不变量（每篇一个主标签/一个归属桶、置信度门槛、静态分冻结等）位于
``papersteward.core.invariants``。
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlparse

from papersteward.core.enums import (
    AssignedBy,
    BucketRole,
    BucketType,
    DocumentStatus,
    EventType,
    OaStatus,
    PaperStatus,
    ReadMode,
    ReviewResolution,
    ReviewStatus,
    RunStatus,
    ScoreTier,
    SpecialBucketKind,
    SummaryValidationStatus,
    TagLevel,
    TagRole,
)
from papersteward.core.errors import DomainValidationError

_MAX_SUMMARY_CHARS = 100
_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_-]{1,31}$")
_HASH_PATTERN = re.compile(r"^[0-9a-fA-F]{16,128}$")


def utc_now() -> datetime:
    """领域层的时间源；与 ``storage.db.utc_now`` 保持同一 UTC 约定。"""
    return datetime.now(UTC)


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DomainValidationError(field_name, "不能为空")
    return value.strip()


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DomainValidationError(field_name, "必须是字符串或 null")
    stripped = value.strip()
    return stripped or None


def _optional_http_url(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    text = _require_text(value, field_name)
    parsed = urlparse(text)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
    ):
        raise DomainValidationError(field_name, "必须是 http(s) URL 且不能包含认证信息")
    return text


def _require_code(value: object, field_name: str) -> str:
    code = _require_text(value, field_name)
    if not _CODE_PATTERN.fullmatch(code):
        raise DomainValidationError(
            field_name,
            "必须是以大写字母开头、由大写字母/数字/下划线/连字符组成的稳定代码",
        )
    return code


def _require_aware(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise DomainValidationError(field_name, "必须是 datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise DomainValidationError(field_name, "必须带时区")
    return value.astimezone(UTC)


def _optional_aware(value: datetime | None, field_name: str) -> datetime | None:
    if value is None:
        return None
    return _require_aware(value, field_name)


def _require_ratio(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise DomainValidationError(field_name, "必须是 0–1 之间的数值")
    ratio = float(value)
    if not 0.0 <= ratio <= 1.0:
        raise DomainValidationError(field_name, "必须在 0–1 之间")
    return ratio


def _require_score(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise DomainValidationError(field_name, "必须是 0–100 之间的数值")
    score = float(value)
    if not 0.0 <= score <= 100.0:
        raise DomainValidationError(field_name, "必须在 0–100 之间")
    return score


def _require_text_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, tuple | list):
        raise DomainValidationError(field_name, "必须是字符串元组")
    return tuple(_require_text(item, f"{field_name}[]") for item in value)


def _require_mapping(value: object, field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise DomainValidationError(field_name, "必须是 JSON 对象")
    return dict(value)


def _require_hash(value: object, field_name: str) -> str:
    digest = _require_text(value, field_name)
    if not _HASH_PATTERN.fullmatch(digest):
        raise DomainValidationError(field_name, "必须是 16–128 位十六进制摘要")
    return digest


def _require_quota(value: int | None, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise DomainValidationError(field_name, "必须是整数或 null")
    if value < 0:
        raise DomainValidationError(field_name, "不能为负数")
    return value


def _document_confirmed_at(
    status: DocumentStatus, confirmed_at: datetime | None, field_name: str
) -> datetime | None:
    confirmed = _optional_aware(confirmed_at, field_name)
    if status is DocumentStatus.DRAFT and confirmed is not None:
        raise DomainValidationError(field_name, "草稿不能有确认时间")
    if status is not DocumentStatus.DRAFT and confirmed is None:
        raise DomainValidationError(field_name, "已确认/已归档文档必须有确认时间")
    return confirmed


@dataclass(frozen=True, slots=True)
class Provenance:
    """Agent 产物溯源：模型、prompt 版本、生成时间与输入哈希。"""

    model: str
    prompt_version: str
    generated_at: datetime
    input_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "model", _require_text(self.model, "provenance.model"))
        object.__setattr__(
            self, "prompt_version", _require_text(self.prompt_version, "provenance.prompt_version")
        )
        object.__setattr__(
            self,
            "generated_at",
            _require_aware(self.generated_at, "provenance.generated_at"),
        )
        object.__setattr__(
            self, "input_hash", _require_hash(self.input_hash, "provenance.input_hash")
        )


@dataclass(frozen=True, slots=True)
class Tag:
    """主标签或副标签的受控词条；``code`` 一旦分配不再复用。"""

    code: str
    name: str
    level: TagLevel
    definition: str = ""
    parent_code: str | None = None
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    active: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _require_code(self.code, "tag.code"))
        object.__setattr__(self, "name", _require_text(self.name, "tag.name"))
        if not isinstance(self.level, TagLevel):
            raise DomainValidationError("tag.level", "必须是 TagLevel")
        object.__setattr__(
            self, "definition", _optional_text(self.definition, "tag.definition") or ""
        )
        if self.parent_code is not None:
            parent = _require_code(self.parent_code, "tag.parent_code")
            if parent == self.code:
                raise DomainValidationError("tag.parent_code", "不能引用自身")
            object.__setattr__(self, "parent_code", parent)
            if self.level is TagLevel.PRIMARY:
                raise DomainValidationError("tag.parent_code", "主标签不能有父标签")
        for name in ("include", "exclude", "examples", "aliases"):
            object.__setattr__(self, name, _require_text_tuple(getattr(self, name), f"tag.{name}"))


@dataclass(frozen=True, slots=True)
class Bucket:
    """主题桶或特殊桶；主题桶由主标签驱动，特殊桶由特殊归属规则驱动。"""

    id: str
    name: str
    bucket_type: BucketType
    tag_code: str | None = None
    special_kind: SpecialBucketKind | None = None
    quota: int | None = None
    active: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _require_text(self.id, "bucket.id"))
        object.__setattr__(self, "name", _require_text(self.name, "bucket.name"))
        if not isinstance(self.bucket_type, BucketType):
            raise DomainValidationError("bucket.bucket_type", "必须是 BucketType")
        if self.bucket_type is BucketType.TOPIC:
            if self.tag_code is None:
                raise DomainValidationError("bucket.tag_code", "主题桶必须绑定主标签")
            if self.special_kind is not None:
                raise DomainValidationError("bucket.special_kind", "主题桶不能设置特殊类型")
            object.__setattr__(self, "tag_code", _require_code(self.tag_code, "bucket.tag_code"))
        else:
            if self.special_kind is None:
                raise DomainValidationError("bucket.special_kind", "特殊桶必须设置特殊类型")
            if self.tag_code is not None:
                raise DomainValidationError("bucket.tag_code", "特殊桶不能绑定主标签")
        if self.special_kind is not None and not isinstance(self.special_kind, SpecialBucketKind):
            raise DomainValidationError("bucket.special_kind", "必须是 SpecialBucketKind")
        object.__setattr__(self, "quota", _require_quota(self.quota, "bucket.quota"))

    @property
    def is_topic(self) -> bool:
        return self.bucket_type is BucketType.TOPIC

    @property
    def is_special(self) -> bool:
        return self.bucket_type is BucketType.SPECIAL


@dataclass(frozen=True, slots=True)
class ResearchProfile:
    """研究方向档案；Markdown 为源，``parsed_json`` 是解析缓存。"""

    id: str
    version: str
    status: DocumentStatus
    content_md: str
    parsed_json: Mapping[str, Any] = field(default_factory=dict)
    confirmed_at: datetime | None = None
    change_log: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _require_text(self.id, "profile.id"))
        object.__setattr__(self, "version", _require_text(self.version, "profile.version"))
        object.__setattr__(self, "content_md", _require_text(self.content_md, "profile.content_md"))
        if not isinstance(self.status, DocumentStatus):
            raise DomainValidationError("profile.status", "必须是 DocumentStatus")
        object.__setattr__(
            self,
            "confirmed_at",
            _document_confirmed_at(self.status, self.confirmed_at, "profile.confirmed_at"),
        )
        object.__setattr__(
            self, "parsed_json", _require_mapping(self.parsed_json, "profile.parsed_json")
        )
        object.__setattr__(
            self,
            "change_log",
            _require_text_tuple(self.change_log, "profile.change_log"),
        )


# 计划文档使用 ``Profile`` 作为简称；与持久化表名 ``research_profile`` 对应。
Profile = ResearchProfile


@dataclass(frozen=True, slots=True)
class Taxonomy:
    """分类法版本；标签代码在版本内唯一，确认后冻结供文献引用。"""

    id: str
    version: str
    status: DocumentStatus
    content_md: str
    tags: tuple[Tag, ...] = ()
    confirmed_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _require_text(self.id, "taxonomy.id"))
        object.__setattr__(self, "version", _require_text(self.version, "taxonomy.version"))
        object.__setattr__(
            self, "content_md", _require_text(self.content_md, "taxonomy.content_md")
        )
        if not isinstance(self.status, DocumentStatus):
            raise DomainValidationError("taxonomy.status", "必须是 DocumentStatus")
        object.__setattr__(
            self,
            "confirmed_at",
            _document_confirmed_at(self.status, self.confirmed_at, "taxonomy.confirmed_at"),
        )
        if not isinstance(self.tags, tuple | list):
            raise DomainValidationError("taxonomy.tags", "必须是 Tag 元组")
        tags = tuple(self.tags)
        for tag in tags:
            if not isinstance(tag, Tag):
                raise DomainValidationError("taxonomy.tags[]", "必须是 Tag")
        codes = [tag.code for tag in tags]
        if len(codes) != len(set(codes)):
            raise DomainValidationError("taxonomy.tags", "标签代码必须唯一")
        if self.status is not DocumentStatus.DRAFT and not tags:
            raise DomainValidationError("taxonomy.tags", "已确认分类法不能没有标签")
        object.__setattr__(self, "tags", tags)


@dataclass(frozen=True, slots=True)
class Paper:
    """文献主记录；状态迁移由 ``core.state_machine`` 控制。"""

    id: str
    title: str
    status: PaperStatus = PaperStatus.DISCOVERED
    authors: tuple[str, ...] = ()
    venue: str | None = None
    published_at: datetime | None = None
    first_seen_at: datetime = field(default_factory=utc_now)
    abstract: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    url: str | None = None
    pdf_url: str | None = None
    oa_status: OaStatus = OaStatus.UNKNOWN
    canonical_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _require_text(self.id, "paper.id"))
        object.__setattr__(self, "title", _require_text(self.title, "paper.title"))
        if not isinstance(self.status, PaperStatus):
            raise DomainValidationError("paper.status", "必须是 PaperStatus")
        object.__setattr__(self, "authors", _require_text_tuple(self.authors, "paper.authors"))
        for name in ("venue", "abstract", "doi", "arxiv_id", "url", "pdf_url", "canonical_id"):
            object.__setattr__(self, name, _optional_text(getattr(self, name), f"paper.{name}"))
        object.__setattr__(
            self, "published_at", _optional_aware(self.published_at, "paper.published_at")
        )
        object.__setattr__(
            self,
            "first_seen_at",
            _require_aware(self.first_seen_at, "paper.first_seen_at"),
        )
        if not isinstance(self.oa_status, OaStatus):
            raise DomainValidationError("paper.oa_status", "必须是 OaStatus")

    @property
    def created_at(self) -> datetime:
        """P0 ``/papers`` 响应的兼容别名；新代码使用 ``first_seen_at``。"""
        return self.first_seen_at


@dataclass(frozen=True, slots=True)
class PaperTag:
    """文献标签分配；每篇恰好一条 ``PRIMARY``，副标签 0–5 条。"""

    paper_id: str
    tag_code: str
    role: TagRole
    taxonomy_version: str
    confidence: float = 1.0
    assigned_by: AssignedBy = AssignedBy.SYSTEM
    provenance: Provenance | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "paper_id", _require_text(self.paper_id, "paper_tag.paper_id"))
        object.__setattr__(self, "tag_code", _require_text(self.tag_code, "paper_tag.tag_code"))
        if not isinstance(self.role, TagRole):
            raise DomainValidationError("paper_tag.role", "必须是 TagRole")
        object.__setattr__(
            self,
            "taxonomy_version",
            _require_text(self.taxonomy_version, "paper_tag.taxonomy_version"),
        )
        object.__setattr__(
            self, "confidence", _require_ratio(self.confidence, "paper_tag.confidence")
        )
        if not isinstance(self.assigned_by, AssignedBy):
            raise DomainValidationError("paper_tag.assigned_by", "必须是 AssignedBy")
        if self.assigned_by is AssignedBy.AGENT and self.provenance is None:
            raise DomainValidationError("paper_tag.provenance", "Agent 生成的标签必须带溯源信息")
        if self.provenance is not None and not isinstance(self.provenance, Provenance):
            raise DomainValidationError("paper_tag.provenance", "必须是 Provenance 或 null")


@dataclass(frozen=True, slots=True)
class PaperBucket:
    """文献与桶的关系；每篇恰好一条 ``FILING``，成员关系可多条。"""

    paper_id: str
    bucket_id: str
    role: BucketRole
    assigned_by: AssignedBy = AssignedBy.SYSTEM
    reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "paper_id", _require_text(self.paper_id, "paper_bucket.paper_id"))
        object.__setattr__(
            self, "bucket_id", _require_text(self.bucket_id, "paper_bucket.bucket_id")
        )
        if not isinstance(self.role, BucketRole):
            raise DomainValidationError("paper_bucket.role", "必须是 BucketRole")
        if not isinstance(self.assigned_by, AssignedBy):
            raise DomainValidationError("paper_bucket.assigned_by", "必须是 AssignedBy")
        object.__setattr__(self, "reason", _optional_text(self.reason, "paper_bucket.reason"))


@dataclass(frozen=True, slots=True)
class Summary:
    """100 字概括与三槽拆解；``evidence`` 保存可核对的原文引文。"""

    paper_id: str
    summary_100: str
    what: str
    how: str
    finding: str
    limitation: str
    evidence: tuple[str, ...] = ()
    validation_status: SummaryValidationStatus = SummaryValidationStatus.PENDING
    provenance: Provenance | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "paper_id", _require_text(self.paper_id, "summary.paper_id"))
        text = _require_text(self.summary_100, "summary.summary_100")
        if sum(1 for char in text if not char.isspace()) > _MAX_SUMMARY_CHARS:
            raise DomainValidationError("summary.summary_100", "不能超过 100 个非空白字符")
        object.__setattr__(self, "summary_100", text)
        for name in ("what", "how", "finding", "limitation"):
            object.__setattr__(self, name, _require_text(getattr(self, name), f"summary.{name}"))
        object.__setattr__(self, "evidence", _require_text_tuple(self.evidence, "summary.evidence"))
        if not isinstance(self.validation_status, SummaryValidationStatus):
            raise DomainValidationError(
                "summary.validation_status", "必须是 SummaryValidationStatus"
            )
        if self.validation_status is SummaryValidationStatus.PASSED:
            if self.provenance is None:
                raise DomainValidationError("summary.provenance", "校验通过的摘要必须带溯源信息")
            if not self.evidence:
                raise DomainValidationError("summary.evidence", "校验通过的摘要必须至少有一条证据")
        if self.provenance is not None and not isinstance(self.provenance, Provenance):
            raise DomainValidationError("summary.provenance", "必须是 Provenance 或 null")


@dataclass(frozen=True, slots=True)
class EnrichmentSignals:
    """P4 富化信号；未知字段保持中性值，缺失证据不得被解释为负面信号。"""

    teams: tuple[str, ...] = ()
    venue_level: str | None = None
    citation_count: int | None = None
    citation_velocity: float | None = None
    code_url: str | None = None
    data_url: str | None = None
    oa_status: OaStatus = OaStatus.UNKNOWN
    experiment_quality: tuple[str, ...] = ()
    confidence: float = 0.0
    source: str = "neutral"

    def __post_init__(self) -> None:
        for name in ("teams", "experiment_quality"):
            values = _require_text_tuple(getattr(self, name), f"enrichment.{name}")
            if len(set(values)) != len(values):
                raise DomainValidationError(f"enrichment.{name}", "不能包含重复项")
            object.__setattr__(self, name, values)
        object.__setattr__(
            self, "venue_level", _optional_text(self.venue_level, "enrichment.venue_level")
        )
        object.__setattr__(
            self, "citation_count", _require_quota(self.citation_count, "enrichment.citation_count")
        )
        if self.citation_velocity is not None:
            velocity = self.citation_velocity
            if (
                isinstance(velocity, bool)
                or not isinstance(velocity, int | float)
                or not math.isfinite(velocity)
                or velocity < 0
            ):
                raise DomainValidationError(
                    "enrichment.citation_velocity", "必须是非负有限数或 null"
                )
            object.__setattr__(self, "citation_velocity", float(velocity))
        object.__setattr__(
            self, "code_url", _optional_http_url(self.code_url, "enrichment.code_url")
        )
        object.__setattr__(
            self, "data_url", _optional_http_url(self.data_url, "enrichment.data_url")
        )
        if not isinstance(self.oa_status, OaStatus):
            raise DomainValidationError("enrichment.oa_status", "必须是 OaStatus")
        object.__setattr__(
            self, "confidence", _require_ratio(self.confidence, "enrichment.confidence")
        )
        object.__setattr__(self, "source", _require_text(self.source, "enrichment.source"))


@dataclass(frozen=True, slots=True)
class Enrichment:
    """P4 富化结果；每篇文献一条，保留来源/置信度与可选 Agent 溯源。"""

    paper_id: str
    signals: EnrichmentSignals
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    provenance: Provenance | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "paper_id", _require_text(self.paper_id, "enrichment.paper_id"))
        if not isinstance(self.signals, EnrichmentSignals):
            raise DomainValidationError("enrichment.signals", "必须是 EnrichmentSignals")
        object.__setattr__(
            self, "created_at", _require_aware(self.created_at, "enrichment.created_at")
        )
        object.__setattr__(
            self, "updated_at", _require_aware(self.updated_at, "enrichment.updated_at")
        )
        if self.updated_at < self.created_at:
            raise DomainValidationError("enrichment.updated_at", "不能早于 created_at")
        if self.provenance is not None and not isinstance(self.provenance, Provenance):
            raise DomainValidationError("enrichment.provenance", "必须是 Provenance 或 null")


@dataclass(frozen=True, slots=True)
class ScoreSnapshot:
    """某日评分快照；追加写，历史不覆盖。"""

    paper_id: str
    score_date: date
    relevance: float
    authority: float
    extra_value: float
    static_score: float
    freshness: float
    hunger: float
    dynamic_score: float
    preference: float
    config_version: str
    profile_version: str
    taxonomy_version: str
    priority: float | None = None
    tier: ScoreTier | None = None
    breakdown_json: Mapping[str, Any] = field(default_factory=dict)
    provenance: Provenance | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "paper_id", _require_text(self.paper_id, "score_snapshot.paper_id")
        )
        if isinstance(self.score_date, datetime):
            object.__setattr__(self, "score_date", self.score_date.date())
        if not isinstance(self.score_date, date):
            raise DomainValidationError("score_snapshot.score_date", "必须是 date")
        for name in (
            "relevance",
            "authority",
            "extra_value",
            "static_score",
            "freshness",
            "hunger",
            "dynamic_score",
            "preference",
        ):
            object.__setattr__(
                self,
                name,
                _require_score(getattr(self, name), f"score_snapshot.{name}"),
            )
        if self.priority is not None:
            object.__setattr__(
                self,
                "priority",
                _require_score(self.priority, "score_snapshot.priority"),
            )
        if (self.priority is None) == (self.tier is None):
            raise DomainValidationError(
                "score_snapshot",
                "主队列快照必须有 priority 且无 tier；特殊桶快照必须有 tier 且无 priority",
            )
        if self.tier is not None and not isinstance(self.tier, ScoreTier):
            raise DomainValidationError("score_snapshot.tier", "必须是 ScoreTier 或 null")
        for name in ("config_version", "profile_version", "taxonomy_version"):
            object.__setattr__(
                self, name, _require_text(getattr(self, name), f"score_snapshot.{name}")
            )
        object.__setattr__(
            self,
            "breakdown_json",
            _require_mapping(self.breakdown_json, "score_snapshot.breakdown_json"),
        )
        if self.provenance is not None and not isinstance(self.provenance, Provenance):
            raise DomainValidationError("score_snapshot.provenance", "必须是 Provenance 或 null")


@dataclass(frozen=True, slots=True)
class StaticScoreSnapshot:
    """P4 静态分快照；与每日完整 score_snapshot 分离，按版本冻结/追加。"""

    paper_id: str
    score_date: date
    relevance: float
    authority: float
    extra_value: float
    static_score: float
    confidence: float
    config_version: str
    profile_version: str
    taxonomy_version: str
    input_hash: str
    breakdown_json: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "paper_id", _require_text(self.paper_id, "static_score_snapshot.paper_id")
        )
        if isinstance(self.score_date, datetime):
            object.__setattr__(self, "score_date", self.score_date.date())
        if not isinstance(self.score_date, date):
            raise DomainValidationError("static_score_snapshot.score_date", "必须是 date")
        for name in ("relevance", "authority", "extra_value", "static_score"):
            object.__setattr__(
                self,
                name,
                _require_score(getattr(self, name), f"static_score_snapshot.{name}"),
            )
        object.__setattr__(
            self,
            "confidence",
            _require_ratio(self.confidence, "static_score_snapshot.confidence"),
        )
        for name in ("config_version", "profile_version", "taxonomy_version"):
            object.__setattr__(
                self,
                name,
                _require_text(getattr(self, name), f"static_score_snapshot.{name}"),
            )
        object.__setattr__(
            self, "input_hash", _require_hash(self.input_hash, "static_score_snapshot.input_hash")
        )
        object.__setattr__(
            self,
            "breakdown_json",
            _require_mapping(self.breakdown_json, "static_score_snapshot.breakdown_json"),
        )
        object.__setattr__(
            self, "created_at", _require_aware(self.created_at, "static_score_snapshot.created_at")
        )


@dataclass(frozen=True, slots=True)
class ReviewItem:
    """人工审核项；每篇文献一条，处理低置信度/边界/特殊提名。"""

    paper_id: str
    status: ReviewStatus
    reasons: tuple[str, ...]
    payload_json: Mapping[str, Any]
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    resolution: ReviewResolution | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "paper_id", _require_text(self.paper_id, "review.paper_id"))
        if not isinstance(self.status, ReviewStatus):
            raise DomainValidationError("review.status", "必须是 ReviewStatus")
        if not isinstance(self.reasons, tuple | list) or not self.reasons:
            raise DomainValidationError("review.reasons", "至少需要一个审核原因")
        reasons = tuple(_require_text(item, "review.reasons[]") for item in self.reasons)
        object.__setattr__(self, "reasons", reasons)
        object.__setattr__(
            self, "payload_json", _require_mapping(self.payload_json, "review.payload_json")
        )
        object.__setattr__(self, "created_at", _require_aware(self.created_at, "review.created_at"))
        object.__setattr__(self, "updated_at", _require_aware(self.updated_at, "review.updated_at"))
        if self.updated_at < self.created_at:
            raise DomainValidationError("review.updated_at", "不能早于 created_at")
        resolved = (self.resolved_at, self.resolved_by, self.resolution)
        if self.status is ReviewStatus.OPEN:
            if any(value is not None for value in resolved):
                raise DomainValidationError("review.resolved", "开放项不能有处理结果")
        else:
            if any(value is None for value in resolved):
                raise DomainValidationError("review.resolved", "已处理项必须记录时间/处理人/方式")
            assert self.resolved_at is not None
            object.__setattr__(
                self, "resolved_at", _require_aware(self.resolved_at, "review.resolved_at")
            )
            object.__setattr__(
                self, "resolved_by", _require_text(self.resolved_by, "review.resolved_by")
            )
            if not isinstance(self.resolution, ReviewResolution):
                raise DomainValidationError("review.resolution", "必须是 ReviewResolution")
        object.__setattr__(self, "note", _optional_text(self.note, "review.note"))


@dataclass(frozen=True, slots=True)
class Event:
    """追加写事件；``paper_id`` 为空表示文档/配置级事件。"""

    id: str
    event_type: EventType
    created_at: datetime = field(default_factory=utc_now)
    paper_id: str | None = None
    payload_json: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _require_text(self.id, "event.id"))
        if not isinstance(self.event_type, EventType):
            raise DomainValidationError("event.event_type", "必须是 EventType")
        object.__setattr__(
            self,
            "created_at",
            _require_aware(self.created_at, "event.created_at"),
        )
        object.__setattr__(self, "paper_id", _optional_text(self.paper_id, "event.paper_id"))
        object.__setattr__(
            self,
            "payload_json",
            _require_mapping(self.payload_json, "event.payload_json"),
        )


@dataclass(frozen=True, slots=True)
class Rating:
    """阅读完成后的主观评分与备注；主观分可以为空。"""

    paper_id: str
    score: int | None = None
    note: str | None = None
    tags_at_read: tuple[str, ...] = ()
    read_mode: ReadMode | None = None
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "paper_id", _require_text(self.paper_id, "rating.paper_id"))
        if self.score is not None:
            if isinstance(self.score, bool) or not isinstance(self.score, int):
                raise DomainValidationError("rating.score", "必须是整数或 null")
            if not 1 <= self.score <= 5:
                raise DomainValidationError("rating.score", "必须在 1–5 之间")
        object.__setattr__(self, "note", _optional_text(self.note, "rating.note"))
        object.__setattr__(
            self,
            "tags_at_read",
            _require_text_tuple(self.tags_at_read, "rating.tags_at_read"),
        )
        if self.read_mode is not None and not isinstance(self.read_mode, ReadMode):
            raise DomainValidationError("rating.read_mode", "必须是 ReadMode 或 null")
        object.__setattr__(
            self,
            "created_at",
            _require_aware(self.created_at, "rating.created_at"),
        )


@dataclass(frozen=True, slots=True)
class Config:
    """版本化配置；具体 YAML 校验在 P1-S4 实现。"""

    key: str
    version: str
    value_json: Mapping[str, Any]
    updated_by: AssignedBy = AssignedBy.SYSTEM
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", _require_text(self.key, "config.key"))
        object.__setattr__(self, "version", _require_text(self.version, "config.version"))
        if not isinstance(self.updated_by, AssignedBy):
            raise DomainValidationError("config.updated_by", "必须是 AssignedBy")
        object.__setattr__(self, "updated_at", _require_aware(self.updated_at, "config.updated_at"))
        object.__setattr__(
            self,
            "value_json",
            _require_mapping(self.value_json, "config.value_json"),
        )


@dataclass(frozen=True, slots=True)
class Run:
    """一次任务运行记录；终态必须有结束时间。"""

    id: str
    run_type: str
    status: RunStatus = RunStatus.RUNNING
    started_at: datetime = field(default_factory=utc_now)
    finished_at: datetime | None = None
    stats_json: Mapping[str, Any] = field(default_factory=dict)
    errors_json: Mapping[str, Any] = field(default_factory=dict)
    cost_json: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _require_text(self.id, "run.id"))
        object.__setattr__(self, "run_type", _require_text(self.run_type, "run.run_type"))
        if not isinstance(self.status, RunStatus):
            raise DomainValidationError("run.status", "必须是 RunStatus")
        started = _require_aware(self.started_at, "run.started_at")
        object.__setattr__(self, "started_at", started)
        finished = _optional_aware(self.finished_at, "run.finished_at")
        if self.status is RunStatus.RUNNING:
            if finished is not None:
                raise DomainValidationError("run.finished_at", "运行中不能有结束时间")
        elif finished is None:
            raise DomainValidationError("run.finished_at", "终态运行必须有结束时间")
        elif finished < started:
            raise DomainValidationError("run.finished_at", "不能早于开始时间")
        object.__setattr__(self, "finished_at", finished)
        for name in ("stats_json", "errors_json", "cost_json"):
            object.__setattr__(self, name, _require_mapping(getattr(self, name), f"run.{name}"))


@dataclass(frozen=True, slots=True)
class ArchiveSnapshot:
    """读完时生成的不可变归档快照；修订只能追加新记录。"""

    paper_id: str
    archived_at: datetime
    primary_tag: str
    filing_bucket_id: str
    profile_version: str
    taxonomy_version: str
    config_version: str
    content_hash: str
    secondary_tags: tuple[str, ...] = ()
    rating: int | None = None
    note: str | None = None
    score_date: date | None = None
    priority: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "paper_id", _require_text(self.paper_id, "archive.paper_id"))
        object.__setattr__(
            self,
            "archived_at",
            _require_aware(self.archived_at, "archive.archived_at"),
        )
        object.__setattr__(
            self, "primary_tag", _require_text(self.primary_tag, "archive.primary_tag")
        )
        object.__setattr__(
            self,
            "filing_bucket_id",
            _require_text(self.filing_bucket_id, "archive.filing_bucket_id"),
        )
        object.__setattr__(
            self,
            "secondary_tags",
            _require_text_tuple(self.secondary_tags, "archive.secondary_tags"),
        )
        if len(self.secondary_tags) > 5:
            raise DomainValidationError("archive.secondary_tags", "最多 5 个副标签")
        for name in ("profile_version", "taxonomy_version", "config_version"):
            object.__setattr__(self, name, _require_text(getattr(self, name), f"archive.{name}"))
        object.__setattr__(
            self, "content_hash", _require_hash(self.content_hash, "archive.content_hash")
        )
        if self.rating is not None:
            if isinstance(self.rating, bool) or not isinstance(self.rating, int):
                raise DomainValidationError("archive.rating", "必须是整数或 null")
            if not 1 <= self.rating <= 5:
                raise DomainValidationError("archive.rating", "必须在 1–5 之间")
        object.__setattr__(self, "note", _optional_text(self.note, "archive.note"))
        if self.score_date is not None:
            if isinstance(self.score_date, datetime):
                object.__setattr__(self, "score_date", self.score_date.date())
            if not isinstance(self.score_date, date):
                raise DomainValidationError("archive.score_date", "必须是 date 或 null")
        if self.priority is not None:
            object.__setattr__(self, "priority", _require_score(self.priority, "archive.priority"))


__all__ = [
    "ArchiveSnapshot",
    "Bucket",
    "Config",
    "Enrichment",
    "EnrichmentSignals",
    "Event",
    "Paper",
    "PaperBucket",
    "PaperTag",
    "Profile",
    "Provenance",
    "Rating",
    "ResearchProfile",
    "ReviewItem",
    "Run",
    "ScoreSnapshot",
    "StaticScoreSnapshot",
    "Summary",
    "Tag",
    "Taxonomy",
    "utc_now",
]
