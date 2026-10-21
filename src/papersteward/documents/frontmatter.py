"""YAML front matter 切分与错误定位。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, cast

import yaml
from pydantic import ValidationError

from papersteward.core.errors import DomainValidationError

_FRONT_MATTER_DELIMITER = "---"
_KEY_PATTERN = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_-]*)\s*:")


class DocumentError(DomainValidationError):
    """文档解析/校验错误；带源文件、行号与字段路径。"""

    def __init__(
        self,
        message: str,
        *,
        source: str,
        field: str = "document",
        line: int | None = None,
    ) -> None:
        super().__init__(field, message)
        self.source = source
        self.line = line

    def __str__(self) -> str:
        location = f"{self.source}:{self.line}" if self.line is not None else self.source
        return f"{location}: {self.field}: {self.message}"


@dataclass(frozen=True, slots=True)
class FrontMatter:
    """切分后的 front matter 与正文。"""

    yaml_text: str
    body: str
    yaml_start_line: int
    body_start_line: int


def split_front_matter(text: str, *, source: str) -> FrontMatter:
    """要求文档以 ``---`` 开头，并以下一行 ``---`` 结束 front matter。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FRONT_MATTER_DELIMITER:
        raise DocumentError(
            "文档必须以 YAML front matter 开始（第一行 ---）",
            source=source,
            field="document",
            line=1,
        )
    closing: int | None = None
    for index in range(1, len(lines)):
        if lines[index].strip() == _FRONT_MATTER_DELIMITER:
            closing = index
            break
    if closing is None:
        raise DocumentError(
            "front matter 缺少结束分隔符 ---",
            source=source,
            field="document",
            line=1,
        )
    body = "\n".join(lines[closing + 1 :])
    if not body.strip():
        raise DocumentError(
            "front matter 之后必须有 Markdown 正文",
            source=source,
            field="document",
            line=closing + 2,
        )
    return FrontMatter(
        yaml_text="\n".join(lines[1:closing]),
        body=body,
        yaml_start_line=2,
        body_start_line=closing + 2,
    )


def load_yaml_mapping(front: FrontMatter, *, source: str) -> dict[str, Any]:
    """用 ``safe_load`` 解析 front matter，并要求根节点是 mapping。"""
    if not front.yaml_text.strip():
        raise DocumentError(
            "front matter 不能为空",
            source=source,
            field="front_matter",
            line=front.yaml_start_line,
        )
    try:
        data = yaml.safe_load(front.yaml_text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = front.yaml_start_line + (mark.line if mark is not None else 0)
        problem = getattr(exc, "problem", None) or str(exc)
        raise DocumentError(
            f"YAML 解析失败：{problem}",
            source=source,
            field="front_matter",
            line=line,
        ) from exc
    if not isinstance(data, dict):
        raise DocumentError(
            "front matter 根节点必须是 YAML mapping",
            source=source,
            field="front_matter",
            line=front.yaml_start_line,
        )
    return cast(dict[str, Any], data)


def field_line(front: FrontMatter, field_name: str) -> int | None:
    """在 front matter 中查找顶层字段的行号，用于错误提示。"""
    for offset, line in enumerate(front.yaml_text.splitlines()):
        match = _KEY_PATTERN.match(line)
        if match and match.group("key") == field_name:
            return front.yaml_start_line + offset
    return None


def document_error_from_validation(
    exc: ValidationError, front: FrontMatter, *, source: str
) -> DocumentError:
    """把 Pydantic 的第一个错误转换成本项目的字段级错误。"""
    error = exc.errors()[0]
    location = tuple(str(part) for part in error.get("loc", ()) if str(part))
    field = "front_matter" + (f".{'.'.join(location)}" if location else "")
    line = field_line(front, location[0]) if location else front.yaml_start_line
    message = str(error.get("msg", "校验失败"))
    if message.startswith("Value error, "):
        message = message.removeprefix("Value error, ")
    return DocumentError(message, source=source, field=field, line=line)


__all__ = [
    "DocumentError",
    "FrontMatter",
    "document_error_from_validation",
    "field_line",
    "load_yaml_mapping",
    "split_front_matter",
]
