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


# —— 结构化阅读理由：版本化理由码词表；码是存储与报告形态，文案仅用于展示 ———

STRUCTURED_FEEDBACK_VOCABULARY = "reading-feedback.vocab/1"

ValueSourceCode = Literal[
    "method_reusable",
    "aids_understanding",
    "changed_understanding",
    "provides_arguments",
    "research_ideas",
    "usable_now",
]
LimitedReasonCode = Literal[
    "task_mismatch",
    "already_known",
    "evidence_doubtful",
    "conditions_unmet",
    "not_yet_clear",
]

VALUE_SOURCE_CODES: tuple[str, ...] = (
    "method_reusable",
    "aids_understanding",
    "changed_understanding",
    "provides_arguments",
    "research_ideas",
    "usable_now",
)
LIMITED_REASON_CODES: tuple[str, ...] = (
    "task_mismatch",
    "already_known",
    "evidence_doubtful",
    "conditions_unmet",
    "not_yet_clear",
)
VALUE_SOURCE_LABELS: dict[str, str] = {
    "method_reusable": "方法可复用",
    "aids_understanding": "帮助理解",
    "changed_understanding": "改变认识",
    "provides_arguments": "提供论据",
    "research_ideas": "带来研究想法",
    "usable_now": "可用于当前任务",
}
LIMITED_REASON_LABELS: dict[str, str] = {
    "task_mismatch": "当前任务不适合",
    "already_known": "内容已知",
    "evidence_doubtful": "证据存疑",
    "conditions_unmet": "适用条件不满足",
    "not_yet_clear": "暂时看不懂",
}


class StructuredFeedbackInput(InsightsModel):
    """结构化阅读理由的全量修订；expected_revision 防并发，幂等键走写入回执。

    全部维度留空是一次显式清除；文本首尾空白已剥离，空理由不补成中立。
    """

    expected_revision: int = Field(ge=0, strict=True)
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    rating_at: AwareDatetime | None = None
    value_sources: tuple[ValueSourceCode, ...] = ()
    limited_reasons: tuple[LimitedReasonCode, ...] = ()
    context_note: str | None = Field(default=None, max_length=2000)
    applicability: str | None = Field(default=None, max_length=2000)
    takeaway: str | None = Field(default=None, max_length=2000)
    question: str | None = Field(default=None, max_length=2000)
    usage_result: str | None = Field(default=None, max_length=2000)


class StructuredFeedbackVersion(InsightsModel):
    """截止前有效的一个结构化理由版本；不含自由文本原文（隐私边界）。"""

    revision: int
    recorded_at: AwareDatetime
    rating_at: AwareDatetime | None = None
    value_sources: list[str] = Field(default_factory=list)
    limited_reasons: list[str] = Field(default_factory=list)
    has_context_note: bool = False
    has_applicability: bool = False
    has_takeaway: bool = False
    has_question: bool = False
    has_usage_result: bool = False
    vocabulary_version: str = STRUCTURED_FEEDBACK_VOCABULARY


class StructuredFeedbackChange(InsightsModel):
    """相邻版本的一次理由修订；只含码与变化标记，可直接冻结进报告。"""

    kind: Literal["structured"] = "structured"
    paper_id: str
    recorded_at: AwareDatetime
    added_value_sources: list[str] = Field(default_factory=list)
    removed_value_sources: list[str] = Field(default_factory=list)
    added_limited_reasons: list[str] = Field(default_factory=list)
    removed_limited_reasons: list[str] = Field(default_factory=list)
    context_changed: bool = False
    free_text_changed: bool = False
    link_changed: bool = False
    cleared: bool = False


class StructuredFeedbackRevisionDetail(InsightsModel):
    """某个理由版本的完整内容；自由文本属私人材料，仅属主读取路径返回。"""

    revision: int
    recorded_at: AwareDatetime
    rating_at: AwareDatetime | None = None
    rating_exists: bool = False
    rating_score: int | None = Field(default=None, ge=1, le=5, strict=True)
    rating_has_note: bool = False
    value_sources: list[str] = Field(default_factory=list)
    limited_reasons: list[str] = Field(default_factory=list)
    context_note: str | None = None
    applicability: str | None = None
    takeaway: str | None = None
    question: str | None = None
    usage_result: str | None = None
    vocabulary_version: str = STRUCTURED_FEEDBACK_VOCABULARY


class StructuredFeedbackView(InsightsModel):
    """一篇归档的结构化理由现状；current_revision=0 表示从未填写。"""

    paper_id: str
    current_revision: int = Field(ge=0)
    latest: StructuredFeedbackRevisionDetail | None = None
    history: list[StructuredFeedbackRevisionDetail] = Field(default_factory=list)


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
    structured_feedback: list[StructuredFeedbackVersion] = Field(default_factory=list)
    structured_changes: list[StructuredFeedbackChange] = Field(default_factory=list)
    archive_hash: str | None = None
    reading_revision: int = 0


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


class StructuredFeedbackCoverageStats(InsightsModel):
    """结构化理由覆盖；只统计显式填写的维度，空理由不补成中立。"""

    total: int = Field(default=0, ge=0)
    covered: int = Field(default=0, ge=0)
    value_sources: int = Field(default=0, ge=0)
    limited_reasons: int = Field(default=0, ge=0)
    context: int = Field(default=0, ge=0)
    free_text: int = Field(default=0, ge=0)


class StructuredFeedbackChangeStats(InsightsModel):
    """本期结构化理由修订次数与涉及篇数；初始记录不算修订。"""

    events: int = Field(default=0, ge=0)
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
    # 结构化理由：覆盖只数显式填写维度；默认值保证旧报告 content_json 可解析。
    structured_coverage: StructuredFeedbackCoverageStats = Field(
        default_factory=lambda: StructuredFeedbackCoverageStats(
            total=0, covered=0, value_sources=0, limited_reasons=0, context=0, free_text=0
        )
    )
    structured_change_stats: StructuredFeedbackChangeStats = Field(
        default_factory=StructuredFeedbackChangeStats
    )
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
    assets: list["ReportAssetRef"] = Field(default_factory=list)
    readings: list[PaperProjection] = Field(default_factory=list)
    backfills: list[PaperProjection] = Field(default_factory=list)
    changes: list[FeedbackChange] = Field(default_factory=list)
    structured_changes: list[StructuredFeedbackChange] = Field(default_factory=list)
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
    # 结构化理由只含版本化码与有无标记，无私人原文，投影与报告共用同一契约。
    structured_feedback: list[StructuredFeedbackVersion] = Field(default_factory=list)


class ReportAssetRef(InsightsModel):
    """报告内的资产引用；body 仅在用户显式勾选时保存正文副本。"""

    asset_id: str
    title: str
    kind: str
    status: str
    body: str | None = None
    revision: int | None = None
    source_paper_ids: list[str] = Field(default_factory=list)
    cross_topic: bool = False


class InsightReportContent(InsightsModel):
    """报告快照的冻结正文；读取时的唯一内容来源。"""

    contract: Literal["reading-insights.report/1"]
    metrics: InsightMetrics
    readings: list[ReportPaper] = Field(default_factory=list)
    backfills: list[ReportPaper] = Field(default_factory=list)
    changes: list[ReportChange] = Field(default_factory=list)
    structured_changes: list[StructuredFeedbackChange] = Field(default_factory=list)
    unassigned: list[ReportPaper] = Field(default_factory=list)
    assets: list["ReportAssetRef"] = Field(default_factory=list)


FACTS_ALGORITHM = "reading-insights.facts/2"


class ScopeTopic(InsightsModel):
    code: str = Field(min_length=1)
    # None 仅用于兼容显式提交的旧式 code 筛选，表示跨分类版本匹配该代码。
    taxonomy_version: str | None = Field(default=None, min_length=1)


class AnalysisScope(InsightsModel):
    """有效范围；截止用于选版本，不作为系列身份或墙钟去重条件。"""

    period: ResolvedPeriod
    mode: ReviewMode
    knowledge_cutoff: AwareDatetime
    topic: ScopeTopic | None = None
    topic_known: bool = True
    algorithm_version: str | None = FACTS_ALGORITHM
    report_contract: str = REPORT_CONTRACT


class EffectivePaperInput(InsightsModel):
    paper_id: str
    archive_hash: str | None
    input_hash: str


class EffectiveAssetInput(InsightsModel):
    asset_id: str
    revision: int
    recorded_at: AwareDatetime
    sources: list["AssetSourceRef"]
    input_hash: str


class EffectiveInputManifest(InsightsModel):
    """只记录范围内有效源版本；不复制私人原文或全库库存。"""

    papers: list[EffectivePaperInput] = Field(default_factory=list)
    asset_versions: list[EffectiveAssetInput] = Field(default_factory=list)


class ReportContext(InsightsModel):
    status: Literal["complete", "legacy_unknown"]
    scope: AnalysisScope
    normalized_request: InsightQueryRequest | None = None
    include_asset_bodies: bool | None = None
    manifest: EffectiveInputManifest | None = None


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
    context: ReportContext | None = None


class ReportDetail(ReportSummary):
    """报告详情：元信息 + 冻结正文。"""

    content: InsightReportContent


# —— 手工资产契约 ————————————————————————————————————————————

AssetKind = Literal["harvest", "problem", "method", "evidence"]
AssetStatus = Literal["active", "archived", "open", "resolved"]

# 方法卡/证据组的结构化版本内容；与自由正文分列存储，不硬塞进 body。
TYPED_ASSET_KINDS: frozenset[str] = frozenset({"method", "evidence"})


class MethodCardContent(InsightsModel):
    """方法卡的类型化内容；资源/数据要求未知时 resources 留空。"""

    problem: str = Field(min_length=1, max_length=2000)
    conditions: str | None = Field(default=None, max_length=2000)
    approach: str = Field(min_length=1, max_length=8000)
    limitations: str | None = Field(default=None, max_length=2000)
    resources: str | None = Field(default=None, max_length=2000)


class EvidenceGroupContent(InsightsModel):
    """证据组的类型化内容；尚未核实部分显式标注。"""

    claim: str = Field(min_length=1, max_length=2000)
    supports: list[str] = Field(default_factory=list, max_length=20)
    differing: list[str] = Field(default_factory=list, max_length=20)
    context: str | None = Field(default=None, max_length=2000)
    unverified: str | None = Field(default=None, max_length=2000)


AssetTypedContent = MethodCardContent | EvidenceGroupContent


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
    # 类型化内容：method→MethodCardContent、evidence→EvidenceGroupContent、
    # harvest/problem 必须为 None（一致性由 coherent 校验器保证）。
    typed: AssetTypedContent | None = None
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")

    @model_validator(mode="after")
    def typed_kind_coherent(self) -> "AssetCreateInput":
        if self.kind in TYPED_ASSET_KINDS:
            if self.typed is None:
                raise ValueError(f"{self.kind} 资产必须提供类型化内容")
            expected = "MethodCardContent" if self.kind == "method" else "EvidenceGroupContent"
            if type(self.typed).__name__ != expected:
                raise ValueError(f"{self.kind} 资产的类型化内容必须是 {expected}")
        elif self.typed is not None:
            raise ValueError("收获/问题资产不接受类型化内容")
        return self


class AssetRevisionInput(InsightsModel):
    """全量内容修订；幂等键绑定完整请求，expected_revision 做乐观并发校验。

    ``sources`` 为空（省略/None）表示保留当前版本的来源集合原样
    （含归档哈希与反馈版本）；显式给出列表即整体替换，至少保留一条。
    ``typed`` 同步替换当前版本的类型化内容；省略（None）表示保留原样。
    """

    expected_revision: int = Field(ge=1, strict=True)
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    title: str = Field(min_length=1, max_length=256)
    body: str = Field(min_length=1, max_length=20000)
    applicability: str | None = Field(default=None, max_length=2000)
    status: AssetStatus
    sources: list[AssetSourceInput] | None = Field(default=None, min_length=1, max_length=20)
    typed: AssetTypedContent | None = None

    @model_validator(mode="after")
    def typed_kind_coherent(self) -> "AssetRevisionInput":
        # kind 在资产行上：None=保留原样（method/evidence 修订时可省略 typed）；
        # 给出 typed 时其类型由服务层按资产 kind 校验。
        return self


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
    sources: list["AssetSourceRef"] = Field(default_factory=list)
    # 方法卡/证据组的类型化内容（宽松 dict 以兼容旧回执重放）；无则 None。
    typed: dict[str, object] | None = None


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


class AssetSourceStatus(InsightsModel):
    """来源引用在读取时的解析状态；只说明当时版本，不改历史。"""

    paper_id: str
    archive_hash: str
    rating_at: AwareDatetime | None = None
    paper_exists: bool = True
    rating_exists: bool = True
    rating_score: int | None = None
    rating_note: str | None = None


class AssetRevisionDetail(InsightsModel):
    """单个历史版本的完整正文与来源状态；缺失或失效的引用明确标注。"""

    asset_id: str
    kind: str
    revision: AssetRevisionView
    source_status: list[AssetSourceStatus] = Field(default_factory=list)


# —— 阅读分析（R2-02）：材料类别、用途授权与预览令牌的契约 ————————————
# 分析是应用数据，不注册为 LLM JSON schema；发送边界由 authorization 模块把关。

ANALYSIS_CONSENT_VERSION = "reading-analysis.consent/1"
ANALYSIS_PURPOSE = "reading_reflection"
PREVIEW_TTL_SECONDS = 15 * 60

MaterialKind = Literal[
    "archive_metadata",
    "model_summary",
    "user_rating",
    "structured_reasons",
    "user_notes",
    "asset_content",
    "user_excerpt",
]

MATERIAL_KIND_LABELS: dict[str, str] = {
    "archive_metadata": "冻结元数据／导入摘要",
    "model_summary": "既有模型概括与评分依据",
    "user_rating": "主观评分",
    "structured_reasons": "结构化理由",
    "user_notes": "备注与个人说明",
    "asset_content": "已确认资产内容",
    "user_excerpt": "用户补充摘录",
}

MATERIAL_KIND_LIMITS: dict[str, str] = {
    "archive_metadata": "只有摘要与元数据，不能宣称验证全文结论",
    "model_summary": "历史模型概括；无法对照原文时不作为已核验原文证据",
    "user_rating": "用户评价，不是论文客观质量",
    "structured_reasons": "用户在截止前记录的理由版本，不是论文客观质量",
    "user_notes": "私人文本，单独授权后才会进入分析",
    "asset_content": "用户确认的个人整理；确认不消除私人属性",
    "user_excerpt": "用户提供；未核对全文时不自动承诺引文真实",
}


class MaterialRef(InsightsModel):
    """清单中的一条材料引用；只存 ID、版本锚点与哈希，不存任何原文。"""

    kind: MaterialKind
    target_id: str = Field(min_length=1, max_length=128)
    paper_id: str | None = Field(default=None, min_length=1, max_length=64)
    version_key: str = Field(min_length=1, max_length=128)
    fields: list[str] = Field(min_length=1, max_length=8)
    ref_hash: str = Field(min_length=64, max_length=64)


class MaterialExclusion(InsightsModel):
    """选材时的显式排除；原因只用于展示，进入清单冻结。"""

    paper_id: str = Field(min_length=1, max_length=64)
    kind: MaterialKind | None = None
    reason: str = Field(min_length=1, max_length=500)


class ManifestCoverage(InsightsModel):
    """覆盖统计：归档库存与本次实际选中分开展示。"""

    archived_total: int = Field(ge=0)
    selected_papers: int = Field(ge=0)
    by_kind: dict[str, int] = Field(default_factory=dict)


class AnalysisManifestContent(InsightsModel):
    """一次分析的材料清单正文；构建后冻结，预览令牌与提交按哈希绑定。"""

    purpose: str
    built_at: AwareDatetime
    materials: list[MaterialRef] = Field(max_length=2000)
    exclusions: list[MaterialExclusion] = Field(default_factory=list, max_length=500)
    coverage: ManifestCoverage


class AnalysisGrantInput(InsightsModel):
    """授权请求：范围模式＋覆盖的材料类别＋显式知情确认。"""

    scope_mode: Literal["once", "workspace"]
    material_classes: list[MaterialKind] = Field(min_length=1, max_length=7)
    acknowledged: bool

    @model_validator(mode="after")
    def acknowledged_is_explicit(self) -> "AnalysisGrantInput":
        if self.acknowledged is not True:
            raise ValueError("必须显式确认数据去向说明")
        return self


class AnalysisGrantView(InsightsModel):
    """授权的属主视图；模型配置版本变化即失效，撤回保留历史。"""

    grant_id: str
    purpose: str
    scope_mode: Literal["once", "workspace"]
    endpoint_identity: str
    model_config_version: str
    model_name: str | None = None
    material_classes: list[str] = Field(default_factory=list)
    manifest_hash: str | None = None
    consent_version: str
    created_at: AwareDatetime
    revoked_at: AwareDatetime | None = None


class AnalysisAuthorizationStatus(InsightsModel):
    """当前工作区的授权现状：模型连接摘要与授权/撤回列表分开表达。"""

    llm_configured: bool
    llm_endpoint_identity: str | None = None
    llm_model: str | None = None
    llm_config_version: str | None = None
    grants: list[AnalysisGrantView] = Field(default_factory=list)


class ExcerptInput(InsightsModel):
    """用户补充摘录；私人材料，独立授权后才可作为清单材料发送。"""

    content: str = Field(min_length=1, max_length=8000)
    source_note: str | None = Field(default=None, max_length=500)


class ExcerptView(InsightsModel):
    """摘录的属主视图；含录入时间与内容哈希。"""

    excerpt_id: str
    paper_id: str
    content: str
    source_note: str | None = None
    content_hash: str
    recorded_at: AwareDatetime


class ManifestSelectionInput(InsightsModel):
    """建清单的选材输入：论文列表＋类别＋逐篇排除（类别级排除留给后续批次）。"""

    purpose: str = ANALYSIS_PURPOSE
    paper_ids: list[str] = Field(min_length=1, max_length=500)
    kinds: list[MaterialKind] = Field(min_length=1, max_length=7)
    exclusions: list[MaterialExclusion] = Field(default_factory=list, max_length=500)


class AnalysisPreviewView(InsightsModel):
    """预览令牌的属主视图：绑定关系与有效期限，不含清单正文。"""

    token: str
    manifest_id: str
    manifest_hash: str
    purpose: str
    model_config_version: str
    expires_at: AwareDatetime
    consumed: bool = False


class CandidateSourceRef(InsightsModel):
    """候选引用的材料来源（与清单一致的类别/目标/版本锚点）。"""

    kind: MaterialKind
    target_id: str
    version_key: str


class AnalysisCandidateView(InsightsModel):
    """分析候选的属主视图；确认/修订入口随下一批提供。"""

    candidate_id: str
    run_id: str
    paper_id: str
    kind: str
    nature: str
    statement: str
    conditions: str | None = None
    limitations: str | None = None
    quote: str | None = None
    quote_source_id: str | None = None
    source_refs: list[CandidateSourceRef] = Field(default_factory=list)
    stale: bool = False
    verification: str = "pending_user"
    created_at: AwareDatetime


class AnalysisRunSummary(InsightsModel):
    """一次分析运行的只读摘要（来自 Run.stats_json，无私人正文）。"""

    run_id: str
    status: str
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    papers_total: int = 0
    papers_completed: int = 0
    papers_failed: int = 0
    papers_stale: int = 0
    papers_insufficient: int = 0
    papers_unfinished: int = 0
    candidate_count: int = 0
    rejected_count: int = 0
    stopped_reason: str | None = None


class CandidateDecisionInput(InsightsModel):
    """候选的用户决定输入；confirm 携带资产内容，merge 携带目标资产。

    ``decision=confirm`` 时 kind/title/body/sources 必填（typed 按 kind 一致性
    校验）；``merge`` 时 target_asset_id/expected_revision 必填；``dismiss``
    只需标识。幂等键绑定完整决定（重复提交重放同一结果）。
    """

    candidate_id: str = Field(min_length=1, max_length=64)
    decision: Literal["confirm", "dismiss", "merge"]
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    note: str | None = Field(default=None, max_length=2000)
    # confirm 分支
    kind: AssetKind | None = None
    title: str | None = Field(default=None, min_length=1, max_length=256)
    body: str | None = Field(default=None, min_length=1, max_length=20000)
    applicability: str | None = Field(default=None, max_length=2000)
    typed: AssetTypedContent | None = None
    sources: list[AssetSourceInput] | None = Field(default=None, min_length=1, max_length=20)
    # merge 分支
    target_asset_id: str | None = Field(default=None, min_length=1, max_length=64)
    expected_revision: int | None = Field(default=None, ge=1, strict=True)
    status: AssetStatus = "active"

    @model_validator(mode="after")
    def branch_coherent(self) -> "CandidateDecisionInput":
        if self.decision == "confirm":
            missing = [
                name
                for name, value in (
                    ("kind", self.kind),
                    ("title", self.title),
                    ("body", self.body),
                    ("sources", self.sources),
                )
                if value is None
            ]
            if missing:
                raise ValueError(f"确认候选需要资产内容：缺少 {', '.join(missing)}")
            if self.target_asset_id is not None or self.expected_revision is not None:
                raise ValueError("确认候选不使用合并目标字段")
        elif self.decision == "merge":
            if self.target_asset_id is None or self.expected_revision is None:
                raise ValueError("合并候选需要目标资产与预期版本")
            if self.title is None or self.body is None:
                raise ValueError("合并候选需要追加的内容")
        else:
            extra = [
                name
                for name, value in (
                    ("kind", self.kind),
                    ("title", self.title),
                    ("body", self.body),
                    ("typed", self.typed),
                    ("sources", self.sources),
                    ("target_asset_id", self.target_asset_id),
                )
                if value is not None
            ]
            if extra:
                raise ValueError(f"忽略候选不接受内容字段：{', '.join(extra)}")
        return self


class CandidateDecisionView(InsightsModel):
    """候选决定的属主视图；确认/合并指向目标资产。"""

    decision_id: str
    candidate_id: str
    decision: Literal["confirmed", "dismissed", "merged"]
    asset_id: str | None = None
    asset_revision: int | None = None
    paper_id: str
    kind: str
    note: str | None = None
    decided_at: AwareDatetime
