"""★ 多机器人管理器(T2A.3)—— **D1 / D5 / D18 三个缺陷的修复点**。

为什么单独一个管理器(而不是模块级全局)
========================================

旧系统 (``app/__init__.py:196``) 在**多机器人**模式下执行了::

    set_bot(bot)          # bot = None(多机器人时没有"单机器人")
    set_bot_manager(manager)

而 ``app/pusher.py:42`` 的 ``send_alert`` 读的是 ``_BOT``::

    bot = _BOT or get_bot()
    if not bot or bot.connected is False:
        logger.warning("告警未发送(机器人不可用)")   # ← 恒走这里
        return False

**结果:配置了多个机器人时,所有运维告警静默丢失** ——
登录失效、机器人掉线、推送失败,一条都发不出去,而且只留一行 warning 日志。
这就是段2 的头号必修项(D1 / 风险 P3)。

新架构的解法:**`BotManager` 是唯一对外能力出口**,没有"单例回退"这条路径。
``Runtime`` 持有它,``send_alert`` **遍历在线机器人投递并返回逐群结果**。

三个缺陷的对应修复
==================

=====  ============================================  ==================================
#      旧缺陷                                          本模块的做法
=====  ============================================  ==================================
D1     ``set_bot(None)`` → 告警恒返回 False           :meth:`send_alert` 遍历**在线**机器人,
                                                       返回 :class:`AlertResult`(含逐群结果
                                                       与错误)。**一台都发不出去时也返回
                                                       带错误的结果,绝不静默 False**。
D5     限频键是 chatid,多机器人分摊**实际失效**       :meth:`route` 按
                                                       ``md5(chatid)[:8] % n`` 稳定分摊;
                                                       派发器据此按**机器人**限频
                                                       (:mod:`hoteldata.push.dispatcher`)。
D18    ``commands.py:298`` 读 ``health["online"]``,
       而 ``get_health()`` 返回 ``{名字: bool}``      :meth:`health` **统一契约**
                                                       ``dict[str, bool]``;消费方一律按
                                                       这个签名读,不许再猜键名。
=====  ============================================  ==================================

★ **路由必须稳定**:``md5(chatid)`` 而不是内置 ``hash()`` —— 后者跨进程不稳定
(``PYTHONHASHSEED`` 随机),重启后同一个群会换机器人,群成员会看到"两个助手"
来回横跳。这一条与 ``infra/db.py::advisory_key`` 的教训同源。
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from sqlalchemy import select

from hoteldata.domains.bot.client import BotClient
from hoteldata.infra.db import Database
from hoteldata.infra.models import Bot
from hoteldata.settings import Settings, get_settings

__all__ = ["AlertResult", "BotManager", "load_bots_from_db"]

#: 告警投递回调签名(由 ``push.service`` 注入,避免 push ↔ bot 反向依赖)
AlertNotifier = Callable[[str], Awaitable[None]]


@dataclass(slots=True)
class AlertResult:
    """一次运维告警的**逐群投递结果**。

    ★ 存在的理由就是 D1:**绝不返回裸 ``bool``**。
    失败必须带着"发给谁、哪台机器人、什么错"一起回来,
    调用方(和日志)才有东西可查。
    """

    text: str = ""
    targets: list[str] = field(default_factory=list)
    delivered: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.delivered) and not self.error

    @property
    def attempted(self) -> int:
        return len(self.delivered) + len(self.failed)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "targets": self.targets,
            "delivered": self.delivered,
            "failed": [{"target": t, "error": e} for t, e in self.failed],
            "error": self.error,
            "attempted": self.attempted,
        }


async def load_bots_from_db(db: Database, settings: Settings | None = None) -> list[dict[str, str]]:
    """从 ``core_bots`` 读并**解密**凭据。

    🚫 新架构**没有** ``AIBOT_BOT_ID`` / ``AIBOT_SECRET`` 这条单机器人回退路径
    (段2 §4.5 D 级丢弃项):「单机器人」就是"1 个实例",走同一张表。

    解密失败的机器人**跳过并告警**,不让一条坏记录拖垮整个网关。
    """
    from hoteldata.infra.crypto import get_cipher

    async with db.session() as s:
        rows = list(
            (
                await s.execute(
                    select(Bot).where(Bot.status == "active").order_by(Bot.name)
                )
            )
            .scalars()
            .all()
        )
    if not rows:
        return []
    cipher = get_cipher(settings)
    out: list[dict[str, str]] = []
    for row in rows:
        try:
            out.append(
                {
                    "name": str(row.name),
                    "bot_id": cipher.decrypt(row.bot_id_enc),
                    "secret": cipher.decrypt(row.secret_enc),
                    "capacity_per_bot": str(row.capacity_per_bot),
                }
            )
        except Exception as exc:  # noqa: BLE001 - 单条坏记录不拖垮网关
            logger.error("机器人 {} 凭据解密失败,跳过: {}", row.name, exc)
    return out


class BotManager:
    """多机器人实例管理。★ **所有对外能力都必须走这里,不依赖单例。**"""

    def __init__(
        self,
        bots: list[dict[str, str]] | None = None,
        *,
        settings: Settings | None = None,
        on_message: Callable[[BotClient, dict[str, Any]], Awaitable[None]] | None = None,
        on_event: Callable[[BotClient, dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self.settings: Settings = settings or get_settings()
        self._clients: dict[str, BotClient] = {}
        self._alert_notifier: AlertNotifier | None = None
        self._health_task: asyncio.Task[None] | None = None
        self._alerted_down: set[str] = set()
        self._on_message: Callable[[BotClient, dict[str, Any]], Awaitable[None]] | None = on_message
        self._on_event: Callable[[BotClient, dict[str, Any]], Awaitable[None]] | None = on_event

        for item in bots or []:
            self.add(item)

    # ==================================================================
    # 装配
    # ==================================================================

    def add(self, item: dict[str, str]) -> BotClient:
        """★ **往同一个实例里加一个机器人**(保持对象身份不变)。

        为什么要有这个方法:``Runtime.bots`` 是**懒构建**的 —— 推送服务可能先拿到
        一个空管理器(``BotManager([])``),随后 ``start_bots()`` 才从 ``core_bots``
        读出真实凭据。若那时**新建一个 BotManager 再赋值**给 ``runtime._bots``,
        已经持有旧实例的 :class:`~hoteldata.push.service.PushService` 会永远对着
        一个 0 实例的管理器发消息 —— 每条推送都失败,而且看起来"配置没错"。
        所以只允许**就地添加**,不允许替换实例。
        """
        name = item.get("name") or "bot"
        client = BotClient(
            name,
            item.get("bot_id") or "",
            item.get("secret") or "",
            settings=self.settings,
            on_message=(lambda frame, c=name: self._dispatch_message(c, frame)),
            on_event=(lambda frame, c=name: self._dispatch_event(c, frame)),
        )
        self._clients[name] = client
        return client

    def set_handlers(
        self,
        *,
        on_message: Callable[[BotClient, dict[str, Any]], Awaitable[None]],
        on_event: Callable[[BotClient, dict[str, Any]], Awaitable[None]],
    ) -> None:
        """注入消息/事件处理器(存在实例上,后加的机器人自动继承)。"""
        self._on_message = on_message
        self._on_event = on_event
        for name, client in self._clients.items():
            client.on_message = lambda frame, n=name: self._dispatch_message(n, frame)
            client.on_event = lambda frame, n=name: self._dispatch_event(n, frame)

    def set_alert_notifier(self, notifier: AlertNotifier) -> None:
        """注入"把告警发给运维群"的回调(健康循环掉线告警用)。

        ★ 用注入而不是 import:``push/`` 与 ``domains/bot`` 谁也不 import 谁,
        由 ``Runtime`` 在装配时接线(避免循环依赖)。
        """
        self._alert_notifier = notifier

    async def _dispatch_message(self, name: str, frame: dict[str, Any]) -> None:
        handler = self._on_message
        client = self._clients.get(name)
        if handler is None or client is None:
            return
        await handler(client, frame)

    async def _dispatch_event(self, name: str, frame: dict[str, Any]) -> None:
        handler = self._on_event
        client = self._clients.get(name)
        if handler is None or client is None:
            return
        await handler(client, frame)

    # ==================================================================
    # 生命周期
    # ==================================================================

    async def start_all(self) -> None:
        for name, client in sorted(self._clients.items()):
            await client.start()
            logger.info("机器人 {} 已启动", name)
        if not self._clients:
            logger.warning("core_bots 表为空,机器人网关未启动(推送将失败并留下审计)")

    async def stop_all(self) -> None:
        if self._health_task is not None and not self._health_task.done():
            self._health_task.cancel()
        self._health_task = None
        for client in self._clients.values():
            await client.stop()

    def start_health_loop(self) -> asyncio.Task[None]:
        """常驻健康巡检(默认 300 秒):掉线/恢复**各告警一次**。"""
        if self._health_task is None or self._health_task.done():
            self._health_task = asyncio.create_task(self._health_loop(), name="aibot-health")
        return self._health_task

    # ==================================================================
    # 路由(D5 对应的修复基础)
    # ==================================================================

    def size(self) -> int:
        return len(self._clients)

    def names(self) -> list[str]:
        return sorted(self._clients)

    def all_clients(self) -> list[BotClient]:
        return [self._clients[n] for n in self.names()]

    def get(self, name: str) -> BotClient | None:
        return self._clients.get(name)

    def route(self, chatid: str) -> BotClient | None:
        """A1-15:``md5(chatid).hexdigest()[:8] % n → sorted(names)[idx]``。

        ★ 用 ``md5`` 而非内置 ``hash()``:后者跨进程不稳定(见模块 docstring)。
        没有机器人时返回 ``None`` —— 调用方**必须显式处理**(写审计 + 报错),
        **不许**像旧系统那样"回退到 ``_BOT``(可能是 ``None``)然后静默失败"。
        """
        if not self._clients:
            return None
        names = self.names()
        idx = int(hashlib.md5(chatid.encode("utf-8")).hexdigest()[:8], 16) % len(names)  # noqa: S324
        return self._clients[names[idx]]

    def for_group(self, chatid: str) -> BotClient | None:
        """:meth:`route` 的别名(语义更直白:给这个群找一个机器人)。"""
        return self.route(chatid)

    @property
    def online(self) -> list[BotClient]:
        return [c for c in self.all_clients() if c.connected]

    # ==================================================================
    # 健康(D18 的统一契约)
    # ==================================================================

    def health(self) -> dict[str, bool]:
        """★ **统一契约**:``{机器人名: 是否连接}``。

        修 D18:旧 ``get_health()`` 返回的就是这个形状,而消费方
        (``commands.py:298``)读的是 ``health["online"]`` —— 键不存在,
        于是 `online_bots` 永远回退到"配置里的活跃机器人数",显示与实际不符。
        **消费方一律按本签名读,不许再猜键名。**
        """
        return {name: bool(client.connected) for name, client in sorted(self._clients.items())}

    def get_health(self) -> dict[str, bool]:
        """旧名兼容(内部一律用 :meth:`health`)。"""
        return self.health()

    def snapshot(self) -> dict[str, Any]:
        """``/status`` 用。"""
        return {
            "bots": self.size(),
            "online": len(self.online),
            "health": self.health(),
            "detail": {c.name: c.snapshot() for c in self.all_clients()},
        }

    def balance_report(self, group_counts: dict[str, int]) -> list[str]:
        """每机器人群数分布检查(``capacity_per_bot`` 超出**只告警不硬拦**,A1-16)。"""
        capacity = self.settings.bot.capacity_per_bot
        return [
            f"机器人 {name} 分摊 {cnt} 群(建议 ≤{capacity})"
            for name, cnt in sorted(group_counts.items())
            if cnt > capacity
        ]

    async def health_check(self, *, alert: bool = True) -> dict[str, list[str]]:
        """掉线巡检:**掉线一次告警、恢复一次通知**(不重复刷)。

        返回 ``{"down": [...], "recovered": [...]}``。
        """
        down: list[str] = []
        recovered: list[str] = []
        for name, client in sorted(self._clients.items()):
            ok = bool(client.connected)
            if not ok and name not in self._alerted_down:
                self._alerted_down.add(name)
                down.append(name)
                await self._notify(f"⚠️ 机器人 **{name}** 掉线(分组路由受损,自动重连中)")
            elif ok and name in self._alerted_down:
                self._alerted_down.discard(name)
                recovered.append(name)
                await self._notify(f"✅ 机器人 **{name}** 已恢复在线")
        return {"down": down, "recovered": recovered}

    async def _notify(self, text: str) -> None:
        if self._alert_notifier is None:
            logger.warning("告警无法投递(未注入 alert_notifier): {}", text[:120])
            return
        try:
            await self._alert_notifier(text)
        except Exception as exc:  # noqa: BLE001 - 通知失败不许打断健康循环
            logger.error("告警投递异常: {} | {}", text[:80], exc)

    async def _health_loop(self) -> None:
        interval = self.settings.bot.health_interval_s
        while True:
            try:
                await asyncio.sleep(interval)
                await self.health_check(alert=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error("健康巡检异常: {}", exc)

    # ==================================================================
    # ★ 告警(D1 修复)
    # ==================================================================

    async def send_alert(self, text: str, *, chatids: list[str]) -> AlertResult:
        """★ **修 D1**:遍历**在线**机器人投递,返回逐群结果;失败必须可见。

        为什么"遍历在线机器人"而不是"按 chatid 路由":
        告警是**运维消息**,它的可达性不能取决于"这个群被分给了哪台机器人"。
        某台机器人正好掉线时,正是最需要发告警的时候 —— 若按路由投递,
        告警会跟着那台掉线的机器人一起消失(P3:"出事了没人知道")。

        ★ 没有目标群 / 一台机器人在线都没有 → 返回**带 error 的结果**
        (``ok=False`` + ``error``),**不是裸 ``False``**。调用方据此写审计。
        """
        result = AlertResult(text=text, targets=list(chatids))
        if not chatids:
            result.error = "未配置告警目标群(MANAGE_CHATIDS / OPS_CHATID 均为空)"
            logger.warning("告警未发送:{} | {}", result.error, text.replace("\n", " ")[:120])
            return result

        online = self.online
        if not online:
            result.error = f"没有在线机器人({self.size()} 个实例全部离线)"
            logger.warning("告警未发送:{} | {}", result.error, text.replace("\n", " ")[:120])
            return result

        for chatid in chatids:
            sent = False
            last_error = ""
            for bot in online:
                try:
                    await bot.send_markdown(chatid, text)
                    result.delivered.append(chatid)
                    logger.info("告警已推送 → {} (机器人 {})", chatid, bot.name)
                    sent = True
                    break
                except Exception as exc:  # noqa: BLE001 - 换下一台机器人再试
                    last_error = f"{bot.name}: {exc}"
                    logger.warning("告警投递失败 → {} (机器人 {}): {}", chatid, bot.name, exc)
            if not sent:
                result.failed.append((chatid, last_error or "全部机器人都失败"))
        return result
