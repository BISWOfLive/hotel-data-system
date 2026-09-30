"""单实例 async 企微客户端(T2A.2)—— **传输层换 async,协议层逐字节不动**。

为什么必须换成 ``websockets``(段2 §3 / P8)
==========================================

旧系统用**同步** ``websocket-client`` + ``threading``:一个机器人一个线程。
放进单进程 asyncio(FastAPI + APScheduler)里,同步阻塞调用会**卡住整个事件循环**
—— 一个机器人收消息时,其它机器人和**所有定时任务**全部停摆。30 个机器人
= 30 个阻塞点。

新实现:``websockets.connect`` + ``asyncio.Task``。协议帧、命令字、``req_id``、
心跳节奏、超时、退避公式**一个字符都不改**(全部来自 :mod:`.protocol`)。

★ 两条从旧系统踩出来的实现约束
==============================

① **入站消息必须在独立 Task 里处理,不能 await 在收帧循环上**。
   旧系统 ``aibot.py:162`` 专门为这件事开了新线程,注释写得很清楚:

   > 独立线程处理:接收线程必须持续收帧(回执处理),否则上传图片等
   > "等待回执"的操作会与接收线程互相等待(**死锁**)。

   async 版同样成立:``upload_media`` 等 ack,而 ack 只能由收帧循环投递。
   如果 handler 直接在收帧循环里 await,一旦 handler 里要上传图片 → **永久死锁**。

② **回复类消息不等 ack**(``_send_only``,A1-10)。
   旧系统 ``aibot.py:238-241`` 的理由:回复消息服务端回执可能延迟 >8s,
   等待 + 超时重试会造成**重复回复**。所以回复"发送成功即视为完成"。

★ 心跳失败判定(A1-7):``_missed_pong`` 计数,**连丢 2 次**才强断重连 ——
丢 1 次不重连。旧系统这一条被段2 附录 D 单列为"业务事实"。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import time
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from hoteldata.domains.bot import protocol as P
from hoteldata.settings import BotSettings, Settings, get_settings

__all__ = ["BotClient"]

#: 入站帧处理器签名(``client`` 由管理器绑定,单实例模式下可为 None)
MessageHandler = Callable[[dict[str, Any]], Awaitable[None]]


class BotClient:
    """一个企微智能机器人的长连接客户端。

    ``name`` = ``core_bots.name``(推送审计里 ``push_logs.bot_id`` 写的就是这个,
    ★ 文本类型,D17 修复)。
    """

    def __init__(
        self,
        name: str,
        bot_id: str,
        secret: str,
        *,
        settings: Settings | None = None,
        on_message: MessageHandler | None = None,
        on_event: MessageHandler | None = None,
    ) -> None:
        self.settings: Settings = settings or get_settings()
        self.cfg: BotSettings = self.settings.bot
        self.name = name or "bot"
        self.bot_id = bot_id
        self.secret = secret

        self.on_message = on_message
        self.on_event = on_event

        self._ws: Any = None
        self._task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._handlers: set[asyncio.Task[Any]] = set()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._stopping = False
        self._ready = False
        self._missed_pong = 0
        #: 重连次数(健康快照与日志用)
        self.reconnects = 0
        self.last_error: str | None = None

    # ==================================================================
    # 状态
    # ==================================================================

    @property
    def ready(self) -> bool:
        """订阅成功(``ack.errcode == 0``)后才为 ``True``(A1-5)。"""
        return self._ready

    @property
    def connected(self) -> bool:
        """★ 统一健康口径:``ready and ws is not None``(修 D18 的消费契约)。

        旧系统 ``commands.py:298`` 读的是 ``health["online"]``,而
        ``AiBotManager.get_health()`` 返回的是 ``{机器人名: bool}`` —— 键名对不上,
        于是「状态」命令**永远显示不出真实在线数**。
        新契约固定为 :meth:`BotManager.health` 的 ``dict[str, bool]``,
        本属性就是那本字典里每项的值。
        """
        return self._ready and self._ws is not None

    def health(self) -> dict[str, Any]:
        """单个实例的健康快照。"""
        return {
            "name": self.name,
            "ready": self._ready,
            "connected": self.connected,
            "reconnects": self.reconnects,
            "missed_pong": self._missed_pong,
            "last_error": self.last_error,
        }

    # ==================================================================
    # 生命周期
    # ==================================================================

    async def start(self) -> None:
        """启动后台常驻任务(幂等)。"""
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name=f"aibot-{self.name}")

    async def stop(self) -> None:
        """停止(幂等):取消任务 → 关闭 ws → 清理挂起 Future。"""
        self._stopping = True
        for task in (self._heartbeat_task, self._task):
            if task is not None and not task.done():
                task.cancel()
        for task in list(self._handlers):
            task.cancel()
        await self._close_ws()
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        self._ready = False
        self._heartbeat_task = None
        self._task = None

    async def _close_ws(self) -> None:
        ws, self._ws = self._ws, None
        self._ready = False
        if ws is None:
            return
        with contextlib.suppress(Exception):
            await ws.close()

    # ==================================================================
    # 主循环(A1-8 重连退避)
    # ==================================================================

    async def _run(self) -> None:
        """重连循环:``delay = min(2 ** attempt, 30)`` 秒(A1-8 逐字)。"""
        attempt = 0
        while not self._stopping:
            try:
                await self._serve_once()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 长连接任何异常都要重连,不许退出
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.error("机器人 {} 长连接异常: {}", self.name, exc)
            if self._stopping:
                break
            delay = min(float(2**attempt), 30.0)
            attempt += 1
            self.reconnects += 1
            logger.info("机器人 {}:{} 秒后重连(第 {} 次)", self.name, int(delay), attempt)
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise

    async def _serve_once(self) -> None:
        """连一次:★ **先起收帧循环** → 订阅 → 起心跳 → 收帧直到断开。

        ★★ **收帧循环必须先于订阅启动** —— 这是本项目踩过的第一个死锁:

        ``_subscribe()`` 走 ``_send_wait``,而 ack **只能由收帧循环投递**。
        若先 ``await self._subscribe()`` 再起 ``_recv_loop``,订阅帧发出去之后
        没有任何人在读 socket → 必然等到 10 秒超时 → 判定"认证失败" → 重连 →
        再超时 …… **永远连不上**,而日志只会说"等待回执超时",指不到病根。

        旧系统 (``aibot.py:104-109``) 的同步实现里,``_recv_thread`` 就是在
        ``_send_wait`` **之前** ``start()`` 的 —— 这里保持同样的顺序。

        ★★ **退出时必须把 ``ready`` / ``_ws`` 清掉**(外层 ``finally``):
        本方法的两处状态复位都在 ``async with`` **体之内**,而重连时
        ``websockets.connect()`` 可能**直接抛**(ConnectionRefused 等),
        于是循环体根本不执行 —— 上一轮成功连接的 ``ready=True`` 与那个**已死的
        socket** 就会被一直留着。后果是:服务端永久挂掉,``BotManager.health()``
        仍报"在线"(本地联调实测:``reconnects=2`` 且持续 ConnectionRefused,
        ``ready/connected`` 依然是 True)。那会让「状态」命令、``/status``、
        ``bots health`` 全部说谎,也会让 D1 的"遍历在线机器人发告警"去用一个
        死连接。所以**不管怎么退出**(连不上 / 连上又断),都在 ``finally`` 里
        标记离线。
        """
        import websockets

        logger.info("机器人 {} 正在连接 {}", self.name, self.cfg.ws_url)
        try:
            async with websockets.connect(self.cfg.ws_url) as ws:
                self._ws = ws
                self._ready = False
                self._missed_pong = 0

                # ① 先起收帧循环(ack 的唯一投递者)
                recv_task = asyncio.create_task(self._recv_loop(ws), name=f"aibot-recv-{self.name}")
                try:
                    # ② 再订阅(A1-5:超时 10s,校验 errcode==0)
                    await self._subscribe()
                    self._ready = True
                    logger.success("机器人 {} 订阅成功,已上线", self.name)
                    # ③ 心跳(A1-7)
                    self._heartbeat_task = asyncio.create_task(
                        self._heartbeat_loop(), name=f"aibot-hb-{self.name}"
                    )
                    # ④ 等收帧循环结束(心跳判死会 close ws,让 recv 抛错退出)
                    await recv_task
                finally:
                    hb, self._heartbeat_task = self._heartbeat_task, None
                    if hb is not None and not hb.done():
                        hb.cancel()
                    if not recv_task.done():
                        recv_task.cancel()
        finally:
            # ★ 离线状态必须**立刻**对外可见(见 docstring 的说明)
            if self._ready:
                logger.warning("机器人 {} 连接结束,标记为离线", self.name)
            self._ready = False
            self._ws = None

    async def _subscribe(self) -> dict[str, Any]:
        """A1-5:``aibot_subscribe`` + ``{bot_id, secret}``,超时 10s,校验 ``errcode==0``。"""
        return await self._send_wait(
            P.CMD_SUBSCRIBE,
            {"bot_id": self.bot_id, "secret": self.secret},
            timeout=self.cfg.subscribe_timeout_s,
        )

    # ==================================================================
    # 收帧(A1-9)
    # ==================================================================

    async def _recv_loop(self, ws: Any) -> None:
        """收帧循环:超时 20 秒继续等;断开/异常退出。"""
        while not self._stopping:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=self.cfg.recv_timeout_s)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 断开 → 交给 _run 重连
                logger.warning("机器人 {} 收帧中断: {}", self.name, exc)
                return
            if not raw:
                continue
            frame = P.parse_frame(raw)
            if frame is None:
                logger.warning("机器人 {} 帧解析失败 | 原始: {}", self.name, str(raw)[:120])
                continue
            try:
                await self._handle_frame(frame)
            except Exception as exc:  # noqa: BLE001 - 单帧处理异常不许打断收帧
                logger.error("机器人 {} 帧处理异常: {}", self.name, exc)

    async def _handle_frame(self, frame: dict[str, Any]) -> None:
        """帧分发(A1-6):入站回调 → 独立 Task;ack → 唤醒;迟到回执 → 静默忽略。"""
        cmd = P.cmd_of(frame)
        if cmd == P.CMD_CALLBACK:
            logger.info("机器人 {} 收到消息帧 req_id={}", self.name, P.req_id_of(frame))
            self._spawn(self.on_message, frame, kind="msg")
            return
        if cmd == P.CMD_EVENT_CALLBACK:
            logger.info("机器人 {} 收到事件帧 req_id={}", self.name, P.req_id_of(frame))
            self._spawn(self.on_event, frame, kind="ev")
            return

        rid = P.req_id_of(frame)
        fut = self._pending.get(rid)
        if fut is not None and not fut.done():
            fut.set_result(frame)
            return
        # ★ A1-6 迟到回执:等待方已超时,按 req_id 前缀**静默忽略**(不许刷 warning)
        if P.is_late_ack(frame):
            logger.debug("机器人 {} 迟到回执(忽略): req_id={}", self.name, rid)
            return
        logger.warning("机器人 {} 未知帧: {}", self.name, str(frame)[:200])

    def _spawn(
        self,
        handler: MessageHandler | None,
        frame: dict[str, Any],
        *,
        kind: str,
    ) -> None:
        """把入站帧交给独立 Task —— **不许在收帧循环里 await**(见模块 docstring ①)。"""
        if handler is None:
            return
        task = asyncio.create_task(self._run_handler(handler, frame, kind), name=f"aibot-{kind}-{self.name}")
        self._handlers.add(task)
        task.add_done_callback(self._handlers.discard)

    async def _run_handler(self, handler: MessageHandler, frame: dict[str, Any], kind: str) -> None:
        try:
            await handler(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 业务异常不许把机器人打下线
            logger.exception("机器人 {} 处理 {} 帧失败: {}", self.name, kind, exc)

    # ==================================================================
    # 心跳(A1-7)
    # ==================================================================

    async def _heartbeat_loop(self) -> None:
        """每 30 秒 ``ping``(``body=null``),单次超时 6 秒,连丢 2 次判死强断。"""
        while not self._stopping and self._ready:
            await asyncio.sleep(self.cfg.heartbeat_s)
            if self._stopping or not self._ready:
                return
            try:
                await self._send_wait(P.CMD_HEARTBEAT, None, timeout=self.cfg.heartbeat_timeout_s)
                self._missed_pong = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 心跳失败只计数,不抛
                self._missed_pong += 1
                logger.debug("机器人 {} 心跳失败({}/{}): {}", self.name, self._missed_pong, self.cfg.max_miss, exc)
            if self._missed_pong >= self.cfg.max_miss:
                logger.warning(
                    "机器人 {} 连续 {} 次心跳无响应,判定连接死亡,强制重连",
                    self.name,
                    self.cfg.max_miss,
                )
                await self._close_ws()
                return

    # ==================================================================
    # 发送(A1-6 ack 匹配 / A1-10 回复不等 ack)
    # ==================================================================

    async def _send_frame(self, cmd: str, rid: str, body: Any) -> None:
        ws = self._ws
        if ws is None:
            raise P.AiBotError("WebSocket 未连接")
        try:
            await ws.send(P.dumps_frame(P.build_frame(cmd, body, rid=rid)))
        except Exception as exc:  # noqa: BLE001
            raise P.AiBotError(f"发送失败: {exc}") from exc

    async def _send_wait(self, cmd: str, body: Any, *, timeout: float | None = None) -> dict[str, Any]:
        """发一帧并等 ack(按 ``req_id`` 唤醒,超时抛 :class:`AiBotError`)。"""
        rid = P.req_id(cmd)
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._send_frame(cmd, rid, body)
            frame = await asyncio.wait_for(fut, timeout=timeout or self.cfg.ack_timeout_s)
        except TimeoutError as exc:
            raise P.AiBotError(f"等待回执超时({timeout or self.cfg.ack_timeout_s}s): {cmd}") from exc
        finally:
            self._pending.pop(rid, None)
        return P.raise_for_errcode(frame, cmd)

    async def _send_only(self, cmd: str, rid: str, body: Any) -> None:
        """★ 只发送不等回执(A1-10)。

        原文理由(旧 ``aibot.py:239-240``):「回复消息服务端回执可能延迟 >8s,
        等待+超时重试会造成重复回复,因此回复类消息发送成功即视为完成。」
        """
        await self._send_frame(cmd, rid, body)

    # ---- 回复(回填入站 req_id)----

    async def reply_stream(self, frame: dict[str, Any], content: str, *, finish: bool = True) -> None:
        """回复文本流(A1-10)。★ **必须回填入站 ``headers.req_id``**,否则群内看不到回复。"""
        rid = P.req_id_of(frame)
        if not rid:
            raise P.AiBotError("入站帧缺 headers.req_id,无法回复")
        await self._send_only(P.CMD_RESPONSE, rid, P.stream_reply_body(content, finish=finish))

    async def reply_media(self, frame: dict[str, Any], media_type: str, media_id: str) -> None:
        """回复图片(A1-13:无 news 卡片,图文 = 文本 + 逐张图)。"""
        rid = P.req_id_of(frame)
        if not rid:
            raise P.AiBotError("入站帧缺 headers.req_id,无法回复")
        await self._send_only(P.CMD_RESPONSE, rid, P.media_body(media_type, media_id))

    async def reply_welcome(self, frame: dict[str, Any], content: str) -> None:
        """会话欢迎语(A1-11):``aibot_respond_welcome_msg``,须在 ``enter_chat`` 后 **5 秒内**。"""
        rid = P.req_id_of(frame)
        if not rid:
            raise P.AiBotError("入站帧缺 headers.req_id,无法回复欢迎语")
        await self._send_only(P.CMD_RESPONSE_WELCOME, rid, P.text_body(content))

    # ---- 主动推送(A1-12)----

    async def send_message(self, chatid: str, body: dict[str, Any]) -> dict[str, Any]:
        """主动推送(等 ack,失败抛错 —— 派发器据此重试)。"""
        return await self._send_wait(P.CMD_SEND_MSG, {"chatid": chatid, **body})

    async def send_markdown(self, chatid: str, content: str) -> dict[str, Any]:
        return await self.send_message(chatid, P.markdown_body(content))

    async def send_media(self, chatid: str, media_type: str, media_id: str) -> dict[str, Any]:
        return await self.send_message(chatid, P.media_body(media_type, media_id))

    # ==================================================================
    # 素材三步上传(A1-14)
    # ==================================================================

    async def upload_media(self, data: bytes, media_type: str, filename: str) -> str:
        """三步上传:``init`` → ``chunk`` ×N → ``finish`` → ``media_id``。

        ★ **512KB/片、>100 片报错**(A1-14 逐字);md5 为 **hex**;
        ``body`` 回读键分别是 ``upload_id`` / ``media_id``。
        """
        total = len(data)
        digest = hashlib.md5(data).hexdigest()  # noqa: S324 - 平台协议要求 md5,非安全用途
        total_chunks = max(1, (total + P.CHUNK_SIZE - 1) // P.CHUNK_SIZE)
        if total_chunks > self.cfg.max_chunks:
            raise P.AiBotError(
                f"文件过大({total} 字节 / {total_chunks} 片 > {self.cfg.max_chunks} 片上限)"
            )

        init = await self._send_wait(
            P.CMD_UPLOAD_INIT,
            {
                "type": media_type,
                "filename": filename,
                "total_size": total,
                "total_chunks": total_chunks,
                "md5": digest,
            },
            timeout=self.cfg.ack_timeout_s,
        )
        upload_id = P.body_of(init).get("upload_id")
        if not upload_id:
            raise P.AiBotError(f"上传初始化失败: {init}")

        for idx in range(total_chunks):
            chunk = data[idx * P.CHUNK_SIZE : (idx + 1) * P.CHUNK_SIZE]
            await self._send_wait(
                P.CMD_UPLOAD_CHUNK,
                {
                    "upload_id": upload_id,
                    "chunk_index": idx,
                    "base64_data": _b64(chunk),
                },
                timeout=max(self.cfg.ack_timeout_s, 20.0),
            )

        fin = await self._send_wait(
            P.CMD_UPLOAD_FINISH, {"upload_id": upload_id}, timeout=self.cfg.ack_timeout_s
        )
        media_id = P.body_of(fin).get("media_id")
        if not media_id:
            raise P.AiBotError(f"上传完成失败: {fin}")
        logger.info(
            "机器人 {} 素材上传成功: {} ({} 字节, {} 片)", self.name, filename, total, total_chunks
        )
        return str(media_id)

    # ==================================================================

    def snapshot(self) -> dict[str, Any]:
        """``/status`` 用(不含凭据)。"""
        return {
            **self.health(),
            "handlers": len(self._handlers),
            "pending_acks": len(self._pending),
            "since": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<BotClient {self.name} connected={self.connected}>"


def _b64(chunk: bytes) -> str:
    import base64

    return base64.b64encode(chunk).decode("ascii")
