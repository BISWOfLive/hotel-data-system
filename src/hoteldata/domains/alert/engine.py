"""预警引擎(T2E.3 六条规则判定 + T2E.4 两层时刻与 slot 映射)。

★★★ 三条语义陷阱(计划书 §5.7 / 附录 D;写错就全盘错)
====================================================

**1 ——"连续 7 天"不在状态机里,在数据里。**
    ``unavailable_days`` 由 :func:`~hoteldata.domains.alert.state.derive_room_availability`
    从段1 ``alert_room_states`` **逐日推导**:从今日起逐日往后看该房型,**可订即断**;
    窗口内某天没有行按"不可订"计(旧口径),但**今日缺数据 → 保守不触发**(``consec=0``);
    本模块只做阈值比较(``>= 7``)。逐字继承旧 ``fetch_room_payload``(``:130-179``)。
    ★ ``alert_states.streak`` **只是展示用计数,不是推送门槛**;改成"``streak >= 7`` 才推"
    即语义漂移(**V48 专测**)。

**2 ——两层时刻结构,必须原样保留。**
    ``check_times`` 是**名义时刻**(业务语义层 09:00/14:30/19:00)→ 调度层错峰 +4 分钟
    (运维层 09:04/14:34/19:04,cron 在 ``jobs.py``)→ :data:`ROOM_SLOT_BY_TIME`
    **映回**名义 slot(匹配层 :func:`nominal_slot`)→ 与 ``check_times`` 字符串匹配
    (引擎层 :func:`check`)。``check("09:04")`` 必须**先映回 ``"09:00"``**;合并两层
    → **slot 匹配立刻失效**(**V49 专测**)。映射逐字继承旧 ``scheduler.py:339``。

**3 ——D 只做携程三项,F 有量纲守卫。**
    D(``channel_below_mean``)只判 ``visitor_total`` / ``min_price`` / ``ratingall``
    (甲方 2026-08-25 收窄;R4-2 去哪儿起价无数据,**不要按旧文档恢复**);
    ``min_price`` 无直采均值(R4-1)→ **排名兜底** ``rank > total/2``。
    F(``city_heat_remind``)阈值 80,但实测热度量纲 0~5 → **量纲守卫**:``0 < heat <= 20``
    视为"未配置",**跳过阈值只按倒计时**(旧 ``:537-549``)。

红线:引擎**只读 + 不发起任何平台写操作**,状态落库与取数辅助都在 :mod:`.state`;段1 只经
``domains.collect.repository``;**不 import 任何提取器实现**(``api`` / ``browser`` /
``channels`` / ``datacenter`` / ``rules`` / ``windows``),也不 import ``domains/report`` /
``domains/review``(硬约束)。

与旧系统的三处**有意差异**:① ``Trigger`` 是 dataclass(旧是裸 dict),字段拼错立刻报错;
② ``room_closed_today`` 已退役,无判定分支(旧 ``:488-495``);③ D 不再重复输出
"去哪儿暂无数据"行(该说明已在模板正文里)。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import select

from hoteldata.domains.alert.render import PENDING_LABELS, RULE_TITLES
from hoteldata.domains.alert.rules import AlertRule, AlertRuleSet, any_below_mean, load_rules
from hoteldata.domains.alert.state import (
    AlertStateStore,
    derive_room_availability,
    derive_unavailable_days,
    heat_payload,
    latest_collect_date,
    num_of,
    portal_rows,
    row_field,
)
from hoteldata.domains.collect.repository import CollectRepository  # 段1 契约(只读)
from hoteldata.infra.models import AlertRoomState, Hotel

__all__ = [
    "DATA_SLOT_BY_TIME",
    "ROOM_SLOT_BY_TIME",
    "AlertCheckResult",
    "Trigger",
    "check",
    "derive_room_availability",
    "derive_unavailable_days",
    "nominal_slot",
]

#: ★ 匹配层:调度错峰时刻 → 名义时刻(逐字继承旧 ``app/scheduler.py:339``)
ROOM_SLOT_BY_TIME: dict[str, str] = {"09:04": "09:00", "14:34": "14:30", "19:04": "19:00"}
#: 数据线错峰 +10 分钟(``scheduler.py:335``)。旧系统给 ``alert.data`` 注册的就是**名义
#: 时刻** ``slot="09:00"``(``:354``),故旧代码不需要这条;段2 的任务注册表若直接把 cron
#: 时刻 ``"09:10"`` 传进来,有它才不会"数据线一条都不触发"。
DATA_SLOT_BY_TIME: dict[str, str] = {"09:10": "09:00"}
#: 关房窗口天数兜底(与 ``alert_rules.json`` 的 ``condition.value`` 同源)
DEFAULT_ROOM_WINDOW_DAYS = 7
#: F 规则数据源 + 量纲守卫上限(旧 ``alert_engine``:252 / :541)
HEAT_MODULE = "每日热度-未来14日热度"
HEAT_WINDOW = "未来14天"
HEAT_SCALE_GUARD = 20.0

#: E 规则 5 个计数(旧 ``fetch_pending_payload``:233-234)
PENDING_KEYS = ("comment_pending", "qa_pending", "audit_pending", "violation_pending", "todo_more")


def nominal_slot(slot: str) -> str:
    """★ 匹配层:调度时刻 → **名义时刻**(``"09:04"`` → ``"09:00"``)。

    两张表都没命中的 slot **原样返回**(旧 ``scheduler.py:342`` 硬兜底 ``"09:00"``,对表外
    时刻反而误判);ops 侧手工传名义时刻也能工作。
    """
    return DATA_SLOT_BY_TIME.get(slot) or ROOM_SLOT_BY_TIME.get(slot, slot)


@dataclass(slots=True)
class Trigger:
    """一条待推送的预警(旧 ``evaluate_rule`` 返回的裸 dict)。"""

    rule_id: str
    hotel_id: int
    hotel_name: str
    entity_key: str
    title: str
    detail_lines: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)
    template: str = "alert_generic"
    #: 推送目标群:由 ``AlertService`` 解析后回填(管理群全量 + 该店绑定运营群;属 T2E.5)
    chatids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        out = {f: getattr(self, f) for f in ("rule_id", "hotel_id", "hotel_name", "entity_key")}
        return out | {
            "title": self.title, "detail_lines": list(self.detail_lines),
            "payload": dict(self.payload), "template": self.template, "chatids": list(self.chatids),
        }


@dataclass(slots=True)
class AlertCheckResult:
    """一次巡检的汇总(``as_dict()`` 直接进 ``job_runs.summary``)。"""

    rules_checked: int = 0
    hotels_checked: int = 0
    triggers: list[Trigger] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)
    slot: str = ""
    nominal_slot: str = ""
    errors: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0

    def bump(self, reason: str, count: int = 1) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + count

    def as_dict(self) -> dict[str, Any]:
        return {
            "rules_checked": self.rules_checked,
            "hotels_checked": self.hotels_checked,
            "triggers": [t.as_dict() for t in self.triggers],
            "trigger_count": len(self.triggers),
            "skipped": dict(self.skipped),
            "slot": self.slot,
            "nominal_slot": self.nominal_slot,
            "errors": list(self.errors),
            "elapsed_s": self.elapsed_s,
        }


# ---------------------------------------------------------------------------
# 六条规则判定(各返回 ``(Trigger 列表, 跳过原因)``)
# ---------------------------------------------------------------------------


def _mk(
    rule: AlertRule, hotel: Hotel, entity_key: str, detail_lines: list[str], payload: dict[str, Any]
) -> Trigger:
    """组装一条 Trigger(旧 ``evaluate_rule.mk``:466-469)。"""
    return Trigger(
        rule_id=rule.id,
        hotel_id=int(hotel.id),
        hotel_name=str(hotel.name),
        entity_key=entity_key,
        title=f"【{RULE_TITLES.get(rule.id, rule.id)}】{hotel.name}",
        detail_lines=detail_lines,
        payload=payload,
        template=rule.template,
    )


async def _rule_room_closed_7d(
    runtime: Any, rule: AlertRule, hotel: Hotel, today: date, repo: CollectRepository, session: Any
) -> tuple[list[Trigger], str | None]:
    """**A ``room_closed_7d``**:``unavailable_days >= 7``(数据层推导,见陷阱 1)。

    ``state_key="hotel:room_type"``;``entity_key = f"{hotel_id}:{room_type_id}"``。
    """
    latest = await latest_collect_date(session, AlertRoomState, int(hotel.id))
    if latest is None:
        return [], "room_states:no_data"
    rows = await repo.list_room_states(int(hotel.id), latest)
    window_days = max(1, int(num_of(rule.condition.get("value")) or DEFAULT_ROOM_WINDOW_DAYS))
    threshold = num_of(rule.condition.get("value")) or float(window_days)
    win_s = today.isoformat()
    win_e = (today + timedelta(days=window_days - 1)).isoformat()
    out: list[Trigger] = []
    for item in derive_room_availability(rows, today, window_days=window_days):
        if item.today_missing:
            logger.debug("房型 {} 今日无房态数据,保守不触发", item.room_name)
            continue
        if item.unavailable_days < threshold:
            continue
        out.append(
            _mk(
                rule,
                hotel,
                f"{int(hotel.id)}:{item.room_type_id}",
                [
                    f"房型 {item.room_name} 已连续 {item.unavailable_days} 天不可订"
                    f"（{win_s} ~ {win_e}）",
                    "建议动作:确认开房设置(房价房态→日历→房态房量),并告知酒店开房",
                ],
                {
                    "window_start": win_s,
                    "window_end": win_e,
                    "room_name": item.room_name,
                    "room_type_id": item.room_type_id,
                    "days": item.unavailable_days,
                },
            )
        )
    return out, None


async def _rule_hot_event_price(
    runtime: Any, rule: AlertRule, hotel: Hotel, today: date, repo: CollectRepository, session: Any
) -> tuple[list[Trigger], str | None]:
    """**C ``hot_event_price``**:``hot_calendar`` 的 ``lead_days ∈ {10, 3}``。

    逐字继承旧 ``:202-226`` + ``:497-508``:按 ``holiName``(即 ``column_name``)分组、
    **首日 = 组内最早日期**;``detail.holiday is False`` 的事件**跳过**(旧 ``:213-214``;
    该键缺失则不跳过,与旧 ``.get()`` 语义一致)。
    """
    rows = await portal_rows(repo, session, int(hotel.id), "hot_calendar")
    if not rows:
        return [], "hot_calendar:no_data"
    grouped: dict[str, list[str]] = {}
    for r in rows:
        name, value = str(row_field(r, "column_name")), str(row_field(r, "value") or "")
        if name and value:
            grouped.setdefault(name, []).append(value)
    detail_of = {str(row_field(r, "column_name")): row_field(r, "detail_json") for r in rows}
    allow = {str(x) for x in (rule.condition.get("value") or [])}
    out: list[Trigger] = []
    for name, dates in grouped.items():
        detail = detail_of.get(name)
        detail = detail if isinstance(detail, Mapping) else {}
        if detail.get("holiday") is False:
            continue
        first = min(dates)
        try:
            lead = (date.fromisoformat(first) - today).days
        except ValueError:
            logger.warning("热点事件 {} 日期非法({}),跳过", name, first)
            continue
        if str(lead) not in allow:
            continue
        out.append(
            _mk(
                rule,
                hotel,
                f"event:{name}:{first}",
                [f"事件「{name}」发生在 {first}(倒计时 {lead} 天)", "建议检查房态和房价是否准确"],
                {
                    "event_name": name,
                    "event_date": first,
                    "end_date": detail.get("end_date") or first,
                    "lead_days": lead,
                },
            )
        )
    return out, None


async def _rule_channel_below_mean(
    runtime: Any, rule: AlertRule, hotel: Hotel, today: date, repo: CollectRepository, session: Any
) -> tuple[list[Trigger], str | None]:
    """**D ``channel_below_mean``**:携程三项低于竞争圈均值(见陷阱 3)。"""
    rows = await portal_rows(repo, session, int(hotel.id), "channel_ctrip")
    if not rows:
        return [], "channel_ctrip:no_data"
    values = {str(row_field(r, "column_name")): str(row_field(r, "value") or "") for r in rows}
    ok, lines = any_below_mean(rule.fields, values, rule.condition)
    if not ok:
        return [], None
    hits = [ln for ln in lines if ln.startswith("[低于]")]
    notes = [ln.replace("[低于]", "", 1) for ln in lines if not ln.startswith("[低于]")]
    return (
        [
            _mk(
                rule,
                hotel,
                "hotel",
                [f"• {ln}" for ln in hits] + notes[:3],
                {"hits": hits, "fields": list(rule.fields)},
            )
        ],
        None,
    )


async def _rule_home_pending(
    runtime: Any, rule: AlertRule, hotel: Hotel, today: date, repo: CollectRepository, session: Any
) -> tuple[list[Trigger], str | None]:
    """**E ``home_pending``**:5 个计数任一 ``> 0``。

    ★ ``audit_pending`` / ``violation_pending`` **由模块记录派生**(旧系统同),派生发生在
    **采集期**(段1 ``PortalExtractor._audit_pending`` / ``_violation_pending`` 读审核记录、
    违约看板模块后**落成列**),引擎只读这 5 列。
    """
    rows = await portal_rows(repo, session, int(hotel.id), "home_pending")
    if not rows:
        return [], "home_pending:no_data"
    values = {str(row_field(r, "column_name")): str(row_field(r, "value") or "") for r in rows}
    counts = {key: int(num_of(values.get(key)) or 0) for key in PENDING_KEYS}
    threshold = num_of(rule.condition.get("value")) or 0.0
    detail = [
        f"• {PENDING_LABELS.get(f, f)} {int(counts.get(f, 0))} 条待处理"
        for f in (rule.fields or PENDING_KEYS)
        if float(counts.get(f, 0)) > threshold
    ]
    return ([_mk(rule, hotel, "hotel", detail, dict(counts))] if detail else []), None


async def _rule_city_heat_remind(
    runtime: Any, rule: AlertRule, hotel: Hotel, today: date, repo: CollectRepository, session: Any
) -> tuple[list[Trigger], str | None]:
    """**F ``city_heat_remind``**:``lead_days ∈ {10, 3}`` + **量纲守卫**(见陷阱 3)。

    热度阈值取 ``value_from.heat_threshold``(配置 80);实测热度量纲 0~5
    → ``0 < heat <= 20`` 时**跳过阈值判定**,只按倒计时(旧 ``:537-549``)。
    """
    payload = await heat_payload(repo, int(hotel.id), HEAT_MODULE, HEAT_WINDOW)
    if not payload:
        return [], "market_heat:no_data"
    start = str(payload.get("热点开始日期") or "")
    if not start:
        return [], "market_heat:no_start"
    try:
        lead = max((date.fromisoformat(start) - today).days, 0)
    except ValueError:
        logger.warning("热度模块开始日期非法({}),跳过", start)
        return [], "market_heat:bad_start"
    if str(lead) not in {str(x) for x in (rule.condition.get("value") or [])}:
        return [], None
    heat = num_of(payload.get("当日城市热度值"))
    threshold = num_of((rule.value_from or {}).get("heat_threshold"))
    skip_threshold = bool(threshold is not None and heat is not None and 0 < heat <= HEAT_SCALE_GUARD)
    if skip_threshold:
        # ★ 量纲守卫:热度落在 0~5 这类"未配置量纲"区间,阈值 80 永远不可能命中
        logger.info("热度值量纲异常({}),跳过阈值过滤(规则 {})", heat, rule.id)
    elif threshold is not None and heat is not None and heat < threshold:
        return [], None
    hot_name = str(payload.get("热点名称") or "")
    return (
        [
            _mk(
                rule,
                hotel,
                f"city:{hot_name}:{start}",
                [
                    f"「{hot_name}」进入 {start}(倒计时 {lead} 天)",
                    f"热度{payload.get('热度等级')}({heat})",
                ],
                {
                    # ★ 城市名取自 ``core_hotels.city``(旧实现在模块 payload 里找 ``city``,
                    #   那个键不存在 → 模板 ``{city}`` 被清成空串;这里补上真实城市)
                    "city": str(getattr(hotel, "city", "") or ""),
                    "hot_name": hot_name,
                    "start": start,
                    "end": str(payload.get("热点结束日期") or ""),
                    "lead_days": lead,
                    "heat_value": heat,
                    "heat_level": payload.get("热度等级"),
                    "threshold_skipped": skip_threshold,
                },
            )
        ],
        None,
    )


async def _rule_price_line_optional(
    runtime: Any, rule: AlertRule, hotel: Hotel, today: date, repo: CollectRepository, session: Any
) -> tuple[list[Trigger], str | None]:
    """**可选线 ``price_line_optional``**:``price >= high`` 或 ``price <= low``。

    ★ 段2 **没有比价库**(比价是段3)→ 数据源缺失时**明确跳过并记原因**,
    **绝不报错、绝不误报**(旧系统读 ``hotel_prices.db`` 影子库,新架构不留它)。

    钩子:装配 ``runtime.price_source`` 即可启用 —— 可以是
    ``async (runtime, hotel, today) -> {"price": float, "source": str} | None`` 的可调用对象,
    也可以直接是这样一个 dict(段3 接入口,计划书 §1.4)。
    """
    from hoteldata.domains.alert.rules import hotel_lines

    high, low = hotel_lines(str(hotel.name), settings=getattr(runtime, "settings", None))
    if high is None and low is None:
        return [], "price_line:no_lines"
    source = getattr(runtime, "price_source", None)
    if source is None:
        return [], "price_line:no_source"
    data = source(runtime, hotel, today) if callable(source) else source
    if hasattr(data, "__await__"):
        data = await data
    price = num_of((data or {}).get("price"))
    if price is None:
        return [], "price_line:no_price"
    side = "高" if (high is not None and price >= high) else ""
    if not side and low is not None and price <= low:
        side = "低"
    if not side:
        return [], None
    return (
        [
            _mk(
                rule,
                hotel,
                "hotel",
                [
                    f"当前最低卖价 {price:g} 元{side}于预警线"
                    f"(高 {high if high is not None else '—'} / 低 {low if low is not None else '—'})"
                ],
                {
                    "price": price,
                    "high_line": high,
                    "low_line": low,
                    "side": side,
                    "source": str((data or {}).get("source") or ""),
                },
            )
        ],
        None,
    )


#: 规则 id → 判定函数(**id 与函数一一对应,不做通用 DSL 解释**;白名单保证不会缺)
_EVALUATORS: dict[str, Any] = {
    "room_closed_7d": _rule_room_closed_7d,
    "hot_event_price": _rule_hot_event_price,
    "channel_below_mean": _rule_channel_below_mean,
    "home_pending": _rule_home_pending,
    "city_heat_remind": _rule_city_heat_remind,
    "price_line_optional": _rule_price_line_optional,
}


# ---------------------------------------------------------------------------
# 巡检入口
# ---------------------------------------------------------------------------


async def _active_hotels(runtime: Any) -> list[Hotel]:
    """活跃酒店(逐字继承旧 ``list_hotels(status='active')``)。"""
    async with runtime.db.session() as s:
        stmt = select(Hotel).where(Hotel.status == "active").order_by(Hotel.id)
        return list((await s.execute(stmt)).scalars().all())


async def check(
    runtime: Any,
    slot: str,
    *,
    today: date | None = None,
    dry_run: bool = False,
    rule_id: str | None = None,
    force: bool = False,
) -> AlertCheckResult:
    """巡检一个时刻:``slot`` **映回名义时刻** → 匹配 ``check_times`` → 逐规则×逐店判定。

    * ``slot`` 是**调度层时刻**(``"09:04"`` 等,可来自 cron,也可直接传名义时刻);
    * ``dry_run=True``(**命令「预警测试」**):**不写状态、不写日志、不发送**,
      只返回 triggers(``should_push`` 照常判定,但只读);
    * ``force=True`` 绕过当日去重;``rule_id`` 只跑某一条规则;
    * 单店/单规则异常**不阻断**其余判定:进 ``errors`` 并计 ``skipped['error']``。
    """
    t0 = time.monotonic()
    day = today or date.today()
    nominal = nominal_slot(slot)
    result = AlertCheckResult(slot=slot, nominal_slot=nominal)

    rule_set: AlertRuleSet = load_rules()
    active = rule_set.for_slot(nominal)
    if rule_id:
        active = [r for r in active if r.id == rule_id]
    if not active:
        logger.info(
            "预警巡检:slot={}(名义 {})无匹配规则(已声明时刻 {})", slot, nominal,
            sorted({t for r in rule_set.rules for t in r.check_times}),
        )
        result.elapsed_s = round(time.monotonic() - t0, 2)
        return result

    hotels = await _active_hotels(runtime)
    result.rules_checked = len(active)
    result.hotels_checked = len(hotels)
    store = AlertStateStore(runtime.db)

    for rule in active:
        evaluator = _EVALUATORS.get(rule.id)
        if evaluator is None:  # pragma: no cover - 规则加载已做白名单校验
            logger.warning("规则 {} 无判定实现,跳过", rule.id)
            result.bump(f"no_evaluator:{rule.id}")
            continue
        for hotel in hotels:
            try:
                if await store.is_ignored(int(hotel.id), rule.id, today=day):
                    result.bump("ignored")
                    continue
                async with runtime.db.session() as s:
                    repo = CollectRepository(s)
                    triggers, reason = await evaluator(runtime, rule, hotel, day, repo, s)
                if reason:
                    result.bump(reason)
                elif not triggers:
                    result.bump("not_triggered")
                fired: set[str] = set()
                for tr in triggers:
                    if not await store.should_push(
                        rule.id,
                        int(hotel.id),
                        tr.entity_key,
                        today=day,
                        dedup=rule.dedup,
                        force=force,
                    ):
                        result.bump("deduped")
                        continue
                    fired.add(tr.entity_key)
                    result.triggers.append(tr)
                if dry_run:
                    continue
                for key in fired:
                    await store.mark_triggered(
                        rule.id, int(hotel.id), key, today=day, dedup=rule.dedup, force=True
                    )
                if rule.reset_when_ok:
                    # ★ reset_when_ok:本店**没再触发**的实体清零。传的是"本次产生了触发的
                    #   实体集合"(含被当日去重挡下的),不是"本次新推的集合" —— 否则 14:30
                    #   那一轮会把 09:00 已触发、条件仍在的实体误标成 ok。旧实现只在
                    #   "整条规则 0 触发"时清零,多房型场景会漏掉已恢复的房型。
                    await store.reset_when_ok(
                        rule.id, int(hotel.id), {t.entity_key for t in triggers}, today=day
                    )
            except Exception as exc:  # noqa: BLE001 - 单店异常不阻断(旧 check_tier 同款)
                logger.exception("预警评估失败:规则={} 店={}", rule.id, hotel.name)
                result.errors.append(f"{rule.id}|{hotel.name}: {exc}")
                result.bump("error")

    result.elapsed_s = round(time.monotonic() - t0, 2)
    logger.info(
        "预警巡检完成:slot={}(名义 {}) 规则 {} 酒店 {} 触发 {} 跳过 {} 耗时 {}s",
        slot, nominal, result.rules_checked, result.hotels_checked,
        len(result.triggers), result.skipped, result.elapsed_s,
    )
    return result
