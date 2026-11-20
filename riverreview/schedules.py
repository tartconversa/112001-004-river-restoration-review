"""巡查频率调度。

只有复核结论为 ``recovered`` 的河段才允许进入低频巡查；
``uncertain`` 与 ``not_recovered`` 一律保持标准频率，
不确定性不得被“乐观地”解释为可降频。
"""

from __future__ import annotations

from .models import RECOVERED, InspectionPolicy, Schedule

STANDARD = "standard"
REDUCED = "reduced"


def expected_schedule(status: str, policy: InspectionPolicy) -> Schedule:
    if status == RECOVERED:
        return Schedule(
            reach_id=policy.reach_id,
            current_interval_days=policy.reduced_interval_days,
            mode=REDUCED,
        )
    return Schedule(
        reach_id=policy.reach_id,
        current_interval_days=policy.standard_interval_days,
        mode=STANDARD,
    )
