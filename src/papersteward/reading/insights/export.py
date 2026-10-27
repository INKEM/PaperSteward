"""报告快照的本地导出：Markdown 与 JSON；纯函数，不触碰存储。

导出内容完全来自保存的快照（ReportDetail），不重算、不补最新数据；
正文本身已剥离备注原文，导出不会引入私人备注。
"""

import json
from typing import Any, Literal

from papersteward.delivery.render import markdown as _escape_md
from papersteward.reading.insights.contracts import (
    LIMITED_REASON_LABELS,
    MATERIAL_KIND_LABELS,
    VALUE_SOURCE_LABELS,
    InsightMetrics,
    InsightReportContentV2,
    ReportContent,
    ReportDetail,
    ReportPaper,
    ReviewMode,
    StructuredFeedbackChange,
    TimePrecision,
)

ReportExportFormat = Literal["markdown", "json"]

_MODE_LABELS = {ReviewMode.AS_RECORDED: "当时记录", ReviewMode.REVISED_NOW: "现在重新回顾"}
_SOURCE_LABELS = {"system": "系统完成", "imported": "外部导入", "unknown": "来源未明"}


def export_report(
    detail: ReportDetail, format: ReportExportFormat, *, include_private: bool = False
) -> str:
    if not include_private:
        detail = detail.model_copy(update={"content": shareable_content(detail.content)})
    if format == "json":
        return json.dumps(detail.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
    return _export_markdown(detail)


def shareable_content(content: ReportContent) -> ReportContent:
    """分享副本：清除资产标题/正文，并剔除依赖私人材料的整条归纳。"""
    shared = content.model_copy(deep=True)
    for ref in shared.assets:
        ref.title = _asset_placeholder(ref.kind)
        ref.body = None
    if isinstance(shared, InsightReportContentV2):
        section = shared.synthesis
        removed = 0
        for name in ("topics", "open_questions", "candidate_links"):
            items = list(getattr(section, name))
            kept = [item for item in items if not item.private]
            removed += len(items) - len(kept)
            setattr(section, name, kept)
        if removed and section.note:
            # 模型说明可能复述私人材料；存在私人条目时整段省略。
            section.note = "（模型说明因涉及私人材料已省略）"
        section.private_items = 0
        section.redacted_private_items += removed
    return shared


def private_content_summary(content: ReportContent) -> dict[str, object]:
    """导出页披露的私人内容概览；只含类别与数量，不含原文。"""
    asset_titles = sum(1 for ref in content.assets if bool(ref.title))
    asset_bodies = sum(1 for ref in content.assets if ref.body is not None)
    private_items = 0
    kinds: set[str] = set()
    if isinstance(content, InsightReportContentV2):
        for topic in content.synthesis.topics:
            if topic.private:
                private_items += 1
            kinds.update(
                ref.kind
                for ref in list(topic.supporting_refs) + list(topic.limiting_refs)
                if ref.private
            )
        for question in content.synthesis.open_questions:
            if question.private:
                private_items += 1
            kinds.update(ref.kind for ref in question.source_refs if ref.private)
        for link in content.synthesis.candidate_links:
            if link.private:
                private_items += 1
            kinds.update(ref.kind for ref in link.source_refs if ref.private)
    return {
        "asset_titles": asset_titles,
        "asset_bodies": asset_bodies,
        "private_items": private_items,
        "material_kinds": sorted(kinds),
    }


def _asset_placeholder(kind: str) -> str:
    return {
        "problem": "手工问题",
        "method": "手工方法卡",
        "evidence": "手工证据组",
    }.get(kind, "手工收获")


def _export_markdown(detail: ReportDetail) -> str:
    metrics = detail.content.metrics
    mode = _MODE_LABELS.get(detail.mode, detail.mode.value)
    lines = [
        # 周期标签由系统解析生成（如 2026-W43），无需转义。
        f"# 阅读回顾报告 · {detail.period_label}",
        "",
        f"- 模式：{mode} · 时区：{_escape_md(detail.timezone)}",
        f"- 知识截止：{detail.knowledge_cutoff.isoformat()}",
        f"- 生成时间：{detail.generated_at.isoformat()}",
        f"- 报告契约：{_escape_md(detail.contract)}",
        "",
        "## 指标",
        "",
        "| 指标 | 数值 |",
        "| --- | --- |",
        f"| 本期阅读（独立文献） | {metrics.readings_total} |",
        f"| 系统 {metrics.readings_system} · 导入 {metrics.readings_imported} | — |",
        f"| 本期系统内完成数（辅助对照） | {metrics.system_completions} |",
        f"| 本期补录（与阅读可能重叠） | {metrics.backfills_total} |",
        f"| 阅读时间未知（未定库存） | {metrics.unassigned_total} |",
        f"| 月精度未计入 | {metrics.partial_month_total} |",
        f"| 已评分覆盖 | {_coverage_text(metrics, 'scored')} |",
        f"| 有备注覆盖 | {_coverage_text(metrics, 'noted')} |",
        f"| 反馈变化 | 评分 {metrics.feedback_changes.score_events} · "
        f"备注 {metrics.feedback_changes.note_events} · "
        f"涉及 {metrics.feedback_changes.papers_affected} 篇 |",
        f"| 结构化理由覆盖 | {_structured_coverage_text(metrics)} |",
        f"| 全部历史（当前范围） | {metrics.all_history_total} |",
        "",
    ]
    lines += _distribution_lines(metrics)
    sections = (
        ("阅读明细", detail.content.readings),
        ("补录明细", detail.content.backfills),
        ("未定阅读日期", detail.content.unassigned),
    )
    for heading, papers in sections:
        lines.append(f"## {heading}（{len(papers)}）")
        lines.append("")
        if not papers:
            lines.append("（无）")
            lines.append("")
        for paper in papers:
            lines.append(f"- {_paper_line(paper)}")
        lines.append("")
    changes = detail.content.changes
    lines.append(f"## 反馈修订（{len(changes)}）")
    lines.append("")
    if not changes:
        lines.append("（无）")
        lines.append("")
    for change in changes:
        if change.kind == "score":
            text = f"评分 {change.old_score or '未评分'} → {change.new_score or '未评分'}"
        else:
            text = "备注变更"
        lines.append(f"- {change.recorded_at.isoformat()} · {_escape_md(change.paper_id)} · {text}")
    lines.append("")
    structured = detail.content.structured_changes
    lines.append(f"## 结构化理由修订（{len(structured)}）")
    lines.append("")
    if not structured:
        lines.append("（无）")
        lines.append("")
    for reason_change in structured:
        lines.append(
            f"- {reason_change.recorded_at.isoformat()} · "
            f"{_escape_md(reason_change.paper_id)} · "
            + _escape_md(_structured_change_text(reason_change))
        )
    lines.append("")
    if isinstance(detail.content, InsightReportContentV2):
        lines += _synthesis_markdown(detail.content)
    lines += ["## 手工资产", ""]
    for ref in detail.content.assets:
        lines.append(
            f"- {_escape_md(ref.title)} · {_escape_md(ref.status)} · {_escape_md(ref.asset_id)}"
        )
        if ref.body is not None:
            lines.append("")
            lines.append(_escape_md(ref.body))
            lines.append("")
    lines.append("> 数据完整性：阅读时间未知与月精度未计入的文献单列，不冒充精确计数；")
    lines.append("> 本报告不含备注原文，评分与备注可在原归档页查看。")
    lines.append("")
    return "\n".join(lines)


def _synthesis_markdown(content: InsightReportContentV2) -> list[str]:
    section = content.synthesis
    lines = ["## 知识整理", ""]
    if section.note:
        lines += [f"> {_escape_md(section.note)}", ""]
    if section.redacted_private_items:
        lines += [
            f"> 分享副本已排除 {section.redacted_private_items} 条依赖私人材料的归纳；"
            "原始报告未修改。",
            "",
        ]
    if section.private_items and not section.redacted_private_items:
        lines += [f"> 个人副本包含 {section.private_items} 条依赖私人材料的归纳。", ""]
    if section.insufficient_material:
        lines += ["材料不足，本次未形成专题判断。", ""]
    if section.topics:
        lines += ["### 专题", ""]
        for topic in section.topics:
            lines += [f"#### {_escape_md(topic.question)}", ""]
            lines += [_escape_md(topic.understanding), ""]
            lines += [f"- 支持材料：{_refs_text(topic.supporting_refs)}"]
            if topic.limiting_refs:
                lines += [f"- 限制/不同结果：{_refs_text(topic.limiting_refs)}"]
            if topic.comparison:
                lines += [f"- 条件对照：{_escape_md(topic.comparison)}"]
            if topic.unverified:
                lines += [f"- 待确认：{_escape_md(topic.unverified)}"]
            lines += [f"- 独立来源：{topic.independent_sources}", ""]
    if section.open_questions:
        lines += ["### 仍未解决", ""]
        for question in section.open_questions:
            status = {
                "needs_evidence": "缺证据",
                "depends_on_conditions": "条件不可比",
                "unresolved": "仍未解决",
            }.get(question.status, question.status)
            lines += [
                f"- {_escape_md(question.question)}（{status}；来源："
                f"{_refs_text(question.source_refs)}）"
            ]
        lines.append("")
    if section.candidate_links:
        lines += ["### 候选联系（未确认）", ""]
        for link in section.candidate_links:
            kind = "跨领域迁移" if link.link_kind == "cross_domain_transfer" else "研究机会"
            lines += [
                f"- **{kind}**：{_escape_md(link.statement)}"
                f"（来源：{_refs_text(link.source_refs)}）"
            ]
            if link.caution:
                lines += [f"  - 注意：{_escape_md(link.caution)}"]
        lines.append("")
    context = content.analysis
    lines += [
        "## 实际分析覆盖",
        "",
        f"- 材料：使用 {context.materials_used} / 清单 {context.materials_total} 条"
        f"（过期或缺失 {context.materials_stale} 条）",
        f"- 论文：范围内 {context.papers_in_scope} 篇，有材料 {context.papers_with_materials} 篇，"
        f"缺失 {context.missing_papers_total} 篇",
        f"- 省略：论文 {context.omitted_papers} 篇、材料 {context.omitted_materials} 条",
        f"- 已核对单篇候选 {context.verified_candidates} 条 · 已确认资产 {context.assets_used} 个",
        f"- 被服务端拒绝的引用 {context.rejected_refs} 个 · 被拒条目 {context.rejected_items} 条",
    ]
    if context.partial:
        lines += ["- **覆盖不完整**：缺失或省略的材料没有参与本次综合，不冒充全量。"]
    if context.sources_changed_after_send:
        lines += ["- 生成后源数据已有变化；本报告保留生成时的固定内容。"]
    lines += [
        "",
        "> 知识整理是模型归纳，待用户核对；它不改变事实统计，也不等于系统验证论文结论。",
        "",
    ]
    return lines


def _refs_text(refs: list[Any]) -> str:
    return "；".join(
        f"{MATERIAL_KIND_LABELS.get(ref.kind, ref.kind)} {ref.target_id}@{ref.version_key}"
        for ref in refs
    )


def _coverage_text(metrics: InsightMetrics, key: str) -> str:
    coverage = metrics.coverage
    if coverage.total == 0:
        return "暂无"
    value = getattr(coverage, key)
    return f"{value}/{coverage.total}"


def _structured_coverage_text(metrics: InsightMetrics) -> str:
    coverage = metrics.structured_coverage
    if coverage.total == 0:
        return "暂无"
    if coverage.covered == 0:
        return f"0/{coverage.total}"
    return (
        f"{coverage.covered}/{coverage.total}（价值来源 {coverage.value_sources}"
        f" · 价值有限 {coverage.limited_reasons} · 语境 {coverage.context}"
        f" · 收获疑问 {coverage.free_text}）"
    )


def _structured_change_text(change: StructuredFeedbackChange) -> str:
    parts = [
        f"{sign}{_code_label(labels, code)}"
        for codes, labels, sign in (
            (change.added_value_sources, VALUE_SOURCE_LABELS, "+"),
            (change.removed_value_sources, VALUE_SOURCE_LABELS, "−"),
            (change.added_limited_reasons, LIMITED_REASON_LABELS, "+"),
            (change.removed_limited_reasons, LIMITED_REASON_LABELS, "−"),
        )
        for code in codes
    ]
    if change.context_changed:
        parts.append("语境变更")
    if change.free_text_changed:
        parts.append("文字变更")
    if change.link_changed:
        parts.append("关联评分变更")
    if change.cleared:
        parts.append("全部清除")
    return " · ".join(parts) if parts else "内容调整"


def _code_label(labels: dict[str, str], code: str) -> str:
    return labels.get(code, code)


def _distribution_lines(metrics: InsightMetrics) -> list[str]:
    dist = metrics.score_distribution
    lines = [
        "## 分布",
        "",
        "### 评分分布",
        "",
    ]
    if metrics.readings_total == 0:
        lines.append("暂无")
    else:
        lines += [
            "| 档位 | 篇数 |",
            "| --- | --- |",
            f"| 1 分 | {dist.score_1} |",
            f"| 2 分 | {dist.score_2} |",
            f"| 3 分 | {dist.score_3} |",
            f"| 4 分 | {dist.score_4} |",
            f"| 5 分 | {dist.score_5} |",
            f"| 未评分 | {dist.unrated} |",
        ]
    lines += ["", "### 阅读方式", ""]
    if metrics.reading_modes:
        lines += ["| 方式 | 篇数 |", "| --- | --- |"]
        lines += [
            f"| {_escape_md(entry.label if entry.key != 'unknown' else '未知')} | {entry.count} |"
            for entry in metrics.reading_modes
        ]
    else:
        lines.append("暂无")
    lines += ["", "### 主题分布（互斥；跨版本同名不合并）", ""]
    if metrics.primary_tags:
        lines += ["| 标签 | 版本 | 篇数 |", "| --- | --- | --- |"]
        for entry in metrics.primary_tags:
            version = _escape_md(entry.taxonomy_version or "—")
            lines.append(f"| {_escape_md(entry.label)} | {version} | {entry.count} |")
    else:
        lines.append("暂无")
    lines += ["", "### 发表年代", ""]
    if metrics.published_decades:
        lines += ["| 年代 | 篇数 |", "| --- | --- |"]
        lines += [
            f"| {_escape_md('未知' if entry.key == 'unknown' else entry.label)} | {entry.count} |"
            for entry in metrics.published_decades
        ]
    else:
        lines.append("暂无")
    lines.append("")
    return lines


def _paper_line(paper: ReportPaper) -> str:
    title = _escape_md(paper.title or paper.paper_id)
    source = _SOURCE_LABELS.get(paper.source.value, paper.source.value)
    reading = _reading_text(paper)
    latest = paper.feedback[-1] if paper.feedback else None
    score = "未评分" if latest is None or latest.score is None else f"{latest.score} 分"
    tag = _escape_md(paper.primary_tag or "缺标签")
    return f"{title}（{_escape_md(source)}）· 阅读时间：{reading} · 评分：{score} · 主标签：{tag}"


def _reading_text(paper: ReportPaper) -> str:
    reading = paper.reading
    if reading.precision is TimePrecision.DAY and reading.day is not None:
        label = reading.day.isoformat()
        return f"{label}（系统记录）" if reading.basis.value == "system_recorded" else label
    if reading.precision is TimePrecision.MONTH and reading.month is not None:
        return f"{reading.month}（仅知月份）"
    return "未记录"
