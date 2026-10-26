"""报告快照的本地导出：Markdown 与 JSON；纯函数，不触碰存储。

导出内容完全来自保存的快照（ReportDetail），不重算、不补最新数据；
正文本身已剥离备注原文，导出不会引入私人备注。
"""

import json
from typing import Literal

from papersteward.delivery.render import markdown as _escape_md
from papersteward.reading.insights.contracts import (
    LIMITED_REASON_LABELS,
    VALUE_SOURCE_LABELS,
    InsightMetrics,
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
        detail = detail.model_copy(
            update={
                "content": detail.content.model_copy(
                    update={
                        "assets": [
                            ref.model_copy(
                                update={
                                    "title": "手工问题" if ref.kind == "problem" else "手工收获",
                                    "body": None,
                                }
                            )
                            for ref in detail.content.assets
                        ]
                    }
                )
            }
        )
    if format == "json":
        return json.dumps(detail.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
    return _export_markdown(detail)


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
