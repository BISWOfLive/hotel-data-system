"""轮换裁剪(T3.6)—— 每日从 21 项清单取 5 项。

**A8 遗产算法**(旧 ``app/rotation.py:60-66``)::

    n      = min(max(daily_count, 1), len(items))
    offset = day.toordinal() % len(items)
    取 items[offset], items[offset+1], ... 循环(连续 n 条)

→ 跨天滚动**无重无漏**,周期 = 清单长度(21 天,**不是**"4 天一轮")。

**采集 + 截图 + 推送共用同一份清单**(``config/push_rotation.json``)。

★ 段1 修掉旧系统的坑(T3.6 / V9)
--------------------------------
旧系统清单里有 2 项**不在** ``api_rules.sub_modules[*].name`` 中(「市场分析」「预警-热点日历」),
按"子模块名"一套规则查就会判 ``unknown`` 并**静默跳过**。

真实原因是**清单混装了三种语义**,旧实现却只用一套查法:

  * ``module_screenshot`` 项的 ``name`` = **子模块名**(17 项,全部对得上);
  * ``fullpage`` 项(「市场分析」)的 ``name`` = **页名**(``pages`` 里有「市场分析」);
  * ``预警*`` 项(「预警-热点日历」)走**预警三源采集**(旧 ``scheduler.py:72`` 前缀特判),
    **不属于模块采集**。

→ 新实现做 **type-aware 校验**:对不上就**直接报错**,不再静默跳过。

> ⚠️ 顺带修正计划书 D11 的另一半:旧清单「每天实际只采 3 项」**不成立** ——
> 枚举 21 个 offset,有效采集项数为 ``{2项:3天, 3项:2天, 4项:2天, 5项:14天}``,
> 只采 3 项仅 **2/21 天**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from hoteldata.domains.collect.rules import ApiRules, ApiRulesError, get_api_rules
from hoteldata.settings import get_settings

__all__ = [
    "RotationError",
    "RotationItem",
    "RotationPlan",
    "RotationRegistry",
    "get_rotation",
]

ItemType = Literal["module_screenshot", "fullpage"]


class RotationError(ValueError):
    """轮换清单非法(消息带**精确路径定位**)。"""


class RotationItem(BaseModel):
    """清单一项。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    page: str = Field(min_length=1)
    type: ItemType
    alias: list[str] = Field(default_factory=list)
    collect: bool | None = None
    url: str | None = None
    shot_name: str | None = None

    @model_validator(mode="after")
    def _check_fullpage(self) -> RotationItem:
        if self.type == "fullpage" and not self.url:
            raise ValueError("type=fullpage 的项必须提供 url(整页截图要直达该 URL)")
        return self


class _RotationRoot(BaseModel):
    """``push_rotation.json`` 顶层。

    ⚠️ 同 :class:`~hoteldata.domains.collect.rules.ApiRules`:``_comment`` 必须用
    **别名**而不是下划线字段名 —— pydantic v2 会把下划线名字当私有属性,
    导致 ``extra="forbid"`` 拒绝 JSON 里真实存在的注释键。
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    daily_count: int = Field(ge=1)
    rotation: str | None = None
    comment: Any = Field(default=None, alias="_comment")
    items: list[RotationItem]


def _format_loc(loc: tuple[Any, ...]) -> str:
    out = ""
    for part in loc:
        out += f"[{part}]" if isinstance(part, int) else (("." if out else "") + str(part))
    return out


@dataclass(frozen=True, slots=True)
class RotationPlan:
    """某一天的轮换结果。"""

    day: date
    offset: int
    items: tuple[RotationItem, ...]
    total_items: int
    #: ``fixed_daily=True`` 的模块(**不进轮换**,每日固定采;来自 ``api_rules``)
    fixed_daily: tuple[str, ...] = ()
    #: 预告:每个模块名对应它落在清单的第几项(便于日志)
    index_note: dict[str, int] = field(default_factory=dict)

    @property
    def names(self) -> list[str]:
        return [i.name for i in self.items]

    @property
    def module_names(self) -> list[str]:
        """只属于**模块采集**的项(排除 ``预警*`` 与纯整页项)。"""
        return [i.name for i in self.items if i.type == "module_screenshot"]

    @property
    def fullpage_names(self) -> list[str]:
        return [i.name for i in self.items if i.type == "fullpage"]

    @property
    def alert_names(self) -> list[str]:
        return [i.name for i in self.items if i.name.startswith("预警")]

    def effective_modules(self) -> list[str]:
        """当日实际要采的模块 = 轮换项 + ``fixed_daily``(去重,**保持顺序**)。"""
        out: list[str] = []
        for name in [*self.module_names, *self.fixed_daily]:
            if name not in out:
                out.append(name)
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.day.isoformat(),
            "offset": self.offset,
            "total_items": self.total_items,
            "daily_count": len(self.items),
            "items": [
                {"name": i.name, "page": i.page, "type": i.type, "alias": list(i.alias)} for i in self.items
            ],
            "fixed_daily": list(self.fixed_daily),
            "effective_modules": self.effective_modules(),
        }


class RotationRegistry:
    """清单加载 + **type-aware 一致性校验** + 轮换算法。"""

    def __init__(self, path: Path | str, rules: ApiRules | None = None) -> None:
        self.path = Path(path)
        self._rules = rules
        self._cached: tuple[float, _RotationRoot] | None = None

    # ------------------------------------------------------------------

    @property
    def rules(self) -> ApiRules:
        if self._rules is None:
            self._rules = get_api_rules()
        return self._rules

    def load(self, *, force: bool = False) -> _RotationRoot:
        """按 mtime 热加载。"""
        if not self.path.exists():
            raise RotationError(f"轮换清单不存在: {self.path}")
        mtime = self.path.stat().st_mtime
        if not force and self._cached is not None and self._cached[0] == mtime:
            return self._cached[1]
        import json

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RotationError(
                f"轮换清单不是合法 JSON: {self.path}:{exc.lineno}:{exc.colno} — {exc.msg}"
            ) from exc
        try:
            root = _RotationRoot.model_validate(raw)
        except ValidationError as exc:
            lines = [f"轮换清单校验失败: {self.path}"]
            for err in exc.errors():
                lines.append(f"  - {_format_loc(tuple(err.get('loc') or ()))}: {err.get('msg')}")
            raise RotationError("\n".join(lines)) from exc
        if not root.items:
            raise RotationError(f"轮换清单 items 为空: {self.path}")
        self._cached = (mtime, root)
        return root

    # ------------------------------------------------------------------

    def validate_against_rules(self) -> list[str]:
        """★ 清单与规则的一致性校验(T3.6)。

        返回"有意的例外"说明列表(例如 ``预警*`` 归预警三源);
        发现**无法归类**的项则抛 :class:`RotationError`。
        """
        root = self.load()
        notes: list[str] = []
        problems: list[str] = []
        seen: set[str] = set()
        for idx, item in enumerate(root.items):
            loc = f"items[{idx}]({item.name})"
            if item.name in seen:
                problems.append(f"{loc}: 名称重复")
            seen.add(item.name)
            try:
                kind = self.rules.classify_rotation_name(item.name, item.page, item.type)
            except ApiRulesError as exc:
                problems.append(f"{loc}: {exc}")
                continue
            if kind == "alert":
                notes.append(f"{loc}: 归预警三源采集(不参与模块采集)")
            elif kind == "page":
                notes.append(f"{loc}: 整页项(name 是页名,不是子模块名)")
            # 交叉核对 page 字段
            if kind == "module":
                located = self.rules.locate_module(item.name)
                if located and located[0] != item.page:
                    problems.append(f"{loc}: page 声明为 {item.page!r},但该模块实际属页 {located[0]!r}")
        if problems:
            raise RotationError(
                "轮换清单与 api_rules 不一致(旧系统会静默跳过,新实现直接报错):\n"
                + "\n".join(f"  - {p}" for p in problems)
            )
        return notes

    # ------------------------------------------------------------------

    def pick(self, day: date, *, daily_count: int | None = None) -> RotationPlan:
        """★ 取某日的轮换项(连续 n 条,循环)。"""
        root = self.load()
        items = root.items
        total = len(items)
        n = min(max(daily_count or root.daily_count, 1), total)
        offset = day.toordinal() % total
        picked = tuple(items[(offset + i) % total] for i in range(n))
        fixed = tuple(self._fixed_daily_modules())
        return RotationPlan(
            day=day,
            offset=offset,
            items=picked,
            total_items=total,
            fixed_daily=fixed,
            index_note={i.name: (offset + k) % total for k, i in enumerate(picked)},
        )

    def _fixed_daily_modules(self) -> list[str]:
        """``fixed_daily=True`` 的**模块名**(★ 注意 ``all_sub_modules()`` 返回
        ``(page, sub)`` 二元组 —— 直接解包会拿到**页名**而不是模块名)。"""
        return [sub.name for _page, sub in self.rules.all_sub_modules() if sub.fixed_daily]

    def fixed_daily_modules(self) -> list[str]:
        """``fixed_daily=True`` 的模块(每日常采,不进轮换)。"""
        return self._fixed_daily_modules()

    def preview(self, start: date, days: int = 21) -> list[RotationPlan]:
        """连续 N 天预览(V9 的"无重无漏"验证用)。"""
        from datetime import timedelta

        return [self.pick(start + timedelta(days=i)) for i in range(days)]

    def verify_no_gap(self, start: date, days: int = 21) -> dict[str, Any]:
        """验证连续 ``days`` 天**无重无漏**。

        V9 的判定:``days == total_items`` 时,每一天的 5 项合并后,
        **每一项恰好被覆盖** ``days * daily_count / total_items`` 次;
        且任意一天的 5 项**互不重复**。
        """
        root = self.load()
        plans = self.preview(start, days)
        counts: dict[str, int] = {}
        dup_days: list[str] = []
        for plan in plans:
            names = plan.names
            if len(set(names)) != len(names):
                dup_days.append(plan.day.isoformat())
            for name in names:
                counts[name] = counts.get(name, 0) + 1
        all_names = [i.name for i in root.items]
        missing = [n for n in all_names if counts.get(n, 0) == 0]
        expected_each = days * root.daily_count // len(all_names)
        uneven = {n: c for n, c in counts.items() if c != expected_each}
        return {
            "days": days,
            "daily_count": root.daily_count,
            "total_items": len(all_names),
            "per_day_counts": [len(p.items) for p in plans],
            "coverage": counts,
            "expected_each": expected_each,
            "missing": missing,
            "uneven": uneven,
            "dup_within_day": dup_days,
            "ok": not missing and not uneven and not dup_days,
        }


# ---------------------------------------------------------------------------
# 进程内单例
# ---------------------------------------------------------------------------

_registry: RotationRegistry | None = None


def get_rotation(*, force: bool = False) -> RotationRegistry:
    global _registry
    if _registry is None:
        cfg_dir = get_settings().paths.config_dir
        _registry = RotationRegistry(cfg_dir / "push_rotation.json")
    if force:
        _registry.load(force=True)
    return _registry


def reset_cache() -> None:
    global _registry
    _registry = None
