"""冻结归档查询与导出，不提供删除/覆盖入口。"""

from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import PlainTextResponse

from papersteward.api.deps import SessionDep
from papersteward.api.review import _review_errors
from papersteward.delivery.archive import list_archives, read_archive
from papersteward.delivery.contracts import ArchiveRecord
from papersteward.delivery.export import ExportFormat, export_archives

router = APIRouter(prefix="/archive", tags=["archive"])


@router.get("")
def listing(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    tag: str | None = None,
    min_score: Annotated[int | None, Query(ge=1, le=5)] = None,
    query: str | None = None,
    bucket: str | None = None,
) -> dict[str, object]:
    with _review_errors():
        return dict(
            jsonable_encoder(
                list_archives(
                    session,
                    limit=limit,
                    offset=offset,
                    tag=tag,
                    min_score=min_score,
                    query=query,
                    bucket=bucket,
                )
            )
        )


@router.get("/export", response_class=PlainTextResponse)
def export(session: SessionDep, format: ExportFormat = "bibtex") -> str:
    with _review_errors():
        records = []
        offset = 0
        while True:
            page = list_archives(session, limit=200, offset=offset)
            records.extend(page["items"])
            offset += len(page["items"])
            if offset >= page["total"]:
                break
        return export_archives(records, format)


@router.get("/{paper_id}", response_model=ArchiveRecord)
def read(paper_id: str, session: SessionDep) -> ArchiveRecord:
    with _review_errors():
        return read_archive(session, paper_id)
