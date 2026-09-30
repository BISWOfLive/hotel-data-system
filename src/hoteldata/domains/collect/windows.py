"""9 个数据窗口 + 窗口 → 日期区间口径 —— **A12 级遗产,逐字继承**。

单一事实源。对应旧系统 ``collectors/rules.py:40-45``(窗口名)、
``collectors/api_collector.py:102-114``(窗口键)、``api_collector.py:140-173``(区间口径)。

**最容易写错的一处**:三个"多日"窗口的**基准日不同**
--------------------------------------------------------------------
============  ==========  ====================  ==================================
窗口           基准日      区间                   说明
============  ==========  ====================  ==================================
``昨日``       昨日        ``[t-1, t-1]``        单日
``上周``       **今日**    ``[上周一, 上周日]``   **上一自然周**(周一~周日),两端含
``过去7天``    **昨日**    ``[t-7, t-1]``        = 昨日−6 ~ 昨日,两端含
``过去30天``   **昨日**    ``[t-30, t-1]``       = 昨日−29 ~ 昨日,两端含
``上月``       **今日**    ``[上月1日, 上月末]``  **上一自然月**,两端含
============  ==========  ====================  ==================================

> ``上周`` 的实现是 ``monday = t - timedelta(days=t.weekday())`` → 本周一,
> 再 ``[monday-7d, monday-1d]``。**即使 t 是周一,start 也是「上周一」而非「今天」**。
> 而 ``过去7天`` 的 ``end`` 是 ``t-1``(昨日)。二者基准日不同,不可互相推导。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

__all__ = [
    "ALL_WINDOWS",
    "DEFAULT_WINDOW",
    "WINDOW_KEYS",
    "WINDOW_LAST_30D",
    "WINDOW_LAST_7D",
    "WINDOW_LAST_MONTH",
    "WINDOW_LAST_WEEK",
    "WINDOW_NEXT_14D",
    "WINDOW_NEXT_30D",
    "WINDOW_REALTIME",
    "WINDOW_SET",
    "WINDOW_TODAY_REALTIME",
    "WINDOW_YESTERDAY",
    "compute_window_range",
    "normalize_window",
    "window_date_ctx",
]

# ---------------------------------------------------------------------------
# 窗口名常量(旧 rules.py:40-45 原文)
# ---------------------------------------------------------------------------

#: 模块 9(未来30天搜索热度)专属窗口:中文显示名与内部键 ``next_30d`` 均接受
WINDOW_NEXT_30D = "未来30天"
#: 模块 15(每日热度-未来14日热度)专属窗口
WINDOW_NEXT_14D = "未来14天"

WINDOW_YESTERDAY = "昨日"
WINDOW_TODAY_REALTIME = "今日实时"
WINDOW_LAST_7D = "过去7天"
WINDOW_LAST_WEEK = "上周"
WINDOW_LAST_30D = "过去30天"
WINDOW_LAST_MONTH = "上月"
WINDOW_REALTIME = "实时"

#: 9 个窗口的规范顺序(段1 §2.2 表格顺序,亦为校验白名单)
ALL_WINDOWS: tuple[str, ...] = (
    WINDOW_YESTERDAY,
    WINDOW_TODAY_REALTIME,
    WINDOW_LAST_7D,
    WINDOW_LAST_WEEK,
    WINDOW_LAST_30D,
    WINDOW_LAST_MONTH,
    WINDOW_REALTIME,
    WINDOW_NEXT_30D,
    WINDOW_NEXT_14D,
)

WINDOW_SET: frozenset[str] = frozenset(ALL_WINDOWS)

#: 子模块未声明 ``windows`` 时的默认窗口
DEFAULT_WINDOW = WINDOW_YESTERDAY

#: 中文名 → 内部键(旧 ``api_collector.py:102-114``)。
#: ★ 注意 ``未来30天``/``未来14天`` 的中文名与内部键**都接受**(两者等价)。
WINDOW_KEYS: dict[str, str] = {
    WINDOW_YESTERDAY: "yesterday",
    WINDOW_TODAY_REALTIME: "today_realtime",
    WINDOW_LAST_7D: "last_7d",
    WINDOW_LAST_WEEK: "last_week",
    WINDOW_LAST_30D: "last_30d",
    WINDOW_LAST_MONTH: "last_month",
    WINDOW_REALTIME: "realtime",
    WINDOW_NEXT_30D: "next_30d",
    WINDOW_NEXT_14D: "next_14d",
    # 内部键别名(幂等:已是内部键时 normalize 不报错)
    "yesterday": "yesterday",
    "today_realtime": "today_realtime",
    "last_7d": "last_7d",
    "last_week": "last_week",
    "last_30d": "last_30d",
    "last_month": "last_month",
    "realtime": "realtime",
    "next_30d": "next_30d",
    "next_14d": "next_14d",
}


class WindowError(ValueError):
    """非法窗口名。"""


def normalize_window(window: str | None) -> str:
    """窗口名规范化:接受中文名或内部键,统一返回**中文名**。"""
    name = (window or DEFAULT_WINDOW).strip()
    if name in WINDOW_KEYS:
        key = WINDOW_KEYS[name]
        # 内部键 → 中文名(反向查表,取第一个匹配的中文名)
        for cn, k in WINDOW_KEYS.items():
            if k == key and cn in WINDOW_SET:
                return cn
        return name
    raise WindowError(
        f"非法窗口名 {window!r};合法值为 {list(ALL_WINDOWS)} 或内部键 "
        f"{sorted({v for v in WINDOW_KEYS.values()})}"
    )


def compute_window_range(window: str, today: date | None = None) -> tuple[date, date]:
    """窗口 → 日期区间(**闭区间 ``[start, end]``,两端都含**)。

    分支判定顺序与旧实现一致(复现必须一致):
    ``yesterday`` → (``today_realtime``, ``realtime``) → ``last_7d`` → ``last_week``
    → ``last_30d`` → ``next_14d`` → ``next_30d`` → 兜底 ``last_month``。
    """
    t = today or date.today()
    key = WINDOW_KEYS.get(normalize_window(window))
    if key == "yesterday":
        return t - timedelta(days=1), t - timedelta(days=1)
    if key in ("today_realtime", "realtime"):
        return t, t
    if key == "last_7d":
        return t - timedelta(days=7), t - timedelta(days=1)
    if key == "last_week":
        monday = t - timedelta(days=t.weekday())  # 本周一(weekday: 周一=0)
        return monday - timedelta(days=7), monday - timedelta(days=1)
    if key == "last_30d":
        return t - timedelta(days=30), t - timedelta(days=1)
    if key == "next_14d":
        return t, t + timedelta(days=13)
    if key == "next_30d":
        return t, t + timedelta(days=29)
    # last_month:上一自然月(函数末尾兜底 return,与旧实现一致)
    first_this = t.replace(day=1)
    last_prev = first_this - timedelta(days=1)
    return last_prev.replace(day=1), last_prev


def window_date_ctx(window: str, today: Any = None) -> dict[str, str]:
    """窗口 → 日期占位符替换表。

    ★ ``date`` = **窗口末日**(单日窗口即该日),**不是采集日**。
    采集日只在 ``today`` 键体现(旧实现实测:``window_date_ctx`` 的 ``date``
    会覆盖 ctx 里的采集日)。
    """
    t = date.today() if today is None else (date.fromisoformat(today) if isinstance(today, str) else today)
    start, end = compute_window_range(window, t)
    fmt = "%Y-%m-%d"
    ch = normalize_window(window)
    return {
        "date": end.strftime(fmt),
        "startDate": start.strftime(fmt),
        "endDate": end.strftime(fmt),
        "statDate": end.strftime(fmt),
        "yesterday": (t - timedelta(days=1)).strftime(fmt),
        "today": t.strftime(fmt),
        "window": ch,
        "window_key": WINDOW_KEYS[ch],
    }


def parse_collect_date(value: str | date | datetime | None) -> date:
    """采集日归一(CLI ``--date`` / DB ``date`` / ``datetime`` 都吃)。"""
    if value is None:
        return date.today()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))
