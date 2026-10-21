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

SchemaName = Literal["classification", "summary", "scoring", "special_scoring", "event"]
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
    name: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)


class SpecialCandidateOutput(_OutputModel):
    bucket: SpecialBucketKind
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1)


class ClassificationOutput(_OutputModel):
    primary_tag: str = Field(pattern=_CODE_PATTERN)
    primary_confidence: float = Field(ge=0.0, le=1.0)
    secondary_tags: list[SecondaryTagOutput] = Field(default_factory=list, max_length=5)
    special_candidates: list[SpecialCandidateOutput] = Field(default_factory=list, max_length=3)
    boundary_note: str | None = None

    @model_validator(mode="after")
    def _unique_secondary_tags(self) -> ClassificationOutput:
        keys = [(tag.facet, tag.name.casefold()) for tag in self.secondary_tags]
        if len(keys) != len(set(keys)):
            raise ValueError("secondary_tags 的 facet+name 不能重复")
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


SCHEMA_MODELS: MappingProxyType[SchemaName, type[BaseModel]] = MappingProxyType(
    {
        "classification": ClassificationOutput,
        "summary": SummaryOutput,
        "scoring": ScoreSnapshotOutput,
        "special_scoring": SpecialScoreSnapshotOutput,
        "event": EventEnvelope,
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
    "ClassificationOutput",
    "DynamicScoreOutput",
    "EventEnvelope",
    "EvidenceItem",
    "PriorityScoreOutput",
    "QuasiScoreOutput",
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
