"""预警规则加载与校验(T2E.1)—— ``config/alert_rules.json`` 的**唯一读入口**。

为什么必须做「白名单字段校验」
==============================

旧系统 ``app/alert_engine.py:33-37`` 有一份 ``RULE_KEYS`` 白名单,但它**只定义不使用**
—— 校验函数 ``validate_rules``(旧 ``:47-79``)只检查"必填字段在不在",
**从不检查"有没有多出来的字段"**。后果是典型的**静默失效**:把 ``condition`` 拼成
``condtion`` 能被必填检查逮到(侥幸),但把 ``condition.op`` 拼错、``condition`` 还在
→ **一路通过**,引擎按"空条件"求值(旧 ``eval_condition(None)`` 返回 ``True``)→ 天天误报。
段2 §6 批次 E / T2E.1 要求「字段名合法性校验(防拼错静默失效)」,所以本模块对每条规则做
**未知字段即报错**,并带上**路径**(``rules[2] (channel_below_mean): 未知字段 'condtion'``)。

规则 id 白名单(6 条 + 1 条退役)
================================

甲方 2026-08-26 口径:``room_closed_today``(当日关房提醒)**已退役**;它出现在配置里
**不是错误**,而是"配置没同步"—— 本模块**警告并跳过**它,其余规则照常工作
(旧 ``RULE_IDS`` 仍含该 id,是历史残留)。合法集合就是那 6 条;**未知 id 一律报错**。

条件求值 + 另外两张配置资产
============================

``any_below_mean`` 也放在本模块 —— 旧 ``alert_engine.py`` 把 ``load_rules`` /
``validate_rules`` 与 ``eval_condition`` / ``_any_below_mean``(``:324-431``)放在一起,
**条件是这个模型自己的字段**;其余五条规则的条件在 ``engine.py`` 就地判定。
``config/alert_shots.json``(``{targets: {rule_id: {url, name}}}``)与
``config/alert_lines.json``(``{hotels: {店名: {high, low}}}``,**只读不改**,
写用 :func:`hoteldata.infra.atomic.atomic_write_json` —— ★ Windows/3.14 上只读句柄
fsync 抛 EBADF,见 ``infra/atomic.py`` 的实测注释)也由本模块读写。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from hoteldata.infra.atomic import atomic_write_json
from hoteldata.settings import Settings, get_settings

__all__ = [
    "ALL_RULE_IDS",
    "ASSET_FILES",
    "COMPARE_OPS",
    "RETIRED_RULE_IDS",
    "RULE_FIELDS",
    "RULE_IDS",
    "AlertRule",
    "AlertRuleSet",
    "AlertRulesError",
    "any_below_mean",
    "asset_path",
    "hotel_lines",
    "lines_path",
    "load_lines",
    "load_rules",
    "prompts_dir",
    "rules_path",
    "set_alert_lines",
    "validate_rules",
]

#: ``alert_rules.json`` 中单条规则的**白名单**字段(逐字继承旧 ``RULE_KEYS``,
#: 外加段2 新增的 ``value_from``)—— 多一个字段就报错,防拼错静默失效。
RULE_FIELDS = frozenset(
    {
        "id",
        "name",
        "type",
        "source",
        "condition",
        "state_key",
        "reset_when_ok",
        "check_times",
        "push",
        "dedup",
        "value_from",
    }
)

#: 合法规则 id(6 条)
RULE_IDS = (
    "room_closed_7d",
    "hot_event_price",
    "channel_below_mean",
    "home_pending",
    "city_heat_remind",
    "price_line_optional",
)

#: 已退役规则 id —— 配置里出现它要**警告并跳过**,不是报错(甲方 2026-08-26 口径)
RETIRED_RULE_IDS = ("room_closed_today",)

#: 全部"认识"的 id(合法 + 退役);之外的 id 一律报错
ALL_RULE_IDS = frozenset(RULE_IDS) | frozenset(RETIRED_RULE_IDS)

#: 比较算子(旧 ``COMPARE_OPS``)
COMPARE_OPS = frozenset({">", ">=", "<", "<=", "==", "!="})

#: 结构化算子(旧 ``validate_rules`` 的算子表)
STRUCT_OPS = frozenset({"any_gt", "any_below_mean", "in", "or", "lead_days", "lte", "present"})

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

#: 单条规则**必填**字段
_REQUIRED = ("id", "name", "type", "source", "condition", "state_key", "check_times", "push")


class AlertRulesError(RuntimeError):
    """规则配置不合法(启动即抛,绝不降级成"空规则集")。"""


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------


def _config_dir(settings: Settings | None = None, config_dir: Path | str | None = None) -> Path:
    """解析 ``config/`` 目录:显式入参 > ``settings.config_dir`` > ``settings.paths.config_dir``。"""
    if config_dir is not None:
        return Path(config_dir)
    s = settings or get_settings()
    direct = getattr(s, "config_dir", None)
    if direct:
        return Path(direct)
    return Path(s.paths.config_dir)


#: ``_config_dir`` 的公开别名(``shots.py`` 解析 ``alert_shots.json`` 路径时复用,不跨模块私下用)
config_dir_of = _config_dir


#: ``config/`` 下的配置资产(路径解析的唯一事实源)
ASSET_FILES: dict[str, str] = {
    "rules": "alert_rules.json",
    "lines": "alert_lines.json",
}


def asset_path(
    name: str, settings: Settings | None = None, *, config_dir: Path | str | None = None
) -> Path:
    """``config/<资产>``;``name="prompts"`` 特指 ``config/prompts/`` 目录。"""
    base = _config_dir(settings, config_dir)
    return base / "prompts" if name == "prompts" else base / ASSET_FILES[name]


def rules_path(settings: Settings | None = None, *, config_dir: Path | str | None = None) -> Path:
    """``config/alert_rules.json`` 路径。"""
    return asset_path("rules", settings, config_dir=config_dir)


def lines_path(settings: Settings | None = None, *, config_dir: Path | str | None = None) -> Path:
    """``config/alert_lines.json`` 路径。"""
    return asset_path("lines", settings, config_dir=config_dir)


def prompts_dir(settings: Settings | None = None, *, config_dir: Path | str | None = None) -> Path:
    """``config/prompts/`` 目录(模板校验与渲染共用)。"""
    return asset_path("prompts", settings, config_dir=config_dir)


# ---------------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class AlertRule:
    """一条预警规则(**已校验**)。

    ``check_times`` 是**名义时刻**(业务语义层 09:00/14:30/19:00);调度层错峰
    (09:04/14:34/19:04)由 ``jobs.py`` 的 cron 承担,引擎用
    :data:`hoteldata.domains.alert.engine.ROOM_SLOT_BY_TIME` 映回名义时刻再比对。
    """

    id: str
    name: str
    type: str
    source: str
    condition: dict[str, Any] = field(default_factory=dict)
    state_key: str = "hotel"
    reset_when_ok: bool = False
    check_times: tuple[str, ...] = ()
    push: dict[str, Any] = field(default_factory=dict)
    dedup: str = "once_per_day"
    value_from: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def template(self) -> str:
        """文案模板名(``config/prompts/<template>.md``,启动即校验存在)。"""
        return str(self.push.get("template") or "alert_generic")

    @property
    def to(self) -> tuple[str, ...]:
        """推送目标:``"group"`` = 管理群全量(旧命名),``"ops"`` = 该店绑定运营群。

        ★ 旧 ``alert_push.recipients_for_trigger``(``:145-149``)**声明了 ``push.to``
        却完全没用**,恒发"管理群 + 运营群"。段2 真正消费它;缺省(空)取两者并集
        —— 与旧行为等价,配得更细时能生效。
        """
        items = tuple(str(x) for x in (self.push.get("to") or []) if str(x).strip())
        return items or ("group", "ops")

    def wants(self, target: str) -> bool:
        """是否要推给 ``target``("group" / "ops")。"""
        return target in self.to

    @property
    def threshold(self) -> float | None:
        """``condition.value``(数值形态;缺失 → ``None``)。"""
        return _num(self.condition.get("value"))

    @property
    def fields(self) -> tuple[str, ...]:
        return tuple(str(x) for x in (self.condition.get("fields") or []))


@dataclass(slots=True, frozen=True)
class AlertRuleSet:
    """一次加载的规则集合(带 mtime,供热加载比对)。"""

    rules: tuple[AlertRule, ...]
    path: Path
    mtime: float
    warnings: tuple[str, ...] = ()

    def get(self, rule_id: str) -> AlertRule | None:
        for rule in self.rules:
            if rule.id == rule_id:
                return rule
        return None

    def for_slot(self, nominal_slot: str) -> list[AlertRule]:
        """名义时刻命中 ``check_times`` 的规则(**slot 必须已映回名义时刻**)。"""
        return [r for r in self.rules if nominal_slot in r.check_times]

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(r.id for r in self.rules)

    def __len__(self) -> int:
        return len(self.rules)


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


def _num(value: Any) -> float | None:
    """宽松取数(``None`` / ``""`` / ``"None"`` → ``None``);逐字继承旧 ``_num``。"""
    try:
        if value is None or value == "" or value == "None":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _bad_ops(cond: dict[str, Any]) -> list[str]:
    """递归收集未知算子(``or`` / ``any_below_mean`` 的子条件也要查)。"""
    bad: list[str] = []
    op = cond.get("op")
    if op is not None and op not in COMPARE_OPS and op not in STRUCT_OPS:
        bad.append(str(op))
    for item in cond.get("items") or []:
        if isinstance(item, dict):
            bad.extend(_bad_ops(item))
    return bad


def validate_rules(
    raw: Any,
    *,
    prompts: Path | None = None,
) -> tuple[list[AlertRule], list[str]]:
    """校验 ``alert_rules.json`` 的原始 dict → ``(合法规则, 警告)``。

    硬错误(全部收集后一次抛 :class:`AlertRulesError`,不用改一个跑一次):未知字段名 /
    缺必填 / id 不在白名单 / 重复 id / 非法算子 / ``check_times`` 非 ``HH:MM`` 或为空 /
    ``push.template`` 在 ``prompts/`` 下不存在。警告:退役规则 id(跳过该条,不报错)。
    """
    errors: list[str] = []
    warnings: list[str] = []
    items = (raw or {}).get("rules") if isinstance(raw, dict) else None
    if not items:
        raise AlertRulesError(f"alert_rules.json 的 rules 为空或非法:{type(raw).__name__}")

    out: list[AlertRule] = []
    seen: set[str] = set()
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(f"rules[{idx}]: 应为 dict,实际 {type(item).__name__}")
            continue
        rid = str(item.get("id") or "?")
        tag = f"rules[{idx}] ({rid})"
        # ① 白名单字段(★ 防拼错静默失效)
        for key in item:
            if key not in RULE_FIELDS:
                errors.append(f"{tag}: 未知字段 {key!r}(合法字段:{sorted(RULE_FIELDS)})")
        # ② 必填
        for key in _REQUIRED:
            if item.get(key) in (None, "", [], {}):
                errors.append(f"{tag}: 缺必填字段 {key!r}")
        # ③ id 白名单
        if rid in RETIRED_RULE_IDS:
            warnings.append(f"{tag}: 规则已退役(甲方 2026-08-26),已跳过该条")
            continue
        if rid not in RULE_IDS:
            errors.append(f"{tag}: 未知规则 id(合法:{list(RULE_IDS)})")
        if rid in seen:
            errors.append(f"{tag}: id 重复")
        seen.add(rid)
        # ④ 条件
        cond = item.get("condition")
        if not isinstance(cond, dict):
            errors.append(f"{tag}: condition 应为 dict,实际 {type(cond).__name__}")
            cond = {}
        for op in _bad_ops(cond):
            errors.append(f"{tag}: 未知算子 {op!r}")
        # ⑤ check_times:非空 + HH:MM
        times = item.get("check_times") or []
        if not isinstance(times, list) or not times:
            errors.append(f"{tag}: check_times 必须是非空列表")
            times = []
        for value in times:
            if not (isinstance(value, str) and _TIME_RE.match(value)):
                errors.append(f"{tag}: check_times 非法时刻 {value!r}(应为 HH:MM)")
        # ⑥ push.template 必须能找到模板文件(启动即校验)
        push = item.get("push")
        if not isinstance(push, dict):
            errors.append(f"{tag}: push 应为 dict,实际 {type(push).__name__}")
            push = {}
        template = str(push.get("template") or "")
        if not template:
            errors.append(f"{tag}: push.template 缺失")
        elif prompts is not None:
            name = template if template.endswith(".md") else f"{template}.md"
            if not (prompts / name).is_file():
                errors.append(f"{tag}: 模板不存在 {prompts / name}")
        out.append(
            AlertRule(
                id=rid,
                name=str(item.get("name") or rid),
                type=str(item.get("type") or ""),
                source=str(item.get("source") or ""),
                condition=dict(cond),
                state_key=str(item.get("state_key") or "hotel"),
                reset_when_ok=bool(item.get("reset_when_ok")),
                check_times=tuple(str(x) for x in times),
                push=dict(push),
                dedup=str(item.get("dedup") or "once_per_day"),
                value_from=dict(item.get("value_from") or {}),
                raw=dict(item),
            )
        )

    if errors:
        raise AlertRulesError("alert_rules.json 校验失败:\n  - " + "\n  - ".join(errors))
    if not out:
        raise AlertRulesError("alert_rules.json 校验后无任何可用规则")
    return out, warnings


# ---------------------------------------------------------------------------
# 条件求值(condition DSL)
# ---------------------------------------------------------------------------
#
# ★ 为什么在 ``rules.py``:旧 ``alert_engine.py`` 把 ``load_rules`` / ``validate_rules``
# 与 ``eval_condition`` / ``_any_below_mean``(``:324-431``)放在同一个文件里 ——
# **条件是这个模型自己的字段**(``AlertRule.condition``),求值器与条件定义同源。
# 段2 只搬 ``any_below_mean`` 这一个算子过来;其余五条规则的条件在 ``engine.py``
# 各自判定函数里就地判定,不做通用 DSL 解释。第二个理由是行数纪律(≤550 行):
# ``engine.py`` 要留给 slot 映射 + 六条规则判定 + 巡检编排。


def any_below_mean(
    fields: Sequence[str], values: Mapping[str, str], cond: Mapping[str, Any]
) -> tuple[bool, list[str]]:
    """``op: "any_below_mean"`` 的求值器(**目前只有 D 规则用**)。

    逐字继承旧 ``alert_engine._any_below_mean``(``:324-361``):有均值且 ``均值 > 0``
    → ``实际 < 均值`` 命中;均值缺失 → **排名兜底** ``rank > total/2``(``min_price``
    无直采均值,R4-1);实际值缺失 → 跳过该字段(**不误报**);命中行带 ``[低于]`` 前缀
    (调用方据此分行),其余行进说明(只取前 3 条防刷屏)。

    ★ D 只传**携程三项**(``visitor_total`` / ``min_price`` / ``ratingall``,
    甲方 2026-08-25 收窄);规则字段名带 ``ctrip.`` 前缀 → 这里剥前缀。
    """
    means = cond.get("means") or {}
    fallbacks = cond.get("rank_fallbacks") or {}
    hits: list[str] = []
    notes: list[str] = []

    def short(path: Any) -> str:
        return str(path).split(".")[-1] if path else ""

    for full in fields:
        key = short(full)
        actual = _num(values.get(key))
        if actual is None:
            notes.append(f"{key} 缺失,跳过")
            continue
        mean = _num(values.get(short(means.get(full)))) if means.get(full) else None
        if mean is not None and mean > 0:
            if actual < mean:
                hits.append(f"[低于] {key}: {actual:g} < 竞争圈均值 {mean:g}")
            else:
                notes.append(f"{key} 正常 {actual:g}≥{mean:g}")
            continue
        fb = fallbacks.get(full) or {}
        rank = _num(values.get(short(fb.get("rank")))) if fb else None
        total = _num(values.get(short(fb.get("total")))) if fb else None
        if rank is not None and total is not None and total > 0:
            if rank > total / 2:
                hits.append(f"[低于] {key}: 排名 {rank:g}/{total:g} 落后(均值缺失,排名兜底)")
            else:
                notes.append(f"{key} 排名 {rank:g}/{total:g} 未落后")
            continue
        notes.append(f"{key} 均值与排名均缺失,跳过")
    return (len(hits) > 0), hits + notes


# ---------------------------------------------------------------------------
# 加载(按 mtime 热加载)
# ---------------------------------------------------------------------------

#: 进程内缓存:``{路径: (mtime, AlertRuleSet)}`` —— 规则是**只读配置**,热加载靠 mtime
_RULE_CACHE: dict[str, tuple[float, AlertRuleSet]] = {}


def load_rules(
    *,
    force: bool = False,
    settings: Settings | None = None,
    config_dir: Path | str | None = None,
) -> AlertRuleSet:
    """加载并校验规则集(**mtime 变了就重载**;``force=True`` 强制重读)。

    文件缺失 / 校验失败 → 抛 :class:`AlertRulesError`
    (启动即失败,绝不静默降级成"空规则集"——那会让预警整体不触发而没人知道)。
    """
    path = rules_path(settings, config_dir=config_dir)
    if not path.is_file():
        raise AlertRulesError(f"缺失预警规则配置:{path}")
    mtime = path.stat().st_mtime
    cached = _RULE_CACHE.get(str(path))
    if cached is not None and not force and cached[0] == mtime:
        return cached[1]

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AlertRulesError(f"预警规则配置无法解析:{path}({exc})") from exc

    rules, warnings = validate_rules(raw, prompts=prompts_dir(settings, config_dir=config_dir))
    rule_set = AlertRuleSet(rules=tuple(rules), path=path, mtime=mtime, warnings=tuple(warnings))
    for msg in warnings:
        logger.warning("预警规则警告: {}", msg)
    if cached is not None:
        logger.info("预警规则已热加载:{} 条(mtime 变化)", len(rule_set))
    _RULE_CACHE[str(path)] = (mtime, rule_set)
    return rule_set


# ---------------------------------------------------------------------------
# 预警线(``config/alert_lines.json``:只读资产 + 原子写)
# ---------------------------------------------------------------------------
#
# ★ ``config/alert_shots.json`` 的读写**不在本模块**,在同域的
# :mod:`hoteldata.domains.alert.shots` —— 那里是它唯一的消费者(附图),
# 放在一起才符合"单文件 ≤550 行"的行数纪律。


def load_lines(
    *,
    settings: Settings | None = None,
    config_dir: Path | str | None = None,
) -> dict[str, Any]:
    """``alert_lines.json`` 原文(缺失/非法 → ``{"hotels": {}}``)。"""
    path = lines_path(settings, config_dir=config_dir)
    if not path.is_file():
        return {"hotels": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("预警线配置不可用:{} ({})", path, exc)
        return {"hotels": {}}
    if not isinstance(data, dict):
        return {"hotels": {}}
    data.setdefault("hotels", {})
    return data


def hotel_lines(
    hotel_name: str, *, settings: Settings | None = None, config_dir: Path | str | None = None
) -> tuple[float | None, float | None]:
    """某店预警线 → ``(high_line, low_line)``(未配置 → ``(None, None)``)。"""
    cfg = (load_lines(settings=settings, config_dir=config_dir).get("hotels") or {}).get(hotel_name) or {}
    return _num(cfg.get("high_line")), _num(cfg.get("low_line"))


def set_alert_lines(
    hotel_name: str,
    high_line: float | None = None,
    low_line: float | None = None,
    *,
    settings: Settings | None = None,
    config_dir: Path | str | None = None,
) -> bool:
    """设置/取消某店预警线(命令「预警线」用)。返回**是否有变更**。

    逐字继承旧 ``alert_push.set_alert_lines``(``:43-62``):两者都传 ``None`` 且该店本来
    就在配置里 → **删除该店条目**并返回 ``True``;本来就不在 → ``False``。
    ★ 写盘走 :func:`~hoteldata.infra.atomic.atomic_write_json`(临时文件 + fsync +
    ``os.replace``),不是 ``write_text``:进程被 kill 时不会留下半个 JSON 把预警线读空。
    """
    path = lines_path(settings, config_dir=config_dir)
    data = load_lines(settings=settings, config_dir=config_dir)
    hotels = data.setdefault("hotels", {})
    if high_line is None and low_line is None:
        if hotel_name not in hotels:
            return False
        del hotels[hotel_name]
    else:
        cur = dict(hotels.get(hotel_name) or {})
        if high_line is not None:
            cur["high_line"] = float(high_line)
        if low_line is not None:
            cur["low_line"] = float(low_line)
        hotels[hotel_name] = {k: v for k, v in cur.items() if v is not None}
    atomic_write_json(path, data)
    logger.info("预警线已更新:店={} 高={} 低={}", hotel_name, high_line, low_line)
    return True
