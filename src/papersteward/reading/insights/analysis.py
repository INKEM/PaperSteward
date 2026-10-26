"""单篇分析运行编排（R2-03）：只读备料 → 逐篇发送 → 短写落库。

运行边界（计划 §13）：只读阶段一个会话固定清单、授权与材料解析后即释放；
模型调用一律在事务外，发送前经 ``ensure_send_authorized`` 核对授权、端点
与配置版本，gateway 只能经 ``build_analysis_gateway`` 构造（通用缓存强制
关闭）；每篇结果在 ``BEGIN IMMEDIATE`` 短写内再次校验授权版本、任务未
取消与材料未漂移后落库并登记派生副本。授权检查失败即停止新发送（异常
上抛，运行标记失败，已完成篇目保留）；单篇模型失败只记录该篇，不改其他
结果。进度与结果摘要只含计数、状态码与对象 ID，不含私人正文或原始模型
异常。
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from sqlalchemy.orm import Session, sessionmaker

from papersteward.core.entities import utc_now
from papersteward.errors import NotAuthorizedError
from papersteward.llm.cost import merge_usage, record_llm_usage
from papersteward.llm.errors import LLMError
from papersteward.llm.gateway import LLMGateway, LLMUsage
from papersteward.reading.insights.authorization import (
    ResolvedSendConfig,
    build_analysis_gateway,
    ensure_send_authorized,
)
from papersteward.reading.insights.contracts import ANALYSIS_PURPOSE, MaterialRef
from papersteward.reading.insights.evidence import MaterialEntry, verify_candidates
from papersteward.reading.insights.materials import (
    MaterialContent,
    manifest_content_of,
    resolve_material_content,
)
from papersteward.schemas import ReadingReflectionOutput, ReflectionCandidateOutput
from papersteward.storage.db import session_scope
from papersteward.storage.insights import (
    ReadingAnalysisCandidateRepository,
    ReadingAnalysisGrantRepository,
    ReadingAnalysisManifestRepository,
)
from papersteward.storage.mappers import as_utc
from papersteward.storage.models import ReadingAnalysisCandidateRow, ReadingDerivedCopyRow

if TYPE_CHECKING:
    from papersteward.config import Settings

__all__ = [
    "ANALYSIS_BUDGET_MAX_INPUT_CHARS",
    "ANALYSIS_BUDGET_MAX_PAPERS",
    "AnalysisRunParamsView",
    "run_insight_analysis",
]

# 预算常量（计划 §7.6）：批数与输入量在篇目边界生效，不静默截断单篇材料；
# gateway 的重试与 repair 计入 usage.calls/retries/repairs。
ANALYSIS_BUDGET_MAX_PAPERS = 200
ANALYSIS_BUDGET_MAX_INPUT_CHARS = 400_000

# 候选衍生内容与派生链来源类别的映射；元数据与模型概括非私人材料，不登记。
_DERIVED_SOURCE_KIND = {
    "user_notes": "paper",
    "structured_reasons": "paper",
    "user_rating": "paper",
    "user_excerpt": "paper",
    "asset_content": "asset",
}


@dataclass(frozen=True)
class AnalysisRunParamsView:
    """提交参数的运行时视图（与 FlowRequest 的扁平字段一一对应）。"""

    manifest_id: str
    manifest_hash: str
    grant_id: str
    purpose: str = ANALYSIS_PURPOSE


@dataclass
class _PaperBatch:
    """一篇论文在清单固定时点解析出的全部材料；事务外持有，不落库。"""

    paper_id: str
    built_at: datetime
    refs: list[MaterialRef] = field(default_factory=list)
    entries: list[MaterialEntry] = field(default_factory=list)
    input_chars: int = 0


@dataclass
class _RunCounters:
    """运行计数摘要；``to_generated()`` 输出进 Run.stats_json，无私人正文。"""

    papers_total: int = 0
    papers_completed: int = 0
    papers_failed: int = 0
    papers_stale: int = 0
    papers_insufficient: int = 0
    papers_already_done: int = 0
    papers_unfinished: int = 0
    papers_over_budget: int = 0
    candidate_count: int = 0
    rejected_count: int = 0
    failed_paper_ids: list[str] = field(default_factory=list)
    failed_reasons: dict[str, str] = field(default_factory=dict)
    usage_json: dict[str, object] = field(default_factory=dict)
    stopped_reason: str | None = None

    def to_generated(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "papers_total": self.papers_total,
            "papers_completed": self.papers_completed,
            "papers_failed": self.papers_failed,
            "papers_stale": self.papers_stale,
            "papers_insufficient": self.papers_insufficient,
            "papers_already_done": self.papers_already_done,
            "papers_unfinished": self.papers_unfinished,
            "papers_over_budget": self.papers_over_budget,
            "candidate_count": self.candidate_count,
            "rejected_count": self.rejected_count,
            "failed_paper_ids": list(self.failed_paper_ids),
            "failed_reasons": dict(self.failed_reasons),
            "usage": dict(self.usage_json),
        }
        if self.stopped_reason is not None:
            payload["stopped_reason"] = self.stopped_reason
        return payload


def run_insight_analysis(
    factory: sessionmaker[Session],
    settings: Settings,
    *,
    params: AnalysisRunParamsView,
    run_id: str,
    should_abort: Callable[[], bool],
    progress: Callable[..., None],
) -> dict[str, object]:
    """执行一次单篇分析；返回写入 ``Run.stats_json["generated"]`` 的摘要。"""
    batches, stale_ids, already_done = _prepare(factory, params)
    pending = [batch for batch in batches if batch.paper_id not in already_done]
    over_budget = pending[ANALYSIS_BUDGET_MAX_PAPERS:]
    pending = pending[:ANALYSIS_BUDGET_MAX_PAPERS]
    counters = _RunCounters(
        papers_total=len(batches),
        papers_stale=len(stale_ids),
        papers_already_done=len(already_done),
        papers_over_budget=len(over_budget),
        usage_json={"llm_calls": []},
    )
    processed = 0
    used_chars = 0
    try:
        for batch in pending:
            if should_abort():
                break
            if used_chars + batch.input_chars > ANALYSIS_BUDGET_MAX_INPUT_CHARS:
                break
            usage, completed, rejected, outcome = _analyze_one(
                factory, settings, params, run_id, batch, should_abort
            )
            used_chars += batch.input_chars
            counters.usage_json = merge_usage(counters.usage_json, usage)  # type: ignore[arg-type]
            if outcome == "aborted":
                break
            processed += 1
            if outcome == "completed":
                counters.papers_completed += 1
                counters.candidate_count += len(completed)
                counters.rejected_count += len(rejected)
            elif outcome == "insufficient":
                counters.papers_insufficient += 1
            elif outcome == "stale":
                counters.papers_stale += 1
            else:
                counters.papers_failed += 1
                counters.failed_paper_ids.append(batch.paper_id)
                counters.failed_reasons[batch.paper_id] = outcome
            counters.papers_unfinished = max(0, len(pending) - processed)
            progress(
                stage="analyzing",
                processing_total=len(pending),
                processed=(
                    counters.papers_completed
                    + counters.papers_failed
                    + counters.papers_insufficient
                    + counters.papers_stale
                ),
            )
    except NotAuthorizedError as exc:
        # 授权或来源检查失败即停止新发送；已完成篇目保留，运行标记失败。
        counters.papers_unfinished = max(0, len(pending) - processed)
        counters.stopped_reason = exc.message
        raise
    counters.papers_unfinished = max(0, len(pending) - processed)
    return counters.to_generated()


def _prepare(
    factory: sessionmaker[Session], params: AnalysisRunParamsView
) -> tuple[list[_PaperBatch], list[str], set[str]]:
    """只读阶段：固定清单、核对授权身份、按篇解析材料后即释放连接。"""
    with session_scope(factory) as session:
        manifest = ReadingAnalysisManifestRepository(session).get(params.manifest_id)
        if (
            manifest is None
            or manifest.purpose != params.purpose
            or manifest.manifest_hash != params.manifest_hash
        ):
            raise NotAuthorizedError("材料清单不存在或与提交参数不符，请重新生成预览")
        content = manifest_content_of(manifest)
        grants = ReadingAnalysisGrantRepository(session).active_for_purpose(params.purpose)
        grant = next((item for item in grants if item.id == params.grant_id), None)
        if grant is None or grant.revoked_at is not None:
            raise NotAuthorizedError("授权不存在或已撤回，请重新核对数据去向")
        if grant.scope_mode == "once" and grant.manifest_hash != params.manifest_hash:
            raise NotAuthorizedError("本次授权与清单不匹配，请重新预览并授权")
        by_paper: dict[str, list[MaterialRef]] = {}
        for ref in content.materials:
            if ref.paper_id:
                by_paper.setdefault(ref.paper_id, []).append(ref)
        batches: list[_PaperBatch] = []
        stale_ids: list[str] = []
        built_at = as_utc(content.built_at)
        for paper_id, refs in sorted(by_paper.items()):
            batch = _PaperBatch(paper_id=paper_id, built_at=built_at, refs=list(refs))
            ok = True
            for ref in refs:
                resolved: MaterialContent = resolve_material_content(
                    session, ref, built_at=built_at
                )
                if not resolved.ok:
                    ok = False
                    break
                batch.entries.append(
                    MaterialEntry(
                        index=len(batch.entries),
                        kind=ref.kind,
                        target_id=ref.target_id,
                        version_key=ref.version_key,
                        content_text=resolved.text,
                    )
                )
                batch.input_chars += len(resolved.text)
            if ok and batch.entries:
                batches.append(batch)
            else:
                stale_ids.append(paper_id)
        already_done = {
            paper_id
            for paper_id in by_paper
            if ReadingAnalysisCandidateRepository(session).by_manifest_paper(
                params.manifest_hash, paper_id
            )
        }
    return batches, stale_ids, already_done


def _analyze_one(
    factory: sessionmaker[Session],
    settings: Settings,
    params: AnalysisRunParamsView,
    run_id: str,
    batch: _PaperBatch,
    should_abort: Callable[[], bool],
) -> tuple[object, list[ReflectionCandidateOutput], list[str], str]:
    """单篇：发送边界授权 → 事务外模型调用 → 证据验证 → 短写落库。

    返回 ``(usage, 有效候选, 拒绝原因码, 结果码)``；结果码为
    ``completed/insufficient/stale/failed/aborted``，failed 时第四元素携带
    可读失败文案（不含原始异常与私人正文）。授权失败上抛
    ``NotAuthorizedError`` 由调用方停止整个运行。
    """
    kinds = sorted({entry.kind for entry in batch.entries})
    resolved = _authorized(factory, settings, params, kinds)
    gateway = build_analysis_gateway(resolved)
    materials_text = "\n\n".join(
        f"[{entry.index}] 类别 {entry.kind} · 版本 {entry.version_key}\n{entry.content_text}"
        for entry in batch.entries
    )
    try:
        result = gateway.complete_json(
            "reading_reflection",
            {
                "paper_id": batch.paper_id,
                "paper_title": batch.paper_id,
                "materials": materials_text,
            },
        )
    except LLMError as exc:
        _close_provider(gateway)
        from papersteward.jobs.ui_flow import llm_failure_message

        return LLMUsage(model_id=resolved.runtime.model or "none"), [], [], llm_failure_message(exc)
    _close_provider(gateway)
    output = cast(ReadingReflectionOutput, result.data)
    valid, rejected = verify_candidates(output, batch.entries, paper_id=batch.paper_id)
    short_write = _persist_paper(
        factory,
        settings,
        params,
        run_id,
        batch,
        valid,
        resolved.model_config_version,
        result.usage,
        should_abort,
    )
    if short_write == "stale":
        return result.usage, [], [], "stale"
    if short_write == "aborted":
        return result.usage, [], [], "aborted"
    if output.insufficient_material or not valid:
        # 材料不足、或候选全部被证据验证拒绝：都不产生凭空候选。
        return result.usage, [], rejected, "insufficient"
    return result.usage, valid, rejected, "completed"


def _persist_paper(
    factory: sessionmaker[Session],
    settings: Settings,
    params: AnalysisRunParamsView,
    run_id: str,
    batch: _PaperBatch,
    valid: list[ReflectionCandidateOutput],
    model_config_version: str,
    usage: object,
    should_abort: Callable[[], bool],
) -> str:
    """短写：授权版本/取消/材料漂移复核后落候选、派生副本与成本。"""
    with session_scope(factory, immediate=True) as session:
        if should_abort():
            return "aborted"
        _authorized_on(session, settings, params, sorted({entry.kind for entry in batch.entries}))
        if _materials_drifted(session, batch):
            return "stale"
        now = as_utc(utc_now())
        rows = [
            ReadingAnalysisCandidateRow(
                id=uuid4().hex,
                run_id=run_id,
                manifest_id=params.manifest_id,
                manifest_hash=params.manifest_hash,
                grant_id=params.grant_id,
                model_config_version=model_config_version,
                paper_id=batch.paper_id,
                kind=candidate.kind,
                nature=candidate.nature,
                statement=candidate.statement,
                conditions=candidate.conditions,
                limitations=candidate.limitations,
                quote=candidate.quote,
                quote_source_id=(
                    batch.entries[candidate.quote_source_index].target_id
                    if candidate.quote is not None and candidate.quote_source_index is not None
                    else None
                ),
                source_refs_json=[
                    {
                        "kind": batch.entries[index].kind,
                        "target_id": batch.entries[index].target_id,
                        "version_key": batch.entries[index].version_key,
                    }
                    for index in candidate.source_indexes
                ],
                verification="pending_user",
                stale=0,
                created_at=now,
            )
            for candidate in valid
        ]
        if rows:
            ReadingAnalysisCandidateRepository(session).add_many(rows)
            _register_candidate_copies(session, rows, created_at=now)
        record_llm_usage(session, run_id, usage)  # type: ignore[arg-type]
    return "ok"


def _register_candidate_copies(
    session: Session, rows: list[ReadingAnalysisCandidateRow], *, created_at: object
) -> None:
    """候选引用私人材料时登记派生链：来源（材料对象）→ 候选。"""
    copies: list[ReadingDerivedCopyRow] = []
    for row in rows:
        fields = ["statement"] + (["quote"] if row.quote is not None else [])
        for ref in row.source_refs_json or []:
            source_kind = _DERIVED_SOURCE_KIND.get(str(ref.get("kind")))
            if source_kind is None:
                continue
            copies.extend(
                ReadingDerivedCopyRow(
                    id=uuid4().hex,
                    source_kind=source_kind,
                    source_id=str(ref.get("target_id")),
                    source_revision=None,
                    derived_kind="candidate",
                    derived_id=row.id,
                    field_kind=field_kind,
                    created_at=created_at,
                )
                for field_kind in fields
            )
    if copies:
        from papersteward.storage.insights import ReadingDerivedCopyRepository

        ReadingDerivedCopyRepository(session).add_many(copies)


def _authorized(
    factory: sessionmaker[Session],
    settings: Settings,
    params: AnalysisRunParamsView,
    kinds: list[str],
) -> ResolvedSendConfig:
    """发送边界核对（只读会话）；返回解析后的绑定快照供 gateway 构造。"""
    with session_scope(factory) as session:
        return ensure_send_authorized(
            session,
            settings=settings,
            purpose=params.purpose,
            manifest_hash=params.manifest_hash,
            material_kinds=kinds,
        )


def _authorized_on(
    session: Session,
    settings: Settings,
    params: AnalysisRunParamsView,
    kinds: list[str],
) -> None:
    """落库边界的授权复核；失败即抛出停止新发送。"""
    ensure_send_authorized(
        session,
        settings=settings,
        purpose=params.purpose,
        manifest_hash=params.manifest_hash,
        material_kinds=kinds,
    )


def _materials_drifted(session: Session, batch: _PaperBatch) -> bool:
    """落库前复核材料版本锚点；任何一条失配即视为该篇源已漂移。"""
    for ref in batch.refs:
        resolved = resolve_material_content(session, ref, built_at=batch.built_at)
        if not resolved.ok:
            return True
    return False


def _close_provider(gateway: LLMGateway) -> None:
    provider = getattr(gateway, "_provider", None)
    if provider is not None and hasattr(provider, "close"):
        with suppress(Exception):
            provider.close()
