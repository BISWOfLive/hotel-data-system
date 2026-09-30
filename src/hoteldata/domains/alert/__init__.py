"""★ 预警域(段2 批次 E)—— 计划书 §5.7 / §6 批次 E / 附录 D。

一个域七个模块,各管一件事(旧系统把同样的事塞进 ``alert_engine.py`` 608 行 +
``alert_push.py`` 323 行两个文件里):

======================  ==================================================================
模块                     只做什么
======================  ==================================================================
``rules``                ``alert_rules.json`` 加载 + **白名单字段校验** + ``alert_shots`` /
                         ``alert_lines`` 读写(T2E.1)
``state``                ``alert_states`` 状态机:当日去重 / 恢复清零 / 忽略(T2E.2)
``engine``               ★ slot 映射 + 六条规则判定 + 状态落库(T2E.3 / T2E.4)
``render``               ``config/prompts/<template>.md`` 填充 + 残留 ``{x}`` 清空(T2E.5)
``shots``                ``alert_shots.json`` → 段1 ``shot_url`` 附图,**失败仅告警**(T2E.6)
``summary``              每日汇总 + 送达率 + ``alert_logs`` 写入(T2E.7)
``service``              :class:`AlertService` —— 任务与命令的**单一门面**
======================  ==================================================================

★★ 三条语义陷阱(读任何模块前先看这三条)
==========================================

1. **"连续 7 天"不在状态机里,在数据里** —— ``unavailable_days`` 由 ``engine``
   从段1 ``alert_room_states`` **逐日推导**(可订即断;今日缺数据保守不触发);
   ``alert_states.streak`` **只是展示用计数,不是推送门槛**(V48)。
2. **两层时刻结构必须原样保留** —— 规则声明名义时刻(09:00/14:30/19:00),
   调度层 cron 错峰 +4 分钟(09:04/14:34/19:04),``engine.ROOM_SLOT_BY_TIME``
   **映回**名义时刻后再匹配 ``check_times``。合并两层 → slot 匹配立刻失效(V49)。
3. **D 只做携程三项**(``visitor_total`` / ``min_price`` / ``ratingall``,
   ``min_price`` 无均值走**排名兜底** ``rank > total/2``);**F 有量纲守卫**
   (热度值 ≤20 视为未配置 → 跳过阈值只按倒计时)。

导入策略
========

本包对外暴露的名字走 :pep:`562` 的模块级 ``__getattr__`(惰性导入):
``import hoteldata.domains.alert`` 本身**不拉起** ``push`` / ``bot`` / ``playwright``,
``ops.cleanup`` 这类只想知道"有哪些规则"的调用方不必付启动代价。
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    "ROOM_SLOT_BY_TIME",
    "AlertCheckResult",
    "AlertRule",
    "AlertRuleSet",
    "AlertRulesError",
    "AlertService",
    "AlertStateStore",
    "Trigger",
    "build_daily_summary",
    "capture",
    "check",
    "derive_unavailable_days",
    "load_lines",
    "load_rules",
    "load_shots",
    "render_trigger",
    "set_alert_lines",
    "nominal_slot",
    "write_log",
]

#: 对外名字 → (子模块, 属性名)
_LAZY: dict[str, tuple[str, str]] = {
    "ROOM_SLOT_BY_TIME": ("hoteldata.domains.alert.engine", "ROOM_SLOT_BY_TIME"),
    "AlertCheckResult": ("hoteldata.domains.alert.engine", "AlertCheckResult"),
    "Trigger": ("hoteldata.domains.alert.engine", "Trigger"),
    "check": ("hoteldata.domains.alert.engine", "check"),
    "derive_unavailable_days": ("hoteldata.domains.alert.engine", "derive_unavailable_days"),
    "nominal_slot": ("hoteldata.domains.alert.engine", "nominal_slot"),
    "AlertRule": ("hoteldata.domains.alert.rules", "AlertRule"),
    "AlertRuleSet": ("hoteldata.domains.alert.rules", "AlertRuleSet"),
    "AlertRulesError": ("hoteldata.domains.alert.rules", "AlertRulesError"),
    "load_lines": ("hoteldata.domains.alert.rules", "load_lines"),
    "load_rules": ("hoteldata.domains.alert.rules", "load_rules"),
    "load_shots": ("hoteldata.domains.alert.shots", "load_shots"),
    "set_alert_lines": ("hoteldata.domains.alert.rules", "set_alert_lines"),
    "AlertService": ("hoteldata.domains.alert.service", "AlertService"),
    "AlertStateStore": ("hoteldata.domains.alert.state", "AlertStateStore"),
    "render_trigger": ("hoteldata.domains.alert.render", "render_trigger"),
    "capture": ("hoteldata.domains.alert.shots", "capture"),
    "build_daily_summary": ("hoteldata.domains.alert.summary", "build_daily_summary"),
    "write_log": ("hoteldata.domains.alert.summary", "write_log"),
}


def __getattr__(name: str) -> Any:
    """惰性导出(PEP 562):首次访问才 import 对应子模块。"""
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(target[0])
    value = getattr(module, target[1])
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)
