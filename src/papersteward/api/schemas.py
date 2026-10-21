"""API 响应模型；这些字段构成 S3 的最小对外契约。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class HealthResponse(BaseModel):
    """``/health`` 的响应；数据库不可用时状态为 degraded。"""

    status: Literal["ok", "degraded"] = Field(description="ok 或 degraded")
    version: str = Field(description="PaperSteward 版本")
    environment: str = Field(description="运行环境：dev/test/prod")
    database: Literal["ok", "unavailable"] = Field(description="数据库连通性")


class PaperRead(BaseModel):
    """论文列表项；P0 只暴露 stub 字段。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    status: str
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _ensure_utc(cls, value: datetime) -> datetime:
        # SQLite 读取出来是 naive datetime；在 API 边界统一补成 UTC。
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class PaperListResponse(BaseModel):
    """``GET /papers`` 的稳定列表信封。"""

    items: list[PaperRead]
    total: int
    limit: int
    offset: int


__all__ = ["HealthResponse", "PaperListResponse", "PaperRead"]
