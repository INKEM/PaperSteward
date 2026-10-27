"""跨文献专题综合（R2-05）：只读备料 → 事务外模型综合 → 短写保存知识版报告。

边界沿用 R2-03 的运行约定：只读会话固定材料清单、授权与事实快照后释放；
模型调用在事务外，发送前经 ``ensure_send_authorized`` 核对用途、端点与配置
版本，gateway 只能经 ``build_analysis_gateway`` 构造（通用缓存关闭）。结果
在 ``BEGIN IMMEDIATE`` 短写内再次核对授权、取消标志与材料漂移后落库；数字
全部来自确定性事实层，模型只写有来源引用的知识整理。
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from papersteward.core.entities import utc_now
from papersteward.errors import NotAuthorizedError
from papersteward.llm.cost import record_llm_usage
from papersteward.llm.errors import LLMError
from papersteward.llm.gateway import LLMGateway, LLMUsage
from papersteward.llm.prompts import READING_SYNTHESIS_V1
from papersteward.reading.insights.authorization import (
    ResolvedSendConfig,
    build_analysis_gateway,
    ensure_send_authorized,
)
from papersteward.reading.insights.contracts import (
    MATERIAL_KIND_LABELS,
    PRIVATE_MATERIAL_KINDS,
    SYNTHESIS_PURPOSE,
    SYNTHESIS_REPORT_CONTRACT,
    InsightPeriodRequest,
    InsightQueryRequest,
    InsightReportContentV2,
    ManifestCoverage,
    MaterialKind,
    MaterialRef,
    ReviewMode,
    SynthesisAnalysisContext,
    SynthesisCandidateLink,
    SynthesisOpenQuestion,
    SynthesisSection,
    SynthesisSourceRef,
    SynthesisTopic,
)
from papersteward.reading.insights.evidence import MaterialEntry
from papersteward.reading.insights.materials import (
    manifest_content_of,
    resolve_material_content,
)
from papersteward.reading.insights.periods import resolve_period
from papersteward.reading.insights.reports import (
    PreparedReport,
    ReportPreparationCancelled,
    prepare_report,
)
from papersteward.reading.insights.scope import series_key
from papersteward.schemas import ReadingSynthesisOutput
from papersteward.storage.db import session_scope
from papersteward.storage.delivery import content_hash
from papersteward.storage.insights import (
    ReadingAnalysisManifestRepository,
    ReadingAssetRepository,
    ReadingDerivedCopyRepository,
    ReadingReportRepository,
)
from papersteward.storage.mappers import as_utc
from papersteward.storage.models import (
    ReadingDerivedCopyRow,
    ReadingReportRow,
)

if TYPE_CHECKING:
    from papersteward.config import Settings

__all__ = [
    "SynthesisRunParamsView",
    "run_insight_synthesis",
    "verify_synthesis",
]

SYNTHESIS_PROMPT_VERSION = READING_SYNTHESIS_V1.version
# 单次综合的输入字符预算：在材料边界停止，超出部分写入实际分析覆盖。
SYNTHESIS_MAX_INPUT_CHARS = 400_000

# 私人综合归纳的来源类别映射；只登记 ID 与字段类别，不复制原文。
_DERIVED_SOURCE_KIND = {
    "structured_reasons": "paper",
    "user_notes": "paper",
    "user_excerpt": "paper",
    "verified_candidate": "candidate",
    "asset_content": "asset",
}


@dataclass(frozen=True)
class SynthesisRunParamsView:
    """知识版回顾的运行时参数；与 FlowRequest 的扁平字段一一对应。"""

    period: str
    mode: str
    anchor_date: date | None
    start_date: date | None
    end_date: date | None
    tag: str | None
    manifest_id: str
    manifest_hash: str
    grant_id: str
    purpose: str = SYNTHESIS_PURPOSE


@dataclass(frozen=True)
class _ManifestSnapshot:
    manifest_id: str
    manifest_hash: str
    built_at: datetime
    coverage: ManifestCoverage
    materials: list[MaterialRef]


@dataclass(frozen=True)
class _SynthesisPreparation:
    prepared: PreparedReport
    manifest: _ManifestSnapshot
    entries: list[MaterialEntry]
    stale_refs: int
    budget_omitted: int
    covered_paper_ids: set[str]
    missing_paper_ids: list[str]


@dataclass(frozen=True)
class _GeneratedSynthesis:
    output: ReadingSynthesisOutput
    usage: LLMUsage | None
    resolved: ResolvedSendConfig | None


def run_insight_synthesis(
    factory: sessionmaker[Session],
    settings: Settings,
    *,
    params: SynthesisRunParamsView,
    run_id: str,
    should_abort: Callable[[], bool],
    progress: Callable[..., None],
) -> dict[str, object]:
    """执行一次知识版回顾；返回写入 ``Run.stats_json["generated"]`` 的摘要。"""
    try:
        prepared = _prepare(factory, params, should_abort=should_abort)
    except ReportPreparationCancelled:
        return {"report_id": None, "stopped_reason": "用户停止任务"}
    if should_abort():
        return {"report_id": None, "stopped_reason": "用户停止任务"}
    progress(stage="synthesizing")
    generated = _generate(factory, settings, params, prepared)
    topics, questions, links, rejected_items, rejected_refs = verify_synthesis(
        generated.output, prepared.entries
    )
    insufficient = generated.output.insufficient_material or not topics
    note = generated.output.note
    if not generated.output.insufficient_material and not topics:
        note = "综合结果的来源引用无法核对，未保存推断；请重试或补充材料。"
    section = _build_section(
        topics,
        questions,
        links,
        insufficient=insufficient,
        note=note,
    )
    if should_abort():
        return {"report_id": None, "stopped_reason": "用户停止任务"}
    return _save(
        factory,
        settings,
        params,
        prepared,
        generated,
        section,
        run_id=run_id,
        should_abort=should_abort,
        rejected_items=rejected_items,
        rejected_refs=rejected_refs,
    )


def _prepare(
    factory: sessionmaker[Session],
    params: SynthesisRunParamsView,
    *,
    should_abort: Callable[[], bool],
) -> _SynthesisPreparation:
    """只读阶段：核对清单、解析材料、固定事实快照后释放连接。"""
    with session_scope(factory) as session:
        manifest = ReadingAnalysisManifestRepository(session).get(params.manifest_id)
        if (
            manifest is None
            or manifest.purpose != params.purpose
            or manifest.manifest_hash != params.manifest_hash
        ):
            raise NotAuthorizedError("材料清单不存在或与提交参数不符，请重新生成预览")
        content = manifest_content_of(manifest)
        if content.scope_request is None or not _scope_matches(params, content.scope_request):
            raise NotAuthorizedError("回顾范围与材料清单不一致，请重新生成预览")
        built_at = as_utc(content.built_at)
        review = _ReviewSpec(
            period=params.period,
            mode=params.mode,
            anchor_date=params.anchor_date,
            start_date=params.start_date,
            end_date=params.end_date,
            tag=params.tag,
        )
        # 先固定事实快照（内部显式 BEGIN DEFERRED / ROLLBACK），再解析材料，
        # 避免在同一连接上把显式读事务叠到已有隐式事务里。
        prepared = prepare_report(
            session,
            review,
            collected_at=built_at,
            include_asset_bodies=False,
            should_abort=should_abort,
        )
        entries: list[MaterialEntry] = []
        stale_refs = 0
        budget_omitted = 0
        used_chars = 0
        covered: set[str] = set()
        for ref in content.materials:
            resolved = resolve_material_content(session, ref, built_at=built_at)
            if not resolved.ok:
                stale_refs += 1
                continue
            if used_chars + len(resolved.text) > SYNTHESIS_MAX_INPUT_CHARS:
                budget_omitted += 1
                continue
            used_chars += len(resolved.text)
            entries.append(
                MaterialEntry(
                    index=len(entries),
                    kind=ref.kind,
                    target_id=ref.target_id,
                    version_key=ref.version_key,
                    content_text=resolved.text,
                    paper_id=ref.paper_id,
                )
            )
            if ref.paper_id:
                covered.add(ref.paper_id)
            elif ref.kind == "asset_content":
                covered.update(_asset_source_paper_ids(session, ref))
        paper_ids = set(prepared.paper_ids)
        missing = sorted(paper_ids - covered)
        snapshot = _ManifestSnapshot(
            manifest_id=manifest.id,
            manifest_hash=manifest.manifest_hash,
            built_at=built_at,
            coverage=content.coverage,
            materials=list(content.materials),
        )
    return _SynthesisPreparation(
        prepared=prepared,
        manifest=snapshot,
        entries=entries,
        stale_refs=stale_refs,
        budget_omitted=budget_omitted,
        covered_paper_ids=covered,
        missing_paper_ids=missing,
    )


def _scope_matches(params: SynthesisRunParamsView, query: InsightQueryRequest) -> bool:
    """范围参数可带原始 anchor，但解析后必须与清单冻结的周期完全一致。"""
    if params.mode != query.mode.value or (params.tag or None) != (query.tag or None):
        return False
    try:
        period = InsightPeriodRequest(
            kind=params.period,
            timezone=query.period.timezone,
            anchor_date=params.anchor_date,
            start_date=params.start_date,
            end_date=params.end_date,
        )
        resolved = resolve_period(period)
    except (TypeError, ValueError):
        return False
    return resolved == resolve_period(query.period)


def _generate(
    factory: sessionmaker[Session],
    settings: Settings,
    params: SynthesisRunParamsView,
    prepared: _SynthesisPreparation,
) -> _GeneratedSynthesis:
    """发送边界授权后调用一次模型；无材料时不发送也不构造 provider。"""
    if not prepared.entries:
        empty = ReadingSynthesisOutput.model_validate(
            {
                "topics": [],
                "open_questions": [],
                "candidate_links": [],
                "insufficient_material": True,
                "note": "本次范围内没有可用的授权材料，未形成专题判断。",
            }
        )
        return _GeneratedSynthesis(output=empty, usage=None, resolved=None)
    resolved = _authorized(factory, settings, params, prepared)
    gateway = build_analysis_gateway(resolved)
    try:
        result = gateway.complete_json(
            "reading_synthesis",
            {
                "report_kind": _report_kind_label(params.period),
                "period_label": prepared.prepared.period.label,
                "review_mode": _mode_label(prepared.prepared.query.mode),
                "facts": synthesis_facts_text(prepared.prepared),
                "materials": synthesis_materials_text(prepared.entries),
            },
        )
    except LLMError:
        _close_provider(gateway)
        raise
    _close_provider(gateway)
    return _GeneratedSynthesis(
        output=cast(ReadingSynthesisOutput, result.data),
        usage=result.usage,
        resolved=resolved,
    )


def _save(
    factory: sessionmaker[Session],
    settings: Settings,
    params: SynthesisRunParamsView,
    prepared: _SynthesisPreparation,
    generated: _GeneratedSynthesis,
    section: SynthesisSection,
    *,
    run_id: str,
    should_abort: Callable[[], bool],
    rejected_items: int,
    rejected_refs: int,
) -> dict[str, object]:
    """短写：授权/取消/材料漂移复核后保存报告、来源登记与成本。"""
    with session_scope(factory, immediate=True) as session:
        if should_abort():
            return {"report_id": None, "stopped_reason": "用户停止任务"}
        if prepared.entries:
            _authorized_on(session, settings, params, prepared)
        sources_changed = _materials_drifted(session, prepared)
        context = _analysis_context(
            params,
            prepared,
            generated,
            rejected_items=rejected_items,
            rejected_refs=rejected_refs,
            sources_changed=sources_changed,
        )
        content = _content_v2(prepared.prepared, section, context)
        row, deduplicated = save_synthesis_report(
            session,
            params,
            prepared,
            content,
            run_id=run_id,
            now=as_utc(utc_now()),
        )
        if generated.usage is not None:
            record_llm_usage(session, run_id, generated.usage)
    return {
        "report_id": row.id,
        "deduplicated": deduplicated,
        "topics": len(section.topics),
        "open_questions": len(section.open_questions),
        "candidate_links": len(section.candidate_links),
        "materials_total": context.materials_total,
        "materials_used": context.materials_used,
        "materials_stale": context.materials_stale,
        "papers_in_scope": context.papers_in_scope,
        "papers_with_materials": context.papers_with_materials,
        "missing_papers": context.missing_papers_total,
        "verified_candidates": context.verified_candidates,
        "assets_used": context.assets_used,
        "rejected_items": context.rejected_items,
        "rejected_refs": context.rejected_refs,
        "partial": context.partial,
        "insufficient_material": section.insufficient_material,
        "usage": generated.usage.as_json() if generated.usage is not None else {},
    }


def verify_synthesis(
    output: ReadingSynthesisOutput, entries: list[MaterialEntry]
) -> tuple[
    list[SynthesisTopic],
    list[SynthesisOpenQuestion],
    list[SynthesisCandidateLink],
    int,
    int,
]:
    """逐条核对来源编号；被拒条目与引用计数，不整批作废也不凭空保留。"""
    if output.insufficient_material:
        return [], [], [], 0, 0
    by_index = {entry.index: entry for entry in entries}
    topics: list[SynthesisTopic] = []
    questions: list[SynthesisOpenQuestion] = []
    links: list[SynthesisCandidateLink] = []
    rejected_items = 0
    rejected_refs = 0
    for index, candidate in enumerate(output.topics, start=1):
        supporting, invalid = _source_refs(candidate.supporting_indexes, by_index)
        rejected_refs += invalid
        if not supporting:
            rejected_items += 1
            continue
        limiting, invalid = _source_refs(candidate.limiting_indexes, by_index)
        rejected_refs += invalid
        refs = supporting + limiting
        topics.append(
            SynthesisTopic(
                topic_id=f"t{index}",
                question=candidate.question,
                understanding=candidate.understanding,
                supporting_refs=supporting,
                limiting_refs=limiting,
                independent_sources=_independent_sources(refs),
                comparison=candidate.comparison,
                unverified=candidate.unverified,
                private=any(ref.private for ref in refs),
            )
        )
    for index, candidate_question in enumerate(output.open_questions, start=1):
        refs, invalid = _source_refs(candidate_question.source_indexes, by_index)
        rejected_refs += invalid
        if not refs:
            rejected_items += 1
            continue
        questions.append(
            SynthesisOpenQuestion(
                question_id=f"q{index}",
                question=candidate_question.question,
                status=candidate_question.status,
                source_refs=refs,
                independent_sources=_independent_sources(refs),
                private=any(ref.private for ref in refs),
            )
        )
    for index, candidate_link in enumerate(output.candidate_links, start=1):
        refs, invalid = _source_refs(candidate_link.source_indexes, by_index)
        rejected_refs += invalid
        if not refs:
            rejected_items += 1
            continue
        links.append(
            SynthesisCandidateLink(
                link_id=f"l{index}",
                statement=candidate_link.statement,
                link_kind=candidate_link.link_kind,
                source_refs=refs,
                independent_sources=_independent_sources(refs),
                caution=candidate_link.caution,
                private=any(ref.private for ref in refs),
            )
        )
    return topics, questions, links, rejected_items, rejected_refs


def _source_refs(
    indexes: list[int], by_index: dict[int, MaterialEntry]
) -> tuple[list[SynthesisSourceRef], int]:
    refs: list[SynthesisSourceRef] = []
    seen: set[int] = set()
    invalid = 0
    for index in indexes:
        if index in seen:
            continue
        seen.add(index)
        entry = by_index.get(index)
        if entry is None:
            invalid += 1
            continue
        refs.append(
            SynthesisSourceRef(
                kind=cast(MaterialKind, entry.kind),
                target_id=entry.target_id,
                version_key=entry.version_key,
                paper_id=entry.paper_id,
                private=entry.kind in PRIVATE_MATERIAL_KINDS,
            )
        )
    return refs, invalid


def _independent_sources(refs: list[SynthesisSourceRef]) -> int:
    """同一篇论文的概括与候选只算一个独立来源；资产按资产身份计。"""
    return len(
        {f"paper:{ref.paper_id}" if ref.paper_id else f"{ref.kind}:{ref.target_id}" for ref in refs}
    )


def _build_section(
    topics: list[SynthesisTopic],
    questions: list[SynthesisOpenQuestion],
    links: list[SynthesisCandidateLink],
    *,
    insufficient: bool,
    note: str | None,
) -> SynthesisSection:
    items = list(topics) + list(questions) + list(links)
    private_items = sum(1 for item in items if item.private)
    return SynthesisSection(
        topics=[] if insufficient else topics,
        open_questions=[] if insufficient else questions,
        candidate_links=[] if insufficient else links,
        note=note,
        insufficient_material=insufficient,
        private_items=0 if insufficient else private_items,
    )


def _analysis_context(
    params: SynthesisRunParamsView,
    prepared: _SynthesisPreparation,
    generated: _GeneratedSynthesis,
    *,
    rejected_items: int,
    rejected_refs: int,
    sources_changed: bool,
) -> SynthesisAnalysisContext:
    coverage = prepared.manifest.coverage
    materials = prepared.manifest.materials
    kinds = sorted({entry.kind for entry in prepared.entries})
    missing = prepared.missing_paper_ids
    papers_in_scope = len(prepared.prepared.paper_ids)
    verified = sum(1 for entry in prepared.entries if entry.kind == "verified_candidate")
    assets = sum(1 for entry in prepared.entries if entry.kind == "asset_content")
    partial = bool(
        prepared.stale_refs
        or len(prepared.entries) < len(materials)
        or missing
        or coverage.omitted_papers
        or coverage.omitted_materials
        or prepared.budget_omitted
        or sources_changed
        or rejected_items
        or rejected_refs
    )
    return SynthesisAnalysisContext(
        purpose=params.purpose,
        manifest_id=prepared.manifest.manifest_id,
        manifest_hash=prepared.manifest.manifest_hash,
        grant_id=generated.resolved.grant_id if generated.resolved else params.grant_id,
        model_config_version=(
            generated.resolved.model_config_version if generated.resolved else ""
        ),
        prompt_version=SYNTHESIS_PROMPT_VERSION,
        built_at=prepared.manifest.built_at,
        materials_total=len(materials),
        materials_used=len(prepared.entries),
        materials_stale=prepared.stale_refs,
        selected_papers=coverage.selected_papers,
        papers_in_scope=papers_in_scope,
        papers_with_materials=len(prepared.covered_paper_ids & set(prepared.prepared.paper_ids)),
        missing_paper_ids=missing[:200],
        missing_papers_total=len(missing),
        material_kinds=kinds,
        omitted_papers=coverage.omitted_papers,
        omitted_materials=coverage.omitted_materials + prepared.budget_omitted,
        verified_candidates=verified,
        assets_used=assets,
        rejected_items=rejected_items,
        rejected_refs=rejected_refs,
        partial=partial,
        sources_changed_after_send=sources_changed,
    )


def _content_v2(
    prepared: PreparedReport,
    section: SynthesisSection,
    context: SynthesisAnalysisContext,
) -> InsightReportContentV2:
    facts = prepared.content
    return InsightReportContentV2(
        contract=SYNTHESIS_REPORT_CONTRACT,
        metrics=facts.metrics,
        readings=list(facts.readings),
        backfills=list(facts.backfills),
        changes=list(facts.changes),
        structured_changes=list(facts.structured_changes),
        unassigned=list(facts.unassigned),
        assets=list(facts.assets),
        synthesis=section,
        analysis=context,
    )


def synthesis_fingerprint(
    prepared: _SynthesisPreparation,
    params: SynthesisRunParamsView,
    *,
    model_config_version: str,
) -> str:
    """按事实范围＋材料锚点＋用途/模型身份去重，不含清单构建时间。"""
    key = series_key(prepared.prepared.context)
    if key is None:
        raise NotAuthorizedError("报告范围未知，无法保存知识版回顾；请重新选择明确范围")
    return content_hash(
        {
            "contract": SYNTHESIS_REPORT_CONTRACT,
            "series": key,
            "materials": [
                {
                    "kind": ref.kind,
                    "target_id": ref.target_id,
                    "version_key": ref.version_key,
                    "ref_hash": ref.ref_hash,
                }
                for ref in prepared.manifest.materials
            ],
            "purpose": params.purpose,
            "prompt_version": SYNTHESIS_PROMPT_VERSION,
            "model_config_version": model_config_version,
        }
    )


def save_synthesis_report(
    session: Session,
    params: SynthesisRunParamsView,
    prepared: _SynthesisPreparation,
    content: InsightReportContentV2,
    *,
    run_id: str,
    now: datetime,
) -> tuple[ReadingReportRow, bool]:
    """短写保存；同输入沿用既有快照，返回 (报告行, 是否去重)。"""
    fingerprint = synthesis_fingerprint(
        prepared,
        params,
        model_config_version=content.analysis.model_config_version,
    )
    repository = ReadingReportRepository(session)
    existing = repository.get_by_run(run_id)
    if existing is not None:
        return existing, True
    existing = repository.get_by_fingerprint(fingerprint)
    if existing is not None:
        return existing, True
    facts = prepared.prepared
    content_json = content.model_dump(mode="json")
    row = ReadingReportRow(
        id=uuid4().hex,
        report_kind=facts.period.kind.value,
        period_label=facts.period.label,
        period_start=facts.period.start_date,
        period_end=facts.period.end_date_inclusive,
        timezone=facts.period.timezone,
        review_mode=facts.query.mode.value,
        knowledge_cutoff=facts.knowledge_cutoff,
        generated_at=now,
        contract=SYNTHESIS_REPORT_CONTRACT,
        input_fingerprint=fingerprint,
        content_json=content_json,
        content_hash=content_hash(content_json),
        run_id=run_id,
    )
    try:
        repository.add(row)
    except IntegrityError:
        session.rollback()
        concurrent = repository.get_by_fingerprint(fingerprint)
        if concurrent is not None:
            return concurrent, True
        raise
    context = facts.context
    repository.save_context(row.id, context.model_dump(mode="json"), series_key(context))
    _register_report_assets(session, row.id, content, created_at=now)
    _register_synthesis_copies(session, row.id, content.synthesis, created_at=now)
    return row, False


def _register_report_assets(
    session: Session, report_id: str, content: Any, *, created_at: datetime
) -> None:
    from papersteward.reading.insights.reports import _register_asset_copies

    _register_asset_copies(session, report_id, content, created_at=created_at)


def _register_synthesis_copies(
    session: Session, report_id: str, section: SynthesisSection, *, created_at: datetime
) -> None:
    """私人归纳的来源链：来源材料 → 报告的知识整理字段。"""
    seen: set[tuple[str, str, int | None]] = set()
    rows: list[ReadingDerivedCopyRow] = []
    refs = []
    for topic in section.topics:
        if topic.private:
            refs.extend(topic.supporting_refs)
            refs.extend(topic.limiting_refs)
    refs.extend(
        ref
        for question in section.open_questions
        if question.private
        for ref in question.source_refs
    )
    refs.extend(ref for link in section.candidate_links if link.private for ref in link.source_refs)
    for ref in refs:
        if not ref.private:
            continue
        source_kind = _DERIVED_SOURCE_KIND.get(ref.kind)
        if source_kind is None:
            continue
        key = (source_kind, ref.target_id, _source_revision(ref))
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            ReadingDerivedCopyRow(
                id=uuid4().hex,
                source_kind=source_kind,
                source_id=ref.target_id,
                source_revision=key[2],
                derived_kind="report",
                derived_id=report_id,
                field_kind="synthesis",
                created_at=created_at,
            )
        )
    if rows:
        ReadingDerivedCopyRepository(session).add_many(rows)


def _source_revision(ref: SynthesisSourceRef) -> int | None:
    if ref.kind != "asset_content":
        return None
    try:
        return int(ref.version_key.removeprefix("revision:"))
    except ValueError:
        return None


def _materials_drifted(session: Session, prepared: _SynthesisPreparation) -> bool:
    """落库前复核材料版本锚点；任何一条失配即标记生成后源已变化。"""
    built_at = prepared.manifest.built_at
    for ref in prepared.manifest.materials:
        if not resolve_material_content(session, ref, built_at=built_at).ok:
            return True
    return False


def _authorized(
    factory: sessionmaker[Session],
    settings: Settings,
    params: SynthesisRunParamsView,
    prepared: _SynthesisPreparation,
) -> ResolvedSendConfig:
    with session_scope(factory) as session:
        return ensure_send_authorized(
            session,
            settings=settings,
            purpose=params.purpose,
            manifest_hash=params.manifest_hash,
            material_kinds=sorted({entry.kind for entry in prepared.entries}),
        )


def _authorized_on(
    session: Session,
    settings: Settings,
    params: SynthesisRunParamsView,
    prepared: _SynthesisPreparation,
) -> None:
    ensure_send_authorized(
        session,
        settings=settings,
        purpose=params.purpose,
        manifest_hash=params.manifest_hash,
        material_kinds=sorted({entry.kind for entry in prepared.entries}),
    )


def _asset_source_paper_ids(session: Session, ref: MaterialRef) -> set[str]:
    try:
        revision = int(ref.version_key.removeprefix("revision:"))
    except ValueError:
        return set()
    return {
        source.paper_id
        for source in ReadingAssetRepository(session).sources(ref.target_id, revision)
    }


def synthesis_materials_text(entries: list[MaterialEntry]) -> str:
    return "\n\n".join(
        f"[{entry.index}] 类别 {MATERIAL_KIND_LABELS.get(entry.kind, entry.kind)} · "
        f"{_material_subject(entry)} · 版本 {entry.version_key}\n"
        f"{entry.content_text}"
        for entry in entries
    )


def _material_subject(entry: MaterialEntry) -> str:
    if entry.kind == "asset_content":
        return f"资产 {entry.target_id}"
    if entry.kind == "verified_candidate":
        return f"论文 {entry.paper_id or '未知'}／候选 {entry.target_id}"
    return f"论文 {entry.paper_id or entry.target_id}"


def synthesis_facts_text(prepared: PreparedReport) -> str:
    """确定性事实摘要；只含计数、分布与周期，不含任何私人正文。"""
    metrics = prepared.content.metrics
    period = prepared.period
    start = period.start_date.isoformat() if period.start_date else "未限定"
    end = period.end_date_inclusive.isoformat() if period.end_date_inclusive else "未限定"
    lines = [
        f"周期：{period.label}（{start} 至 {end}，时区 {period.timezone}）",
        f"模式：{_mode_label(prepared.query.mode)}；知识截止：{prepared.knowledge_cutoff.isoformat()}",
        f"本期阅读 {metrics.readings_total} 篇（系统 {metrics.readings_system} · "
        f"导入 {metrics.readings_imported}）；补录 {metrics.backfills_total} 篇；"
        f"系统内完成 {metrics.system_completions} 篇",
        f"覆盖率：已评分 {metrics.coverage.scored}/{metrics.coverage.total} · "
        f"有备注 {metrics.coverage.noted}/{metrics.coverage.total}",
        f"反馈变化：评分 {metrics.feedback_changes.score_events} 次 · "
        f"备注 {metrics.feedback_changes.note_events} 次 · "
        f"结构化理由修订 {metrics.structured_change_stats.events} 次",
        f"资产整理活动：{metrics.asset_events} 次操作 / {metrics.asset_assets} 个资产",
        f"全部历史（当前范围）：{metrics.all_history_total} 篇",
        "主题分布：" + _count_entries(metrics.primary_tags),
        "评分分布：" + _score_distribution(metrics.score_distribution),
    ]
    return "\n".join(lines)


def _count_entries(entries: list[Any]) -> str:
    if not entries:
        return "暂无"
    return "、".join(f"{entry.label} {entry.count}" for entry in entries[:8])


def _score_distribution(dist: Any) -> str:
    return (
        f"1 分 {dist.score_1} · 2 分 {dist.score_2} · 3 分 {dist.score_3} · "
        f"4 分 {dist.score_4} · 5 分 {dist.score_5} · 未评分 {dist.unrated}"
    )


def _report_kind_label(period: str) -> str:
    return {"week": "周报", "month": "月报", "all": "全部历史", "custom": "自选周期"}.get(
        period, "回顾"
    )


def _mode_label(mode: ReviewMode) -> str:
    return "当时记录" if mode is ReviewMode.AS_RECORDED else "现在重新回顾"


def _close_provider(gateway: LLMGateway) -> None:
    provider = getattr(gateway, "_provider", None)
    if provider is not None and hasattr(provider, "close"):
        with suppress(Exception):
            provider.close()


@dataclass(frozen=True)
class _ReviewSpec:
    """``prepare_report`` 的结构化参数；不依赖 jobs 模块。"""

    period: str
    mode: str
    anchor_date: date | None = None
    start_date: date | None = None
    end_date: date | None = None
    tag: str | None = None


__all__ += ["synthesis_facts_text", "synthesis_materials_text"]
