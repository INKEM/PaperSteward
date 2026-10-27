"""0024 的保守旧报告上下文恢复；不写正文、不猜测缺失的主题或算法。"""

from typing import Any

from papersteward.reading.insights.contracts import (
    AnalysisScope,
    InsightMetrics,
    InsightPeriodRequest,
    InsightQueryRequest,
    ReportContext,
)
from papersteward.reading.insights.periods import resolve_period
from papersteward.reading.insights.scope import normalized_query, scope_topic


def legacy_report_context(content: dict[str, Any], request: Any, *, timezone: str) -> ReportContext:
    metrics = InsightMetrics.model_validate(content["metrics"])
    query = None
    body_policy = None
    review = request.get("review") if isinstance(request, dict) else None
    if isinstance(review, dict) and "tag" in review:
        try:
            period = InsightPeriodRequest.model_validate(
                {
                    "kind": review.get("period"),
                    "timezone": timezone,
                    "anchor_date": review.get("anchor_date"),
                    "start_date": review.get("start_date"),
                    "end_date": review.get("end_date"),
                }
            )
            candidate = normalized_query(
                InsightQueryRequest(
                    period=period,
                    mode=review.get("mode", "as_recorded"),
                    tag=review["tag"],
                )
            )
            if (
                resolve_period(candidate.period) == metrics.period
                and candidate.mode == metrics.mode
            ):
                query = candidate
                if isinstance(review.get("asset_bodies"), bool):
                    body_policy = review["asset_bodies"]
        except (ValueError, TypeError):
            pass
    return ReportContext(
        status="legacy_unknown",
        normalized_request=query,
        include_asset_bodies=body_policy,
        scope=AnalysisScope(
            period=metrics.period,
            mode=metrics.mode,
            knowledge_cutoff=metrics.knowledge_cutoff,
            topic=scope_topic(query.tag) if query else None,
            topic_known=query is not None,
            # /1 冻结正文没有保存统计算法版本，不能从当前代码或正文内容反推。
            algorithm_version=None,
        ),
    )
