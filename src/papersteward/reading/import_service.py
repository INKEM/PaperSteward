"""外部已读论文导入：标识解析、去重守卫与单事务编排。

解析与守卫是两步独立能力，供 UI 两步表单与 JSON API 共用；
``import_read_paper`` 是唯一写入路径，复用批次A的 ``archive_imported``
完成快照、状态迁移与补发事件，不放宽其任何不变量。
"""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from papersteward.core.entities import ArchiveImportInput, Paper, Rating
from papersteward.core.enums import STATUS_LABELS, PaperStatus
from papersteward.delivery.contracts import ImportReadPaperInput
from papersteward.errors import ConflictError, InvalidRequestError
from papersteward.pipeline.dedupe import (
    canonicalize,
    normalize_arxiv,
    normalize_doi,
    normalized_text,
)
from papersteward.reading.archive import archive_imported
from papersteward.sources.arxiv import ArxivAdapter
from papersteward.sources.contracts import PaperCandidate, SourceProvenance
from papersteward.sources.http import SourceHTTP
from papersteward.sources.openalex import OpenAlexAdapter
from papersteward.storage.ingestion import attach_source
from papersteward.storage.models import ArchiveSnapshotRow, PaperRow
from papersteward.storage.repositories import PaperRepository, RatingRepository
from papersteward.storage.tx import atomic


class DoiLookup(Protocol):
    """按 DOI 精确直查元数据的能力（OpenAlexAdapter 满足）。"""

    def get_by_doi(self, doi: str) -> PaperCandidate | None: ...


class ArxivLookup(Protocol):
    """按 arXiv ID 精确直查元数据的能力（ArxivAdapter 满足）。"""

    def get_by_id(self, arxiv_id: str) -> PaperCandidate | None: ...


@dataclass(frozen=True)
class ResolvedIdentifier:
    """解析产物：规范化后的候选元数据 + 解析溯源。"""

    candidate: PaperCandidate

    def resolve_payload(self) -> dict[str, Any]:
        """写进归档 content_json.resolve 的溯源摘要。"""
        proof = self.candidate.provenance
        return {
            "source": proof.source,
            "source_id": proof.source_id,
            "url": proof.url,
            "retrieved_at": proof.retrieved_at.isoformat(),
        }


def resolve_identifier(
    raw: str | None,
    *,
    now: datetime,
    openalex: DoiLookup | None = None,
    arxiv: ArxivLookup | None = None,
) -> ResolvedIdentifier | None:
    """规范化用户输入并按标识符直查元数据。

    识别裸 DOI / arXiv ID 及 doi.org、arxiv.org 链接形式（normalize_* 内建）；
    浏览器复制的链接常不带协议头（``arxiv.org/abs/…``），先补全再规范化。
    无标识符返回 None；来源不可达或响应畸形抛 SourceError，由调用方降级手填。
    解析不查库、不写库——去重守卫由调用方另调 ``guard_duplicate``。
    """
    doi = normalize_doi(_with_scheme(raw))
    arxiv_id = None if doi is not None else normalize_arxiv(_with_scheme(raw))
    if doi is None and arxiv_id is None:
        return None
    clients: list[SourceHTTP] = []
    try:
        if doi is not None:
            lookup: DoiLookup = openalex if openalex is not None else _default_openalex(clients)
            candidate = lookup.get_by_doi(doi)
        else:
            assert arxiv_id is not None
            arxiv_lookup: ArxivLookup = arxiv if arxiv is not None else _default_arxiv(clients)
            candidate = arxiv_lookup.get_by_id(arxiv_id)
    finally:
        for client in clients:
            client.close()
    if candidate is None:
        return None
    return ResolvedIdentifier(candidate=canonicalize(candidate))


def _with_scheme(raw: str | None) -> str | None:
    """浏览器复制的链接常省略协议头；仅对已知的两个文献域名补全 https://。"""
    if raw is None:
        return None
    return re.sub(r"^(?:(?:https?://)?(?:www\.)?)?(doi\.org|arxiv\.org)/", r"https://\1/", raw)


def _default_openalex(clients: list[SourceHTTP]) -> DoiLookup:
    client = SourceHTTP("openalex")
    clients.append(client)
    return OpenAlexAdapter(client)


def _default_arxiv(clients: list[SourceHTTP]) -> ArxivLookup:
    client = SourceHTTP("arxiv")
    clients.append(client)
    return ArxivAdapter(client)


def guard_duplicate(
    session: Session, *, title: str, doi: str | None = None, arxiv_id: str | None = None
) -> None:
    """按 doi → arxiv → title 键序查存量，命中即拒绝导入。

    title 为 normalized_text 精确匹配（近似匹配首版不做）。命中 ARCHIVED 给出
    归档日期与详情页指引；其余状态说明导入的适用范围。
    """
    doi_key = normalize_doi(doi)
    arxiv_key = normalize_arxiv(arxiv_id)
    title_key = normalized_text(title)
    rows = session.scalars(select(PaperRow).order_by(PaperRow.first_seen_at, PaperRow.id)).all()
    hit: PaperRow | None = None
    if doi_key is not None:
        hit = next((row for row in rows if (row.doi or "").casefold() == doi_key), None)
    if hit is None and arxiv_key is not None:
        hit = next((row for row in rows if (row.arxiv_id or "").casefold() == arxiv_key), None)
    if hit is None and title_key:
        hit = next((row for row in rows if normalized_text(row.title) == title_key), None)
    if hit is None:
        return
    # PaperRow.status 是字符串值；与枚举做值比较（is 身份比较恒为 False）。
    if hit.status == PaperStatus.ARCHIVED.value:
        snapshot = session.get(ArchiveSnapshotRow, hit.id)
        archived_on = snapshot.archived_at.date().isoformat() if snapshot is not None else "未知"
        raise ConflictError(
            f"该论文已归档（{archived_on}）。如需修订评价，请到详情页操作",
            details={"paper_id": hit.id, "detail_url": f"/ui/papers/{hit.id}"},
        )
    label = STATUS_LABELS.get(hit.status, hit.status)
    raise ConflictError(
        f"该论文已在库中（当前状态：{label}）。导入仅用于添加系统外已读论文",
        details={"paper_id": hit.id, "status": hit.status},
    )


def import_read_paper(session: Session, request: ImportReadPaperInput, *, now: datetime) -> str:
    """导入一篇系统外已读完的论文并归档；返回新论文 ID。

    单事务（atomic）：守卫 → 建档(DONE) → 来源行(manual_import) → 首条评分
    → ``archive_imported``（快照 + ARCHIVED 迁移 + 补发 PAPER_DONE）。
    中途任何失败整体回滚，不留残留行。
    """
    with atomic(session):
        doi_key = normalize_doi(request.doi)
        if request.doi and doi_key is None:
            raise InvalidRequestError("DOI 格式无法识别，请修正或留空", details={"field": "doi"})
        arxiv_key = normalize_arxiv(request.arxiv_id)
        if request.arxiv_id and arxiv_key is None:
            raise InvalidRequestError(
                "arXiv ID 格式无法识别，请修正或留空", details={"field": "arxiv_id"}
            )
        guard_duplicate(session, title=request.title, doi=doi_key, arxiv_id=arxiv_key)
        paper_id = uuid4().hex
        PaperRepository(session).add(
            Paper(
                id=paper_id,
                title=request.title,
                status=PaperStatus.DONE,
                authors=tuple(request.authors),
                venue=request.venue,
                published_at=request.published_at,
                first_seen_at=now,
                abstract=request.abstract,
                doi=doi_key,
                arxiv_id=arxiv_key,
                url=request.url,
            )
        )
        attach_source(
            session, paper_id, _manual_candidate(request, paper_id, doi_key, arxiv_key, now)
        )
        RatingRepository(session).add(
            Rating(
                paper_id,
                request.score,
                request.note,
                tags_at_read=(request.primary_tag,) if request.primary_tag else (),
                created_at=now,
            )
        )
        archive_imported(
            session,
            paper_id,
            now=now,
            import_input=ArchiveImportInput(
                identifier_input=request.identifier_input,
                resolve=request.resolve,
                primary_tag=request.primary_tag,
            ),
        )
        return paper_id


def _manual_candidate(
    request: ImportReadPaperInput,
    paper_id: str,
    doi: str | None,
    arxiv_id: str | None,
    now: datetime,
) -> PaperCandidate:
    """用户确认元数据构造成候选；来源行统一记 manual_import，解析溯源入 raw_metadata。"""
    return PaperCandidate(
        title=request.title,
        provenance=SourceProvenance(
            source="manual_import",
            source_id=doi or arxiv_id or uuid4().hex,
            url=request.url or request.identifier_input or f"urn:paper:{paper_id}",
            retrieved_at=now,
            raw_metadata={
                "identifier_input": request.identifier_input,
                "resolve": request.resolve,
            },
        ),
        authors=tuple(request.authors),
        doi=doi,
        arxiv_id=arxiv_id,
        venue=request.venue,
        published_at=request.published_at,
        abstract=request.abstract,
        url=request.url,
    )


__all__ = [
    "ResolvedIdentifier",
    "guard_duplicate",
    "import_read_paper",
    "resolve_identifier",
]
