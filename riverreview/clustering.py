"""社区反馈去重聚类。

领域规则：重复反馈应能关联，但不能被误计为多个独立证据。

两阶段处理：
1. 显式 ``linked_group`` 优先——同一河段、同一标注的反馈先合入一簇；
2. 自动补充分组——同一河段、归一化后描述相同、且首条与末条报告时间
   相差不超过 ``window_days``（默认 30 天）的反馈视为同一线索，使用
   并查集合并，避免传递性重复漏判。

一簇反馈整体只产生一条独立证据；簇是否开放取决于是否还有未关闭成员。
"""

from __future__ import annotations

import re
from collections import defaultdict

from .models import (
    OPEN_STATUSES,
    Cluster,
    CommunityReport,
    Reason,
)

_WORD_RE = re.compile(r"[a-z0-9一-鿿]+")


def normalize_description(text: str) -> str:
    """归一化描述：小写、去标点与多余空白，用于重复判定。"""
    tokens = _WORD_RE.findall((text or "").lower())
    return " ".join(tokens)


class _UnionFind:
    def __init__(self, items: list[str]) -> None:
        self._parent = {item: item for item in items}

    def find(self, item: str) -> str:
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, left: str, right: str) -> None:
        self._parent[self.find(left)] = self.find(right)

    def groups(self) -> dict[str, list[str]]:
        buckets: dict[str, list[str]] = defaultdict(list)
        for item in self._parent:
            buckets[self.find(item)].append(item)
        return dict(buckets)


def cluster_reports(
    reports: list[CommunityReport],
    *,
    window_days: int = 30,
) -> list[Cluster]:
    """把社区反馈按重复线索聚类。"""
    if not reports:
        return []
    uf = _UnionFind([r.record_id for r in reports])
    by_id = {r.record_id: r for r in reports}

    # 第一阶段：显式 linked_group（组名仅在同一河段内有效，禁止跨河段合并）
    group_reaches: dict[str, set[str]] = defaultdict(set)
    explicit: dict[tuple[str, str], list[str]] = defaultdict(list)
    for report in reports:
        if report.linked_group:
            group_reaches[report.linked_group].add(report.reach_id)
            explicit[(report.reach_id, report.linked_group)].append(
                report.record_id)
    for group_name, reach_ids in group_reaches.items():
        if len(reach_ids) > 1:
            raise ValueError(
                f"linked_group={group_name!r} 出现在多个河段 "
                f"{sorted(reach_ids)}，跨河段反馈不得合并"
            )
    for member_ids in explicit.values():
        for other in member_ids[1:]:
            uf.union(member_ids[0], other)

    # 第二阶段：同河段 + 同归一化描述 + 时间窗内自动关联
    buckets: dict[tuple[str, str], list[CommunityReport]] = defaultdict(list)
    for report in reports:
        key = (report.reach_id, normalize_description(report.description))
        buckets[key].append(report)
    for members in buckets.values():
        members.sort(key=lambda r: r.reported_at)
        anchor = members[0]
        for other in members[1:]:
            if (other.reported_at - anchor.reported_at).days <= window_days:
                uf.union(anchor.record_id, other.record_id)

    clusters: list[Cluster] = []
    for idx, member_ids in enumerate(sorted(uf.groups().values(),
                                            key=lambda ids: min(ids)), start=1):
        members = [by_id[m] for m in member_ids]
        members.sort(key=lambda r: (r.reported_at, r.record_id))
        primary = members[0]
        reach_id = primary.reach_id
        if any(r.reach_id != reach_id for r in members):
            # 河段是基线一级键：跨河段的“重复”不允许合并，显式分组也不例外
            raise ValueError(
                "聚类错误：不同河段的反馈被合并，请检查 linked_group 标注"
            )
        cluster = Cluster(
            cluster_id=f"CL-{idx:03d}",
            reach_id=reach_id,
            description=primary.description,
            primary_report_id=primary.record_id,
            member_ids=[r.record_id for r in members],
            is_open=any(r.status in OPEN_STATUSES for r in members),
        )
        clusters.append(cluster)
    clusters.sort(key=lambda c: (c.reach_id, c.cluster_id))
    return clusters


def clusters_for_reach(clusters: list[Cluster], reach_id: str) -> list[Cluster]:
    return [c for c in clusters if c.reach_id == reach_id]


def explain_cluster(cluster: Cluster) -> str:
    """生成供结论解释使用的可读文本。"""
    dup_note = ""
    if len(cluster.member_ids) > 1:
        dup_note = (
            f"（含 {len(cluster.member_ids)} 条关联反馈 "
            f"{', '.join(cluster.member_ids)}，按 1 项独立证据计；"
            f"原因码 {Reason.DUPLICATE_LINKED}）"
        )
    state = "仍开放" if cluster.is_open else "已关闭"
    return f"{cluster.cluster_id} {state}：{cluster.description}{dup_note}"
