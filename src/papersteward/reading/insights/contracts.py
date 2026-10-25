"""阅读回顾的严格输入/输出契约；不依赖 ORM、路由或模型服务。

回顾是应用数据，不注册为 LLM JSON schema，也不进入 ``SCHEMA_MODELS``。
"""

from datetime import date
from enum import StrEnum
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from papersteward.core.enums import ReadingTimeBasis as ReadingTimeBasis
from papersteward.core.enums import TimePrecision as TimePrecision

PROJECTION_VERSION = "reading-insights.projection/1"


class InsightsModel(BaseModel):
    """回顾契约基类；与 delivery.InputModel 同配置但保持模块独立。"""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)


class ReviewMode(StrEnum):
    """回顾模式：知识截止取历史周期结束（当时记录）还是采集时刻（现在重新回顾）。"""

    AS_RECORDED = "as_recorded"
    REVISED_NOW = "revised_now"


class PeriodKind(StrEnum):
    WEEK = "week"
    MONTH = "month"
    ALL = "all"
    CUSTOM = "custom"


class ReadingSourceKind(StrEnum):
    """归档的来源性质：系统内完成、外部导入或来源未明；与时间精度分开记录。"""

    SYSTEM = "system"
    IMPORTED = "imported"
    UNKNOWN = "unknown"


class PeriodMembership(StrEnum):
    """单篇归档相对活动周期的归属；月精度部分覆盖与未知日期单列，不冒充精确计数。"""

    IN_PERIOD = "in_period"
    OUTSIDE_PERIOD = "outside_period"
    UNASSIGNED_UNKNOWN = "unassigned_unknown"
    UNASSIGNED_PARTIAL_MONTH = "unassigned_partial_month"


class InsightPeriodRequest(InsightsModel):
    """周期选择；周期表示为本地 ``[开始日, 结束日次日)``。"""

    kind: PeriodKind
    timezone: str = Field(min_length=1, max_length=64)
    anchor_date: date | None = None
    start_date: date | None = None
    end_date: date | None = None

    @model_validator(mode="after")
    def coherent(self) -> "InsightPeriodRequest":
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"未知时区：{self.timezone}") from exc
        provided = {
            name
            for name in ("anchor_date", "start_date", "end_date")
            if getattr(self, name) is not None
        }
        if self.kind in (PeriodKind.WEEK, PeriodKind.MONTH):
            if self.anchor_date is None or provided - {"anchor_date"}:
                raise ValueError("周/月周期必须且只能提供 anchor_date")
        elif self.kind is PeriodKind.CUSTOM:
            if self.anchor_date is not None or self.start_date is None or self.end_date is None:
                raise ValueError("自选周期必须提供 start_date 与 end_date，不得提供 anchor_date")
            if self.start_date > self.end_date:
                raise ValueError("自选周期开始不能晚于结束")
        elif provided:
            raise ValueError("全部历史周期不接受日期参数")
        return self


class InsightQueryRequest(InsightsModel):
    """一次事实投影的完整输入；默认还原当时已知内容。"""

    period: InsightPeriodRequest
    mode: ReviewMode = ReviewMode.AS_RECORDED


class ResolvedPeriod(InsightsModel):
    """解析后的周期边界；UTC 边界供查询使用，本地日期供归属判断。"""

    kind: PeriodKind
    timezone: str
    label: str
    start_date: date | None = None
    end_date_inclusive: date | None = None
    start_utc: AwareDatetime | None = None
    end_utc_exclusive: AwareDatetime | None = None


class ReadTimeDeclaration(InsightsModel):
    """一次阅读时间声明；三种精度字段互斥，字段名避开类型名 ``date``。"""

    precision: TimePrecision
    day: date | None = None
    month: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}$")

    @model_validator(mode="after")
    def coherent(self) -> "ReadTimeDeclaration":
        if self.precision is TimePrecision.DAY:
            if self.day is None or self.month is not None:
                raise ValueError("日精度必须提供 day 且不得提供 month")
        elif self.precision is TimePrecision.MONTH:
            if self.month is None or self.day is not None:
                raise ValueError("月精度必须提供 month 且不得提供 day")
        elif self.day is not None or self.month is not None:
            raise ValueError("未知精度不得提供 day 或 month")
        return self


class ReadingTime(ReadTimeDeclaration):
    """一篇文献的阅读时间解释；声明或系统回退得到的当前有效值。"""

    basis: ReadingTimeBasis
    recorded_at_utc: AwareDatetime | None = None


class ReadTimeInput(ReadTimeDeclaration):
    """补记/更正阅读时间的请求；expected_revision 防止两个页面相互覆盖。"""

    expected_revision: int = Field(ge=0, strict=True)
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class ReadTimeRevision(InsightsModel):
    """已记录的一个阅读时间版本；只追加，不修改。"""

    revision: int
    recorded_at: AwareDatetime
    precision: TimePrecision
    basis: ReadingTimeBasis
    day: date | None = None
    month: str | None = None


class ReadTimeView(InsightsModel):
    """一篇归档的阅读时间现状；effective 无声明时回退归档兼容默认。"""

    paper_id: str
    current_revision: int
    effective: ReadingTime
    revisions: list[ReadTimeRevision] = Field(default_factory=list)


class FeedbackVersion(InsightsModel):
    """截止前有效的一个反馈版本；首个版本是归档时的初始反馈。"""

    recorded_at: AwareDatetime
    score: int | None = Field(default=None, ge=1, le=5, strict=True)
    note: str | None = None
    is_initial: bool = False


class ScoreFeedbackChange(InsightsModel):
    kind: Literal["score"]
    paper_id: str
    recorded_at: AwareDatetime
    old: int | None = Field(default=None, ge=1, le=5, strict=True)
    new: int | None = Field(default=None, ge=1, le=5, strict=True)


class NoteFeedbackChange(InsightsModel):
    kind: Literal["note"]
    paper_id: str
    recorded_at: AwareDatetime
    old: str | None = None
    new: str | None = None


FeedbackChange = Annotated[ScoreFeedbackChange | NoteFeedbackChange, Field(discriminator="kind")]


class PaperProjection(InsightsModel):
    """单篇归档在知识截止前的事实；元数据只取冻结值，缺失即未知。"""

    paper_id: str
    archived_at: AwareDatetime
    source: ReadingSourceKind
    content_available: bool
    reading: ReadingTime
    membership: PeriodMembership
    recorded_in_period: bool
    title: str | None = None
    reading_mode: str | None = None
    published_on: date | None = None
    primary_tag: str | None = None
    secondary_tags: list[str] = Field(default_factory=list)
    taxonomy_version: str | None = None
    feedback: list[FeedbackVersion] = Field(default_factory=list)
    changes: list[FeedbackChange] = Field(default_factory=list)


class InsightsProjection(InsightsModel):
    """一次投影的完整输出；同库同请求同采集时刻结果稳定。"""

    projection_version: Literal["reading-insights.projection/1"]
    mode: ReviewMode
    period: ResolvedPeriod
    knowledge_cutoff: AwareDatetime
    collected_at: AwareDatetime
    papers: list[PaperProjection] = Field(default_factory=list)
