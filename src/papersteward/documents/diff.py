"""基于领域实体的可读 diff。

diff 不比较原始 Markdown（排版噪声太多），而是比较解析后的结构：

- dict 逐键比较；
- 带唯一 ``id`` / ``code`` 的对象列表按标识比较；
- 标量列表按多重集合比较，忽略顺序变化；
- 输出稳定的 ``+ / - / ~`` 行，供 CLI 与测试复用。
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from papersteward.core.entities import ResearchProfile, Taxonomy

Change = Literal["add", "remove", "change"]


@dataclass(frozen=True, slots=True)
class DiffEntry:
    path: str
    change: Change
    old: Any = None
    new: Any = None


def _format_path(parent: str, key: str | int) -> str:
    if isinstance(key, int):
        return f"{parent}[{key}]" if parent else f"[{key}]"
    return f"{parent}.{key}" if parent else key


def _keyed(items: list[Any], key: str) -> bool:
    if not items:
        return False
    if not all(isinstance(item, Mapping) for item in items):
        return False
    values = [item.get(key) for item in items]
    if any(value is None for value in values):
        return False
    return len({str(value) for value in values}) == len(values)


def _list_key(left: list[Any], right: list[Any]) -> str | None:
    candidates = left or right
    for key in ("id", "code"):
        if (
            _keyed(candidates, key)
            and (not left or _keyed(left, key))
            and (not right or _keyed(right, key))
        ):
            return key
    return None


def _value_key(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _diff_lists(path: str, left: list[Any], right: list[Any], out: list[DiffEntry]) -> None:
    key = _list_key(left, right)
    if key is not None:
        left_map = {str(item[key]): item for item in left}
        right_map = {str(item[key]): item for item in right}
        for identifier in sorted(set(left_map) | set(right_map)):
            child = _format_path(path, identifier)
            if identifier not in left_map:
                out.append(DiffEntry(child, "add", new=right_map[identifier]))
            elif identifier not in right_map:
                out.append(DiffEntry(child, "remove", old=left_map[identifier]))
            else:
                _diff(child, left_map[identifier], right_map[identifier], out)
        return

    if all(not isinstance(item, Mapping | list) for item in left + right):
        left_counter = Counter(_value_key(item) for item in left)
        right_counter = Counter(_value_key(item) for item in right)
        removed = left_counter - right_counter
        added = right_counter - left_counter
        for value in sorted(removed.elements()):
            out.append(DiffEntry(path, "remove", old=json.loads(value)))
        for value in sorted(added.elements()):
            out.append(DiffEntry(path, "add", new=json.loads(value)))
        return

    for index in range(max(len(left), len(right))):
        child = _format_path(path, index)
        if index >= len(left):
            out.append(DiffEntry(child, "add", new=right[index]))
        elif index >= len(right):
            out.append(DiffEntry(child, "remove", old=left[index]))
        else:
            _diff(child, left[index], right[index], out)


def _diff(path: str, left: Any, right: Any, out: list[DiffEntry]) -> None:
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        for key in sorted(set(left) | set(right), key=str):
            child = _format_path(path, str(key))
            if key not in left:
                out.append(DiffEntry(child, "add", new=right[key]))
            elif key not in right:
                out.append(DiffEntry(child, "remove", old=left[key]))
            else:
                _diff(child, left[key], right[key], out)
        return
    if isinstance(left, list) and isinstance(right, list):
        _diff_lists(path, left, right, out)
        return
    if left != right:
        out.append(DiffEntry(path, "change", old=left, new=right))


def diff_values(left: Mapping[str, Any], right: Mapping[str, Any]) -> list[DiffEntry]:
    """比较两份结构化文档，返回按路径排序的条目。"""
    entries: list[DiffEntry] = []
    _diff("", dict(left), dict(right), entries)
    return sorted(entries, key=lambda entry: (entry.path, entry.change))


def profile_diff_payload(profile: ResearchProfile) -> dict[str, Any]:
    return dict(profile.parsed_json)


def taxonomy_diff_payload(taxonomy: Taxonomy) -> dict[str, Any]:
    return {
        "id": taxonomy.id,
        "version": taxonomy.version,
        "tags": {
            tag.code: {
                "name": tag.name,
                "level": tag.level.value,
                "definition": tag.definition,
                "parent_code": tag.parent_code,
                "include": list(tag.include),
                "exclude": list(tag.exclude),
                "examples": list(tag.examples),
                "aliases": list(tag.aliases),
                "active": tag.active,
            }
            for tag in taxonomy.tags
        },
    }


def diff_profiles(left: ResearchProfile, right: ResearchProfile) -> list[DiffEntry]:
    return diff_values(profile_diff_payload(left), profile_diff_payload(right))


def diff_taxonomies(left: Taxonomy, right: Taxonomy) -> list[DiffEntry]:
    return diff_values(taxonomy_diff_payload(left), taxonomy_diff_payload(right))


def _format_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def render_diff(entries: list[DiffEntry]) -> str:
    """渲染为稳定、可读的文本；无差异时返回“无差异”。"""
    if not entries:
        return "无差异"
    lines: list[str] = []
    for entry in entries:
        if entry.change == "add":
            lines.append(f"+ {entry.path}: {_format_value(entry.new)}")
        elif entry.change == "remove":
            lines.append(f"- {entry.path}: {_format_value(entry.old)}")
        else:
            lines.append(
                f"~ {entry.path}: {_format_value(entry.old)} -> {_format_value(entry.new)}"
            )
    return "\n".join(lines)


__all__ = [
    "DiffEntry",
    "diff_profiles",
    "diff_taxonomies",
    "diff_values",
    "profile_diff_payload",
    "render_diff",
    "taxonomy_diff_payload",
]
