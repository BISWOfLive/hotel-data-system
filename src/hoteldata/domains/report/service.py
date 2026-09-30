"""报告域服务入口(T2C.3 · T2D.6 · ``push.daily`` / ``push.schedule`` / ``report run``)。

职责边界(计划书 §1.3 点名要避免的"上帝模块")
============================================

=================================  ==============================================
放这里                               不放这里
=================================  ==============================================
群维度编排(哪群发哪些项)             内容排版(:mod:`~hoteldata.domains.report.render`)
取数(只走段1 ``collect.service``)     条件判定(:mod:`~hoteldata.domains.report.engine`)
图文绑定 / 缺图策略(V41)             日报段组装(:mod:`~hoteldata.domains.report.daily`)
合并拆分与入队(``push/`` 层)          限频 / 重试 / 审计(``push/dispatcher.py``)
=================================  ==============================================

旧系统把上面全部塞进 ``app/pusher.py`` + ``app/report_push.py``(约 1200 行),
"日报没下发"这种问题要同时读五个文件才能定位。

★ 三个必修项都在本模块可见
=========================

* **V41 图文绑定**:配了 ``images`` 却一张都没截到 → **该项整条不发**,
  最后向管理群告警「缺图未发 N 条」(:meth:`ReportService._build_section`)。
  缺图**不是错误**,是"跳过 + 告知" —— 半条报告比没有报告更糟。
* **D10 周报上期列**:``svc_weekly`` 走 :meth:`ReportService._aggregate_data`,
  本期 + **上一期**两次聚合,合成为 ``compare`` 交给渲染器(旧 ``report_push.py:154``
  把 ``compare`` 硬编码成 ``None``,周报因此没有上期列、没有环比)。
* **D9 条件 ``in``**:判定在 :func:`~hoteldata.domains.report.engine.eval_condition`
  (算子表里含 ``in``,可达);这里只负责"不通过就跳过并计数"。

★ 消息条数 ≠ 审计行数(段2 契约修正,2026-09-30)
=============================================

旧 ``report_push._merge_kept``(``report_push.py:360-380``)的真实语义是:

  * 把该群**当日全部命中的 (店 × 报告项) section** 用 ``\\n\\n\\n`` 连成 **1 条**;
  * 总字数 > ``MERGE_LIMIT_CHARS``(3500)→ **按店对半拆成至多 2 条**;

所以 ``publish_schedule`` 的输出是**每群 1~2 条消息**(不是 22 条)——
这正是 V60「单群日消息 ≤4 条」成立的前提(日报 1 + 报告 ≤2 + 点评分析 1)。

而**去重与审计的粒度仍是 (店, 报告项)**:

  * 去重:组装**之前**逐 ``(chatid, hotel_id, module_{item_id}, slot)`` 过滤
    (旧 ``run_scheduled_push`` 第 424-431 行),过滤后一条不剩 → 该群**不发**;
  * 审计:发完之后按每个 (店, 报告项) 各写一行 ``push_logs``,
    ``push_type = module_{item_id}`` —— 由 ``BuiltMessage.audit_targets`` 承载。

两件事解耦之后,"某个报告项到底发出去没有"仍然查得到。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from loguru import logger

from hoteldata.domains.collect.rotation import get_rotation
from hoteldata.domains.collect.screenshot import Screenshoter
from hoteldata.domains.collect.service import aggregate_daily, build_payload, today_module_shots
from hoteldata.domains.report import daily as daily_mod
from hoteldata.domains.report import render as render_mod
from hoteldata.domains.report.engine import (
    aggregate_with_compare,
    buckets_for_date,
    eval_condition,
    item_windows_for_date,
    pick_value,
    windows_for_push,
)
from hoteldata.domains.report.schedule import ReportItem, get_schedule
from hoteldata.push.audit import PushRecord, slot_of
from hoteldata.push.bindings import BoundHotel
from hoteldata.push.sender import SECTION_SEP, merge_sections, split_message
from hoteldata.push.service import BuiltMessage, PushSummary

__all__ = ["ReportService"]

#: 合并消息的 ``push_type``:一条消息里跨了多个报告项时用它(单项时仍用 ``module_{id}``)
MERGED_PUSH_TYPE = "report_schedule"
#: 实时问答短文本上限(计划书 T2B.5:短回答,不带脚注)
REALTIME_MAX_CHARS = 800


# ---------------------------------------------------------------------------
# 内部值对象
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _ItemData:
    """一项 × 一店 × 一天的取数结果(**service 内部结构**)。"""

    payload: dict[str, Any]
    compare: dict[str, Any]
    window: str
    #: 渲染标题里的「采集日期」:普通项 = 采集日,聚合项 = ``起~止``
    collect_date: str
    note: str = ""
    samples: int = 0


@dataclass(slots=True)
class _Section:
    """一个 (店 × 报告项) 的内容段(**合并与审计的最小单位**)。"""

    md: str
    hotel_id: int
    hotel_name: str
    item_id: str
    item_name: str
    images: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 合并拆分(纯函数,便于离线单测)
# ---------------------------------------------------------------------------


def _group_by_hotel(sections: Sequence[_Section]) -> list[list[_Section]]:
    """按店分组(**保序**)—— 旧 ``_merge_kept`` 的"按店对半"以店为单位。"""
    order: list[list[_Section]] = []
    index: dict[int, int] = {}
    for section in sections:
        if section.hotel_id not in index:
            index[section.hotel_id] = len(order)
            order.append([])
        order[index[section.hotel_id]].append(section)
    return order


def _attribute(sections: Sequence[_Section], parts: Sequence[str]) -> list[list[_Section]]:
    """``split_message`` 的结果按 ``\\n\\n\\n`` 边界切回 (店, 项) 段(**审计归属**用)。

    段边界计数只在 ``split_message`` 的"按段对半"分支上精确;硬切兜底时会把边界
    落在段内部,此时仍按"前面整段算上一部分"归属 —— **宁可审计多算一段,不可漏算**。
    """
    if len(parts) <= 1:
        return [list(sections)]
    head = min(parts[0].count(SECTION_SEP) + 1, len(sections))
    return [list(sections[:head]), list(sections[head:])]


def paginate_sections(sections: Sequence[_Section], limit: int) -> list[list[_Section]]:
    """该群当日全部 section → **1~2 条**消息(A2-6;旧 ``report_push._merge_kept``)。

    * 合计 ≤ ``limit`` → 一条;
    * 超限 → **按店对半**拆(前半数店 / 后半数店),再各自用
      :func:`~hoteldata.push.sender.split_message` 兜底(不截断、不丢内容);
    * 极端情况(两半各自仍超限)多出的段**并入最后一条**,保证条数 ≤2。
    """
    if not sections:
        return []
    content = merge_sections([s.md for s in sections])
    if len(content) <= limit:
        return [list(sections)]

    groups = _group_by_hotel(sections)
    if len(groups) <= 1:
        return _attribute(sections, split_message(content, limit=limit, max_parts=2))

    mid = (len(groups) + 1) // 2
    out: list[list[_Section]] = []
    for group in (groups[:mid], groups[mid:]):
        entries = [section for chunk in group for section in chunk]
        merged = merge_sections([s.md for s in entries])
        if len(merged) <= limit:
            out.append(entries)
        else:
            out.extend(_attribute(entries, split_message(merged, limit=limit, max_parts=2)))
    if len(out) > 2:
        out = [out[0], [section for chunk in out[1:] for section in chunk]]
    return out


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------


class ReportService:
    """报告域的唯一入口(日报 / 22 项节奏 / 单跑 / 实时问答)。"""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.settings = runtime.settings
        #: 现场截图(**类型二** ``{shot:true,...}``)的进程内缓存,键 = ``(name, hotel_id, date)``
        #: —— 旧 ``report_push._FP_CACHE``(``report_push.py:177``)同款:同一项同一店同日只截一次
        self._shot_cache: dict[tuple[str, int, str], str | None] = {}

    # ==================================================================
    # 基础
    # ==================================================================

    def _today(self) -> date:
        """本地「今天」(★ 统一 ``datetime.now(settings.tzinfo)``,禁止 naive datetime)。"""
        return datetime.now(self.settings.tzinfo).date()

    def _slot(self) -> str:
        """当日时段键(A2-3;优先走 ``PushService``,没有则用 ``audit.slot_of``)。"""
        push = getattr(self.runtime, "push", None)
        if push is not None and hasattr(push, "current_slot"):
            return str(push.current_slot())
        return slot_of()

    @property
    def _push(self) -> Any:
        return self.runtime.push

    # ==================================================================
    # T2C.1 / T2C.2 日报
    # ==================================================================

    async def build_daily_message(
        self, chatid: str, *, price_section: str | None = None
    ) -> BuiltMessage | None:
        """某群日报(**只有组装,不发送**)。群未绑定酒店 → ``None``。"""
        return await daily_mod.build_daily_message(
            self.runtime, chatid, price_section=price_section, day=self._today()
        )

    async def publish_daily(self, *, force: bool = False) -> dict[str, Any]:
        """``push.daily`` 任务入口:逐群组装日报 → 入队(A2-5 一群多店 1 条)。

        投递(限频 / 重试 / 审计 / slot 去重)全部归 ``push/`` 层;
        这里只组装 + 入队,异常**逐群隔离**(一个群炸了不影响其它群)。
        """
        summary = PushSummary(push_type="daily_report")
        groups = await self.runtime.bindings.grouped()
        summary.groups = len(groups)
        if not groups:
            logger.warning("日报推送:没有任何群绑定酒店(core_group_bindings 为空)")
            return summary.as_dict()

        day = self._today()
        for chatid in groups:
            try:
                msg = await self.build_daily_message(chatid)
            except Exception as exc:  # noqa: BLE001 - 单群失败不拖垮其它群
                logger.exception("日报组装失败(群 {}): {}", chatid, exc)
                summary.errors.append(f"{chatid}: {exc}")
                summary.failed += 1
                continue
            if msg is None:
                summary.empty += 1
                continue
            await self._push.push(msg, force=force)
            summary.enqueued += 1
            logger.info("日报已入队:群={} 店={} 图={} 日={}", chatid, len(msg.hotel_ids), len(msg.images), day)
        return summary.as_dict()

    # ==================================================================
    # T2D.6 22 项节奏
    # ==================================================================

    async def publish_schedule(self, *, day: date | None = None, force: bool = False) -> dict[str, Any]:
        """``push.schedule`` 任务入口:22 项按节奏 → **每群 1~2 条消息**。

        流程(计划书 §5.6 / 批次 D):

        1. :func:`buckets_for_date` 定桶;
        2. 每项 :func:`item_windows_for_date` 为空 → 跳过(当日不发);
        3. 逐店逐窗口取数(**实时优先昨日**,首个有数据即止;周期窗口兜底);
        4. ``svc_weekly`` 走 :meth:`_aggregate_data`(★ D10 上期对比);
        5. :func:`eval_condition` 不通过 → 跳过并计数;``pick_rule`` 取值优先;
        6. 渲染;``images`` 两种类型解析(★ 类型二现场截图,进程内缓存);
        7. ★ 配了 ``images`` 却 0 张 → **整项不发**,最后向管理群告警(V41);
        8. 单条 ≤ ``push.merge_limit_chars``,超限按店对半拆 ≤2 条;
        9. 按群组装,入队(``dedup_checked=True`` —— 去重已在上游逐项做过)。
        """
        target = day or self._today()
        schedule = get_schedule()
        groups = await self.runtime.bindings.grouped()
        slot = self._slot()
        stats: dict[str, Any] = {
            "items": len(schedule.items),
            "buckets": buckets_for_date(target),
            "sections": 0,
            "no_data": 0,
            "condition": 0,
            "no_image": 0,
            "deduped": 0,
            "messages": 0,
            "no_image_details": [],
        }
        summary = PushSummary(push_type=MERGED_PUSH_TYPE)
        summary.groups = len(groups)
        if not groups:
            logger.warning("报告节奏推送:没有任何群绑定酒店")
            return {**summary.as_dict(), "day": target.isoformat(), "stats": stats}

        for chatid, hotels in groups.items():
            sections: list[_Section] = []
            deduped: list[tuple[int, str]] = []
            for hotel in hotels:
                for item in schedule.items:
                    if not item_windows_for_date(item, target):
                        continue  # 当日不发(桶裁剪 / 周一让位)
                    push_type = f"module_{item.id}"
                    if not force and await self._already_pushed(chatid, hotel.hotel_id, push_type, slot):
                        deduped.append((hotel.hotel_id, item.id))
                        stats["deduped"] += 1
                        continue
                    section, reason = await self._build_section(item, hotel, target)
                    if section is None:
                        stats[reason] = stats.get(reason, 0) + 1
                        if reason == "no_image":
                            stats["no_image_details"].append(f"{hotel.name}·{item.name}")
                        continue
                    stats["sections"] += 1
                    sections.append(section)
            if deduped:
                await self._audit_skipped(chatid, deduped, slot)
            if not sections:
                continue
            summary.enqueued += await self._enqueue_sections(chatid, sections, force=force)

        if stats["no_image"]:
            await self._alert_missing_images(stats)
        summary.skipped = int(stats["deduped"])
        stats["messages"] = summary.enqueued
        out = {**summary.as_dict(), "day": target.isoformat(), "stats": stats}
        logger.info(
            "报告节奏推送完成:日={} 桶={} 群={} section={} 消息={} 缺图未发={}",
            target,
            stats["buckets"],
            len(groups),
            stats["sections"],
            summary.enqueued,
            stats["no_image"],
        )
        return out

    async def run_item(
        self, item_id: str, *, force: bool = True, hotel_id: int | None = None
    ) -> dict[str, Any]:
        """**单跑一项**(CLI ``report run``):只推这一项,返回 ``{item, groups, ok, detail}``。

        * 不走桶裁剪(``ignore_bucket=True``):周三也能跑 ``svc_weekly``(CLI 调试语义);
        * ``force=True``(缺省)→ 忽略当日去重,直接重推;
        * ``hotel_id`` 指定时只跑该店。
        """
        schedule = get_schedule()
        item = schedule.by_id(item_id)
        if item is None:
            return {
                "item": item_id,
                "groups": 0,
                "ok": False,
                "detail": {"error": f"未知报告项 {item_id!r}", "known": schedule.ids()},
            }
        groups = await self.runtime.bindings.grouped()
        if hotel_id is not None:
            groups = {
                chatid: [h for h in hotels if int(h.hotel_id) == int(hotel_id)]
                for chatid, hotels in groups.items()
            }
            groups = {chatid: hotels for chatid, hotels in groups.items() if hotels}

        target = self._today()
        slot = self._slot()
        stats: dict[str, Any] = {"sections": 0, "no_data": 0, "condition": 0, "no_image": 0, "deduped": 0}
        summary = PushSummary(push_type=f"module_{item.id}")
        summary.groups = len(groups)
        for chatid, hotels in groups.items():
            sections: list[_Section] = []
            for hotel in hotels:
                if not force and await self._already_pushed(chatid, hotel.hotel_id, f"module_{item.id}", slot):
                    stats["deduped"] += 1
                    continue
                section, reason = await self._build_section(item, hotel, target, ignore_bucket=True)
                if section is None:
                    stats[reason] = stats.get(reason, 0) + 1
                    continue
                stats["sections"] += 1
                sections.append(section)
            if sections:
                summary.enqueued += await self._enqueue_sections(chatid, sections, force=force)
        summary.skipped = int(stats["deduped"])
        stats["messages"] = summary.enqueued
        return {
            "item": item.id,
            "groups": len(groups),
            "ok": bool(summary.enqueued),
            "detail": {**summary.as_dict(), "stats": stats, "day": target.isoformat()},
        }

    # ==================================================================
    # 实时问答(T2B.5 的报告域部分)
    # ==================================================================

    def match_realtime_item(self, question: str) -> ReportItem | None:
        """问题命中哪个 ``realtime_ask`` 项(命中 ``name`` 或 ``module``,**最长优先**)。"""
        text = str(question or "")
        if not text:
            return None
        hit: ReportItem | None = None
        hit_len = 0
        for item in get_schedule().realtime_items():
            for token in (item.name, item.module):
                if token and str(token) in text and len(str(token)) > hit_len:
                    hit, hit_len = item, len(str(token))
        return hit

    async def realtime_answer(self, question: str, chatid: str) -> str | None:
        """实时问答(**问才发**,仅群聊):命中项 → 该店当日数据 → 短文本(≤800 字,无脚注)。

        * ``chatid`` 为空(单聊/未知来源)→ ``None``,不回答;
        * 一群多店:问题里出现店名就用那家店,否则用第一家(旧口径:群即店);
        * 命中项但当日无数据 → ``None``(调用方走 FAQ / 兜底,**不编数据**)。
        """
        if not chatid or not question:
            return None
        item = self.match_realtime_item(question)
        if item is None:
            return None
        hotels = await self.runtime.bindings.for_group(chatid)
        if not hotels:
            logger.info("实时问答跳过:群 {} 未绑定酒店", chatid)
            return None
        hotel = self._pick_hotel(hotels, question)
        data = await self._item_data(item, hotel, self._today(), prefer_realtime=True)
        if data is None or not data.payload:
            logger.info("实时问答无数据:群={} 店={} 项={}", chatid, hotel.name, item.id)
            return None
        return self._short_text(item, hotel, data)

    def _pick_hotel(self, hotels: Sequence[BoundHotel], question: str) -> BoundHotel:
        for hotel in hotels:
            if hotel.name and hotel.name in str(question):
                return hotel
        return hotels[0]

    def _short_text(self, item: ReportItem, hotel: BoundHotel, data: _ItemData) -> str:
        """短回答:标题 + 指标表(**不含脚注**)+ ≤:data:`REALTIME_MAX_CHARS` 字。"""
        title = f"【{hotel.name}】{item.name} · {data.window or item.prime_window()}"
        body = render_mod.scalar_table(
            data.payload,
            compare=data.compare or None,
            max_rows=render_mod.DEFAULT_MAX_ROWS,
        )
        text = f"{title}\n\n{body}"
        list_key = render_mod.first_list_key(data.payload)
        if list_key:
            section = render_mod.list_section(data.payload, list_key, max_rows=5)
            if section:
                text += "\n\n" + section
        if len(text) > REALTIME_MAX_CHARS:
            cut = text.rfind("\n", 0, REALTIME_MAX_CHARS)
            text = text[: cut if cut > 0 else REALTIME_MAX_CHARS]
        return text.strip()

    # ==================================================================
    # 轮换预览
    # ==================================================================

    def rotation_preview(self, day: date | None = None) -> dict[str, Any]:
        """当日轮换预览(段1 ``push_rotation.json`` 同一份清单,不查库)。"""
        target = day or self._today()
        plan = get_rotation().pick(target)
        out = dict(plan.as_dict())
        out["names"] = plan.names
        out["alert_names"] = plan.alert_names
        out["fullpage_names"] = plan.fullpage_names
        out["module_names"] = plan.module_names
        return out

    # ==================================================================
    # 取数(★ 只走段1 契约)
    # ==================================================================

    async def _item_data(
        self,
        item: ReportItem,
        hotel: BoundHotel,
        day: date,
        *,
        ignore_bucket: bool = False,
        prefer_realtime: bool = False,
    ) -> _ItemData | None:
        """一项 × 一店 × 一天的取数。无数据 → ``None``(调用方记 ``no_data``)。

        * **聚合项**(``aggregate``)走 :meth:`_aggregate_data`(★ D10);
        * 普通项按 :func:`windows_for_push` 顺序逐窗口 :func:`build_payload`
          (**实时优先昨日**,首个有数据即止);
        * ``prefer_realtime`` → 单次 ``window=None`` 取数,由段1 的
          ``fetch_module_record`` 做"实时优先昨日"择优(问答场景)。
        """
        if item.aggregate:
            return await self._aggregate_data(item, hotel, day)
        windows = windows_for_push(item, day, ignore_bucket=ignore_bucket)
        if not windows:
            return None
        if item.on_demand:
            logger.info(
                "项 {} 标记 on_demand,但段2 无按需采集契约(段1 只提供读接口),按当日记录取数",
                item.id,
            )
        page = item.page
        module = item.module_name
        for window in [None] if prefer_realtime else windows:
            payload = await self._build_payload(hotel.hotel_id, day, page, module, window)
            if str(payload.get("status")) == "no_data" or not payload.get("indicators"):
                continue
            compare = payload.get("compare")
            return _ItemData(
                payload=dict(payload.get("indicators") or {}),
                compare=dict(compare) if isinstance(compare, Mapping) else {},
                window=str(payload.get("window") or window or ""),
                collect_date=day.isoformat(),
                note=f"环比对比日 {payload['prev_date']}" if payload.get("prev_date") else "",
            )
        return None

    async def _build_payload(
        self, hotel_id: int, day: date, page: str, module: str, window: str | None
    ) -> dict[str, Any]:
        async with self.runtime.db.session() as session:
            return await build_payload(
                session, hotel_id, day, page, module, window, with_compare=True
            )

    async def _aggregate_data(self, item: ReportItem, hotel: BoundHotel, day: date) -> _ItemData | None:
        """★ **D10**:周报 = 本期聚合 + **上一期**聚合 → ``payload`` + ``compare``。

        本期 = ``aggregate_daily(end_date=day-1, days=N)``;
        上期 = ``aggregate_daily(end_date=day-N-1, days=N)``(再往前 N 天)。
        旧系统周报这条路径的 ``compare`` 被硬编码成 ``None``(``report_push.py:154``),
        于是"服务概览(周报)"永远只有本期列。
        """
        spec = item.aggregate or {}
        days = item.aggregate_days
        page = item.page
        module = item.module_name
        window = item.prime_window()
        end = day - timedelta(days=1)
        start = end - timedelta(days=days - 1)
        prev_end = end - timedelta(days=days)
        kwargs = {
            "page": page,
            "module": module,
            "window": window,
            "days": days,
            "sum_fields": item.aggregate_fields("sum"),
            "avg_fields": item.aggregate_fields("avg"),
            "last_fields": item.aggregate_fields("last"),
        }
        async with self.runtime.db.session() as session:
            current = await aggregate_daily(session, hotel.hotel_id, end, **kwargs)
            previous = await aggregate_daily(session, hotel.hotel_id, prev_end, **kwargs)
        combined = aggregate_with_compare(
            current.as_dict(), previous.as_dict(), spec=spec, start=start, end=end, days=days
        )
        if not combined["payload"]:
            return None
        span = combined["range"]
        return _ItemData(
            payload=dict(combined["payload"]),
            compare=dict(combined["compare"]),
            window=window,
            collect_date=span,
            note=f"近{days}天聚合({combined['samples']} 天样本)" if combined["samples"] else "",
            samples=int(combined["samples"]),
        )

    # ==================================================================
    # 单段组装(渲染 + 图文绑定)
    # ==================================================================

    async def _build_section(
        self,
        item: ReportItem,
        hotel: BoundHotel,
        day: date,
        *,
        ignore_bucket: bool = False,
    ) -> tuple[_Section | None, str]:
        """一项 × 一店 → ``(_Section, "ok")`` 或 ``(None, 原因)``。

        原因取值:``no_data`` / ``condition`` / ``no_image``(★ V41)。
        """
        data = await self._item_data(item, hotel, day, ignore_bucket=ignore_bucket)
        if data is None:
            logger.info("报告项无数据:{}·{}", hotel.name, item.name)
            return None, "no_data"

        payload = dict(data.payload)
        if item.condition and not eval_condition(item.condition, payload):
            logger.info("报告项条件不通过:{}·{}(condition={})", hotel.name, item.name, item.condition)
            return None, "condition"

        note = data.note
        if item.pick_rule:
            payload, pick_note = pick_value(item, payload)
            note = " ".join(x for x in (note, pick_note) if x)

        images = await self._images_for(item, hotel, day)
        if item.images and not images:
            # ★ V41:配了图却一张都没有 → **整条不发**(绝不发"半条")
            logger.warning(
                "图文绑定拦截:{}·{} 配了 {} 组 images 但 0 张成功,该项不发",
                hotel.name,
                item.name,
                len(item.images),
            )
            return None, "no_image"

        record = {
            "payload": payload,
            "window": data.window,
            "collect_date": data.collect_date,
            "created_at": data.collect_date,
        }
        markdown = render_mod.render_item(item, record, data.compare or None, hotel)
        section_md = f"### 「{hotel.name}」\n{markdown}"
        if note:
            section_md += "\n\n" + note
        return (
            _Section(
                md=section_md,
                hotel_id=int(hotel.hotel_id),
                hotel_name=hotel.name,
                item_id=item.id,
                item_name=item.name,
                images=images,
            ),
            "ok",
        )

    # ==================================================================
    # 图文绑定(T2C.2 / V41)
    # ==================================================================

    async def _images_for(self, item: ReportItem, hotel: BoundHotel, day: date) -> list[str]:
        """按 ``item.images`` 解析配图(两种类型;旧 ``report_push.py:180-224`` + ``renderer.py:258-295``)。

        * **类型一** ``{"page","module"}`` → 当日模块截图,**去重后最多 2 张**;
          全缺 → 用整页 ``screenshot_path`` 兜底(**≤1 张**);
        * **类型二** ``{"shot":true,"url","selector","click"}`` → 现场截图
          :meth:`Screenshoter.shot_url`,按 ``(name, hotel, date)`` **进程内缓存**;
        * 上限 ``settings.push.max_images``(发送层还会再截一次,这里先截一次是为了
          让"截了多少张"在日志里可见)。
        """
        specs = [s for s in (item.images or []) if isinstance(s, Mapping)]
        if not specs:
            return []
        page_specs = [s for s in specs if s.get("page") and s.get("module")]
        shot_specs = [s for s in specs if s.get("shot")]

        found: list[str] = []
        for spec in page_specs:
            if len(found) >= 2:  # 类型一上限 2 张
                break
            hit = await self._module_shot(hotel.hotel_id, day, str(spec["page"]), str(spec["module"]))
            if hit and hit not in found:
                found.append(hit)
        if not found and page_specs:
            fallback = await self._page_shot(hotel.hotel_id, day, str(page_specs[0]["page"]))
            if fallback:
                found.append(fallback)
        for spec in shot_specs:
            path = await self._onsite_shot(spec, hotel, day)
            if path and path not in found:
                found.append(path)

        limit = max(1, int(self.settings.push.max_images))
        if len(found) > limit:
            logger.warning(
                "报告项 {}·{} 附图 {} 张超过上限 {},已截断", hotel.name, item.name, len(found), limit
            )
            found = found[:limit]
        return found

    async def _module_shot(self, hotel_id: int, day: date, page: str, module: str) -> str | None:
        """类型一取图:段1 ``today_module_shots`` → ``ModuleShots.pick(module)``。"""
        try:
            async with self.runtime.db.session() as session:
                groups = await today_module_shots(session, hotel_id, day, page=page)
        except Exception as exc:  # noqa: BLE001 - 取图失败 = 无图(V41 会拦住该项)
            logger.warning("模块截图读取失败({} {} {}): {}", hotel_id, day, page, exc)
            return None
        for group in groups:
            hit = group.pick(module)
            if hit:
                return hit
        return None

    async def _page_shot(self, hotel_id: int, day: date, page: str) -> str | None:
        """整页截图兜底(类型一全缺时 ≤1 张;旧 ``renderer.resolve_item_images`` 末段)。"""
        try:
            async with self.runtime.db.session() as session:
                groups = await today_module_shots(session, hotel_id, day, page=page)
        except Exception as exc:  # noqa: BLE001
            logger.warning("整页截图读取失败({} {} {}): {}", hotel_id, day, page, exc)
            return None
        for group in groups:
            if group.screenshot_path:
                return group.screenshot_path
        return None

    async def _onsite_shot(self, spec: Mapping[str, Any], hotel: BoundHotel, day: date) -> str | None:
        """类型二现场截图(**失败返回 None,不抛**;按 ``(name, hotel, date)`` 缓存)。"""
        name = str(spec.get("name") or spec.get("selector") or "shot")
        url = str(spec.get("url") or "")
        if not url:
            logger.warning("现场截图条目缺少 url:{}·{}", hotel.name, name)
            return None
        cache_key = (name, int(hotel.hotel_id), day.isoformat())
        if cache_key in self._shot_cache:
            return self._shot_cache[cache_key]
        path: str | None = None
        try:
            context = await self._extract_context(hotel.hotel_id, day)
            if context is None:
                logger.warning("现场截图跳过(无账号/无酒店):{}·{}", hotel.name, name)
            else:
                await self._ensure_browser()
                shooter = Screenshoter(
                    settings=self.settings, rules=self.runtime.rules, pool=self.runtime.browser
                )
                path = await shooter.shot_url(
                    context,
                    url,
                    name,
                    selector=spec.get("selector") or None,
                    click=spec.get("click") or None,
                )
        except Exception as exc:  # noqa: BLE001 - 附图失败只告警(段1 §5.7 口径)
            logger.warning("现场截图失败({}·{}): {}", hotel.name, name, exc)
            path = None
        self._shot_cache[cache_key] = path
        return path

    async def _ensure_browser(self) -> None:
        try:
            await self.runtime.start_browser()
        except Exception as exc:  # noqa: BLE001 - 起不来就算了,截图会自己失败
            logger.error("浏览器池启动失败,现场截图将不可用: {}", exc)

    async def _extract_context(self, hotel_id: int, day: date) -> Any | None:
        """构造现场截图需要的 ``ExtractContext``(★ 由 ``Runtime.extract_context`` 造)。

        需要 ``Hotel`` / ``Account`` 的 ORM 行(``extract_context`` 的入参形状),
        所以在这里用**短会话**取一次;账号缺失回退"首个活跃账号"(旧口径)。
        """
        from sqlalchemy import select

        from hoteldata.infra.models import Account, Hotel

        async with self.runtime.db.session() as session:
            hotel = await session.get(Hotel, int(hotel_id))
            if hotel is None:
                return None
            account = await session.get(Account, hotel.account_id) if hotel.account_id else None
            if account is None:
                account = (
                    await session.execute(
                        select(Account).where(Account.status == "active").order_by(Account.id).limit(1)
                    )
                ).scalars().first()
        if account is None:
            return None
        return self.runtime.extract_context(hotel, account, day)

    # ==================================================================
    # 入队 / 去重 / 告警
    # ==================================================================

    async def _enqueue_sections(self, chatid: str, sections: list[_Section], *, force: bool) -> int:
        """把该群的 sections 合并成 1~2 条消息入队,返回消息条数。

        ``dedup_checked=True``:去重已在组装前逐 ``(店, 项)`` 做过,
        所以不让派发器再按 ``push_type`` 查一次库(否则合并消息会被误判重复)。
        """
        limit = int(self.settings.push.merge_limit_chars)
        max_images = int(self.settings.push.max_images)
        parts = paginate_sections(sections, limit)
        sent = 0
        for entries in parts:
            if not entries:
                continue
            item_ids = {s.item_id for s in entries}
            push_type = f"module_{next(iter(item_ids))}" if len(item_ids) == 1 else MERGED_PUSH_TYPE
            images = [img for s in entries for img in s.images][:max_images]
            msg = BuiltMessage(
                chatid=chatid,
                push_type=push_type,
                content=merge_sections([s.md for s in entries]),
                images=tuple(images),
                hotel_ids=tuple(dict.fromkeys(s.hotel_id for s in entries)),
                audit_targets=tuple((s.hotel_id, f"module_{s.item_id}") for s in entries),
                note=f"{len(entries)} 项报告合并" if len(entries) > 1 else entries[0].item_name,
            )
            await self._push.dispatcher.enqueue(msg.to_task(force=force, dedup_checked=True))
            sent += 1
        logger.info("报告节奏入队:群={} 段={} 条={}", chatid, len(sections), sent)
        return sent

    async def _already_pushed(self, chatid: str, hotel_id: int, push_type: str, slot: str) -> bool:
        """当日该时段 ``(群, 店, 类型)`` 是否已成功推送过(A2-3 / V37)。"""
        try:
            return bool(
                await self._push.audit.pushed_ok(chatid, int(hotel_id), push_type, slot)
            )
        except Exception as exc:  # noqa: BLE001 - 查库失败按"没推过"处理(宁可重发,不可漏发)
            logger.warning("去重查询失败({} {} {}): {}", chatid, hotel_id, push_type, exc)
            return False

    async def _audit_skipped(self, chatid: str, pairs: Sequence[tuple[int, str]], slot: str) -> None:
        """去重命中的 (店, 项) 写一行 ``skipped`` 审计 —— **命中留痕,绝不静默**。"""
        try:
            await self._push.audit.write_many(
                [
                    PushRecord(
                        group_chatid=chatid,
                        hotel_id=hotel_id,
                        push_type=f"module_{item_id}",
                        status="skipped",
                        bot_id="report",
                        slot=slot,
                        content_preview="当日该时段已推送(status=ok),去重跳过",
                    )
                    for hotel_id, item_id in pairs
                ]
            )
        except Exception as exc:  # noqa: BLE001 - 审计失败不影响主流程
            logger.error("写 skipped 审计失败: {}", exc)

    async def _alert_missing_images(self, stats: Mapping[str, Any]) -> None:
        """★ V41:向**管理群**告警「缺图未发 N 条」(缺图不是错误,是"跳过 + 告知")。"""
        details = [str(x) for x in (stats.get("no_image_details") or [])][:5]
        text = (
            f"⚠️ 缺图未发 {stats.get('no_image')} 条"
            "(配图项无当日截图,图文绑定已拦截):" + "；".join(details)
        )
        try:
            await self._push.send_alert(text)
        except Exception as exc:  # noqa: BLE001 - D1:告警失败要可见,不能吞
            logger.error("缺图告警发送失败: {}", exc)
