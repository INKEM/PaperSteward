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
    tag: str | None = None


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


class CoverageStats(InsightsModel):
    """覆盖统计；空集合（total=0）由展示层显示「暂无」，不显示虚假的 0%。"""

    scored: int = Field(ge=0)
    noted: int = Field(ge=0)
    total: int = Field(ge=0)


class ScoreDistribution(InsightsModel):
    """本期阅读集合的评分档分布；1–5 分及未评分互斥且完备。"""

    score_1: int = Field(default=0, ge=0)
    score_2: int = Field(default=0, ge=0)
    score_3: int = Field(default=0, ge=0)
    score_4: int = Field(default=0, ge=0)
    score_5: int = Field(default=0, ge=0)
    unrated: int = Field(default=0, ge=0)


class FeedbackChangeStats(InsightsModel):
    """本期发生的实际认知变化；初始归档不算修订，值未变不产事件。"""

    score_events: int = Field(default=0, ge=0)
    note_events: int = Field(default=0, ge=0)
    papers_affected: int = Field(default=0, ge=0)


class CountEntry(InsightsModel):
    """分布表的一个分组；主题分布以 (code, taxonomy_version) 互斥分组。"""

    key: str
    label: str
    count: int = Field(ge=0)
    taxonomy_version: str | None = None


class InsightMetrics(InsightsModel):
    """事实概览指标；口径见阶段计划 §3.3，阅读量与补录量可能重叠。"""

    period: ResolvedPeriod
    mode: ReviewMode
    knowledge_cutoff: AwareDatetime
    collected_at: AwareDatetime
    readings_total: int = Field(ge=0)
    readings_system: int = Field(ge=0)
    readings_imported: int = Field(ge=0)
    readings_backfilled: int = Field(ge=0)
    system_completions: int = Field(ge=0)
    backfills_total: int = Field(ge=0)
    unassigned_total: int = Field(ge=0)
    partial_month_total: int = Field(ge=0)
    coverage: CoverageStats
    backfill_coverage: CoverageStats
    score_distribution: ScoreDistribution
    feedback_changes: FeedbackChangeStats
    # 资产整理活动：版本数=操作数，去重资产数=不同资产；不增加已读篇数。
    asset_events: int = Field(default=0, ge=0)
    asset_assets: int = Field(default=0, ge=0)
    reading_modes: list[CountEntry] = Field(default_factory=list)
    primary_tags: list[CountEntry] = Field(default_factory=list)
    published_decades: list[CountEntry] = Field(default_factory=list)
    all_history_total: int = Field(ge=0)
    source_distribution: list[CountEntry] = Field(default_factory=list)
    date_precision_distribution: list[CountEntry] = Field(default_factory=list)


class InsightOverview(InsightsModel):
    """一次事实概览：指标与四组明细由同一函数产出，计数天然一致。"""

    metrics: InsightMetrics
    readings: list[PaperProjection] = Field(default_factory=list)
    backfills: list[PaperProjection] = Field(default_factory=list)
    changes: list[FeedbackChange] = Field(default_factory=list)
    unassigned: list[PaperProjection] = Field(default_factory=list)


# —— 报告快照契约：冻结正文剥离私人备注原文 ————————————————————————

REPORT_CONTRACT = "reading-insights.report/1"


class ReportFeedbackVersion(InsightsModel):
    """报告内的反馈版本：只保存评分事实与备注有无，不保存备注原文。"""

    recorded_at: AwareDatetime
    score: int | None = Field(default=None, ge=1, le=5, strict=True)
    has_note: bool
    is_initial: bool = False


class ReportChange(InsightsModel):
    """报告内的变化事件：评分保留前后值；备注只留变更标记。"""

    kind: Literal["score", "note"]
    paper_id: str
    recorded_at: AwareDatetime
    old_score: int | None = Field(default=None, ge=1, le=5, strict=True)
    new_score: int | None = Field(default=None, ge=1, le=5, strict=True)


class ReportPaper(InsightsModel):
    """报告内单篇归档的冻结事实；feedback 为去隐私版本。"""

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
    feedback: list[ReportFeedbackVersion] = Field(default_factory=list)


class ReportAssetRef(InsightsModel):
    """报告内的资产引用；body 仅在用户显式勾选时保存正文副本。"""

    asset_id: str
    title: str
    kind: str
    status: str
    body: str | None = None


class InsightReportContent(InsightsModel):
    """报告快照的冻结正文；读取时的唯一内容来源。"""

    contract: Literal["reading-insights.report/1"]
    metrics: InsightMetrics
    readings: list[ReportPaper] = Field(default_factory=list)
    backfills: list[ReportPaper] = Field(default_factory=list)
    changes: list[ReportChange] = Field(default_factory=list)
    unassigned: list[ReportPaper] = Field(default_factory=list)
    assets: list[ReportAssetRef] = Field(default_factory=list)


class ReportSummary(InsightsModel):
    """报告列表条目与详情的元信息。"""

    report_id: str
    report_kind: str
    period_label: str
    period_start: date | None = None
    period_end: date | None = None
    timezone: str
    mode: ReviewMode
    knowledge_cutoff: AwareDatetime
    generated_at: AwareDatetime
    contract: str
    run_id: str


class ReportDetail(ReportSummary):
    """报告详情：元信息 + 冻结正文。"""

    content: InsightReportContent


# —— 手工资产契约 ————————————————————————————————————————————

AssetKind = Literal["harvest", "problem"]
AssetStatus = Literal["active", "archived", "open", "resolved"]


class AssetSourceInput(InsightsModel):
    """资产来源：指向归档；可选引用具体反馈版本（记录时间戳）。"""

    paper_id: str
    rating_at: AwareDatetime | None = None


class AssetCreateInput(InsightsModel):
    kind: AssetKind
    title: str = Field(min_length=1, max_length=256)
    body: str = Field(min_length=1, max_length=20000)
    applicability: str | None = Field(default=None, max_length=2000)
    sources: list[AssetSourceInput] = Field(min_length=1, max_length=20)
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class AssetRevisionInput(InsightsModel):
    """全量内容修订；expected_revision 做乐观并发校验。"""

    expected_revision: int = Field(ge=1, strict=True)
    title: str = Field(min_length=1, max_length=256)
    body: str = Field(min_length=1, max_length=20000)
    applicability: str | None = Field(default=None, max_length=2000)
    status: AssetStatus
    sources: list[AssetSourceInput] = Field(min_length=1, max_length=20)


class AssetSourceRef(InsightsModel):
    paper_id: str
    archive_hash: str
    rating_at: AwareDatetime | None = None


class AssetRevisionView(InsightsModel):
    revision: int
    recorded_at: AwareDatetime
    title: str
    body: str
    applicability: str | None = None
    status: str
    sources: list[AssetSourceRef] = Field(default_factory=list)


class AssetRevisionSummary(InsightsModel):
    revision: int
    recorded_at: AwareDatetime
    title: str
    status: str


class AssetView(InsightsModel):
    asset_id: str
    kind: str
    created_at: AwareDatetime
    revision: AssetRevisionView
    history: list[AssetRevisionSummary] = Field(default_factory=list)
