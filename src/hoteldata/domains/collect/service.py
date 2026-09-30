"""★ 对段2 的查询契约(段1 §1.4)。

**段2/段3 只能通过本模块取数,不许绕过** —— 不允许直接 import ``domains/collect``
内部的提取器实现,也不允许直接读它的表。

===============  ====================================================
契约             方法
===============  ====================================================
结构化数据        PG 表 ``collect_reports`` / ``collect_modules`` /
                 ``alert_portal_columns`` / ``alert_room_states`` /
                 ``review_reviews`` / ``review_materials``
查询服务          ``fetch_module_record()`` / ``build_payload()``(含环比)/
                 ``aggregate_daily()`` / ``today_module_shots()``
基础设施          ``infra/browser.py`` / ``infra/session_store.py`` /
                 ``infra/rate_limit.py`` / ``infra/db.py``
===============  ====================================================

> 段2 的报告引擎要"上期对比列",段1 就把 ``compare`` 一起给出来 ——
> 旧系统周报的 ``compare`` 被硬编码成 ``None``(D10),根因就是取数层没提供环比。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hoteldata.domains.collect.repository import CollectRepository
from hoteldata.infra.models import CollectModule

__all__ = [
    "DailyAggregate",
    "ModuleRecord",
    "ModuleShots",
    "aggregate_daily",
    "build_payload",
    "fetch_module_record",
    "today_module_shots",
]


@dataclass(slots=True)
class ModuleRecord:
    """一个「模块 × 窗口」在数据库里的样子(段2 的输入)。"""

    hotel_id: int
    collect_date: date
    page: str
    module: str
    window: str
    payload: dict[str, Any]
    status: str
    channel: str
    raw_json_path: str | None = None
    error: str | None = None

    @classmethod
    def from_row(cls, row: CollectModule) -> ModuleRecord:
        return cls(
            hotel_id=row.hotel_id,
            collect_date=row.collect_date,
            page=row.page,
            module=row.module,
            window=row.window,
            payload=dict(row.payload_json or {}),
            status=row.status,
            channel=row.channel,
            raw_json_path=row.raw_json_path,
            error=row.error,
        )

    @property
    def indicator_count(self) -> int:
        return len(self.payload)


@dataclass(slots=True)
class ModuleShots:
    """某店某日某页的模块截图索引。"""

    hotel_id: int
    collect_date: date
    page: str
    shots: dict[str, str] = field(default_factory=dict)
    screenshot_path: str | None = None

    def pick(self, *names: str) -> str | None:
        """按 ``screenshot_modules[*].name`` 取图(段2 的取图口径)。"""
        for name in names:
            if name and name in self.shots:
                return self.shots[name]
        return None


# ---------------------------------------------------------------------------
# ① 单条读取
# ---------------------------------------------------------------------------


async def fetch_module_record(
    session: AsyncSession,
    hotel_id: int,
    collect_date: date,
    page: str,
    module: str,
    window: str | None = None,
    *,
    prefer_realtime: bool = False,
) -> ModuleRecord | None:
    """取一个模块记录。

    ``window=None`` 时按"**实时优先昨日**"择优(甲方 2026-08-26 定稿口径,B23):
    有实时用实时,实时无数据再回退昨日 —— 由 ``prefer_realtime`` 控制是否启用。
    """
    repo = CollectRepository(session)
    if window is not None:
        row = await repo.fetch_module(hotel_id, collect_date, page, module, window)
        return ModuleRecord.from_row(row) if row else None

    rows = await repo.list_modules(hotel_id, collect_date, page=page, module=module)
    if not rows:
        return None
    if prefer_realtime:
        order = ("今日实时", "实时", "昨日")
        for win in order:
            for row in rows:
                if row.window == win and row.payload_json:
                    return ModuleRecord.from_row(row)
    # 缺省:返回第一条有数据的
    for row in rows:
        if row.payload_json:
            return ModuleRecord.from_row(row)
    return ModuleRecord.from_row(rows[0])


async def build_payload(
    session: AsyncSession,
    hotel_id: int,
    collect_date: date,
    page: str,
    module: str,
    window: str | None = None,
    *,
    with_compare: bool = True,
) -> dict[str, Any]:
    """构造段2 渲染用的 payload(**含环比**)。

    返回结构::

        {
          "page": ..., "module": ..., "window": ...,
          "status": "ok", "channel": "api",
          "indicators": {"离店间夜": 12, ...},
          "compare": {"离店间夜": {"prev": 10, "delta": 2, "pct": 0.2}},   # 可选
          "prev_date": "2026-09-27"
        }
    """
    record = await fetch_module_record(
        session, hotel_id, collect_date, page, module, window, prefer_realtime=window is None
    )
    if record is None:
        return {
            "page": page,
            "module": module,
            "window": window,
            "status": "no_data",
            "indicators": {},
            "compare": {},
        }

    out: dict[str, Any] = {
        "page": record.page,
        "module": record.module,
        "window": record.window,
        "status": record.status,
        "channel": record.channel,
        "indicators": record.payload,
        "compare": {},
    }
    if not with_compare:
        return out

    prev = await _previous_record(session, hotel_id, page, module, record.window, record.collect_date)
    if prev is not None:
        out["prev_date"] = prev.collect_date.isoformat()
        out["compare"] = _diff(prev.payload, record.payload)
    return out


def _diff(prev: dict[str, Any], cur: dict[str, Any]) -> dict[str, Any]:
    """逐指标环比(只对**两边都是数值**的指标算)。"""
    out: dict[str, Any] = {}
    for key, now in cur.items():
        if key not in prev:
            continue
        before = prev[key]
        if isinstance(now, bool) or isinstance(before, bool):
            continue
        if not isinstance(now, (int, float)) or not isinstance(before, (int, float)):
            continue
        delta = now - before
        pct = (delta / before) if before else None
        out[key] = {"prev": before, "delta": delta, "pct": pct}
    return out


async def _previous_record(
    session: AsyncSession,
    hotel_id: int,
    page: str,
    module: str,
    window: str,
    before: date,
) -> ModuleRecord | None:
    """**同模块同窗口的上一条记录**(旧系统月报的环比口径)。"""
    stmt = (
        select(CollectModule)
        .where(
            CollectModule.hotel_id == hotel_id,
            CollectModule.page == page,
            CollectModule.module == module,
            CollectModule.window == window,
            CollectModule.collect_date < before,
        )
        .order_by(CollectModule.collect_date.desc())
        .limit(1)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    return ModuleRecord.from_row(row) if row else None


# ---------------------------------------------------------------------------
# ② 日聚合(周报用)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class DailyAggregate:
    """N 天聚合结果(周报的 ``aggregate``:sum / avg / last)。"""

    page: str
    module: str
    window: str
    days: int
    sum: dict[str, float] = field(default_factory=dict)
    avg: dict[str, float] = field(default_factory=dict)
    last: dict[str, Any] = field(default_factory=dict)
    samples: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "module": self.module,
            "window": self.window,
            "days": self.days,
            "samples": self.samples,
            "sum": self.sum,
            "avg": self.avg,
            "last": self.last,
        }


async def aggregate_daily(
    session: AsyncSession,
    hotel_id: int,
    end_date: date,
    *,
    page: str,
    module: str,
    window: str = "昨日",
    days: int = 7,
    sum_fields: list[str] | None = None,
    avg_fields: list[str] | None = None,
    last_fields: list[str] | None = None,
) -> DailyAggregate:
    """★ 近 N 天「昨日」记录聚合(**周报的数据源**)。

    旧系统周报这条路径把 ``compare`` 硬编码为 ``None``(D10)→ 周报没有上期列。
    段1 在这里把聚合做出来,段2 直接乘。
    """
    start = end_date - timedelta(days=days - 1)
    stmt = (
        select(CollectModule)
        .where(
            CollectModule.hotel_id == hotel_id,
            CollectModule.page == page,
            CollectModule.module == module,
            CollectModule.window == window,
            CollectModule.collect_date >= start,
            CollectModule.collect_date <= end_date,
        )
        .order_by(CollectModule.collect_date)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    agg = DailyAggregate(page=page, module=module, window=window, days=days, samples=len(rows))
    if not rows:
        return agg

    def _numeric(field_name: str) -> list[float]:
        out: list[float] = []
        for r in rows:
            v = (r.payload_json or {}).get(field_name)
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            out.append(float(v))
        return out

    fields = set(sum_fields or []) | set(avg_fields or []) | set(last_fields or [])
    if not fields:
        # 未指定 → 对所有数值型指标都算 sum/avg,last 取全部
        for r in rows:
            for k, v in (r.payload_json or {}).items():
                if not isinstance(v, bool) and isinstance(v, (int, float)):
                    fields.add(k)
    for fname in sorted(fields):
        values = _numeric(fname)
        if values:
            if sum_fields is None or fname in (sum_fields or []):
                agg.sum[fname] = sum(values)
            if avg_fields is None or fname in (avg_fields or []):
                agg.avg[fname] = round(sum(values) / len(values), 4)
        if last_fields is None or fname in (last_fields or []):
            agg.last[fname] = (rows[-1].payload_json or {}).get(fname)
    return agg


# ---------------------------------------------------------------------------
# ③ 截图索引
# ---------------------------------------------------------------------------


async def today_module_shots(
    session: AsyncSession,
    hotel_id: int,
    collect_date: date,
    page: str | None = None,
) -> list[ModuleShots]:
    """★ 取当日模块截图索引(段2 的取图入口)。

    键名与旧系统一致(``screenshot_modules[*].name``),否则段2 取不到图。
    """
    repo = CollectRepository(session)
    if page is not None:
        shots = await repo.module_screenshots(hotel_id, collect_date, page)
        row = await repo.find_report(hotel_id, collect_date, page)
        return [
            ModuleShots(
                hotel_id=hotel_id,
                collect_date=collect_date,
                page=page,
                shots=shots,
                screenshot_path=row.screenshot_path if row else None,
            )
        ]

    stmt = (
        select(CollectModule.page)
        .where(
            CollectModule.hotel_id == hotel_id,
            CollectModule.collect_date == collect_date,
        )
        .distinct()
    )
    pages = [p for (p,) in (await session.execute(stmt)).all()]
    out: list[ModuleShots] = []
    for pname in pages or []:
        shots = await repo.module_screenshots(hotel_id, collect_date, pname)
        row = await repo.find_report(hotel_id, collect_date, pname)
        out.append(
            ModuleShots(
                hotel_id=hotel_id,
                collect_date=collect_date,
                page=pname,
                shots=shots,
                screenshot_path=row.screenshot_path if row else None,
            )
        )
    return out


async def pick_screenshot(
    session: AsyncSession,
    hotel_id: int,
    collect_date: date,
    names: list[str],
) -> str | None:
    """按候选名列表跨页找图(**找不到返回 None,由调用方决定是否回退整页**)。"""
    for shot in await today_module_shots(session, hotel_id, collect_date):
        found = shot.pick(*names)
        if found:
            return found
    return None
