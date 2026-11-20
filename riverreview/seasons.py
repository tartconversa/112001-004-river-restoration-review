"""采样季节划分。

领域规则：不同采样季节的观测不得并入同一基线，因此季节是证据分组的
一级键。采用北半球气象季节：3-5 月春季、6-8 月夏季、9-11 月秋季、
12-2 月冬季。跨年时年份参与排序，避免去年冬季与今年冬季混同。
"""

from __future__ import annotations

from datetime import date

WINTER = "winter"
SPRING = "spring"
SUMMER = "summer"
AUTUMN = "autumn"

SEASON_ORDER = (WINTER, SPRING, SUMMER, AUTUMN)
SEASON_TEXT = {
    WINTER: "冬季",
    SPRING: "春季",
    SUMMER: "夏季",
    AUTUMN: "秋季",
}

_MONTH_TO_SEASON = {
    12: WINTER, 1: WINTER, 2: WINTER,
    3: SPRING, 4: SPRING, 5: SPRING,
    6: SUMMER, 7: SUMMER, 8: SUMMER,
    9: AUTUMN, 10: AUTUMN, 11: AUTUMN,
}


def season_of(day: date) -> str:
    """返回某一日期所属的气象季节。"""
    return _MONTH_TO_SEASON[day.month]


def season_key(day: date) -> tuple[int, int]:
    """可跨年排序的季节键：(年份, 季节序号)。

    12 月归入次年冬季键，使 2026-12 与 2027-01 落在同一冬季分组。
    """
    season = season_of(day)
    year = day.year + 1 if day.month == 12 else day.year
    return year, SEASON_ORDER.index(season)


def key_to_text(key: tuple[int, int]) -> str:
    return f"{key[0]} 年{SEASON_TEXT[SEASON_ORDER[key[1]]]}"
