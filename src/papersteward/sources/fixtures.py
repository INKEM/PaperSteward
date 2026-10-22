"""离线规范化 cassette；不访问网络，也不模拟真实来源的检索语义。"""

from pathlib import Path
from typing import Literal, Self

from pydantic import Field, ValidationError, model_validator

from papersteward.sources.contracts import (
    PaperCandidate,
    QuerySpec,
    SourceError,
    SourceErrorCode,
    SourceModel,
    SourceName,
)


class SourceCassette(SourceModel):
    schema_version: Literal[1]
    source: SourceName
    items: tuple[PaperCandidate, ...]
    error: SourceErrorCode | None = None
    retryable: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def consistent_response(self) -> Self:
        if any(item.provenance.source != self.source for item in self.items):
            raise ValueError("候选来源与 cassette 不一致")
        if (self.error and self.items) or (self.retryable and not self.error):
            raise ValueError("成功与失败响应不能混用")
        return self


def load_cassette(path: Path, *, source: str) -> SourceCassette:
    try:
        cassette = SourceCassette.model_validate_json(path.read_text(encoding="utf-8"))
        if cassette.source != source:
            raise SourceError(source, "fixture_invalid")
        return cassette
    except (ValidationError, UnicodeError) as exc:
        raise SourceError(source, "fixture_invalid") from exc
    except OSError as exc:
        raise SourceError(source, "fixture_io") from exc


class FakeSourceAdapter:
    """每次重放同一文件；记录查询，只执行结果上限；无缓存、重试或副作用。"""

    def __init__(self, path: Path, *, source: str = "fake") -> None:
        self._source = source
        self._path = path
        self.calls: list[QuerySpec] = []

    @property
    def source(self) -> str:
        return self._source

    def search(self, query: QuerySpec) -> list[PaperCandidate]:
        self.calls.append(query)
        cassette = load_cassette(self._path, source=self.source)
        if cassette.error:
            raise SourceError(self.source, cassette.error, retryable=cassette.retryable)
        return list(cassette.items[: query.max_results])
