"""复核结论引擎。

评估层次：准则（按河段 × 季节）→ 河段结论 → 项目结论。

不可违背的领域规则在本模块集中落地：

1. 不同季节、不同河段的观测分组评估，绝不跨组合并求平均；
2. 同季同段证据同时出现达标与不达标读数 → ``contradictory``，保留不确定性；
3. 准则缺少观测或观测超时效 → ``missing`` / ``stale``，同样保留不确定性；
4. 开放的社区反馈簇、未被完成处置覆盖的污染源、开放高风险事项是硬性阻碍；
5. 只要存在任何不确定性或硬性阻碍，河段/项目都不得给出 ``recovered``。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from .clustering import cluster_reports
from .coverage import unaddressed_sources
from .models import (
    CONTRADICTORY,
    Criterion,
    CriterionResult,
    FAIL,
    HIGH_RISK_KINDS,
    MISSING,
    NOT_RECOVERED,
    OPEN_STATUSES,
    PASS,
    ProjectConclusion,
    Reason,
    ReachConclusion,
    ReviewDiff,
    RECOVERED,
    STALE,
    ScheduleFlag,
    SeasonResult,
    UNCERTAIN,
)
from .schedules import expected_schedule
from .seasons import key_to_text, season_key

#: open_high_risk_actions 一类“非测量型”准则的特殊测量名
OPEN_HIGH_RISK_MEASURE = "open_high_risk_actions"


@dataclass
class ReviewBundle:
    as_of: date
    reach_conclusions: list[ReachConclusion]
    project_conclusions: list[ProjectConclusion]
    clusters: list
    schedule_flags: list[ScheduleFlag]


# ---------------------------------------------------------------------------
# 准则级评估
# ---------------------------------------------------------------------------


def _criterion_active(criterion: Criterion, as_of: date) -> bool:
    if criterion.valid_from and as_of < criterion.valid_from:
        return False
    if criterion.valid_to and as_of > criterion.valid_to:
        return False
    return True


def _evaluate_measured_criterion(
    criterion: Criterion,
    evidence,
    reach_id: str,
    as_of: date,
    max_age_days: int,
) -> CriterionResult:
    from .models import OPERATORS, OPERATOR_TEXT

    scoped = [
        e
        for e in evidence
        if e.reach_id == reach_id and e.measure == criterion.measure
    ]
    evidence_ids = sorted(e.evidence_id for e in scoped)
    comparator = OPERATORS[criterion.operator]
    base = dict(
        criterion_id=criterion.criterion_id,
        measure=criterion.measure,
        operator=criterion.operator,
        threshold=criterion.threshold,
        evidence_ids=evidence_ids,
    )

    if not scoped:
        return CriterionResult(
            status=MISSING,
            season_results=[],
            reason_codes=[Reason.NO_BASELINE_DATA],
            detail=(
                f"河段 {reach_id} 缺少测量 {criterion.measure!r} 的任何观测，"
                "无法形成基线"
            ),
            **base,
        )

    # 按 (年份, 季节) 分组——跨季数据永远不进入同一基线
    grouped: dict[tuple[int, int], list] = {}
    for item in scoped:
        grouped.setdefault(season_key(item.observed_on), []).append(item)

    season_results: list[SeasonResult] = []
    for key in sorted(grouped):
        members = sorted(grouped[key], key=lambda e: e.observed_on)
        passes = [e for e in members if comparator(e.value, criterion.threshold)]
        observations = [
            (e.evidence_id, e.value, e.observed_on.isoformat()) for e in members
        ]
        if not passes:
            status = FAIL
        elif len(passes) == len(members):
            status = PASS
        else:
            status = CONTRADICTORY
        season_results.append(
            SeasonResult(season=key_to_text(key), status=status,
                         observations=observations)
        )

    latest = season_results[-1]
    latest_obs_date = max(e.observed_on for e in grouped[max(grouped)])
    reason_codes: list[str] = []
    if len(grouped) > 1:
        reason_codes.append(Reason.SEASON_SPLIT)

    is_stale = latest_obs_date < as_of - timedelta(days=max_age_days)

    if is_stale:
        status = STALE
        reason_codes.append(Reason.STALE_EVIDENCE)
    elif latest.status == CONTRADICTORY:
        status = CONTRADICTORY
        reason_codes.append(Reason.CONTRADICTORY_EVIDENCE)
    elif latest.status == FAIL:
        status = FAIL
        reason_codes.append(Reason.THRESHOLD_FAIL)
    else:
        status = PASS

    symbol = OPERATOR_TEXT[criterion.operator]
    older = ""
    if len(season_results) > 1:
        earlier = ", ".join(
            f"{s.season}={s.status}" for s in season_results[:-1]
        )
        older = f"；历史季节单独留痕：{earlier}（未与最新季节合并）"
    readings = "; ".join(
        f"{eid}={val}({day})" for eid, val, day in latest.observations
    )
    stale_note = (
        f"；最新观测 {latest_obs_date.isoformat()} 已超出 {max_age_days} 天时效窗口"
        if is_stale
        else ""
    )
    detail = (
        f"最新基线（{latest.season}）读数 {readings}，要求 "
        f"{criterion.measure} {symbol} {criterion.threshold:g} → {status}"
        f"{stale_note}{older}"
    )
    return CriterionResult(
        status=status,
        season_results=season_results,
        reason_codes=reason_codes,
        detail=detail,
        **base,
    )


def _evaluate_high_risk_criterion(
    criterion: Criterion,
    actions,
    reach_id: str,
) -> CriterionResult:
    from .models import OPERATORS

    open_items = [
        a
        for a in actions
        if a.reach_id == reach_id
        and a.kind in HIGH_RISK_KINDS
        and a.status in OPEN_STATUSES
    ]
    count = len(open_items)
    ok = OPERATORS[criterion.operator](count, criterion.threshold)
    ids = sorted(a.record_id for a in open_items)
    return CriterionResult(
        criterion_id=criterion.criterion_id,
        measure=criterion.measure,
        operator=criterion.operator,
        threshold=criterion.threshold,
        status=PASS if ok else FAIL,
        season_results=[],
        evidence_ids=ids,
        reason_codes=[Reason.OPEN_HIGH_RISK_ACTION] if not ok else [],
        detail=(
            f"开放高风险事项 {count} 项（{', '.join(ids) if ids else '无'}），"
            f"要求 {criterion.measure} {criterion.operator} "
            f"{criterion.threshold:g} → {'pass' if ok else 'fail'}"
        ),
    )


# ---------------------------------------------------------------------------
# 河段 / 项目级评估
# ---------------------------------------------------------------------------


def evaluate_reach(
    reach,
    criteria: list[Criterion],
    evidence,
    sources,
    disposals,
    actions,
    clusters,
    as_of: date,
    max_age_days: int,
    *,
    revision_no: int = 1,
) -> ReachConclusion:
    reason_codes: list[str] = []
    criterion_results: list[CriterionResult] = []
    used_evidence: set[str] = set()

    for criterion in criteria:
        if criterion.project_id != reach.project_id:
            continue
        if not _criterion_active(criterion, as_of):
            reason_codes.append(Reason.OUTSIDE_VALIDITY)
            continue
        if criterion.measure == OPEN_HIGH_RISK_MEASURE:
            result = _evaluate_high_risk_criterion(criterion, actions,
                                                   reach.reach_id)
        else:
            result = _evaluate_measured_criterion(
                criterion, evidence, reach.reach_id, as_of, max_age_days
            )
        criterion_results.append(result)
        reason_codes.extend(result.reason_codes)
        used_evidence.update(result.evidence_ids)

    # 硬性阻碍 1：开放的社区反馈簇（整簇只算一项独立证据）
    open_clusters = [
        c for c in clusters
        if c.reach_id == reach.reach_id and c.is_open
    ]
    if open_clusters:
        reason_codes.append(Reason.OPEN_COMMUNITY_CLUSTER)

    # 硬性阻碍 2：没有完成处置覆盖的污染源
    pending = unaddressed_sources(
        [s for s in sources if s.reach_id == reach.reach_id],
        disposals, as_of,
    )
    if pending:
        reason_codes.append(Reason.SOURCE_NOT_ADDRESSED)

    # 硬性阻碍 3：开放高风险事项（即使没有对应准则也阻断恢复）
    open_high_risk = [
        a.record_id for a in actions
        if a.reach_id == reach.reach_id
        and a.kind in HIGH_RISK_KINDS
        and a.status in OPEN_STATUSES
    ]
    if open_high_risk and Reason.OPEN_HIGH_RISK_ACTION not in reason_codes:
        reason_codes.append(Reason.OPEN_HIGH_RISK_ACTION)
    used_evidence.update(open_high_risk)

    blocking = bool(open_clusters or pending or open_high_risk)
    criterion_statuses = {r.status for r in criterion_results}
    uncertain_set = {CONTRADICTORY, MISSING, STALE}

    if criterion_statuses & {FAIL}:
        status = NOT_RECOVERED
    elif criterion_statuses & uncertain_set:
        status = UNCERTAIN
    elif blocking:
        # 准则全部通过但仍有明确未决问题：已知未恢复，而非不确定
        status = NOT_RECOVERED
    elif not criterion_results:
        status = UNCERTAIN
        if Reason.NO_BASELINE_DATA not in reason_codes:
            reason_codes.append(Reason.NO_BASELINE_DATA)
    else:
        status = RECOVERED

    # 去重并保持原因码稳定顺序
    reason_codes = list(dict.fromkeys(reason_codes))
    return ReachConclusion(
        reach_id=reach.reach_id,
        project_id=reach.project_id,
        as_of=as_of.isoformat(),
        status=status,
        reason_codes=reason_codes,
        criterion_results=criterion_results,
        open_cluster_ids=[c.cluster_id for c in open_clusters],
        unaddressed_sources=sorted(pending),
        evidence_ids=sorted(used_evidence),
        revision_no=revision_no,
    )


def roll_up_project(
    project_id: str,
    reach_conclusions: list[ReachConclusion],
    as_of: date,
    *,
    revision_no: int = 1,
) -> ProjectConclusion:
    mine = [c for c in reach_conclusions if c.project_id == project_id]
    statuses = {c.status for c in mine}
    if statuses == {RECOVERED}:
        status = RECOVERED
    elif NOT_RECOVERED in statuses:
        status = NOT_RECOVERED
    else:
        status = UNCERTAIN
    reason_codes = list(dict.fromkeys(
        code for c in mine for code in c.reason_codes
    ))
    return ProjectConclusion(
        project_id=project_id,
        as_of=as_of.isoformat(),
        status=status,
        reason_codes=reason_codes,
        reach_statuses={c.reach_id: c.status for c in mine},
        revision_no=revision_no,
    )


# ---------------------------------------------------------------------------
# 整库复核与增量差异
# ---------------------------------------------------------------------------


def run_review(workspace, as_of: date, *, max_age_days: int = 120) -> ReviewBundle:
    """对工作区当前全部数据执行一次完整复核。"""
    clusters = cluster_reports(workspace.reports)
    conclusions = [
        evaluate_reach(
            reach,
            workspace.criteria,
            workspace.evidence,
            workspace.sources,
            workspace.disposals,
            workspace.actions,
            clusters,
            as_of,
            max_age_days,
            revision_no=max(
                workspace.current_revision("reach", reach.reach_id), 1),
        )
        for reach in workspace.reaches
    ]
    projects = sorted({r.project_id for r in workspace.reaches})
    project_conclusions = [
        roll_up_project(
            pid, conclusions, as_of,
            revision_no=max(
                workspace.current_revision("project", pid), 1),
        )
        for pid in projects
    ]
    flags = [
        flag
        for reach in workspace.reaches
        for flag in [
            _schedule_flag(workspace, reach.reach_id,
                           next(c for c in conclusions
                                if c.reach_id == reach.reach_id),
                           as_of)
        ]
        if flag is not None
    ]
    return ReviewBundle(
        as_of=as_of,
        reach_conclusions=conclusions,
        project_conclusions=project_conclusions,
        clusters=clusters,
        schedule_flags=flags,
    )


def _schedule_flag(workspace, reach_id: str,
                   conclusion: ReachConclusion, as_of: date) -> Optional[ScheduleFlag]:
    policy = next((p for p in workspace.policies
                   if p.reach_id == reach_id), None)
    if policy is None:
        return None
    current = workspace.schedules.get(reach_id)
    current_mode = current.mode if current else "standard"
    expected = expected_schedule(conclusion.status, policy)
    if expected.mode == current_mode:
        return None
    reason = (
        "河段已达标，可下调巡查频率"
        if expected.mode == "reduced"
        else "河段未达标或存在不确定性，必须恢复标准巡查频率"
    )
    return ScheduleFlag(
        reach_id=reach_id,
        current_mode=current_mode,
        expected_mode=expected.mode,
        reason=reason,
    )


def diff_bundles(
    previous: Optional[ReviewBundle],
    current: ReviewBundle,
    *,
    workspace,
    new_evidence_ids: list[str],
    new_report_ids: list[str],
    new_item_ids: list[str],
) -> ReviewDiff:
    """对比两次复核，列出结论、事项与巡查安排的待更新项。

    新证据/反馈/事项涉及的河段上，所有开放事项都会被列为“需要回看”。
    """
    prev_reach = (
        {c.reach_id: c for c in previous.reach_conclusions}
        if previous else {}
    )
    prev_project = (
        {c.project_id: c for c in previous.project_conclusions}
        if previous else {}
    )

    reach_changes = []
    for conclusion in current.reach_conclusions:
        old = prev_reach.get(conclusion.reach_id)
        if old is None or old.status != conclusion.status or \
                old.reason_codes != conclusion.reason_codes:
            old_status = old.status if old else "new"
            reach_changes.append(
                (conclusion.reach_id, old_status, conclusion.status,
                 list(conclusion.reason_codes))
            )

    project_changes = []
    for conclusion in current.project_conclusions:
        old = prev_project.get(conclusion.project_id)
        if old is None or old.status != conclusion.status or \
                old.reason_codes != conclusion.reason_codes:
            old_status = old.status if old else "new"
            project_changes.append(
                (conclusion.project_id, old_status, conclusion.status,
                 list(conclusion.reason_codes))
            )

    new_report_set = set(new_report_ids)
    new_clusters = [
        c for c in current.clusters
        if any(member in new_report_set for member in c.member_ids)
    ]

    evidence_by_id = {e.evidence_id: e for e in workspace.evidence}
    reports_by_id = {r.record_id: r for r in workspace.reports}
    items_by_id = {a.record_id: a for a in workspace.actions}

    touched_reaches: set[str] = {
        evidence_by_id[eid].reach_id
        for eid in new_evidence_ids if eid in evidence_by_id
    }
    touched_reaches.update(
        reports_by_id[rid].reach_id
        for rid in new_report_ids if rid in reports_by_id
    )
    touched_reaches.update(
        items_by_id[iid].reach_id
        for iid in new_item_ids if iid in items_by_id
    )
    for reach_id, *_ in reach_changes:
        touched_reaches.add(reach_id)

    affected: dict[str, list[str]] = {}
    for reach_id in sorted(touched_reaches):
        ids = sorted(
            a.record_id for a in workspace.actions
            if a.reach_id == reach_id and a.status in OPEN_STATUSES
        )
        if ids:
            affected[reach_id] = ids

    return ReviewDiff(
        as_of=current.as_of.isoformat(),
        reach_changes=reach_changes,
        project_changes=project_changes,
        new_clusters=new_clusters,
        affected_actions=affected,
        schedule_flags=current.schedule_flags,
    )
