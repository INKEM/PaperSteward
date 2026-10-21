"""种子导入、独立校准轮次和 G3；事务由调用方控制。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from papersteward.core.entities import Event, Paper
from papersteward.core.enums import EventType
from papersteward.core.errors import DomainValidationError, InvariantViolation
from papersteward.core.onboarding import Gate, GateApproval, Onboarding, OnboardingStage
from papersteward.errors import ConflictError, NotFoundError
from papersteward.onboarding.calibration import (
    AgentBatch,
    CalibrationPaper,
    UserBatch,
    calibration_report,
    report_hash,
    validate_cohort,
    validate_labels,
)
from papersteward.onboarding.service import _atomic, confirm_gate
from papersteward.onboarding.taxonomy import SeedLabel, taxonomy_review
from papersteward.storage.models import CalibrationRoundRow, EventRow
from papersteward.storage.onboarding import get_onboarding
from papersteward.storage.repositories import EventRepository, PaperRepository


@dataclass(frozen=True)
class CalibrationContext:
    binding: dict[str, Any]
    seeds: list[SeedLabel]
    codes: set[str]
    sample_kind: str


def _context(session: Session) -> CalibrationContext:
    event = session.scalar(
        select(EventRow)
        .where(EventRow.event_type == EventType.ONBOARDING_G2_CONFIRMED.value)
        .order_by(EventRow.created_at.desc(), EventRow.id.desc())
        .limit(1)
    )
    if event is None or "evidence" not in event.payload_json:
        raise InvariantViolation("onboarding.g2", "缺少完整 G2 确认证据")
    evidence = event.payload_json["evidence"]
    proof = EventRepository(session).get(evidence["draft_event_id"])
    if proof is None or report_hash(evidence["report"]) != evidence["review_hash"]:
        raise InvariantViolation("onboarding.g2", "G2 审阅依据不一致")
    review = taxonomy_review(
        session, event.payload_json["artifact_id"], event.payload_json["artifact_version"]
    )
    if (
        review["status"] != "confirmed"
        or evidence["profile"] != review["report"]["profile"]
        or review["content_hash"] != proof.payload_json["content_hash"]
    ):
        raise InvariantViolation("onboarding.g2", "G2 档案/分类法不再匹配，请先处理再协商")
    binding = {
        "g2_event_id": event.id,
        "profile": evidence["profile"],
        "taxonomy": {
            "id": review["id"],
            "version": review["version"],
            "content_hash": review["content_hash"],
        },
        "draft_event_id": proof.id,
        "g2_review_hash": evidence["review_hash"],
    }
    return CalibrationContext(
        binding,
        [SeedLabel.model_validate(item) for item in proof.payload_json["seeds"]],
        {item["tag_code"] for item in review["report"]["distribution"]},
        proof.payload_json["sample_kind"],
    )


def _require_calibration(session: Session) -> None:
    if get_onboarding(session).stage is not OnboardingStage.CALIBRATION:
        raise InvariantViolation("onboarding.stage", "仅 G2 后的校准阶段允许此操作")


def _append(
    session: Session, event_type: EventType, event_id: str, at: datetime, payload: dict[str, Any]
) -> None:
    EventRepository(session).append(
        Event(id=event_id, event_type=event_type, created_at=at, payload_json=payload)
    )


def _import_papers(
    session: Session, papers: list[dict[str, Any]], at: datetime, *, exact_metadata: bool = False
) -> tuple[int, int]:
    repository = PaperRepository(session)
    created = 0
    for metadata in papers:
        previous = repository.get(metadata["paper_id"])
        if previous is not None:
            if previous.title != metadata["title"] or (
                (exact_metadata or metadata.get("abstract") is not None)
                and previous.abstract != metadata.get("abstract")
            ):
                raise ConflictError(
                    "文献 ID 已存在且内容不同；不会覆盖已有数据",
                    details={"paper_id": metadata["paper_id"]},
                )
        else:
            repository.add(
                Paper(
                    id=metadata["paper_id"],
                    title=metadata["title"],
                    abstract=metadata.get("abstract"),
                    first_seen_at=at,
                )
            )
            created += 1
    return created, len(papers) - created


def import_seeds(session: Session, *, occurred_at: datetime) -> dict[str, Any]:
    _require_calibration(session)
    context = _context(session)
    with _atomic(session):
        created, existing = _import_papers(
            session, [seed.model_dump() for seed in context.seeds], occurred_at
        )
        identifier = f"evt-seeds-{report_hash(context.binding)[:32]}"
        if EventRepository(session).get(identifier) is None:
            _append(
                session,
                EventType.ONBOARDING_SEEDS_IMPORTED,
                identifier,
                occurred_at,
                {
                    "binding": context.binding,
                    "paper_ids": [seed.paper_id for seed in context.seeds],
                    "sample_kind": context.sample_kind,
                },
            )
    return {
        "created": created,
        "existing": existing,
        "total": len(context.seeds),
        "sample_kind": context.sample_kind,
    }


def _papers(row: CalibrationRoundRow) -> list[CalibrationPaper]:
    return [CalibrationPaper.model_validate(item) for item in row.cohort_json]


def _load(session: Session, number: int, context: CalibrationContext) -> CalibrationRoundRow:
    row = session.get(CalibrationRoundRow, number, populate_existing=True)
    if row is None:
        raise NotFoundError("校准轮次不存在", details={"round_number": number})
    if row.binding_json != context.binding or row.g2_event_id != context.binding["g2_event_id"]:
        raise InvariantViolation("calibration.binding", "校准轮次绑定已变化，不能沿用旧报告")
    papers = _papers(row)
    validate_cohort(papers, context.codes, {seed.paper_id for seed in context.seeds})
    for paper in papers:
        stored = PaperRepository(session).get(paper.paper_id)
        if stored is None or stored.title != paper.title or stored.abstract != paper.abstract:
            raise InvariantViolation("calibration.papers", "校准文献内容已变化")
    return row


def _compute(row: CalibrationRoundRow, context: CalibrationContext) -> dict[str, Any] | None:
    if row.agent_json is None or row.user_json is None:
        return None
    agent = AgentBatch.model_validate(row.agent_json)
    user = UserBatch.model_validate(row.user_json)
    papers = _papers(row)
    validate_labels(papers, agent.labels, context.codes)
    validate_labels(papers, user.labels, context.codes)
    report = calibration_report(papers, agent.labels, user.labels)
    report.update(
        {
            "binding": context.binding,
            "round_number": row.round_number,
            "rubric_version": row.rubric_version,
            "sample_kind": row.sample_kind,
            "sample_notice": "仅流程验证"
            if row.sample_kind == "synthetic"
            else "基于用户提供的独立标注",
            "agent_source": agent.source,
            "user_submitted_by": user.submitted_by,
        }
    )
    return report


def read_round(session: Session, number: int) -> dict[str, Any]:
    context = _context(session)
    row = _load(session, number, context)
    report = _compute(row, context)
    if report != row.report_json:
        raise InvariantViolation("calibration.report", "保存的校准报告与输入不一致")
    # 双方完成前不返回参考标签、分层、负例标记或任何一方的标注。
    return {
        "round_number": number,
        "status": "ready" if report is not None else "awaiting_labels",
        "agent_submitted": row.agent_json is not None,
        "user_submitted": row.user_json is not None,
        "papers": [
            {"paper_id": paper.paper_id, "title": paper.title, "abstract": paper.abstract}
            for paper in _papers(row)
        ],
        "report": report,
        "report_hash": None if report is None else report_hash(report),
    }


def create_round(
    session: Session,
    number: int,
    papers: list[CalibrationPaper],
    *,
    sample_kind: str,
    rubric_version: str,
    occurred_at: datetime,
) -> dict[str, Any]:
    _require_calibration(session)
    context = _context(session)
    validate_cohort(papers, context.codes, {seed.paper_id for seed in context.seeds})
    latest = session.scalar(select(func.max(CalibrationRoundRow.round_number))) or 0
    if number not in (1, 2) or number != latest + 1:
        raise ConflictError("轮次必须按 1、2 依次创建，最多两轮且不能覆盖")
    if latest and _load(session, latest, context).report_json is None:
        raise ConflictError("请先完成上一轮的双方标注与报告")
    with _atomic(session):
        _import_papers(
            session,
            [paper.model_dump(mode="json") for paper in papers],
            occurred_at,
            exact_metadata=True,
        )
        row = CalibrationRoundRow(
            round_number=number,
            g2_event_id=context.binding["g2_event_id"],
            binding_json=context.binding,
            cohort_json=[paper.model_dump(mode="json") for paper in papers],
            rubric_version=rubric_version,
            sample_kind="synthetic"
            if "synthetic" in (sample_kind, context.sample_kind)
            else "user",
            created_at=occurred_at,
        )
        session.add(row)
        session.flush()
        _append(
            session,
            EventType.ONBOARDING_CALIBRATION_CREATED,
            f"evt-calibration-{number}-created",
            occurred_at,
            {
                "round_number": number,
                "binding": context.binding,
                "cohort_hash": report_hash(row.cohort_json),
                "rubric_version": rubric_version,
                "sample_kind": row.sample_kind,
            },
        )
        return read_round(session, number)


def submit_labels(
    session: Session, number: int, batch: AgentBatch | UserBatch, *, occurred_at: datetime
) -> dict[str, Any]:
    _require_calibration(session)
    context = _context(session)
    row = _load(session, number, context)
    validate_labels(_papers(row), batch.labels, context.codes)
    agent = isinstance(batch, AgentBatch)
    column = CalibrationRoundRow.agent_json if agent else CalibrationRoundRow.user_json
    side = "agent" if agent else "user"
    with _atomic(session):
        changed = session.execute(
            update(CalibrationRoundRow)
            .where(CalibrationRoundRow.round_number == number, column.is_(None))
            .values({column: batch.model_dump(mode="json")})
            .returning(CalibrationRoundRow.round_number)
        ).scalar_one_or_none()
        if changed is None:
            raise ConflictError("本方已提交，标注不能覆盖；请使用下一轮校准")
        session.refresh(row)
        report = _compute(row, context)
        row.report_json = report
        session.flush()
        _append(
            session,
            EventType.ONBOARDING_AGENT_LABELED if agent else EventType.ONBOARDING_USER_LABELED,
            f"evt-calibration-{number}-{side}",
            occurred_at,
            {"round_number": number, "labels_hash": report_hash(batch.model_dump(mode="json"))},
        )
        if report is not None:
            _append(
                session,
                EventType.ONBOARDING_CALIBRATION_REPORTED,
                f"evt-calibration-{number}-report",
                occurred_at,
                {"round_number": number, "report_hash": report_hash(report), "report": report},
            )
        return read_round(session, number)


def confirm_g3(
    session: Session,
    number: int,
    *,
    confirmed_by: str,
    change_summary: str,
    expected_revision: int,
    expected_report_hash: str,
    accept_deviations: bool,
    known_biases: str | None,
    event_id: str,
    occurred_at: datetime,
) -> Onboarding:
    evidence: dict[str, Any] = {}

    def validate(session: Session, state: Onboarding, gate: Gate, approval: GateApproval) -> None:
        latest = session.scalar(select(func.max(CalibrationRoundRow.round_number)))
        if latest != number:
            raise InvariantViolation("calibration.round_number", "只能确认最新校准轮次")
        view = read_round(session, number)
        if view["report"] is None:
            raise InvariantViolation("calibration.labels", "必须先完成双方独立标注")
        if view["report_hash"] != expected_report_hash:
            raise InvariantViolation("calibration.report_hash", "报告已变化，请重新审阅")
        report = view["report"]
        note = (known_biases or "").strip()
        if (not report["metrics_passed"] and not accept_deviations) or (
            accept_deviations and not note
        ):
            raise DomainValidationError(
                "calibration.known_biases", "指标未达标时需明确接受并填写已知偏差"
            )
        evidence.update(
            {
                "round_number": number,
                "report_hash": expected_report_hash,
                "report": report,
                "accepted_deviations": not report["metrics_passed"] and accept_deviations,
                "known_biases": note or None,
            }
        )

    return confirm_gate(
        session,
        Gate.G3,
        GateApproval(confirmed_by, "calibration", str(number), change_summary),
        expected_revision=expected_revision,
        validate_evidence=validate,
        event_id=event_id,
        occurred_at=occurred_at,
        evidence_json=evidence,
    )
