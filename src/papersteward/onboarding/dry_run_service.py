"""版本化试运行与 G4；只 flush，事务提交由调用方控制。"""

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from papersteward.core.entities import Config, Event
from papersteward.core.enums import AssignedBy, EventType
from papersteward.core.errors import InvariantViolation
from papersteward.core.onboarding import Gate, GateApproval, Onboarding, OnboardingStage
from papersteward.errors import ConflictError, NotFoundError
from papersteward.onboarding.calibration import report_hash
from papersteward.onboarding.calibration_service import read_round
from papersteward.onboarding.dry_run import DryRunFeedback, DryRunRequest, build_report
from papersteward.onboarding.service import _atomic, confirm_gate
from papersteward.scoring.service import CONFIG_KEY, save_scoring_config
from papersteward.storage.models import EventRow
from papersteward.storage.onboarding import get_onboarding
from papersteward.storage.repositories import ConfigRepository, EventRepository

RUNTIME_KEY = "onboarding_runtime"


def _phase(session: Session) -> None:
    if get_onboarding(session).stage is not OnboardingStage.DRY_RUN:
        raise InvariantViolation("onboarding.stage", "仅 G3 后的试运行阶段允许此操作")


def _context(session: Session) -> tuple[dict[str, Any], set[str]]:
    event = session.scalar(
        select(EventRow)
        .where(EventRow.event_type == EventType.ONBOARDING_G3_CONFIRMED.value)
        .order_by(EventRow.created_at.desc(), EventRow.id.desc())
        .limit(1)
    )
    if event is None or "evidence" not in event.payload_json:
        raise InvariantViolation("onboarding.g3", "缺少完整 G3 确认证据")
    evidence = event.payload_json["evidence"]
    view = read_round(session, evidence["round_number"])
    if (
        view["report"] is None
        or view["report_hash"] != evidence["report_hash"]
        or view["report"] != evidence["report"]
    ):
        raise InvariantViolation("onboarding.g3", "G3 依据已变化，不能沿用旧校准结果")
    report = view["report"]
    return {
        "g3_event_id": event.id,
        "calibration_round": evidence["round_number"],
        "calibration_report_hash": view["report_hash"],
        "calibration_binding": report["binding"],
    }, {paper["reference_tag"] for paper in report["cohort"]}


def _latest_version(session: Session) -> int:
    value = session.scalar(
        select(EventRow.payload_json["version"].as_integer())
        .where(EventRow.event_type == EventType.ONBOARDING_DRY_RUN_CREATED.value)
        .order_by(EventRow.payload_json["version"].as_integer().desc())
        .limit(1)
    )
    return value or 0


def _append(
    session: Session, identifier: str, event_type: EventType, at: datetime, payload: dict[str, Any]
) -> None:
    EventRepository(session).append(
        Event(id=identifier, event_type=event_type, created_at=at, payload_json=payload)
    )


def _check_config(session: Session, key: str, version: str, value: dict[str, Any]) -> bool:
    previous = ConfigRepository(session).get(key, version)
    if previous is not None and dict(previous.value_json) != value:
        raise ConflictError(
            "配置版本已存在且内容不同，请提升版本后重新预览",
            details={"key": key, "version": version},
        )
    return previous is not None


def _body(session: Session, version: int) -> tuple[DryRunRequest, dict[str, Any]]:
    event = EventRepository(session).get(f"evt-dry-run-{version}")
    if event is None or event.event_type is not EventType.ONBOARDING_DRY_RUN_CREATED:
        raise NotFoundError("试运行版本不存在", details={"version": version})
    body = DryRunRequest.model_validate(event.payload_json["input"])
    if body.version != version:
        raise InvariantViolation("dry_run.version", "试运行版本与保存依据不一致")
    return body, dict(event.payload_json)


def read_preview(session: Session, version: int) -> dict[str, Any]:
    body, proof = _body(session, version)
    binding, codes = _context(session)
    report = build_report(body, binding, codes)
    digest = report_hash(report)
    if proof["report"] != report or proof["report_hash"] != digest:
        raise InvariantViolation("dry_run.report", "日报/参数或前序依据已变化，请重新审阅")
    _check_config(session, CONFIG_KEY, body.scoring.version, body.scoring.model_dump(mode="json"))
    _check_config(session, RUNTIME_KEY, str(version), body.parameters.model_dump(mode="json"))
    rows = session.scalars(
        select(EventRow)
        .where(
            EventRow.event_type == EventType.ONBOARDING_DRY_RUN_FEEDBACK.value,
            EventRow.payload_json["version"].as_integer() == version,
        )
        .order_by(EventRow.payload_json["sequence"].as_integer())
    ).all()
    history = [dict(row.payload_json) for row in rows]
    if any(item["report_hash"] != digest for item in history):
        raise InvariantViolation("dry_run.feedback", "反馈与日报依据不一致")
    feedback = history[-1] if history else None
    return {
        "version": version,
        "report": report,
        "report_hash": digest,
        "feedback": feedback,
        "feedback_hash": None if feedback is None else report_hash(feedback),
        "feedback_history": history,
    }


def create_preview(
    session: Session, body: DryRunRequest, *, occurred_at: datetime
) -> dict[str, Any]:
    with _atomic(session):
        _phase(session)
        if body.version != _latest_version(session) + 1:
            raise ConflictError("试运行版本必须从 1 开始依次追加，不能覆盖或跳号")
        binding, codes = _context(session)
        report = build_report(body, binding, codes)
        _check_config(
            session, CONFIG_KEY, body.scoring.version, body.scoring.model_dump(mode="json")
        )
        _check_config(
            session, RUNTIME_KEY, str(body.version), body.parameters.model_dump(mode="json")
        )
        _append(
            session,
            f"evt-dry-run-{body.version}",
            EventType.ONBOARDING_DRY_RUN_CREATED,
            occurred_at,
            {
                "version": body.version,
                "input": body.model_dump(mode="json"),
                "report": report,
                "report_hash": report_hash(report),
            },
        )
        return read_preview(session, body.version)


def record_feedback(
    session: Session, version: int, body: DryRunFeedback, *, occurred_at: datetime
) -> dict[str, Any]:
    with _atomic(session):
        _phase(session)
        if version != _latest_version(session):
            raise InvariantViolation("dry_run.version", "只能反馈最新试运行版本")
        view = read_preview(session, version)
        if body.report_hash != view["report_hash"]:
            raise InvariantViolation("dry_run.report_hash", "日报已变化，请刷新后再反馈")
        sequence = len(view["feedback_history"]) + 1
        _append(
            session,
            f"evt-dry-run-{version}-feedback-{sequence}",
            EventType.ONBOARDING_DRY_RUN_FEEDBACK,
            occurred_at,
            {**body.model_dump(mode="json"), "version": version, "sequence": sequence},
        )
        return read_preview(session, version)


def confirm_g4(
    session: Session,
    version: int,
    approval: GateApproval,
    *,
    expected_revision: int,
    expected_report_hash: str,
    expected_feedback_hash: str,
    event_id: str,
    occurred_at: datetime,
) -> Onboarding:
    evidence: dict[str, Any] = {}

    def validate(session: Session, state: Onboarding, gate: Gate, approval: GateApproval) -> None:
        if version != _latest_version(session):
            raise InvariantViolation("dry_run.version", "只能确认最新试运行版本")
        view = read_preview(session, version)
        if (
            view["report_hash"] != expected_report_hash
            or view["feedback_hash"] != expected_feedback_hash
        ):
            raise InvariantViolation("dry_run.review_hash", "日报或反馈已变化，请重新审阅")
        if view["feedback"] is None or not view["feedback"]["accepted"]:
            raise InvariantViolation("dry_run.accepted", "试运行日报尚未获认可，请先调整参数或反馈")
        body, _ = _body(session, version)
        scoring = body.scoring.model_dump(mode="json")
        runtime = body.parameters.model_dump(mode="json")
        if not _check_config(session, CONFIG_KEY, body.scoring.version, scoring):
            save_scoring_config(session, body.scoring, updated_at=occurred_at)
        if not _check_config(session, RUNTIME_KEY, str(version), runtime):
            ConfigRepository(session).add(
                Config(
                    key=RUNTIME_KEY,
                    version=str(version),
                    value_json=runtime,
                    updated_by=AssignedBy.USER,
                    updated_at=occurred_at,
                )
            )
            _append(
                session,
                f"evt-runtime-config-{version}",
                EventType.CONFIG_UPDATED,
                occurred_at,
                {"key": RUNTIME_KEY, "version": str(version), "value_hash": report_hash(runtime)},
            )
        evidence.update(
            {
                "version": version,
                "report_hash": view["report_hash"],
                "report": view["report"],
                "feedback_hash": view["feedback_hash"],
                "feedback": view["feedback"],
                "feedback_history": view["feedback_history"],
                "scoring_version": body.scoring.version,
                "runtime_version": str(version),
            }
        )

    return confirm_gate(
        session,
        Gate.G4,
        approval,
        expected_revision=expected_revision,
        validate_evidence=validate,
        event_id=event_id,
        occurred_at=occurred_at,
        evidence_json=evidence,
    )
