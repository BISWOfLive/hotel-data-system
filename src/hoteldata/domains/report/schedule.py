"""22 项报告节奏配置 —— ``config/report_schedule.json`` 加载 / 强校验 / 热加载(T2D.1)。

为什么用 pydantic 强校验
======================

旧系统 ``report_engine.validate_schedule``(``report_engine.py:51-73``)是**手写 if 链**:
它只检查 ``mode``/桶名/窗口名三样,``condition`` 只检查"是不是 dict"。
于是配置里把 ``daily_except_monday`` 拼成 ``daily_except_mondayy``、
把 ``images`` 写成 ``image``、把 ``pick_rule`` 写成 ``pick_rules`` —— **全部静默失效**,
表现是"某一项该发没发/该配图没图",而日志里一个字都没有。

这是旧系统反复出现的病(与 D12 同源):**拼错即静默失效**。新实现照
:mod:`hoteldata.domains.collect.rules` 的风格用 ``extra="forbid"`` +
模块级交叉校验,非法配置**直接抛** :class:`ScheduleError`,并且**带路径定位**::

    report_schedule 校验失败: D:\\...\\config\\report_schedule.json
      - items[3] checkout.mode: AI 报告模式已退役 ...
      - items[21] hos_psi.windows.daily[0]: 非法窗口 '实时2' ...

三条口径
========

1. **桶枚举** ``daily`` / ``monday`` / ``month1`` / ``realtime``(计划书 §5.6 四项桶);
2. **窗口枚举单一事实源** = :mod:`hoteldata.domains.collect.windows`(9 个窗口 + 内部键别名),
   本模块**不重复定义**窗口名 —— 否则立刻产生第二个来源(段1 ``windows.py`` 的 docstring 明写);
3. ``mode`` **只允许 ``"data"``**:``ai`` / ``data_ai`` 已退役(计划书 §4.5),
   出现即报错(不是"忽略")。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from hoteldata.domains.collect.windows import WINDOW_KEYS, normalize_window
from hoteldata.domains.report.engine import BUCKET_ENUM, COND_OPS

__all__ = [
    "MODE_ENUM",
    "RETIRED_MODES",
    "ReportItem",
    "Schedule",
    "ScheduleError",
    "ScheduleLoader",
    "get_schedule",
    "reset_cache",
    "validate",
]

#: 唯一在用的模式(计划书 §4.5:``ai`` / ``data_ai`` 已退役)
MODE_ENUM: tuple[str, ...] = ("data",)

#: 已退役模式(出现即报错,给的是**可执行**的提示)
RETIRED_MODES: tuple[str, ...] = ("ai", "data_ai")

DEFAULT_FILENAME = "report_schedule.json"

#: 条件 DSL 的顶层键白名单(拼错 → 报错;具体语义在 ``engine.eval_condition``)
_COND_KEYS: frozenset[str] = frozenset(
    {"present", "field", "op", "value", "or", "items", "any_gt", "any_below_mean", "fields"}
)


class ScheduleError(ValueError):
    """``report_schedule.json`` 结构非法(消息带**精确路径定位**)。"""


# ---------------------------------------------------------------------------
# 模型(``extra="forbid"`` —— 拼错即报错)
# ---------------------------------------------------------------------------


class ReportItem(BaseModel):
    """一项报告节奏(22 项之一)。

    ======================  ==================================================
    字段                     语义
    ======================  ==================================================
    ``id``                  ``module_{id}`` 的审计键(**不许改名**,A2-7)
    ``name``                显示名
    ``page``                采集页(取数用)
    ``windows``             ``{桶: [窗口, ...]}``
    ``mode``                恒 ``"data"``
    ``module``              取数用的模块名(**缺省回退 ``name``**)
    ``condition``           条件 DSL(dict)
    ``images``              图文绑定(类型一 ``{page,module}`` / 类型二 ``{shot,url,...}``)
    ``pick_rule``           ``{field_from: [...], default: ...}``
    ``aggregate``           ``{days, sum, avg, last}``(周报)
    ``on_demand``           不在每日采集清单里的低频项
    ``daily_except_monday`` 周一让位给周报
    ``branch``              ``config/prompts/<name>.md`` 模板分支(**保留机制**)
    ``realtime_ask``        群内问答可命中(``checkout`` / ``booking``)
    ======================  ==================================================
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    page: str = Field(min_length=1)
    windows: dict[str, list[str]]
    mode: str = "data"
    module: str | None = None
    condition: dict[str, Any] | None = None
    images: list[dict[str, Any]] | None = None
    pick_rule: dict[str, Any] | None = None
    aggregate: dict[str, Any] | None = None
    on_demand: bool | None = None
    daily_except_monday: bool | None = None
    branch: dict[str, Any] | None = None
    realtime_ask: bool | None = None
    note: str | None = None

    # ---- 字段级校验 ----

    @field_validator("mode")
    @classmethod
    def _check_mode(cls, value: str) -> str:
        if value in RETIRED_MODES:
            raise ValueError(
                f"mode={value!r} 已退役(AI 报告模式在段2 移除,22 项全部 mode=data;计划书 §4.5)"
            )
        if value not in MODE_ENUM:
            raise ValueError(f"mode 只能是 {list(MODE_ENUM)},实际 {value!r}")
        return value

    @field_validator("windows")
    @classmethod
    def _check_windows(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        """桶名 + 窗口名校验(**消息自己带下标**,否则 9 个窗口里错哪一个看不出来)。"""
        if not value:
            raise ValueError("windows 不能为空(至少一个桶)")
        out: dict[str, list[str]] = {}
        for bucket, windows in value.items():
            if bucket not in BUCKET_ENUM:
                raise ValueError(f"桶 {bucket!r} 非法;合法值为 {list(BUCKET_ENUM)}")
            if not windows:
                raise ValueError(f"桶 {bucket!r} 的窗口列表为空(要么写窗口,要么删掉这个桶)")
            names: list[str] = []
            for idx, window in enumerate(windows):
                if window not in WINDOW_KEYS:
                    raise ValueError(
                        f"windows.{bucket}[{idx}] 非法窗口 {window!r};合法值为 9 个窗口名 "
                        f"{sorted({k for k in WINDOW_KEYS if not k.isascii()})} 或其内部键"
                    )
                name = normalize_window(window)
                if name not in names:
                    names.append(name)
            out[bucket] = names
        return out

    # ---- 便捷视图(engine / service 都读这里) ----

    @property
    def module_name(self) -> str:
        """取数用的模块名(``module`` 缺省回退 ``name``;旧 ``report_push.py:146``)。"""
        return self.module or self.name

    @property
    def aggregate_days(self) -> int:
        """聚合天数(``aggregate.days``,缺省 7)。"""
        if not isinstance(self.aggregate, dict):
            return 7
        try:
            return max(1, int(self.aggregate.get("days") or 7))
        except (TypeError, ValueError):
            return 7

    def aggregate_fields(self, tier: str) -> list[str] | None:
        """``aggregate`` 某一档的字段列表(sum / avg / last)。"""
        if not isinstance(self.aggregate, dict):
            return None
        raw = self.aggregate.get(tier)
        return [str(x) for x in raw] if isinstance(raw, (list, tuple)) else None

    def bucket_windows(self, bucket: str) -> list[str]:
        return list(self.windows.get(bucket) or [])

    def prime_window(self) -> str:
        """首个声明窗口(周报聚合的 ``window`` 参数用;缺省「昨日」)。"""
        for bucket in ("daily", "monday", "month1", "realtime"):
            windows = self.windows.get(bucket) or []
            if windows:
                return windows[0]
        return "昨日"


class _ScheduleRoot(BaseModel):
    """``report_schedule.json`` 顶层。

    ⚠️ 同 ``api_rules.json`` / ``push_rotation.json``:``_comment`` 必须用**别名**,
    pydantic v2 会把下划线开头的名字当私有属性,``extra="forbid"`` 于是把 JSON 里
    真实存在的注释键判成"多余输入"—— **一个纯注释键能把整个配置加载炸掉**。
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    items: list[ReportItem]
    defaults: dict[str, Any] = Field(default_factory=dict)
    comment: Any = Field(default=None, alias="_comment")


# ---------------------------------------------------------------------------
# 已校验的配置集
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Schedule:
    """已校验的报告节奏配置(**不可变**)。"""

    path: Path
    mtime: float
    items: tuple[ReportItem, ...]
    defaults: dict[str, Any] = field(default_factory=dict)
    #: **软问题**(不阻断加载,但 ``report check`` 要显示)
    warnings: tuple[str, ...] = ()

    # ---- 查询 ----

    @property
    def push_time(self) -> str:
        """默认推送时刻(``defaults.push_time``;只作展示,时刻表在 ``jobs.py``)。"""
        return str(self.defaults.get("push_time") or "09:00")

    def ids(self) -> list[str]:
        return [item.id for item in self.items]

    def names(self) -> list[str]:
        return [item.name for item in self.items]

    def by_id(self, item_id: str) -> ReportItem | None:
        for item in self.items:
            if item.id == item_id:
                return item
        return None

    def require(self, item_id: str) -> ReportItem:
        item = self.by_id(item_id)
        if item is None:
            raise ScheduleError(f"未知报告项 {item_id!r};已知:{self.ids()}")
        return item

    def realtime_items(self) -> list[ReportItem]:
        """``realtime_ask == true`` 的项(群内问答可命中)。"""
        return [item for item in self.items if item.realtime_ask]

    def with_images(self) -> list[ReportItem]:
        """配了 ``images`` 的项(V41 图文绑定的适用面)。"""
        return [item for item in self.items if item.images]

    def bucket_items(self, bucket: str) -> list[ReportItem]:
        return [item for item in self.items if item.windows.get(bucket)]

    def stats(self) -> dict[str, Any]:
        return {
            "items": len(self.items),
            "realtime_ask": len(self.realtime_items()),
            "with_images": len(self.with_images()),
            "buckets": {b: len(self.bucket_items(b)) for b in BUCKET_ENUM},
            "aggregate": [item.id for item in self.items if item.aggregate],
        }


# ---------------------------------------------------------------------------
# 路径定位(与 rules.py 同款:错误消息要能直接定位到配置里的那一行)
# ---------------------------------------------------------------------------


def _format_loc(loc: tuple[Any, ...], raw_items: list[Any] | None = None) -> str:
    """``('items', 3, 'windows')`` → ``items[3] svc_weekly.windows``(带项名,便于定位)。

    计划书 T2D.1 的验收形态是"篡改一项非法桶名 → **报错带路径**";
    只给 ``items[3].windows`` 还要人去数第 4 项是谁,所以这里把项名一并带上。
    """
    if raw_items and len(loc) >= 2 and loc[0] == "items" and isinstance(loc[1], int):
        idx = loc[1]
        name = ""
        if 0 <= idx < len(raw_items) and isinstance(raw_items[idx], dict):
            name = str(raw_items[idx].get("id") or raw_items[idx].get("name") or "")
        tail = "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in loc[2:])
        return f"items[{idx}] {name}".rstrip() + tail
    out = ""
    for part in loc:
        out += f"[{part}]" if isinstance(part, int) else (("." if out else "") + str(part))
    return out


def _format_validation_error(exc: ValidationError, source: Path, raw_items: list[Any] | None) -> list[str]:
    lines = [f"report_schedule 校验失败: {source}"]
    for err in exc.errors():
        loc = tuple(err.get("loc") or ())
        lines.append(f"  - {_format_loc(loc, raw_items)}: {err.get('msg', '')}")
    return lines


# ---------------------------------------------------------------------------
# 交叉校验(单字段模型表达不了的那些)
# ---------------------------------------------------------------------------


def _check_condition(cond: Any, where: str) -> list[str]:
    """条件 DSL 结构校验(未知键 / 未知算子 → 报错,**不许**退回恒真)。"""
    problems: list[str] = []
    if cond is None:
        return problems
    if not isinstance(cond, dict):
        return [f"{where}: condition 应为对象,实际 {type(cond).__name__}"]
    unknown = sorted(set(cond) - _COND_KEYS)
    if unknown:
        problems.append(f"{where}.condition: 未知键 {unknown}(拼错会静默失效)")
    if "op" in cond:
        op = cond.get("op")
        if op not in COND_OPS:
            problems.append(
                f"{where}.condition.op: 算子 {op!r} 非法;合法值为 {list(COND_OPS)}"
                f"(★ in 已是一等算子,D9)"
            )
        if "value" not in cond:
            problems.append(f"{where}.condition: op 比较缺少 value")
        if not cond.get("field"):
            problems.append(f"{where}.condition: op 比较缺少 field(字段缺失判定为 False)")
    if "or" in cond:
        items = cond.get("items") if not isinstance(cond.get("or"), list) else cond.get("or")
        if not isinstance(items, list) or not items:
            problems.append(f"{where}.condition: or 需要非空 items 列表")
        else:
            for idx, sub in enumerate(items):
                problems.extend(_check_condition(sub, f"{where}.condition.items[{idx}]"))
    for key in ("any_gt", "any_below_mean"):
        if key in cond and isinstance(cond[key], dict):
            unknown_sub = sorted(set(cond[key]) - {"fields", "value", "ratio"})
            if unknown_sub:
                problems.append(f"{where}.condition.{key}: 未知键 {unknown_sub}")
    return problems


def _check_images(images: Any, where: str) -> list[str]:
    """``images`` 的两种类型校验(V41 的适用面 —— 配了就要有图)。"""
    problems: list[str] = []
    if images is None:
        return problems
    if not isinstance(images, list) or not images:
        return [f"{where}.images: 应为非空数组(不想配图就删掉这个键)"]
    for idx, entry in enumerate(images):
        place = f"{where}.images[{idx}]"
        if not isinstance(entry, dict):
            problems.append(f"{place}: 条目应为对象")
            continue
        if entry.get("shot"):
            if not entry.get("url"):
                problems.append(f"{place}: shot 条目缺少 url")
            if not entry.get("name"):
                problems.append(f"{place}: shot 条目缺少 name(截图落盘与缓存键都用它)")
            continue
        if entry.get("page") and entry.get("module"):
            continue
        problems.append(
            f"{place}: 既不是类型一({{page, module}})也不是类型二({{shot, url, name}});"
            "拼错键名会让图文绑定静默失效(V41)"
        )
    return problems


def _check_pick_rule(rule: Any, where: str) -> list[str]:
    problems: list[str] = []
    if rule is None:
        return problems
    if not isinstance(rule, dict):
        return [f"{where}.pick_rule: 应为对象"]
    unknown = sorted(set(rule) - {"field_from", "default"})
    if unknown:
        problems.append(f"{where}.pick_rule: 未知键 {unknown}")
    field_from = rule.get("field_from")
    if field_from is not None and not isinstance(field_from, list):
        problems.append(f"{where}.pick_rule.field_from: 应为数组")
    return problems


def _check_aggregate(spec: Any, where: str) -> list[str]:
    problems: list[str] = []
    if spec is None:
        return problems
    if not isinstance(spec, dict):
        return [f"{where}.aggregate: 应为对象"]
    unknown = sorted(set(spec) - {"days", "sum", "avg", "last"})
    if unknown:
        problems.append(f"{where}.aggregate: 未知键 {unknown}(只认 days/sum/avg/last)")
    try:
        days = int(spec.get("days") or 7)
        if days < 1:
            problems.append(f"{where}.aggregate.days: 必须 >= 1,实际 {days}")
    except (TypeError, ValueError):
        problems.append(f"{where}.aggregate.days: 不是整数({spec.get('days')!r})")
    for tier in ("sum", "avg", "last"):
        fields = spec.get(tier)
        if fields is None:
            continue
        if not isinstance(fields, list) or not all(isinstance(x, str) for x in fields):
            problems.append(f"{where}.aggregate.{tier}: 应为字符串数组")
    if not any(spec.get(t) for t in ("sum", "avg", "last")):
        problems.append(f"{where}.aggregate: sum/avg/last 至少配一档")
    return problems


def _check_branch(branch: Any, where: str) -> list[str]:
    problems: list[str] = []
    if branch is None:
        return problems
    if not isinstance(branch, dict):
        return [f"{where}.branch: 应为对象"]
    unknown = sorted(set(branch) - {"field", "has", "none", "detail_field"})
    if unknown:
        problems.append(f"{where}.branch: 未知键 {unknown}")
    if not branch.get("field"):
        problems.append(f"{where}.branch: 缺少 field")
    if not (branch.get("has") or branch.get("none")):
        problems.append(f"{where}.branch: has / none 至少配一个模板名")
    return problems


def _cross_check(items: list[ReportItem], source: Path) -> tuple[list[str], list[str]]:
    """跨字段/跨项校验 → ``(problems, warnings)``。

    ``problems`` 非空 = 配置非法(``load()`` 直接抛);
    ``warnings`` 只提示(如"只配了 daily 桶却写了 daily_except_monday")。
    """
    problems: list[str] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for idx, item in enumerate(items):
        where = f"items[{idx}] {item.id}"
        if item.id in seen:
            problems.append(f"{where}: id 重复(会静默覆盖)")
        seen.add(item.id)
        problems.extend(_check_condition(item.condition, where))
        problems.extend(_check_images(item.images, where))
        problems.extend(_check_pick_rule(item.pick_rule, where))
        problems.extend(_check_aggregate(item.aggregate, where))
        problems.extend(_check_branch(item.branch, where))
        if item.daily_except_monday and "daily" not in item.windows:
            warnings.append(f"{where}: 配了 daily_except_monday 但没有 daily 桶(空转)")
        if item.aggregate and not (item.windows.get("monday") or item.windows.get("month1")):
            warnings.append(f"{where}: 配了 aggregate 但只在每日桶发(聚合会算成滚动 7 天)")
        if item.module and item.module != item.name:
            warnings.append(f"{where}: module={item.module!r} 与 name={item.name!r} 不同(取数按 module)")
        if item.on_demand:
            warnings.append(
                f"{where}: on_demand=true —— 段2 无按需采集契约(段1 只提供读接口),"
                "当日无记录时按 no_data 跳过"
            )
        if item.branch:
            warnings.append(f"{where}: branch 模板机制(段2 保留,22 项当前均未使用)")
    if not items:
        problems.append("items: 22 项报告配置为空")
    return problems, warnings


# ---------------------------------------------------------------------------
# 加载 + 热加载
# ---------------------------------------------------------------------------


def _collect_errors(raw: Any, source: Path) -> tuple[list[str], list[str], _ScheduleRoot | None]:
    """解析 + 校验 → ``(errors, warnings, root)``;**不抛异常**(``validate()`` 也用它)。"""
    if not isinstance(raw, dict):
        return [f"report_schedule 顶层应为对象: {source}"], [], None
    raw_items = raw.get("items") if isinstance(raw.get("items"), list) else []
    try:
        root = _ScheduleRoot.model_validate(raw)
    except ValidationError as exc:
        return _format_validation_error(exc, source, raw_items), [], None
    problems, warnings = _cross_check(list(root.items), source)
    return problems, warnings, root


class ScheduleLoader:
    """按 **mtime** 缓存的热加载器(改配置不重启;照 ``rules.RulesLoader``)。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._cached: Schedule | None = None

    def load(self, *, force: bool = False) -> Schedule:
        """加载并校验。非法 → 抛 :class:`ScheduleError`(**带精确路径**)。"""
        if not self.path.exists():
            raise ScheduleError(f"报告节奏配置不存在: {self.path}")
        mtime = self.path.stat().st_mtime
        if not force and self._cached is not None and self._cached.mtime == mtime:
            return self._cached
        schedule = self._parse(mtime)
        self._cached = schedule
        return schedule

    def validate(self) -> list[str]:
        """只校验不改缓存;返回错误列表(空 = 通过)。"""
        if not self.path.exists():
            return [f"报告节奏配置不存在: {self.path}"]
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return [f"report_schedule 不是合法 JSON: {self.path}:{exc.lineno}:{exc.colno} — {exc.msg}"]
        except OSError as exc:
            return [f"report_schedule 读取失败: {self.path} — {exc}"]
        errors, _warnings, _root = _collect_errors(raw, self.path)
        return errors

    def _parse(self, mtime: float) -> Schedule:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ScheduleError(
                f"report_schedule 不是合法 JSON: {self.path}:{exc.lineno}:{exc.colno} — {exc.msg}"
            ) from exc
        except OSError as exc:
            raise ScheduleError(f"report_schedule 读取失败: {self.path} — {exc}") from exc
        errors, warnings, root = _collect_errors(raw, self.path)
        if errors or root is None:
            raise ScheduleError("\n".join(errors))
        for line in warnings:
            # 软问题走日志:既不阻断启动,也不静默(R 类风险"配置空转"要看得见)
            from loguru import logger

            logger.warning("report_schedule 提示: {}", line)
        return Schedule(
            path=self.path,
            mtime=mtime,
            items=tuple(root.items),
            defaults=dict(root.defaults or {}),
            warnings=tuple(warnings),
        )


# ---------------------------------------------------------------------------
# 进程内单例
# ---------------------------------------------------------------------------

_loader: ScheduleLoader | None = None


def _default_path() -> Path:
    from hoteldata.settings import get_settings

    return get_settings().paths.config_dir / DEFAULT_FILENAME


def get_schedule(*, force: bool = False) -> Schedule:
    """取报告节奏配置(按 mtime 热加载)。"""
    global _loader
    if _loader is None:
        _loader = ScheduleLoader(_default_path())
    return _loader.load(force=force)


def validate(path: Path | str | None = None) -> list[str]:
    """模块级校验入口(``hoteldata report check`` / 启动自检):返回错误列表。"""
    target = Path(path) if path is not None else _default_path()
    return ScheduleLoader(target).validate()


def reset_cache() -> None:
    """清空缓存(测试用)。"""
    global _loader
    _loader = None
