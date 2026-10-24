"""Single local worker with durable Run progress; completed steps commit independently."""

from __future__ import annotations

import hashlib
import logging
import time
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import date
from queue import Empty, Queue
from threading import Event, Lock, Thread
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from papersteward.config import Settings
from papersteward.core.entities import Bucket, ResearchProfile, Run, Taxonomy, utc_now
from papersteward.core.enums import PaperStatus, RunStatus
from papersteward.core.errors import DomainError, DomainValidationError
from papersteward.errors import AppError, ConflictError, InvalidRequestError, NotFoundError
from papersteward.llm import LLMGateway, build_llm_provider
from papersteward.llm.cache import DatabaseLLMCache
from papersteward.llm.errors import LLMError, LLMTimeoutError, LLMValidationError
from papersteward.llm.runtime import LLMRuntimeConfig, resolve_llm_config
from papersteward.onboarding.intake import ensure_draft_stage, materials_text, run_extraction
from papersteward.pipeline.context import bound_context
from papersteward.pipeline.embeddings import HashingEmbeddingProvider
from papersteward.pipeline.ingest import ingest
from papersteward.pipeline.process import (
    ProcessOutcome,
    compute_enrichment,
    ensure_topic_buckets,
    persist_enrichment_results,
    persist_triage,
    reconcile_legacy_reviews,
)
from papersteward.pipeline.processor import processor_context
from papersteward.pipeline.relevance import (
    RelevanceResult,
    evaluate_relevance,
    extract_profile_facets,
)
from papersteward.scoring.config import ScoringConfig
from papersteward.scoring.queue import refresh_queue
from papersteward.scoring.service import read_scoring_config
from papersteward.sources.contracts import describe_source_error, display_source_name
from papersteward.sources.http import SourceHTTP
from papersteward.sources.planner import QueryPlan, plan_queries
from papersteward.sources.registry import build_adapters
from papersteward.storage import session_scope
from papersteward.storage.models import PaperRow, RunRow
from papersteward.storage.repositories import (
    PaperAnalysisRepository,
    PaperRepository,
    RunRepository,
)

logger = logging.getLogger(__name__)

# 挖掘漏斗：规划轮数、初筛候选上限系数与每波深处理篇数系数。
MAX_PLAN_ROUNDS = 3
TRIAGE_POOL_FACTOR = 4
TRIAGE_POOL_MINIMUM = 24
ENRICH_WAVE_FACTOR = 2
# 进度节流：每篇论文的进度写与停止检查不必每次都落库/查库；阶段切换与最后一篇强制。
PROGRESS_MIN_INTERVAL_S = 0.5
STOP_CHECK_MIN_INTERVAL_S = 0.3


@dataclass(frozen=True)
class _RunContext:
    """一次 sync 的只读处理上下文（E-05）：运行开始加载一次，全程复用。

    运行内这些依据本就不可变（档案激活后只能重新生成；评分治理变更在下一次
    运行生效；主题桶首波落库后映射不变），冻结到运行开始时刻让整批论文用同一
    依据，也省去逐篇重读 profile/分类法/评分配置（约 8 条查询/篇）。跨午夜的
    评分日期统一取运行开始的本地日。
    """

    profile: ResearchProfile
    taxonomy: Taxonomy
    scoring: ScoringConfig
    buckets: dict[str, Bucket]
    day: date
    embedding_provider: HashingEmbeddingProvider
    facet_vectors: tuple[tuple[float, ...], ...]


class FlowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    kind: Literal["sync", "refresh", "extract"]
    source: Literal["arxiv", "openalex", "semantic_scholar"] = "arxiv"
    # 可选关注点：作为品味信号喂给检索规划 Agent；不填则纯按档案与已读历史决定。
    focus: str = Field(default="", max_length=500)
    # 期望入队篇数；0 表示使用评分配置中的 min_queue_yield 默认值。
    target: int = Field(default=0, ge=0, le=50)


def processing_problem(session: Session, settings: Settings) -> str | None:
    """同步/处理前置检查：模型服务就绪即可运行；授权随设置页保存。"""
    return resolve_llm_config(session, settings).readiness_problem()


def _stat_number(value: Any) -> int:
    """运行统计里的计数防御性转 int（stats 来自 JSON，可能有 None/字符串）。"""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def ingest_run_id(ui_run_id: str, round_index: int) -> str:
    """一轮扩查采集的来源运行 ID：同一（挖掘, 轮次）确定，跨轮唯一。"""
    digest = hashlib.sha256(f"{ui_run_id}:{round_index}".encode()).hexdigest()[:40]
    return "ui-ingest-" + digest


def _collection_failure_message(request: FlowRequest, report: Run) -> str:
    """采集失败时把来源报告里的真实原因翻译成可操作的中文提示。

    ingest run 的 errors_json={"sources":[{source, code, retryable}, ...]}；
    文案说清「发生了什么 + 下一步做什么」，并指向运行报告页。
    """
    sources = report.errors_json.get("sources", []) if report.errors_json else []
    reasons = [
        f"{display_source_name(str(item.get('source', '来源')))}"
        f" {describe_source_error(str(item.get('code', '')))}"
        for item in sources
        if isinstance(item, dict) and item.get("stage") != "persist"
    ][:2]
    retryable = any(isinstance(item, dict) and item.get("retryable") for item in sources)
    unsupported_only = bool(sources) and all(
        isinstance(item, dict) and item.get("code") == "unsupported_query" for item in sources
    )
    if reasons:
        head = "采集失败：" + "；".join(reasons) + "。"
    elif any(isinstance(item, dict) and item.get("stage") == "persist" for item in sources):
        head = "采集完成但入库失败：已采集数据不落库，原数据不受影响。"
        retryable = True
    else:
        head = "采集失败：来源没有返回可用结果。"
    if unsupported_only:
        tail = "用「原参数重试」会自动换一批检索式；仍失败可换一个来源再挖。"
    elif retryable:
        tail = "稍等一两分钟再「用原参数重试」，或换一个来源再挖。"
    else:
        tail = "可「用原参数重试」，或换一个来源再挖；运行结束后的报告页有来源详情。"
    return f"{head}{tail}"


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
        self._last_progress_at: dict[str, float] = {}
        self._stop_check_cache: dict[str, tuple[float, bool]] = {}
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
        """是否已请求停止（无论落库是否成功）或任务已终结；带短窗缓存。"""
        if identity in self._cancel_flags:
            return True
        now = time.monotonic()
        cached = self._stop_check_cache.get(identity)
        if cached is not None and now - cached[0] < STOP_CHECK_MIN_INTERVAL_S:
            return cached[1]
        terminated = self._terminated(identity)
        self._stop_check_cache[identity] = (now, terminated)
        return terminated

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
                self._last_progress_at.pop(identity, None)
                self._stop_check_cache.pop(identity, None)
                self.pending.task_done()

    def _update(self, identity: str, **values: Any) -> None:
        # immediate：进度写与并行 worker 的短写事务竞争时直接拿写锁等待，
        # 避免"读 run 行→写回"的升级路径在 busy timeout 之外立即失败。
        with session_scope(self.factory, immediate=True) as session:
            row = session.get(RunRow, identity)
            assert row is not None
            row.stats_json = {**row.stats_json, **values, "updated_at": utc_now().isoformat()}

    def _progress(self, identity: str, *, force: bool = False, **values: Any) -> None:
        """单篇维度的进度写：2s 轮询下节流到 PROGRESS_MIN_INTERVAL_S 即可感知。

        阶段切换走 _update（无条件写）；force 用于批次最后一篇，保证完成态必落库。
        """
        now = time.monotonic()
        if not force and now - self._last_progress_at.get(identity, 0.0) < PROGRESS_MIN_INTERVAL_S:
            return
        self._last_progress_at[identity] = now
        self._update(identity, **values)

    def execute(self, identity: str) -> None:
        with session_scope(self.factory) as session:
            report = RunRepository(session).get(identity)
            if report is None or report.status is not RunStatus.RUNNING:
                return
            request = FlowRequest(run_id=identity, **report.stats_json["request"])
        try:
            if request.kind == "sync":
                self._run_sync(request)
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
                            "message": (
                                # 字段级校验错误附带定位，避免“必须带时区”这类
                                # 无上下文消息；其余领域错误文案本身已可操作。
                                f"{exc.message}（{exc.field}）"
                                if isinstance(exc, DomainValidationError)
                                else exc.message
                            )
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

    def _target_and_floor(self) -> tuple[int, float]:
        """期望入队篇数与保底填充下限；请求未指定时取评分配置默认。"""
        with session_scope(self.factory) as session:
            config = read_scoring_config(session)
        if config is None:
            return 5, 40.0
        return int(config.gates.min_queue_yield), float(config.gates.quota_fill_relevance_floor)

    def _build_gateway(self) -> tuple[LLMGateway, Any, LLMRuntimeConfig]:
        with session_scope(self.factory) as session:
            llm_config = resolve_llm_config(session, self.settings)
        if problem := llm_config.readiness_problem():
            raise InvalidRequestError(problem)
        provider = build_llm_provider(llm_config)
        cache = DatabaseLLMCache(self.factory) if llm_config.cache_enabled else None
        gateway = LLMGateway(
            provider,
            max_attempts=llm_config.max_attempts,
            timeout_s=llm_config.timeout_s,
            # 429/5xx 指数退避（1s、2s…）：立即重发会加剧限流，反而拉长整批耗时。
            backoff_s=1.0,
            cache=cache,
        )
        return gateway, provider, llm_config

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

    def collect(self, request: FlowRequest, plan: QueryPlan) -> Run | None:
        """执行一轮采集；返回 ingest 报告（测试桩可能返回 None，调用方判空）。

        每轮扩查用独立的来源运行 ID（同轮幂等、跨轮唯一），否则第二轮会撞上
        ingest 的 run_id 唯一性保护（“运行ID已存在”）导致整批挖掘失败。
        """
        clients: list[SourceHTTP] = []
        ingest_id = ingest_run_id(request.run_id, plan.round_index)
        # 规划完成后进入实际抓取：多条检索式逐条请求（间隔 3 秒），相位如实标注。
        self._update(request.run_id, collecting_phase="fetching")
        try:
            adapters = build_adapters([request.source], self.settings, clients, live=True)
            with session_scope(self.factory) as session:
                report = ingest(
                    session,
                    adapters,
                    run_id=ingest_id,
                    queries=[planned.spec for planned in plan.queries],
                )
            self._update(request.run_id, ingest_run_id=ingest_id)
            if report.status is RunStatus.FAILED:
                raise InvalidRequestError(_collection_failure_message(request, report))
            return report
        finally:
            for client in clients:
                client.close()

    def _plan(
        self,
        request: FlowRequest,
        gateway: LLMGateway,
        target: int,
        *,
        round_index: int,
        used_keywords: str = "",
        feedback: str = "",
    ) -> QueryPlan:
        # 检索规划是一次真实模型调用，可能耗时数十秒：相位先落库再调用。
        self._update(request.run_id, collecting_phase="planning")
        with session_scope(self.factory) as session:
            plan = plan_queries(
                session,
                gateway,
                focus_hint=request.focus,
                target=target,
                round_index=round_index,
                used_keywords=used_keywords,
                round_feedback=feedback,
            )
        self._update(request.run_id, query_plan=plan.as_json())
        return plan

    def _run_context(self) -> _RunContext:
        """加载一次只读处理上下文并预计算 profile 片段向量（E-05/E-07）。"""
        with session_scope(self.factory) as session:
            profile, taxonomy, scoring, buckets, day = processor_context(
                session, utc_now(), ensure_buckets=False
            )
        provider = HashingEmbeddingProvider()
        facets = extract_profile_facets(profile)
        facet_vectors = provider.embed([facet.text for facet in facets])
        return _RunContext(
            profile=profile,
            taxonomy=taxonomy,
            scoring=scoring,
            buckets=dict(buckets),
            day=day,
            embedding_provider=provider,
            facet_vectors=facet_vectors,
        )

    def _triage_one(self, gateway: LLMGateway, paper_id: str, ctx: _RunContext) -> RelevanceResult:
        """单篇初筛：读论文 → 模型评估（上下文与片段向量来自运行快照）→ 短写落分析。

        读阶段完全不写库（不建主题桶），模型计算在事务外；写阶段从第一句就
        持有 SQLite 写锁，避免读-写升级撞 BUSY_SNAPSHOT。
        """
        with session_scope(self.factory) as session:
            paper = PaperRepository(session).get(paper_id)
            assert paper is not None
        relevance = evaluate_relevance(
            ctx.profile,
            paper,
            gateway,
            ctx.embedding_provider,
            facet_vectors=ctx.facet_vectors,
        )
        with session_scope(self.factory, immediate=True) as session:
            persist_triage(session, paper.id, ctx.profile, relevance, now=utc_now())
        return relevance

    def _triage_candidates(
        self,
        request: FlowRequest,
        gateway: LLMGateway,
        cap: int,
        workers: int,
        ctx: _RunContext,
    ) -> tuple[list[RelevanceResult], int]:
        """对 screened 候选并行初筛；返回按 R 降序的结果与无摘要跳过数。

        初筛只做相关度评估（每篇一次模型调用）；注定入不了队的论文在此止步，
        省去摘要/分类/特殊桶三次调用。写库只发生在评估之后，缩短写锁持有。
        扩查轮次会再次全量取 SCREENED：同一档案版本已初筛过的论文直接跳过
        （E-06，结果不变——排名合并本就按 paper_id 去重），省去重复调用路径。
        """
        with session_scope(self.factory) as session:
            reconciled = reconcile_legacy_reviews(session, now=utc_now())
            rows = session.execute(
                select(PaperRow.id, PaperRow.title, PaperRow.abstract)
                .where(PaperRow.status == PaperStatus.SCREENED.value)
                .order_by(PaperRow.published_at.desc().nullslast(), PaperRow.first_seen_at)
            ).all()
            analyzed = PaperAnalysisRepository(session).get_many([row[0] for row in rows])
        with_abstract = [
            (paper_id, title) for paper_id, title, abstract in rows if abstract and abstract.strip()
        ]
        skipped = len(rows) - len(with_abstract)

        def _already_triaged(paper_id: str) -> bool:
            analysis = analyzed.get(paper_id)
            if analysis is None:
                return False
            payload = analysis.payload_json.get("relevance")
            return (
                isinstance(payload, dict)
                and payload.get("profile_id") == ctx.profile.id
                and payload.get("profile_version") == ctx.profile.version
            )

        selected = [
            (paper_id, title)
            for paper_id, title in with_abstract[:cap]
            if not _already_triaged(paper_id)
        ]
        if selected:
            self._update(
                request.run_id,
                reconciled=reconciled,
                processing_phase="triage",
                processing_total=len(selected),
                processed=0,
                current_index=0,
                current_paper_title=None,
                skipped_no_abstract=skipped,
            )
        else:
            # 空轮不重置进度统计：后续轮次的 triage 不应抹掉已完成的深处理计数。
            self._update(request.run_id, reconciled=reconciled, skipped_no_abstract=skipped)
        results: list[RelevanceResult] = []
        if selected:
            with ThreadPoolExecutor(
                max_workers=min(workers, len(selected)), thread_name_prefix="ps-triage"
            ) as pool:
                futures = {
                    pool.submit(self._triage_one, gateway, paper_id, ctx): (paper_id, title)
                    for paper_id, title in selected
                }
                pending = set(futures)
                failure: BaseException | None = None
                while pending:
                    done, pending = wait(pending, return_when=FIRST_EXCEPTION)
                    for future in done:
                        paper_id, title = futures[future]
                        if failure is not None:
                            continue
                        try:
                            error = future.exception()
                        except BaseException as exc:  # noqa: BLE001 - 已取消等异常按失败处理
                            error = exc
                        if error is not None or self._is_stopping(request.run_id):
                            failure = error or InvalidRequestError("任务已停止")
                            continue
                        results.append(future.result())
                        self._progress(
                            request.run_id,
                            force=len(results) == len(selected),
                            processed=len(results),
                            current_index=len(results),
                            current_paper_title=title,
                        )
                    if failure is not None:
                        for future in pending:
                            future.cancel()
                        assert failure is not None
                        raise failure
        results.sort(key=lambda item: -item.R)
        return results, skipped

    def _enrich_one(
        self,
        gateway: LLMGateway,
        paper_id: str,
        relevance: RelevanceResult,
        ctx: _RunContext,
    ) -> ProcessOutcome:
        """单篇深处理：读论文 → 三次模型计算（上下文来自运行快照）→ 短写事务落库。"""
        with session_scope(self.factory) as session:
            paper = PaperRepository(session).get(paper_id)
            assert paper is not None
        plan = compute_enrichment(
            paper,
            profile=ctx.profile,
            taxonomy=ctx.taxonomy,
            topic_buckets=ctx.buckets,
            gateway=gateway,
        )
        with session_scope(self.factory, immediate=True) as session:
            ensure_topic_buckets(session, ctx.taxonomy)
            paper = PaperRepository(session).get(paper_id)
            assert paper is not None
            return persist_enrichment_results(
                session,
                paper,
                plan=plan,
                profile=ctx.profile,
                taxonomy=ctx.taxonomy,
                config=ctx.scoring,
                relevance=relevance,
                now=utc_now(),
                score_date=ctx.day,
            )

    def _enrich_wave(
        self,
        request: FlowRequest,
        gateway: LLMGateway,
        batch: list[RelevanceResult],
        workers: int,
        ctx: _RunContext,
    ) -> tuple[int, int, dict[str, list[str]]]:
        """对初筛入选的一批论文并行深处理；返回 (auto_shelved, skipped_no_summary, flags)。"""
        self._update(
            request.run_id,
            processing_phase="enrich",
            processing_total=len(batch),
            processed=0,
            current_index=0,
        )
        outcomes: list[ProcessOutcome] = []
        titles: dict[str, str] = {}
        with session_scope(self.factory) as session:
            for item in batch:
                title = session.scalar(select(PaperRow.title).where(PaperRow.id == item.paper_id))
                titles[item.paper_id] = str(title or "")
        with ThreadPoolExecutor(
            max_workers=min(workers, len(batch)), thread_name_prefix="ps-enrich"
        ) as pool:
            futures = {
                pool.submit(self._enrich_one, gateway, item.paper_id, item, ctx): item.paper_id
                for item in batch
            }
            pending = set(futures)
            failure: BaseException | None = None
            while pending:
                done, pending = wait(pending, return_when=FIRST_EXCEPTION)
                for future in done:
                    paper_id = futures[future]
                    if failure is not None:
                        continue
                    try:
                        error = future.exception()
                    except BaseException as exc:  # noqa: BLE001
                        error = exc
                    if error is not None or self._is_stopping(request.run_id):
                        failure = error or InvalidRequestError("任务已停止")
                        continue
                    outcomes.append(future.result())
                    self._progress(
                        request.run_id,
                        force=len(outcomes) == len(batch),
                        processed=len(outcomes),
                        current_index=len(outcomes),
                        current_paper_title=titles.get(paper_id),
                    )
                if failure is not None:
                    for future in pending:
                        future.cancel()
                    assert failure is not None
                    raise failure
        auto_shelved = sum(outcome.auto_shelved for outcome in outcomes)
        skipped_no_summary = sum(
            1
            for outcome in outcomes
            if outcome.summary is None or not outcome.summary.summary.summary_100.strip()
        )
        flags: dict[str, list[str]] = {}
        for outcome in outcomes:
            if outcome.quality_flags:
                flags[outcome.paper_id] = list(outcome.quality_flags)
        return auto_shelved, skipped_no_summary, flags

    def _refresh_for_sync(
        self, request: FlowRequest, *, target: int, floor: float, fill: bool
    ) -> set[str]:
        with session_scope(self.factory) as session:
            report = refresh_queue(
                session,
                now=utc_now(),
                min_queue_yield=target if fill else None,
                relevance_floor=floor,
            )
        self._update(
            request.run_id,
            queue_total=report.queue_total,
            added_ids=report.added_ids,
            queue_created=report.created,
            queue_existing=report.existing,
            quota_filled=report.quota_filled,
            queue_message="暂无合格候选"
            if report.queue_total == 0
            else f"队列共 {report.queue_total} 篇",
        )
        return set(report.added_ids)

    def _run_sync(self, request: FlowRequest) -> None:
        """挖掘漏斗：Agent 检索规划 → 初筛排序 → 按名次深处理 → 配额循环 → 保底填充。

        目标是每次至少入队 ``target`` 篇：先深处理最相关的候选，正常门槛下不足时
        继续处理次优候选，候选耗尽则追加一轮检索规划；全部耗尽后才以
        ``quota_fill`` 标记降门槛补足，仍不足则如实报告缺口。
        """
        gateway, provider, llm_config = self._build_gateway()
        try:
            ctx = self._run_context()
            configured_target, floor = self._target_and_floor()
            target = request.target or configured_target or 5
            if target <= 0:
                raise InvalidRequestError("期望入队篇数必须大于 0")
            triage_cap = max(target * TRIAGE_POOL_FACTOR, TRIAGE_POOL_MINIMUM)
            wave_size = max(target * ENRICH_WAVE_FACTOR, target + 3)
            workers = max(1, llm_config.concurrency)
            ranked: list[RelevanceResult] = []
            added: set[str] = set()
            used_keywords = ""
            skipped_no_abstract_total = 0
            totals = {"enriched": 0, "auto_shelved": 0, "skipped_no_summary": 0}
            quality_flags: dict[str, list[str]] = {}
            plans: list[dict[str, Any]] = []
            # 采集统计跨轮累计（各轮求和），报告链接逐轮记录；collected 若按轮
            # 覆盖，扩查后终态摘要会从第 1 轮数字“跳变”成末轮数字。
            collected_totals: dict[str, int] = {}
            partial_any = False
            ingest_ids: list[str] = []
            for round_index in range(1, MAX_PLAN_ROUNDS + 1):
                if self.stopped.is_set():
                    raise InvalidRequestError("应用正在停止")
                if round_index == 1:
                    # 真正的“排队”只是等待 worker 取任务（通常瞬间完成）；取到后
                    # 规划与采集都是实际工作，立即离开 queued 以免误标为排队。
                    self._update(request.run_id, stage="collecting")
                else:
                    self._update(request.run_id, stage="collecting", completed=["collecting"])
                plan = self._plan(
                    request,
                    gateway,
                    target,
                    round_index=round_index,
                    used_keywords=used_keywords,
                    feedback=(
                        "" if round_index == 1 else f"已入队 {len(added)} / 目标 {target} 篇"
                    ),
                )
                plans.append(plan.as_json())
                self._update(request.run_id, plans=plans, round=round_index)
                used_keywords = "、".join(filter(None, (used_keywords, plan.keyword_digest())))
                report = self.collect(request, plan)
                if report is not None:
                    round_stats = dict(report.stats_json)
                    for key in (
                        "candidates",
                        "new",
                        "existing",
                        "deduplicated",
                        "source_versions_added",
                    ):
                        collected_totals[key] = collected_totals.get(key, 0) + _stat_number(
                            round_stats.get(key, 0)
                        )
                    filtered_round = round_stats.get("filtered")
                    collected_totals["filtered"] = collected_totals.get("filtered", 0) + (
                        len(filtered_round)
                        if isinstance(filtered_round, list)
                        else _stat_number(round_stats.get("filtered", 0))
                    )
                    partial_any = partial_any or bool(round_stats.get("partial"))
                    ingest_ids.append(ingest_run_id(request.run_id, plan.round_index))
                    self._update(
                        request.run_id,
                        collected={**collected_totals, "partial": partial_any},
                        ingest_run_ids=list(ingest_ids),
                    )
                self._update(request.run_id, stage="processing", completed=["collecting"])
                new_results, skipped = self._triage_candidates(
                    request, gateway, triage_cap, workers, ctx
                )
                skipped_no_abstract_total += skipped
                seen = {item.paper_id for item in ranked}
                ranked = sorted(
                    [*ranked, *(item for item in new_results if item.paper_id not in seen)],
                    key=lambda item: -item.R,
                )
                while ranked and len(added) < target:
                    wave = ranked[:wave_size]
                    del ranked[:wave_size]
                    auto_shelved, skipped_no_summary, flags = self._enrich_wave(
                        request, gateway, wave, workers, ctx
                    )
                    totals["enriched"] += len(wave)
                    totals["auto_shelved"] += auto_shelved
                    totals["skipped_no_summary"] += skipped_no_summary
                    quality_flags.update(flags)
                    self._update(
                        request.run_id,
                        processed=totals["enriched"],
                        # 波内 total 是本波批量；波后回写累计值，终态不再出现
                        # “已处理 24 / 4 篇”这类分子大于分母的矛盾显示。
                        processing_total=totals["enriched"],
                        auto_classified=totals["enriched"],
                        auto_shelved=totals["auto_shelved"],
                        skipped_no_summary=totals["skipped_no_summary"],
                        quality_flags=quality_flags,
                        current_paper_title=None,
                    )
                    added |= self._refresh_for_sync(request, target=target, floor=floor, fill=False)
                    if self._is_stopping(request.run_id):
                        raise InvalidRequestError("任务已停止")
                if len(added) < target:
                    # 本轮候选处理完（或为空）也要刷新一次：collect 入库的合格论文
                    # （如已分类文献）不依赖深处理也能入队。
                    added |= self._refresh_for_sync(request, target=target, floor=floor, fill=False)
                    if self._is_stopping(request.run_id):
                        raise InvalidRequestError("任务已停止")
                if len(added) >= target:
                    break
            shortfall = max(0, target - len(added))
            if shortfall:
                added |= self._refresh_for_sync(request, target=target, floor=floor, fill=True)
            self._update(
                request.run_id,
                stage="refreshing",
                completed=["collecting", "processing"],
                current_paper_title=None,
                target=target,
                # added_ids 用整次运行各轮刷新的并集：单轮刷新的报告会被后续轮覆盖。
                added_ids=sorted(added),
                queued_this_run=len(added),
                shortfall=max(0, target - len(added)),
                skipped_no_abstract=skipped_no_abstract_total,
            )
        finally:
            close = getattr(provider, "close", None)
            if callable(close):
                close()
