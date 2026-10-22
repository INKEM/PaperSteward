"""来源失败隔离、规范化/去重/硬过滤与原子入库；不执行任何评分或推送。"""

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from papersteward.core.entities import Event, Paper, Run, utc_now
from papersteward.core.enums import EventType, PaperStatus, RunStatus
from papersteward.core.errors import InvariantViolation
from papersteward.onboarding.service import _atomic
from papersteward.pipeline.context import bound_context
from papersteward.pipeline.dedupe import DedupeGroup, deduplicate, duplicate_reason
from papersteward.pipeline.screen import ScreenConfig, screen
from papersteward.sources.contracts import PaperCandidate, QuerySpec, SourceAdapter, SourceError
from papersteward.sources.queries import generate_queries
from papersteward.storage.ingestion import attach_source, finish_run
from papersteward.storage.models import EventRow, PaperRow, PaperSourceRow
from papersteward.storage.repositories import EventRepository, PaperRepository, RunRepository


def _paper_candidate(paper: Paper) -> PaperCandidate:
    from dataclasses import asdict

    values = asdict(paper)
    for field in ("id", "status", "first_seen_at", "canonical_id"):
        values.pop(field)
    return PaperCandidate.model_validate(
        {
            **values,
            "provenance": {
                "source": "stored",
                "source_id": paper.id,
                "url": paper.url or "urn:paper:" + paper.id,
                "retrieved_at": paper.first_seen_at,
                "raw_metadata": {},
            },
        }
    )


def _existing(session: Session, group: DedupeGroup) -> set[str]:
    found: set[str] = set()
    for row in session.scalars(select(PaperSourceRow)):
        identity_matches = any(
            (row.source, row.source_id) == (p.source, p.source_id) for p in group.provenances
        )
        if identity_matches or any(
            duplicate_reason(member, PaperCandidate.model_validate(row.candidate_json))
            for member in group.members
        ):
            found.add(row.paper_id)
    for paper in PaperRepository(session).list():
        if any(duplicate_reason(member, _paper_candidate(paper)) for member in group.members):
            found.add(paper.id)
    return found


def _write_group(
    session: Session,
    group: DedupeGroup,
    run_id: str,
    at: datetime,
    config: ScreenConfig,
    report: dict[str, Any],
) -> None:
    papers = PaperRepository(session)
    matches = _existing(session, group)
    reasons = screen(group.candidate, config)
    if len(matches) > 1:
        report["filtered"].append(
            {"canonical_id": group.canonical_id, "reasons": ["ambiguous_existing"]}
        )
        return
    if matches:
        paper_id = next(iter(matches))
        old = papers.get(paper_id)
        assert old is not None
        report["existing"] += 1
        if old.status in {PaperStatus.ARCHIVED, PaperStatus.REJECTED}:
            report["filtered"].append(
                {"paper_id": paper_id, "reasons": ["already_" + old.status.value]}
            )
        owned = session.scalar(
            select(EventRow.id)
            .where(
                EventRow.paper_id == paper_id,
                EventRow.event_type == EventType.PAPER_DISCOVERED.value,
                EventRow.payload_json["stage"].as_string() == "ingest",
            )
            .limit(1)
        )
        if owned and old.status in {
            PaperStatus.DISCOVERED,
            PaperStatus.DEDUPED,
            PaperStatus.SCREENED,
        }:
            combined = deduplicate([_paper_candidate(old), *group.members])
            merged = next(
                item.candidate
                for item in combined
                if any(m.provenance.source == "stored" for m in item.members)
            )
            row = session.get(PaperRow, paper_id)
            assert row is not None
            changed = old.authors != merged.authors
            if changed:
                row.authors_json = list(merged.authors)
            for key in (
                "title",
                "venue",
                "published_at",
                "abstract",
                "doi",
                "arxiv_id",
                "url",
                "pdf_url",
                "oa_status",
            ):
                value = getattr(merged, key)
                if getattr(old, key) != value:
                    setattr(row, key, value.value if key == "oa_status" else value)
                    changed = True
            if changed:
                EventRepository(session).append(
                    Event(
                        id=uuid4().hex,
                        event_type=EventType.PAPER_METADATA_UPDATED,
                        paper_id=paper_id,
                        created_at=at,
                        payload_json={
                            "run_id": run_id,
                            "source_ids": [p.source_id for p in group.provenances],
                        },
                    )
                )
            # 收敛早期中断留下的状态：discovered 必须先到 deduped，再到
            # screened/rejected；已在 screened 的文献只在出现新原因时降级为 rejected。
            if old.status is PaperStatus.DISCOVERED:
                papers.transition(
                    paper_id,
                    PaperStatus.DEDUPED,
                    event_id=uuid4().hex,
                    occurred_at=at,
                    payload={"run_id": run_id, "reasons": list(group.reasons)},
                )
            if reasons:
                papers.transition(
                    paper_id,
                    PaperStatus.REJECTED,
                    event_id=uuid4().hex,
                    occurred_at=at,
                    payload={"run_id": run_id, "reasons": reasons},
                )
            elif old.status in {PaperStatus.DISCOVERED, PaperStatus.DEDUPED}:
                papers.transition(
                    paper_id,
                    PaperStatus.SCREENED,
                    event_id=uuid4().hex,
                    occurred_at=at,
                    payload={"run_id": run_id, "reasons": []},
                )
    else:
        item = group.candidate
        paper_id = group.canonical_id
        values = item.model_dump(
            exclude={"provenance", "language", "document_type", "citation_count"}
        )
        papers.add(Paper(id=paper_id, canonical_id=paper_id, first_seen_at=at, **values))
        EventRepository(session).append(
            Event(
                id=uuid4().hex,
                event_type=EventType.PAPER_DISCOVERED,
                paper_id=paper_id,
                created_at=at,
                payload_json={"run_id": run_id, "stage": "ingest"},
            )
        )
        papers.transition(
            paper_id,
            PaperStatus.DEDUPED,
            event_id=uuid4().hex,
            occurred_at=at,
            payload={"run_id": run_id, "reasons": list(group.reasons)},
        )
        papers.transition(
            paper_id,
            PaperStatus.REJECTED if reasons else PaperStatus.SCREENED,
            event_id=uuid4().hex,
            occurred_at=at,
            payload={"run_id": run_id, "reasons": reasons},
        )
        report["new"] += 1
    for member in group.members:
        report["source_versions_added"] += int(attach_source(session, paper_id, member))
    if reasons:
        report["filtered"].append({"paper_id": paper_id, "reasons": reasons})


def ingest(
    session: Session,
    adapters: Sequence[SourceAdapter],
    *,
    run_id: str,
    now: Callable[[], datetime] = utc_now,
    queries: Sequence[QuerySpec] | None = None,
    dry_run: bool = False,
) -> Run:
    started = now()
    profile, binding = bound_context(session, started)
    if not adapters or len({adapter.source for adapter in adapters}) != len(adapters):
        raise InvariantViolation("ingest.sources", "来源不能为空或重复")
    if RunRepository(session).get(run_id) is not None:
        raise InvariantViolation("ingest.run_id", "运行ID已存在；请使用新的运行ID查看幂等结果")
    plan = list(queries) if queries is not None else generate_queries(profile)
    if not 1 <= len(plan) <= 20:
        raise InvariantViolation("ingest.queries", "每来源须为1–20条查询")
    settings = dict(profile.parsed_json.get("ingestion") or {})
    settings.setdefault("exclude_terms", list(profile.parsed_json.get("exclude") or []))
    config = ScreenConfig.model_validate(settings)
    candidates: list[PaperCandidate] = []
    errors: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    for adapter in adapters:
        source_items: list[PaperCandidate] = []
        succeeded = 0
        for index, query in enumerate(plan):
            try:
                page = adapter.search(query)
                if len(page) > query.max_results or any(
                    p.provenance.source != adapter.source for p in page
                ):
                    raise SourceError(adapter.source, "invalid_response")
                if len(source_items) + len(page) + len(candidates) > 1000:
                    raise SourceError(adapter.source, "budget_exceeded")
                source_items.extend(page)
                succeeded += 1
            except SourceError as exc:
                errors.append(
                    {
                        "source": adapter.source,
                        "query_index": index,
                        "code": exc.code,
                        "retryable": exc.retryable,
                    }
                )
                if exc.code != "unsupported_query":
                    break
            except Exception:
                errors.append(
                    {
                        "source": adapter.source,
                        "query_index": index,
                        "code": "invalid_response",
                        "retryable": False,
                    }
                )
                break
        candidates.extend(source_items)
        http = getattr(adapter, "http", None)
        sources.append(
            {
                "source": adapter.source,
                "queries_succeeded": succeeded,
                "candidates": len(source_items),
                "requests": getattr(http, "request_count", 0),
                "cache_hits": getattr(http, "cache_hits", 0),
            }
        )
    groups = deduplicate(candidates)
    stats: dict[str, Any] = {
        "binding": binding,
        "sources": sources,
        "candidates": len(candidates),
        "deduplicated": len(candidates) - len(groups),
        "new": 0,
        "existing": 0,
        "source_versions_added": 0,
        "filtered": [],
        "partial": bool(errors) and any(s["queries_succeeded"] for s in sources),
        "dry_run": dry_run,
    }
    status = (
        RunStatus.SUCCEEDED if any(s["queries_succeeded"] for s in sources) else RunStatus.FAILED
    )
    if dry_run:
        preview = []
        for group in groups:
            matches = _existing(session, group)
            reasons = screen(group.candidate, config)
            if len(matches) > 1:
                reasons.append("ambiguous_existing")
            preview.append(
                {
                    "canonical_id": group.canonical_id,
                    "existing_ids": sorted(matches),
                    "action": "existing" if matches else "create",
                    "reasons": reasons,
                }
            )
        stats["preview"] = preview
        stats["would_create"] = sum(item["action"] == "create" for item in preview)
        finished = now()
        stats["elapsed_seconds"] = max(0, (finished - started).total_seconds())
        return Run(
            id=run_id,
            run_type="ingest",
            status=status,
            started_at=started,
            finished_at=finished,
            stats_json=stats,
            errors_json={"sources": errors},
        )
    with _atomic(session):
        RunRepository(session).add(Run(id=run_id, run_type="ingest", started_at=started))
        try:
            with _atomic(session):
                if bound_context(session, now())[1] != binding:
                    raise InvariantViolation("ingest.binding", "确认依据已变化")
                for group in groups:
                    _write_group(session, group, run_id, now(), config, stats)
        except Exception:
            # 半批元数据/来源/事件已由savepoint撤销；仅保存脱敏的失败运行报告。
            status = RunStatus.FAILED
            stats.update(new=0, existing=0, source_versions_added=0, filtered=[])
            errors.append({"stage": "persist", "code": "pipeline_failed"})
        finished = now()
        stats["elapsed_seconds"] = max(0, (finished - started).total_seconds())
        return finish_run(session, run_id, status, finished, stats, {"sources": errors})
