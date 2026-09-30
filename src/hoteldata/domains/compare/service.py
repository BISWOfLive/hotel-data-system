"""★ 比价域服务(``runtime.compare()``)—— CLI / 任务 / 段2 钩子的唯一入口。

本模块是**门面**,把"一次比价要做的一串事"收在一处:

* :meth:`CompareService.compare` —— 单店(CLI ``hoteldata compare``);
* :meth:`CompareService.run_collect` —— 定时采集(3 次/天,存档);
* :meth:`CompareService.run_batch` —— 批量清单;
* :meth:`CompareService.push_price` —— 14:00 / 18:00 独立推送(**纯文字**);
* :meth:`CompareService.group_price_section` —— ★ **段2 日报钩子的实现**。

★ 为什么门面里要有 ``group_price_section``
========================================

段2 的日报组装器签名是 ``(chatid, price_section)`` —— 见
:mod:`hoteldata.domains.compare.service` 的模块文档(段3 P7)。
真正把它接到段2 上的是 :meth:`Runtime.start_push` 里的一行闭包:

.. code-block:: python

    async def _build_daily(chatid: str, price_section: str | None) -> Any:
        if price_section is None:
            price_section = await self.compare().group_price_section(chatid)
        return await build_daily_message(self, chatid, price_section=price_section)

**注意这段代码一行都没有改段2 的文件** —— 它写在 ``runtime.py``(装配处),
而 ``domains/report/daily.py`` 的 ``price_section`` 参数本来就是段2 预留的钩子。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from loguru import logger

from hoteldata.domains.compare.contract import (
    CompareResult,
    HumanVerificationError,
)
from hoteldata.domains.compare.registry import resolve_platforms
from hoteldata.domains.compare.report import PLATFORM_LABELS, build_group_price_text
from hoteldata.domains.compare.repository import CompareRepository, batch_slot, query_slot_of
from hoteldata.domains.compare.runner import CompareRunner
from hoteldata.domains.compare.sections import build_group_price_section, build_price_section
from hoteldata.domains.compare.vision import VisionEstimator

__all__ = ["CompareService"]


class CompareService:
    """比价域服务。每次调用各自开 DB 会话(**不做长事务**)。"""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.settings = runtime.settings
        self.cfg = self.settings.compare

    # ==================================================================
    # 构造
    # ==================================================================

    def _runner(self, session: Any) -> CompareRunner:
        return CompareRunner(
            settings=self.settings,
            layout=self.runtime.layout,
            sessions=self.runtime.sessions,
            pool=self.runtime.browser,
            limiter=self.runtime.limiter,
            http=self.runtime.http,
            repo=CompareRepository(session),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled)

    # ==================================================================
    # 单店
    # ==================================================================

    async def compare(
        self,
        anchor_name: str,
        *,
        city: str | None = None,
        platforms: Any = None,
        nights: int = 1,
        ebk_hotel_id: str | None = None,
        hotel_id: int | None = None,
        day: date | None = None,
        demo: bool = False,
        persist: bool = True,
        force: bool = False,
        quote_count: int | None = None,
        nearby_count: int | None = None,
    ) -> CompareResult:
        """单店比价(CLI ``hoteldata compare``)。

        * ``force`` 只影响**存档判重提示**,不影响单店比价本身
          —— 单店是**人主动跑的**,没有"今天跑过就别跑"的语义;
        * ``quote_count`` / ``nearby_count`` 覆盖 ``HOTEL_QUOTE_COUNT`` /
          ``HOTEL_NEARBY_COUNT``(CLI 的 ``--rooms`` / ``--nearby``)。
        """
        target_day = day or self._today()
        async with self.runtime.db.session() as session:
            # 无浏览器时**显式失败**:比价必须要有页面
            if self.runtime.browser is None:
                await self.runtime.start_browser()
            runner = self._runner(session)
            hotel_id = hotel_id or await self._resolve_hotel_id(session, anchor_name)
            ebk = ebk_hotel_id or await self._resolve_ebk_id(session, anchor_name)
            return await runner.compare_one(
                anchor_name,
                city=city,
                platforms=resolve_platforms(platforms or list(self.cfg.platforms)),
                nights=nights,
                quote_count=quote_count,
                nearby_count=nearby_count,
                ebk_hotel_id=ebk,
                hotel_id=hotel_id,
                query_date=target_day,
                slot=query_slot_of(),
                demo=demo,
                persist=persist,
            )

    # ==================================================================
    # 定时采集(3 次/天,存档)
    # ==================================================================

    async def run_collect(
        self, *, day: date | None = None, targets: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        """``compare.collect``(08:30 / 13:30 / 17:30):**只存档 + 出报告**,不推送。

        ★ 09:00 的比价**不单独推** —— 它通过日报钩子合并(计划书 §5.10)。
          等等:采集点是 08:30,而日报是 09:00 —— 按 P8 的 slot 语义,
          日报取"当日最近一个已完成的 slot",08:30 那次正好被 09:00 的日报读到。
        """
        target_day = day or self._today()
        rows = targets if targets is not None else await self.targets(mode="cron", live=False)
        if not rows:
            logger.warning("compare.collect:{} 没有 enabled 的 cron 目标", target_day)
            return {"task": "compare.collect", "date": target_day.isoformat(), "hotels": 0, "ok": 0}

        return await self._run_targets(
            rows, day=target_day, slot=query_slot_of(), label="compare.collect"
        )

    # ==================================================================
    # 批量
    # ==================================================================

    async def run_batch(
        self, *, day: date | None = None, force: bool = False
    ) -> dict[str, Any]:
        """``compare.batch``(01:00):遍历 ``mode='batch'`` 的清单。

        * 先按 **B27「以数据为准」** 判重:今天有真数据就不跑(``force`` 可绕);
        * **单店失败不阻断整批**(附录 B 业务事实);
        * 汇总写 ``cmp_batch_runs``(**每日一行 + UPSERT + attempt**)。
        """
        target_day = day or self._today()
        async with self.runtime.db.session() as session:
            repo = CompareRepository(session)
            runner = self._runner(session)

            should, why = await runner.should_run(target_day, session=session, force=force)
            await repo.upsert_batch_run(
                batch_date=target_day,
                status="running",
                started_at=datetime.now(self.settings.tzinfo),
                summary={"reason": why},
            )
            await session.commit()

            if not should:
                logger.info("compare.batch 跳过:{}", why)
                await repo.upsert_batch_run(
                    batch_date=target_day,
                    status="done",
                    hotels_total=0,
                    hotels_ok=0,
                    hotels_failed=0,
                    hotels_skipped=0,
                    summary={"reason": why, "skipped": True},
                    finished_at=datetime.now(self.settings.tzinfo),
                )
                await session.commit()
                return {
                    "task": "compare.batch",
                    "date": target_day.isoformat(),
                    "skipped": True,
                    "reason": why,
                }

            rows = await self._targets_in(session, mode="batch")
            if not rows:
                await repo.upsert_batch_run(
                    batch_date=target_day,
                    status="done",
                    hotels_total=0,
                    hotels_ok=0,
                    hotels_failed=0,
                    hotels_skipped=0,
                    summary={"reason": "清单为空"},
                    finished_at=datetime.now(self.settings.tzinfo),
                )
                await session.commit()
                logger.warning("compare.batch:{} 清单为空(cmp_price_targets 无 mode=batch 的行)", target_day)
                return {"task": "compare.batch", "date": target_day.isoformat(), "hotels": 0}

        summary = await self._run_targets(
            rows, day=target_day, slot=batch_slot(target_day), label="compare.batch"
        )

        ok = int(summary.get("ok", 0))
        failed = int(summary.get("failed", 0))
        status = "done" if failed == 0 else ("done" if ok > 0 else "failed")
        async with self.runtime.db.session() as session:
            repo = CompareRepository(session)
            await repo.upsert_batch_run(
                batch_date=target_day,
                status=status,
                hotels_total=len(rows),
                hotels_ok=ok,
                hotels_failed=failed,
                hotels_skipped=int(summary.get("skipped", 0)),
                summary=summary,
                finished_at=datetime.now(self.settings.tzinfo),
            )
            await session.commit()
            # ★ V80 的证据:同一 batch_date 只能有一行
            rows_count = await repo.count_batch_runs(target_day)
        summary["cmp_batch_rows"] = rows_count
        summary["status"] = status
        logger.info(
            "compare.batch 完成:{} 家,成功 {} 失败 {};cmp_batch_runs 该日 {} 行",
            len(rows),
            ok,
            failed,
            rows_count,
        )
        return summary

    # ==================================================================
    # 独立推送(14:00 / 18:00,纯文字)
    # ==================================================================

    async def push_price(
        self, *, day: date | None = None, slot: str | None = None, force: bool = False
    ) -> dict[str, Any]:
        """``compare.push``:**纯文字**推送到每个"有比价数据"的群。

        * ``push_type='price_compare'``;
        * 去重交给 ``push/`` 的派发器(slot = ``YYYY-MM-DD-HH``);
        * 走段2 的 ``push.service``(**不直连机器人**)。
        """
        target_day = day or self._today()
        async with self.runtime.db.session() as session:
            grouped = await self.runtime.bindings.grouped_in(session)
            sections: dict[str, list[str]] = {}
            for chatid, hotels in grouped.items():
                parts: list[str] = []
                for hotel in hotels:
                    one = await build_price_section(
                        session,
                        anchor_name=hotel.name,
                        day=target_day,
                        slot=slot,
                        quote_count=self.cfg.quote_count,
                    )
                    if one.text and one.priced > 0:
                        parts.append(one.text)
                if parts:
                    sections[chatid] = parts

        if not sections:
            logger.info("compare.push:{} 无比价数据可推", target_day)
            return {"task": "compare.push", "date": target_day.isoformat(), "groups": 0, "pushed": 0}

        from hoteldata.push.service import BuiltMessage

        pushed = 0
        failed = 0
        for chatid, parts in sections.items():
            content = (
                build_group_price_text(sections=parts, hotel_count=len(parts))
                if len(parts) > 1
                else parts[0]
            )
            msg = BuiltMessage(
                chatid=chatid,
                push_type="price_compare",
                content=content,
                images=(),
                note=f"比价独立推送 {len(parts)} 店",
            )
            try:
                delivery = await self.runtime.push.push(msg, force=force, now=True)
                if delivery is not None and getattr(delivery, "ok", False):
                    pushed += 1
                else:
                    failed += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                logger.error("比价推送失败(group={}): {}", chatid, exc)

        logger.info("compare.push 完成:群 {} 成功 {} 失败 {}", len(sections), pushed, failed)
        return {
            "task": "compare.push",
            "date": target_day.isoformat(),
            "groups": len(sections),
            "pushed": pushed,
            "failed": failed,
        }

    # ==================================================================
    # ★ 段2 日报钩子
    # ==================================================================

    async def group_price_section(self, chatid: str, day: date | None = None) -> str | None:
        """某群当日比价段(供段2 日报注入)。**无数据 → ``None``**。"""
        target_day = day or self._today()
        hotels = await self.runtime.bindings.for_group(chatid)
        if not hotels:
            return None
        async with self.runtime.db.session() as session:
            return await build_group_price_section(
                session,
                hotel_names=[h.name for h in hotels],
                day=target_day,
                quote_count=self.cfg.quote_count,
            )

    # ==================================================================
    # 目标清单
    # ==================================================================

    async def targets(self, *, mode: str | None = None) -> list[dict[str, Any]]:
        """比价目标清单(``cmp_price_targets``)—— 只读 ``enabled`` 的行。"""
        async with self.runtime.db.session() as session:
            return await self._targets_in(session, mode=mode)

    async def _targets_in(self, session: Any, *, mode: str | None = None) -> list[dict[str, Any]]:
        repo = CompareRepository(session)
        rows = await repo.list_targets(mode=mode, enabled_only=True)
        out: list[dict[str, Any]] = []
        for row in rows:
            out.append(
                {
                    "anchor_name": row.anchor_name,
                    "city": row.city,
                    "platforms": list(row.platforms or []),
                    "mode": row.mode,
                    "nights": int(row.nights or 1),
                    "ebk_hotel_id": row.ebk_hotel_id,
                    "hotel_id": row.hotel_id,
                }
            )
        return out

    # ==================================================================
    # 历史
    # ==================================================================

    async def history(
        self, anchor_name: str, *, days: int = 7, include_demo: bool = False
    ) -> dict[str, Any]:
        """按天+slot 汇总历史(CLI ``hoteldata price history``)。"""
        async with self.runtime.db.session() as session:
            repo = CompareRepository(session)
            rows = await repo.list_quotes(
                anchor_name=anchor_name, include_demo=include_demo, limit=2000
            )
        by_day: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = row.query_date.isoformat()
            bucket = by_day.setdefault(key, {"date": key, "slots": set(), "rows": 0, "min": None})
            bucket["slots"].add(row.query_slot)
            bucket["rows"] += 1
            if row.price is not None:
                price = float(row.price)
                bucket["min"] = price if bucket["min"] is None else min(bucket["min"], price)
        out = []
        for key in sorted(by_day, reverse=True)[: max(1, days)]:
            bucket = by_day[key]
            out.append(
                {
                    "date": bucket["date"],
                    "slots": sorted(bucket["slots"]),
                    "rows": bucket["rows"],
                    "min_price": bucket["min"],
                }
            )
        return {"anchor_name": anchor_name, "days": out}

    # ==================================================================
    # 内部
    # ==================================================================

    async def _run_targets(
        self,
        rows: list[dict[str, Any]],
        *,
        day: date,
        slot: str,
        label: str,
    ) -> dict[str, Any]:
        """★ 逐店跑,**单店失败不阻断整批**(附录 B 业务事实)。"""
        if self.runtime.browser is None:
            await self.runtime.start_browser()

        ok = 0
        failed = 0
        errors: list[dict[str, str]] = []
        for item in rows:
            name = str(item.get("anchor_name") or "")
            if not name:
                continue
            try:
                async with self.runtime.db.session() as session:
                    runner = self._runner(session)
                    result = await runner.compare_one(
                        name,
                        city=item.get("city"),
                        platforms=resolve_platforms(item.get("platforms") or list(self.cfg.platforms)),
                        nights=int(item.get("nights") or 1),
                        ebk_hotel_id=item.get("ebk_hotel_id"),
                        hotel_id=item.get("hotel_id"),
                        query_date=day,
                        slot=slot,
                        persist=True,
                    )
                    await session.commit()
                ok += 1
                logger.info(
                    "{}:{} 完成,{} 条报价(slot={})", label, name, len(result.quotes), slot
                )
            except HumanVerificationError as exc:
                failed += 1
                errors.append({"anchor": name, "kind": "HumanVerification", "error": str(exc)})
                logger.error("{}:{} 需要人工处理:{}", label, name, exc)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                errors.append({"anchor": name, "kind": type(exc).__name__, "error": str(exc)})
                logger.error("{}:{} 失败({}):{}", label, name, type(exc).__name__, exc)

        return {
            "task": label,
            "date": day.isoformat(),
            "slot": slot,
            "hotels": len(rows),
            "ok": ok,
            "failed": failed,
            "skipped": 0,
            "errors": errors[:20],
            "platforms": list(resolve_platforms(list(self.cfg.platforms))),
            "labels": PLATFORM_LABELS,
        }

    async def _resolve_hotel_id(self, session: Any, anchor_name: str) -> int | None:
        from sqlalchemy import select

        from hoteldata.infra.models import Hotel

        stmt = select(Hotel.id).where(Hotel.name == anchor_name)
        row = (await session.execute(stmt)).first()
        return int(row[0]) if row else None

    async def _resolve_ebk_id(self, session: Any, anchor_name: str) -> str | None:
        from sqlalchemy import select

        from hoteldata.infra.models import Hotel

        stmt = select(Hotel.ebk_hotel_id).where(Hotel.name == anchor_name)
        row = (await session.execute(stmt)).first()
        return str(row[0]) if row and row[0] else None

    def _today(self) -> date:
        return datetime.now(self.settings.tzinfo).date()

    @property
    def vision(self) -> VisionEstimator:
        """视觉读价器(**门控在它自己身上**;``VISION_ENABLED=0`` 时零调用)。"""
        cached = getattr(self, "_vision", None)
        if cached is None:
            cached = VisionEstimator(self.settings, client=self.runtime.http)
            self._vision = cached
        return cached

    def snapshot(self) -> dict[str, Any]:
        """``/status`` 与 ``hoteldata env`` 用。"""
        return {
            "enabled": self.enabled,
            "platforms": list(self.cfg.platforms),
            "nearby_count": self.cfg.nearby_count,
            "quote_count": self.cfg.quote_count,
            "rank_mode": self.cfg.rank_mode,
            "headless": self.cfg.headless,
            "coord_fallback_max": self.cfg.coord_fallback_max,
            "vision": self.vision.snapshot(),
        }
