"""候选的用户决定（R2-04）：确认生成资产、并入已有资产、忽略。

决定可回看不删数据：``reading_analysis_decision`` 按候选唯一（重复提交
重放同一结果，不同决定明确冲突）；确认/合并复用 R2-00B 资产写入（幂等
键绑定完整请求，expected_revision 乐观并发）；候选核对状态随决定回写。
「拒绝记忆持久」：被忽略候选按 (论文, 类型, 陈述哈希) 记忆，再次分析
产出完全相同陈述时不落库，改措辞的新候选仍会展示。
"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy.orm import Session

from papersteward.errors import ConflictError, NotFoundError
from papersteward.reading.insights.assets import append_revision, create_asset
from papersteward.reading.insights.contracts import (
    CandidateDecisionInput,
)
from papersteward.storage.delivery import content_hash
from papersteward.storage.insights import (
    ReadingAnalysisDecisionRepository,
)
from papersteward.storage.mappers import as_utc
from papersteward.storage.models import (
    ReadingAnalysisCandidateRow,
    ReadingAnalysisDecisionRow,
)

__all__ = [
    "CandidateDecisionOutcome",
    "decide_candidate",
    "dismissed_statement_hashes",
]

_KIND_DEFAULT_FROM_CANDIDATE = {
    "harvest": "harvest",
    "question": "problem",
    "method": "method",
}


class CandidateDecisionOutcome:
    """决定结果：目标资产视图（dismiss 为 None）、决定行与候选当前状态。"""

    def __init__(
        self,
        view: object | None,
        candidate: ReadingAnalysisCandidateRow,
        asset_id: str | None = None,
    ):
        self.view = view
        self.candidate = candidate
        self.asset_id = asset_id


def decide_candidate(
    session: Session, spec: CandidateDecisionInput, *, now: datetime
) -> CandidateDecisionOutcome:
    """处理候选决定；调用方持写事务。

    已有决定时：同 decision 且目标一致 → 重放既有结果（幂等）；不同
    decision 或不同目标 → 409 明确冲突，不覆盖用户决定。
    """
    candidate = session.get(ReadingAnalysisCandidateRow, spec.candidate_id)
    if candidate is None:
        raise NotFoundError("候选不存在")
    decisions = ReadingAnalysisDecisionRepository(session)
    existing = decisions.find_by_candidate(spec.candidate_id)
    if existing is not None:
        mapped = {
            "confirm": "confirmed",
            "dismiss": "dismissed",
            "merge": "merged",
        }[spec.decision]
        if existing.decision == mapped and (
            spec.decision != "merge" or existing.asset_id == spec.target_asset_id
        ):
            return _replay(session, existing, candidate)
        raise ConflictError(
            "该候选已处理，不能更改决定；资产可以修订或删除",
            details={"decision": existing.decision, "asset_id": existing.asset_id},
        )

    statement_hash = content_hash({"statement": candidate.statement})
    asset_id: str | None = None
    asset_revision: int | None = None
    view = None
    if spec.decision == "confirm":
        view, asset_id, asset_revision = _confirm_to_asset(session, spec, candidate, now)
    elif spec.decision == "merge":
        view, asset_id, asset_revision = _merge_into_asset(session, spec, candidate, now)
    decision_row = _decision_row(spec, candidate, statement_hash, asset_id, asset_revision, now)
    decisions.add(decision_row)
    candidate.verification = {
        "confirm": "confirmed",
        "dismiss": "dismissed",
        "merge": "merged",
    }[spec.decision]
    session.flush()
    return CandidateDecisionOutcome(view, candidate, asset_id=asset_id)


def dismissed_statement_hashes(session: Session, paper_id: str, kind: str) -> set[str]:
    """某论文某类型已被忽略的陈述哈希集合（runner 落库前过滤）。"""
    return ReadingAnalysisDecisionRepository(session).dismissed_statement_hashes(paper_id, kind)


def _confirm_to_asset(
    session: Session,
    spec: CandidateDecisionInput,
    candidate: ReadingAnalysisCandidateRow,
    now: datetime,
) -> tuple[object, str, int]:
    from papersteward.reading.insights.contracts import AssetCreateInput

    asset_spec = AssetCreateInput.model_validate(
        {
            "kind": spec.kind,
            "title": spec.title,
            "body": spec.body,
            "applicability": spec.applicability,
            "sources": [source.model_dump(mode="json") for source in (spec.sources or [])],
            "typed": spec.typed.model_dump(mode="json") if spec.typed else None,
            "idempotency_key": spec.idempotency_key,
        }
    )
    view = create_asset(session, asset_spec, now=now)
    return view, view.asset_id, view.revision.revision


def _merge_into_asset(
    session: Session,
    spec: CandidateDecisionInput,
    candidate: ReadingAnalysisCandidateRow,
    now: datetime,
) -> tuple[object, str, int]:
    target = spec.target_asset_id or ""
    from papersteward.reading.insights.contracts import AssetRevisionInput

    asset_spec = AssetRevisionInput.model_validate(
        {
            "expected_revision": spec.expected_revision,
            "idempotency_key": spec.idempotency_key,
            "title": spec.title,
            "body": spec.body,
            "applicability": spec.applicability,
            "status": spec.status,
            "sources": None,
            "typed": spec.typed.model_dump(mode="json") if spec.typed else None,
        }
    )
    view = append_revision(session, target, asset_spec, now=now)
    return view, target, view.revision.revision


def _decision_row(
    spec: CandidateDecisionInput,
    candidate: ReadingAnalysisCandidateRow,
    statement_hash: str,
    asset_id: str | None,
    asset_revision: int | None,
    now: datetime,
) -> ReadingAnalysisDecisionRow:
    from papersteward.storage.models import ReadingAnalysisDecisionRow

    return ReadingAnalysisDecisionRow(
        id=uuid4().hex,
        candidate_id=candidate.id,
        decision={
            "confirm": "confirmed",
            "dismiss": "dismissed",
            "merge": "merged",
        }[spec.decision],
        asset_id=asset_id,
        asset_revision=asset_revision,
        paper_id=candidate.paper_id,
        kind=candidate.kind,
        statement_hash=statement_hash,
        note=spec.note,
        decided_at=as_utc(now),
    )


def _replay(
    session: Session,
    existing: ReadingAnalysisDecisionRow,
    candidate: ReadingAnalysisCandidateRow,
) -> CandidateDecisionOutcome:
    view = None
    if existing.asset_id is not None:
        from papersteward.reading.insights.assets import get_asset_view

        view = get_asset_view(session, existing.asset_id)
    return CandidateDecisionOutcome(view, candidate, asset_id=existing.asset_id)
