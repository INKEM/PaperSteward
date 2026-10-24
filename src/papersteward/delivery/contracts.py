"""P6 冻结的日报与动作输入；内容保存时使用JSON副本。"""

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from papersteward.core.enums import ReadMode
from papersteward.scoring.contracts import QueueItem


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)


class DigestCard(QueueItem):
    read_mode: ReadMode
    reason: str
    authors: list[str] = Field(default_factory=list)
    venue: str | None = None
    url: str | None = None
    pdf_url: str | None = None
    code_url: str | None = None
    secondary_tags: list[str] = Field(default_factory=list)
    filing_bucket: str | None = None


class Digest(InputModel):
    digest_date: date
    timezone: str
    binding: dict[str, Any]
    items: list[DigestCard]
    queue_total: int = Field(ge=0)
    pending_review: int = Field(ge=0)
    deferred: dict[str, str] = Field(default_factory=dict)
    inventory_mode: bool = False
    special_slots: int = Field(ge=0, le=2)
    special_status: str = "特殊桶完整编排将在P7启用"


class Action(InputModel):
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    kind: Literal["done", "score", "note", "reject", "restore"]
    mode: ReadMode | None = None
    score: int | None = Field(default=None, ge=1, le=5, strict=True)
    note: str | None = Field(default=None, max_length=10000)

    @model_validator(mode="after")
    def coherent(self) -> "Action":
        allowed = {
            "done": {"mode", "score", "note"},
            "score": {"score"},
            "note": {"note"},
            "reject": {"note"},
            "restore": set(),
        }[self.kind]
        if (self.model_fields_set - {"kind", "request_id"}) - allowed:
            raise ValueError("操作包含不适用字段")
        if self.kind == "done" and self.mode is None:
            raise ValueError("完成必须提供实际阅读方式")
        if self.kind in {"score", "note"} and self.kind not in self.model_fields_set:
            raise ValueError("修订必须明确提供字段，null表示清除")
        if self.kind == "reject" and not self.note:
            raise ValueError("不相关操作须填写原因")
        return self


class ArchiveRecord(InputModel):
    paper_id: str
    archived_at: datetime
    primary_tag: str
    secondary_tags: list[str]
    filing_bucket_id: str
    content_hash: str
    content: dict[str, Any]
    initial_score: int | None
    initial_note: str | None
    score: int | None
    note: str | None
    revision_history: list[dict[str, Any]]
    content_available: bool
