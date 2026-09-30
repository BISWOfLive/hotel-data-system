"""比价存档(段3 T3D.2 / T3D.3)—— **UPSERT 幂等 + 一天多次各留一份**。

★ D8:旧系统在这里被写脏了(实测)
================================

旧 ``storage/prices.py:88-106`` 是 **SELECT-then-INSERT**、两表**都没有 UNIQUE**:

* ``hotel_price_comparisons`` **18 行 / 8 个唯一组合** → ``(anchor_name, query_date, nights)``
  上 **6 组重复、冗余 10 行**(08-26 那批四家店各 **3 条**);
* ``hotel_batch_runs`` 同日多条 ``done``:08-25 **5 条**、08-26 **2 条**;
* 根因是 ``pusher.py:568`` 的 ``run_price_collect`` **硬编码 ``force=True``** ——
  判重 SQL 本身是对的(模拟 ``exists_today`` 3/3 命中),但**每次都被绕过**;
* 而"冗余 10 行"里 **3 条是不同 slot**(00:02 / 01:16 / 17:30 各一次采集)
  —— 那**不是**脏数据,是"一天多次采集"的真实记录。

★ 所以段3 把两件事**分开**:

=========================================  ==================================================
需求                                         实现
=========================================  ==================================================
同一天多次采集**各留一份**(V79)           唯一键**含 ``query_slot``**
同一 slot 重跑**不产生重复行**(V78)        ``INSERT ... ON CONFLICT DO UPDATE``
批量记录"今天比过没"可判断(V80)            ``cmp_batch_runs`` **每日一行 + UPSERT + attempt**
=========================================  ==================================================

这样"看价格一天怎么变"(要 slot)与"今天跑没跑"(要每日一行)**不再互相干扰**。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime
from typing import Any

from loguru import logger
from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from hoteldata.infra.models import CmpBatchRun, CmpPriceComparison, CmpPriceTarget
from hoteldata.infra.models.compare import TARGET_MODE_BOTH

__all__ = ["CompareRepository", "batch_slot", "query_slot_of"]


def query_slot_of(when: datetime | None = None) -> str:
    """``YYYY-MM-DD-HHMM`` —— **一天多次采集各留一份**的依据。

    与 ``push.audit.slot_of`` 的 ``YYYY-MM-DD-HH`` 不同:**比价要精确到分钟**,
    因为采集点是 08:30 / 13:30 / 17:30 这种**非整点**,按小时会互相覆盖。
    """
    dt = when or datetime.now()
    return f"{dt:%Y-%m-%d-%H%M}"


def batch_slot(day: date) -> str:
    """批量任务在 ``cmp_price_comparisons`` 里用的 slot(``YYYY-MM-DD-0100``)。"""
    return f"{day:%Y-%m-%d}-0100"


class CompareRepository:
    """``cmp_*`` 三张表的读写。

    ★ 只碰**自己的三张表**(硬约束 3:域之间不直接 join 别人的表)。
      需要酒店名时由调用方(service)传入,本层不 join ``core_hotels``。
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ==================================================================
    # cmp_price_comparisons —— 报价行
    # ==================================================================

    async def save_quotes(
        self,
        *,
        anchor_name: str,
        platform: str,
        query_date: date,
        rows: Sequence[dict[str, Any]],
        hotel_id: int | None = None,
        city: str | None = None,
        nights: int = 1,
        query_slot: str | None = None,
        is_demo: bool = False,
    ) -> int:
        """把一批报价 UPSERT 进库。**返回写入行数**。

        ``ON CONFLICT (cmp_price_key) DO UPDATE`` —— 唯一键含 ``query_slot``:

        * **同一 slot 重跑** → 命中冲突 → 更新(行数不增);
        * **不同 slot**(同一天多次采集)→ 不冲突 → **新增一行**(V79)。

        ★ 冲突时**更新**而不是 ``DO NOTHING``:重跑时价格可能已经变了
          (同 slot 内的修正、或补跑时页面数据更新),
          留旧值会让"库里是这个价、报告是那个价"。
        """
        slot = query_slot or query_slot_of()
        if not rows:
            return 0

        records: list[dict[str, Any]] = []
        for row in rows:
            records.append(
                {
                    "hotel_id": hotel_id,
                    "anchor_name": anchor_name,
                    "city": city,
                    "platform": platform,
                    "query_date": query_date,
                    "query_slot": slot,
                    "nights": nights,
                    "hotel_name": row["hotel_name"],
                    "room_type": row.get("room_type"),
                    "price": row.get("price"),
                    "distance_km": row.get("distance_km"),
                    "coord_source": row.get("coord_source"),
                    "price_source": row.get("price_source"),
                    "price_scope": row.get("price_scope") or "from",
                    "need_manual_check": bool(row.get("need_manual_check", False)),
                    "degraded": bool(row.get("degraded", False)),
                    "price_rejected": list(row.get("price_rejected") or []) or None,
                    "url": row.get("url"),
                    "score": row.get("score"),
                    "reviews": row.get("reviews"),
                    "is_demo": is_demo,
                    "raw_json_path": row.get("raw_json_path"),
                }
            )

        stmt = pg_insert(CmpPriceComparison).values(records)
        update_cols = {
            "price": stmt.excluded.price,
            "distance_km": stmt.excluded.distance_km,
            "coord_source": stmt.excluded.coord_source,
            "price_source": stmt.excluded.price_source,
            "price_scope": stmt.excluded.price_scope,
            "need_manual_check": stmt.excluded.need_manual_check,
            "degraded": stmt.excluded.degraded,
            "price_rejected": stmt.excluded.price_rejected,
            "url": stmt.excluded.url,
            "score": stmt.excluded.score,
            "reviews": stmt.excluded.reviews,
            "is_demo": stmt.excluded.is_demo,
            "raw_json_path": stmt.excluded.raw_json_path,
            "city": stmt.excluded.city,
            "hotel_id": stmt.excluded.hotel_id,
        }
        stmt = stmt.on_conflict_do_update(constraint="cmp_price_key", set_=update_cols)
        await self.session.execute(stmt)
        await self.session.flush()
        logger.debug(
            "比价存档 UPSERT {} 行(anchor={} platform={} slot={} demo={})",
            len(records),
            anchor_name,
            platform,
            slot,
            is_demo,
        )
        return len(records)

    async def list_quotes(
        self,
        *,
        anchor_name: str | None = None,
        hotel_id: int | None = None,
        query_date: date | None = None,
        platform: str | None = None,
        slot: str | None = None,
        include_demo: bool = True,
        limit: int = 200,
    ) -> list[CmpPriceComparison]:
        """查报价行(报告 / 历史 / 日报段用)。"""
        stmt = select(CmpPriceComparison).order_by(
            CmpPriceComparison.query_date.desc(),
            CmpPriceComparison.query_slot.desc(),
            CmpPriceComparison.distance_km.asc().nulls_last(),
        )
        if anchor_name:
            stmt = stmt.where(CmpPriceComparison.anchor_name == anchor_name)
        if hotel_id is not None:
            stmt = stmt.where(CmpPriceComparison.hotel_id == hotel_id)
        if query_date:
            stmt = stmt.where(CmpPriceComparison.query_date == query_date)
        if platform:
            stmt = stmt.where(CmpPriceComparison.platform == platform)
        if slot:
            stmt = stmt.where(CmpPriceComparison.query_slot == slot)
        if not include_demo:
            stmt = stmt.where(CmpPriceComparison.is_demo.is_(False))
        stmt = stmt.limit(max(1, limit))
        return list((await self.session.execute(stmt)).scalars().all())

    async def latest_slot(
        self, *, anchor_name: str, query_date: date, include_demo: bool = False
    ) -> str | None:
        """当日**最近一个已完成采集的 slot**(V82 的日报段要它)。

        ★ ``slot`` 是 ``YYYY-MM-DD-HHMM`` 字符串,**按字典序排就是按时间排**
          (定长零填充),所以 ``max()`` 即可 —— 不用解析成时间。
        """
        stmt = select(func.max(CmpPriceComparison.query_slot)).where(
            CmpPriceComparison.anchor_name == anchor_name,
            CmpPriceComparison.query_date == query_date,
        )
        if not include_demo:
            stmt = stmt.where(CmpPriceComparison.is_demo.is_(False))
        return (await self.session.execute(stmt)).scalar()

    async def slots_of_day(
        self, *, anchor_name: str, query_date: date, include_demo: bool = False
    ) -> list[str]:
        """当日全部 slot(升序)—— 看"一天采了几次"。"""
        stmt = select(CmpPriceComparison.query_slot).where(
            CmpPriceComparison.anchor_name == anchor_name,
            CmpPriceComparison.query_date == query_date,
        )
        if not include_demo:
            stmt = stmt.where(CmpPriceComparison.is_demo.is_(False))
        rows = (await self.session.execute(stmt.distinct())).scalars().all()
        return sorted(r for r in rows if r)

    async def has_real_data_today(self, query_date: date) -> bool:
        """★ **以数据为准的判重**(B27):"今天有没有**真数据**",不是"今天跑没跑"。

        ``is_demo=False`` 是**显式列**比较 —— 旧系统用
        ``payload NOT LIKE '%"source": "演示"%'`` 这种 LIKE 过滤(脆弱:
        改一次 payload 结构就失效,而且 JSON 转义稍有不同就漏判)。
        """
        stmt = (
            select(func.count())
            .select_from(CmpPriceComparison)
            .where(
                CmpPriceComparison.query_date == query_date,
                CmpPriceComparison.is_demo.is_(False),
                CmpPriceComparison.price.is_not(None),
            )
        )
        return int((await self.session.execute(stmt)).scalar() or 0) > 0

    async def count_today(self, query_date: date, *, include_demo: bool = True) -> int:
        stmt = select(func.count()).select_from(CmpPriceComparison).where(
            CmpPriceComparison.query_date == query_date
        )
        if not include_demo:
            stmt = stmt.where(CmpPriceComparison.is_demo.is_(False))
        return int((await self.session.execute(stmt)).scalar() or 0)

    async def delete_demo(self, *, before: date | None = None) -> int:
        """清理演示数据(**显式列**,不用 LIKE 猜)。"""
        stmt = delete(CmpPriceComparison).where(CmpPriceComparison.is_demo.is_(True))
        if before:
            stmt = stmt.where(CmpPriceComparison.query_date <= before)
        result = await self.session.execute(stmt)
        await self.session.flush()
        return int(getattr(result, "rowcount", 0) or 0)

    # ==================================================================
    # cmp_batch_runs —— 每日一行
    # ==================================================================

    async def upsert_batch_run(
        self,
        *,
        batch_date: date,
        status: str,
        hotels_total: int | None = None,
        hotels_ok: int | None = None,
        hotels_failed: int | None = None,
        hotels_skipped: int | None = None,
        summary: dict[str, Any] | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
    ) -> int:
        """``cmp_batch_runs`` **每日一行 + UPSERT + ``attempt`` 自增**。

        ★ 旧系统痛点是**同日两条 ``done``** → "今天比过没"无法判断。
          段3 用每日一行 + ``attempt`` 计数消歧;
          **尝试级历史交给段1 的 ``job_runs``**(它本来就是干这个的)。

        ``attempt`` 只在**非 running 的重复写入**时自增:先写 ``running``(attempt=1),
        再写 ``done``(attempt 不变,因为这是同一次尝试的收尾);重跑一次才 +1。

        实现上用 ``on_conflict_do_update`` + SQL 表达式 ``cmp_batch_runs.attempt + 1``,
        但**收尾时保持 attempt 不变** —— 所以这里用一个显式参数区分。
        """
        values = {
            "batch_date": batch_date,
            "status": status,
            "hotels_total": hotels_total,
            "hotels_ok": hotels_ok,
            "hotels_failed": hotels_failed,
            "hotels_skipped": hotels_skipped,
            "summary_json": summary,
            "started_at": started_at,
            "finished_at": finished_at,
        }
        stmt = pg_insert(CmpBatchRun).values(**values)
        # ★ 只在**新一次尝试的开始**(running)自增;收尾(done/failed)不改 attempt
        bump = status == "running"
        set_: dict[str, Any] = {
            "status": stmt.excluded.status,
            "hotels_total": stmt.excluded.hotels_total,
            "hotels_ok": stmt.excluded.hotels_ok,
            "hotels_failed": stmt.excluded.hotels_failed,
            "hotels_skipped": stmt.excluded.hotels_skipped,
            "summary_json": stmt.excluded.summary_json,
            "finished_at": stmt.excluded.finished_at,
        }
        # started_at 用 COALESCE:收尾时不该把开始时间清掉
        set_["started_at"] = func.coalesce(stmt.excluded.started_at, CmpBatchRun.started_at)
        if bump:
            set_["attempt"] = CmpBatchRun.attempt + 1
        stmt = stmt.on_conflict_do_update(constraint="cmp_batch_key", set_=set_).returning(
            CmpBatchRun.id, CmpBatchRun.attempt
        )
        row = (await self.session.execute(stmt)).first()
        await self.session.flush()
        return int(row[0]) if row else 0

    async def get_batch_run(self, batch_date: date) -> CmpBatchRun | None:
        stmt = select(CmpBatchRun).where(CmpBatchRun.batch_date == batch_date)
        return (await self.session.execute(stmt)).scalars().first()

    async def count_batch_runs(self, batch_date: date) -> int:
        """★ V80 的断言点:同一 ``batch_date`` **只能有一行**。"""
        stmt = (
            select(func.count())
            .select_from(CmpBatchRun)
            .where(CmpBatchRun.batch_date == batch_date)
        )
        return int((await self.session.execute(stmt)).scalar() or 0)

    # ==================================================================
    # cmp_price_targets —— 比价目标(取代两个 txt)
    # ==================================================================

    async def list_targets(
        self, *, mode: str | None = None, enabled_only: bool = True
    ) -> list[CmpPriceTarget]:
        """按 mode 取目标。

        ★ ``mode='batch'`` / ``'cron'`` 的查询**同时匹配 ``'both'``** ——
          旧系统里"两个 txt 都列了这家店"的含义是「它既在批量清单、
          也在定时采集清单」,所以 ``both`` 必须被两个任务都选到。
          若只做 ``mode == 'batch'`` 的等值匹配,那家店会在定时采集里**消失**
          (实测 ``price_targets.txt`` 只有一家店,而它在两个文件里都有 →
          定时采集会一个目标都没有,09:00 日报的比价段随之在数据层断掉)。
        """
        stmt = select(CmpPriceTarget).order_by(CmpPriceTarget.id)
        if mode:
            stmt = stmt.where(CmpPriceTarget.mode.in_([mode, TARGET_MODE_BOTH]))
        if enabled_only:
            stmt = stmt.where(CmpPriceTarget.enabled.is_(True))
        return list((await self.session.execute(stmt)).scalars().all())

    async def upsert_target(
        self,
        *,
        anchor_name: str,
        city: str | None = None,
        platforms: Iterable[str] | None = None,
        mode: str = "batch",
        enabled: bool | None = None,
        ebk_hotel_id: str | None = None,
        nights: int = 1,
        hotel_id: int | None = None,
        remark: str | None = None,
        overwrite: bool = True,
    ) -> int:
        """新增/更新一个比价目标(``unique(anchor_name, city)``)。

        ★ ``enabled`` 默认 **``None`` = 不改动已有行的启用状态**。

        这不是细节,是"改了城市就把店悄悄停用回来"这类事故的源头:
        调用方(``targets add`` / ``import`` / ``targets set``)大多**不管启用状态**,
        若 ``enabled`` 默认 ``True``,每次 upsert 都会把管理员手动停用的目标**重新启用**
        —— 而"停用"通常正是因为那家店在改版/风控期间不想跑。
        新增行时仍然默认启用(``server_default=true`` + 这里补 ``True``)。
        """
        plats = list(platforms) if platforms is not None else ["ctrip", "meituan"]
        values: dict[str, Any] = {
            "anchor_name": anchor_name,
            "city": city,
            "platforms": plats,
            "mode": mode,
            "ebk_hotel_id": ebk_hotel_id,
            "nights": nights,
            "hotel_id": hotel_id,
            "remark": remark,
        }
        # 新增行:没显式给就默认启用(与 DDL 的 server_default 一致)
        values["enabled"] = True if enabled is None else enabled

        stmt = pg_insert(CmpPriceTarget).values(**values)
        if overwrite:
            set_ = {
                "platforms": stmt.excluded.platforms,
                "mode": stmt.excluded.mode,
                "ebk_hotel_id": stmt.excluded.ebk_hotel_id,
                "nights": stmt.excluded.nights,
                "hotel_id": stmt.excluded.hotel_id,
                "remark": stmt.excluded.remark,
                "updated_at": func.now(),
            }
            # ★ 只在**显式传了** enabled 时才动它
            if enabled is not None:
                set_["enabled"] = stmt.excluded.enabled
            stmt = stmt.on_conflict_do_update(constraint="cmp_target_key", set_=set_)
        else:
            stmt = stmt.on_conflict_do_nothing(constraint="cmp_target_key")
        stmt = stmt.returning(CmpPriceTarget.id)
        row = (await self.session.execute(stmt)).first()
        await self.session.flush()
        return int(row[0]) if row else 0

    async def remove_target(self, anchor_name: str, *, city: str | None = None) -> int:
        stmt = delete(CmpPriceTarget).where(CmpPriceTarget.anchor_name == anchor_name)
        if city is not None:
            stmt = stmt.where(CmpPriceTarget.city == city)
        result = await self.session.execute(stmt)
        await self.session.flush()
        return int(getattr(result, "rowcount", 0) or 0)

    async def set_target_enabled(
        self, anchor_name: str, enabled: bool, *, city: str | None = None
    ) -> int:
        stmt = (
            CmpPriceTarget.__table__.update()
            .where(CmpPriceTarget.anchor_name == anchor_name)
            .values(enabled=enabled, updated_at=func.now())
        )
        if city is not None:
            stmt = stmt.where(CmpPriceTarget.city == city)
        result = await self.session.execute(stmt)
        await self.session.flush()
        return int(getattr(result, "rowcount", 0) or 0)

    async def backfill_target_hotel_ids(self) -> int:
        """把 ``core_hotels`` 里同名酒店的 ``id`` / ``ebk_hotel_id`` 回填进目标表。

        ★ 这是**只读别人的表**吗?不是 —— 它是**写自己的表**,
          只是把 ``core_hotels`` 当查找源。硬约束 3 禁的是"直接 join 别人的表取数",
          这里的目标是让 ``cmp_price_targets`` 自己带上 ``hotel_id``,
          之后所有查询都只碰本域的表。
        """
        sql = text(
            """
            UPDATE cmp_price_targets t
               SET hotel_id      = h.id,
                   ebk_hotel_id  = COALESCE(t.ebk_hotel_id, h.ebk_hotel_id),
                   city          = COALESCE(t.city, h.city),
                   updated_at    = now()
              FROM core_hotels h
             WHERE h.name = t.anchor_name
               AND (t.hotel_id IS DISTINCT FROM h.id
                    OR (t.ebk_hotel_id IS NULL AND h.ebk_hotel_id IS NOT NULL)
                    OR (t.city IS NULL AND h.city IS NOT NULL))
            """
        )
        result = await self.session.execute(sql)
        await self.session.flush()
        return int(getattr(result, "rowcount", 0) or 0)

    # ==================================================================
    # 概览
    # ==================================================================

    async def day_summary(self, query_date: date) -> dict[str, Any]:
        """某日概览(slot 数 / 行数 / 平台分布 / 距离可用率)。"""
        total = await self.count_today(query_date)
        real = await self.count_today(query_date, include_demo=False)
        stmt = select(
            func.count(func.distinct(CmpPriceComparison.query_slot)),
            func.count(CmpPriceComparison.distance_km),
            func.count(CmpPriceComparison.price),
        ).where(CmpPriceComparison.query_date == query_date)
        slots, with_dist, with_price = (await self.session.execute(stmt)).one()
        return {
            "date": query_date.isoformat(),
            "rows": total,
            "rows_real": real,
            "rows_demo": total - real,
            "slots": int(slots or 0),
            "with_distance": int(with_dist or 0),
            "with_price": int(with_price or 0),
        }
