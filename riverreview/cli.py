"""命令行入口（argparse，纯标准库）。

子命令一览：

  init            从 CSV 目录初始化工作区
  ingest          增量导入新一批 CSV，并输出结论/事项/巡查待更新清单
  review          对当前数据重新复核并记录结论修订
  reaches         列出河段与最新结论
  conclusion      查看单个河段/项目的可解释复核结论
  actions         列出事项与社区反馈簇
  assign          分派调查或处置事项（责任方、期限、证据）
  update-action   更新事项状态并追加证据
  schedule        查看/应用巡查频率调整
  history         回看审计事件与结论修订原因
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import Optional

from .clustering import cluster_reports, explain_cluster
from .coverage import disposal_coverage, source_is_addressed
from .events import EventLog
from .ingest import (
    IngestError,
    load_criteria,
    load_disposals,
    load_evidence,
    load_policies,
    load_reaches,
    load_reports_actions,
    load_sources,
)
from .models import (
    ACTION_STATUSES,
    ITEM_KINDS,
    NOT_RECOVERED,
    RECOVERED,
    UNCERTAIN,
    ActionItem,
)
from .review import ReviewBundle, diff_bundles, run_review
from .schedules import expected_schedule
from .seasons import season_of
from .store import Workspace

DEFAULT_STATE_DIR = Path(".riverreview")
DEFAULT_MAX_AGE_DAYS = 120

STATUS_TEXT = {
    RECOVERED: "已达标(可申请降频)",
    NOT_RECOVERED: "未恢复",
    UNCERTAIN: "不确定(禁止标记恢复)",
}
CRITERION_TEXT = {
    "pass": "通过", "fail": "不通过", "contradictory": "证据矛盾",
    "missing": "缺少观测", "stale": "观测超时效",
}
ACTOR = "watershed_manager"


# ---------------------------------------------------------------------------
# 公共辅助
# ---------------------------------------------------------------------------


def _parse_day(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"日期需为 YYYY-MM-DD: {raw!r}") from exc


def _load_workspace(state_dir: Path) -> Workspace:
    try:
        return Workspace.load(state_dir)
    except FileNotFoundError as exc:
        raise SystemExit(f"错误：{exc}") from exc


def _print_reach_conclusion(conclusion, workspace: Workspace) -> None:
    reach = next(r for r in workspace.reaches
                 if r.reach_id == conclusion.reach_id)
    print(f"\n河段 {conclusion.reach_id}（{reach.river} "
          f"{reach.upstream_km:g}-{reach.downstream_km:g}km，"
          f"项目 {conclusion.project_id}）")
    print(f"  结论: {STATUS_TEXT[conclusion.status]}  "
          f"[修订号 r{conclusion.revision_no}，基准日 {conclusion.as_of}]")
    print("  准则评估（各季节独立成基线，不跨季平均）:")
    for result in conclusion.criterion_results:
        print(f"    - [{CRITERION_TEXT.get(result.status, result.status)}] "
              f"{result.criterion_id} {result.detail}")
        for season in result.season_results:
            readings = ", ".join(f"{eid}={val:g}"
                                 for eid, val, _ in season.observations)
            print(f"        · {season.season}: "
                  f"{CRITERION_TEXT.get(season.status, season.status)} ({readings})")
    if conclusion.open_cluster_ids:
        print("  开放社区反馈簇（整簇按 1 项独立证据）:")
        clusters = {c.cluster_id: c for c in cluster_reports(workspace.reports)}
        for cid in conclusion.open_cluster_ids:
            print(f"    - {explain_cluster(clusters[cid])}")
    if conclusion.unaddressed_sources:
        print("  未被完成处置覆盖的污染源:")
        for sid in conclusion.unaddressed_sources:
            source = next(s for s in workspace.sources if s.source_id == sid)
            addressed, disposal_id = source_is_addressed(
                source, workspace.disposals, date.fromisoformat(conclusion.as_of))
            note = f"，在途/计划处置 {disposal_id}" if disposal_id else "，无任何处置记录"
            print(f"    - {sid} {source.pollutant} @ "
                  f"{source.reach_id} {source.location_km:g}km{note}")
    print("  采用证据: " + (", ".join(conclusion.evidence_ids) or "无"))
    print("  原因码: " + ", ".join(conclusion.reason_codes))
    if conclusion.status == UNCERTAIN:
        print("  ⚠ 存在不确定性：按规则禁止把该河段标记为已恢复。")


def _print_project_conclusion(conclusion, workspace: Workspace) -> None:
    print(f"\n项目 {conclusion.project_id} 汇总 "
          f"[修订号 p{conclusion.revision_no}，基准日 {conclusion.as_of}]")
    print(f"  结论: {STATUS_TEXT[conclusion.status]}")
    for reach_id, status in conclusion.reach_statuses.items():
        print(f"    - {reach_id}: {STATUS_TEXT[status]}")
    print("  原因码: " + ", ".join(conclusion.reason_codes or ["(无)"]))
    if conclusion.status != RECOVERED:
        print("  ⚠ 项目未整体达标，不能降低任一河段的巡查频率。")


def _record_review(workspace: Workspace, bundle: ReviewBundle,
                   event_type: str, extra: Optional[dict] = None) -> None:
    """保存复核快照；仅在结论内容变化时递增修订号并写修订事件。"""
    log = workspace.event_log()
    old_reach = dict(workspace.latest_reach_conclusions)
    old_project = dict(workspace.latest_project_conclusions)
    workspace.store_bundle(bundle)

    for conclusion in bundle.reach_conclusions:
        previous = old_reach.get(conclusion.reach_id)
        changed = (
            previous is None
            or previous.status != conclusion.status
            or previous.reason_codes != conclusion.reason_codes
        )
        revision = (
            workspace.bump_revision("reach", conclusion.reach_id)
            if changed else workspace.current_revision("reach", conclusion.reach_id)
        )
        conclusion.revision_no = max(revision, 1)
        if changed:
            log.append("conclusion_revised", {
                "scope": "reach",
                "key": conclusion.reach_id,
                "previous_status": previous.status if previous else None,
                "new_status": conclusion.status,
                "reason_codes": conclusion.reason_codes,
                "evidence_ids": conclusion.evidence_ids,
                "as_of": conclusion.as_of,
                "revision_no": conclusion.revision_no,
                **(extra or {}),
            })

    for conclusion in bundle.project_conclusions:
        previous = old_project.get(conclusion.project_id)
        changed = (
            previous is None
            or previous.status != conclusion.status
            or previous.reason_codes != conclusion.reason_codes
        )
        revision = (
            workspace.bump_revision("project", conclusion.project_id)
            if changed else workspace.current_revision("project", conclusion.project_id)
        )
        conclusion.revision_no = max(revision, 1)
        if changed:
            log.append("conclusion_revised", {
                "scope": "project",
                "key": conclusion.project_id,
                "previous_status": previous.status if previous else None,
                "new_status": conclusion.status,
                "reason_codes": conclusion.reason_codes,
                "revision_no": conclusion.revision_no,
                **(extra or {}),
            })
    workspace.save()


# ---------------------------------------------------------------------------
# 子命令实现
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir)
    state_dir = Path(args.state_dir)
    if (state_dir / "state.json").exists():
        raise SystemExit(f"错误：工作区 {state_dir} 已初始化，请使用新目录或先删除")
    workspace = Workspace(root=state_dir)
    try:
        workspace.reaches = load_reaches(data_dir / "reaches.csv")
        reach_ids = {r.reach_id for r in workspace.reaches}
        workspace.criteria = load_criteria(data_dir / "review_criteria.csv")
        workspace.evidence = load_evidence(
            data_dir / "evidence.csv", season_resolver=season_of,
            valid_reach_ids=reach_ids)
        workspace.sources = load_sources(
            data_dir / "pollution_sources.csv",
            valid_reach_ids=reach_ids,
        ) if (data_dir / "pollution_sources.csv").exists() else []
        workspace.disposals = load_disposals(
            data_dir / "disposals.csv",
            reaches=workspace.reaches, sources=workspace.sources,
        ) if (data_dir / "disposals.csv").exists() else []
        reports, items = load_reports_actions(
            data_dir / "reports_actions.csv", valid_reach_ids=reach_ids)
        workspace.reports, workspace.actions = reports, items
        workspace.policies = load_policies(
            data_dir / "inspection_policy.csv",
            valid_reach_ids=reach_ids,
        ) if (data_dir / "inspection_policy.csv").exists() else []
    except IngestError as exc:
        raise SystemExit(f"导入失败：{exc}") from exc

    state_dir.mkdir(parents=True, exist_ok=True)
    workspace.save()
    log = EventLog(state_dir / "events.jsonl")
    log.append("workspace_initialized", {
        "data_dir": str(data_dir),
        "counts": {
            "reaches": len(workspace.reaches),
            "evidence": len(workspace.evidence),
            "sources": len(workspace.sources),
            "disposals": len(workspace.disposals),
            "reports": len(workspace.reports),
            "actions": len(workspace.actions),
        },
    })
    print(f"已初始化工作区 {state_dir}："
          f"{len(workspace.reaches)} 个河段，{len(workspace.evidence)} 条观测，"
          f"{len(workspace.reports)} 条社区反馈，{len(workspace.actions)} 个事项。")
    print("可运行 `review --as-of 2026-07-01` 生成首次复核结论。")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    workspace = _load_workspace(Path(args.state_dir))
    batch_dir = Path(args.batch_dir)
    try:
        new_evidence: list = []
        new_reports: list = []
        new_items: list = []
        new_disposals: list = []
        new_sources: list = []

        path = batch_dir / "evidence.csv"
        if path.exists():
            batch = load_evidence(
                path, season_resolver=season_of,
                valid_reach_ids={r.reach_id for r in workspace.reaches})
            workspace.ensure_new_evidence_ids([e.evidence_id for e in batch])
            new_evidence = batch
            workspace.evidence.extend(batch)

        path = batch_dir / "pollution_sources.csv"
        if path.exists():
            batch = load_sources(
                path,
                valid_reach_ids={r.reach_id for r in workspace.reaches})
            existing = {s.source_id for s in workspace.sources}
            clash = sorted({s.source_id for s in batch} & existing)
            if clash:
                raise IngestError(f"污染源 ID 已存在: {', '.join(clash)}")
            new_sources = batch
            workspace.sources.extend(batch)

        path = batch_dir / "disposals.csv"
        if path.exists():
            batch = load_disposals(
                path, reaches=workspace.reaches, sources=workspace.sources)
            existing = {d.disposal_id for d in workspace.disposals}
            clash = sorted({d.disposal_id for d in batch} & existing)
            if clash:
                raise IngestError(f"处置 ID 已存在: {', '.join(clash)}")
            new_disposals = batch
            workspace.disposals.extend(batch)

        path = batch_dir / "reports_actions.csv"
        if path.exists():
            batch_reports, batch_items = load_reports_actions(
                path,
                valid_reach_ids={r.reach_id for r in workspace.reaches})
            workspace.ensure_new_record_ids(
                [r.record_id for r in batch_reports]
                + [a.record_id for a in batch_items])
            new_reports, new_items = batch_reports, batch_items
            workspace.reports.extend(batch_reports)
            workspace.actions.extend(batch_items)
    except (IngestError, ValueError) as exc:
        raise SystemExit(f"批次导入失败（未写入任何数据）：{exc}") from exc

    log = workspace.event_log()
    if new_evidence:
        log.append("evidence_ingested", {
            "batch_dir": str(batch_dir),
            "evidence_ids": [e.evidence_id for e in new_evidence],
            "seasons": sorted({e.season for e in new_evidence}),
        })
    if new_sources:
        log.append("source_ingested", {
            "source_ids": [s.source_id for s in new_sources]})
    if new_disposals:
        log.append("disposal_ingested", {
            "disposals": [disposal_coverage(d) for d in new_disposals]})
    if new_reports or new_items:
        log.append("report_or_action_ingested", {
            "report_ids": [r.record_id for r in new_reports],
            "action_ids": [a.record_id for a in new_items],
        })

    previous_bundle = None
    if workspace.latest_reach_conclusions:
        previous_bundle = ReviewBundle(
            as_of=date.fromisoformat(
                next(iter(workspace.latest_reach_conclusions.values())).as_of),
            reach_conclusions=list(workspace.latest_reach_conclusions.values()),
            project_conclusions=list(workspace.latest_project_conclusions.values()),
            clusters=cluster_reports(workspace.reports),
            schedule_flags=[],
        )

    as_of = args.as_of or date.today()
    bundle = run_review(workspace, as_of, max_age_days=args.max_age_days)
    diff = diff_bundles(
        previous_bundle, bundle, workspace=workspace,
        new_evidence_ids=[e.evidence_id for e in new_evidence],
        new_report_ids=[r.record_id for r in new_reports],
        new_item_ids=[a.record_id for a in new_items],
    )
    _record_review(workspace, bundle, "review_after_ingest",
                   extra={"batch_dir": str(batch_dir)})
    log.append("review_refresh", {
        "batch_dir": str(batch_dir),
        "reach_changes": [
            {"reach": rid, "from": old, "to": new, "reasons": reasons}
            for rid, old, new, reasons in diff.reach_changes
        ],
        "project_changes": [
            {"project": pid, "from": old, "to": new, "reasons": reasons}
            for pid, old, new, reasons in diff.project_changes
        ],
        "new_clusters": [
            {"cluster": c.cluster_id, "members": c.member_ids}
            for c in diff.new_clusters
        ],
        "affected_open_actions": diff.affected_actions,
        "schedule_flags": [vars(f) for f in diff.schedule_flags],
    })
    workspace.save()

    print(f"批次 {batch_dir} 导入完成："
          f"+{len(new_evidence)} 观测，+{len(new_sources)} 污染源，"
          f"+{len(new_disposals)} 处置，+{len(new_reports)} 反馈，"
          f"+{len(new_items)} 事项。")
    print("\n== 新观测到达后的待更新清单 ==")
    if diff.reach_changes:
        for rid, old, new, reasons in diff.reach_changes:
            print(f"  河段结论 {rid}: {old} → {new}（{', '.join(reasons)}）")
    else:
        print("  河段结论：无变化")
    if diff.project_changes:
        for pid, old, new, reasons in diff.project_changes:
            print(f"  项目结论 {pid}: {old} → {new}（{', '.join(reasons)}）")
    if diff.new_clusters:
        for cluster in diff.new_clusters:
            print(f"  反馈聚类 {cluster.cluster_id}: "
                  f"{', '.join(cluster.member_ids)}"
                  + ("（自动关联为同一线索，按 1 项证据）"
                     if len(cluster.member_ids) > 1 else ""))
    if diff.affected_actions:
        print("  需要回看的开放事项:")
        for rid, ids in diff.affected_actions.items():
            print(f"    {rid}: {', '.join(ids)}")
    if diff.schedule_flags:
        print("  巡查安排待调整:")
        for flag in diff.schedule_flags:
            print(f"    {flag.reach_id}: {flag.current_mode} → "
                  f"{flag.expected_mode}（{flag.reason}）")
    if not (diff.reach_changes or diff.project_changes
            or diff.new_clusters or diff.affected_actions
            or diff.schedule_flags):
        print("  无需更新的结论、事项或巡查安排。")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    workspace = _load_workspace(Path(args.state_dir))
    as_of = args.as_of or date.today()
    bundle = run_review(workspace, as_of, max_age_days=args.max_age_days)
    _record_review(workspace, bundle, "review_run",
                   extra={"trigger": "manual", "as_of": as_of.isoformat()})
    print(f"复核完成（基准日 {as_of.isoformat()}，"
          f"观测时效窗口 {args.max_age_days} 天）")
    for conclusion in bundle.reach_conclusions:
        _print_reach_conclusion(conclusion, workspace)
    for conclusion in bundle.project_conclusions:
        _print_project_conclusion(conclusion, workspace)
    if bundle.schedule_flags:
        print("\n巡查安排待调整:")
        for flag in bundle.schedule_flags:
            print(f"  - {flag.reach_id}: {flag.current_mode} → "
                  f"{flag.expected_mode}（{flag.reason}）")
    return 0


def cmd_reaches(args: argparse.Namespace) -> int:
    workspace = _load_workspace(Path(args.state_dir))
    print(f"{'河段':<8}{'河流':<12}{'里程(km)':<14}{'项目':<10}最新结论")
    for reach in workspace.reaches:
        conclusion = workspace.latest_reach_conclusions.get(reach.reach_id)
        status = (
            STATUS_TEXT[conclusion.status] if conclusion else "尚未复核"
        )
        span = f"{reach.upstream_km:g}-{reach.downstream_km:g}"
        print(f"{reach.reach_id:<8}{reach.river:<12}{span:<14}"
              f"{reach.project_id:<10}{status}")
    return 0


def cmd_conclusion(args: argparse.Namespace) -> int:
    workspace = _load_workspace(Path(args.state_dir))
    if args.target in {r.reach_id for r in workspace.reaches}:
        conclusion = workspace.latest_reach_conclusions.get(args.target)
        if conclusion is None:
            raise SystemExit(f"河段 {args.target} 尚无复核结论，请先运行 review")
        _print_reach_conclusion(conclusion, workspace)
        return 0
    if args.target in {r.project_id for r in workspace.reaches}:
        conclusion = workspace.latest_project_conclusions.get(args.target)
        if conclusion is None:
            raise SystemExit(f"项目 {args.target} 尚无复核结论，请先运行 review")
        _print_project_conclusion(conclusion, workspace)
        return 0
    raise SystemExit(f"找不到河段或项目: {args.target}")


def cmd_actions(args: argparse.Namespace) -> int:
    workspace = _load_workspace(Path(args.state_dir))
    print("== 事项（调查/处置） ==")
    if not workspace.actions:
        print("  （无）")
    for item in workspace.actions:
        due = item.due_date.isoformat() if item.due_date else "-"
        refs = ", ".join(item.evidence_refs) or "-"
        print(f"  {item.record_id} [{item.status}] {item.kind} "
              f"@ {item.reach_id} | 责任方: {item.owner or '未分派'} | "
              f"期限: {due} | 证据: {refs} | {item.description}")
    print("\n== 社区反馈聚类（重复反馈只计一项独立证据） ==")
    for cluster in cluster_reports(workspace.reports):
        print(f"  {explain_cluster(cluster)}")
    return 0


def cmd_assign(args: argparse.Namespace) -> int:
    workspace = _load_workspace(Path(args.state_dir))
    due = _parse_day(args.due)
    if args.from_report:
        report = next((r for r in workspace.reports
                       if r.record_id == args.from_report), None)
        if report is None:
            raise SystemExit(f"找不到社区反馈 {args.from_report}")
        reach_id = report.reach_id
        evidence_refs = [report.record_id]
        description = f"源自社区反馈 {report.record_id}: {report.description}"
        created_at = report.reported_at
    else:
        reach_id = args.reach
        if reach_id not in {r.reach_id for r in workspace.reaches}:
            raise SystemExit(f"未知河段 {reach_id}")
        evidence_refs = [e.strip() for e in (args.evidence or "").split(",")
                         if e.strip()]
        valid_ev = {e.evidence_id for e in workspace.evidence}
        bad = [ref for ref in evidence_refs if ref not in valid_ev]
        if bad:
            raise SystemExit(f"证据 ID 不存在: {', '.join(bad)}")
        description = args.description or ""
        created_at = args.as_of or date.today()

    if args.kind not in ITEM_KINDS:
        raise SystemExit(f"事项类型需为 {ITEM_KINDS}")
    record_id = args.record_id
    if workspace.open_action(record_id) is not None or \
            any(r.record_id == record_id for r in workspace.reports):
        raise SystemExit(f"记录 ID {record_id} 已存在")

    item = ActionItem(
        record_id=record_id,
        reach_id=reach_id,
        created_at=created_at,
        kind=args.kind,
        description=description,
        status="open",
        owner=args.owner,
        due_date=due,
        evidence_refs=evidence_refs,
    )
    workspace.actions.append(item)
    workspace.event_log().append("action_assigned", {
        "record_id": record_id,
        "reach_id": reach_id,
        "kind": args.kind,
        "owner": args.owner,
        "due_date": due.isoformat(),
        "evidence_refs": evidence_refs,
        "from_report": args.from_report,
    })
    workspace.save()
    print(f"已分派事项 {record_id}（{args.kind}）→ {args.owner}，"
          f"期限 {due.isoformat()}，关联证据 {evidence_refs or '无'}")
    print("注意：运行 review 可查看该事项对复核结论与巡查安排的影响。")
    return 0


def cmd_update_action(args: argparse.Namespace) -> int:
    workspace = _load_workspace(Path(args.state_dir))
    item = workspace.open_action(args.record_id)
    if item is None:
        raise SystemExit(f"找不到开放事项 {args.record_id}")
    if args.status not in ACTION_STATUSES:
        raise SystemExit(f"状态需为 {ACTION_STATUSES}")
    before = item.status
    item.status = args.status
    if args.owner:
        item.owner = args.owner
    new_refs = [r.strip() for r in (args.add_evidence or "").split(",")
                if r.strip()]
    valid_ev = {e.evidence_id for e in workspace.evidence} | \
        {r.record_id for r in workspace.reports} | \
        {d.disposal_id for d in workspace.disposals}
    bad = [ref for ref in new_refs if ref not in valid_ev]
    if bad:
        raise SystemExit(f"证据 ID 不存在: {', '.join(bad)}")
    for ref in new_refs:
        if ref not in item.evidence_refs:
            item.evidence_refs.append(ref)
    workspace.event_log().append("action_updated", {
        "record_id": item.record_id,
        "status_from": before,
        "status_to": item.status,
        "owner": item.owner,
        "evidence_refs": item.evidence_refs,
        "note": args.note or "",
    })
    workspace.save()
    print(f"事项 {item.record_id} 状态 {before} → {item.status}，"
          f"当前证据 {item.evidence_refs}")
    return 0


def cmd_schedule(args: argparse.Namespace) -> int:
    workspace = _load_workspace(Path(args.state_dir))
    if not workspace.latest_reach_conclusions:
        raise SystemExit("尚无复核结论，请先运行 review")
    if args.apply:
        applied = []
        for reach in workspace.reaches:
            policy = next((p for p in workspace.policies
                           if p.reach_id == reach.reach_id), None)
            if policy is None:
                continue
            conclusion = workspace.latest_reach_conclusions[reach.reach_id]
            expected = expected_schedule(conclusion.status, policy)
            current = workspace.schedules.get(reach.reach_id)
            if current is None or current.mode != expected.mode:
                expected.last_changed = date.today().isoformat()
                workspace.schedules[reach.reach_id] = expected
                applied.append((reach.reach_id, expected.mode,
                                expected.current_interval_days))
        if not applied:
            print("巡查安排已是最新，无需调整。")
            return 0
        workspace.event_log().append("schedule_changed", {
            "changes": [
                {"reach": rid, "mode": mode, "interval_days": interval}
                for rid, mode, interval in applied
            ],
        })
        workspace.save()
        for rid, mode, interval in applied:
            print(f"  {rid} → {mode} 巡查，间隔 {interval} 天")
        return 0

    print(f"{'河段':<8}{'结论':<22}{'当前模式':<12}{'应有模式':<12}间隔(天)")
    for reach in workspace.reaches:
        policy = next((p for p in workspace.policies
                       if p.reach_id == reach.reach_id), None)
        conclusion = workspace.latest_reach_conclusions[reach.reach_id]
        current = workspace.schedules.get(reach.reach_id)
        if policy is None:
            print(f"{reach.reach_id:<8}{STATUS_TEXT[conclusion.status]:<22}"
                  f"{current.mode if current else 'standard':<12}"
                  "未配置策略")
            continue
        expected = expected_schedule(conclusion.status, policy)
        current_mode = current.mode if current else "standard"
        flag = " ← 需调整" if current_mode != expected.mode else ""
        print(f"{reach.reach_id:<8}{STATUS_TEXT[conclusion.status]:<22}"
              f"{current_mode:<12}{expected.mode:<12}"
              f"{expected.current_interval_days}{flag}")
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    workspace = _load_workspace(Path(args.state_dir))
    events = workspace.event_log().read()
    if args.event_type:
        events = [e for e in events if e.event_type == args.event_type]
    if args.target:
        def mentions(event) -> bool:
            details = event.details
            if details.get("key") == args.target or \
                    details.get("reach_id") == args.target or \
                    details.get("record_id") == args.target:
                return True
            return args.target in json_safe_str(details)
        events = [e for e in events if mentions(e)]
    if not events:
        print("没有匹配的审计事件。")
        return 0
    for event in events:
        print(f"#{event.seq} {event.at} {event.event_type} "
              f"({event.actor})")
        for key, value in event.details.items():
            print(f"    {key}: {value}")
    return 0


def json_safe_str(value) -> str:
    import json
    return json.dumps(value, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m riverreview",
        description="河段治理复核分析（标准库实现，证据可解释、不确定性可保留）",
    )
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR),
                        help="工作区状态目录，默认 .riverreview")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="从 CSV 目录初始化工作区")
    p.add_argument("data_dir", help="包含 reaches.csv 等夹具的目录")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("ingest", help="增量导入新一批 CSV 并给出待更新清单")
    p.add_argument("batch_dir", help="批次 CSV 目录")
    p.add_argument("--as-of", type=_parse_day, help="复核基准日，默认今天")
    p.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS,
                   help="观测时效窗口天数，默认 120")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("review", help="重新复核并记录结论修订")
    p.add_argument("--as-of", type=_parse_day, help="复核基准日，默认今天")
    p.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS)
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("reaches", help="列出河段与最新结论")
    p.set_defaults(func=cmd_reaches)

    p = sub.add_parser("conclusion", help="查看河段或项目的可解释结论")
    p.add_argument("target", help="河段 ID（如 R-A）或项目 ID（如 REST-1）")
    p.set_defaults(func=cmd_conclusion)

    p = sub.add_parser("actions", help="列出事项与社区反馈聚类")
    p.set_defaults(func=cmd_actions)

    p = sub.add_parser("assign", help="分派调查/处置事项")
    p.add_argument("record_id", help="新事项 ID，如 T01")
    p.add_argument("--kind", required=True, choices=ITEM_KINDS)
    p.add_argument("--owner", required=True)
    p.add_argument("--due", required=True, help="期限 YYYY-MM-DD")
    p.add_argument("--reach", help="不基于反馈新建时必填的河段 ID")
    p.add_argument("--from-report", help="基于某条社区反馈创建")
    p.add_argument("--description")
    p.add_argument("--evidence", help="逗号分隔的证据 ID")
    p.add_argument("--as-of", type=_parse_day)
    p.set_defaults(func=cmd_assign)

    p = sub.add_parser("update-action", help="更新事项状态/责任方/证据")
    p.add_argument("record_id")
    p.add_argument("--status", required=True, choices=ACTION_STATUSES)
    p.add_argument("--owner")
    p.add_argument("--add-evidence", help="逗号分隔的追加证据 ID")
    p.add_argument("--note")
    p.set_defaults(func=cmd_update_action)

    p = sub.add_parser("schedule", help="查看或应用巡查频率调整")
    p.add_argument("--apply", action="store_true", help="按最新结论应用调整")
    p.set_defaults(func=cmd_schedule)

    p = sub.add_parser("history", help="回看审计事件与结论修订原因")
    p.add_argument("--event-type")
    p.add_argument("--target", help="按河段/项目/事项 ID 过滤")
    p.set_defaults(func=cmd_history)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
