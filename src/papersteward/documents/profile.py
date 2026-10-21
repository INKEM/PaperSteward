"""研究方向档案（profile）Markdown 解析。

格式：YAML front matter + Markdown 正文。front matter 的机器字段遵循设计方案
§4.3，正文用于人阅读与协商；解析结果写入不可变 ``ResearchProfile``（draft）。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from papersteward.core.entities import ResearchProfile
from papersteward.core.enums import DocumentStatus
from papersteward.core.errors import DomainValidationError
from papersteward.documents.frontmatter import (
    DocumentError,
    FrontMatter,
    document_error_from_validation,
    field_line,
    load_yaml_mapping,
    split_front_matter,
)

_TARGET_SHARE_TOLERANCE = 1e-6


class _DocumentModel(BaseModel):
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)


class Positioning(_DocumentModel):
    summary: str | None = None
    domain: str | None = None
    career_stage: str | None = None
    language: str | None = None


class CoreQuestion(_DocumentModel):
    id: str = Field(min_length=1)
    text: str = Field(min_length=1)


class Subdirection(_DocumentModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    priority: Literal["high", "medium", "low"] = "medium"
    target_share: float = Field(ge=0.0, le=1.0)
    keywords: list[str] = Field(default_factory=list)
    description: str | None = None
    representative_work: list[str] = Field(default_factory=list)


class Reading(_DocumentModel):
    weekly_capacity: int = Field(ge=1)
    deep_skim_ratio: list[float] = Field(min_length=2, max_length=2)
    reading_goals: list[str] = Field(default_factory=list)
    push_time: str | None = None

    @field_validator("deep_skim_ratio")
    @classmethod
    def _ratio_range(cls, value: list[float]) -> list[float]:
        if any(ratio < 0.0 or ratio > 1.0 for ratio in value):
            raise ValueError("deep_skim_ratio 的每一项必须在 0–1 之间")
        return value


class ProfileFrontMatter(_DocumentModel):
    """profile front matter 的结构化契约。未知字段保留在 parsed_json。"""

    version: str
    updated: date | None = None
    profile_id: str | None = None
    positioning: Positioning | None = None
    core_questions: list[CoreQuestion] = Field(min_length=3, max_length=5)
    subdirections: list[Subdirection] = Field(min_length=1)
    methods: Any = None
    data: Any = None
    include: list[str] = Field(min_length=1)
    exclude: list[str] = Field(default_factory=list)
    reading: Reading
    authority: Any = None
    negative_preferences: list[str] = Field(default_factory=list)
    change_log: list[Any] = Field(default_factory=list)

    @field_validator("version", mode="before")
    @classmethod
    def _normalize_version(cls, value: object) -> str:
        if isinstance(value, bool) or value is None:
            raise ValueError("version 必须是字符串或数字")
        if isinstance(value, int | float):
            text = str(value)
        elif isinstance(value, str):
            text = value.strip()
        else:
            raise ValueError("version 必须是字符串或数字")
        if not text:
            raise ValueError("version 不能为空")
        return text


def _document_id(source: str, explicit: str | None) -> str:
    if explicit:
        return explicit
    if source not in {"", "<memory>", "<string>"}:
        stem = Path(source).stem.strip()
        if stem:
            return stem
    return "profile"


def _change_log(values: list[Any]) -> tuple[str, ...]:
    entries: list[str] = []
    for value in values:
        if isinstance(value, str):
            entries.append(value)
        else:
            entries.append(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))
    return tuple(entries)


def _raise_duplicate(values: list[Any], field: str, *, source: str, front: FrontMatter) -> None:
    seen: set[str] = set()
    for item in values:
        identifier = str(getattr(item, "id", ""))
        if identifier in seen:
            raise DocumentError(
                f"{field} 的 id 不能重复：{identifier}",
                source=source,
                field=f"front_matter.{field}",
                line=field_line(front, field),
            )
        seen.add(identifier)


def _validate_semantics(model: ProfileFrontMatter, front: FrontMatter, *, source: str) -> None:
    _raise_duplicate(model.core_questions, "core_questions", source=source, front=front)
    _raise_duplicate(model.subdirections, "subdirections", source=source, front=front)

    total_share = sum(item.target_share for item in model.subdirections)
    if abs(total_share - 1.0) > _TARGET_SHARE_TOLERANCE:
        raise DocumentError(
            f"subdirections.target_share 之和必须为 1.0，实际为 {total_share:.4f}",
            source=source,
            field="front_matter.subdirections.target_share",
            line=field_line(front, "subdirections"),
        )

    ratio_total = sum(model.reading.deep_skim_ratio)
    if abs(ratio_total - 1.0) > _TARGET_SHARE_TOLERANCE:
        raise DocumentError(
            f"reading.deep_skim_ratio 之和必须为 1.0，实际为 {ratio_total:.4f}",
            source=source,
            field="front_matter.reading.deep_skim_ratio",
            line=field_line(front, "reading"),
        )

    if any(not item.strip() for item in model.include):
        raise DocumentError(
            "include 不能包含空条目",
            source=source,
            field="front_matter.include",
            line=field_line(front, "include"),
        )
    if any(not item.strip() for item in model.exclude):
        raise DocumentError(
            "exclude 不能包含空条目",
            source=source,
            field="front_matter.exclude",
            line=field_line(front, "exclude"),
        )


def parse_profile(
    text: str,
    *,
    source: str = "<memory>",
    document_id: str | None = None,
) -> ResearchProfile:
    """解析并校验 profile；失败抛出 ``DocumentError``，不接触数据库。"""
    front = split_front_matter(text, source=source)
    data = load_yaml_mapping(front, source=source)
    try:
        model = ProfileFrontMatter.model_validate(data)
    except ValidationError as exc:
        raise document_error_from_validation(exc, front, source=source) from exc

    _validate_semantics(model, front, source=source)
    parsed = model.model_dump(mode="json")
    try:
        return ResearchProfile(
            id=_document_id(source, document_id or model.profile_id),
            version=model.version,
            status=DocumentStatus.DRAFT,
            content_md=text,
            parsed_json=parsed,
            confirmed_at=None,
            change_log=_change_log(model.change_log),
        )
    except DomainValidationError as exc:
        raise DocumentError(
            exc.message, source=source, field="profile", line=front.body_start_line
        ) from exc


__all__ = [
    "CoreQuestion",
    "Positioning",
    "ProfileFrontMatter",
    "Reading",
    "Subdirection",
    "parse_profile",
]
