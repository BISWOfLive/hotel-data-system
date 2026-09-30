"""★ 推送服务层(T2A.8 末)—— **对段3 的推送契约**。

段3(比价)只需要这四件事,不需要知道派发器/限频/审计的存在:

======================================  ==================================================
能力                                     方法
======================================  ==================================================
把一个群消息推出去                        :meth:`PushService.push`
往群里补一段内容(比价段并入日报)          :meth:`PushService.push_daily` 的 ``price_section``
发运维告警(**修 D1**)                    :meth:`PushService.send_alert`
查某群今天推过什么                        :meth:`PushService.group_today`
======================================  ==================================================

★ **内容由域组装,投递由本层负责**
====================================

``push/`` **不 import 任何 domain**。日报/报告项/预警/点评的**内容组装**
由各自域完成,组装结果用 :class:`BuiltMessage` 交给本层。

之所以这样切:旧系统 ``app/pusher.py`` 既组装内容又投递又读表又调采集器,
647 行里没有一处能单独测试(段2 §1.3 明确禁止段2 重蹈覆辙)。

★ **``send_alert`` 是 D1 的对外出口**
====================================

段2 的头号必修项(风险 P3:"出事了没人知道")。本方法:

  1. **走 ``BotManager``,不依赖单例** —— 没有"``_BOT`` 是 ``None`` 就返回 False"的路径;
  2. 目标群默认取 ``MANAGE_CHATIDS`` + ``OPS_CHATID``(去重);
  3. 返回 :class:`~hoteldata.domains.bot.manager.AlertResult`(**逐群结果**),
     **绝不返回裸 bool**;一台机器人都不在线时也返回**带 error 的结果**;
  4. 失败**写日志且可见**(``logger.error``),不是 ``logger.warning`` 一笔带过。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from loguru import logger

from hoteldata.domains.bot.manager import AlertResult, BotManager
from hoteldata.push.audit import PushAudit, slot_of
from hoteldata.push.bindings import Bindings, BoundHotel
from hoteldata.push.dispatcher import PushDispatcher, PushTask
from hoteldata.push.sender import Delivery, Sender
from hoteldata.settings import Settings, get_settings

__all__ = [
    "BuiltMessage",
    "PushService",
    "PushSummary",
]

#: 日报组装回调(段2 由 ``domains/report`` 注入;段3 可以再包一层加比价段)。
#:
#: 签名 ``(chatid, price_section) -> BuiltMessage | None`` —— ★ ``price_section``
#: 是**段3 的显式钩子**(计划书 §1.4):段2 阶段恒 ``None``,
#: 段3 实现 ``domains/compare/service.py::build_price_section()`` 后传进来,
#: **不必回头改日报组装代码**。
DailyBuilder = Callable[[str, "str | None"], Awaitable["BuiltMessage | None"]]


@dataclass(slots=True)
class BuiltMessage:
    """一个**已组装好**的群消息。"""

    chatid: str
    push_type: str
    content: str
    images: tuple[str, ...] = ()
    hotel_ids: tuple[int, ...] = ()
    note: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    #: ★ 审计行 ≠ 消息条数(见 :class:`~hoteldata.push.dispatcher.PushTask`)
    audit_targets: tuple[tuple[int | None, str], ...] = ()

    def to_task(self, *, force: bool = False, dedup_checked: bool = False) -> PushTask:
        return PushTask(
            chatid=self.chatid,
            push_type=self.push_type,
            content=self.content,
            images=tuple(self.images),
            hotel_ids=tuple(self.hotel_ids),
            force=force,
            dedup_checked=dedup_checked,
            note=self.note,
            extra=dict(self.meta),
            audit_targets=tuple(self.audit_targets),
        )


@dataclass(slots=True)
class PushSummary:
    """一次"多群推送"的汇总(日报/报告项任务返回它)。"""

    push_type: str = ""
    groups: int = 0
    enqueued: int = 0
    skipped: int = 0
    empty: int = 0
    delivered: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "push_type": self.push_type,
            "groups": self.groups,
            "enqueued": self.enqueued,
            "skipped": self.skipped,
            "empty": self.empty,
            "delivered": self.delivered,
            "failed": self.failed,
        }
        if self.errors:
            out["errors"] = self.errors[:10]
        return out


class PushService:
    """推送的唯一对外出口。"""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        manager: BotManager | None = None,
        sender: Sender | None = None,
        dispatcher: PushDispatcher | None = None,
        audit: PushAudit,
        bindings: Bindings,
    ) -> None:
        self.settings: Settings = settings or get_settings()
        self.manager = manager
        self.audit = audit
        self.bindings = bindings
        self.sender = sender or Sender(
            manager,
            max_images=self.settings.push.max_images,
            limit_chars=self.settings.push.merge_limit_chars,
        )
        push_cfg = self.settings.push
        self.dispatcher = dispatcher or PushDispatcher(
            self.sender,
            audit,
            min_interval_s=push_cfg.min_interval_s,
            group_min_interval_s=self.settings.push_group_min_interval_s,
            retry_times=push_cfg.retry_times,
            retry_backoff_s=push_cfg.retry_backoff_s,
        )
        #: 日报组装器(由 ``Runtime`` 注入 ``domains/report`` 的实现)
        self.daily_builder: DailyBuilder | None = None

    # ==================================================================
    # 生命周期
    # ==================================================================

    async def start(self) -> None:
        await self.dispatcher.start()

    async def stop(self) -> None:
        await self.dispatcher.stop()

    # ==================================================================
    # 推送
    # ==================================================================

    async def push(self, msg: BuiltMessage, *, force: bool = False, now: bool = False) -> Delivery | None:
        """推一条群消息。

        ``now=False`` → 入队(限频 + 重试由 worker 做),返回 ``None``;
        ``now=True``  → **同步投递**并返回结果(命令「重推」与 CLI 用)。
        """
        task = msg.to_task(force=force)
        if now:
            return await self.dispatcher.deliver_now(task)
        await self.dispatcher.enqueue(task)
        return None

    async def push_many(
        self, messages: list[BuiltMessage], *, force: bool = False, summary: PushSummary | None = None
    ) -> PushSummary:
        """批量入队(日报 / 22 项报告)。"""
        out = summary or PushSummary()
        for msg in messages:
            await self.dispatcher.enqueue(msg.to_task(force=force))
            out.enqueued += 1
        return out

    async def push_daily(
        self, chatid: str, *, force: bool = False, price_section: str | None = None
    ) -> Delivery | None:
        """★ 推某群的日报(**段3 的比价段从这里注入**)。

        * 群**没绑定酒店** / 组装不出内容 → ``None``(调用方据此回"暂无数据");
        * **组装器未注入**(没走 ``Runtime.start_push()``)→ ``Delivery(ok=False, error=…)``
          —— 带原因的失败,不许伪装成"无数据";
        * ``price_section=None`` 是段2 的正常形态;段3 实现
          ``domains/compare/service.py::build_price_section()`` 后传进来
          (计划书 §1.4 的显式钩子)。
        """
        if self.daily_builder is None:
            # ★ 返回**带原因的失败**而不是 ``None``:
            #   ``None`` 在命令链里被解释成"无数据",会把"装配漏了"伪装成"这家店今天没数据"
            #   —— 又一个静默失败。调用方(「今日数据」)现在能回出真实原因。
            logger.error("日报组装器未注入(需先 Runtime.start_push()):群={}", chatid)
            return Delivery(ok=False, error="日报组装器未注入(push 未启动)")
        msg = await self.daily_builder(chatid, price_section)
        if msg is None:
            return None
        # ★ 组装器已经把 price_section 拼在段末(计划书 §1.4 指定的位置);
        #   这里只在**组装器没接钩子**时补一次,避免拼两遍。
        if price_section and price_section not in msg.content:
            msg.content = f"{msg.content}\n\n{price_section}"
        return await self.push(msg, force=force, now=True)

    # ==================================================================
    # ★ 告警(D1 修复)
    # ==================================================================

    def alert_targets(self, chatids: list[str] | None = None) -> list[str]:
        """告警目标群:显式传入 > ``MANAGE_CHATIDS`` + ``OPS_CHATID``(去重保序)。"""
        if chatids:
            return _dedupe(chatids)
        push_cfg = self.settings.push
        targets = list(push_cfg.manage_chatids)
        if push_cfg.ops_chatid:
            targets.append(push_cfg.ops_chatid)
        return _dedupe(targets)

    async def send_alert(self, text: str, *, chatids: list[str] | None = None) -> AlertResult:
        """★ **修 D1**:运维告警走 ``BotManager`` 遍历在线机器人,返回**逐群结果**。

        没有配置任何目标群 → ``ok=False`` + ``error``(调用方写日志/审计),
        **不是**旧系统那种 ``return False`` 一笔带过。
        """
        targets = self.alert_targets(chatids)
        if self.manager is None:
            result = AlertResult(text=text, targets=targets, error="BotManager 未装配")
            logger.error("告警未发送:{} | {}", result.error, text.replace("\n", " ")[:120])
            return result
        result = await self.manager.send_alert(text, chatids=targets)
        if not result.ok:
            logger.error(
                "告警未送达任何目标群:{} | targets={} | {}",
                result.error or "全部失败",
                targets,
                text.replace("\n", " ")[:120],
            )
        return result

    # ==================================================================
    # 查询
    # ==================================================================

    async def group_today(self, chatid: str, day: date | None = None) -> list[Any]:
        """某群今天的推送审计行。"""
        return await self.audit.list_logs(day=day or date.today(), group_chatid=chatid, limit=50)

    async def hotels_of_group(self, chatid: str) -> list[BoundHotel]:
        return await self.bindings.for_group(chatid)

    def current_slot(self) -> str:
        return slot_of()

    def snapshot(self) -> dict[str, Any]:
        return {
            "dispatcher": self.dispatcher.snapshot(),
            "bots": self.manager.snapshot() if self.manager is not None else {"bots": 0, "health": {}},
            "manage_chatids": list(self.settings.push.manage_chatids),
            "ops_chatid": self.settings.push.ops_chatid,
            "daily_builder": self.daily_builder is not None,
        }


def _dedupe(items: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for item in items:
        if item:
            seen.setdefault(item.strip(), None)
    return [k for k in seen if k]
