"""研究记忆：受授权的推断、追加式用户决定与来源治理。"""

import json
from collections.abc import Callable
from datetime import datetime
from typing import cast
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from papersteward.config import Settings
from papersteward.core.entities import utc_now
from papersteward.errors import (
    ConflictError,
    InvalidRequestError,
    NotAuthorizedError,
    NotFoundError,
)
from papersteward.llm.cost import record_llm_usage
from papersteward.llm.errors import LLMError
from papersteward.reading.insights.authorization import (
    build_analysis_gateway,
    ensure_send_authorized,
)
from papersteward.reading.insights.contracts import AnalysisManifestContent, MaterialRef
from papersteward.reading.insights.materials import manifest_content_of, resolve_material_content
from papersteward.reading.insights.memory_contracts import (
    MEMORY_PURPOSE,
    MemoryDecisionInput,
    MemoryGroup,
    MemoryRevisionView,
)
from papersteward.reading.insights.memory_evidence import (
    begin_memory_snapshot,
    group_memory_evidence,
    memory_revision_hash,
)
from papersteward.schemas import ReadingProfileInferenceOutput
from papersteward.storage.db import session_scope
from papersteward.storage.delivery import content_hash
from papersteward.storage.insights import ReadingAnalysisManifestRepository
from papersteward.storage.mappers import as_utc
from papersteward.storage.models import (
    ArchiveSnapshotRow,
    ReadingAnalysisCandidateRow,
    ReadingAnalysisDecisionRow,
    ReadingAnalysisGrantRow,
    ReadingDerivedCopyRow,
    ReadingMemoryRevisionRow,
    ReadingMemoryRow,
)
from papersteward.storage.reading_memory import ReadingMemoryRepository

MEMORY_MAX_CHARS = 400_000
MEMORY_MAX_GROUPS = 16
_INACTIVE = {"disabled", "not_preference", "deferred"}


def _latest_by_key(session: Session) -> dict[str, ReadingMemoryRevisionRow]:
    return {str(row.group_json["key"]): row for row in ReadingMemoryRepository(session).current()}


def run_insight_memory(
    factory: sessionmaker[Session],
    settings: Settings,
    *,
    manifest_id: str,
    manifest_hash: str,
    grant_id: str,
    run_id: str,
    should_abort: Callable[[], bool],
    progress: Callable[..., None],
) -> dict[str, object]:
    """真实读快照 → 事务外模型 → BEGIN IMMEDIATE 短写，返回的进度不含私人文本。"""
    with session_scope(factory) as session:
        begin_memory_snapshot(session)
        manifest_row = ReadingAnalysisManifestRepository(session).get(manifest_id)
        if (
            manifest_row is None
            or manifest_row.purpose != MEMORY_PURPOSE
            or manifest_row.manifest_hash != manifest_hash
        ):
            raise NotAuthorizedError("研究记忆清单不存在或已变化，请重新预览")
        content = manifest_content_of(manifest_row)
        _check_versions(session, content)
        groups = group_memory_evidence(session, content)
        current = _latest_by_key(session)
        # 已由用户处理的条目只更新证据/提示复核，绝不再次发送私人改写或生成替代措辞。
        pending_groups = [
            group
            for group in groups
            if group.key not in current
            or (
                current[group.key].origin == "model"
                and current[group.key].status == "pending"
                and current[group.key].group_json["fingerprint"] != group.fingerprint
            )
        ]
        selected = pending_groups[:MEMORY_MAX_GROUPS]
        selected_refs = {
            (item.ref.kind, item.ref.target_id, item.ref.version_key)
            for group in selected
            for item in group.evidence
        }
        texts = []
        for ref in content.materials:
            if (ref.kind, ref.target_id, ref.version_key) in selected_refs:
                material = resolve_material_content(session, ref, built_at=content.built_at)
                if not material.ok:
                    raise ConflictError("来源材料已缺失或变化，请重新预览；本次未发送")
                texts.append({"ref": ref.model_dump(mode="json"), "text": material.text})
        chars = len(
            json.dumps(
                {
                    "groups": [group.model_dump(mode="json") for group in selected],
                    "materials": texts,
                },
                ensure_ascii=False,
            )
        )
        if chars > MEMORY_MAX_CHARS:
            raise InvalidRequestError("研究记忆材料超过单次预算，请排除部分材料后重新预览")
    if should_abort():
        return {"candidates": 0, "stopped_reason": "用户停止任务"}

    def authorize(session: Session) -> None:
        if should_abort():
            raise ConflictError("研究记忆任务已停止，本次结果未保存")
        _check_versions(session, content)
        resolved = ensure_send_authorized(
            session,
            settings=settings,
            purpose=MEMORY_PURPOSE,
            manifest_hash=manifest_hash,
            material_kinds=list(content.coverage.by_kind),
        )
        if resolved.grant_id != grant_id:
            raise NotAuthorizedError("研究记忆授权已变化，请重新预览并授权")

    def guard() -> None:
        with session_scope(factory) as session:
            authorize(session)

    generated = None
    if selected:
        with session_scope(factory) as session:
            authorize(session)
            resolved = ensure_send_authorized(
                session,
                settings=settings,
                purpose=MEMORY_PURPOSE,
                manifest_hash=manifest_hash,
                material_kinds=list(content.coverage.by_kind),
            )
        progress(stage="synthesizing")
        gateway = build_analysis_gateway(resolved, before_send=guard)
        try:
            generated = gateway.complete_json(
                "reading_profile_inference",
                {
                    "groups": [group.model_dump(mode="json") for group in selected],
                    "materials": texts,
                },
            )
        except LLMError:
            raise InvalidRequestError(
                "研究记忆模型分析未完成，请检查连接后重试；本次没有保存候选"
            ) from None
        finally:
            close = getattr(gateway.provider, "close", None)
            if close is not None:
                close()
    if should_abort():
        return {"candidates": 0, "stopped_reason": "用户停止任务"}
    progress(stage="saving")
    candidates = (
        [] if generated is None else cast(ReadingProfileInferenceOutput, generated.data).candidates
    )
    allowed = {group.key: group for group in selected}
    rejected = 0
    saved = 0
    reviewed = 0
    now = as_utc(utc_now())
    with session_scope(factory, immediate=True) as session:
        # 无模型结果也须绑定当初的用户决定；禁止旧任务绕过新的停用。
        authorize(session)
        current = _latest_by_key(session)
        seen: set[str] = set()
        for candidate in candidates:
            group = allowed.get(candidate.group_key)
            if group is None or candidate.group_key in seen:
                rejected += 1
                continue
            seen.add(candidate.group_key)
            existing = current.get(group.key)
            if existing is not None and (
                existing.origin != "model" or existing.status != "pending"
            ):
                continue
            identity = ReadingMemoryRepository(session).by_group(group.key)
            if identity is None:
                identity = ReadingMemoryRow(id=uuid4().hex, group_key=group.key, created_at=now)
                session.add(identity)
                session.flush()
            row = ReadingMemoryRevisionRow(
                memory_id=identity.id,
                revision=existing.revision + 1 if existing else 1,
                kind=group.kind,
                status="pending",
                scope="task_only" if group.single_task else "general",
                statement=candidate.statement,
                explanation=candidate.uncertainty,
                origin="model",
                recorded_at=now,
                group_json=group.model_dump(mode="json"),
                manifest_id=manifest_id,
                grant_id=grant_id,
                run_id=run_id,
                provenance_json={}
                if generated is None
                else {
                    "model": generated.provenance.model,
                    "prompt_version": generated.prompt_version,
                    "input_hash": generated.provenance.input_hash,
                    "generated_at": generated.provenance.generated_at.isoformat(),
                },
            )
            session.add(row)
            _register_sources(session, row, now=now)
            saved += 1
        for group in groups:
            existing = current.get(group.key)
            if existing is None or existing.status in _INACTIVE or existing.origin == "model":
                continue
            if (
                existing.group_json["fingerprint"] == group.fingerprint
                and not memory_revision_view(session, existing).source_problems
            ):
                continue
            # 新证据只提示复核，保留用户措辞、范围、限制与决定。
            updated = _append_copy(
                session,
                existing,
                now=now,
                status="needs_review",
                origin="source_change",
                group=group,
            )
            updated.manifest_id = manifest_id
            updated.grant_id = grant_id
            updated.run_id = run_id
            reviewed += 1
        session.flush()
        if generated is not None:
            record_llm_usage(session, run_id, generated.usage)
    return {
        "candidates": saved,
        "needs_review": reviewed,
        "rejected_items": rejected,
        "groups": len(groups),
        "omitted_groups": max(0, len(pending_groups) - MEMORY_MAX_GROUPS),
        "materials": len(texts),
        "omitted_papers": content.coverage.omitted_papers,
        "omitted_materials": content.coverage.omitted_materials,
        "insufficient_material": not groups or (generated is not None and not candidates),
        "unchanged": not saved and not reviewed,
        "usage": {} if generated is None else generated.usage.as_json(),
    }


def _check_versions(session: Session, content: AnalysisManifestContent) -> None:
    if content.memory_revision_hash != memory_revision_hash(session):
        raise ConflictError("画像决定已更新，请重新预览；旧任务不会覆盖你的修正")
    for ref in content.materials:
        if not memory_material_available(session, ref, built_at=content.built_at):
            raise ConflictError("研究记忆来源已缺失或变化，请重新预览")


def memory_material_available(session: Session, ref: MaterialRef, *, built_at: datetime) -> bool:
    """保守复查间接来源；确认资产不消除其原始候选的授权边界。"""
    if not resolve_material_content(session, ref, built_at=built_at).ok:
        return False
    candidates = []
    if ref.kind == "verified_candidate":
        candidates = [ref.target_id]
    elif ref.kind == "asset_content":
        from papersteward.storage.insights import ReadingAssetRepository

        revision = int(ref.version_key.removeprefix("revision:"))
        for source in ReadingAssetRepository(session).sources(ref.target_id, revision):
            archive = session.get(ArchiveSnapshotRow, source.paper_id)
            if archive is None or archive.content_hash != source.archive_hash:
                return False
        candidates = list(
            session.scalars(
                select(ReadingAnalysisDecisionRow.candidate_id).where(
                    ReadingAnalysisDecisionRow.asset_id == ref.target_id,
                    ReadingAnalysisDecisionRow.asset_revision <= revision,
                )
            )
        )
    for candidate_id in candidates:
        candidate = session.get(ReadingAnalysisCandidateRow, candidate_id)
        if candidate is None or candidate.stale:
            return False
        grant = session.get(ReadingAnalysisGrantRow, candidate.grant_id)
        if grant is None or grant.revoked_at is not None:
            return False
        manifest = ReadingAnalysisManifestRepository(session).get(candidate.manifest_id)
        if manifest is None:
            return False
        original = manifest_content_of(manifest)
        if any(
            not resolve_material_content(session, item, built_at=original.built_at).ok
            for item in original.materials
        ):
            return False
    return True


def _register_sources(session: Session, row: ReadingMemoryRevisionRow, *, now: datetime) -> None:
    group = MemoryGroup.model_validate(row.group_json)
    keys: set[tuple[str, str, int | None]] = set()
    for item in group.evidence:
        ref = item.ref
        kind = (
            "asset"
            if ref.kind == "asset_content"
            else "candidate"
            if ref.kind == "verified_candidate"
            else "paper"
        )
        revision = (
            int(ref.version_key.removeprefix("revision:"))
            if ref.version_key.startswith("revision:")
            else None
        )
        keys.add((kind, ref.target_id, revision))
        if ref.paper_id:
            keys.add(("paper", ref.paper_id, None))
    for kind, source_id, revision in sorted(
        keys, key=lambda item: (item[0], item[1], item[2] or 0)
    ):
        existing = session.scalar(
            select(ReadingDerivedCopyRow.id)
            .where(
                ReadingDerivedCopyRow.source_kind == kind,
                ReadingDerivedCopyRow.source_id == source_id,
                ReadingDerivedCopyRow.source_revision == revision,
                ReadingDerivedCopyRow.derived_kind == "memory",
                ReadingDerivedCopyRow.derived_id == row.memory_id,
            )
            .limit(1)
        )
        if existing is None:
            session.add(
                ReadingDerivedCopyRow(
                    id=uuid4().hex,
                    source_kind=kind,
                    source_id=source_id,
                    source_revision=revision,
                    derived_kind="memory",
                    derived_id=row.memory_id,
                    field_kind="memory",
                    created_at=now,
                )
            )
            session.flush()


def memory_revision_view(session: Session, row: ReadingMemoryRevisionRow) -> MemoryRevisionView:
    group = MemoryGroup.model_validate(row.group_json)
    problems = []
    grant = session.get(ReadingAnalysisGrantRow, row.grant_id)
    if grant is None or grant.revoked_at is not None:
        problems.append("分析授权已撤回或不存在；本地副本仍保留，可在此删除")
    problems.extend(
        f"来源已缺失或撤回：{item.ref.target_id}（{item.ref.version_key}）"
        for item in group.evidence
        if not memory_material_available(session, item.ref, built_at=as_utc(row.recorded_at))
    )
    effective = "needs_review" if problems and row.status not in _INACTIVE else row.status
    return MemoryRevisionView.model_validate(
        {
            "memory_id": row.memory_id,
            "revision": row.revision,
            "kind": row.kind,
            "status": row.status,
            "scope": row.scope,
            "statement": row.statement,
            "explanation": row.explanation,
            "origin": row.origin,
            "recorded_at": as_utc(row.recorded_at),
            "group": group,
            "manifest_id": row.manifest_id,
            "grant_id": row.grant_id,
            "run_id": row.run_id,
            "provenance": row.provenance_json,
            "source_problems": list(dict.fromkeys(problems)),
            "effective_status": effective,
            "usable": row.status == "confirmed" and not problems,
        }
    )


def get_memory(session: Session, memory_id: str, revision: int | None = None) -> MemoryRevisionView:
    row = (
        session.get(ReadingMemoryRevisionRow, (memory_id, revision))
        if revision is not None
        else ReadingMemoryRepository(session).latest(memory_id)
    )
    if row is None:
        raise NotFoundError("研究记忆或版本不存在")
    return memory_revision_view(session, row)


def list_memory(
    session: Session, *, keyword: str = "", kind: str = "", start: str = "", end: str = ""
) -> list[MemoryRevisionView]:
    result = []
    for row in ReadingMemoryRepository(session).current():
        if kind and row.kind != kind:
            continue
        if (
            keyword
            and keyword.casefold()
            not in (row.statement + str(row.group_json) + (row.explanation or "")).casefold()
        ):
            continue
        evidence = MemoryGroup.model_validate(row.group_json).evidence
        # 时间筛选使用实际反馈时间；未知时间不会猜入区间。
        times = [
            as_utc(item.feedback_at).date().isoformat() for item in evidence if item.feedback_at
        ]
        if (start or end) and not any(
            (not start or stamp >= start) and (not end or stamp <= end) for stamp in times
        ):
            continue
        result.append(memory_revision_view(session, row))
    return result


def decide_memory(
    session: Session, memory_id: str, data: MemoryDecisionInput, *, now: datetime
) -> MemoryRevisionView:
    repository = ReadingMemoryRepository(session)
    digest = content_hash(
        {"memory_id": memory_id, **data.model_dump(mode="json", exclude={"idempotency_key"})}
    )
    existing = repository.request(data.idempotency_key)
    if existing is not None:
        if existing.request_hash != digest:
            raise ConflictError("同一保存请求不能用于不同条目或内容，请保留输入并使用新请求重试")
        return memory_revision_view(session, existing)
    row = repository.latest(memory_id)
    if row is None:
        raise NotFoundError("研究记忆不存在")
    if row.revision != data.expected_revision:
        raise ConflictError("研究记忆已有新版本，请核对最新内容后重试；你的输入尚未保存")
    status = {
        "confirm": "confirmed",
        "revise": row.status,
        "task_only": "confirmed",
        "not_preference": "not_preference",
        "defer": "deferred",
        "disable": "disabled",
        "review": "needs_review",
    }[data.action]
    if (
        data.action in {"confirm", "task_only"}
        and memory_revision_view(session, row).source_problems
    ):
        raise ConflictError("来源已撤回或缺失，请先补充证据后重新分析；不能确认旧来源")
    new = _append_copy(session, row, now=now, status=status, origin="user")
    new.statement = data.statement if data.statement is not None else row.statement
    new.explanation = data.explanation if data.explanation is not None else row.explanation
    new.scope = "task_only" if data.action == "task_only" else row.scope
    new.request_key = data.idempotency_key
    new.request_hash = digest
    try:
        session.flush()
    except IntegrityError as exc:
        raise ConflictError("另一次保存已完成，请保留输入并重新核对") from exc
    return memory_revision_view(session, new)


def _append_copy(
    session: Session,
    row: ReadingMemoryRevisionRow,
    *,
    now: datetime,
    status: str,
    origin: str,
    group: MemoryGroup | None = None,
) -> ReadingMemoryRevisionRow:
    new = ReadingMemoryRevisionRow(
        memory_id=row.memory_id,
        revision=row.revision + 1,
        kind=row.kind,
        status=status,
        scope=row.scope,
        statement=row.statement,
        explanation=row.explanation,
        origin=origin,
        recorded_at=as_utc(now),
        group_json=row.group_json if group is None else group.model_dump(mode="json"),
        manifest_id=row.manifest_id,
        grant_id=row.grant_id,
        run_id=row.run_id,
        provenance_json=row.provenance_json,
    )
    session.add(new)
    if group is not None:
        _register_sources(session, new, now=now)
    return new


def invalidate_memory_sources(
    session: Session,
    *,
    grant_id: str | None = None,
    source_kind: str | None = None,
    source_id: str | None = None,
) -> list[str]:
    """来源/授权撤回追加需复核版本；停用与纠错保持原状态，正文不写入审计。"""

    ids = set()
    if source_kind is not None:
        ids = set(
            session.scalars(
                select(ReadingDerivedCopyRow.derived_id).where(
                    ReadingDerivedCopyRow.source_kind == source_kind,
                    ReadingDerivedCopyRow.source_id == source_id,
                    ReadingDerivedCopyRow.derived_kind == "memory",
                )
            )
        )
    if grant_id is not None:
        candidates = list(
            session.scalars(
                select(ReadingAnalysisCandidateRow.id).where(
                    ReadingAnalysisCandidateRow.grant_id == grant_id
                )
            )
        )
        assets = list(
            session.scalars(
                select(ReadingAnalysisDecisionRow.asset_id).where(
                    ReadingAnalysisDecisionRow.candidate_id.in_(candidates)
                )
            )
        )
        for source_type, source_ids in (("candidate", candidates), ("asset", assets)):
            for identity in source_ids:
                ids.update(
                    affected_memory_sources(
                        session, source_kind=source_type, source_id=str(identity)
                    )
                )
    affected = []
    for row in ReadingMemoryRepository(session).current():
        if (grant_id is not None and row.grant_id == grant_id) or row.memory_id in ids:
            affected.append(row.memory_id)
            if row.status not in _INACTIVE and row.status != "needs_review":
                _append_copy(
                    session, row, now=utc_now(), status="needs_review", origin="source_change"
                )
    session.flush()
    return affected


def delete_memory(session: Session, memory_id: str) -> None:
    row = session.get(ReadingMemoryRow, memory_id)
    if row is None:
        raise NotFoundError("研究记忆不存在")
    session.execute(
        delete(ReadingDerivedCopyRow).where(
            ReadingDerivedCopyRow.derived_kind == "memory",
            ReadingDerivedCopyRow.derived_id == memory_id,
        )
    )
    session.delete(row)
    session.flush()


def affected_memory_sources(session: Session, *, source_kind: str, source_id: str) -> list[str]:

    return sorted(
        set(
            session.scalars(
                select(ReadingDerivedCopyRow.derived_id).where(
                    ReadingDerivedCopyRow.source_kind == source_kind,
                    ReadingDerivedCopyRow.source_id == source_id,
                    ReadingDerivedCopyRow.derived_kind == "memory",
                )
            )
        )
    )
