"""Agent 输出 JSON schema：分类、摘要、评分快照、事件。

模型使用 ``extra="forbid"``，避免 LLM 输出夹带未知字段进入业务表。Pydantic 的
``model_json_schema()`` 即对外 JSON Schema；S5 的 FakeLLM/修复重试直接复用
``validate_mapping`` / ``validate_json_text``。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date, datetime
from types import MappingProxyType
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from papersteward.core.enums import EventType, ScoreTier, SpecialBucketKind
from papersteward.core.errors import DomainValidationError

SchemaName = Literal[
    "classification",
    "relevance",
    "summary",
    "scoring",
    "special_scoring",
    "special_nomination",
    "event",
    "profile_extraction",
]
_CODE_PATTERN = r"^[A-Z][A-Z0-9_-]{1,31}$"


class SchemaError(DomainValidationError):
    """schema 校验失败；字段路径可直接定位到 payload。"""

    def __init__(self, message: str, *, field: str = "schema", source: str | None = None) -> None:
        super().__init__(field, message)
        self.source = source

    def __str__(self) -> str:
        prefix = f"{self.source}: " if self.source else ""
        return f"{prefix}{self.field}: {self.message}"


class _OutputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SecondaryTagOutput(_OutputModel):
    facet: Literal["method", "task", "data", "theory", "application", "evaluation"]
    name: str = Field(min_length=1, max_length=64)
    confidence: float = Field(ge=0.0, le=1.0)


class SpecialCandidateOutput(_OutputModel):
    bucket: SpecialBucketKind
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1)


class BridgeMemoOutput(_OutputModel):
    source_mechanism: str = Field(min_length=1, max_length=1000)
    shared_structure: str = Field(min_length=1, max_length=1000)
    transfer_experiment: str = Field(min_length=1, max_length=1000)
    falsification_signal: str = Field(min_length=1, max_length=1000)


class SpecialEvidenceOutput(_OutputModel):
    quote: str = Field(min_length=1, max_length=500)


class SpecialNominationOutput(_OutputModel):
    bucket: SpecialBucketKind
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1, max_length=2000)
    evidence: list[SpecialEvidenceOutput] = Field(min_length=1, max_length=3)
    review_layers: list[
        Literal["problem", "theory", "method", "system", "evaluation", "application"]
    ] = Field(default_factory=list, max_length=6)
    classic_basis: list[Literal["foundational", "citation", "standard", "award", "expert"]] = Field(
        default_factory=list, max_length=5
    )
    bridge_memo: BridgeMemoOutput | None = None

    @model_validator(mode="after")
    def _bucket_specific_requirements(self) -> SpecialNominationOutput:
        if self.bucket is SpecialBucketKind.REVIEW and len(set(self.review_layers)) < 2:
            raise ValueError("review 提名必须覆盖至少两个层级")
        if self.bucket is SpecialBucketKind.CLASSIC and not self.classic_basis:
            raise ValueError("classic 提名必须提供至少一个经典依据")
        if self.bucket is SpecialBucketKind.DISTANT and self.bridge_memo is None:
            raise ValueError("distant 提名必须提供 bridge_memo")
        return self


class SpecialNominationsOutput(_OutputModel):
    nominations: list[SpecialNominationOutput] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def _unique_buckets(self) -> SpecialNominationsOutput:
        buckets = [item.bucket for item in self.nominations]
        if len(buckets) != len(set(buckets)):
            raise ValueError("同一个特殊桶只能提名一次")
        return self


class ClassificationEvidenceOutput(_OutputModel):
    quote: str = Field(min_length=1, max_length=500)


class ClassificationOutput(_OutputModel):
    primary_tag: str = Field(pattern=_CODE_PATTERN)
    primary_confidence: float = Field(ge=0.0, le=1.0)
    runner_up_tag: str | None = Field(default=None, pattern=_CODE_PATTERN)
    runner_up_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    secondary_tags: list[SecondaryTagOutput] = Field(default_factory=list, max_length=5)
    special_candidates: list[SpecialCandidateOutput] = Field(default_factory=list, max_length=3)
    evidence: list[ClassificationEvidenceOutput] = Field(default_factory=list, max_length=5)
    boundary_note: str | None = None

    @model_validator(mode="after")
    def _unique_secondary_tags(self) -> ClassificationOutput:
        keys = [(tag.facet, tag.name.casefold()) for tag in self.secondary_tags]
        if len(keys) != len(set(keys)):
            raise ValueError("secondary_tags 的 facet+name 不能重复")
        if (self.runner_up_tag is None) != (self.runner_up_confidence is None):
            raise ValueError("runner_up_tag 与 runner_up_confidence 必须同时提供")
        if self.runner_up_tag is not None and self.runner_up_tag == self.primary_tag:
            raise ValueError("runner_up_tag 不能等于 primary_tag")
        return self


class EvidenceItem(_OutputModel):
    field: Literal["what", "how", "finding", "limitation"]
    quote: str = Field(min_length=1)


class SummaryOutput(_OutputModel):
    summary_100: str = Field(min_length=1)
    what: str = Field(min_length=1)
    how: str = Field(min_length=1)
    finding: str = Field(min_length=1)
    limitation: str = Field(min_length=1)
    evidence: list[EvidenceItem] = Field(min_length=1)

    @model_validator(mode="after")
    def _length_limit(self) -> SummaryOutput:
        count = sum(1 for char in self.summary_100 if not char.isspace())
        if count > 100:
            raise ValueError(f"summary_100 不能超过 100 个非空白字符，实际 {count}")
        return self


class RelevanceEvidenceOutput(_OutputModel):
    dimension: Literal["problem", "method", "domain", "actionability"]
    quote: str = Field(min_length=1, max_length=500)


class RelevanceOutput(_OutputModel):
    problem: int = Field(ge=0, le=5)
    method: int = Field(ge=0, le=5)
    domain: int = Field(ge=0, le=5)
    actionability: int = Field(ge=0, le=5)
    evidence: list[RelevanceEvidenceOutput] = Field(min_length=1, max_length=12)
    confidence: float = Field(ge=0.0, le=1.0)
    boundary_note: str | None = None


class StaticScoreOutput(_OutputModel):
    R: float = Field(ge=0.0, le=100.0)
    A: float = Field(ge=0.0, le=100.0)
    E: float = Field(ge=0.0, le=100.0)
    S: float = Field(ge=0.0, le=100.0)


class DynamicScoreOutput(_OutputModel):
    F: float = Field(ge=0.0, le=100.0)
    H: float = Field(ge=0.0, le=100.0)
    D: float = Field(ge=0.0, le=100.0)


class QuasiScoreOutput(_OutputModel):
    U: float = Field(ge=0.0, le=100.0)
    confidence: float = Field(ge=0.0, le=1.0)


class PriorityScoreOutput(_OutputModel):
    P: float = Field(ge=0.0, le=100.0)
    rank_score: float = Field(ge=0.0, le=100.0)


class ScoreSnapshotOutput(_OutputModel):
    paper_id: str = Field(min_length=1)
    date: date
    profile_version: str = Field(min_length=1)
    taxonomy_version: str = Field(min_length=1)
    config_version: str = Field(min_length=1)
    static: StaticScoreOutput
    dynamic: DynamicScoreOutput
    quasi: QuasiScoreOutput
    priority: PriorityScoreOutput
    explanation: list[str] = Field(min_length=1)


class SpecialScoreSnapshotOutput(_OutputModel):
    paper_id: str = Field(min_length=1)
    date: date
    profile_version: str = Field(min_length=1)
    taxonomy_version: str = Field(min_length=1)
    config_version: str = Field(min_length=1)
    static: StaticScoreOutput
    dynamic: DynamicScoreOutput
    quasi: QuasiScoreOutput
    tier: ScoreTier
    explanation: list[str] = Field(min_length=1)


class EventEnvelope(_OutputModel):
    type: EventType
    paper_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _aware_datetime(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at 必须带时区")
        return value

    @model_validator(mode="after")
    def _paper_events_need_paper_id(self) -> EventEnvelope:
        if self.type.value.startswith("paper.") and not self.paper_id:
            raise ValueError("paper.* 事件必须提供 paper_id")
        return self


class ExtractionPositioning(_OutputModel):
    summary: str = Field(min_length=1, max_length=500)
    domain: str = Field(min_length=1, max_length=200)
    career_stage: str = Field(default="", max_length=64)
    language: str = Field(default="zh", max_length=16)


class ExtractionCoreQuestion(_OutputModel):
    id: str = Field(pattern=r"^Q[0-9]{1,3}$")
    text: str = Field(min_length=1, max_length=500)


class ExtractionSubdirection(_OutputModel):
    id: str = Field(pattern=r"^S[0-9]{1,3}$")
    name: str = Field(min_length=1, max_length=128)
    priority: Literal["high", "medium", "low"] = "medium"
    target_share: float = Field(ge=0.0, le=1.0)
    keywords: list[str] = Field(min_length=1, max_length=10)
    description: str | None = Field(default=None, max_length=1000)


class ExtractionReading(_OutputModel):
    weekly_capacity: int = Field(ge=1, le=200)
    deep_skim_ratio: list[float] = Field(min_length=2, max_length=2)
    reading_goals: list[str] = Field(min_length=1, max_length=5)
    push_time: str | None = Field(default=None, pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")

    @field_validator("deep_skim_ratio")
    @classmethod
    def _ratio_range(cls, value: list[float]) -> list[float]:
        if any(ratio < 0.0 or ratio > 1.0 for ratio in value):
            raise ValueError("deep_skim_ratio 的每一项必须在 0–1 之间")
        return value


class ExtractionAuthority(_OutputModel):
    venues: list[str] = Field(default_factory=list, max_length=20)
    authors: list[str] = Field(default_factory=list, max_length=20)
    teams: list[str] = Field(default_factory=list, max_length=20)


class ExtractionProfile(_OutputModel):
    positioning: ExtractionPositioning
    core_questions: list[ExtractionCoreQuestion] = Field(min_length=3, max_length=5)
    subdirections: list[ExtractionSubdirection] = Field(min_length=1, max_length=8)
    include: list[str] = Field(min_length=1, max_length=10)
    exclude: list[str] = Field(min_length=1, max_length=10)
    methods: list[str] = Field(default_factory=list, max_length=10)
    data: list[str] = Field(default_factory=list, max_length=10)
    authority: ExtractionAuthority | None = None
    negative_preferences: list[str] = Field(default_factory=list, max_length=10)
    reading: ExtractionReading

    @model_validator(mode="after")
    def _unique_ids(self) -> ExtractionProfile:
        question_ids = [question.id for question in self.core_questions]
        if len(question_ids) != len(set(question_ids)):
            raise ValueError("core_questions 的 id 不能重复")
        sub_ids = [item.id for item in self.subdirections]
        if len(sub_ids) != len(set(sub_ids)):
            raise ValueError("subdirections 的 id 不能重复")
        return self


class ExtractionTag(_OutputModel):
    code: str = Field(pattern=_CODE_PATTERN)
    name: str = Field(min_length=1, max_length=64)
    definition: str = Field(min_length=1, max_length=1000)
    include: list[str] = Field(min_length=1, max_length=5)
    exclude: list[str] = Field(min_length=1, max_length=5)
    examples: list[str] = Field(min_length=3, max_length=8)
    aliases: list[str] = Field(default_factory=list, max_length=10)
    boundaries: list[str] = Field(default_factory=list, max_length=4)


class ProfileExtractionOutput(_OutputModel):
    """从用户材料抽取的研究方向草案：profile front matter + 分类标签。"""

    profile: ExtractionProfile
    tags: list[ExtractionTag] = Field(min_length=5, max_length=9)

    @model_validator(mode="after")
    def _coherent(self) -> ProfileExtractionOutput:
        codes = [tag.code for tag in self.tags]
        if len(codes) != len(set(codes)):
            raise ValueError("标签代码不能重复")
        total = sum(item.target_share for item in self.profile.subdirections)
        if abs(total - 1.0) > 1e-6:
            raise ValueError("subdirections.target_share 之和必须为 1.0")
        if abs(sum(self.profile.reading.deep_skim_ratio) - 1.0) > 1e-6:
            raise ValueError("reading.deep_skim_ratio 之和必须为 1.0")
        return self


SCHEMA_MODELS: MappingProxyType[SchemaName, type[BaseModel]] = MappingProxyType(
    {
        "classification": ClassificationOutput,
        "relevance": RelevanceOutput,
        "summary": SummaryOutput,
        "scoring": ScoreSnapshotOutput,
        "special_scoring": SpecialScoreSnapshotOutput,
        "special_nomination": SpecialNominationsOutput,
        "event": EventEnvelope,
        "profile_extraction": ProfileExtractionOutput,
    }
)


def _model_for(name: str) -> type[BaseModel]:
    key = cast(SchemaName, name)
    model = SCHEMA_MODELS.get(key)
    if model is None:
        names = ", ".join(sorted(SCHEMA_MODELS))
        raise SchemaError(f"未知 schema：{name}；可选 {names}", field="schema")
    return model


def schema_for(name: str) -> dict[str, Any]:
    """返回指定模型的 JSON Schema。"""
    return _model_for(name).model_json_schema()


def validate_mapping(
    name: str, payload: Mapping[str, Any], *, source: str | None = None
) -> BaseModel:
    """校验 JSON 对象；错误字段形如 ``schema.summary.evidence.0.quote``。"""
    model = _model_for(name)
    try:
        return model.model_validate(dict(payload))
    except ValidationError as exc:
        error = exc.errors()[0]
        location = ".".join(str(part) for part in error.get("loc", ()) if str(part))
        field = f"schema.{name}" + (f".{location}" if location else "")
        message = str(error.get("msg", "校验失败"))
        if message.startswith("Value error, "):
            message = message.removeprefix("Value error, ")
        raise SchemaError(message, field=field, source=source) from exc


def validate_json_text(name: str, text: str, *, source: str | None = None) -> BaseModel:
    """解析 JSON 文本并校验；语法错误与字段错误都抛 ``SchemaError``。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SchemaError(
            f"JSON 解析失败：{exc.msg}（line {exc.lineno}, column {exc.colno}）",
            field=f"schema.{name}",
            source=source,
        ) from exc
    if not isinstance(data, dict):
        raise SchemaError("根节点必须是 JSON object", field=f"schema.{name}", source=source)
    return validate_mapping(name, data, source=source)


__all__ = [
    "ClassificationEvidenceOutput",
    "ClassificationOutput",
    "DynamicScoreOutput",
    "EventEnvelope",
    "EvidenceItem",
    "ExtractionAuthority",
    "ExtractionCoreQuestion",
    "ExtractionPositioning",
    "ExtractionProfile",
    "ExtractionReading",
    "ExtractionSubdirection",
    "ExtractionTag",
    "PriorityScoreOutput",
    "ProfileExtractionOutput",
    "QuasiScoreOutput",
    "RelevanceEvidenceOutput",
    "SpecialEvidenceOutput",
    "SpecialNominationOutput",
    "SpecialNominationsOutput",
    "BridgeMemoOutput",
    "RelevanceOutput",
    "SCHEMA_MODELS",
    "SchemaError",
    "SchemaName",
    "ScoreSnapshotOutput",
    "SecondaryTagOutput",
    "SpecialCandidateOutput",
    "SpecialScoreSnapshotOutput",
    "StaticScoreOutput",
    "SummaryOutput",
    "schema_for",
    "validate_json_text",
    "validate_mapping",
]
