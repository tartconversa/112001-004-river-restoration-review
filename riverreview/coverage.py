"""处置记录覆盖判定。

领域规则：一项处置记录只能支持它实际覆盖的污染源或区段。

判定一个污染源是否“已处置”，仅接受满足全部条件的记录：

1. 处置状态为 ``complete`` 且有完成日期；
2. 处置显式引用该污染源（source_id 精确匹配），或处置区间在同一河段内
   覆盖污染源里程（按里程区间匹配，不跨河段推断）；
3. 完成日期不晚于复核基准日。

未完成的处置、位于其他河段的处置，都不能消除该污染源对恢复结论的阻碍。
"""

from __future__ import annotations

from datetime import date

from .models import (
    DISPOSAL_COMPLETE,
    Disposal,
    PollutionSource,
)


def completed_disposals(
    disposals: list[Disposal], as_of: date
) -> list[Disposal]:
    return [
        d
        for d in disposals
        if d.status == DISPOSAL_COMPLETE
        and d.completed_at is not None
        and d.completed_at <= as_of
    ]


def source_is_addressed(
    source: PollutionSource,
    disposals: list[Disposal],
    as_of: date,
) -> tuple[bool, str | None]:
    """返回 (是否已处置, 支持该判定的处置记录 ID)。"""
    for disposal in completed_disposals(disposals, as_of):
        # 精确引用优先
        if disposal.source_id == source.source_id:
            if disposal.reach_id != source.reach_id:
                # 数据在导入阶段已拦截，这里是防御性检查
                continue
            return True, disposal.disposal_id
        # 无显式 source_id 时，只允许同河段的里程覆盖
        if (
            disposal.source_id is None
            and disposal.reach_id == source.reach_id
            and disposal.covered_from_km <= source.location_km <= disposal.covered_to_km
        ):
            return True, disposal.disposal_id
    return False, None


def unaddressed_sources(
    sources: list[PollutionSource],
    disposals: list[Disposal],
    as_of: date,
) -> dict[str, str | None]:
    """返回 {未处置污染源 ID: 最接近的在途/计划处置 ID（如有）}。"""
    result: dict[str, str | None] = {}
    for source in sources:
        addressed, _ = source_is_addressed(source, disposals, as_of)
        if addressed:
            continue
        candidate = next(
            (
                d.disposal_id
                for d in disposals
                if (
                    d.reach_id == source.reach_id
                    and (
                        d.source_id == source.source_id
                        or (
                            d.source_id is None
                            and d.covered_from_km <= source.location_km
                            <= d.covered_to_km
                        )
                    )
                )
            ),
            None,
        )
        result[source.source_id] = candidate
    return result


def disposal_coverage(disposal: Disposal) -> str:
    target = disposal.source_id or "里程覆盖"
    return (
        f"{disposal.disposal_id}[{disposal.status}] 覆盖 {target} @ "
        f"{disposal.reach_id} {disposal.covered_from_km}-{disposal.covered_to_km}km"
    )
