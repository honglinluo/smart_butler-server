"""
【模块说明】中国法定节假日检测 — 判断某天是不是工作日或休息日

当用户设置"工作日提醒"或"只在休息日执行某任务"时，系统需要知道具体哪天是工作日、
哪天是法定节假日（包括春节、国庆等）、哪天是调休补班（本来是周末但要上班）。

【内置数据】
  预置了 2025 和 2026 年国务院发布的节假日安排，包括：
  - 法定放假日期（含调休后的连假）
  - 调休补班日期（周末但需要上班的日期）

【三个判断函数】
  is_holiday(date)          → 这天是否放假（法定节假日）
  is_workday(date)          → 这天是否需要上班（包括调休补班的周末）
  is_weekend_or_holiday(date) → 这天是否可以休息（对应"周末任务"的触发条件）

中国法定节假日检测。

内置 2025-2026 年假日数据（静态维护），支持：
  - is_holiday(date)  → 是否法定节假日（含周末调休补班则返回 False）
  - is_workday(date)  → 是否工作日（法定节假日/周末 → False，调休补班 → True）

数据来源：国务院每年发布的节假日安排通知。
"""

from datetime import date, datetime
from typing import Optional, Set


# ── 2025 年法定节假日（放假日期集合）────────────────────────────────────────
_HOLIDAYS_2025: Set[date] = {
    # 元旦
    date(2025, 1, 1),
    # 春节（1月28日-2月4日）
    date(2025, 1, 28), date(2025, 1, 29), date(2025, 1, 30),
    date(2025, 1, 31), date(2025, 2, 1),  date(2025, 2, 2),
    date(2025, 2, 3),  date(2025, 2, 4),
    # 清明节
    date(2025, 4, 4), date(2025, 4, 5), date(2025, 4, 6),
    # 劳动节
    date(2025, 5, 1), date(2025, 5, 2), date(2025, 5, 3),
    date(2025, 5, 4), date(2025, 5, 5),
    # 端午节
    date(2025, 5, 31), date(2025, 6, 1), date(2025, 6, 2),
    # 国庆节 + 中秋节
    date(2025, 10, 1), date(2025, 10, 2), date(2025, 10, 3),
    date(2025, 10, 4), date(2025, 10, 5), date(2025, 10, 6),
    date(2025, 10, 7), date(2025, 10, 8),
}

# ── 2025 年调休补班（需上班的周末）──────────────────────────────────────────
_WORKDAYS_2025: Set[date] = {
    date(2025, 1, 26),  # 春节前补班
    date(2025, 2, 8),   # 春节后补班
    date(2025, 4, 27),  # 劳动节前补班
    date(2025, 9, 28),  # 国庆节前补班
    date(2025, 10, 11), # 国庆节后补班
}

# ── 2026 年法定节假日 ─────────────────────────────────────────────────────
_HOLIDAYS_2026: Set[date] = {
    # 元旦
    date(2026, 1, 1), date(2026, 1, 2),
    # 春节（2月17日-2月24日，估算）
    date(2026, 2, 17), date(2026, 2, 18), date(2026, 2, 19),
    date(2026, 2, 20), date(2026, 2, 21), date(2026, 2, 22),
    date(2026, 2, 23), date(2026, 2, 24),
    # 清明节
    date(2026, 4, 5), date(2026, 4, 6),
    # 劳动节
    date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 3),
    date(2026, 5, 4), date(2026, 5, 5),
    # 端午节
    date(2026, 6, 19), date(2026, 6, 20), date(2026, 6, 21),
    # 中秋节
    date(2026, 9, 25), date(2026, 9, 26), date(2026, 9, 27),
    # 国庆节
    date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3),
    date(2026, 10, 4), date(2026, 10, 5), date(2026, 10, 6),
    date(2026, 10, 7),
}

# ── 2026 年调休补班 ───────────────────────────────────────────────────────
_WORKDAYS_2026: Set[date] = {
    date(2026, 2, 15),  # 春节前补班
    date(2026, 2, 28),  # 春节后补班
    date(2026, 5, 9),   # 劳动节后补班
    date(2026, 10, 10), # 国庆节后补班
}

_ALL_HOLIDAYS: Set[date] = _HOLIDAYS_2025 | _HOLIDAYS_2026
_ALL_WORKDAYS: Set[date] = _WORKDAYS_2025 | _WORKDAYS_2026


def _to_date(d: date | datetime) -> date:
    return d.date() if isinstance(d, datetime) else d


def is_holiday(d: date | datetime) -> bool:
    """是否为法定节假日休息日（含调休后的放假日）。

    补班的周末不算节假日，返回 False。
    """
    day = _to_date(d)
    if day in _ALL_WORKDAYS:
        return False
    if day in _ALL_HOLIDAYS:
        return True
    return False


def is_workday(d: date | datetime) -> bool:
    """是否为工作日。

    法定放假日 → False
    调休补班周末 → True
    普通周一至周五 → True
    普通周末 → False
    """
    day = _to_date(d)
    if day in _ALL_HOLIDAYS:
        return False
    if day in _ALL_WORKDAYS:
        return True
    return day.weekday() < 5  # Mon=0 … Fri=4


def is_weekend_or_holiday(d: date | datetime) -> bool:
    """是否为周末/节假日（对应 TaskType.WEEKEND 的触发条件）。"""
    return not is_workday(d)
