"""★ 消息处理链编排(T2B.6 / T2B.7)—— **顺序是踩出来的,不许改**。

顺序(计划书 §5.2 逐字)
=======================

::

    收到消息
     ├─ record_target(记录会话目标)
     ├─ 非 text → 丢弃
     ├─ ① 群命令        startswith 最长前缀匹配;私聊无 chatid → 跳过(返回 None)
     ├─ ② 比价问答      命中关键词(★ 段3 提供实现,段2 只留**接口钩子**,默认 None)
     ├─ ③ 实时问答      仅群聊;realtime_ask 项 + 别名
     ├─ ④ FAQ 匹配      精确 questions → keywords 包含
     └─ ⑤ 兜底文案

为什么**命令必须最先**:否则「绑定 XX酒店」会被 FAQ 的 ``keywords`` 抢走
(「绑定」二字进了 ``keywords`` 的模糊匹配),群成员永远绑不上店。

为什么**比价问答在实时问答之前**:比价问的是"XX酒店多少钱",
里面**带着酒店名**;而实时项「离店」的匹配是**包含**匹配 ——
顺序反了,酒店名会被实时项吃掉,答非所问(甲方原话:"它答的不是我问的")。

② 是**显式接口钩子,不是 TODO**
==============================

``MessageRouter.__init__(..., price_qna=None)`` 的 ``price_qna`` 参数就是段3 的注入点::

    Callable[[str], Awaitable[tuple[str, list[Path]] | None]]

段2 阶段恒为 ``None`` → 这一步**直接跳过**(零成本、零分支副作用)。
段3 只要在装配时传进来,不需要改本文件一行 —— 这正是 §1.4
"避免段3 回头改段2 的代码"的落地方式。

异常隔离(硬要求)
=================

整条链外面包一层 ``try/except`` + ``logger.exception``:
机器人**最怕的不是答错,是掉线**。业务侧任何一个未捕获异常
(某家店数据坏了、某个服务没就绪、某个字段是 ``None``)都不该把长连接带走。
client 层已经对 handler 做了保护,这里**再兜一层**并留下可检索的堆栈。

``record_target`` 的**有意差异**(★ 与旧系统不同)
================================================

旧 ``pusher.record_target`` 把会话目标写进 ``config/aibot_targets.json`` ——
**可变状态进 config**,``settings.py`` 明令禁止(配置目录只放"人写的配置",
不放"程序运行长出来的状态")。

新架构:

* **不写任何文件**;
* 进程内 ``self._targets: dict[str, dict]`` 记录 ``{userid, chatid, first_seen}``;
* 同时 ``logger.info`` 落一条可检索日志。

代价说明:进程重启后内存表清空。**这是有意的** —— 新架构的推送目标来自
``core_group_bindings``(权威、可审计、可解绑),而不是"谁说过话"这种隐式状态。
``_targets`` 只服务于"排障时看谁在什么时候出现过"。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from hoteldata.domains.bot import commands as commands_mod
from hoteldata.domains.bot import faq as faq_mod
from hoteldata.domains.bot import realtime as realtime_mod
from hoteldata.domains.bot.client import BotClient
from hoteldata.domains.bot.protocol import (
    chatid_of,
    event_type_of,
    userid_of,
)
from hoteldata.push.sender import Sender

__all__ = ["FALLBACK_TEXT", "WELCOME_TEXT", "MessageRouter"]

#: 欢迎语(**逐字继承**旧 ``app/__init__.py:21-24``)
WELCOME_TEXT = (
    "您好！我是经营数据助手。可以问我：今日订单量、出租率、携程评分、"
    "流量曝光等；发送「帮助」查看说明。"
)

#: 兜底文案(**逐字继承**旧 ``app/__init__.py:25-28``)
FALLBACK_TEXT = (
    "暂未找到相关内容，请联系人工处理。您可以试试问我："
    "今日订单量、出租率、携程评分、流量曝光。"
)

#: ② 比价问答钩子签名(段3 注入):问题 → ``(文本, 图片列表)`` 或 ``None``
PriceQnA = Callable[[str], Awaitable["tuple[str, list[Path]] | None"]]


class MessageRouter:
    """消息 / 事件 → 处理链的编排者(**唯一知道顺序的地方**)。"""

    def __init__(self, runtime: Any, *, sender: Sender, price_qna: PriceQnA | None = None) -> None:
        self.runtime = runtime
        self.sender = sender
        #: ★ 段3 注入的比价问答实现。**段2 默认 ``None`` —— 这是显式接口钩子,不是 TODO**
        self.price_qna = price_qna
        #: 会话目标(进程内;★ **有意不写 config**,见模块 docstring)
        self._targets: dict[str, dict[str, Any]] = {}

    # ==================================================================
    # 会话目标
    # ==================================================================

    def record_target(self, frame: dict[str, Any]) -> str | None:
        """记录会话目标 ``{userid, chatid, first_seen}``;无 userid/chatid → ``None``。

        返回 "目标键"(userid 优先,其次 chatid),与旧 ``pusher.record_target`` 一致。
        首次出现记 ``logger.info``(可检索),重复出现只更新 ``last_seen``。
        """
        userid = userid_of(frame)
        chatid = chatid_of(frame)
        if not userid and not chatid:
            return None
        key = str(userid or chatid)
        now = datetime.now().isoformat(timespec="seconds")
        existing = self._targets.get(key)
        if existing is None:
            self._targets[key] = {"userid": userid, "chatid": chatid, "first_seen": now}
            logger.info("记录新会话目标: userid={} chatid={}", userid, chatid)
        else:
            existing["last_seen"] = now
            if chatid and not existing.get("chatid"):
                existing["chatid"] = chatid
        return key

    def targets(self) -> dict[str, dict[str, Any]]:
        """进程内会话目标快照(排障用;不是推送目标来源 —— 那来自 ``core_group_bindings``)。"""
        return {k: dict(v) for k, v in self._targets.items()}

    # ==================================================================
    # 消息链
    # ==================================================================

    async def on_message(self, bot: BotClient, frame: dict[str, Any]) -> None:
        """收到消息:严格按 §5.2 的 5 步顺序处理;**异常绝不外泄**。"""
        try:
            await self._on_message(bot, frame)
        except Exception:  # noqa: BLE001 - ★ 业务异常不许把机器人打下线
            logger.exception("消息处理链异常(已隔离,机器人保持在线)")

    async def _on_message(self, bot: BotClient, frame: dict[str, Any]) -> None:
        """消息链主体(顺序即计划书 §5.2,位置见各步注释)。"""
        # ── record_target(第一步:谁在什么时候出现过)
        self.record_target(frame)

        # ── 非 text → 丢弃(图片/文件/语音一律不回,避免机器人变成复读机)
        content = self._text_of(frame)
        if content is None:
            logger.info("收到非文本消息,丢弃")
            return
        chatid = chatid_of(frame)
        logger.info("收到消息: chatid={} content={}", chatid or "(私聊)", content[:80])

        # ── ① 群命令(★ 必须最先)
        reply = await commands_mod.handle_command(self.runtime, bot, frame, content)
        if reply is not None:
            logger.info("命中命令,回复: {}", reply[:60])
            await self.sender.reply_text(bot, frame, reply)
            return

        question = faq_mod.clean_question(content)

        # ── ② 比价问答(段3 注入的实现;段2 默认 None → 直接跳过)
        if self.price_qna is not None:
            try:
                price_reply = await self.price_qna(question)
            except Exception:  # noqa: BLE001 - 段3 实现出错不许吃掉后面的问答链
                logger.exception("比价问答钩子异常(跳过,继续实时/FAQ)")
                price_reply = None
            if price_reply:
                text, images = price_reply
                await self.sender.reply_text(bot, frame, text)
                if images:
                    await self.sender.reply_images(bot, frame, [str(p) for p in images])
                return

        # ── ③ 实时问答(仅群聊;问才发)
        if chatid:
            realtime_reply = await realtime_mod.build_realtime_reply(self.runtime, question, chatid)
            if realtime_reply:
                text, images = self._split_reply(realtime_reply)
                await self.sender.reply_text(bot, frame, text)
                if images:
                    await self.sender.reply_images(bot, frame, [str(p) for p in images])
                return

        # ── ④ FAQ 匹配(精确 questions → keywords 包含)
        item = faq_mod.match_faq(self.runtime, question)
        if item is None:
            # ── ⑤ 兜底文案
            logger.info("未命中问答,回复兜底文案: {}", question[:40])
            await self.sender.reply_text(bot, frame, FALLBACK_TEXT)
            return

        payload = self._faq_payload()
        text = faq_mod.build_reply(item, payload)
        logger.info("命中问答: {} -> {}", question[:40], text[:60])
        await self.sender.reply_text(bot, frame, text)
        try:
            images = await faq_mod.resolve_image_sources(
                self.runtime, item, await self._faq_hotel_id(chatid)
            )
        except Exception as exc:  # noqa: BLE001 - 缺图不该变成"没有回复"
            logger.warning("FAQ 配图解析失败(仅文字回复): {}", exc)
            images = []
        if images:
            await self.sender.reply_images(bot, frame, [str(p) for p in images])

    # ==================================================================
    # 事件(T2B.7)
    # ==================================================================

    async def on_event(self, bot: BotClient, frame: dict[str, Any]) -> None:
        """收到事件:``enter_chat`` → 回欢迎语(**5 秒内,所以只发一条、不重试**)。"""
        try:
            self.record_target(frame)
            event_type = event_type_of(frame)
            logger.info("收到事件: {}", event_type)
            if event_type == "enter_chat":
                # A1-11:欢迎语必须在 5 秒内送达 —— 重试/拆分都来不及,只发一条
                await self.sender.reply_welcome(bot, frame, WELCOME_TEXT)
        except Exception:  # noqa: BLE001 - 同上:事件链异常不许影响长连接
            logger.exception("事件处理链异常(已隔离)")

    # ==================================================================
    # 工具
    # ==================================================================

    @staticmethod
    def _text_of(frame: dict[str, Any]) -> str | None:
        """文本正文;非 text 消息 → ``None``(与 ``protocol.text_content_of`` 一致)。"""
        body = frame.get("body")
        body = body if isinstance(body, dict) else {}
        if body.get("msgtype") != "text":
            return None
        text = body.get("text")
        text = text if isinstance(text, dict) else {}
        value = text.get("content")
        return value if isinstance(value, str) else None

    @staticmethod
    def _split_reply(reply: Any) -> tuple[str, list[Any]]:
        """``str`` / ``(text, images)`` 两种返回形态 → 统一解包(实时问答的域层可能给图)。"""
        if isinstance(reply, tuple):
            text = str(reply[0]) if reply else ""
            images = list(reply[1]) if len(reply) > 1 and reply[1] else []
            return text, images
        return str(reply), []

    def _faq_payload(self) -> dict[str, Any]:
        """FAQ 占位符的取数上下文。

        段2 的 FAQ(9 条 A3-3 级遗产)``reply`` 里**没有** ``{{指标名}}`` 占位符 →
        这里返回空上下文,占位符机制**空转**但保留(运营随时可以写回占位符)。
        真要填数,由报告域在日报组装时自己渲染,不在问答链里临时查库。
        """
        return {}

    async def _faq_hotel_id(self, chatid: str | None) -> int | None:
        """FAQ 配图的目标酒店:**本群恰好绑定 1 家**时用它;0 家或多店 → ``None``。

        为什么多店不猜第一家:FAQ 的图是"经营报告整页截图",指错店的截图
        比不附图更糟(群成员会拿错店的数据做决策)。
        """
        if not chatid:
            return None
        try:
            bound = await self.runtime.bindings.for_group(chatid)
        except Exception as exc:  # noqa: BLE001 - 查询失败按"无目标店"处理
            logger.warning("FAQ 取群绑定失败: chatid={} err={}", chatid, exc)
            return None
        return int(bound[0].hotel_id) if len(bound) == 1 else None
