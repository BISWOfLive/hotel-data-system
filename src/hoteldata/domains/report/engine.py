"""报告节奏引擎 —— 四项桶 / 条件 DSL / 窗口择优 / 聚合环比(T2D.2 · T2D.3 · T2D.5)。

为什么是**纯函数模块**
====================

计划书 §1.3 把旧系统 ``app/pusher.py``(647 行)当反面教材:一个文件里同时有取数、
判定、渲染、投递、读表。段2 的切法是 —— **engine 只做判定与算术,一行 DB 都不碰**,
所以 T2D.2 / T2D.3 / T2D.5 全部**离线可测**。取数在 :mod:`~hoteldata.domains.report.service`
(只走段1 的 ``collect.service`` 契约),排版在 :mod:`~hoteldata.domains.report.render`。

三处**逐字继承旧系统**的判定(改一个字就是行为变化)
==================================================

==========================  =============================================  ==================================
判定                         旧系统出处                                      新语义
==========================  =============================================  ==================================
四项桶                       ``report_engine.py:76-83``                     周一且 1 号 → daily+monday+month1
``daily_except_monday``      ``report_engine.py:86-96``                     周一让位给周报(svc_daily)
「实时优先昨日」              ``report_engine.py:99-125``(甲方 2026-08-26)   daily 桶内重排,首个有数据即止
==========================  =============================================  ==================================

★ D9:``in`` 必须是**可达**的一等算子
====================================

旧 ``report_engine.py:195-201``::

    if not field or op not in _OPS:      # ← _OPS 里没有 "in"
        return True                      # ← op="in" 在这里就返回了 True
    ...
    if op == "in":                       # ← 永远走不到
        return actual in value

``_OPS`` 只有 6 个比较算子,``in`` 不在其中 —— 于是计划书 §5.6 承诺的
``{"field":"lead_days","op":"in","value":[10,3]}`` **恒为 True**(等于没有条件,
该拦的不拦)。本实现把 ``in`` 放进 :data:`COND_OPS` 算子表,**先查表再比**,它就成了
不可绕过的一等算子;同一张表还是 ``schedule.py`` 的**校验白名单**,配置里写别的算子
**直接报错**,不会退回"恒真"。

★ 语义细节(计划书 §5.6 / 附录 D,必须继承)
==========================================

* ``0`` / ``""`` / ``None`` / 空容器 → **视为无值**(只影响 ``present`` 判定);
* **字段缺失 → ``False``**(防误发):取不到值一律不通过,不是"不拦";
* 数值清洗 = **去逗号 + 去 ``%``**(旧 ``window_queries.py:136`` 逐字),
  ``"1,234"`` → ``1234.0``、``"12.5%"`` → ``12.5``(**不除 100**,旧口径如此)。
"""

from __future__ import annotations

import operator
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
from typing import Any

__all__ = [
    "AGG_TIERS",
    "BUCKET_ENUM",
    "COND_OPS",
    "REALTIME_WINDOWS",
    "aggregate_payloads",
    "aggregate_with_compare",
    "all_item_windows",
    "buckets_for_date",
    "clean_number",
    "collect_windows_for_date",
    "eval_condition",
    "flatten_daily",
    "flatten_map",
    "flatten_scalars",
    "has_any_value",
    "is_present",
    "item_field",
    "item_windows_for_date",
    "pick_value",
    "windows_for_push",
]

# ---------------------------------------------------------------------------
# 常量(每一个都能在旧代码/计划书里找到出处)
# ---------------------------------------------------------------------------

#: 四项桶(计划书 §5.6「四项桶」;旧 ``report_engine.py:19`` ``BUCKET_ENUM``)
BUCKET_ENUM: tuple[str, ...] = ("daily", "monday", "month1", "realtime")

#: daily 桶内的排序依据 —— **实时优先于昨日**(甲方 2026-08-26 定稿;旧 ``report_engine.py:100``)
REALTIME_WINDOWS: tuple[str, ...] = ("今日实时", "实时")

#: 条件 DSL 的算子白名单。★ ``in`` 在表内 —— 这就是 D9 的修复点。
COND_OPS: tuple[str, ...] = (">", ">=", "<", "<=", "==", "!=", "in")

#: 聚合三档(旧 ``window_queries.py:152`` 的 ``sum``/``avg``/``last``)
AGG_TIERS: tuple[str, ...] = ("sum", "avg", "last")

#: 数值算子表。``in`` 用 :func:`_contains` 实现(集合包含,不是数值比较)。
_OP_FUNCS: dict[str, Callable[[Any, Any], bool]] = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
}


# ---------------------------------------------------------------------------
# 取值辅助
# ---------------------------------------------------------------------------


def item_field(item: Any, name: str, default: Any = None) -> Any:
    """从 ``ReportItem``(模型)或 ``dict``(旧结构/测试)里取一个字段。

    段2 的项既可能是 :class:`~hoteldata.domains.report.schedule.ReportItem`,
    也可能是从 JSON 直接读出的 dict(测试与 CLI dry-run)。取字段的地方统一走这里,
    免得每个函数都写一遍 ``isinstance(item, dict)``。
    """
    if isinstance(item, Mapping):
        value = item.get(name)
    else:
        value = getattr(item, name, None)
    return default if value is None else value


def is_present(value: Any) -> bool:
    """「有值」判定(计划书 §5.6 / 附录 D)。

    ``0`` / ``""`` / ``None`` / 空容器 → **无值**;其余 → 有值。
    ★ ``False`` 与 ``0`` 等价 → 也判无值(继承旧 ``v != 0`` 的写法)。
    """
    if value is None or value == "" or value == 0:
        return False
    if isinstance(value, (list, tuple, set, frozenset, dict)) and len(value) == 0:
        return False
    return True


def has_any_value(payload: Any) -> bool:
    """``{"present": true}`` 不带 ``field`` 时的口径:payload 中**任一**值有值。"""
    if not isinstance(payload, Mapping):
        return False
    return any(is_present(v) for v in payload.values())


def clean_number(value: Any) -> float | None:
    """数值清洗(model 计划书 §6 T2D.5;旧 ``window_queries.py:136`` 逐字)。

    ``float(str(v).replace(",", "").replace("%", "").strip())`` ——
    ``"1,234"`` → ``1234.0``、``"12.5%"`` → ``12.5``(**不除以 100**)、
    ``"A"`` / ``None`` / ``True`` → ``None``(非数值)。
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# payload 展开(渲染与条件判定共用同一份语义)
# ---------------------------------------------------------------------------


def flatten_scalars(payload: Any) -> list[tuple[str, Any]]:
    """payload → ``[(指标名, 原始值)]``。

    * 嵌套 dict **只展开一层** ``parent.child``(旧 ``renderer.py:73-88``);
    * ``list`` 值**跳过**(交给 ``render.list_section`` 渲染);
    * 再嵌套的 dict/list 也跳过。

    ★ 与旧实现同源:展开规则一旦不一致,"条件里能判、表里看不到"就会成对出现。
    """
    out: list[tuple[str, Any]] = []
    if not isinstance(payload, Mapping):
        return out
    for key, value in payload.items():
        if isinstance(value, Mapping):
            for child_key, child_value in value.items():
                if isinstance(child_value, (Mapping, list)):
                    continue
                out.append((f"{key}.{child_key}", child_value))
        elif isinstance(value, list):
            continue
        else:
            out.append((str(key), value))
    return out


def flatten_map(payload: Any) -> dict[str, Any]:
    """payload → ``{指标名: 原始值}`` 查表(环比/上期取值用)。"""
    return dict(flatten_scalars(payload))


def _numeric_values(payload: Any, fields: Sequence[str] | None = None) -> list[float]:
    """payload 里的**数值**列表(可只取指定字段名)。"""
    wanted = {str(f) for f in fields} if fields else None
    out: list[float] = []
    for name, value in flatten_scalars(payload):
        if wanted is not None and name not in wanted:
            continue
        num = clean_number(value)
        if num is not None:
            out.append(num)
    return out


# ---------------------------------------------------------------------------
# T2D.2 四项桶与窗口索取
# ---------------------------------------------------------------------------


def buckets_for_date(day: date) -> list[str]:
    """日期 → 触发桶(``realtime`` 桶由群内问答触发,**不入此表**)。

    ★ 周一且 1 号 → ``["daily", "monday", "month1"]``(**两桶同时**,旧 ``report_engine.py:76-83``)。
    """
    out = ["daily"]
    if day.weekday() == 0:
        out.append("monday")
    if day.day == 1:
        out.append("month1")
    return out


def _declared_windows(item: Any) -> dict[str, list[str]]:
    raw = item_field(item, "windows", {}) or {}
    if not isinstance(raw, Mapping):
        return {}
    return {str(k): list(v or []) for k, v in raw.items()}


def item_windows_for_date(item: Any, day: date) -> list[str]:
    """某 item 在某日期应发的窗口列表(**空 = 当日不发**)。

    ``daily_except_monday`` 且周一 → **跳过 daily**,让位给周报(svc_daily / svc_weekly)。
    旧 ``report_engine.py:86-96`` 逐字。
    """
    windows = _declared_windows(item)
    out: list[str] = []
    daily = windows.get("daily") or []
    if daily and not (bool(item_field(item, "daily_except_monday", False)) and day.weekday() == 0):
        out.extend(daily)
    for bucket in ("monday", "month1"):
        if bucket in buckets_for_date(day):
            out.extend(windows.get(bucket) or [])
    return out


def collect_windows_for_date(item: Any, day: date) -> tuple[list[str], list[str]]:
    """某 item 在某日期应采集的窗口:``(priority, must)``。

    * ``priority`` —— 每日基础窗口(daily 桶),按「**今日实时/实时 优先于 昨日**」重排;
      采集时按序尝试、**首个有数据即止**(甲方 2026-08-26:有实时用实时,实时无数据回退昨日);
      ``daily_except_monday`` 且周一 → 空(与 :func:`item_windows_for_date` 一致);
    * ``must`` —— 周期窗口(monday/month1 桶),仅在对应日期触发,**全部必采**
      (周一/月 1 推送需要周期数据);
    * 仅配周期桶的模块(无 daily)→ ``priority`` 空、``must`` 只在触发日非空
      (如「服务质量对比」仅周一采「上周」)。

    旧 ``report_engine.py:103-125`` 逐字。
    """
    windows = _declared_windows(item)
    priority: list[str] = []
    must: list[str] = []
    daily = windows.get("daily") or []
    if not (bool(item_field(item, "daily_except_monday", False)) and day.weekday() == 0):
        realtime = [w for w in daily if w in REALTIME_WINDOWS]
        others = [w for w in daily if w not in REALTIME_WINDOWS]
        priority = [*realtime, *others]
    for bucket in ("monday", "month1"):
        if bucket in buckets_for_date(day):
            must.extend(windows.get(bucket) or [])
    return priority, must


def all_item_windows(item: Any) -> list[str]:
    """项声明的**全部**窗口(``daily`` → ``monday`` → ``month1``,**忽略桶**)—— CLI 单跑用。

    ``hoteldata report run svc_weekly`` 在周三也要能跑出东西,所以单跑路径不做桶裁剪。
    """
    windows = _declared_windows(item)
    out: list[str] = []
    for bucket in ("daily", "monday", "month1", "realtime"):
        for window in windows.get(bucket) or []:
            if window not in out:
                out.append(window)
    return out


def windows_for_push(item: Any, day: date, *, ignore_bucket: bool = False) -> list[str]:
    """推送时逐窗口取数的顺序(去重保序)。

    ``priority + must`` —— priority 在前,因为「实时优先昨日」的择优口径在**推送侧**
    同样成立(旧 ``report_push.build_item_content`` 也是"第一个有记录的窗口胜出")。
    ``ignore_bucket=True`` → :func:`all_item_windows`(CLI 单跑)。
    """
    if ignore_bucket:
        return all_item_windows(item)
    priority, must = collect_windows_for_date(item, day)
    out: list[str] = []
    for window in (*priority, *must):
        if window not in out:
            out.append(window)
    return out


# ---------------------------------------------------------------------------
# T2D.3 条件 DSL
# ---------------------------------------------------------------------------


def _dig(obj: Any, path: str) -> Any:
    """点号路径取值,支持 ``[0]`` 下标;取不到返回 ``None``(旧 ``report_engine.py:24-41`` 逐字)。"""
    cur = obj
    for part in str(path).split("."):
        if part.startswith("[") and part.endswith("]"):
            try:
                idx = int(part[1:-1])
            except ValueError:
                return None
            if isinstance(cur, (list, tuple)) and 0 <= idx < len(cur):
                cur = cur[idx]
            else:
                return None
        elif isinstance(cur, Mapping) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _contains(container: Any, actual: Any) -> bool:
    """``in`` 算子:值集合包含判定(★ D9 的语义)。

    * 容器是 list/tuple/set → 先按 ``==`` 找,再按**数值等价**找
      (``"10"`` 命中 ``[10, 3]``,防"窗口字段从 JSON 读出来是字符串"这类假阴性);
    * 容器是 dict → 键包含;
    * 容器是标量 → 退化为**相等**比较(``{"op":"in","value":"通过"}`` 也讲得通);
    * ``value`` 缺失(``None``)→ ``False``(防误发,不是恒真)。
    """
    if container is None:
        return False
    if isinstance(container, (list, tuple, set, frozenset)):
        for candidate in container:
            if candidate == actual:
                return True
        num = clean_number(actual)
        if num is None:
            return False
        return any(clean_number(candidate) == num for candidate in container)
    if isinstance(container, Mapping):
        return actual in container
    return bool(container == actual)


def _compare(op: str, actual: Any, expected: Any) -> bool:
    """按算子比较。数值优先(旧口径:先 ``float()`` 再回退原值),出错 → ``False``。

    ★ 旧实现的回退分支 ``_OPS[op](actual, value)`` **没有兜异常**:``value`` 是 ``None``
    时 ``actual > None`` 在 py3 直接抛 ``TypeError``,把整个推送任务炸掉。这里兜住并
    **失败关闭**(返回 ``False``)—— 与"字段缺失 → False"是同一条防误发原则。
    """
    if op == "in":
        return _contains(expected, actual)
    func = _OP_FUNCS[op]
    if expected is None:
        return False
    left, right = clean_number(actual), clean_number(expected)
    if left is not None and right is not None:
        return bool(func(left, right))
    try:
        return bool(func(actual, expected))
    except TypeError:
        return False


def _spec_fields_value(cond: Mapping[str, Any], spec: Any) -> tuple[list[str] | None, Any]:
    """``any_gt`` / ``any_below_mean`` 的 ``(fields, value)`` 解析(两种写法都吃)。

    * ``{"any_gt": 3, "fields": ["a"]}``
    * ``{"any_gt": {"fields": ["a"], "value": 3}}``
    """
    fields: Any = cond.get("fields")
    value: Any = cond.get("value")
    if isinstance(spec, Mapping):
        fields = spec.get("fields", fields)
        value = spec.get("value", spec.get("ratio", value))
    elif isinstance(spec, (int, float)) and not isinstance(spec, bool):
        value = spec
    names = [str(f) for f in fields] if isinstance(fields, (list, tuple)) else None
    return names, value


def _eval_any_gt(cond: Mapping[str, Any], payload: Any) -> bool:
    """``any_gt``:payload 中**任一**数值字段 > 阈值。"""
    fields, value = _spec_fields_value(cond, cond.get("any_gt"))
    threshold = clean_number(value)
    if threshold is None:
        return False
    return any(v > threshold for v in _numeric_values(payload, fields))


def _eval_any_below_mean(cond: Mapping[str, Any], payload: Any) -> bool:
    """``any_below_mean``:payload 中**任一**数值字段低于这些字段的均值。

    可选 ``ratio``(默认 ``1.0``)= 阈值系数,``0.8`` 即"低于均值的 80%"。
    """
    fields, value = _spec_fields_value(cond, cond.get("any_below_mean"))
    values = _numeric_values(payload, fields)
    if not values:
        return False
    factor = clean_number(value)
    if factor is None:
        factor = 1.0
    threshold = (sum(values) / len(values)) * factor
    return any(v < threshold for v in values)


def _or_items(cond: Mapping[str, Any]) -> list[Any]:
    """``or`` 的条件列表:``{"or": [..]}`` 或 ``{"or": true, "items": [..]}`` 都吃。"""
    raw = cond.get("or")
    if isinstance(raw, (list, tuple)):
        return list(raw)
    items = cond.get("items")
    return list(items) if isinstance(items, (list, tuple)) else []


def eval_condition(cond: Any, payload: Any) -> bool:
    """条件求值(计划书 §5.6 条件 DSL / 附录 D)。

    支持的全部形式::

        {"present": true}                                   # payload 任一值有效
        {"field": "公示内容", "present": true}                # 指定字段有值
        {"field": "投产比", "op": ">", "value": 3}            # 数值比较
        {"field": "lead_days", "op": "in", "value": [10, 3]} # ★ in(D9:可达)
        {"any_gt": 3, "fields": ["a", "b"]}                  # 任一字段 > 3
        {"any_below_mean": true, "fields": ["a", "b"]}        # 任一字段低于均值
        {"or": true, "items": [cond1, cond2]}                # 任一条件成立

    约定:空/``None`` → ``True``(无条件即放行);**字段缺失 → ``False``**(防误发);
    ``0``/``""``/``None``/空容器 = 无值(仅 ``present`` 用)。
    """
    if not cond or not isinstance(cond, Mapping):
        return True

    # ★ 算子既可写成键(``{"any_gt": ...}``),也可写成 ``op`` 的值
    #   (``{"op": "any_gt", "fields": [...], "value": 0}``)。
    #
    #   计划书 §5.6 的 DSL 表用的是 **``op`` 形式**,而 ``alert_rules.json`` 的
    #   D/E 两条规则也用 ``{"op": "any_gt", ...}`` / ``{"op": "or", "items": [...]}``。
    #   只认键形式的后果**与 D9 同一类**:``op`` 形式的 ``any_gt`` 会掉到下面
    #   「没有 field → 返回 True」那一行 —— **恒真**,于是"首页待办 5 个计数任一 > 0"
    #   变成"永远触发"。这类"签名兼容但语义恒真"的 bug 比直接报错危险得多,
    #   所以两种写法都要走同一条分支(V43 正例 + 反例一起验)。
    op_name = cond.get("op")

    if "or" in cond or op_name == "or":
        items = _or_items(cond)
        if not items:
            return False  # 一个候选条件都没有 = 没人通过(不是"无条件放行")
        return any(eval_condition(sub, payload) for sub in items)

    if "present" in cond:
        field = cond.get("field")
        if field:
            return is_present(_dig(payload, field))
        return has_any_value(payload)

    if "any_gt" in cond or op_name == "any_gt":
        return _eval_any_gt(cond, payload)

    if "any_below_mean" in cond or op_name == "any_below_mean":
        return _eval_any_below_mean(cond, payload)

    field = cond.get("field")
    op = op_name
    if not field:
        return True  # 没有字段的比较条件无从判定 → 不阻断(旧口径)
    if op not in _OP_FUNCS and op != "in":
        # 未知算子:schedule 校验会先拦下来;真漏到这里也**不恒真**,而是不通过
        return False
    actual = _dig(payload, field)
    if actual is None:
        return False  # ★ 字段缺失 → False(附录 D)
    return _compare(str(op), actual, cond.get("value"))


# ---------------------------------------------------------------------------
# T2D.5 取值优先 / 聚合 / 环比
# ---------------------------------------------------------------------------


def pick_value(item: Any, payload: Any) -> tuple[dict[str, Any], str]:
    """按 ``item.pick_rule = {"field_from": [...], "default": "pv"}`` 取值。

    * 按 ``field_from`` 顺序在 payload **顶层键**里找(大小写不敏感,保留实际键名);
    * 命中 → ``({实际键名: 值}, "仅<实际键名>口径")`` —— 只保留被选中的键
      (旧 ``report_push.py:27-44`` 逐字:下游渲染只显示选中口径);
    * ``field_from`` 全不命中 → 用 ``default`` 兜一次(计划书 §5.6 列了这个键,
      旧实现读了没用 —— 这里补上,语义仍是"取一个口径");
    * 还没命中 → ``(payload, "")``(原样返回,不加 note)。

    ★ 为什么只查顶层:配置里的 ``pv``/``uv``/``order_convert_rate`` 对应的是**指标
    标签**(如「未来30天搜索热度」的 ``PV``/``UV``),大小写不敏感即命中;这也是旧口径。
    """
    rule = item_field(item, "pick_rule")
    if not isinstance(payload, Mapping) or not isinstance(rule, Mapping):
        return dict(payload) if isinstance(payload, Mapping) else {}, ""
    wanted: list[Any] = list(rule.get("field_from") or [])
    if rule.get("default"):
        wanted.append(rule.get("default"))
    if not wanted:
        return dict(payload), ""
    lower_map = {str(k).lower(): k for k in payload.keys()}
    for candidate in wanted:
        key = lower_map.get(str(candidate).lower())
        if key is not None:
            return {key: payload[key]}, f"仅{key}口径"
    return dict(payload), ""


def aggregate_payloads(payloads: Sequence[Mapping[str, Any]], spec: Mapping[str, Any]) -> dict[str, Any]:
    """把多期 payload 按 ``spec`` 聚合成**扁平** ``{指标: 值}``。

    ``spec = {"days": 7, "sum": [...], "avg": [...], "last": [...]}``;
    ``days`` 等非字段键跳过(旧 ``window_queries.py:152-156``)。

    * 数值采集走 :func:`clean_number`(去逗号 / 去 ``%``);
    * ``sum`` / ``avg`` 取不到数值 → ``None``;``avg`` 保留 2 位(旧口径);
    * ``last`` 取**最后一个非空**原值(不参与数值清洗)。
    """
    per: dict[str, list[float]] = {}
    last_val: dict[str, Any] = {}
    for payload in payloads:
        if not isinstance(payload, Mapping):
            continue
        for field, value in payload.items():
            key = str(field)
            num = clean_number(value)
            if num is not None:
                per.setdefault(key, []).append(num)
            if value is not None and value != "":
                last_val[key] = value

    out: dict[str, Any] = {}
    for tier in AGG_TIERS:
        fields = spec.get(tier)
        if not isinstance(fields, (list, tuple)):
            continue
        for field in fields:
            key = str(field)
            values = per.get(key) or []
            if tier == "sum":
                out[key] = round(sum(values), 2) if values else None
            elif tier == "avg":
                out[key] = round(sum(values) / len(values), 2) if values else None
            else:
                out[key] = last_val.get(key)
    return out


def flatten_daily(agg: Mapping[str, Any] | None, spec: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """``DailyAggregate.as_dict()`` → 扁平 ``{指标: 值}``(**周报的数据源**)。

    覆盖顺序 ``sum`` → ``avg`` → ``last``(与旧 ``window_queries.py:152`` 的写入顺序一致);
    给 ``spec`` 时**只保留 spec 里声明过的字段** —— 甲方指标名是数据契约,不许改名
    (计划书 A3-4),但也不该把聚合里混进来的杂键推到群里。
    """
    if not isinstance(agg, Mapping):
        return {}
    out: dict[str, Any] = {}
    for tier in AGG_TIERS:
        section = agg.get(tier)
        if not isinstance(section, Mapping):
            continue
        declared = spec.get(tier) if isinstance(spec, Mapping) else None
        allowed = {str(f) for f in declared} if isinstance(declared, (list, tuple)) else None
        for field, value in section.items():
            key = str(field)
            if allowed is not None and key not in allowed:
                continue
            out[key] = value
    return out


def aggregate_with_compare(
    current: Mapping[str, Any],
    previous: Mapping[str, Any] | None = None,
    *,
    spec: Mapping[str, Any] | None = None,
    start: date | datetime | str | None = None,
    end: date | datetime | str | None = None,
    days: int | None = None,
) -> dict[str, Any]:
    """★ **D10 修复**:本期聚合 + 上期聚合 → ``payload`` + ``compare``。

    旧 ``report_push.py:154`` 把 ``compare`` **硬编码成 ``None``** → 周报没有上期列、
    没有环比。这里把"上一期"(再往前 ``days`` 天)的聚合一起合成为 :mod:`render`
    认得的 ``compare`` 形状(扁平 ``{指标: 上期值}``),渲染器就会输出四列表。

    返回 ``{"payload", "compare", "range", "samples", "days", "prev_samples"}``:

    * ``payload`` / ``compare`` —— 扁平 ``{指标: 值}``(compare 为空 = 无上期);
    * ``range`` —— ``"起~止"``(旧 ``window_queries.py:157`` 的 ``collect_date`` 口径,
      进渲染标题的「采集日期」);
    * ``samples`` / ``prev_samples`` —— 两期各自的样本天数(0 = 该期无记录)。
    """
    payload = flatten_daily(current, spec)
    compare = flatten_daily(previous, spec) if previous else {}
    span = ""
    if start is not None and end is not None:
        span = f"{_date_str(start)}~{_date_str(end)}"
    return {
        "payload": payload,
        "compare": compare,
        "range": span,
        "samples": int((current or {}).get("samples") or 0),
        "prev_samples": int((previous or {}).get("samples") or 0) if previous else 0,
        "days": int(days if days is not None else (current or {}).get("days") or 0),
    }


def _date_str(value: date | datetime | str) -> str:
    """日期 → ``YYYY-MM-DD``(datetime 只取日期部分;字符串原样)。"""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)
