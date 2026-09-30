"""预警状态机 + 预警数据面(T2E.2,含 T2E.3 的取数侧与"连续 N 天"推导)。

两个职责:

① **``alert_states`` 的唯一读写入口**(:class:`AlertStateStore`):当日去重 /
   恢复清零 / 忽略 / UPSERT 幂等;
② **段1 数据的只读取数辅助**(``num_of`` / ``row_field`` / ``column_values`` /
   ``latest_collect_date`` / ``portal_rows`` / ``heat_payload``)与
   **"连续 N 天不可订"的数据层推导**(:func:`derive_room_availability`)。

为什么合成一个模块(而不是都塞进 ``engine.py``)
------------------------------------------------

① 本模块是预警域**唯一持有 ``Database`` 的模块** —— ``rules.py`` 刻意保持纯 JSON
   配置 IO(启动校验规则时数据库可能还没起来),取数口径只此一处;
② 单文件行数纪律(项目硬性 ≤550 行):``engine.py`` 已有 slot 映射 + 六条规则判定 +
   巡检编排,把**与"判定语义"无关的取数**搬出来,engine 才能只剩判定;
③ 主题自洽:**"连续 7 天"这件事本来就"不在状态机里,在数据里"** ——
   推导函数与"状态"同处一层,但**与 ``streak`` 没有任何关系**。

★★ 这个模块里最容易写错的一件事
================================

**"连续 7 天"不在状态机里,在数据里**(计划书 §5.7 陷阱 1 / 附录 D)。

``alert_states.streak`` 是**展示用计数**:它记录"这条实体连续多少天触发过",
可以拿来在文案里写"已连续第 N 天提醒"。它**不是推送门槛** ——
真正的"连续 7 天不可订"由 :func:`derive_room_availability` 从
``alert_room_states`` **逐日推导**(可订即断;今日缺数据保守不触发)。

若把这里写成 "``streak >= 7`` 才推",语义直接漂移:
一个今天刚关房、但关了 30 天没被采集到的房型会永远不报;**V48 专测这条**。

四个机制(与计划书 §5.7 的表格一一对应)
========================================

======================  ==========================================================
机制                     规则
======================  ==========================================================
当日去重                 ``last_trigger_date == today`` 且 ``dedup='once_per_day'``
                         → 不再推(``force=True`` 可绕,给命令「预警测试」用)
恢复清零                 ``reset_when_ok``:该规则当天**无触发**时,把该店所有
                         ``status='triggered'`` 的实体清零
忽略                     「忽略此店 X N」→ 对该店**每条规则** + ``__all__`` 写
                         ``entity_key='__hotel__'`` / ``ignored_until`` / ``ignored``
UPSERT 幂等              ``UNIQUE(rule_id, hotel_id, entity_key)`` + ``ON CONFLICT
                         DO UPDATE`` —— 重跑不产生第二行(旧系统
                         ``SELECT-then-INSERT`` 在并发下必漏)
======================  ==========================================================

为什么忽略要**每条规则各写一行 + 一行 ``__all__``**(旧 ``alert_engine.py:610-616``)
----------------------------------------------------------------------------------------
只写 ``__all__`` 一行:规则清单变了(新增规则)后,新规则的查询可能漏掉兜底行;
只写每条规则:`is_ignored` 得遍历全部规则。两行都写 = **规则清单变化也不会漏**,
这也是 ``infra/models/alert.py`` 把 ``ALERT_ALL_RULE`` 单独导出成常量的原因。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from hoteldata.domains.collect.repository import CollectRepository  # 段1 契约(只读)
from hoteldata.infra.db import Database
from hoteldata.infra.models.alert import (
    ALERT_ALL_RULE,
    ALERT_HOTEL_ENTITY,
    AlertState,
)
from hoteldata.infra.models.extractors import AlertPortalColumn

__all__ = [
    "AlertStateStore",
    "RoomAvailability",
    "column_values",
    "derive_room_availability",
    "derive_unavailable_days",
    "heat_payload",
    "latest_collect_date",
    "num_of",
    "portal_rows",
    "row_field",
]

#: 允许通过 :meth:`AlertStateStore.upsert` 写入的列(白名单,防手滑写错列名)
_STATE_FIELDS = frozenset(
    {"streak", "last_trigger_date", "last_ok_date", "status", "ignored_until"}
)


class AlertStateStore:
    """``alert_states`` 的状态机。持 :class:`~hoteldata.infra.db.Database`(不是 session)。

    与 ``push/audit.py`` 同一条理由:巡检是**后台任务**,每写一行自己开短会话,
    不能把长事务拖在手里(预警巡检要遍历全部酒店,事务一长就顶住连接池)。
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    # ==================================================================
    # 读
    # ==================================================================

    async def get(self, rule_id: str, hotel_id: int, entity_key: str) -> AlertState | None:
        """取一条状态(不存在 → ``None``)。"""
        async with self.db.session() as s:
            return await self.get_in(s, rule_id, hotel_id, entity_key)

    async def get_in(
        self, session: AsyncSession, rule_id: str, hotel_id: int, entity_key: str
    ) -> AlertState | None:
        stmt = select(AlertState).where(
            AlertState.rule_id == rule_id,
            AlertState.hotel_id == hotel_id,
            AlertState.entity_key == entity_key,
        )
        return (await session.execute(stmt)).scalars().first()

    async def list_states(
        self, hotel_id: int | None = None, rule_id: str | None = None
    ) -> list[AlertState]:
        """按店/规则列状态(**「状态」「预警测试」命令用**),按店、规则、实体排序。"""
        stmt = select(AlertState).order_by(
            AlertState.hotel_id, AlertState.rule_id, AlertState.entity_key
        )
        if hotel_id is not None:
            stmt = stmt.where(AlertState.hotel_id == hotel_id)
        if rule_id:
            stmt = stmt.where(AlertState.rule_id == rule_id)
        async with self.db.session() as s:
            return list((await s.execute(stmt)).scalars().all())

    async def is_ignored(self, hotel_id: int, rule_id: str, *, today: date) -> bool:
        """该店该规则今天是否被忽略(查 ``(__hotel__, rule_id)`` 与 ``(__hotel__, __all__)`` 两条)。

        逐字继承旧 ``alert_engine._is_hotel_ignored``(``:610-616``):
        ``ignored_until >= today`` 即"忽略至当天仍有效"。
        """
        async with self.db.session() as s:
            for rid in (rule_id, ALERT_ALL_RULE):
                row = await self.get_in(s, rid, hotel_id, ALERT_HOTEL_ENTITY)
                if row is not None and row.ignored_until is not None and row.ignored_until >= today:
                    return True
        return False

    async def should_push(
        self,
        rule_id: str,
        hotel_id: int,
        entity_key: str,
        *,
        today: date,
        dedup: str = "once_per_day",
        force: bool = False,
    ) -> bool:
        """**当日去重**判定:今天已推过同 (规则,店,实体) → ``False``。

        * ``dedup='once_per_day'``(全部 6 条规则的配置)且 ``last_trigger_date == today``
          → ``False``;``force=True``(**命令「预警测试」**)绕过;
        * 行上若带 ``ignored_until >= today`` → 一律 ``False``(实体级忽略);
        * 没有任何状态行 → ``True``(第一次触发当然要推)。

        ★ 这里**不看** ``streak`` —— 见模块 docstring。
        """
        row = await self.get(rule_id, hotel_id, entity_key)
        if row is None:
            return True
        if row.ignored_until is not None and row.ignored_until >= today:
            return False
        if force or dedup != "once_per_day":
            return True
        return row.last_trigger_date != today

    # ==================================================================
    # 写
    # ==================================================================

    async def upsert(
        self, rule_id: str, hotel_id: int, entity_key: str, **fields: Any
    ) -> None:
        """**UPSERT 幂等**写状态:``ON CONFLICT (rule_id, hotel_id, entity_key) DO UPDATE``。

        只允许 ``_STATE_FIELDS`` 里的列(写错列名立刻报错,不静默忽略)。
        """
        bad = set(fields) - _STATE_FIELDS
        if bad:
            raise ValueError(f"alert_states 非法字段 {sorted(bad)};合法:{sorted(_STATE_FIELDS)}")
        values: dict[str, Any] = {
            "rule_id": rule_id,
            "hotel_id": hotel_id,
            "entity_key": entity_key,
            **fields,
        }
        stmt = pg_insert(AlertState).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["rule_id", "hotel_id", "entity_key"],
            set_={**fields, "updated_at": func.now()},
        )
        async with self.db.session() as s:
            await s.execute(stmt)

    async def mark_triggered(
        self,
        rule_id: str,
        hotel_id: int,
        entity_key: str,
        *,
        today: date,
        dedup: str = "once_per_day",
        force: bool = False,
    ) -> bool:
        """标记已触发:``streak``(仅展示)+1(昨日也触发过才累加,否则重置为 1)。

        返回**本次是否真的算新触发**(``False`` = 当天已推过,去重命中)。
        ★ 与旧 ``apply_state``(``alert_engine.py:582-607``)同款 streak 语义。
        """
        if not await self.should_push(
            rule_id, hotel_id, entity_key, today=today, dedup=dedup, force=force
        ):
            return False
        row = await self.get(rule_id, hotel_id, entity_key)
        yesterday = today - timedelta(days=1)
        prev = row.last_trigger_date if row is not None else None
        streak = (row.streak or 0) + 1 if (row is not None and prev == yesterday) else 1
        await self.upsert(
            rule_id,
            hotel_id,
            entity_key,
            streak=streak,
            last_trigger_date=today,
            status="triggered",
        )
        return True

    async def mark_ok(self, rule_id: str, hotel_id: int, entity_key: str, *, today: date) -> None:
        """标记已恢复:``status='ok'`` / ``last_ok_date=today`` / ``streak=0``。"""
        await self.upsert(
            rule_id,
            hotel_id,
            entity_key,
            streak=0,
            last_ok_date=today,
            status="ok",
        )

    async def reset_when_ok(
        self,
        rule_id: str,
        hotel_id: int,
        triggered_keys: set[str],
        *,
        today: date,
    ) -> int:
        """``reset_when_ok``:**该规则当天无触发的实体**一律清零,返回影响行数。

        ``triggered_keys`` 是本次**真的触发并要推**的实体键集合;
        库里 ``status='triggered'`` 但不在这个集合里的实体 = 条件已恢复 → 清零
        (``status='ok'`` / ``last_ok_date=today`` / ``streak=0``)。

        旧实现是"逐个实体 SELECT 再 UPSERT"(``alert_engine.py:672-676``);
        这里一次 ``UPDATE ... WHERE entity_key NOT IN`` 完成,少了 N 次往返。
        """
        stmt = (
            update(AlertState)
            .where(
                AlertState.rule_id == rule_id,
                AlertState.hotel_id == hotel_id,
                AlertState.status == "triggered",
            )
            .values(streak=0, last_ok_date=today, status="ok", updated_at=func.now())
        )
        if triggered_keys:
            stmt = stmt.where(AlertState.entity_key.not_in(sorted(triggered_keys)))
        async with self.db.session() as s:
            result = await s.execute(stmt)
            count = int(result.rowcount or 0)
        if count:
            logger.info(
                "预警恢复清零:规则={} 店={} {} 个实体(status: triggered→ok)", rule_id, hotel_id, count
            )
        return count

    async def ignore_hotel(
        self, hotel_id: int, until: date, rule_ids: Iterable[str]
    ) -> int:
        """「忽略此店 X N」:对该店**每条规则** + ``__all__`` 各写一行 ``__hotel__``。

        ``until`` 是**含当天**的忽略截止日(命令里 ``days=7`` → ``today + 6``,
        即"今天起 7 天内不推",与 ``ignored_until >= today`` 的判定配套)。
        返回写入行数(去重后 = 规则数 + 1)。
        """
        targets = {str(r) for r in rule_ids if r} | {ALERT_ALL_RULE}
        for rid in sorted(targets):
            await self.upsert(
                rid,
                hotel_id,
                ALERT_HOTEL_ENTITY,
                status="ignored",
                ignored_until=until,
            )
        logger.warning("预警已忽略:店={} 至 {} 共 {} 条规则", hotel_id, until, len(targets))
        return len(targets)


# ===========================================================================
# 预警数据面(段1 只读;T2E.3 的取数侧)
# ===========================================================================
#
# 这五个函数放在本模块而不是 ``engine.py``,有两个理由:
#
# ① 本模块是**预警域唯一持有 ``Database`` 的模块**(``rules.py`` 刻意保持纯 JSON 配置 IO,
#    启动校验规则时不需要数据库在线);取数辅助与状态读写同源,读法口径只此一处;
# ② 单文件行数纪律(项目硬性要求 ≤550 行):``engine.py`` 已经有 slot 映射 + 六条规则
#    判定 + 巡检编排,把**与判定无关的通用取数**搬出来,engine 才能专注"判定语义"。
#
# 口径(逐字继承旧 ``alert_engine``):一律取**最新一次采集**
#   * ``portal_columns``:先算该页内最大 ``collect_date``,再取该日的列;
#   * ``module_records``:两级回退(先 ``module + window``,为空才只按 ``module``),
#     且 **不按当天过滤** —— 旧口径允许取到历史任意一天的最新记录。


def num_of(value: Any) -> float | None:
    """宽松取数:``None`` / ``""`` / ``"None"`` / 非法串 → ``None``(旧 ``_num``)。"""
    try:
        if value is None or value == "" or value == "None":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def row_field(row: Any, key: str, default: Any = None) -> Any:
    """取列值:ORM 对象用属性,测试里的 dict 用键(**两种形态都认**)。"""
    if isinstance(row, Mapping):
        return row.get(key, default)
    return getattr(row, key, default)


@dataclass(slots=True)
class RoomAvailability:
    """一个房型在窗口内的可订情况(见 :func:`derive_room_availability`)。"""

    room_type_id: str
    room_name: str
    unavailable_days: int = 0
    today_missing: bool = False
    statuses: dict[date, bool] = field(default_factory=dict)


def derive_room_availability(
    rows: Iterable[Any], today: date, *, window_days: int = 7
) -> list[RoomAvailability]:
    """★ **陷阱 1 的实现**:"连续 N 天"不在状态机里,在数据里 —— 逐日推导。

    逐字继承旧 ``alert_engine.fetch_room_payload``(``:158-178``)的循环::

        consec = 0
        for i in range(window_days):
            d = today + i 天; ok = statuses.get(d)
            if ok is None:            # 该天没有行
                if i == 0: break      # ★ 今日缺数据 → 保守不触发(consec 保持 0)
                ok = False            # 其它天缺数据 → 按不可订计(旧口径)
            if not ok: consec += 1
            else: break               # ★ 可订即断

    ★ ``alert_states.streak`` **不参与**这里的任何计算(它只是展示用计数,V48 专测)。
    ``available == 1`` 当且仅当 ``roomStatus == 'G'``(售完**仍算可订**,附录 D)。
    """
    by_room: dict[str, RoomAvailability] = {}
    for row in rows:
        rid = str(row_field(row, "room_type_id") or "")
        eff = _as_date(row_field(row, "effect_date"))
        if not rid or eff is None:
            continue
        item = by_room.get(rid)
        if item is None:
            item = RoomAvailability(str(rid), str(row_field(row, "room_name") or rid))
            by_room[rid] = item
        item.statuses[eff] = int(row_field(row, "available") or 0) == 1
    out: list[RoomAvailability] = []
    for item in by_room.values():
        item.today_missing = today not in item.statuses
        if not item.today_missing:
            consec = 0
            for i in range(max(1, int(window_days))):
                state = item.statuses.get(today + timedelta(days=i))
                if state is None:
                    state = False  # i>0 缺数据:旧口径按不可订计
                if state:
                    break  # ★ 可订即断
                consec += 1
            item.unavailable_days = consec
        out.append(item)
    return out


def derive_unavailable_days(
    rows: Iterable[Any], today: date, *, window_days: int = 7
) -> dict[str, int]:
    """``{room_type_id: unavailable_days}``(「房态核查」与自测脚本用)。"""
    return {
        item.room_type_id: item.unavailable_days
        for item in derive_room_availability(rows, today, window_days=window_days)
    }


def _as_date(value: Any) -> date | None:
    """``date`` 原样;``"YYYY-MM-DD"`` 解析;其它 → ``None``。"""
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def column_values(rows: Sequence[Any]) -> dict[str, str]:
    """列行 → ``{column_name: value}``(``value`` 统一 TEXT,读出后自行 cast)。"""
    return {
        str(getattr(r, "column_name", "") or ""): str(getattr(r, "value", "") or "")
        for r in rows
        if getattr(r, "column_name", None)
    }


async def latest_collect_date(
    session: AsyncSession, model: Any, hotel_id: int, page: str | None = None
) -> date | None:
    """该店(该页)最新 ``collect_date``(旧 ``_latest_collect_date``:120-124)。"""
    stmt = select(func.max(model.collect_date)).where(model.hotel_id == hotel_id)
    if page is not None:
        stmt = stmt.where(model.page == page)
    return await session.scalar(stmt)


async def portal_rows(
    repo: CollectRepository, session: AsyncSession, hotel_id: int, page: str
) -> list[AlertPortalColumn]:
    """取某页**最新一次采集**的列(旧口径:先取页内最大 ``collect_date`` 再过滤)。"""
    latest = await latest_collect_date(session, AlertPortalColumn, hotel_id, page)
    if latest is None:
        return []
    return await repo.list_portal_columns(hotel_id, latest, page=page)


async def heat_payload(
    repo: CollectRepository, hotel_id: int, module: str, window: str | None = None
) -> dict[str, Any]:
    """某模块的最新 payload(★ 两级回退:先 ``module + window``,为空才只按 ``module``)。

    模块名/窗口由调用方传入 —— 这样本模块与 :mod:`.engine` **没有循环依赖**。
    """
    row = await repo.latest_module(hotel_id, module, window)
    if row is None and window is not None:
        row = await repo.latest_module(hotel_id, module)
    return dict(getattr(row, "payload_json", None) or {}) if row is not None else {}
