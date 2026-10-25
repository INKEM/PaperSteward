"""回顾统计的纯计算层；首阶段先交付反馈变化清单，指标口径在后续批次叠加。"""

from collections.abc import Sequence

from papersteward.reading.insights.contracts import (
    FeedbackVersion,
    NoteFeedbackChange,
    ScoreFeedbackChange,
)


def feedback_changes(
    paper_id: str, versions: Sequence[FeedbackVersion]
) -> list[ScoreFeedbackChange | NoteFeedbackChange]:
    """从相邻反馈版本得到实际认知变化。

    首个版本是初始反馈，不算修订；备注变更只算备注、评分变更只算评分；
    清除为 null 也是有效变化；值未变的重复写入不制造变化事件。
    同一版本同时改两字段时，先评分后备注，各记一条。
    """
    changes: list[ScoreFeedbackChange | NoteFeedbackChange] = []
    for prev, curr in zip(versions, versions[1:], strict=False):
        if prev.score != curr.score:
            changes.append(
                ScoreFeedbackChange(
                    kind="score",
                    paper_id=paper_id,
                    recorded_at=curr.recorded_at,
                    old=prev.score,
                    new=curr.score,
                )
            )
        if prev.note != curr.note:
            changes.append(
                NoteFeedbackChange(
                    kind="note",
                    paper_id=paper_id,
                    recorded_at=curr.recorded_at,
                    old=prev.note,
                    new=curr.note,
                )
            )
    return changes
