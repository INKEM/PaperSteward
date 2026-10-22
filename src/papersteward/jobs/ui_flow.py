"""Single local worker with durable Run progress; completed steps commit independently."""

from __future__ import annotations

import hashlib
import logging
from queue import Empty, Queue
from threading import Event, Lock, Thread
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import inspect, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from papersteward.cli.ingestion import _adapters
from papersteward.cli.processing import _processor_context
from papersteward.config import LLMProviderKind, Settings
from papersteward.core.entities import Run, utc_now
from papersteward.core.enums import PaperStatus, RunStatus
from papersteward.core.errors import DomainError
from papersteward.delivery.service import create_digest
from papersteward.errors import AppError, ConflictError, InvalidRequestError
from papersteward.llm import LLMGateway, build_llm_provider
from papersteward.pipeline.context import bound_context, runtime_local_day
from papersteward.pipeline.embeddings import HashingEmbeddingProvider
from papersteward.pipeline.ingest import ingest
from papersteward.pipeline.process import process_paper
from papersteward.sources.contracts import QuerySpec
from papersteward.sources.http import SourceHTTP
from papersteward.storage import session_scope
from papersteward.storage.models import PaperRow, RunRow
from papersteward.storage.repositories import ConfigRepository, PaperRepository, RunRepository

logger = logging.getLogger(__name__)


class FlowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    kind: Literal["sync", "digest"]
    source: Literal["arxiv", "openalex", "semantic_scholar"] = "arxiv"
    query: str = Field(default="retrieval augmented generation", min_length=1, max_length=500)
    limit: int = Field(default=10, ge=1, le=50)
    allow_external: bool = Field(default=False, strict=True)


def processing_problem(session: Session, settings: Settings, binding: dict[str, Any]) -> str | None:
    runtime = ConfigRepository(session).get("onboarding_runtime", binding["runtime_version"])
    assert runtime is not None
    if runtime.value_json["external_model_data"] != "metadata_abstract":
        return "初始化未授权论文元数据/摘要外发。请用新工作区重新设置并明确授权。"
    if settings.llm_provider is LLMProviderKind.FAKE or not settings.llm_model:
        return "尚未配置真实 LLM。请配置 PAPERSTEWARD_LLM_PROVIDER/MODEL 并重启。"
    if (
        urlsplit(settings.llm_base_url).hostname not in {"localhost", "127.0.0.1", "::1"}
        and not settings.llm_api_key
    ):
        return "缺少 PAPERSTEWARD_LLM_API_KEY，请配置后重启；本机模型可不需要 key。"
    return None


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
                        RunRow.run_type.in_(["ui.sync", "ui.digest"]), RunRow.status == "running"
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
                _, binding = bound_context(session, utc_now(), action="ui." + request.kind)
                if request.kind == "sync":
                    if not request.allow_external:
                        raise InvalidRequestError("请明确勾选允许访问来源并调用配置的模型")
                    if problem := processing_problem(session, self.settings, binding):
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
                                "该步骤失败。已完成的数据保留。"
                                "请检查来源/模型/运行时段后显式重试，也可先处理审核项。"
                            ),
                        }
                    ]
                }

    def collect(self, request: FlowRequest) -> None:
        import argparse

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
        provider = build_llm_provider(self.settings, live=True)
        try:
            gateway = LLMGateway(
                provider,
                max_attempts=self.settings.llm_max_attempts,
                timeout_s=self.settings.llm_timeout_s,
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
                    profile, taxonomy, config, buckets, day = _processor_context(session, now)
                    paper = PaperRepository(session).get(paper_id)
                    assert paper is not None
                    process_paper(
                        session,
                        paper,
                        profile=profile,
                        taxonomy=taxonomy,
                        config=config,
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
