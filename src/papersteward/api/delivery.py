"""日报生成和只读审阅；文件/邮件投递由本地CLI持有独立事务边界。"""

from datetime import date
from typing import Literal

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from papersteward.api.deps import SessionDep
from papersteward.api.domain_errors import domain_errors
from papersteward.core.entities import utc_now
from papersteward.delivery.contracts import Digest, InputModel
from papersteward.delivery.render import render_html, render_markdown
from papersteward.delivery.service import create_digest
from papersteward.pipeline.context import bound_context
from papersteward.storage.delivery import read_digest

router = APIRouter(prefix="/digests", tags=["delivery"])


class DigestRequest(InputModel):
    digest_date: date
    allow_downgrade: bool = False


@router.post("", response_model=Digest)
def create(body: DigestRequest, session: SessionDep) -> Digest:
    with domain_errors(), session.begin():
        return create_digest(
            session, body.digest_date, now=utc_now(), allow_downgrade=body.allow_downgrade
        )


@router.get("/{day}", response_model=None)
def read(
    day: date, session: SessionDep, format: Literal["json", "markdown", "html"] = "json"
) -> Digest | Response:
    with domain_errors():
        bound_context(session, utc_now(), action="digest.read", enforce_schedule=False)
        digest = read_digest(session, day)
        if format == "html":
            return HTMLResponse(render_html(digest))
        if format == "markdown":
            return PlainTextResponse(render_markdown(digest))
        return digest
