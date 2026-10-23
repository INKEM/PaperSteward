"""Single local worker with durable Run progress; completed steps commit independently."""

from __future__ import annotations

import hashlib
import logging
from queue import Empty, Queue
from threading import Event, Lock, Thread
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import inspect, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from papersteward.cli.ingestion import _adapters
from papersteward.cli.processing import _processor_context
from papersteward.config import Settings
from papersteward.core.entities import Run, utc_now
from papersteward.core.enums import PaperStatus, RunStatus
from papersteward.core.errors import DomainError
from papersteward.delivery.service import create_digest
from papersteward.errors import AppError, ConflictError, InvalidRequestError
from papersteward.llm import LLMGateway, build_llm_provider
from papersteward.llm.errors import LLMError, LLMValidationError
from papersteward.llm.runtime import resolve_llm_config
from papersteward.onboarding.intake import ensure_draft_stage, materials_text, run_extraction
from papersteward.pipeline.context import bound_context, runtime_local_day
from papersteward.pipeline.embeddings import HashingEmbeddingProvider
from papersteward.pipeline.ingest import ingest
from papersteward.pipeline.process import process_paper
from papersteward.sources.contracts import QuerySpec
from papersteward.sources.http import SourceHTTP
from papersteward.storage import session_scope
from papersteward.storage.models import PaperRow, RunRow
from papersteward.storage.repositories import PaperRepository, RunRepository

logger = logging.getLogger(__name__)


class FlowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    kind: Literal["sync", "digest", "extract"]
    source: Literal["arxiv", "openalex", "semantic_scholar"] = "arxiv"
    query: str = Field(default="", max_length=500)
    limit: int = Field(default=10, ge=1, le=50)


def processing_problem(session: Session, settings: Settings) -> str | None:
    """同步/处理前置检查：模型服务就绪即可运行；授权随设置页保存。"""
    return resolve_llm_config(session, settings).readiness_problem()


def llm_failure_message(exc: LLMError) -> str:
    """把网关/provider 错误转成用户可操作的中文提示，不包含 prompt 或密钥。"""
    text = str(exc)
    if isinstance(exc, LLMValidationError):
        return f"模型返回不符合要求：{text}。可以重试，或在「设置」页换一个模型。"
    if "认证失败" in text:
        return (
            "模型认证失败：API 密钥或地址不正确。"
            "请到「设置」页检查密钥后点「测试连接」，再重试同步。"
        )
    if "超时" in text:
        return "模型请求超时。可到「设置」页增大超时时间，或稍后重试；已完成数据保留。"
    return (
        f"模型服务调用失败：{text}。请到「设置」页用「测试连接」检查 API 地址、密钥和模型名后重试。"
    )


class UIFlowManager:
    def __init__(self, factory: sessionmaker[Session], settings: Settings):
        self.factory, self.settings = factory, settings
        self.pending: Queue[str] = Queue(maxsize=8)
        self.stopped = Event()
        self.lock = Lock()
        self.thread: Thread | None = None
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
                        RunRow.run_type.in_(["ui.sync", "ui.digest", "ui.extract"]),
                        RunRow.status == "running",
                    )
                ).all()
                for row in rows:
                    row.status, row.finished_at = "failed", utc_now()
                    row.errors_json = {
                        "errors": [
                            {
                                "code": "interrupted",
                                "message": (
                                    "应用重启，未完成步骤已停止。已完成的数据保留，请显式重试。"
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
                existing = RunRepository(session).get(request.run_id)
                if existing is not None:
                    if dict(existing.stats_json).get("request") != payload:
                        raise ConflictError("同一运行 ID 不能用于不同操作")
                    return request.run_id
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
                RunRepository(session).add(
                    Run(
                        id=request.run_id,
                        run_type="ui." + request.kind,
                        started_at=utc_now(),
                        stats_json={"request": payload, "stage": "queued", "completed": []},
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

    def _loop(self) -> None:
        while not self.stopped.is_set():
            try:
                identity = self.pending.get(timeout=0.2)
            except Empty:
                continue
            try:
                self.execute(identity)
            except Exception:
                logger.exception("UI worker could not record failure", extra={"run_id": identity})
            finally:
                self.pending.task_done()

    def _update(self, identity: str, **values: Any) -> None:
        with session_scope(self.factory) as session:
            row = session.get(RunRow, identity)
            assert row is not None
            row.stats_json = {**row.stats_json, **values}

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
                completed = ["collecting", "processing"]
            elif request.kind == "extract":
                self._update(identity, stage="extracting")
                with session_scope(self.factory) as session:
                    outcome = run_extraction(session, self.settings)
                self._update(
                    identity,
                    generated={
                        "profile_version": outcome["profile"]["version"],
                        "taxonomy_version": outcome["taxonomy"]["version"],
                    },
                )
                completed = ["extracting"]
            else:
                self._update(identity, stage="digest")
                with session_scope(self.factory) as session:
                    now = utc_now()
                    _, binding = bound_context(session, now, action="ui.digest")
                    digest = create_digest(
                        session, runtime_local_day(session, now, binding), now=now
                    )
                    if not digest.items:
                        raise InvalidRequestError(
                            "暂无合格候选。请先同步/处理并解决审核项，再生成日报；本次未保存空日报。"
                        )
                completed = ["digest"]
            self._update(identity, stage="completed", completed=completed)
            with session_scope(self.factory) as session:
                RunRepository(session).transition(
                    identity, RunStatus.SUCCEEDED, finished_at=utc_now()
                )
        except Exception as exc:
            logger.exception("UI flow failed", extra={"run_id": identity})
            with session_scope(self.factory) as session:
                row = session.get(RunRow, identity)
                assert row is not None
                row.status, row.finished_at = "failed", utc_now()
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
                                    "请检查来源/模型/运行时段后显式重试，也可先处理审核项。"
                                )
                            ),
                        }
                    ]
                }

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
                identities = list(
                    session.scalars(
                        select(PaperRow.id)
                        .where(PaperRow.status == PaperStatus.SCREENED.value)
                        .order_by(PaperRow.first_seen_at, PaperRow.id)
                        .limit(request.limit)
                    )
                )
            for index, paper_id in enumerate(identities):
                if self.stopped.is_set():
                    raise InvalidRequestError("应用正在停止")
                with session_scope(self.factory) as session:
                    now = utc_now()
                    profile, taxonomy, scoring, buckets, day = _processor_context(session, now)
                    paper = PaperRepository(session).get(paper_id)
                    assert paper is not None
                    process_paper(
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
                self._update(request.run_id, processed=index + 1, processing_total=len(identities))
        finally:
            close = getattr(provider, "close", None)
            if callable(close):
                close()
