"""研究档案/分类草案与已确认设置的只读渲染；设置页与首次设置共用（Q-02）。"""

from __future__ import annotations

from html import escape
from typing import Any

from papersteward.api.deps import SessionDep
from papersteward.documents import parse_profile, parse_taxonomy


def _tags(values: list[str]) -> str:
    return "".join(f'<span class="tag">{escape(str(value))}</span>' for value in values)


def profile_preview(review: dict[str, Any]) -> str:
    profile = parse_profile(review["content_md"], document_id=review["id"])
    data = dict(profile.parsed_json)
    positioning = data.get("positioning") or {}
    body = '<div class="draft-preview">'
    summary = positioning.get("summary")
    if summary:
        body += f"<p>{escape(str(summary))}</p>"
    meta = [str(positioning[key]) for key in ("domain", "career_stage") if positioning.get(key)]
    if meta:
        body += f'<p class="muted">{escape(" · ".join(meta))}</p>'
    body += "<h3>核心问题</h3><ol>"
    body += "".join(f"<li>{escape(str(item['text']))}</li>" for item in data["core_questions"])
    body += "</ol><h3>子方向</h3><table><thead><tr><th>子方向</th><th>优先级</th>"
    body += "<th>目标占比</th><th>检索关键词</th></tr></thead><tbody>"
    for item in data["subdirections"]:
        body += (
            f"<tr><td>{escape(str(item['name']))}</td>"
            f"<td>{escape(str(item.get('priority', 'medium')))}</td>"
            f"<td>{float(item['target_share']):.0%}</td>"
            f"<td>{_tags(item.get('keywords', []))}</td></tr>"
        )
    body += "</tbody></table>"
    for label, key in (("纳入标准", "include"), ("排除标准", "exclude")):
        values = data.get(key) or []
        if values:
            body += f"<h3>{label}</h3><ul>"
            body += "".join(f"<li>{escape(str(value))}</li>" for value in values)
            body += "</ul>"
    reading = data.get("reading") or {}
    ratio = reading.get("deep_skim_ratio") or [0, 1]
    body += (
        "<h3>阅读节奏</h3><p>"
        f"每周 {escape(str(reading.get('weekly_capacity', '—')))} 篇，"
        f"精读/粗读约 {float(ratio[0]):.0%}:{float(ratio[1]):.0%}。"
        "</p>"
    )
    goals = reading.get("reading_goals") or []
    if goals:
        body += f"<p>目标：{_tags(goals)}</p>"
    return body + "</div>"


def taxonomy_preview(review: dict[str, Any]) -> str:
    taxonomy = parse_taxonomy(review["content_md"], document_id=review["id"])
    body = '<div class="draft-preview">'
    for tag in taxonomy.tags:
        if tag.level.value != "primary":
            continue
        body += f"<details><summary>{escape(tag.code)} {escape(tag.name)}</summary>"
        body += f"<p>{escape(tag.definition)}</p>"
        body += "<p><strong>纳入</strong></p><ul>"
        body += "".join(f"<li>{escape(value)}</li>" for value in tag.include)
        body += "</ul><p><strong>排除</strong></p><ul>"
        body += "".join(f"<li>{escape(value)}</li>" for value in tag.exclude)
        body += "</ul><p><strong>正例</strong> " + _tags(list(tag.examples)) + "</p></details>"
    return body + "</div>"


def draft_card(title: str, review: dict[str, Any], preview: str) -> str:
    body = (
        f'<article><h2>{escape(title)} <span class="muted">v{escape(review["version"])}</span></h2>'
    )
    if review.get("missing_fields"):
        body += (
            '<p class="notice">还需补充：'
            + escape("、".join(str(field) for field in review["missing_fields"]))
            + "。可以提意见让 Agent 补全。</p>"
        )
    body += (
        "<details open><summary>渲染视图</summary>"
        + preview
        + "</details><details><summary>Markdown 源码</summary><pre>"
        + escape(str(review["content_md"]))
        + "</pre></details>"
    )
    if review.get("markdown_diff"):
        body += (
            "<details><summary>与上一版的差异</summary><pre>"
            + escape(str(review["markdown_diff"]))
            + "</pre></details>"
        )
    return body + "</article>"


def confirmed_results(session: SessionDep) -> str:
    """确认后的研究档案与分类规则继续可见，复用已有安全渲染。"""
    from papersteward.onboarding.setup import RUNTIME_KEY
    from papersteward.storage.repositories import (
        ConfigRepository,
        ProfileRepository,
        TaxonomyRepository,
    )

    profile = ProfileRepository(session).latest_confirmed()
    taxonomy = TaxonomyRepository(session).latest_confirmed()
    if profile is None or taxonomy is None:
        return (
            "<article><h2>研究档案与分类标签</h2><p>尚未完成首次设置。</p>"
            '<a href="/ui/setup">开始首次设置</a></article>'
        )
    profile_data = {"id": profile.id, "version": profile.version, "content_md": profile.content_md}
    taxonomy_data = {
        "id": taxonomy.id,
        "version": taxonomy.version,
        "content_md": taxonomy.content_md,
    }
    body = "<h2>当前使用的研究设置</h2>"
    body += draft_card("已确认的研究方向档案", profile_data, profile_preview(profile_data))
    body += draft_card("已确认的分类标签", taxonomy_data, taxonomy_preview(taxonomy_data))
    runtime = ConfigRepository(session).latest(RUNTIME_KEY)
    if runtime is not None:
        body += (
            '<p class="muted">工作区时区：'
            + escape(str(runtime.value_json.get("timezone", "")))
            + "。挖掘由你手动发起，阅读容量仅作建议。</p>"
        )
    return body


__all__ = [
    "confirmed_results",
    "draft_card",
    "profile_preview",
    "taxonomy_preview",
]
