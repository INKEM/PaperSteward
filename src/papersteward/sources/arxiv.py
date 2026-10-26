"""arXiv Atom 搜索；只抓元数据，不访问记录中的链接。"""

import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import datetime
from urllib.parse import urlsplit

from pydantic import ValidationError

from papersteward.core.entities import utc_now
from papersteward.sources.contracts import PaperCandidate, QuerySpec, SourceError
from papersteward.sources.http import SourceHTTP

ENDPOINT = "https://export.arxiv.org/api/query"
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"


def compile_query(query: QuerySpec) -> str:
    if query.field == "team":
        raise SourceError("arxiv", "unsupported_query")

    def term(text: str) -> str:
        if any(c in text for c in ('"', "\\")) or any(ord(c) < 32 for c in text):
            raise SourceError("arxiv", "unsupported_query")
        return f'"{text}"'

    field = {"all": "all", "author": "au", "venue": "jr"}[query.field]
    operation = " OR " if query.operator == "any" else " AND "
    result = "(" + operation.join(f"{field}:{term(value)}" for value in query.keywords) + ")"
    for value in query.exclude_keywords:
        result += f" ANDNOT all:{term(value)}"
    if query.date_from or query.date_to:
        start = query.date_from.strftime("%Y%m%d") if query.date_from else "00010101"
        end = query.date_to.strftime("%Y%m%d") if query.date_to else "99991231"
        result += f" AND submittedDate:[{start}0000 TO {end}2359]"
    return result


def parse_feed(body: bytes, retrieved_at: datetime) -> list[PaperCandidate]:
    try:
        if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
            raise ValueError("DTD forbidden")
        feed = ET.fromstring(body)
        if feed.tag != ATOM + "feed":
            raise ValueError("not Atom")
        result: list[PaperCandidate] = []
        for entry in feed.findall(ATOM + "entry"):

            def text(tag: str, entry: ET.Element = entry) -> str | None:
                value = entry.findtext(tag)
                return " ".join(value.split()) if value and value.strip() else None

            url = text(ATOM + "id") or ""
            parsed_url = urlsplit(url)
            identifier = parsed_url.path.removeprefix("/abs/")
            if parsed_url.hostname != "arxiv.org" or not parsed_url.path.startswith("/abs/"):
                raise ValueError("invalid arxiv identity or API error")
            if not re.fullmatch(r"(?:\d{4}\.\d{4,5}|[A-Za-z.-]+/\d{7})(?:v\d+)?", identifier):
                raise ValueError("invalid id")
            result.append(
                PaperCandidate.model_validate(
                    {
                        "title": text(ATOM + "title"),
                        "abstract": text(ATOM + "summary"),
                        "authors": [
                            name
                            for author in entry.findall(ATOM + "author")
                            if (name := author.findtext(ATOM + "name")) and name.strip()
                        ],
                        "doi": text(ARXIV + "doi"),
                        "arxiv_id": identifier,
                        "venue": text(ARXIV + "journal_ref"),
                        "published_at": text(ATOM + "published"),
                        "url": url,
                        "pdf_url": next(
                            (
                                link.get("href")
                                for link in entry.findall(ATOM + "link")
                                if link.get("title") == "pdf"
                            ),
                            None,
                        ),
                        "oa_status": "open",
                        "document_type": "preprint",
                        "provenance": {
                            "source": "arxiv",
                            "source_id": identifier,
                            "url": url,
                            "retrieved_at": retrieved_at,
                            "raw_metadata": {"entry_xml": ET.tostring(entry, encoding="unicode")},
                        },
                    }
                )
            )
        return result
    except (ET.ParseError, ValueError, ValidationError) as exc:
        raise SourceError("arxiv", "invalid_response") from exc


class ArxivAdapter:
    source = "arxiv"

    def __init__(
        self, http: SourceHTTP, *, now: Callable[[], datetime] = utc_now, page_size: int = 100
    ) -> None:
        if not 1 <= page_size <= 100:
            raise ValueError("page_size 必须在1–100之间")
        self.http, self.now, self.page_size = http, now, page_size

    def get_by_id(self, arxiv_id: str) -> PaperCandidate | None:
        """按 arXiv ID 直查（id_list 参数）；未知 ID 返回空 feed → None。

        ``arxiv_id`` 须为规范化的裸 ID（``normalize_arxiv`` 产物）；
        API 返回错误条目时由 parse_feed 抛 SourceError，由调用方降级。
        """
        entries = parse_feed(
            self.http.get(ENDPOINT, {"id_list": arxiv_id, "max_results": 1}), self.now()
        )
        return entries[0] if entries else None

    def search(self, query: QuerySpec) -> list[PaperCandidate]:
        compiled = compile_query(query)
        result: list[PaperCandidate] = []
        seen: set[str] = set()
        while len(result) < query.max_results:
            size = min(self.page_size, query.max_results - len(result))
            body = self.http.get(
                ENDPOINT,
                {
                    "search_query": compiled,
                    "start": len(result),
                    "max_results": size,
                    "sortBy": "submittedDate",
                    "sortOrder": "descending",
                },
            )
            page = parse_feed(body, self.now())
            for candidate in page[:size]:
                if candidate.provenance.source_id in seen:
                    raise SourceError(self.source, "invalid_response")
                seen.add(candidate.provenance.source_id)
                result.append(candidate)
            if len(page) < size:
                break
        return result
