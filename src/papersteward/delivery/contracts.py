"""P6 冻结的日报与动作输入；内容保存时使用JSON副本。"""

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

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
    kind: Literal["done", "score", "note", "snooze", "skip", "reject"]
    mode: ReadMode | None = None
    score: int | None = Field(default=None, ge=1, le=5, strict=True)
    note: str | None = Field(default=None, max_length=10000)
    days: Literal[3, 7, 14] | None = None
