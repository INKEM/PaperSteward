"""P3 来源边界；候选不含库内 ID、状态或评分，论文内容始终作为数据。"""

from datetime import UTC, date, datetime
from typing import Annotated, Literal, Protocol, Self, runtime_checkable

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

from papersteward.core.enums import OaStatus

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
SourceName = Annotated[Text, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
SourceErrorCode = Literal[
    "timeout", "rate_limited", "unavailable", "invalid_response", "fixture_invalid", "fixture_io"
]


class SourceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class QuerySpec(SourceModel):
    """来源无关的关键词输入；日期闭区间，max_results 为整次调用的候选上限。"""

    keywords: tuple[Text, ...] = Field(min_length=1)
    date_from: date | None = None
    date_to: date | None = None
    max_results: int = Field(default=100, ge=1, le=1000, strict=True)

    @model_validator(mode="after")
    def ordered_dates(self) -> Self:
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from 不能晚于 date_to")
        return self


class SourceProvenance(SourceModel):
    """来源记录溯源，区别于模型/prompt 的 core.Provenance。禁止存认证信息。"""

    source: SourceName
    source_id: Text
    url: Text
    retrieved_at: AwareDatetime
    raw_metadata: dict[str, JsonValue]

    @field_validator("retrieved_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class PaperCandidate(SourceModel):
    """统一元数据；缺失可选字段用 None，作者未知用空元组，OA 未知用 unknown。"""

    title: Text
    provenance: SourceProvenance
    authors: tuple[Text, ...] = ()
    doi: Text | None = None
    arxiv_id: Text | None = None
    venue: Text | None = None
    published_at: AwareDatetime | None = None
    abstract: Text | None = None
    url: Text | None = None
    pdf_url: Text | None = None
    oa_status: OaStatus = OaStatus.UNKNOWN
    language: Text | None = None
    document_type: Text | None = None
    citation_count: int | None = Field(default=None, ge=0, strict=True)

    @field_validator("published_at")
    @classmethod
    def utc_time(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value else None


class SourceError(Exception):
    """可预期来源失败；message 固定，不回显原始响应、请求或认证信息。"""

    def __init__(self, source: str, code: SourceErrorCode, *, retryable: bool = False) -> None:
        self.source = source
        self.code = code
        self.retryable = retryable
        super().__init__(f"来源调用失败：{code}")


@runtime_checkable
class SourceAdapter(Protocol):
    @property
    def source(self) -> str: ...

    def search(self, query: QuerySpec) -> list[PaperCandidate]:
        """按来源顺序返回最多 max_results 篇；空结果为 []，失败抛 SourceError。"""
        ...
