"""★ 推送派发器(T2A.6)—— **限频 + 重试 + 异步队列**(D5 修复点)。

模型
====

::

    push(chatid, push_type, content, images, force=False)
      │
      ├─ 去重检查:slot = YYYY-MM-DD-HH;查 push_logs 该 slot status='ok'
      │    └─ 已推 且 非 force → 写一行 skipped 审计后跳过
      │
      └─ 入队(asyncio.Queue)→ worker
           │
           ├─ ★ 机器人级限频:同一机器人相邻发送间隔 ≥ 2.0s   (修 D5)
           ├─ 展开为多次尝试;每次:
           │     ├─ 清空上一条失败的**部分成果**计数后重发(见下)
           │     └─ 发送(文本 N 条 + 图片 ≤5 张)
           ├─ 失败 → 退避 [2, 8, 30] 秒重试,共 4 次尝试
           └─ 写 push_logs(bot_id 为文本,修 D17)

★ **D5:限频键必须从 chatid 改成机器人**
======================================

旧系统 ``pusher.py:622-629``::

    with self._lock:
        last = self._last_send.get(chatid, 0)       # ← 键是 chatid
        wait = self.min_interval - (now - last)
        ...
        self._last_send[chatid] = time.time()

类文档写的是"每消费者(机器人侧)发送间最小间隔",**代码写的是 chatid** ——
于是"同一个机器人的 10 个群"可以**同时并发打**,30 机器人分摊的限频**实际失效**。
(旧系统 worker 数 = 机器人数,每个 worker 只对自己发过的群限频,
换个群就是"新键、零等待"。)

新实现按**机器人**限频(键 = ``bot.name``,由 :meth:`BotManager.route` 决定),
**并保留群级二次节流**(键 = chatid)。两道闸门叠加:

  * 机器人级:平台风控看的是"这个机器人打得多快" → 主闸门;
  * 群级:防止多店群在极短时间内被两条不同类型的推送刷屏 → 体验闸门。

★ **重试会重发整条消息**,所以失败后不能只补发图片 —— 那会造成
"文本 1 条 + 图片重复 N 张"。平台的 markdown 发送是幂等的**内容**,不幂等**条数**,
所以重试单位就是"整条群消息"(旧系统 ``_send_with_retry`` 也是这个粒度)。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from loguru import logger

from hoteldata.push.audit import PushAudit, PushRecord, slot_of
from hoteldata.push.sender import Delivery, Sender

__all__ = ["PushDispatcher", "PushTask"]

#: 派发结果回调(任务, 投递结果)
ResultHook = Callable[["PushTask", Delivery], Awaitable[None] | None]


@dataclass(slots=True)
class PushTask:
    """一个待投递的群消息(**内容已组装好**,派发器不关心它从哪来)。"""

    chatid: str
    push_type: str
    content: str
    images: tuple[str, ...] = ()
    #: 审计归属(一群多店 → 每家店一行 ``push_logs``;为空则写一行 ``hotel_id=NULL``)
    hotel_ids: tuple[int, ...] = ()
    force: bool = False
    slot: str = ""
    #: 去重已在上游判定过(命令「重推」/CLI 直推/报告项已逐项过滤)→ 派发器不再查库
    dedup_checked: bool = False
    #: 附加上下文(进 ``content_preview`` 或回调)
    note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    #: ★ **审计行 ≠ 消息条数**。
    #:
    #: 22 项报告是**合并成 1~2 条消息**发出去的(旧 ``report_push._merge_kept``),
    #: 但审计粒度仍然是"每个 (店, 报告项) 一行"(``push_type=module_{id}``)。
    #: 两件事必须解耦,否则"某个报告项到底发出去没有"就查不到了。
    #:
    #: 元素 = ``(hotel_id | None, push_type)``;为空时回退
    #: ``[(h, self.push_type) for h in hotel_ids] or [(None, self.push_type)]``。
    audit_targets: tuple[tuple[int | None, str], ...] = ()

    def effective_slot(self) -> str:
        return self.slot or slot_of()

    def resolved_audit_targets(self) -> list[tuple[int | None, str]]:
        """审计行清单(见 :attr:`audit_targets`)。"""
        if self.audit_targets:
            return list(self.audit_targets)
        if self.hotel_ids:
            return [(h, self.push_type) for h in self.hotel_ids]
        return [(None, self.push_type)]


class PushDispatcher:
    """``asyncio.Queue`` + workers = ``max(1, 在线机器人数)``。

    **不用 Celery/Redis**:推送是 IO 密集,单进程 asyncio 足够;
    加中间件违背"一个进程、一条命令"(段2 §3 / §5.4)。
    """

    def __init__(
        self,
        sender: Sender,
        audit: PushAudit,
        *,
        min_interval_s: float = 2.0,
        group_min_interval_s: float = 2.0,
        retry_times: int = 3,
        retry_backoff_s: tuple[float, ...] = (2.0, 8.0, 30.0),
        workers: int = 0,
        on_result: ResultHook | None = None,
    ) -> None:
        self.sender = sender
        self.audit = audit
        self.min_interval_s = float(min_interval_s)
        self.group_min_interval_s = float(group_min_interval_s)
        self.retry_times = int(retry_times)
        self.retry_backoff_s = tuple(retry_backoff_s) or (2.0, 8.0, 30.0)
        self._workers = int(workers)
        self.on_result = on_result

        self._queue: asyncio.Queue[PushTask] = asyncio.Queue()
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = False
        self._lock = asyncio.Lock()
        #: ★ D5:限频键 = **机器人名**(旧系统是 chatid)
        self._last_send_by_bot: dict[str, float] = {}
        #: 群级二次节流(体验闸门,与机器人级叠加)
        self._last_send_by_group: dict[str, float] = {}
        self.sent = 0
        self.failed = 0
        self.skipped = 0

    # ==================================================================
    # 生命周期
    # ==================================================================

    @property
    def worker_count(self) -> int:
        if self._workers > 0:
            return self._workers
        manager = getattr(self.sender, "manager", None)
        size = manager.size() if manager is not None else 0
        return max(1, size)

    async def start(self) -> None:
        if self._tasks:
            return
        self._stopping = False
        for idx in range(self.worker_count):
            self._tasks.append(asyncio.create_task(self._worker(idx), name=f"push-w-{idx}"))
        logger.info("推送派发器已启动:{} 个 worker", len(self._tasks))

    async def stop(self, *, drain: bool = True, timeout_s: float = 30.0) -> None:
        if drain and not self._queue.empty():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._queue.join(), timeout=timeout_s)
        self._stopping = True
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()

    async def __aenter__(self) -> PushDispatcher:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # ==================================================================
    # 入队
    # ==================================================================

    async def enqueue(self, task: PushTask) -> None:
        await self._queue.put(task)
        logger.debug("推送任务入队:群={} 类型={}", task.chatid, task.push_type)

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    async def drain(self, timeout_s: float = 60.0) -> bool:
        """等队列清空(CLI 直推与验收脚本用)。返回是否在超时前清空。"""
        try:
            await asyncio.wait_for(self._queue.join(), timeout=timeout_s)
            return True
        except TimeoutError:
            return False

    # ==================================================================
    # worker
    # ==================================================================

    async def _worker(self, idx: int) -> None:
        while not self._stopping:
            try:
                task = await self._queue.get()
            except asyncio.CancelledError:
                raise
            try:
                await self._handle(task)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - worker 不许死
                logger.exception("推送 worker {} 异常: {}", idx, exc)
            finally:
                self._queue.task_done()

    async def _handle(self, task: PushTask) -> None:
        """去重 → 限频 → 投递 → 审计。"""
        slot = task.effective_slot()

        # ---- ① 去重(force 可绕;上游已判过则跳过)----
        if not task.force and not task.dedup_checked:
            if await self._is_duplicated(task, slot):
                self.skipped += 1
                return

        # ---- ② 限频 + 投递 ----
        delivery = await self.deliver(task)

        # ---- ③ 审计 ----
        await self._write_audit(task, delivery, slot)
        if delivery.ok:
            self.sent += 1
        else:
            self.failed += 1
        if self.on_result is not None:
            try:
                result = self.on_result(task, delivery)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:  # noqa: BLE001
                logger.warning("推送结果回调异常: {}", exc)

    async def _is_duplicated(self, task: PushTask, slot: str) -> bool:
        """当日去重(V37)。

        一群多店时是**逐店**判定:已推过的店留在 ``skipped`` 审计里,
        **未推过的店继续发**(旧系统在这里是"任一家推过就整群跳过",
        多店群只要有一家店先推成功,其它店当天就永远收不到了)。
        命中留痕,绝不静默。
        """
        hotel_ids: list[int | None] = list(task.hotel_ids) or [None]
        pending: list[int | None] = []
        already: list[int | None] = []
        for hotel_id in hotel_ids:
            if await self.audit.pushed_ok(task.chatid, hotel_id, task.push_type, slot):
                already.append(hotel_id)
            else:
                pending.append(hotel_id)

        if already:
            await self.audit.write_many(
                [
                    PushRecord(
                        group_chatid=task.chatid,
                        hotel_id=hotel_id,
                        push_type=task.push_type,
                        status="skipped",
                        bot_id=self._bot_name(task.chatid),
                        slot=slot,
                        content_preview="当日该时段已推送(status=ok),去重跳过",
                    )
                    for hotel_id in already
                ]
            )
        if not pending:
            logger.info(
                "推送去重命中:群={} 类型={} slot={}(全部 {} 家店已推)",
                task.chatid,
                task.push_type,
                slot,
                len(hotel_ids),
            )
            return True

        task.hotel_ids = tuple(pending)
        if len(pending) != len(hotel_ids):
            logger.info(
                "推送部分去重:群={} 类型={} 跳过 {} 家已推,继续发 {} 家",
                task.chatid,
                task.push_type,
                len(already),
                len(pending),
            )
        return False

    async def deliver(self, task: PushTask) -> Delivery:
        """**限频 + 重试**后投递一条群消息。"""
        attempts = self.retry_times + 1
        last: Delivery = Delivery(ok=False, error="未尝试")
        for attempt in range(attempts):
            bot_name = self._bot_name(task.chatid)
            await self._throttle(task.chatid, bot_name)
            try:
                last = await self.sender.deliver(task.chatid, task.content, task.images)
            except Exception as exc:  # noqa: BLE001 - 失败即重试
                last = Delivery(ok=False, bot_id=bot_name, error=f"{type(exc).__name__}: {exc}")
                logger.warning(
                    "群 {} 推送失败(第 {}/{} 次): {}", task.chatid, attempt + 1, attempts, exc
                )
            last.attempts = attempt + 1
            if last.ok:
                return last
            if attempt < attempts - 1:
                delay = self.retry_backoff_s[min(attempt, len(self.retry_backoff_s) - 1)]
                logger.info("群 {} 将在 {}s 后重试(退避表 {})", task.chatid, delay, self.retry_backoff_s)
                await asyncio.sleep(delay)
        return last

    async def _throttle(self, chatid: str, bot_name: str) -> None:
        """★ 两道闸门:机器人级(主,修 D5) + 群级(次)。"""
        while True:
            now = time.monotonic()
            async with self._lock:
                wait_bot = self.min_interval_s - (now - self._last_send_by_bot.get(bot_name, 0.0))
                wait_group = self.group_min_interval_s - (
                    now - self._last_send_by_group.get(chatid, 0.0)
                )
                wait = max(wait_bot, wait_group)
                if wait <= 0:
                    stamp = time.monotonic()
                    self._last_send_by_bot[bot_name] = stamp
                    self._last_send_by_group[chatid] = stamp
                    return
            await asyncio.sleep(min(wait, 5.0))

    def _bot_name(self, chatid: str) -> str:
        manager = getattr(self.sender, "manager", None)
        if manager is None:
            return "none"
        bot = manager.route(chatid)
        return bot.name if bot is not None else "none"

    # ==================================================================
    # 审计
    # ==================================================================

    async def _write_audit(self, task: PushTask, delivery: Delivery, slot: str) -> None:
        """按**审计目标**写行(一群多店 / 多报告项 → 多行)。

        ★ 审计粒度与消息条数解耦:22 项报告合并成 1 条消息发出,
        但审计仍按 ``(店, module_{item_id})`` 逐项留痕 —— 否则"某项没发出去"查不到。
        ``bot_id`` 是**文本**(D17)。
        """
        preview = task.content[:200]
        if task.note:
            preview = f"{task.note} | {preview}"[:200]
        recs = [
            PushRecord(
                group_chatid=task.chatid,
                hotel_id=hotel_id,
                push_type=push_type,
                status="ok" if delivery.ok else "failed",
                bot_id=delivery.bot_id or self._bot_name(task.chatid),
                content_preview=preview,
                media_count=delivery.media_count,
                error=delivery.error,
                images=tuple(delivery.images_sent),
                slot=slot,
                pushed_at=datetime.now(),
            )
            for hotel_id, push_type in task.resolved_audit_targets()
        ]
        try:
            await self.audit.write_many(recs)
        except Exception as exc:  # noqa: BLE001 - 审计失败不许影响投递结果
            logger.error("写 push_logs 失败: {}", exc)

    # ==================================================================
    # 直投(CLI / 命令「重推」)
    # ==================================================================

    async def deliver_now(self, task: PushTask) -> Delivery:
        """**不入队**直接投递(同步等结果)。命令回执与 CLI 需要即时结果。"""
        slot = task.effective_slot()
        if not task.force:
            if await self._is_duplicated(task, slot):
                self.skipped += 1
                return Delivery(ok=False, error="今日已推送过(用 --force / 「重推」强制)")
        delivery = await self.deliver(task)
        await self._write_audit(task, delivery, slot)
        if delivery.ok:
            self.sent += 1
        else:
            self.failed += 1
        return delivery

    def snapshot(self) -> dict[str, Any]:
        return {
            "workers": self.worker_count,
            "pending": self.pending,
            "sent": self.sent,
            "failed": self.failed,
            "skipped": self.skipped,
            "min_interval_s": self.min_interval_s,
            "group_min_interval_s": self.group_min_interval_s,
            "retry_backoff_s": list(self.retry_backoff_s),
            "sender": self.sender.stats(),
        }
