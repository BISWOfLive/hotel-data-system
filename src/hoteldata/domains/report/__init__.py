"""报告域(段2 批次 C + D)—— 日报组装、22 项报告节奏、条件 DSL、渲染契约。

模块地图(计划书 §5.1 / §5.5 / §5.6)
===================================

====================  ==========================================================  ==========
模块                   职责                                                         任务号
====================  ==========================================================  ==========
``schedule.py``        ``config/report_schedule.json`` 加载 / 强校验 / 热加载        T2D.1
``engine.py``          四项桶 · 条件 DSL(D9) · 窗口择优 · 聚合环比(D10)              T2D.2/3/5
``render.py``          渲染契约(硬编码 markdown,无模板引擎)                       T2D.4
``daily.py``           日报段组装(标题行 + 轮换图 + 热点日历 + 比价钩子)            T2C.1/2
``service.py``         群维度编排 / 合并拆分 / 单跑 / 实时问答                       T2C.3, T2D.6
====================  ==========================================================  ==========

**依赖方向是单向的**:``schedule``/``engine``/``render`` 是纯逻辑(不碰 DB),
``daily`` 只组装内容,``service`` 才编排与取数。取数**只允许**走段1 的
``domains.collect.service`` 契约(计划书 §1.3:禁止直接读提取表或 import 提取器)。

本模块**不在导入期加载任何资源**:``__getattr__`` 惰性导入,避免
``import hoteldata.domains.report`` 就触发配置解析 / DB 依赖(CLI 单跑与测试都要轻)。
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    "ReportItem",
    "ReportService",
    "Schedule",
    "ScheduleError",
    "aggregate_with_compare",
    "buckets_for_date",
    "build_daily_message",
    "build_daily_section",
    "eval_condition",
    "fmt_num",
    "fmt_ratio",
    "get_schedule",
    "render_data_block",
    "rotation_preview",
]

#: 公开名 → 定义它的模块(惰性导入用)
_LAZY: dict[str, str] = {
    "ReportItem": "hoteldata.domains.report.schedule",
    "Schedule": "hoteldata.domains.report.schedule",
    "ScheduleError": "hoteldata.domains.report.schedule",
    "get_schedule": "hoteldata.domains.report.schedule",
    "aggregate_with_compare": "hoteldata.domains.report.engine",
    "buckets_for_date": "hoteldata.domains.report.engine",
    "eval_condition": "hoteldata.domains.report.engine",
    "fmt_num": "hoteldata.domains.report.render",
    "fmt_ratio": "hoteldata.domains.report.render",
    "render_data_block": "hoteldata.domains.report.render",
    "build_daily_message": "hoteldata.domains.report.daily",
    "build_daily_section": "hoteldata.domains.report.daily",
    "ReportService": "hoteldata.domains.report.service",
}


def __getattr__(name: str) -> Any:
    """按需导入公开名(PEP 562)。"""
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(module_name), name)


def __dir__() -> list[str]:
    return sorted(__all__)


def rotation_preview(day: Any = None) -> dict[str, Any]:
    """当日轮换预览(薄封装:不必为了看一眼轮换就构造 ``ReportService``)。

    段1 ``push_rotation.json`` 是**同一份清单**(采集 + 截图 + 推送共用),
    所以这里不查库、不取数。
    """
    from datetime import datetime

    from hoteldata.domains.collect.rotation import get_rotation
    from hoteldata.settings import get_settings

    target = day or datetime.now(get_settings().tzinfo).date()
    plan = get_rotation().pick(target)
    out = dict(plan.as_dict())
    out["names"] = plan.names
    return out
