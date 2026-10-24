"""面向阅读决策的评分解释；实现细节留在原有 JSON API。"""

from html import escape

from papersteward.scoring.contracts import Exclusion, ScoreExplanation

COMPONENT_LABELS = {
    "R": ("研究相关性", "与研究问题、方法和领域的匹配程度"),
    "A": ("可信度线索", "发表渠道、作者与引用等可用信号"),
    "E": ("复用价值", "代码、数据与开放获取等可用资源"),
    "F": ("新鲜度", "论文发表时间带来的参考价值"),
    "H": ("方向平衡", "让近期阅读较少的研究方向得到关注"),
    "U": ("阅读偏好", "根据已完成论文的评分提供参考"),
}
EXCLUSION_LABELS = {
    "summary_unavailable": "暂时没有可读概括",
    "below_relevance_gate": "与当前研究方向的相关性偏低",
    "special_bucket": "已收录在特殊桶，可到特殊桶阅读",
    "missing_static_snapshot": "尚未完成评分",
    "static_version_mismatch": "研究设置已变化，需要刷新评分",
    "invalid_classification_or_filing": "分类或研究方向归属尚未就绪",
    "classification_version_mismatch": "分类规则已更新，当前标签需要重新核对",
}


def score_explanation(score: ScoreExplanation | Exclusion) -> str:
    if isinstance(score, Exclusion):
        reasons = [EXCLUSION_LABELS.get(reason, "当前不在阅读队列") for reason in score.reasons]
        return (
            "<section><h2>评分说明</h2><p>"
            + escape("；".join(dict.fromkeys(reasons)))
            + "</p></section>"
        )
    body = (
        '<section class="score-explanation"><h2>为什么推荐这篇</h2>'
        f'<p class="score-overview"><strong>{score.scores["P"]:.1f}</strong> / 100 分'
        f' <span class="muted">评分日期：{score.score_date}</span></p>'
        "<p>分数帮助你选择先读什么；队列按平滑后的评分排序，名次可能与综合分略有不同。</p>"
        '<div class="score-components">'
    )
    for key, (label, description) in COMPONENT_LABELS.items():
        component = score.components.get(key)
        if component is None:
            continue
        body += (
            f"<div><h3>{label} <span>{component.score:.0f} / 100</span></h3>"
            f'<meter min="0" max="100" value="{component.score}" aria-label="{label}"></meter>'
            f'<p class="muted">{description}</p></div>'
        )
    body += "</div>"
    if score.components.get("U") and score.components["U"].confidence == 0:
        body += (
            '<p class="muted">阅读评分积累较少，偏好暂按中性值参考；'
            "缺少资源信息也不会直接记为零分。</p>"
        )
    quotes: list[str] = []
    for evidence in score.evidence:
        if evidence.get("stage") != "relevance":
            continue
        details = evidence.get("details") or {}
        if not isinstance(details, dict):
            continue
        rubric = details.get("rubric") or {}
        if isinstance(rubric, dict):
            quotes.extend(
                item["quote"]
                for item in rubric.get("evidence", [])
                if isinstance(item, dict)
                and item.get("valid")
                and isinstance(item.get("quote"), str)
            )
    if quotes:
        body += "<details><summary>相关性依据（论文原文片段）</summary>"
        body += "".join(
            f"<blockquote>{escape(quote)}</blockquote>" for quote in list(dict.fromkeys(quotes))[:3]
        )
        body += "</details>"
    return body + "</section>"
