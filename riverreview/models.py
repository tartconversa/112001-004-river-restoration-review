"""领域数据模型与状态常量。

所有日期均以 ISO 字符串（YYYY-MM-DD）或 :class:`datetime.date` 表示；
持久化时统一由 :mod:`riverreview.store` 负责转换。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

# ---------------------------------------------------------------------------
# 状态常量
# ---------------------------------------------------------------------------

#: 河段/项目复核结论
RECOVERED = "recovered"          # 达标，可申请降低巡查频率
NOT_RECOVERED = "not_recovered"  # 有明确证据表明未达标
UNCERTAIN = "uncertain"          # 证据矛盾或覆盖不全，禁止标记为已恢复

CONCLUSION_STATUSES = (RECOVERED, NOT_RECOVERED, UNCERTAIN)

#: 单条准则评估结果
PASS = "pass"
FAIL = "fail"
CONTRADICTORY = "contradictory"  # 同季同段证据相互矛盾
MISSING = "missing"              # 没有任何可用观测
STALE = "stale"                  # 最新观测超过时效窗口

#: 事项/反馈状态
OPEN = "open"
INVESTIGATING = "investigating"
COMPLETE = "complete"
DISMISSED = "dismissed"

OPEN_STATUSES = (OPEN, INVESTIGATING)
DONE_STATUSES = (COMPLETE, DISMISSED)
ACTION_STATUSES = OPEN_STATUSES + DONE_STATUSES

#: 事项类型
COMMUNITY = "community"
ACTION = "action"
INVESTIGATION = "investigation"
DISPOSAL_TASK = "disposal"
ITEM_KINDS = (COMMUNITY, ACTION, INVESTIGATION, DISPOSAL_TASK)

#: 高风险事项（开放状态计入准则 K03 一类的 open_high_risk_actions）
HIGH_RISK_KINDS = (ACTION, INVESTIGATION, DISPOSAL_TASK)

#: 证据类型
WATER = "water"
ECOLOGY = "ecology"
EVIDENCE_TYPES = (WATER, ECOLOGY)

#: 处置记录状态
DISPOSAL_COMPLETE = "complete"
DISPOSAL_IN_PROGRESS = "in_progress"
DISPOSAL_PLANNED = "planned"

# ---------------------------------------------------------------------------
# 稳定的原因码：写入审计日志，供结论修订追溯
# ---------------------------------------------------------------------------


class Reason:
    THRESHOLD_FAIL = "THRESHOLD_FAIL"                 # 同季观测明确低于/高于阈值
    CONTRADICTORY_EVIDENCE = "CONTRADICTORY_EVIDENCE"  # 同季同段证据矛盾
    NO_BASELINE_DATA = "NO_BASELINE_DATA"             # 准则所需测量完全无观测
    STALE_EVIDENCE = "STALE_EVIDENCE"                 # 最新观测超出时效
    OPEN_COMMUNITY_CLUSTER = "OPEN_COMMUNITY_CLUSTER"  # 仍有未关闭的社区反馈簇
    OPEN_HIGH_RISK_ACTION = "OPEN_HIGH_RISK_ACTION"    # 存在开放高风险事项
    SOURCE_NOT_ADDRESSED = "SOURCE_NOT_ADDRESSED"      # 污染源无完成处置覆盖
    DUPLICATE_LINKED = "DUPLICATE_LINKED"              # 重复反馈已关联，不重复计证
    SEASON_SPLIT = "SEASON_SPLIT"                      # 信息性：已按季节分组评估
    OUTSIDE_VALIDITY = "OUTSIDE_VALIDITY"              # 准则不在生效区间内


OPERATORS = {
    "ge": lambda a, b: a >= b,
    "le": lambda a, b: a <= b,
    "eq": lambda a, b: a == b,
    "gt": lambda a, b: a > b,
    "lt": lambda a, b: a < b,
}

OPERATOR_TEXT = {"ge": "≥", "le": "≤", "eq": "=", "gt": ">", "lt": "<"}


# ---------------------------------------------------------------------------
# 输入侧实体
# ---------------------------------------------------------------------------


@dataclass
class Reach:
    reach_id: str
    river: str
    upstream_km: float
    downstream_km: float
    project_id: str


@dataclass
class Criterion:
    criterion_id: str
    project_id: str
    measure: str
    operator: str
    threshold: float
    valid_from: Optional[date]
    valid_to: Optional[date]


@dataclass
class Evidence:
    evidence_id: str
    reach_id: str
    observed_on: date
    evidence_type: str
    measure: str
    value: float
    unit: str
    source_ref: str
    season: str


@dataclass
class PollutionSource:
    source_id: str
    reach_id: str
    location_km: float
    pollutant: str
    first_reported: Optional[date]


@dataclass
class Disposal:
    """污染源处置记录，只能证明其显式覆盖的污染源或区段。"""

    disposal_id: str
    source_id: Optional[str]
    reach_id: str
    covered_from_km: float
    covered_to_km: float
    status: str
    completed_at: Optional[date]
    evidence_ref: str


@dataclass
class CommunityReport:
    record_id: str
    reach_id: str
    reported_at: date
    description: str
    status: str
    owner: str
    due_date: Optional[date]
    linked_group: Optional[str] = None


@dataclass
class ActionItem:
    record_id: str
    reach_id: str
    created_at: date
    kind: str
    description: str
    status: str
    owner: str
    due_date: Optional[date]
    evidence_refs: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 派生实体
# ---------------------------------------------------------------------------


@dataclass
class Cluster:
    """重复社区反馈聚类：整簇只算作一项独立证据。"""

    cluster_id: str
    reach_id: str
    description: str
    primary_report_id: str
    member_ids: list[str]
    is_open: bool


@dataclass
class InspectionPolicy:
    reach_id: str
    standard_interval_days: int
    reduced_interval_days: int


@dataclass
class Schedule:
    reach_id: str
    current_interval_days: int
    mode: str  # standard | reduced
    last_changed: Optional[str] = None


@dataclass
class SeasonResult:
    season: str
    status: str  # pass | fail | contradictory
    observations: list[tuple[str, float, str]]  # (evidence_id, value, ISO 日期)


@dataclass
class CriterionResult:
    criterion_id: str
    measure: str
    operator: str
    threshold: float
    status: str  # pass | fail | contradictory | missing | stale
    season_results: list[SeasonResult]
    evidence_ids: list[str]
    reason_codes: list[str]
    detail: str


@dataclass
class ReachConclusion:
    reach_id: str
    project_id: str
    as_of: str
    status: str
    reason_codes: list[str]
    criterion_results: list[CriterionResult]
    open_cluster_ids: list[str]
    unaddressed_sources: list[str]
    evidence_ids: list[str]
    revision_no: int


@dataclass
class ProjectConclusion:
    project_id: str
    as_of: str
    status: str
    reason_codes: list[str]
    reach_statuses: dict[str, str]
    revision_no: int


@dataclass
class ScheduleFlag:
    reach_id: str
    current_mode: str
    expected_mode: str
    reason: str


@dataclass
class ReviewDiff:
    as_of: str
    reach_changes: list[tuple[str, str, str, list[str]]]
    project_changes: list[tuple[str, str, str, list[str]]]
    new_clusters: list[Cluster]
    affected_actions: dict[str, list[str]]
    schedule_flags: list[ScheduleFlag]
