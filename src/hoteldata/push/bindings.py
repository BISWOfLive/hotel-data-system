"""群 ↔ 酒店绑定(T2A.8)—— ``core_group_bindings`` 的唯一读写入口。

语义(逐字继承旧 ``pusher.bindings_grouped`` / ``commands._bind_reply``)
======================================================================

* ``UNIQUE(chatid, hotel_id)`` → 「绑定」命令**幂等**;重复绑定不报错、不重复;
* **过滤 ``paused``**:绑定被暂停的群×店不参与推送(旧系统过滤的是
  ``hotels.status='paused'`` —— 那会连带停掉该店的全部业务,新表把它独立出来);
* **一群多店**:``grouped()`` 返回 ``{chatid: [店, ...]}``,推送时多店内容用
  ``\\n\\n\\n`` 合并成 **1 条**消息(A2-5)。

🚫 本模块**不 import 任何 domain**:它只认 ``core_group_bindings`` + ``core_hotels``
两张表的读取形状,返回 :class:`BoundHotel` 值对象。**不 join 别人的提取表**
(硬约束 3)。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from hoteldata.infra.db import Database
from hoteldata.infra.models import GroupBinding, Hotel

__all__ = ["BoundHotel", "Bindings"]


@dataclass(frozen=True, slots=True)
class BoundHotel:
    """绑定关系里的酒店(推送组装需要的**最小字段集**)。

    刻意不返回 ORM 对象:调用方拿到的是不可变值,
    不会因为会话关闭而踩到 ``DetachedInstanceError``。
    """

    hotel_id: int
    name: str
    city: str | None = None
    ebk_hotel_id: str | None = None
    chatid: str = ""
    paused: bool = False

    @classmethod
    def from_row(cls, hotel: Hotel, chatid: str, paused: bool) -> BoundHotel:
        return cls(
            hotel_id=int(hotel.id),
            name=str(hotel.name),
            city=hotel.city,
            ebk_hotel_id=hotel.ebk_hotel_id,
            chatid=chatid,
            paused=bool(paused),
        )


class Bindings:
    """``core_group_bindings`` 读写。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    # ==================================================================
    # 读
    # ==================================================================

    async def grouped(self, *, include_paused: bool = False) -> dict[str, list[BoundHotel]]:
        """群 → 绑定酒店列表(**过滤 ``paused``**,一群多店)。"""
        async with self.db.session() as s:
            return await self.grouped_in(s, include_paused=include_paused)

    async def grouped_in(
        self, session: AsyncSession, *, include_paused: bool = False
    ) -> dict[str, list[BoundHotel]]:
        stmt = (
            select(GroupBinding, Hotel)
            .join(Hotel, Hotel.id == GroupBinding.hotel_id, isouter=True)
            .order_by(GroupBinding.chatid, Hotel.id)
        )
        if not include_paused:
            stmt = stmt.where(GroupBinding.paused.is_(False))
        rows = (await session.execute(stmt)).all()
        out: dict[str, list[BoundHotel]] = {}
        for binding, hotel in rows:
            if hotel is None:
                # 外键 CASCADE 保证不该发生;真发生了要可见,不静默丢
                out.setdefault(str(binding.chatid), [])
                continue
            out.setdefault(str(binding.chatid), []).append(
                BoundHotel.from_row(hotel, str(binding.chatid), bool(binding.paused))
            )
        return out

    async def for_group(self, chatid: str, *, include_paused: bool = False) -> list[BoundHotel]:
        """某群绑定的酒店(**推送目标解析的唯一入口**)。"""
        async with self.db.session() as session:
            stmt = (
                select(GroupBinding, Hotel)
                .join(Hotel, Hotel.id == GroupBinding.hotel_id, isouter=True)
                .where(GroupBinding.chatid == chatid)
                .order_by(Hotel.id)
            )
            if not include_paused:
                stmt = stmt.where(GroupBinding.paused.is_(False))
            rows = (await session.execute(stmt)).all()
        return [
            BoundHotel.from_row(hotel, chatid, bool(binding.paused))
            for binding, hotel in rows
            if hotel is not None
        ]

    async def groups_of_hotel(self, hotel_id: int) -> list[str]:
        """某店被哪些群绑定(预警的"运营群"目标解析用)。"""
        async with self.db.session() as s:
            stmt = (
                select(GroupBinding.chatid)
                .where(GroupBinding.hotel_id == hotel_id, GroupBinding.paused.is_(False))
                .order_by(GroupBinding.chatid)
            )
            return [str(x) for (x,) in (await s.execute(stmt)).all()]

    async def list_all(self) -> list[BoundHotel]:
        """全部绑定(**含 paused**,供 CLI ``bind list``)。"""
        rows = await self.grouped(include_paused=True)
        return [h for items in rows.values() for h in items]

    # ==================================================================
    # 写
    # ==================================================================

    async def bind(self, chatid: str, hotel_id: int, *, paused: bool = False) -> bool:
        """绑定(★ **幂等**:``UNIQUE`` 冲突时 ``DO NOTHING``)。

        返回 ``True`` = 本次新建;``False`` = 本来就有(重复绑定,不报错)。
        """
        stmt = (
            pg_insert(GroupBinding)
            .values(chatid=chatid, hotel_id=hotel_id, paused=paused)
            .on_conflict_do_nothing(index_elements=["chatid", "hotel_id"])
            .returning(GroupBinding.id)
        )
        async with self.db.session() as s:
            return (await s.scalar(stmt)) is not None

    async def unbind(self, chatid: str, hotel_id: int | None = None) -> int:
        """解绑(``hotel_id=None`` → 解绑该群全部)。返回删除行数。"""
        stmt = delete(GroupBinding).where(GroupBinding.chatid == chatid)
        if hotel_id is not None:
            stmt = stmt.where(GroupBinding.hotel_id == hotel_id)
        async with self.db.session() as s:
            result = await s.execute(stmt)
            return int(result.rowcount or 0)

    async def set_paused(self, chatid: str, hotel_id: int, paused: bool) -> int:
        """暂停/恢复某条绑定。返回受影响行数。"""
        from sqlalchemy import update

        stmt = (
            update(GroupBinding)
            .where(GroupBinding.chatid == chatid, GroupBinding.hotel_id == hotel_id)
            .values(paused=paused)
        )
        async with self.db.session() as s:
            result = await s.execute(stmt)
            return int(result.rowcount or 0)
