"""推送审计(T2A.7)—— ``push_logs`` 的**唯一写入口**。

三条口径
========

① **去重时段键 slot = ``YYYY-MM-DD-HH``**(A2-3 逐字继承)。
   同一 ``(群, 店, push_type, slot)`` 已有 ``status='ok'`` → 跳过。
   旧系统把 slot 藏在 ``pushed_at`` 里靠两次 ``substr`` 切片比对
   (``substr(pushed_at,1,10)`` + ``substr(pushed_at,12,2)``),
   新库把 slot **显式落列**并建索引,去重变成一次等值比较。

② **``bot_id`` 是文本**(D17 修复)。旧库列是 ``INTEGER``,代码写的却是
   ``getattr(bot, "bot_name", None) or "legacy"`` 这样的**机器人名字符串** ——
   SQLite 动态类型不报错,但审计数据不可用,**无法按机器人统计**。
   V38 专门验这一条。

③ **去重命中要留痕**(写一行 ``status='skipped'``),不许静默跳过。
   旧系统的去重是"直接 return False",日志里查不到任何痕迹 ——
   这正是"静默失败 0 次/周"这个 KPI 最难守的地方。

★ 本模块**只写审计,不做投递**。投递在 :mod:`hoteldata.push.dispatcher`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hoteldata.infra.db import Database
from hoteldata.infra.models import PushLog

__all__ = [
    "DAY_STATS_KEYS",
    "PushAudit",
    "PushRecord",
    "slot_of",
]

#: ``success_rate`` 返回的键
DAY_STATS_KEYS = ("ok", "failed", "skipped", "total", "rate")


def slot_of(now: datetime | None = None) -> str:
    """去重时段键 ``YYYY-MM-DD-HH``(A2-3)。

    ★ 09:00 的日报与 09:00 的比价**共用同一小时键** —— 这是甲方口径
    「比价 09:00 与日报合并为一条」(段2 §1.4)能去重的前提。
    """
    return (now or datetime.now()).strftime("%Y-%m-%d-%H")


@dataclass(slots=True)
class PushRecord:
    """一行 ``push_logs`` 的内容。"""

    group_chatid: str
    push_type: str
    status: str
    bot_id: str
    hotel_id: int | None = None
    content_preview: str | None = None
    media_count: int = 0
    error: str | None = None
    images: tuple[str, ...] = ()
    slot: str = ""
    pushed_at: datetime | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def preview(self, limit: int = 200) -> str | None:
        if self.content_preview is not None:
            return self.content_preview[:limit]
        return None


class PushAudit:
    """``push_logs`` 的写入与查询。

    持有 :class:`~hoteldata.infra.db.Database` 而不是 session:
    派发器的 worker 是**后台长驻任务**,每写一行都要自己开一个短会话。
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    # ==================================================================
    # 写
    # ==================================================================

    async def write(self, rec: PushRecord) -> int:
        """写一行审计(返回行 id)。"""
        async with self.db.session() as s:
            return await self.write_in(s, rec)

    async def write_in(self, session: AsyncSession, rec: PushRecord) -> int:
        row = PushLog(
            hotel_id=rec.hotel_id,
            group_chatid=rec.group_chatid,
            bot_id=rec.bot_id,
            push_type=rec.push_type,
            content_preview=rec.preview(),
            media_count=int(rec.media_count),
            status=rec.status,
            error=(rec.error or None),
            images_json=list(rec.images) or None,
            slot=rec.slot or slot_of(),
            pushed_at=rec.pushed_at or datetime.now(),
        )
        session.add(row)
        await session.flush()
        return int(row.id)

    async def write_many(self, recs: list[PushRecord]) -> int:
        """批量写(一次会话)。返回写入行数。"""
        if not recs:
            return 0
        async with self.db.session() as s:
            for rec in recs:
                await self.write_in(s, rec)
        return len(recs)

    # ==================================================================
    # 去重查询
    # ==================================================================

    async def pushed_ok(
        self,
        group_chatid: str,
        hotel_id: int | None,
        push_type: str,
        slot: str,
    ) -> bool:
        """当天该时段 ``(群, 店, 类型)`` 是否已**成功**推送过(A2-3)。"""
        async with self.db.session() as s:
            return await self.pushed_ok_in(s, group_chatid, hotel_id, push_type, slot)

    async def pushed_ok_in(
        self,
        session: AsyncSession,
        group_chatid: str,
        hotel_id: int | None,
        push_type: str,
        slot: str,
    ) -> bool:
        stmt = (
            select(func.count())
            .select_from(PushLog)
            .where(
                PushLog.group_chatid == group_chatid,
                PushLog.push_type == push_type,
                PushLog.slot == slot,
                PushLog.status == "ok",
            )
        )
        if hotel_id is not None:
            stmt = stmt.where(PushLog.hotel_id == hotel_id)
        return bool(await session.scalar(stmt))

    # ==================================================================
    # 查询 / 统计
    # ==================================================================

    async def list_logs(
        self,
        *,
        day: date | None = None,
        group_chatid: str | None = None,
        hotel_id: int | None = None,
        push_type: str | None = None,
        status: str | None = None,
        limit: int = 200,
    ) -> list[PushLog]:
        """按条件列审计行(倒序)。``day`` 命中 ``slot`` 前 10 位。"""
        stmt = select(PushLog).order_by(PushLog.id.desc()).limit(max(1, limit))
        if day is not None:
            stmt = stmt.where(PushLog.slot.like(f"{day.isoformat()}-%"))
        if group_chatid:
            stmt = stmt.where(PushLog.group_chatid == group_chatid)
        if hotel_id is not None:
            stmt = stmt.where(PushLog.hotel_id == hotel_id)
        if push_type:
            stmt = stmt.where(PushLog.push_type == push_type)
        if status:
            stmt = stmt.where(PushLog.status == status)
        async with self.db.session() as s:
            return list((await s.execute(stmt)).scalars().all())

    async def day_stats(self, day: date | None = None) -> dict[str, Any]:
        """当日推送成败统计(``/status`` 与「状态」命令用)。

        ``rate`` = ``ok / (ok + failed)`` —— **``skipped`` 不进分母**:
        它是"今天已经推过了"的正常结果,不是投递失败。
        """
        target = (day or date.today()).isoformat()
        stmt = (
            select(PushLog.status, func.count())
            .where(PushLog.slot.like(f"{target}-%"))
            .group_by(PushLog.status)
        )
        async with self.db.session() as s:
            rows = (await s.execute(stmt)).all()
        counts = {str(k): int(v) for k, v in rows}
        ok = counts.get("ok", 0)
        failed = counts.get("failed", 0)
        skipped = counts.get("skipped", 0)
        denom = ok + failed
        return {
            "ok": ok,
            "failed": failed,
            "skipped": skipped,
            "total": sum(counts.values()),
            "rate": (ok / denom * 100.0) if denom else 0.0,
        }

    async def group_stats(self, day: date | None = None, limit: int = 20) -> dict[str, int]:
        """当日按群统计推送条数(「汇总」命令用)。"""
        target = (day or date.today()).isoformat()
        stmt = (
            select(PushLog.group_chatid, func.count())
            .where(PushLog.slot.like(f"{target}-%"), PushLog.status == "ok")
            .group_by(PushLog.group_chatid)
            .order_by(func.count().desc())
            .limit(max(1, limit))
        )
        async with self.db.session() as s:
            rows = (await s.execute(stmt)).all()
        return {str(k): int(v) for k, v in rows}

    async def type_stats(self, day: date | None = None) -> dict[str, int]:
        """当日按 ``push_type`` 统计(「状态」命令用)。"""
        target = (day or date.today()).isoformat()
        stmt = (
            select(PushLog.push_type, func.count())
            .where(PushLog.slot.like(f"{target}-%"))
            .group_by(PushLog.push_type)
            .order_by(PushLog.push_type)
        )
        async with self.db.session() as s:
            rows = (await s.execute(stmt)).all()
        return {str(k): int(v) for k, v in rows}

    # ==================================================================
    # 便捷:去重 + 留痕一步到位
    # ==================================================================

    async def skip_if_duplicated(
        self,
        session: AsyncSession,
        *,
        group_chatid: str,
        hotel_id: int | None,
        push_type: str,
        slot: str,
        bot_id: str = "",
        note: str = "当日该时段已推送(status=ok),去重跳过",
    ) -> bool:
        """命中当日去重 → 写一行 ``skipped`` 并返回 ``True``(调用方应放弃投递)。

        ★ 传 ``session`` 是为了让"查重 + 留痕"在**同一事务**里完成;
        跨事务会有"查完未写"的窗口,并发下两个 worker 会同时认为自己是第一个。
        """
        if not await self.pushed_ok_in(session, group_chatid, hotel_id, push_type, slot):
            return False
        await self.write_in(
            session,
            PushRecord(
                group_chatid=group_chatid,
                hotel_id=hotel_id,
                push_type=push_type,
                status="skipped",
                bot_id=bot_id or "",
                slot=slot,
                content_preview=note,
            ),
        )
        logger.info("推送去重命中:群={} 店={} 类型={} slot={}", group_chatid, hotel_id, push_type, slot)
        return True
