"""分类法（taxonomy）Markdown 解析。

格式：YAML front matter 提供版本与可选副标签；Markdown 正文用
``## T01 名称`` 分节，每节包含定义、纳入、排除、正例等字段。正文保持为
``Taxonomy.content_md``，机器可读标签写入 ``Taxonomy.tags``。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from papersteward.core.entities import Tag, Taxonomy
from papersteward.core.enums import DocumentStatus, TagLevel
from papersteward.core.errors import DomainValidationError, InvariantViolation
from papersteward.core.invariants import ensure_taxonomy_shape
from papersteward.documents.frontmatter import (
    DocumentError,
    FrontMatter,
    document_error_from_validation,
    field_line,
    load_yaml_mapping,
    split_front_matter,
)

_CODE_PATTERN = r"^[A-Z][A-Z0-9_-]{1,31}$"
_SECTION_RE = re.compile(r"^##\s+(?P<code>[A-Z][A-Z0-9_-]{1,31})\s+(?P<name>.+?)\s*$")
_HEADING_RE = re.compile(r"^##\s+(?P<rest>.+?)\s*$")
_CODE_PREFIX_RE = re.compile(r"^(?P<code>[A-Z][A-Z0-9_-]{1,31})(?:\s|$)")
_FIELD_RE = re.compile(
    r"^\s*[-*]\s*(?P<field>定义|纳入|排除|正例|边界|常见误分|别名|同义词)\s*[：:]\s*(?P<value>.*?)\s*$"
)


class _DocumentModel(BaseModel):
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)


class SecondaryTag(_DocumentModel):
    code: str = Field(pattern=_CODE_PATTERN)
    name: str = Field(min_length=1)
    parent_code: str | None = Field(default=None, pattern=_CODE_PATTERN)
    definition: str = ""
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    active: bool = True


class TaxonomyFrontMatter(_DocumentModel):
    version: str
    updated: date | None = None
    taxonomy_id: str | None = None
    secondary_tags: list[SecondaryTag] = Field(default_factory=list)

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


@dataclass
class _Section:
    code: str
    name: str
    line: int
    lines: list[tuple[int, str]] = field(default_factory=list)


def _document_id(source: str, explicit: str | None) -> str:
    if explicit:
        return explicit
    if source not in {"", "<memory>", "<string>"}:
        stem = Path(source).stem.strip()
        if stem:
            return stem
    return "taxonomy"


def _split_items(value: str) -> list[str]:
    parts = re.split(r"[；;]", value)
    return [part.strip() for part in parts if part.strip()]


def _split_examples(value: str) -> list[str]:
    parts = re.split(r"[、,，；;]", value)
    cleaned = (part.strip().strip("[]【】").strip() for part in parts)
    return [part for part in cleaned if part]


def _collect_sections(front: FrontMatter, *, source: str) -> list[_Section]:
    sections: list[_Section] = []
    current: _Section | None = None
    for offset, line in enumerate(front.body.splitlines()):
        line_number = front.body_start_line + offset
        match = _SECTION_RE.match(line)
        if match is not None:
            current = _Section(
                code=match.group("code"),
                name=match.group("name").strip(),
                line=line_number,
            )
            sections.append(current)
            continue
        if line.startswith("## "):
            current = None
            heading = _HEADING_RE.match(line)
            rest = heading.group("rest") if heading is not None else ""
            if _CODE_PREFIX_RE.match(rest):
                raise DocumentError(
                    "主标签标题必须为 `## Txx 名称`（缺少名称）",
                    source=source,
                    field="taxonomy.heading",
                    line=line_number,
                )
            continue
        if current is not None:
            current.lines.append((line_number, line))
    if not sections:
        raise DocumentError(
            "分类法正文必须包含至少一个 `## Txx 名称` 主标签小节",
            source=source,
            field="taxonomy.tags",
            line=front.body_start_line,
        )
    return sections


def _parse_section(section: _Section, *, source: str) -> Tag:
    definitions: list[str] = []
    include: list[str] = []
    exclude: list[str] = []
    examples: list[str] = []
    aliases: list[str] = []
    for _line_number, line in section.lines:
        match = _FIELD_RE.match(line)
        if match is None:
            continue
        field_name = match.group("field")
        value = match.group("value")
        if field_name == "定义":
            definitions.append(value)
        elif field_name == "纳入":
            include.extend(_split_items(value))
        elif field_name == "排除":
            exclude.extend(_split_items(value))
        elif field_name == "正例":
            examples.extend(_split_examples(value))
        elif field_name in {"别名", "同义词"}:
            aliases.extend(_split_examples(value))
        # 边界/常见误分保留在 Markdown 正文中，不进入 Tag 机器字段。

    definition = "；".join(part for part in definitions if part).strip()
    if not definition:
        raise DocumentError(
            f"主标签 {section.code} 缺少定义",
            source=source,
            field=f"taxonomy.{section.code}.definition",
            line=section.line,
        )
    if not include:
        raise DocumentError(
            f"主标签 {section.code} 缺少纳入条件",
            source=source,
            field=f"taxonomy.{section.code}.include",
            line=section.line,
        )
    if not exclude:
        raise DocumentError(
            f"主标签 {section.code} 缺少排除条件",
            source=source,
            field=f"taxonomy.{section.code}.exclude",
            line=section.line,
        )
    if not examples:
        raise DocumentError(
            f"主标签 {section.code} 缺少正例",
            source=source,
            field=f"taxonomy.{section.code}.examples",
            line=section.line,
        )
    try:
        return Tag(
            code=section.code,
            name=section.name,
            level=TagLevel.PRIMARY,
            definition=definition,
            include=tuple(include),
            exclude=tuple(exclude),
            examples=tuple(examples),
            aliases=tuple(aliases),
            active=True,
        )
    except DomainValidationError as exc:
        raise DocumentError(
            exc.message,
            source=source,
            field=f"taxonomy.{section.code}",
            line=section.line,
        ) from exc


def _build_secondary_tags(
    metadata: TaxonomyFrontMatter, primary_codes: set[str], *, source: str, front: FrontMatter
) -> list[Tag]:
    tags: list[Tag] = []
    seen: set[str] = set()
    for item in metadata.secondary_tags:
        if item.code in seen or item.code in primary_codes:
            raise DocumentError(
                f"标签代码重复：{item.code}",
                source=source,
                field="front_matter.secondary_tags.code",
                line=field_line(front, "secondary_tags"),
            )
        seen.add(item.code)
        if item.parent_code is not None and item.parent_code not in primary_codes:
            raise DocumentError(
                f"副标签 {item.code} 的 parent_code 不存在：{item.parent_code}",
                source=source,
                field="front_matter.secondary_tags.parent_code",
                line=field_line(front, "secondary_tags"),
            )
        try:
            tags.append(
                Tag(
                    code=item.code,
                    name=item.name,
                    level=TagLevel.SECONDARY,
                    definition=item.definition,
                    parent_code=item.parent_code,
                    include=tuple(item.include),
                    exclude=tuple(item.exclude),
                    examples=tuple(item.examples),
                    aliases=tuple(item.aliases),
                    active=item.active,
                )
            )
        except DomainValidationError as exc:
            raise DocumentError(
                exc.message,
                source=source,
                field="front_matter.secondary_tags",
                line=field_line(front, "secondary_tags"),
            ) from exc
    return tags


def parse_taxonomy(
    text: str,
    *,
    source: str = "<memory>",
    document_id: str | None = None,
) -> Taxonomy:
    """解析并校验 taxonomy；失败抛出 ``DocumentError``，不接触数据库。"""
    front = split_front_matter(text, source=source)
    data = load_yaml_mapping(front, source=source)
    try:
        metadata = TaxonomyFrontMatter.model_validate(data)
    except ValidationError as exc:
        raise document_error_from_validation(exc, front, source=source) from exc

    sections = _collect_sections(front, source=source)
    primary_tags = [_parse_section(section, source=source) for section in sections]
    codes = [tag.code for tag in primary_tags]
    if len(codes) != len(set(codes)):
        raise DocumentError(
            "主标签代码不能重复",
            source=source,
            field="taxonomy.tags",
            line=sections[0].line,
        )
    secondary_tags = _build_secondary_tags(metadata, set(codes), source=source, front=front)
    try:
        taxonomy = Taxonomy(
            id=_document_id(source, document_id or metadata.taxonomy_id),
            version=metadata.version,
            status=DocumentStatus.DRAFT,
            content_md=text,
            tags=tuple(primary_tags + secondary_tags),
            confirmed_at=None,
        )
    except DomainValidationError as exc:
        raise DocumentError(
            exc.message, source=source, field="taxonomy", line=front.body_start_line
        ) from exc
    try:
        ensure_taxonomy_shape(taxonomy)
    except InvariantViolation as exc:
        raise DocumentError(
            exc.message,
            source=source,
            field="taxonomy.tags",
            line=sections[0].line,
        ) from exc
    return taxonomy


__all__ = [
    "SecondaryTag",
    "TaxonomyFrontMatter",
    "parse_taxonomy",
]
