"""CSV 导入与结构校验。

夹具按来源分文件，本模块逐文件解析并做最小必要的领域校验：

- 日期/数值可解析，枚举字段合法；
- 引用完整性（河段、污染源等外键存在）；
- 处置记录显式声明其覆盖的污染源或里程区间，区间必须落在声明河段内；
- 同文件内主键唯一。

校验失败抛出 :class:`IngestError`，调用方（CLI）负责呈现，绝不静默丢弃。
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path
from typing import Iterable, Optional

from .models import (
    ActionItem,
    CommunityReport,
    Criterion,
    Disposal,
    EVIDENCE_TYPES,
    Evidence,
    InspectionPolicy,
    PollutionSource,
    Reach,
    ACTION_STATUSES,
    DISPOSAL_COMPLETE,
    DISPOSAL_IN_PROGRESS,
    DISPOSAL_PLANNED,
    OPERATORS,
)


class IngestError(ValueError):
    """输入数据无法安全进入分析时抛出，消息中带文件名与行号。"""


_DISPOSAL_STATUSES = (
    DISPOSAL_COMPLETE,
    DISPOSAL_IN_PROGRESS,
    DISPOSAL_PLANNED,
)


def _parse_date(raw: str, *, field: str, row_no: int, file: str) -> Optional[date]:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise IngestError(f"{file} 第 {row_no} 行 {field} 日期无效: {raw!r}") from exc


def _parse_float(raw: str, *, field: str, row_no: int, file: str) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise IngestError(f"{file} 第 {row_no} 行 {field} 数值无效: {raw!r}") from exc


def _require(row: dict[str, str], column: str, row_no: int, file: str) -> str:
    value = (row.get(column) or "").strip()
    if not value:
        raise IngestError(f"{file} 第 {row_no} 行缺少必填列 {column!r}")
    return value


def _open_rows(path: Path) -> tuple[list[str], list[tuple[int, dict[str, str]]]]:
    if not path.exists():
        raise IngestError(f"找不到输入文件: {path}")
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None or any(not c.strip() for c in reader.fieldnames):
            raise IngestError(f"{path.name} 缺少有效的表头行")
        rows = [(i, row) for i, row in enumerate(reader, start=2)]
    return list(reader.fieldnames), rows


def _check_unique(ids: Iterable[str], *, file: str, column: str) -> None:
    seen: set[str] = set()
    for value in ids:
        if value in seen:
            raise IngestError(f"{file} 中 {column}={value!r} 重复，主键必须唯一")
        seen.add(value)


def load_reaches(path: Path) -> list[Reach]:
    file = path.name
    _, rows = _open_rows(path)
    reaches: list[Reach] = []
    for row_no, row in rows:
        reach = Reach(
            reach_id=_require(row, "reach_id", row_no, file),
            river=(row.get("river") or "").strip(),
            upstream_km=_parse_float(_require(row, "upstream_km", row_no, file),
                                     field="upstream_km", row_no=row_no, file=file),
            downstream_km=_parse_float(_require(row, "downstream_km", row_no, file),
                                       field="downstream_km", row_no=row_no, file=file),
            project_id=_require(row, "project_id", row_no, file),
        )
        if reach.upstream_km >= reach.downstream_km:
            raise IngestError(
                f"{file} 第 {row_no} 行河段里程非法: "
                f"upstream_km={reach.upstream_km} 必须小于 downstream_km={reach.downstream_km}"
            )
        reaches.append(reach)
    _check_unique((r.reach_id for r in reaches), file=file, column="reach_id")
    return reaches


def load_criteria(path: Path) -> list[Criterion]:
    file = path.name
    _, rows = _open_rows(path)
    criteria: list[Criterion] = []
    for row_no, row in rows:
        operator = _require(row, "operator", row_no, file)
        if operator not in OPERATORS:
            raise IngestError(
                f"{file} 第 {row_no} 行 operator={operator!r} 不支持，"
                f"可选: {sorted(OPERATORS)}"
            )
        criteria.append(Criterion(
            criterion_id=_require(row, "criterion_id", row_no, file),
            project_id=_require(row, "project_id", row_no, file),
            measure=_require(row, "measure", row_no, file),
            operator=operator,
            threshold=_parse_float(_require(row, "threshold", row_no, file),
                                   field="threshold", row_no=row_no, file=file),
            valid_from=_parse_date(row.get("valid_from", ""),
                                   field="valid_from", row_no=row_no, file=file),
            valid_to=_parse_date(row.get("valid_to", ""),
                                 field="valid_to", row_no=row_no, file=file),
        ))
    _check_unique((c.criterion_id for c in criteria), file=file, column="criterion_id")
    return criteria


def load_evidence(path: Path, *, season_resolver,
                  valid_reach_ids: Optional[set[str]] = None) -> list[Evidence]:
    file = path.name
    _, rows = _open_rows(path)
    evidence: list[Evidence] = []
    for row_no, row in rows:
        observed = _parse_date(_require(row, "date", row_no, file),
                               field="date", row_no=row_no, file=file)
        etype = _require(row, "evidence_type", row_no, file)
        if etype not in EVIDENCE_TYPES:
            raise IngestError(
                f"{file} 第 {row_no} 行 evidence_type={etype!r} 不支持，"
                f"可选: {EVIDENCE_TYPES}"
            )
        reach_id = _require(row, "reach_id", row_no, file)
        if valid_reach_ids is not None and reach_id not in valid_reach_ids:
            raise IngestError(
                f"{file} 第 {row_no} 行引用了未知河段 {reach_id!r}"
            )
        explicit_season = (row.get("season") or "").strip()
        evidence.append(Evidence(
            evidence_id=_require(row, "evidence_id", row_no, file),
            reach_id=reach_id,
            observed_on=observed,
            evidence_type=etype,
            measure=_require(row, "measure", row_no, file),
            value=_parse_float(_require(row, "value", row_no, file),
                               field="value", row_no=row_no, file=file),
            unit=(row.get("unit") or "").strip(),
            source_ref=(row.get("source_ref") or "").strip(),
            season=explicit_season or season_resolver(observed),
        ))
    _check_unique((e.evidence_id for e in evidence), file=file, column="evidence_id")
    return evidence


def load_sources(
    path: Path,
    *,
    valid_reach_ids: Optional[set[str]] = None,
) -> list[PollutionSource]:
    file = path.name
    _, rows = _open_rows(path)
    sources: list[PollutionSource] = []
    for row_no, row in rows:
        reach_id = _require(row, "reach_id", row_no, file)
        if valid_reach_ids is not None and reach_id not in valid_reach_ids:
            raise IngestError(
                f"{file} 第 {row_no} 行引用了未知河段 {reach_id!r}"
            )
        sources.append(PollutionSource(
            source_id=_require(row, "source_id", row_no, file),
            reach_id=reach_id,
            location_km=_parse_float(_require(row, "location_km", row_no, file),
                                     field="location_km", row_no=row_no, file=file),
            pollutant=_require(row, "pollutant", row_no, file),
            first_reported=_parse_date(row.get("first_reported", ""),
                                       field="first_reported", row_no=row_no,
                                       file=file),
        ))
    _check_unique((s.source_id for s in sources), file=file, column="source_id")
    return sources


def load_disposals(path: Path, *, reaches: list[Reach],
                   sources: list[PollutionSource]) -> list[Disposal]:
    file = path.name
    _, rows = _open_rows(path)
    reach_by_id = {r.reach_id: r for r in reaches}
    source_by_id = {s.source_id: s for s in sources}
    disposals: list[Disposal] = []
    for row_no, row in rows:
        status = _require(row, "status", row_no, file)
        if status not in _DISPOSAL_STATUSES:
            raise IngestError(
                f"{file} 第 {row_no} 行 status={status!r} 不支持，"
                f"可选: {_DISPOSAL_STATUSES}"
            )
        reach_id = _require(row, "reach_id", row_no, file)
        reach = reach_by_id.get(reach_id)
        if reach is None:
            raise IngestError(
                f"{file} 第 {row_no} 行引用了未知河段 {reach_id!r}"
            )
        source_id = (row.get("source_id") or "").strip() or None
        if source_id is not None and source_id not in source_by_id:
            raise IngestError(
                f"{file} 第 {row_no} 行引用了未知污染源 {source_id!r}"
            )
        covered_from = _parse_float(_require(row, "covered_from_km", row_no, file),
                                    field="covered_from_km", row_no=row_no, file=file)
        covered_to = _parse_float(_require(row, "covered_to_km", row_no, file),
                                  field="covered_to_km", row_no=row_no, file=file)
        if covered_from >= covered_to:
            raise IngestError(
                f"{file} 第 {row_no} 行处置覆盖里程非法: "
                f"{covered_from} 必须小于 {covered_to}"
            )
        # 一条处置记录只能支撑它实际覆盖的区段：区间不得越出声明河段。
        if not (reach.upstream_km <= covered_from < covered_to <= reach.downstream_km):
            raise IngestError(
                f"{file} 第 {row_no} 行处置 {row.get('disposal_id')!r} 的覆盖区间 "
                f"[{covered_from}, {covered_to}] 越出河段 {reach_id} "
                f"[{reach.upstream_km}, {reach.downstream_km}]"
            )
        if source_id is not None:
            source = source_by_id[source_id]
            if source.reach_id != reach_id or not (
                covered_from <= source.location_km <= covered_to
            ):
                raise IngestError(
                    f"{file} 第 {row_no} 行处置声明覆盖污染源 {source_id!r}，"
                    "但该污染源不在处置河段/里程覆盖范围内"
                )
        completed = _parse_date(row.get("completed_at", ""),
                                field="completed_at", row_no=row_no, file=file)
        if status == DISPOSAL_COMPLETE and completed is None:
            raise IngestError(
                f"{file} 第 {row_no} 行处置状态为 complete 但缺少 completed_at"
            )
        disposals.append(Disposal(
            disposal_id=_require(row, "disposal_id", row_no, file),
            source_id=source_id,
            reach_id=reach_id,
            covered_from_km=covered_from,
            covered_to_km=covered_to,
            status=status,
            completed_at=completed,
            evidence_ref=(row.get("evidence_ref") or "").strip(),
        ))
    _check_unique((d.disposal_id for d in disposals), file=file,
                  column="disposal_id")
    return disposals


def _validate_status(status: str, *, row_no: int, file: str) -> str:
    if status not in ACTION_STATUSES:
        raise IngestError(
            f"{file} 第 {row_no} 行 action_status={status!r} 不支持，"
            f"可选: {ACTION_STATUSES}"
        )
    return status


def load_reports_actions(
    path: Path,
    *,
    valid_reach_ids: Optional[set[str]] = None,
) -> tuple[list[CommunityReport], list[ActionItem]]:
    """读取社区反馈与处置事项混合表（保持随附夹具的原始形态）。

    ``kind=community`` 进入社区反馈集合；其余 kind 进入事项集合。
    可选列 ``linked_group`` 允许显式标注重复反馈，缺省时由聚类模块自动识别。
    """
    file = path.name
    _, rows = _open_rows(path)
    reports: list[CommunityReport] = []
    items: list[ActionItem] = []
    seen_report_ids: set[str] = set()
    seen_item_ids: set[str] = set()
    for row_no, row in rows:
        record_id = _require(row, "record_id", row_no, file)
        kind = _require(row, "kind", row_no, file)
        status = _validate_status(_require(row, "action_status", row_no, file),
                                  row_no=row_no, file=file)
        reach_id = _require(row, "reach_id", row_no, file)
        if valid_reach_ids is not None and reach_id not in valid_reach_ids:
            raise IngestError(
                f"{file} 第 {row_no} 行引用了未知河段 {reach_id!r}"
            )
        due = _parse_date(row.get("due_date", ""), field="due_date",
                          row_no=row_no, file=file)
        if kind == "community":
            if record_id in seen_report_ids:
                raise IngestError(f"{file} 中 record_id={record_id!r} 重复")
            seen_report_ids.add(record_id)
            reports.append(CommunityReport(
                record_id=record_id,
                reach_id=reach_id,
                reported_at=_parse_date(_require(row, "reported_at", row_no, file),
                                        field="reported_at", row_no=row_no,
                                        file=file),
                description=(row.get("description") or "").strip(),
                status=status,
                owner=(row.get("owner") or "").strip(),
                due_date=due,
                linked_group=(row.get("linked_group") or "").strip() or None,
            ))
        else:
            if record_id in seen_item_ids:
                raise IngestError(f"{file} 中 record_id={record_id!r} 重复")
            seen_item_ids.add(record_id)
            normalized_kind = "action" if kind == "action" else kind
            if normalized_kind not in ("action", "investigation", "disposal"):
                raise IngestError(
                    f"{file} 第 {row_no} 行 kind={kind!r} 不支持"
                )
            items.append(ActionItem(
                record_id=record_id,
                reach_id=reach_id,
                created_at=_parse_date(_require(row, "reported_at", row_no, file),
                                       field="reported_at", row_no=row_no,
                                       file=file),
                kind=normalized_kind,
                description=(row.get("description") or "").strip(),
                status=status,
                owner=(row.get("owner") or "").strip(),
                due_date=due,
                evidence_refs=[
                    part.strip()
                    for part in (row.get("evidence_refs") or "").split(";")
                    if part.strip()
                ],
            ))
    return reports, items


def load_policies(
    path: Path,
    *,
    valid_reach_ids: Optional[set[str]] = None,
) -> list[InspectionPolicy]:
    file = path.name
    _, rows = _open_rows(path)
    policies: list[InspectionPolicy] = []
    for row_no, row in rows:
        reach_id = _require(row, "reach_id", row_no, file)
        if valid_reach_ids is not None and reach_id not in valid_reach_ids:
            raise IngestError(
                f"{file} 第 {row_no} 行引用了未知河段 {reach_id!r}"
            )
        standard = int(_parse_float(_require(row, "standard_interval_days",
                                             row_no, file),
                                    field="standard_interval_days",
                                    row_no=row_no, file=file))
        reduced = int(_parse_float(_require(row, "reduced_interval_days",
                                            row_no, file),
                                   field="reduced_interval_days",
                                   row_no=row_no, file=file))
        if standard <= 0 or reduced <= 0 or reduced <= standard:
            raise IngestError(
                f"{file} 第 {row_no} 行巡查间隔非法: "
                f"standard={standard}, reduced={reduced}"
                "（要求 0 < standard < reduced，降频后间隔应更长）"
            )
        policies.append(InspectionPolicy(
            reach_id=reach_id,
            standard_interval_days=standard,
            reduced_interval_days=reduced,
        ))
    _check_unique((p.reach_id for p in policies), file=file, column="reach_id")
    return policies
