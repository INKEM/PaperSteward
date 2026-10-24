"""运行参数：确认完成时落库的调度与阅读偏好。"""

from __future__ import annotations

from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ClockTime = Annotated[str, Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")]
Share = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class RuntimeParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    push_time: ClockTime | None = None
    timezone: str = Field(min_length=1, max_length=64)
    quiet_start: ClockTime | None = None
    quiet_end: ClockTime | None = None
    paused: bool = Field(default=False, strict=True)
    target_shares: dict[str, Share] = Field(min_length=1, max_length=9)
    channel: Literal["local_markdown", "smtp"] | None = None
    external_model_data: Literal["disabled", "metadata_abstract"]
    share_notes: Literal[False] = False

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("需要有效的 IANA 时区") from exc
        return value

    @model_validator(mode="after")
    def coherent_schedule(self) -> RuntimeParameters:
        start, end = self.quiet_start, self.quiet_end
        if (start is None) != (end is None):
            raise ValueError("安静时段起止必须同时提供或同时关闭")
        if start is not None and end is not None and self.push_time is not None:
            if start == end:
                raise ValueError("安静时段起止不能相同")
            within = (
                start <= self.push_time < end
                if start < end
                else (self.push_time >= start or self.push_time < end)
            )
            if within:
                raise ValueError("推送时间不能落在安静时段内")
        if abs(sum(self.target_shares.values()) - 1) > 1e-6:
            raise ValueError("目标主标签占比之和必须为 1，不会自动归一化")
        return self


def default_runtime_parameters(
    *,
    timezone: str,
    target_shares: dict[str, float],
    external_model_data: Literal["disabled", "metadata_abstract"] = "metadata_abstract",
) -> RuntimeParameters:
    """确认完成时的默认运行参数；后续治理流程可单独调整评分权重。"""
    return RuntimeParameters(
        timezone=timezone,
        target_shares=target_shares,
        external_model_data=external_model_data,
    )


__all__ = ["ClockTime", "RuntimeParameters", "Share", "default_runtime_parameters"]
