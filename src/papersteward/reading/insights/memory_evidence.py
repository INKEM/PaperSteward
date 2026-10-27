"""先确定性分组与计数，再允许模型措辞；原始反馈是时间证据。"""

from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from papersteward.errors import InvalidRequestError
from papersteward.reading.insights.contracts import (
    VALUE_SOURCE_LABELS,
    AnalysisManifestContent,
    InsightPeriodRequest,
    InsightQueryRequest,
    MaterialExclusion,
    MaterialKind,
    MaterialRef,
    PaperProjection,
)
from papersteward.reading.insights.materials import build_synthesis_manifest
from papersteward.reading.insights.memory_contracts import (
    MEMORY_PURPOSE,
    MemoryEvidence,
    MemoryGroup,
)
from papersteward.reading.insights.projection import build_projection
from papersteward.reading.insights.timeline import workspace_timezone
from papersteward.storage.delivery import content_hash
from papersteward.storage.insights import ReadingAssetRepository
from papersteward.storage.mappers import as_utc
from papersteward.storage.models import ReadingStructuredFeedbackRow
from papersteward.storage.reading_memory import ReadingMemoryRepository


def memory_revision_hash(session: Session) -> str:
    return content_hash(
        [
            [row.memory_id, row.revision]
            for row in sorted(
                [
                    row
                    for row in ReadingMemoryRepository(session).current()
                    if row.origin != "model"
                ],
                key=lambda row: row.memory_id,
            )
        ]
    )


def build_memory_manifest(
    session: Session,
    *,
    kinds: list[MaterialKind],
    exclusions: list[MaterialExclusion],
    now: datetime,
) -> AnalysisManifestContent:
    query = InsightQueryRequest(
        period=InsightPeriodRequest(kind="all", timezone=workspace_timezone(session).key),
        mode="revised_now",
    )
    content = build_synthesis_manifest(
        session, query=query, kinds=kinds, exclusions=exclusions, now=now
    )
    return content.model_copy(
        update={"purpose": MEMORY_PURPOSE, "memory_revision_hash": memory_revision_hash(session)}
    )


def group_memory_evidence(session: Session, content: AnalysisManifestContent) -> list[MemoryGroup]:
    """只对本清单的真实材料分组；旧报告/画像从不作为独立证据。"""
    if content.scope_request is None or content.scope_request.period.kind != "all":
        raise InvalidRequestError("研究记忆需要全部历史材料，请重新预览")
    # 调用者负责真实一致读快照，避免 ORM SELECT 的隐式事务升级。
    projection = build_projection(
        session, content.scope_request, collected_at=content.built_at, shared_snapshot=True
    )
    papers = {paper.paper_id: paper for paper in projection.papers}
    refs_by_paper: dict[str, list[MaterialRef]] = defaultdict(list)
    for ref in content.materials:
        if ref.paper_id in papers:
            refs_by_paper[ref.paper_id].append(ref)
    values: dict[str, list[MemoryEvidence]] = defaultdict(list)
    topics: dict[str, list[MemoryEvidence]] = defaultdict(list)
    structured: dict[str, ReadingStructuredFeedbackRow] = {}
    for paper_id, refs in refs_by_paper.items():
        paper = papers[paper_id]
        for ref in refs:
            if ref.kind == "structured_reasons":
                revision = int(ref.version_key.removeprefix("revision:"))
                row = session.get(ReadingStructuredFeedbackRow, (paper_id, revision))
                if row is None:
                    continue
                structured[paper_id] = row
                previous_rows = session.scalars(
                    select(ReadingStructuredFeedbackRow).where(
                        ReadingStructuredFeedbackRow.paper_id == paper_id,
                        ReadingStructuredFeedbackRow.revision < row.revision,
                    )
                ).all()
                for previous_row in previous_rows:
                    for previous_code in previous_row.value_sources_json:
                        if previous_code in VALUE_SOURCE_LABELS:
                            values.setdefault(str(previous_code), [])
                for code in row.value_sources_json:
                    if code in VALUE_SOURCE_LABELS:
                        values[str(code)].append(_evidence(paper, ref, as_utc(row.recorded_at)))
            if ref.kind == "archive_metadata":
                key = f"{paper.taxonomy_version or 'unknown'}:{paper.primary_tag or 'unknown'}"
                # 兴趣窗口只能使用实际留有反馈的时间；仅阅读日期不代替反馈形成时间。
                timestamp = None
                if any(item.kind == "user_rating" for item in refs) and paper.feedback:
                    timestamp = paper.feedback[-1].recorded_at
                topics[key].append(_evidence(paper, ref, timestamp))
    groups: list[MemoryGroup] = []
    for code, supporting in sorted(values.items()):
        support_ids = {item.ref.paper_id for item in supporting}
        contrary = []
        for paper_id, row in structured.items():
            if paper_id in support_ids:
                # 同一反馈可同时包含价值与限制；限制不得被高评价吞掉。
                has_contrary = bool(row.limited_reasons_json)
            else:
                previous = session.scalars(
                    select(ReadingStructuredFeedbackRow).where(
                        ReadingStructuredFeedbackRow.paper_id == paper_id,
                        ReadingStructuredFeedbackRow.revision < row.revision,
                    )
                ).all()
                has_contrary = any(code in item.value_sources_json for item in previous)
            if has_contrary:
                ref = next(
                    item for item in refs_by_paper[paper_id] if item.kind == "structured_reasons"
                )
                contrary.append(
                    _evidence(papers[paper_id], ref, as_utc(row.recorded_at), contrary=True)
                )
        groups.append(
            _group(
                f"value:{code}",
                VALUE_SOURCE_LABELS[code],
                supporting + contrary,
                content,
                structured,
                value=True,
                task_value=code == "usable_now",
            )
        )
    for key, evidence in sorted(topics.items()):
        groups.append(
            _group(
                f"topic:{key}", f"主题阅读线索（{key}）", evidence, content, structured, value=False
            )
        )
    # 确认资产与单篇结果仅补语境，不额外增加独立论文或跨期反馈样本。
    for index, group in enumerate(groups):
        ids = {item.ref.paper_id for item in group.evidence}
        extra = []
        for ref in content.materials:
            parent_ids = set()
            if ref.kind == "asset_content":
                revision = int(ref.version_key.removeprefix("revision:"))
                parent_ids = {
                    row.paper_id
                    for row in ReadingAssetRepository(session).sources(ref.target_id, revision)
                }
            elif (
                ref.kind in {"verified_candidate", "user_notes", "user_excerpt", "user_rating"}
                and ref.paper_id
            ):
                parent_ids = {ref.paper_id}
            matched = sorted(parent_ids & ids - {None})
            if matched:
                extra.append(
                    _evidence(papers[matched[0]], ref, None).model_copy(update={"role": "context"})
                )
        evidence = group.evidence + extra
        groups[index] = group.model_copy(
            update={
                "evidence": evidence,
                "fingerprint": content_hash(
                    {
                        "base": group.fingerprint,
                        "context": [item.model_dump(mode="json") for item in extra],
                    }
                ),
            }
        )
    return groups


def _evidence(
    paper: PaperProjection, ref: MaterialRef, timestamp: datetime | None, *, contrary: bool = False
) -> MemoryEvidence:
    reading = paper.reading
    start = str(reading.day or reading.month) if reading.day or reading.month else None
    return MemoryEvidence(
        ref=ref,
        role="contrary" if contrary else "supporting",
        reading_precision=reading.precision.value,
        reading_basis=reading.basis.value,
        reading_start=start,
        reading_end=start,
        reading_recorded_at=reading.recorded_at_utc,
        feedback_at=timestamp,
        archived_at=paper.archived_at,
    )


def _group(
    key: str,
    label: str,
    evidence: list[MemoryEvidence],
    content: AnalysisManifestContent,
    structured: dict[str, ReadingStructuredFeedbackRow],
    *,
    value: bool,
    task_value: bool = False,
) -> MemoryGroup:
    timezone = (
        ZoneInfo(content.scope_request.period.timezone)
        if content.scope_request
        else ZoneInfo("UTC")
    )
    supporting = [item for item in evidence if item.role == "supporting"]
    ids = {item.ref.paper_id for item in supporting}
    times = sorted({as_utc(item.feedback_at) for item in supporting if item.feedback_at})
    months = sorted({stamp.astimezone(timezone).strftime("%Y-%m") for stamp in times})
    contexts = [
        (structured[paper_id].context_note or "").strip()
        for paper_id in ids
        if paper_id in structured
    ]
    single_task = (
        bool(contexts) and len(contexts) == len(ids) and all(contexts) and len(set(contexts)) == 1
    )
    archive_days = {item.archived_at.astimezone(timezone).date() for item in supporting}
    feedback_days = {stamp.astimezone(timezone).date() for stamp in times}
    batch = len(ids) > 1 and (len(archive_days) == 1 or len(feedback_days) == 1)
    eligible = (
        value
        and len(ids) >= 3
        and len(months) >= 2
        and not single_task
        and not batch
        and not task_value
    )
    limitations = ["只代表已记录样本；缺少的反馈不视为中立或反向偏好"]
    if not eligible:
        limitations.append("未达到稳定价值标准门槛，仅作为兴趣或价值线索")
    if batch:
        limitations.append("集中补录或集中反馈，不能制造跨期连续评价")
    if single_task or task_value:
        limitations.append("单任务或当前任务样本，适用范围限于任务")
    if not contexts or not all(contexts):
        limitations.append("任务语境未完整填写；不能推断适用于其他任务")
    if any(item.reading_precision == "unknown" for item in supporting):
        limitations.append("部分阅读日期未知")
    if any(item.reading_basis == "declared" for item in supporting):
        limitations.append("用户回溯声明的阅读日期不等于当年留下的反馈")
    if any(item.role == "contrary" for item in evidence):
        limitations.append("存在限制或撤去原理由的反馈，需并列核对；不自动解释为反向偏好")
    end = as_utc(content.built_at)
    recent = end - timedelta(weeks=8)
    previous = recent - timedelta(weeks=8)
    windows = {
        "recent_8_weeks": len(
            {
                item.ref.paper_id
                for item in supporting
                if item.feedback_at and recent <= as_utc(item.feedback_at) < end
            }
        ),
        "previous_8_weeks": len(
            {
                item.ref.paper_id
                for item in supporting
                if item.feedback_at and previous <= as_utc(item.feedback_at) < recent
            }
        ),
        "all_history": len(ids),
        "feedback_time_unknown": len(
            {item.ref.paper_id for item in supporting if not item.feedback_at}
        ),
    }
    data = {
        "key": key,
        "label": label,
        "kind": "value_standard" if eligible else "interest",
        "evidence": evidence,
        "distinct_papers": len(ids),
        "feedback_months": months,
        "first_feedback_at": times[0] if times else None,
        "last_feedback_at": times[-1] if times else None,
        "stable_eligible": eligible,
        "single_task": single_task or task_value,
        "concentrated_batch": batch,
        "context": "仅当前任务"
        if single_task or task_value
        else "任务语境未完整覆盖"
        if not contexts or not all(contexts)
        else "多个任务语境",
        "windows": windows,
        "limitations": limitations,
    }
    # 窗口随时间变化是说明；仅源版本/证据变化才需要新候选。
    fingerprint = content_hash(
        {
            "key": key,
            "evidence": [item.model_dump(mode="json") for item in evidence],
            "stable": eligible,
            "single_task": single_task or task_value,
            "batch": batch,
        }
    )
    return MemoryGroup.model_validate({**data, "fingerprint": fingerprint})


def begin_memory_snapshot(session: Session) -> None:
    session.execute(text("BEGIN DEFERRED"))
