"""导入服务的标识解析路由与请求契约；纯逻辑不落库。"""

from datetime import UTC, datetime

import pytest

from papersteward.delivery.contracts import ImportReadPaperInput
from papersteward.reading.import_service import resolve_identifier
from papersteward.sources.contracts import PaperCandidate

NOW = datetime(2026, 10, 22, tzinfo=UTC)


def _candidate(source: str, source_id: str, **overrides: object) -> PaperCandidate:
    values: dict[str, object] = {
        "title": "Resolved Paper",
        "provenance": {
            "source": source,
            "source_id": source_id,
            "url": f"https://example.org/{source_id}",
            "retrieved_at": NOW,
            "raw_metadata": {},
        },
    }
    values.update(overrides)
    return PaperCandidate.model_validate(values)


class _FakeLookup:
    def __init__(self, candidate: PaperCandidate | None) -> None:
        self.candidate = candidate
        self.calls: list[str] = []

    def get_by_doi(self, doi: str) -> PaperCandidate | None:
        self.calls.append(doi)
        return self.candidate

    def get_by_id(self, arxiv_id: str) -> PaperCandidate | None:
        self.calls.append(arxiv_id)
        return self.candidate


def test_resolve_routes_bare_doi_to_openalex() -> None:
    fake = _FakeLookup(_candidate("openalex", "W1", doi="https://doi.org/10.0000/Fixture"))
    resolved = resolve_identifier("10.0000/fixture", now=NOW, openalex=fake)  # type: ignore[arg-type]

    assert fake.calls == ["10.0000/fixture"]
    assert resolved is not None
    # canonicalize 把 OpenAlex 的 doi.org 形式规范为裸 DOI。
    assert resolved.candidate.doi == "10.0000/fixture"
    payload = resolved.resolve_payload()
    assert payload["source"] == "openalex"
    assert payload["source_id"] == "W1"
    assert payload["retrieved_at"] == NOW.isoformat()


def test_resolve_routes_arxiv_id_to_arxiv() -> None:
    fake = _FakeLookup(_candidate("arxiv", "2610.00001v2", arxiv_id="2610.00001v2"))
    resolved = resolve_identifier("2610.00001", now=NOW, arxiv=fake)  # type: ignore[arg-type]

    assert fake.calls == ["2610.00001"]
    assert resolved is not None
    # canonicalize 规范化 arXiv ID（剥版本号）。
    assert resolved.candidate.arxiv_id == "2610.00001"


def test_resolve_accepts_url_forms() -> None:
    doi_fake = _FakeLookup(_candidate("openalex", "W1"))
    resolve_identifier(
        "https://doi.org/10.0000/Fixture",
        now=NOW,
        openalex=doi_fake,  # type: ignore[arg-type]
    )
    assert doi_fake.calls == ["10.0000/fixture"]

    arxiv_fake = _FakeLookup(_candidate("arxiv", "2610.00001v2"))
    resolve_identifier(
        "https://arxiv.org/abs/2610.00001v2",
        now=NOW,
        arxiv=arxiv_fake,  # type: ignore[arg-type]
    )
    assert arxiv_fake.calls == ["2610.00001"]


def test_resolve_returns_none_without_identifier() -> None:
    assert resolve_identifier(None, now=NOW) is None
    assert resolve_identifier("只是一段没有标识符的文字", now=NOW) is None


def test_resolve_accepts_scheme_less_urls() -> None:
    """浏览器复制的链接常不带协议头（表单占位符即此形式）。"""
    doi_fake = _FakeLookup(_candidate("openalex", "W1"))
    resolve_identifier("doi.org/10.0000/Fixture", now=NOW, openalex=doi_fake)  # type: ignore[arg-type]
    assert doi_fake.calls == ["10.0000/fixture"]

    arxiv_fake = _FakeLookup(_candidate("arxiv", "2610.00001v2"))
    resolve_identifier("www.arxiv.org/abs/2610.00001", now=NOW, arxiv=arxiv_fake)  # type: ignore[arg-type]
    assert arxiv_fake.calls == ["2610.00001"]


def test_resolve_returns_none_on_lookup_miss() -> None:
    fake = _FakeLookup(None)
    assert resolve_identifier("10.0000/missing", now=NOW, openalex=fake) is None  # type: ignore[arg-type]
    assert fake.calls == ["10.0000/missing"]


def test_import_request_contract() -> None:
    request = ImportReadPaperInput.model_validate(
        {"title": " 标题 ", "score": 4, "authors": ["作者"], "primary_tag": " T01 "}
    )
    assert request.title == "标题"
    assert request.primary_tag == "T01"
    assert request.doi is None and request.resolve is None

    with pytest.raises(ValueError, match="score"):
        ImportReadPaperInput.model_validate({"title": "x", "score": 6})
    with pytest.raises(ValueError, match="title"):
        ImportReadPaperInput.model_validate({"title": " ", "score": 3})
    with pytest.raises(ValueError, match="extra"):
        ImportReadPaperInput.model_validate({"title": "x", "score": 3, "unknown": 1})
