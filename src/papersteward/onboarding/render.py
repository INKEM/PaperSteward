"""把抽取结果渲染为可通过解析器校验的 profile / taxonomy Markdown。"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import yaml

from papersteward.schemas.models import ProfileExtractionOutput

PROFILE_ID = "research-profile"
TAXONOMY_ID = "research-taxonomy"


def version_key(version: str) -> tuple[int, Decimal | str]:
    """数字版本按大小排序，非数字版本排在后面并按字符串排序。"""
    try:
        return (0, Decimal(version))
    except InvalidOperation:
        return (1, version)


def _today_iso() -> str:
    return datetime.now(UTC).date().isoformat()


def normalize_shares(shares: list[float]) -> list[float]:
    """按比例缩放到和为 1；尾差记入最后一项，避免 0.1+0.2 类浮点误差。"""
    total = sum(shares)
    if total <= 0:
        return [1.0 / len(shares)] * len(shares)
    scaled = [round(value / total, 6) for value in shares]
    drift = round(1.0 - sum(scaled), 6)
    scaled[-1] = round(scaled[-1] + drift, 6)
    return scaled


def _yaml_front_matter(data: dict[str, Any]) -> str:
    dumped = yaml.safe_dump(
        data, allow_unicode=True, sort_keys=False, default_flow_style=False, width=100
    )
    return f"---\n{dumped}---\n"


def next_draft_version(versions: list[str]) -> str:
    """生成下一个草案版本号 0.1、0.2…；与数字版本冲突时继续递增。"""
    numbers = []
    for version in versions:
        try:
            numbers.append(int(round(float(version) * 10)))
        except ValueError:
            continue
    return f"0.{(max(numbers) + 1) if numbers else 1}"


def render_profile(
    data: ProfileExtractionOutput,
    *,
    version: str,
    change_note: str,
) -> str:
    profile = data.profile
    front: dict[str, Any] = {
        "version": version,
        "updated": _today_iso(),
        "profile_id": PROFILE_ID,
        "positioning": profile.positioning.model_dump(),
        "core_questions": [
            {"id": question.id, "text": question.text} for question in profile.core_questions
        ],
    }
    shares = normalize_shares([item.target_share for item in profile.subdirections])
    front["subdirections"] = [
        {
            "id": item.id,
            "name": item.name,
            "priority": item.priority,
            "target_share": share,
            "keywords": item.keywords,
            **({"description": item.description} if item.description else {}),
        }
        for item, share in zip(profile.subdirections, shares, strict=True)
    ]
    front["include"] = profile.include
    front["exclude"] = profile.exclude
    if profile.methods:
        front["methods"] = profile.methods
    if profile.data:
        front["data"] = profile.data
    if profile.authority is not None:
        authority = profile.authority.model_dump(exclude_none=True)
        if any(authority.values()):
            front["authority"] = authority
    if profile.negative_preferences:
        front["negative_preferences"] = profile.negative_preferences
    ratio = normalize_shares(profile.reading.deep_skim_ratio)
    reading: dict[str, Any] = {
        "weekly_capacity": profile.reading.weekly_capacity,
        "deep_skim_ratio": ratio,
        "reading_goals": profile.reading.reading_goals,
    }
    if profile.reading.push_time:
        reading["push_time"] = profile.reading.push_time
    front["reading"] = reading
    front["change_log"] = [{"version": version, "date": _today_iso(), "reason": change_note}]

    body_lines = [
        "",
        "# 研究方向档案",
        "",
        "## 研究定位",
        "",
        profile.positioning.summary,
        "",
        "## 核心问题",
        "",
    ]
    body_lines += [
        f"{index}. {question.text}" for index, question in enumerate(profile.core_questions, 1)
    ]
    body_lines += ["", "## 子方向", ""]
    body_lines += [
        f"- {item.name}（{share:.0%}）：{'、'.join(item.keywords)}"
        for item, share in zip(profile.subdirections, shares, strict=True)
    ]
    body_lines += [
        "",
        "## 阅读节奏",
        "",
        f"每周 {profile.reading.weekly_capacity} 篇，精读/粗读约 {ratio[0]:.0%}:{ratio[1]:.0%}。",
        "",
    ]
    return _yaml_front_matter(front) + "\n".join(body_lines)


def render_taxonomy(
    data: ProfileExtractionOutput,
    *,
    version: str,
) -> str:
    front: dict[str, Any] = {"version": version, "taxonomy_id": TAXONOMY_ID}
    lines = [
        _yaml_front_matter(front),
        "# 研究分类法",
        "",
        "每篇论文恰好一个主标签；主要贡献对象 > 方法 > 应用 > 数据。副标签不决定归属。",
        "",
    ]
    for tag in data.tags:
        lines.append(f"## {tag.code} {tag.name}")
        lines.append(f"- 定义：{tag.definition}")
        lines.append("- 纳入：" + "；".join(tag.include))
        lines.append("- 排除：" + "；".join(tag.exclude))
        lines.append("- 正例：" + "、".join(tag.examples))
        if tag.aliases:
            lines.append("- 别名：" + "、".join(tag.aliases))
        lines.extend(f"- 边界：{boundary}" for boundary in tag.boundaries)
        lines.append("")
    return "\n".join(lines)


__all__ = [
    "PROFILE_ID",
    "TAXONOMY_ID",
    "next_draft_version",
    "normalize_shares",
    "render_profile",
    "render_taxonomy",
    "version_key",
]
