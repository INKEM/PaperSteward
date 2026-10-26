"""外部已读论文导入的 JSON API；页面两步表单（批次C）复用同一服务层。"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict

from papersteward.api.deps import SessionDep
from papersteward.api.domain_errors import domain_errors
from papersteward.core.entities import utc_now
from papersteward.delivery.contracts import ImportReadPaperInput
from papersteward.reading.import_service import import_read_paper

router = APIRouter(tags=["papers"])


class ImportReadPaperResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_id: str
    detail_url: str


@router.post(
    "/papers/import",
    response_model=ImportReadPaperResult,
    status_code=status.HTTP_201_CREATED,
    summary="导入一篇系统外已读完的论文并归档",
)
def import_paper(body: ImportReadPaperInput, session: SessionDep) -> ImportReadPaperResult:
    with domain_errors(), session.begin():
        paper_id = import_read_paper(session, body, now=utc_now())
    return ImportReadPaperResult(
        paper_id=paper_id, detail_url=f"/ui/papers/{quote(paper_id, safe='')}"
    )
