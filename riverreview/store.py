"""工作区状态持久化。

状态目录（默认 ``.riverreview/``）包含：

- ``state.json`` 当前全部实体与最新结论快照（可整体重建，非审计来源）；
- ``events.jsonl`` 仅追加审计事件（事实来源，见 :mod:`riverreview.events`）。

JSON 不携带类型信息，序列化/反序列化在本模块集中处理，日期统一转 ISO。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, is_dataclass
from datetime import date
from pathlib import Path
from typing import Optional

from .events import EventLog
from .models import (
    ActionItem,
    CommunityReport,
    Criterion,
    Disposal,
    Evidence,
    InspectionPolicy,
    PollutionSource,
    ProjectConclusion,
    Reach,
    ReachConclusion,
    Schedule,
)
from .review import ReviewBundle

STATE_FILE = "state.json"
EVENTS_FILE = "events.jsonl"
STATE_VERSION = 1


def _iso(day: Optional[date]) -> Optional[str]:
    return day.isoformat() if day else None


def _date(raw: Optional[str]) -> Optional[date]:
    return date.fromisoformat(raw) if raw else None


# 每个实体的日期字段清单，用于通用反序列化
_DATE_FIELDS = {
    "Criterion": ("valid_from", "valid_to"),
    "Evidence": ("observed_on",),
    "PollutionSource": ("first_reported",),
    "Disposal": ("completed_at",),
    "CommunityReport": ("reported_at", "due_date"),
    "ActionItem": ("created_at", "due_date"),
}


@dataclass
class Workspace:
    root: Path
    reaches: list[Reach] = field(default_factory=list)
    criteria: list[Criterion] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    sources: list[PollutionSource] = field(default_factory=list)
    disposals: list[Disposal] = field(default_factory=list)
    reports: list[CommunityReport] = field(default_factory=list)
    actions: list[ActionItem] = field(default_factory=list)
    policies: list[InspectionPolicy] = field(default_factory=list)
    schedules: dict[str, Schedule] = field(default_factory=dict)
    latest_reach_conclusions: dict[str, ReachConclusion] = field(default_factory=dict)
    latest_project_conclusions: dict[str, ProjectConclusion] = field(default_factory=dict)
    revision_counter: dict[str, int] = field(default_factory=dict)

    # -- 主键守卫 -----------------------------------------------------------

    def ensure_new_evidence_ids(self, ids: list[str]) -> None:
        existing = {e.evidence_id for e in self.evidence}
        clash = sorted(set(ids) & existing)
        if clash:
            raise ValueError(f"证据 ID 已存在，拒绝重复导入: {', '.join(clash)}")

    def ensure_new_record_ids(self, ids: list[str]) -> None:
        existing = {r.record_id for r in self.reports} | \
            {a.record_id for a in self.actions}
        clash = sorted(set(ids) & existing)
        if clash:
            raise ValueError(f"记录 ID 已存在，拒绝重复导入: {', '.join(clash)}")

    # -- 修订号 -------------------------------------------------------------

    def bump_revision(self, scope: str, key: str) -> int:
        token = f"{scope}:{key}"
        self.revision_counter[token] = self.revision_counter.get(token, 0) + 1
        return self.revision_counter[token]

    def current_revision(self, scope: str, key: str) -> int:
        return self.revision_counter.get(f"{scope}:{key}", 0)

    # -- 结论快照 -----------------------------------------------------------

    def store_bundle(self, bundle: ReviewBundle) -> None:
        self.latest_reach_conclusions = {
            c.reach_id: c for c in bundle.reach_conclusions
        }
        self.latest_project_conclusions = {
            c.project_id: c for c in bundle.project_conclusions
        }

    # -- 持久化 -------------------------------------------------------------

    def save(self) -> None:
        payload = {
            "version": STATE_VERSION,
            "reaches": [self._dump(r) for r in self.reaches],
            "criteria": [self._dump(r) for r in self.criteria],
            "evidence": [self._dump(r) for r in self.evidence],
            "sources": [self._dump(r) for r in self.sources],
            "disposals": [self._dump(r) for r in self.disposals],
            "reports": [self._dump(r) for r in self.reports],
            "actions": [self._dump(r) for r in self.actions],
            "policies": [self._dump(p) for p in self.policies],
            "schedules": {k: self._dump(v)
                          for k, v in self.schedules.items()},
            "latest_reach_conclusions": {
                k: self._dump(v) for k, v in
                self.latest_reach_conclusions.items()
            },
            "latest_project_conclusions": {
                k: self._dump(v) for k, v in
                self.latest_project_conclusions.items()
            },
            "revision_counter": dict(self.revision_counter),
        }
        target = self.root / STATE_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(target)

    @staticmethod
    def _dump(obj) -> object:
        """把数据类（含嵌套）递归转为 JSON 可序列化的原始结构。"""
        if is_dataclass(obj):
            return {
                name: Workspace._dump(value)
                for name, value in vars(obj).items()
            }
        if isinstance(obj, date):
            return obj.isoformat()
        if isinstance(obj, dict):
            return {k: Workspace._dump(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [Workspace._dump(v) for v in obj]
        return obj

    @classmethod
    def load(cls, root: Path) -> "Workspace":
        target = root / STATE_FILE
        if not target.exists():
            raise FileNotFoundError(
                f"工作区尚未初始化: {target} 不存在，请先运行 init"
            )
        payload = json.loads(target.read_text(encoding="utf-8"))
        ws = cls(root=root)
        ws.reaches = [Reach(**r) for r in payload["reaches"]]
        ws.criteria = [cls._hydrate(Criterion, r) for r in payload["criteria"]]
        ws.evidence = [cls._hydrate(Evidence, r) for r in payload["evidence"]]
        ws.sources = [cls._hydrate(PollutionSource, r)
                      for r in payload["sources"]]
        ws.disposals = [cls._hydrate(Disposal, r)
                        for r in payload["disposals"]]
        ws.reports = [cls._hydrate(CommunityReport, r)
                      for r in payload["reports"]]
        ws.actions = [cls._hydrate(ActionItem, r)
                      for r in payload["actions"]]
        ws.policies = [InspectionPolicy(**p) for p in payload["policies"]]
        ws.schedules = {
            k: Schedule(**v) for k, v in payload.get("schedules", {}).items()
        }
        ws.latest_reach_conclusions = {
            k: Workspace._hydrate_reach(v)
            for k, v in payload.get("latest_reach_conclusions", {}).items()
        }
        ws.latest_project_conclusions = {
            k: cls._hydrate(ProjectConclusion, v)
            for k, v in payload.get("latest_project_conclusions", {}).items()
        }
        ws.revision_counter = dict(payload.get("revision_counter", {}))
        return ws

    @staticmethod
    def _hydrate_reach(data: dict) -> ReachConclusion:
        from .models import CriterionResult, ReachConclusion, SeasonResult
        data = dict(data)
        results = []
        for raw in data["criterion_results"]:
            raw = dict(raw)
            raw["season_results"] = [
                SeasonResult(**season) for season in raw["season_results"]
            ]
            results.append(CriterionResult(**raw))
        data["criterion_results"] = results
        return ReachConclusion(**data)

    @staticmethod
    def _hydrate(cls_, data: dict):
        data = dict(data)
        for name in _DATE_FIELDS.get(cls_.__name__, ()):
            if name in data:
                data[name] = _date(data[name])
        return cls_(**data)

    def event_log(self) -> EventLog:
        return EventLog(self.root / EVENTS_FILE)

    # -- 便捷查询 -----------------------------------------------------------

    def open_action(self, record_id: str) -> Optional[ActionItem]:
        return next((a for a in self.actions if a.record_id == record_id), None)

    def clusters_snapshot(self) -> list[Cluster]:
        from .clustering import cluster_reports
        return cluster_reports(self.reports)
