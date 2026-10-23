"""Single local worker with durable Run progress; completed steps commit independently."""

from __future__ import annotations

import hashlib
import logging
from queue import Empty, Queue
from threading import Event, Lock, Thread
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from papersteward.cli.ingestion import _adapters
from papersteward.cli.processing import _processor_context
from papersteward.config import Settings
from papersteward.core.entities import Run, utc_now
from papersteward.core.enums import PaperStatus, RunStatus
from papersteward.core.errors import DomainError
from papersteward.errors import AppError, ConflictError, InvalidRequestError, NotFoundError
from papersteward.llm import LLMGateway, build_llm_provider
from papersteward.llm.errors import LLMError, LLMTimeoutError, LLMValidationError
from papersteward.llm.runtime import resolve_llm_config
from papersteward.onboarding.intake import ensure_draft_stage, materials_text, run_extraction
from papersteward.pipeline.context import bound_context
from papersteward.pipeline.embeddings import HashingEmbeddingProvider
from papersteward.pipeline.ingest import ingest
from papersteward.pipeline.process import process_paper, reconcile_legacy_reviews
from papersteward.scoring.queue import refresh_queue
from papersteward.sources.contracts import QuerySpec
from papersteward.sources.http import SourceHTTP
from papersteward.storage import session_scope
from papersteward.storage.models import PaperRow, RunRow
from papersteward.storage.repositories import PaperRepository, RunRepository

logger = logging.getLogger(__name__)


class FlowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    kind: Literal["sync", "refresh", "extract"]
    source: Literal["arxiv", "openalex", "semantic_scholar"] = "arxiv"
    query: str = Field(default="", max_length=500)
    limit: int = Field(default=10, ge=1, le=50)


def processing_problem(session: Session, settings: Settings) -> str | None:
    """同步/处理前置检查：模型服务就绪即可运行；授权随设置页保存。"""
    return resolve_llm_config(session, settings).readiness_problem()


def llm_failure_message(exc: LLMError) -> str:
    """把网关/provider 错误转成用户可操作的中文提示；不含 prompt、密钥或技术后缀。"""
    if isinstance(exc, LLMValidationError):
        return f"模型返回不符合要求：{exc}。可以重试，或在「设置」页换一个模型。"
    detail = exc.args[0] if exc.args else str(exc)
    if isinstance(exc, LLMTimeoutError):
        return (
            "模型服务已连上，但等待响应超时（自动重试后仍超时），通常是模型处理太久。"
            "可到「设置」页把「请求超时（秒）」调大（如 120–300）后重新执行；已完成的数据保留。"
        )
    if "认证失败" in detail:
        return (
            "模型认证失败：API 密钥或地址不正确。"
            "请到「设置」页检查密钥后点「测试连接」，再重新执行。"
        )
    if "限流" in detail:
        return (
            "模型服务限流（请求太频繁或额度用尽）。请稍后重试，或到「设置」页更换模型；"
            "已完成的数据保留。"
        )
    if "服务不可用" in detail:
        return (
            "模型服务暂时不可用（服务端错误）。请稍后重试，或到「设置」页更换模型服务；"
            "已完成的数据保留。"
        )
    if "网络请求失败" in detail:
        return (
            "无法连接模型服务（网络不通或 API 地址错误）。"
            "请到「设置」页点「测试连接」验证 API 地址、密钥和模型名后重试；已完成的数据保留。"
        )
    return f"模型服务调用失败：{detail}。请到「设置」页用「测试连接」检查配置后重试。"


class UIFlowManager:
    def __init__(self, factory: sessionmaker[Session], settings: Settings):
        self.factory, self.settings = factory, settings
        self.pending: Queue[str] = Queue(maxsize=8)
        self.stopped = Event()
        self.lock = Lock()
        self.thread: Thread | None = None
        self.active_id: str | None = None
        self._cancel_flags: set[str] = set()
        self.recover()

    def recover(self) -> None:
        try:
            with session_scope(self.factory) as session:
                inspector = inspect(session.get_bind())
                assert inspector is not None
                if not inspector.has_table("run"):
                    return
                rows = session.scalars(
                    select(RunRow).where(
                        RunRow.run_type.in_(["ui.sync", "ui.digest", "ui.extract", "ui.refresh"]),
                        RunRow.status == "running",
                    )
                ).all()
                for row in rows:
                    row.status, row.finished_at = "failed", utc_now()
                    row.stats_json = {**row.stats_json, "updated_at": utc_now().isoformat()}
                    row.errors_json = {
                        "errors": [
                            {
                                "code": "interrupted",
                                "message": (
                                    "应用重启，未完成步骤已停止。已完成的数据保留，请用原参数重试。"
                                ),
                            }
                        ]
                    }
        except SQLAlchemyError:
            logger.warning("UI run recovery unavailable; check database migrations")

    def submit(self, request: FlowRequest) -> str:
        payload = request.model_dump(exclude={"run_id"})
        with self.lock:
            with session_scope(self.factory) as session:
                # 在读预约前取得 SQLite 写锁，跨工作进程同样不会静默排队。
                if session.get_bind().dialect.name == "sqlite":
                    session.execute(text("BEGIN IMMEDIATE"))
                else:
                    session.execute(select(RunRow).with_for_update())
                existing = RunRepository(session).get(request.run_id)
                if existing is not None:
                    if dict(existing.stats_json).get("request") != payload:
                        raise ConflictError("同一运行 ID 不能用于不同操作")
                    return request.run_id
                if running := RunRepository(session).running_ui_runs():
                    raise ConflictError(
                        "已有任务正在运行，完成后再开始新任务", details={"run_id": running[0].id}
                    )
                if request.kind == "extract":
                    if problem := resolve_llm_config(session, self.settings).readiness_problem():
                        raise InvalidRequestError(problem)
                    ensure_draft_stage(session, occurred_at=utc_now())
                    if not materials_text(session).strip():
                        raise InvalidRequestError("请先上传至少一份描述研究方向的材料")
                else:
                    bound_context(session, utc_now(), action="ui." + request.kind)
                if request.kind == "sync" and (
                    problem := processing_problem(session, self.settings)
                ):
                    raise InvalidRequestError(problem)
                if self.pending.full():
                    raise ConflictError("已有较多待运行操作，请等待当前流程完成")
                stats: dict[str, Any] = {
                    "request": payload,
                    "stage": "queued",
                    "completed": [],
                    "updated_at": utc_now().isoformat(),
                }
                if self.active_id is not None:
                    blocking = RunRepository(session).get(self.active_id)
                    if blocking is not None and (
                        blocking.status is RunStatus.CANCELLED or blocking.id in self._cancel_flags
                    ):
                        stats["queued_note"] = (
                            "上一个任务正在停止（进行中的模型调用无法中断），结束后自动开始。"
                        )
                RunRepository(session).add(
                    Run(
                        id=request.run_id,
                        run_type="ui." + request.kind,
                        started_at=utc_now(),
                        stats_json=stats,
                    )
                )
            # Commit the reservation before enqueueing, under the same submission lock.
            self.pending.put_nowait(request.run_id)
            if self.thread is None:
                self.thread = Thread(target=self._loop, daemon=True, name="papersteward-ui")
                self.thread.start()
        return request.run_id

    def close(self) -> None:
        self.stopped.set()
        if self.thread:
            self.thread.join(timeout=1)

    def cancel(self, run_id: str) -> str:
        """请求停止一个运行中的任务；已结束的任务按空操作处理，返回当前状态。

        先记内存标志再尝试落库：worker 可能正持有 SQLite 写事务（处理一篇
        论文要跨多次模型调用），此时落库会等锁超时。落库失败不抛错——
        worker 会在下一个检查点看到标志并补写 CANCELLED。
        """
        with self.lock:
            self._cancel_flags.add(run_id)
            try:
                with session_scope(self.factory) as session:
                    repository = RunRepository(session)
                    run = repository.get(run_id)
                    if run is None:
                        self._cancel_flags.discard(run_id)
                        raise NotFoundError("运行记录不存在")
                    if run.status is not RunStatus.RUNNING:
                        self._cancel_flags.discard(run_id)
                        return run.status.value
                    repository.transition(run_id, RunStatus.CANCELLED, finished_at=utc_now())
                    row = session.get(RunRow, run_id)
                    assert row is not None
                    row.stats_json = {**row.stats_json, "updated_at": utc_now().isoformat()}
                    self._cancel_flags.discard(run_id)
                    return RunStatus.CANCELLED.value
            except SQLAlchemyError:
                logger.warning(
                    "取消落库暂被运行中的事务占用，已请求停止，任务将在检查点结束",
                    extra={"run_id": run_id},
                )
                return "pending"

    def _terminated(self, identity: str) -> bool:
        """任务是否已离开 running 状态（被取消或因其他原因终结）。"""
        with session_scope(self.factory) as session:
            row = session.get(RunRow, identity)
            return row is None or row.status != RunStatus.RUNNING.value

    def _is_stopping(self, identity: str) -> bool:
        """是否已请求停止（无论落库是否成功）或任务已终结。"""
        return identity in self._cancel_flags or self._terminated(identity)

    def _finalize_stop(self, identity: str) -> bool:
        """有停止请求时在其自身事务结束后补写 CANCELLED；返回是否因此结束执行。"""
        if identity not in self._cancel_flags:
            return False
        self._cancel_flags.discard(identity)
        try:
            with session_scope(self.factory) as session:
                run = RunRepository(session).get(identity)
                if run is not None and run.status is RunStatus.RUNNING:
                    RunRepository(session).transition(
                        identity, RunStatus.CANCELLED, finished_at=utc_now()
                    )
        except (SQLAlchemyError, DomainError):
            logger.warning("停止落库未完成；运行状态以数据库为准", extra={"run_id": identity})
        return True

    def _loop(self) -> None:
        while not self.stopped.is_set():
            try:
                identity = self.pending.get(timeout=0.2)
            except Empty:
                continue
            self.active_id = identity
            try:
                self.execute(identity)
            except Exception:
                logger.exception("UI worker could not record failure", extra={"run_id": identity})
            finally:
                self.active_id = None
                self.pending.task_done()

    def _update(self, identity: str, **values: Any) -> None:
        with session_scope(self.factory) as session:
            row = session.get(RunRow, identity)
            assert row is not None
            row.stats_json = {**row.stats_json, **values, "updated_at": utc_now().isoformat()}

    def execute(self, identity: str) -> None:
        with session_scope(self.factory) as session:
            report = RunRepository(session).get(identity)
            if report is None or report.status is not RunStatus.RUNNING:
                return
            request = FlowRequest(run_id=identity, **report.stats_json["request"])
        try:
            if request.kind == "sync":
                self._update(identity, stage="collecting")
                self.collect(request)
                self._update(identity, stage="processing", completed=["collecting"])
                self.process(request)
                self._update(identity, stage="refreshing", completed=["collecting", "processing"])
                self.refresh(request)
                completed = ["collecting", "processing", "refreshing"]
            elif request.kind == "extract":
                self._update(identity, stage="extracting", extract_note="正在读取材料与历史意见…")
                if self._finalize_stop(identity) or self._terminated(identity):
                    return
                with session_scope(self.factory) as session:
                    outcome = run_extraction(
                        session,
                        self.settings,
                        should_abort=lambda: self._is_stopping(identity),
                        note=lambda text: self._update(identity, extract_note=text),
                    )
                self._update(
                    identity,
                    generated={
                        "profile_version": outcome["profile"]["version"],
                        "taxonomy_version": outcome["taxonomy"]["version"],
                    },
                )
                completed = ["extracting"]
            else:
                self._update(identity, stage="refreshing")
                self.refresh(request)
                completed = ["refreshing"]
            self._update(identity, stage="completed", completed=completed)
            if self._finalize_stop(identity) or self._terminated(identity):
                return
            with session_scope(self.factory) as session:
                RunRepository(session).transition(
                    identity, RunStatus.SUCCEEDED, finished_at=utc_now()
                )
        except Exception as exc:
            logger.exception("UI flow failed", extra={"run_id": identity})
            if self._finalize_stop(identity) or self._terminated(identity):
                return
            with session_scope(self.factory) as session:
                row = session.get(RunRow, identity)
                assert row is not None
                row.status, row.finished_at = "failed", utc_now()
                row.stats_json = {**row.stats_json, "updated_at": utc_now().isoformat()}
                row.errors_json = {
                    "errors": [
                        {
                            "code": "step_failed",
                            "message": exc.message
                            if isinstance(exc, DomainError | AppError)
                            else (
                                llm_failure_message(exc)
                                if isinstance(exc, LLMError)
                                else (
                                    "该步骤失败。已完成的数据保留。"
                                    "请检查来源、模型设置和运行时段，然后用原参数重试。"
                                )
                            ),
                        }
                    ]
                }

    def refresh(self, request: FlowRequest) -> None:
        with session_scope(self.factory) as session:
            report = refresh_queue(session, now=utc_now())
        self._update(
            request.run_id,
            queue_total=report.queue_total,
            added_ids=report.added_ids,
            queue_created=report.created,
            queue_existing=report.existing,
            queue_message="暂无合格候选"
            if report.queue_total == 0
            else f"队列共 {report.queue_total} 篇",
        )

    def collect(self, request: FlowRequest) -> None:
        import argparse

        if not request.query.strip():
            raise InvalidRequestError("请填写检索词后再同步")
        clients: list[SourceHTTP] = []
        try:
            adapters = _adapters(
                argparse.Namespace(source=[request.source], fixture=[], live=True),
                self.settings,
                clients,
            )
            with session_scope(self.factory) as session:
                report = ingest(
                    session,
                    adapters,
                    run_id="ui-ingest-" + hashlib.sha256(request.run_id.encode()).hexdigest()[:40],
                    queries=[QuerySpec(keywords=(request.query,), max_results=request.limit)],
                )
            self._update(request.run_id, collected=dict(report.stats_json))
            if report.status is RunStatus.FAILED:
                raise InvalidRequestError("采集失败，请查看来源运行报告")
        finally:
            for client in clients:
                client.close()

    def process(self, request: FlowRequest) -> None:
        with session_scope(self.factory) as session:
            llm_config = resolve_llm_config(session, self.settings)
        if problem := llm_config.readiness_problem():
            raise InvalidRequestError(problem)
        provider = build_llm_provider(llm_config)
        try:
            gateway = LLMGateway(
                provider,
                max_attempts=llm_config.max_attempts,
                timeout_s=llm_config.timeout_s,
            )
            with session_scope(self.factory) as session:
                reconciled = reconcile_legacy_reviews(session, now=utc_now())
                identities = list(
                    session.scalars(
                        select(PaperRow.id)
                        .where(
                            PaperRow.status.in_(
                                [
                                    PaperStatus.SCREENED.value,
                                    PaperStatus.ENRICHED.value,
                                    PaperStatus.SUMMARIZED.value,
                                ]
                            )
                        )
                        .order_by(PaperRow.first_seen_at, PaperRow.id)
                        .limit(request.limit)
                    )
                )
            processed = auto_classified = auto_shelved = skipped_no_summary = 0
            quality_flags: dict[str, list[str]] = {}
            self._update(
                request.run_id,
                processing_total=len(identities),
                processed=0,
                current_index=0,
                current_paper_title=None,
                reconciled=reconciled,
                auto_classified=0,
                auto_shelved=0,
                skipped_no_summary=0,
            )
            for index, paper_id in enumerate(identities):
                if self.stopped.is_set():
                    raise InvalidRequestError("应用正在停止")
                if self._is_stopping(request.run_id):
                    raise InvalidRequestError("任务已停止")
                with session_scope(self.factory) as session:
                    title = session.scalar(select(PaperRow.title).where(PaperRow.id == paper_id))
                self._update(request.run_id, current_index=index + 1, current_paper_title=title)
                with session_scope(self.factory) as session:
                    now = utc_now()
                    profile, taxonomy, scoring, buckets, day = _processor_context(session, now)
                    paper = PaperRepository(session).get(paper_id)
                    assert paper is not None
                    outcome = process_paper(
                        session,
                        paper,
                        profile=profile,
                        taxonomy=taxonomy,
                        config=scoring,
                        topic_buckets=buckets,
                        gateway=gateway,
                        embedding_provider=HashingEmbeddingProvider(),
                        now=now,
                        score_date=day,
                    )
                processed = auto_classified = index + 1
                auto_shelved += int(outcome.auto_shelved)
                skipped_no_summary += int(
                    outcome.summary is None or not outcome.summary.summary.summary_100.strip()
                )
                if outcome.quality_flags:
                    quality_flags[paper_id] = list(outcome.quality_flags)
                self._update(
                    request.run_id,
                    processed=processed,
                    auto_classified=auto_classified,
                    auto_shelved=auto_shelved,
                    skipped_no_summary=skipped_no_summary,
                    quality_flags=quality_flags,
                )
            self._update(request.run_id, current_paper_title=None)
        finally:
            close = getattr(provider, "close", None)
            if callable(close):
                close()
