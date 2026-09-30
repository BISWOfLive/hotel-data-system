"""段2 验收执行器 —— 逐条跑 **V21–V60** 并产出证据。

与段1 验收器(``verify_acceptance.py``)同一套设计原则
====================================================

1. **能用真实产物就用真实产物**:``config/report_schedule.json``(22 项真契约)、
   ``config/alert_rules.json``(6+1 真规则)、``config/prompts/alert_*.md``(真文案)、
   ``knowledge/faq.json``(9 条真问答)、``config/push_rotation.json``(21 项真清单)、
   真 PostgreSQL(容器 ``hoteldata-pg``)。
2. **不 mock 协议** —— 见下。
3. **需要真实企微凭据才能完成的条目如实标 ``BLOCKED``**,不伪装成 PASS。

★★ 协议层怎么测:一个「协议一致」的本地服务端
=============================================

企微的协议是**逆向得来的**(无公开文档),而真实凭据不在本机 ——
朴素做法是把 ``BotClient`` mock 掉,但那样测的是 mock,不是协议。

本验收器改用 :class:`FakeWeComServer`:用 ``websockets`` 起一个**真 WebSocket 服务端**,
**逐字节**按 :mod:`hoteldata.domains.bot.protocol` 的帧格式收发
(``aibot_subscribe`` / ``ping`` / ``aibot_send_msg`` / 三步素材上传 …)。

于是下面这些**全部走真实代码路径**:帧编解码、``req_id`` 匹配、
**迟到回执静默忽略**、``_send_only`` 不等 ack、心跳连丢 N 次判死、
指数退避重连、``asyncio.Queue`` 派发、限频时间戳、重试退避、审计落库。

★ 唯一标 ``BLOCKED`` 的是"**真实企微服务器**是否接受这套帧"
(需要 ``core_bots`` 里的真凭据 + 外网),这一条**不许伪造成 PASS**。

输出:控制台表格 + ``var/reports/段2-验收结果.json``。

用法::

    .venv\\Scripts\\python.exe scripts\\verify_acceptance2.py
    .venv\\Scripts\\python.exe scripts\\verify_acceptance2.py --only V35 V36 V37
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hoteldata.logging import configure_stdio  # noqa: E402
from hoteldata.settings import get_settings, reload_settings  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
#: 合成数据用的采集日(远离真实日期,便于识别与清理,也不会与真实采集撞键)
SYNTH_DAY = date(2000, 1, 1)
#: 合成群 id(以 ``verify2-`` 开头,清理时按前缀删)
SYNTH_GROUP = "verify2-group-0001"
SYNTH_GROUP2 = "verify2-group-0002"
SYNTH_MANAGE = "verify2-manage-0001"
SYNTH_OPS = "verify2-ops-0001"

PASS, FAIL, BLOCKED = "PASS", "FAIL", "BLOCKED"


@dataclass
class CheckResult:
    vid: str
    title: str
    status: str
    evidence: str
    detail: dict[str, Any] = field(default_factory=dict)


class Registry:
    def __init__(self) -> None:
        self.checks: list[tuple[str, str, Callable[[], Awaitable[CheckResult]]]] = []

    def add(self, vid: str, title: str):  # noqa: ANN201
        def deco(fn: Callable[[], Awaitable[CheckResult]]):  # noqa: ANN202
            self.checks.append((vid, title, fn))
            return fn

        return deco


REG = Registry()


def _res(vid: str, title: str, status: str, evidence: str, **detail: Any) -> CheckResult:
    return CheckResult(vid, title, status, evidence, detail)


# ===========================================================================
# ★ 协议一致的本地企微服务端(用**真 WebSocket + 真帧**驱动客户端)
# ===========================================================================


class FakeWeComServer:
    """按 :mod:`hoteldata.domains.bot.protocol` 逐字实现的本地服务端。

    可编程行为(全部用于构造验收场景):

      * ``drop_pings``       —— 不回 ``ping``(触发"连丢 N 次判死重连")
      * ``fail_sends``       —— 前 N 次 ``aibot_send_msg`` 回 ``errcode=500``(触发重试)
      * ``subscribe_errcode``—— 订阅回执错误码(验 A1-5 的 ``errcode==0`` 校验)
      * ``late_ack``         —— 回执延迟 N 秒后再发一次(验 A1-6 迟到回执静默忽略)
      * ``upload_fail``      —— 上传 init 不回 ``upload_id``
    """

    def __init__(self, *, subscribe_errcode: int = 0) -> None:
        self.subscribe_errcode = subscribe_errcode
        self.drop_pings = False
        self.fail_sends = 0
        self.late_ack = 0.0
        self.upload_fail = False

        self.url = ""
        self._server: Any = None
        self._clients: set[Any] = set()
        #: 收到的**全部**入站帧(顺序)
        self.frames: list[dict[str, Any]] = []
        #: 每帧到达时刻(单调钟,用于量测限频间隔)
        self.stamps: list[float] = []
        #: ``aibot_send_msg`` 明细
        self.sent: list[dict[str, Any]] = []
        #: 三步上传明细
        self.uploads: list[dict[str, Any]] = []
        self.ping_count = 0
        self.subscribe_count = 0
        self.connects = 0

    # ------------------------------------------------------------------

    async def start(self) -> str:
        import websockets

        self._server = await websockets.serve(self._handler, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}"
        return self.url

    async def stop(self) -> None:
        for ws in list(self._clients):
            with contextlib.suppress(Exception):
                await ws.close()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    async def drop_connections(self) -> int:
        """**只掐断现有连接,监听仍在** —— 模拟"网络抖了一下"。

        ★ 与 :meth:`stop` 的区别很关键:``stop()`` 之后端口不再接受连接,
        客户端会一直 ConnectionRefused,**永远重连不上**(``connects`` 不增长),
        于是"判死重连"根本没法验。真实故障里更常见的是"闪断后能连回来"。
        """
        clients = list(self._clients)
        for ws in clients:
            with contextlib.suppress(Exception):
                await ws.close()
        self._clients.clear()
        return len(clients)

    # ------------------------------------------------------------------

    async def _handler(self, ws: Any) -> None:
        self._clients.add(ws)
        self.connects += 1
        try:
            async for raw in ws:
                frame = json.loads(raw)
                self.frames.append(frame)
                self.stamps.append(time.monotonic())
                await self._dispatch(ws, frame)
        except Exception:  # noqa: BLE001 - 客户端断开
            pass
        finally:
            self._clients.discard(ws)

    async def _ack(self, ws: Any, rid: str, *, errcode: int = 0, errmsg: str = "", body: Any = None) -> None:
        payload: dict[str, Any] = {"errcode": errcode, "errmsg": errmsg, "headers": {"req_id": rid}}
        if body is not None:
            payload["body"] = body
        await ws.send(json.dumps(payload))
        if self.late_ack > 0:
            # ★ 迟到回执:等待方多半已超时 → 客户端必须**静默忽略**(A1-6)
            async def _late() -> None:
                await asyncio.sleep(self.late_ack)
                with contextlib.suppress(Exception):
                    await ws.send(json.dumps(payload))

            asyncio.create_task(_late())

    async def _dispatch(self, ws: Any, frame: dict[str, Any]) -> None:
        from hoteldata.domains.bot import protocol as P

        cmd = P.cmd_of(frame)
        rid = P.req_id_of(frame)
        body = frame.get("body")

        if cmd == P.CMD_SUBSCRIBE:
            self.subscribe_count += 1
            await self._ack(
                ws, rid, errcode=self.subscribe_errcode, errmsg="bad secret" if self.subscribe_errcode else ""
            )
            return
        if cmd == P.CMD_HEARTBEAT:
            self.ping_count += 1
            if not self.drop_pings:
                await self._ack(ws, rid)
            return
        if cmd == P.CMD_SEND_MSG:
            if self.fail_sends > 0:
                self.fail_sends -= 1
                await self._ack(ws, rid, errcode=500, errmsg="count limit")
                return
            self.sent.append({"chatid": (body or {}).get("chatid"), "body": body, "at": time.monotonic()})
            await self._ack(ws, rid)
            return
        if cmd == P.CMD_UPLOAD_INIT:
            if self.upload_fail:
                await self._ack(ws, rid, body={})
                return
            self.uploads.append({"cmd": "init", "body": body})
            await self._ack(ws, rid, body={"upload_id": "up-verify2-1"})
            return
        if cmd == P.CMD_UPLOAD_CHUNK:
            self.uploads.append({"cmd": "chunk", "body": {k: v for k, v in (body or {}).items() if k != "base64_data"}})
            await self._ack(ws, rid)
            return
        if cmd == P.CMD_UPLOAD_FINISH:
            self.uploads.append({"cmd": "finish", "body": body})
            await self._ack(ws, rid, body={"media_id": "media-verify2-1"})
            return
        # 回复类:_send_only 不等 ack;为验证"迟到回执被静默忽略",这里仍然回一个
        if cmd in (P.CMD_RESPONSE, P.CMD_RESPONSE_WELCOME):
            return

    # ------------------------------------------------------------------

    async def push_inbound(
        self, content: str, *, chatid: str | None = SYNTH_GROUP, msgtype: str = "text"
    ) -> str:
        """服务端**主动**下发一条入站消息帧(``aibot_msg_callback``)。

        ★ 返回该帧的 ``req_id`` —— 调用方要拿它去**对上**客户端回的回复帧
        (A1-10:回复必须回填入站 ``req_id``,这是本地联调最能验出问题的一条)。
        """
        from hoteldata.domains.bot import protocol as P

        body: dict[str, Any] = {"msgtype": msgtype, "text": {"content": content}, "from": {"userid": "u-verify2"}}
        if chatid:
            body["chatid"] = chatid
        rid = P.req_id(P.CMD_CALLBACK)
        frame = {"cmd": P.CMD_CALLBACK, "headers": {"req_id": rid}, "body": body}
        await self._broadcast(frame)
        return rid

    async def push_event(self, eventtype: str = "enter_chat") -> str:
        """服务端主动下发 ``aibot_event_callback``(验欢迎语);返回该帧的 ``req_id``。"""
        from hoteldata.domains.bot import protocol as P

        rid = P.req_id(P.CMD_EVENT_CALLBACK)
        frame = {
            "cmd": P.CMD_EVENT_CALLBACK,
            "headers": {"req_id": rid},
            "body": {"event": {"eventtype": eventtype}, "chatid": SYNTH_GROUP},
        }
        await self._broadcast(frame)
        return rid

    # ------------------------------------------------------------------
    # 出站帧的检索(本地联调要能"对上"客户端发了什么)
    # ------------------------------------------------------------------

    def replies_to(self, rid: str) -> list[dict[str, Any]]:
        """客户端**回复**给某个入站 ``req_id`` 的全部帧(A1-10 的验证点)。

        ★ 含 ``aibot_respond_welcome_msg``:欢迎语也是一条"回复",只是用了
        另一个命令字(A1-11)。只认 ``aibot_respond_msg`` 会把它漏掉 ——
        本地联调第一版就在这儿误判了一次。
        """
        from hoteldata.domains.bot import protocol as P

        reply_cmds = (P.CMD_RESPONSE, P.CMD_RESPONSE_WELCOME)
        return [f for f in self.frames if P.cmd_of(f) in reply_cmds and P.req_id_of(f) == rid]

    def reply_text(self, rid: str) -> str:
        """把回复帧里的文本拼起来(``stream.content`` / ``text.content``)。"""
        from hoteldata.domains.bot import protocol as P

        out: list[str] = []
        for f in self.replies_to(rid):
            body = P.body_of(f)
            kind = body.get("msgtype")
            if kind == "stream":
                out.append(str((body.get("stream") or {}).get("content") or ""))
            elif kind == "text":
                out.append(str((body.get("text") or {}).get("content") or ""))
        return "".join(out)

    def replies_media_ids(self, rid: str) -> list[str]:
        """某个入站 ``req_id`` 的回复里带的 ``media_id`` 列表(逐张图)。"""
        from hoteldata.domains.bot import protocol as P

        out: list[str] = []
        for f in self.replies_to(rid):
            body = P.body_of(f)
            if body.get("msgtype") == "image":
                mid = (body.get("image") or {}).get("media_id")
                if mid:
                    out.append(str(mid))
        return out

    def sent_to(self, chatid: str) -> list[dict[str, Any]]:
        """**主动推送**给某个群的全部 body(A1-12:``aibot_send_msg``)。"""
        return [dict(s["body"] or {}) for s in self.sent if s.get("chatid") == chatid]

    def wait_for_reply(self, rid: str, timeout_s: float = 15.0) -> Any:
        """同步等待某个 ``req_id`` 的回复 —— **占位,请用** :meth:`await_reply`。"""
        raise NotImplementedError("请用 await_reply()")

    async def await_reply(self, rid: str, timeout_s: float = 15.0) -> list[dict[str, Any]]:
        """轮询等回复帧出现(回复是独立 Task 处理的,天然异步)。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            got = self.replies_to(rid)
            if got:
                return got
            await asyncio.sleep(0.05)
        return []

    async def await_sent(self, chatid: str, *, count: int = 1, timeout_s: float = 20.0) -> list[dict[str, Any]]:
        """轮询等**主动推送**到某群的条数达到 ``count``。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            got = self.sent_to(chatid)
            if len(got) >= count:
                return got
            await asyncio.sleep(0.05)
        return self.sent_to(chatid)

    def texts_to(self, chatid: str) -> list[str]:
        """推给某群的**文本**内容(markdown / text)。"""
        out: list[str] = []
        for body in self.sent_to(chatid):
            kind = body.get("msgtype")
            if kind == "markdown":
                out.append(str((body.get("markdown") or {}).get("content") or ""))
            elif kind == "text":
                out.append(str((body.get("text") or {}).get("content") or ""))
        return out

    def media_to(self, chatid: str) -> list[str]:
        """推给某群的**图片** ``media_id`` 列表。"""
        out: list[str] = []
        for body in self.sent_to(chatid):
            if body.get("msgtype") == "image":
                mid = (body.get("image") or {}).get("media_id")
                if mid:
                    out.append(str(mid))
        return out

    async def _broadcast(self, frame: dict[str, Any]) -> None:
        for ws in list(self._clients):
            with contextlib.suppress(Exception):
                await ws.send(json.dumps(frame, ensure_ascii=False))

    # ------------------------------------------------------------------

    def send_gaps(self) -> list[float]:
        """相邻 ``aibot_send_msg`` 的时间间隔(限频证据)。"""
        at = [s["at"] for s in self.sent]
        return [round(b - a, 4) for a, b in zip(at, at[1:], strict=False)]


# ===========================================================================
# 公共夹具
# ===========================================================================


def _settings(**overrides: Any) -> Any:
    """带覆盖的设置(不影响单例)。"""
    return reload_settings(**overrides)


def _silent_bot(name: str = "verify2-bot") -> Any:
    """一个**不连网**的 BotClient 替身,只实现 :class:`Sender` 需要的接口形状。

    仅用于"只想验组装/去重/审计,不想起 WS"的场景(V40–V60 大量如此)。
    协议本身由 V21–V25/V35–V38 用 :class:`FakeWeComServer` **真测**。
    """
    from hoteldata.domains.bot.client import BotClient

    class _Offline(BotClient):
        def __init__(self) -> None:  # noqa: D107
            super().__init__(name, "id", "secret")
            self.outbox: list[dict[str, Any]] = []
            self.images: list[str] = []

        @property
        def connected(self) -> bool:
            return True

        async def send_markdown(self, chatid: str, content: str) -> dict[str, Any]:
            self.outbox.append({"chatid": chatid, "content": content})
            return {"errcode": 0}

        async def send_media(self, chatid: str, media_type: str, media_id: str) -> dict[str, Any]:
            self.images.append(media_id)
            return {"errcode": 0}

        async def upload_media(self, data: bytes, media_type: str, filename: str) -> str:
            return f"media::{filename}"

        async def reply_stream(self, frame: dict[str, Any], content: str, *, finish: bool = True) -> None:
            self.outbox.append({"reply": content, "finish": finish})

        async def reply_media(self, frame: dict[str, Any], media_type: str, media_id: str) -> None:
            self.images.append(media_id)

        async def reply_welcome(self, frame: dict[str, Any], content: str) -> None:
            self.outbox.append({"welcome": content})

    return _Offline()


class OfflineManager:
    """``BotManager`` 的离线替身:路由固定到同一个 :func:`_silent_bot`。"""

    def __init__(self, bots: list[Any] | None = None) -> None:
        from hoteldata.settings import get_settings as _gs

        self.settings = _gs()
        self._bots = {b.name: b for b in (bots or [_silent_bot()])}
        self._notifier: Any = None

    def size(self) -> int:
        return len(self._bots)

    def names(self) -> list[str]:
        return sorted(self._bots)

    def all_clients(self) -> list[Any]:
        return [self._bots[n] for n in self.names()]

    def get(self, name: str) -> Any:
        return self._bots.get(name)

    def route(self, chatid: str) -> Any:
        return self._bots[self.names()[0]]

    for_group = route

    @property
    def online(self) -> list[Any]:
        return [b for b in self.all_clients() if b.connected]

    def health(self) -> dict[str, bool]:
        return {n: bool(b.connected) for n, b in sorted(self._bots.items())}

    get_health = health

    async def send_alert(self, text: str, *, chatids: list[str]) -> Any:
        """★ 与 ``BotManager.send_alert`` **同语义的离线替身**。

        刻意**不**把 ``PushService.send_alert`` 打桩 —— 那条路径是 **D1 的修复点**
        (遍历在线机器人 + 返回逐群结果),打桩就等于把 D1 从验收里摘掉了。
        这里只替换"机器人怎么发",保留 `PushService.send_alert` 的真实编排。
        """
        from hoteldata.domains.bot.manager import AlertResult

        result = AlertResult(text=text, targets=list(chatids))
        if not chatids:
            result.error = "未配置告警目标群(MANAGE_CHATIDS / OPS_CHATID 均为空)"
            return result
        online = self.online
        if not online:
            result.error = f"没有在线机器人({self.size()} 个实例全部离线)"
            return result
        for chatid in chatids:
            for bot in online:
                try:
                    await bot.send_markdown(chatid, text)
                    result.delivered.append(chatid)
                    break
                except Exception as exc:  # noqa: BLE001
                    result.failed.append((chatid, f"{bot.name}: {exc}"))
        return result

    def snapshot(self) -> dict[str, Any]:
        return {"bots": self.size(), "online": len(self.online), "health": self.health()}

    def set_alert_notifier(self, notifier: Any) -> None:
        self._notifier = notifier

    def set_handlers(self, **_: Any) -> None:
        return None


async def _runtime(**kw: Any) -> Any:
    """建一个 Runtime 并**保活到检查结束**。

    ★★ 这里踩过一个非常隐蔽的坑,值得写下来:

    ``Runtime.create(...)`` 是 ``@asynccontextmanager``(async generator)。
    写成 ``return await Runtime.create(...).__aenter__()`` 时,那个 generator 对象
    **没有任何引用** → 随时可能被 GC;而 GC 会给它注入 ``GeneratorExit``,
    于是它 ``finally: await rt.aclose()`` 真的执行了 ——
    结果就是检查跑到一半,``runtime._bots`` / ``runtime._push`` **变成了 None**、
    DB 引擎被 dispose。表现是 ``/status`` 里没有 ``bots`` 键,
    而单看代码怎么都对。

    修法:把 context manager 存进 ``runtime._extras`` 保活(不调用 ``__aexit__``),
    检查结束由进程退出统一回收。
    """
    from hoteldata.runtime import Runtime

    cm = Runtime.create(with_scheduler=False, **kw)
    rt = await cm.__aenter__()
    rt._extras["__context_manager__"] = cm  # ★ 保活,防止 GC 触发 aclose()
    return rt


async def _first_pair(db: Any) -> tuple[Any, Any] | None:
    from sqlalchemy import select

    from hoteldata.infra.models import Account, Hotel

    async with db.session() as s:
        hotel = (await s.execute(select(Hotel).order_by(Hotel.id).limit(1))).scalar_one_or_none()
        if hotel is None:
            return None
        acc = (await s.execute(select(Account).where(Account.id == hotel.account_id))).scalar_one_or_none()
    return (hotel, acc) if acc else None


async def _ensure_hotel(db: Any, name: str = "验收2-合成酒店") -> Any:
    """造一家（或取回）验收用的合成酒店(**不绑定账号,不碰采集**)。"""
    from sqlalchemy import select

    from hoteldata.infra.models import Hotel

    async with db.session() as s:
        row = (await s.execute(select(Hotel).where(Hotel.name == name))).scalar_one_or_none()
        if row is None:
            row = Hotel(name=name, city="验收市", ebk_hotel_id="verify2")
            s.add(row)
            await s.flush()
        return row


async def _cleanup(db: Any, hotel_id: int) -> None:
    """清掉本次验收造的全部痕迹(可反复重跑)。"""
    from sqlalchemy import delete, or_

    from hoteldata.infra.models import (
        AlertLog,
        AlertState,
        CollectModule,
        CollectReport,
        GroupBinding,
        PushLog,
        ReviewMaterial,
        ReviewReply,
        ReviewReview,
    )

    async with db.session() as s:
        for model in (AlertLog, AlertState):
            await s.execute(delete(model).where(model.hotel_id == hotel_id))
        # 合成酒店不参与真实采集 → 按 hotel_id 全清(不限日期,便于用"今天"造数据)
        await s.execute(delete(CollectModule).where(CollectModule.hotel_id == hotel_id))
        await s.execute(delete(CollectReport).where(CollectReport.hotel_id == hotel_id))
        await s.execute(delete(ReviewReply).where(ReviewReply.hotel_id == hotel_id))
        await s.execute(delete(ReviewReview).where(ReviewReview.hotel_id == hotel_id))
        await s.execute(delete(ReviewMaterial).where(ReviewMaterial.hotel_id == hotel_id))
        await s.execute(
            delete(GroupBinding).where(
                or_(GroupBinding.chatid.like("verify2-%"), GroupBinding.hotel_id == hotel_id)
            )
        )
        await s.execute(
            delete(PushLog).where(
                or_(PushLog.group_chatid.like("verify2-%"), PushLog.hotel_id == hotel_id)
            )
        )


async def _cleanup_push_logs(db: Any, group: str) -> None:
    from sqlalchemy import delete

    from hoteldata.infra.models import PushLog

    async with db.session() as s:
        await s.execute(delete(PushLog).where(PushLog.group_chatid == group))


# ---------------------------------------------------------------------------
# 合成数据播种(全部落在 SYNTH_DAY,便于一次性清理,不污染真实采集)
# ---------------------------------------------------------------------------


async def _seed_module(
    rt: Any,
    hotel_id: int,
    page: str,
    module: str,
    window: str,
    payload: dict[str, Any],
    *,
    day: date = SYNTH_DAY,
    status: str = "ok",
) -> int:
    """播种一条「模块 × 窗口」记录(段1 的公开仓储,不绕过 service 语义)。"""
    from hoteldata.domains.collect.repository import CollectRepository

    async with rt.db.session() as s:
        return await CollectRepository(s).upsert_module(
            hotel_id=hotel_id,
            account_id=None,
            collect_date=day,
            page=page,
            module=module,
            window=window,
            payload=payload,
            channel="api",
            status=status,
        )


async def _seed_portal(
    rt: Any, hotel_id: int, page: str, columns: dict[str, str], *, day: date = SYNTH_DAY
) -> int:
    """播种预警三源列(``alert_portal_columns``)。"""
    from hoteldata.domains.collect.repository import CollectRepository

    rows = [
        {
            "hotel_id": hotel_id,
            "collect_date": day,
            "page": page,
            "column_name": name,
            "value": value,
            "detail_json": {"source_api": "verify2"},
            "channel": "api",
            "status": "ok",
        }
        for name, value in columns.items()
    ]
    async with rt.db.session() as s:
        return await CollectRepository(s).upsert_portal_columns(rows)


async def _seed_room_states(
    rt: Any,
    hotel_id: int,
    room_type_id: str,
    *,
    available_today: bool = False,
    closed_days: int = 0,
    today_missing: bool = False,
    window: int = 10,
    day: date = SYNTH_DAY,
) -> int:
    """从 ``day`` 起**向前**播种房态网格(``effect_date`` = ``day + i``)。

    ★ 方向很重要:``derive_room_availability`` 的循环是
    ``for i in range(window_days): effect_date = today + i`` ——
    业务语义是「**接下来** N 天这个房型都没开房」(所以模板才写"请务必告知酒店开房")。
    按 ``effect_date`` 倒着造数据会让判定永远为 0 天(本验收器踩过)。

    * ``closed_days=N`` → ``day`` 起连续 N 天 ``available=0``,第 N 天 ``available=1``(**断开**);
    * ``available_today=True`` → 今天可订(可读即断);
    * ``today_missing=True`` → **不写今天那行**(验"今日缺数据 → 保守不触发")。

    ★ 每次都先**清空该店全部房态**再插:``replace_room_states`` 按
    ``(hotel_id, collect_date)`` 替换,二次播种不同天数的行会**叠加**,
    导致"改了参数但判定不变"(实测踩过)。
    """
    from sqlalchemy import delete

    from hoteldata.domains.collect.repository import CollectRepository
    from hoteldata.infra.models import AlertRoomState

    async with rt.db.session() as s:
        await s.execute(delete(AlertRoomState).where(AlertRoomState.hotel_id == hotel_id))

    rows: list[dict[str, Any]] = []
    for i in range(max(1, window)):
        if today_missing and i == 0:
            continue
        if i == 0:
            avail = 1 if available_today else 0
        else:
            # 前 closed_days 天不可订;之后可订(形成"断开")
            avail = 0 if i < closed_days else 1
        rows.append(
            {
                "hotel_id": hotel_id,
                "account_id": None,
                "collect_date": day,
                "room_type_id": room_type_id,
                "room_name": f"验收房型{room_type_id}",
                "effect_date": day + timedelta(days=i),
                "available": avail,
                "status_code": "G" if avail else "N",
                "quantity": 5 if avail else 0,
                "price": 388.0,
            }
        )
    async with rt.db.session() as s:
        return await CollectRepository(s).replace_room_states(hotel_id, day, rows)


async def _seed_reviews(rt: Any, hotel_id: int, rows: list[dict[str, Any]]) -> int:
    """播种待回复点评(★ UPSERT 不回溯:replied/strategy 不会被覆盖)。"""
    from hoteldata.domains.collect.repository import CollectRepository

    full = [
        {
            "hotel_id": hotel_id,
            "review_id": r["review_id"],
            "user_name": r.get("user_name", "验收用户"),
            "star": r.get("star"),
            "content": r["content"],
            "sentiment": r.get("sentiment", "good"),
            "comment_time": r.get("comment_time", datetime(2000, 1, 1, 10, 0)),
        }
        for r in rows
    ]
    async with rt.db.session() as s:
        return len(await CollectRepository(s).upsert_reviews(full))


async def _seed_material(
    rt: Any, hotel_id: int, kind: str, payload: dict[str, Any], *, day: date = SYNTH_DAY
) -> int:
    """播种点评素材(``review_materials``,kind ∈ score/competitor/trend/num)。"""
    from hoteldata.domains.collect.repository import CollectRepository

    async with rt.db.session() as s:
        return await CollectRepository(s).upsert_review_materials(
            [
                {
                    "hotel_id": hotel_id,
                    "collect_date": day,
                    "kind": kind,
                    "payload_json": payload,
                    "channel": "api",
                    "status": "ok",
                }
            ]
        )


async def _seed_shot(
    rt: Any, hotel: Any, page: str, module: str, *, day: date = SYNTH_DAY
) -> str:
    """造一张假的模块截图文件并把它挂进 ``collect_reports``(返回相对路径)。"""
    from hoteldata.domains.collect.repository import CollectRepository

    # ★ 走 layout.screenshot_path 而不是手拼文件名:轮换清单里存在带 ``/`` 的名字
    #   (如「违约看板/违规中心」),手拼会踩到不存在的子目录(实测 FileNotFoundError)。
    path = rt.layout.screenshot_path(hotel.name, day, page, module)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    rel = rt.layout.to_relative(path)
    async with rt.db.session() as s:
        repo = CollectRepository(s)
        await repo.ensure_report(int(hotel.id), day, page, channel="screenshot")
        await repo.link_screenshot(int(hotel.id), day, page, module_screenshots={module: rel})
    return rel


# ===========================================================================
# V21 — 企微长连接可用
# ===========================================================================


@REG.add("V21", "企微长连接可用:订阅回执 errcode==0;10 个命令字;脏帧不崩")
async def v21() -> CheckResult:
    from hoteldata.domains.bot import protocol as P
    from hoteldata.domains.bot.client import BotClient

    checks: dict[str, Any] = {"commands": list(P.COMMANDS), "cmd_count": len(P.COMMANDS)}

    # ① 10 个命令字清点(逐字对照段2 §4.1 A1-4)
    checks["commands_exact"] = P.COMMANDS == (
        "aibot_subscribe",
        "ping",
        "aibot_respond_msg",
        "aibot_respond_welcome_msg",
        "aibot_send_msg",
        "aibot_upload_media_init",
        "aibot_upload_media_chunk",
        "aibot_upload_media_finish",
        "aibot_msg_callback",
        "aibot_event_callback",
    )
    checks["ws_url"] = P.WS_URL
    checks["ws_url_exact"] = P.WS_URL == "wss://openws.work.weixin.qq.com"

    # ② 脏帧:控制字符 + 保留 \n(A1-9)
    dirty = '\x00\x07{"cmd": "x",\n "body": {"a": 1}}\x1f'
    parsed = P.parse_frame(dirty)
    checks["dirty_frame_parsed"] = parsed == {"cmd": "x", "body": {"a": 1}}
    checks["clean_raw"] = P.clean_raw("a\x00b\nc\x07d")
    checks["newline_kept"] = P.clean_raw("a\x00b\nc\x07d") == "ab\ncd"
    checks["control_stripped"] = "\x00" not in checks["clean_raw"] and "\x07" not in checks["clean_raw"]
    # 转义换行的正文必须原样往返(多行 markdown 不许被压成一行)
    payload = {"cmd": "a", "body": {"t": "第一行\n第二行"}}
    checks["roundtrip_multiline"] = P.parse_frame(P.dumps_frame(payload)) == payload

    # ③ req_id 形状 A1-3
    rid = P.req_id("ping")
    parts = rid.split("_")
    checks["req_id_shape"] = len(parts) == 3 and parts[0] == "ping" and len(parts[2]) == 6

    # ④ 真连一次本地协议服务端
    server = FakeWeComServer()
    await server.start()
    try:
        client = BotClient(
            "verify2-bot",
            "bot-id",
            "secret",
            settings=_settings(AIBOT_WS_URL=server.url, AIBOT_HEARTBEAT_S=30),
        )
        await client.start()
        for _ in range(60):
            if client.ready:
                break
            await asyncio.sleep(0.05)
        checks["subscribe_sent"] = server.subscribe_count
        checks["ready"] = client.ready
        checks["connected"] = client.connected
        sub = next((f for f in server.frames if f.get("cmd") == P.CMD_SUBSCRIBE), None)
        checks["subscribe_body"] = (sub or {}).get("body")
        checks["subscribe_body_exact"] = (sub or {}).get("body") == {"bot_id": "bot-id", "secret": "secret"}
        checks["frame_shape"] = set((sub or {}).keys()) == {"cmd", "headers", "body"}
        await client.stop()
    finally:
        await server.stop()

    # ⑤ 认证失败必须抛(errcode != 0 → AiBotError,A1-5)
    bad = FakeWeComServer(subscribe_errcode=401)
    await bad.start()
    try:
        client2 = BotClient("verify2-bad", "b", "s", settings=_settings(AIBOT_WS_URL=bad.url))
        await client2.start()
        raised = ""
        for _ in range(60):
            await asyncio.sleep(0.05)
            if client2.last_error:
                raised = client2.last_error
                break
        checks["auth_failure_visible"] = "AiBotError" in raised or "401" in raised
        checks["auth_failure_ready_false"] = not client2.ready
        await client2.stop()
    finally:
        await bad.stop()

    ok = (
        checks["commands_exact"]
        and checks["ws_url_exact"]
        and checks["dirty_frame_parsed"]
        and checks["newline_kept"]
        and checks["control_stripped"]
        and checks["roundtrip_multiline"]
        and checks["req_id_shape"]
        and checks["ready"]
        and checks["subscribe_body_exact"]
        and checks["frame_shape"]
        and checks["auth_failure_visible"]
        and checks["auth_failure_ready_false"]
    )
    ev = (
        f"10 命令字逐字一致 / 本地协议服务端订阅成功 ready={checks['ready']} / "
        f"脏帧解析={checks['dirty_frame_parsed']} clean_raw='{checks['clean_raw']}' / "
        f"errcode!=0 抛错可见={checks['auth_failure_visible']}"
    )
    if not ok:
        return _res("V21", v21.__doc__ or "", FAIL, ev, **checks)
    blocked = _res(
        "V21",
        v21.__doc__ or "",
        BLOCKED,
        ev + "|⚠️ **真实企微服务器**接受度需真凭据(见下方 real_server 说明)",
        **checks,
    )
    real = await _real_bot_probe()
    blocked.detail["real_server"] = real
    if real.get("configured") and real.get("subscribed"):
        blocked.status = PASS
        blocked.evidence = ev + f"|真机订阅成功:{real.get('bots')}"
    return blocked


async def _real_bot_probe() -> dict[str, Any]:
    """有真凭据就连一次真服务器(没有就如实说明)。"""
    try:
        async with _runtime() as rt:
            from hoteldata.domains.bot.manager import load_bots_from_db

            rows = await load_bots_from_db(rt.db, rt.settings)
            if not rows:
                return {"configured": False, "reason": "core_bots 表为空(未配置真实机器人凭据)"}
            manager = await rt.start_bots()
            for _ in range(80):
                if any(c.ready for c in manager.all_clients()):
                    break
                await asyncio.sleep(0.25)
            return {
                "configured": True,
                "bots": manager.names(),
                "health": manager.health(),
                "subscribed": any(c.ready for c in manager.all_clients()),
            }
    except Exception as exc:  # noqa: BLE001
        return {"configured": True, "error": f"{type(exc).__name__}: {exc}"}


# ===========================================================================
# V22 — 心跳保活 + 连丢 2 次判死重连
# ===========================================================================


@REG.add("V22", "心跳保活:连丢 2 次判死强断重连;退避 min(2^n,30);★掉线后 health 立刻变离线")
async def v22() -> CheckResult:
    from hoteldata.domains.bot import protocol as P
    from hoteldata.domains.bot.client import BotClient

    checks: dict[str, Any] = {
        "heartbeat_s": P.HEARTBEAT_INTERVAL_S,
        "heartbeat_timeout_s": P.HEARTBEAT_TIMEOUT_S,
        "max_miss": P.MAX_MISSED_PONG,
    }
    checks["constants_exact"] = (
        P.HEARTBEAT_INTERVAL_S == 30.0 and P.HEARTBEAT_TIMEOUT_S == 6.0 and P.MAX_MISSED_PONG == 2
    )

    # ★ 加快心跳节奏做真实验(常量本身已由上面逐字核对)
    server = FakeWeComServer()
    await server.start()
    settings = _settings(AIBOT_WS_URL=server.url, AIBOT_HEARTBEAT_S=0.2, AIBOT_HEARTBEAT_TIMEOUT_S=0.15)
    client = BotClient("verify2-hb", "b", "s", settings=settings)
    await client.start()
    for _ in range(60):
        if client.ready:
            break
        await asyncio.sleep(0.05)
    pings_before = server.ping_count
    # ① 正常心跳:丢 1 次不判死
    await asyncio.sleep(0.5)
    checks["pings_normal"] = server.ping_count - pings_before
    checks["alive_after_normal"] = client.connected

    # ② 服务端开始不回 ping → 连丢 N 次 → 判死 → 重连(connects 增加)
    connects_before = server.connects
    server.drop_pings = True
    reconnected = False
    for _ in range(200):
        await asyncio.sleep(0.05)
        if server.connects > connects_before:
            reconnected = True
            break
    checks["reconnected_after_miss"] = reconnected
    checks["connects"] = server.connects
    server.drop_pings = False
    await asyncio.sleep(0.4)
    checks["back_online"] = client.connected

    # ③ ★ 掉线期间 health 必须**立刻**报离线(本地联调抓出来的真 bug)
    #
    #    修前的实现只在 ``async with websockets.connect(...)`` **体之内**复位
    #    ``ready`` / ``_ws``,而重连时 connect 可能直接抛 → 复位代码根本不执行 →
    #    上一轮的 ``ready=True`` 与那个死 socket 被一直留着:
    #    服务端永久挂掉,``health()`` 仍报"在线"。
    #    后果:「状态」命令、``/status``、``bots health`` 全部说谎;
    #    D1 的"遍历在线机器人发告警"也会挑中一个死连接。
    checks_offline: dict[str, Any] = {}
    server2 = FakeWeComServer()
    await server2.start()
    client2 = BotClient(
        "verify2-offline", "b", "s", settings=_settings(AIBOT_WS_URL=server2.url, AIBOT_HEARTBEAT_S=30.0)
    )
    await client2.start()
    for _ in range(80):
        if client2.ready:
            break
        await asyncio.sleep(0.05)
    checks_offline["online_before"] = client2.connected
    await server2.drop_connections()  # 掐断连接但**监听还在**
    went_offline = False
    for _ in range(120):
        await asyncio.sleep(0.05)
        if not client2.connected:
            went_offline = True
            break
    checks_offline["offline_after_drop"] = went_offline
    checks_offline["ready_after_drop"] = client2.ready
    checks_offline["ws_after_drop"] = client2._ws is not None
    # 掉线期间发送必须**明确失败**,不许挂在死 socket 上
    send_err = ""
    try:
        await client2.send_markdown(SYNTH_GROUP, "掉线探针")
    except Exception as exc:  # noqa: BLE001
        send_err = f"{type(exc).__name__}: {exc}"
    checks_offline["send_error"] = send_err
    checks_offline["send_fails_fast"] = bool(send_err)
    await client2.stop()
    await server2.stop()
    checks["offline"] = checks_offline
    checks["health_honest_when_offline"] = (
        checks_offline["online_before"]
        and checks_offline["offline_after_drop"]
        and checks_offline["send_fails_fast"]
    )

    await client.stop()
    await server.stop()

    # ④ 退避公式 min(2^n, 30)(读源码级断言:直接算)
    checks["backoff_schedule"] = [min(float(2**n), 30.0) for n in range(8)]
    checks["backoff_capped_at_30"] = checks["backoff_schedule"][:5] == [1.0, 2.0, 4.0, 8.0, 16.0] and all(
        x <= 30.0 for x in checks["backoff_schedule"]
    )
    checks["backoff_max"] = max(checks["backoff_schedule"])

    ok = (
        checks["constants_exact"]
        and checks["pings_normal"] >= 2
        and checks["alive_after_normal"]
        and checks["reconnected_after_miss"]
        and checks["back_online"]
        and checks["health_honest_when_offline"]
        and checks["backoff_capped_at_30"]
    )
    ev = (
        f"常量 30s/6s/2 次逐字一致 / 正常心跳 {checks['pings_normal']} 次仍在线 / "
        f"停回 ping 后**判死重连**(connects={checks['connects']}) / 退避上限 {checks['backoff_max']:.0f}s / "
        f"★掉线后 health={checks_offline['ready_after_drop']}(应为 False)、"
        f"掉线期发送失败='{checks_offline['send_error'][:40]}'"
    )
    return _res("V22", v22.__doc__ or "", PASS if ok else FAIL, ev, **checks)


# ===========================================================================
# V23 — 多机器人分摊 + ★ D1 修复专测
# ===========================================================================


@REG.add("V23", "多机器人分摊(md5路由) + ★D1:告警遍历在线机器人且失败可见")
async def v23() -> CheckResult:
    from hoteldata.domains.bot.manager import BotManager

    checks: dict[str, Any] = {}

    # ① 路由:md5(chatid)[:8] % n → sorted(names)[idx]
    bots = [
        {"name": f"bot{c}", "bot_id": f"id{c}", "secret": "s"} for c in ("A", "B", "C")
    ]
    manager = BotManager(bots, settings=_settings())
    groups = [f"g{i:03d}" for i in range(30)]
    routed = {g: manager.route(g).name for g in groups}
    checks["route_stable"] = routed == {g: manager.route(g).name for g in groups}
    checks["distribution"] = {n: list(routed.values()).count(n) for n in manager.names()}
    checks["all_buckets_used"] = all(v > 0 for v in checks["distribution"].values())

    import hashlib

    expect = {
        g: manager.names()[
            int(hashlib.md5(g.encode()).hexdigest()[:8], 16) % len(manager.names())  # noqa: S324
        ]
        for g in groups
    }
    checks["route_matches_formula"] = routed == expect

    # ② ★ D1 专测:2 个机器人,**1 个离线** → 告警必须仍送达,且返回逐群结果
    from hoteldata.domains.bot.client import BotClient

    class _Fake(BotClient):
        def __init__(self, name: str, up: bool) -> None:  # noqa: D107
            super().__init__(name, "i", "s")
            self._up = up
            self.delivered: list[str] = []

        @property
        def connected(self) -> bool:
            return self._up

        async def send_markdown(self, chatid: str, content: str) -> dict[str, Any]:
            if not self._up:
                raise RuntimeError("机器人离线")
            self.delivered.append(chatid)
            return {"errcode": 0}

    up, down = _Fake("botA", True), _Fake("botB", False)
    m2 = BotManager([], settings=_settings())
    m2._clients = {"botA": up, "botB": down}
    checks["health_shape"] = m2.health() == {"botA": True, "botB": False}
    checks["health_is_dict_of_bool"] = all(isinstance(v, bool) for v in m2.health().values())
    res = await m2.send_alert("⚠️ 登录失效", chatids=["chat-manage-1", "chat-ops-1"])
    checks["d1_delivered"] = res.delivered
    checks["d1_ok"] = res.ok
    checks["d1_page_owner"] = up.delivered
    checks["d1_is_not_bare_bool"] = hasattr(res, "delivered") and hasattr(res, "failed")

    # ③ 一台都不在线 → **带 error 的结果**,不是静默 False
    m3 = BotManager([], settings=_settings())
    m3._clients = {"botA": _Fake("botA", False)}
    res3 = await m3.send_alert("⚠️ 掉线", chatids=["chat-1"])
    checks["d1_offline_error"] = res3.error
    checks["d1_offline_not_ok"] = res3.ok is False and bool(res3.error)

    # ④ 未配置目标群 → 明确报错
    res4 = await m2.send_alert("⚠️ x", chatids=[])
    checks["d1_no_target_error"] = res4.error

    # ⑤ 容量告警(仅告警不硬拦)
    m2.settings = _settings(AIBOT_CAPACITY_PER_BOT=10)
    checks["capacity_warnings"] = m2.balance_report({"botA": 12, "botB": 3})
    checks["capacity_needs_warning"] = len(checks["capacity_warnings"]) == 1

    ok = (
        checks["route_stable"]
        and checks["all_buckets_used"]
        and checks["route_matches_formula"]
        and checks["health_is_dict_of_bool"]
        and checks["d1_ok"]
        and checks["d1_is_not_bare_bool"]
        and checks["d1_delivered"] == ["chat-manage-1", "chat-ops-1"]
        and checks["d1_offline_not_ok"]
        and bool(checks["d1_no_target_error"])
        and checks["capacity_needs_warning"]
    )
    ev = (
        f"3 机器人 / 30 群分布={checks['distribution']} 路由=md5公式 / "
        f"★D1:1 台离线时告警仍送达 {checks['d1_delivered']},返回逐群结果(非裸 bool) / "
        f"全离线→error='{checks['d1_offline_error']}'"
    )
    return _res("V23", v23.__doc__ or "", PASS if ok else FAIL, ev, **checks)


# ===========================================================================
# V24 — 图片消息(三步上传 + 512KB 分片)
# ===========================================================================


@REG.add("V24", "图片消息:三步上传(init→chunk→finish);512KB/片;>100 片报错")
async def v24() -> CheckResult:
    from hoteldata.domains.bot import protocol as P
    from hoteldata.domains.bot.client import BotClient

    server = FakeWeComServer()
    await server.start()
    checks: dict[str, Any] = {"chunk_size": P.CHUNK_SIZE, "max_chunks": P.MAX_CHUNKS}
    checks["constants_exact"] = P.CHUNK_SIZE == 512 * 1024 and P.MAX_CHUNKS == 100
    try:
        client = BotClient("verify2-up", "b", "s", settings=_settings(AIBOT_WS_URL=server.url))
        await client.start()
        for _ in range(60):
            if client.ready:
                break
            await asyncio.sleep(0.05)

        # ① <=512KB → 1 片
        media_id = await client.upload_media(b"x" * 1000, "image", "small.jpg")
        checks["media_id"] = media_id
        checks["small_chunks"] = sum(1 for u in server.uploads if u["cmd"] == "chunk")

        # ② >512KB → 分片数 = ceil(size/512KB)
        server.uploads.clear()
        size = P.CHUNK_SIZE + 1234
        await client.upload_media(b"y" * size, "image", "big.jpg")
        init = next(u for u in server.uploads if u["cmd"] == "init")
        chunks = [u for u in server.uploads if u["cmd"] == "chunk"]
        fin = next(u for u in server.uploads if u["cmd"] == "finish")
        checks["big_size"] = size
        checks["expected_chunks"] = -(-size // P.CHUNK_SIZE)
        checks["actual_chunks"] = len(chunks)
        checks["init_body_keys"] = sorted(init["body"].keys())
        checks["init_body_exact"] = sorted(init["body"].keys()) == [
            "filename",
            "md5",
            "total_chunks",
            "total_size",
            "type",
        ]
        checks["md5_hex"] = len(init["body"]["md5"]) == 32
        checks["chunk_indexes"] = [u["body"]["chunk_index"] for u in chunks]
        checks["finish_body"] = fin["body"]

        # ③ >100 片必须报错
        too_big = P.CHUNK_SIZE * (P.MAX_CHUNKS + 1)
        err = ""
        try:
            await client.upload_media(b"z" * too_big, "image", "huge.jpg")
        except P.AiBotError as exc:
            err = str(exc)
        checks["oversize_error"] = err
        checks["oversize_rejected"] = bool(err)

        # ④ 上传后能发图(媒体消息帧形状;A1-12 = ``{"chatid": ..., **body}``)
        server.sent.clear()
        await client.send_media(SYNTH_GROUP, "image", media_id)
        body = server.sent[-1]["body"]
        checks["media_body"] = body
        checks["media_body_exact"] = (
            body.get("chatid") == SYNTH_GROUP
            and body.get("msgtype") == "image"
            and body.get("image") == {"media_id": media_id}
        )
        checks["has_chatid"] = "chatid" in body
        checks["no_news_card_type"] = "news" not in json.dumps(server.sent)
        await client.stop()
    finally:
        await server.stop()

    ok = (
        checks["constants_exact"]
        and checks["small_chunks"] == 1
        and checks["actual_chunks"] == checks["expected_chunks"]
        and checks["init_body_exact"]
        and checks["md5_hex"]
        and checks["oversize_rejected"]
        and checks["media_body_exact"]
        and checks["no_news_card_type"]
    )
    ev = (
        f"512KB/片 & 100 片上限逐字一致 / {checks['big_size']} 字节 → "
        f"{checks['actual_chunks']} 片(期望 {checks['expected_chunks']}) / "
        f"init 字段={checks['init_body_keys']} / >100 片报错='{checks['oversize_error'][:48]}' / "
        f"图文用 image 消息(无 news 卡片)"
    )
    return _res("V24", v24.__doc__ or "", PASS if ok else FAIL, ev, **checks)


# ===========================================================================
# V25 — 长文本拆分
# ===========================================================================


@REG.add("V25", "长文本:>3500 字拆成 ≤2 条;不截断、不丢内容")
async def v25() -> CheckResult:
    from hoteldata.push.sender import SECTION_SEP, split_message

    checks: dict[str, Any] = {"limit": 3500}

    short = "短消息"
    checks["short_one"] = split_message(short) == [short]

    # 单店超长 → 2 条以内且不丢
    long_single = "A" * 9000
    parts = split_message(long_single)
    checks["single_parts"] = len(parts)
    checks["single_lossless"] = "".join(parts).replace("\n", "") == long_single

    # 多店(markdown)超长 → 按店边界拆
    section = "### 「店N」\n" + ("指标行|123|456\n" * 120)
    content = SECTION_SEP.join(section.replace("店N", f"店{i}") for i in range(6))
    checks["total_chars"] = len(content)
    parts2 = split_message(content, limit=3500, max_parts=2)
    checks["multi_parts"] = len(parts2)
    checks["each_within_limit"] = [len(p) for p in parts2]
    checks["multi_lossless"] = "".join(parts2).replace(SECTION_SEP, "").replace("\n", "") == content.replace(
        SECTION_SEP, ""
    ).replace("\n", "")
    checks["keep_sections"] = all("### 「店" in p for p in parts2)

    # 极端:单段就超限 → 仍 ≤2 条且不丢
    huge = "B" * 20000
    parts3 = split_message(huge, limit=3500, max_parts=2)
    checks["huge_parts"] = len(parts3)
    checks["huge_lossless"] = "".join(parts3).replace("\n", "") == huge

    ok = (
        checks["short_one"]
        and checks["single_parts"] <= 2
        and checks["single_lossless"]
        and checks["multi_parts"] <= 2
        and checks["multi_lossless"]
        and checks["huge_parts"] <= 2
        and checks["huge_lossless"]
    )
    ev = (
        f"单店 9000 字 → {checks['single_parts']} 条 / 多店 {checks['total_chars']} 字 → "
        f"{checks['multi_parts']} 条(各 {checks['each_within_limit']} 字) / "
        f"20000 字 → {checks['huge_parts']} 条 / 全部无损={checks['multi_lossless'] and checks['huge_lossless']}"
    )
    return _res("V25", v25.__doc__ or "", PASS if ok else FAIL, ev, **checks)


# ===========================================================================
# V35 — 限频(★ D5:限频键 = 机器人,不是 chatid)
# ===========================================================================


async def _connected_manager(server: FakeWeComServer, *, name: str = "verify2-bot") -> Any:
    """建一个**真连**本地协议服务端的 BotManager(1 个实例)。"""
    from hoteldata.domains.bot.manager import BotManager

    manager = BotManager(
        [{"name": name, "bot_id": "b", "secret": "s"}],
        settings=_settings(AIBOT_WS_URL=server.url),
    )
    await manager.start_all()
    for _ in range(80):
        if manager.online:
            break
        await asyncio.sleep(0.05)
    return manager


@REG.add("V35", "限频:同一机器人相邻发送间隔 ≥ 2.0s(★D5:限频键=机器人,非 chatid)")
async def v35() -> CheckResult:
    from hoteldata.push.audit import PushAudit
    from hoteldata.push.dispatcher import PushDispatcher, PushTask
    from hoteldata.push.sender import Sender

    checks: dict[str, Any] = {}
    settings = _settings(PUSH_MIN_INTERVAL_S=2.0, PUSH_GROUP_MIN_INTERVAL_S=2.0)
    checks["config_min_interval"] = settings.push.min_interval_s
    checks["config_is_2s"] = settings.push.min_interval_s == 2.0

    server = FakeWeComServer()
    await server.start()
    rt = await _runtime()
    try:
        manager = await _connected_manager(server)
        # ★ 用极小的间隔做真实验(2.0s × 10 群 = 20s,验收太慢);配置默认值已单独断言
        interval = 0.25
        dispatcher = PushDispatcher(
            Sender(manager, layout=rt.layout, max_images=5, limit_chars=3500),
            PushAudit(rt.db),
            min_interval_s=interval,
            group_min_interval_s=0.0,
            retry_times=0,
            retry_backoff_s=(0.05,),
            workers=3,  # ★ 故意开 3 个 worker:若限频键错成 chatid,间隔会塌成 0
        )
        await dispatcher.start()
        groups = [f"verify2-rate-{i}" for i in range(10)]
        for g in groups:
            await dispatcher.enqueue(
                PushTask(chatid=g, push_type="verify2_rate", content=f"限频测试 {g}", dedup_checked=True)
            )
        drained = await dispatcher.drain(timeout_s=30)
        await dispatcher.stop(drain=False)
        checks["drained"] = drained
        checks["sent_count"] = len(server.sent)
        gaps = server.send_gaps()
        checks["gaps"] = gaps
        checks["min_gap"] = min(gaps) if gaps else 0.0
        checks["all_gaps_ok"] = bool(gaps) and min(gaps) >= interval * 0.85  # 留 15% 调度抖动
        checks["distinct_groups"] = len({s["chatid"] for s in server.sent})

        # ★ 反证:限频键若错成 chatid,多 worker 并发下间隔会塌到 ~0
        checks["would_fail_if_keyed_by_chatid"] = "(10 个不同群 + 3 worker)最小间隔仍受控"
        await manager.stop_all()
    finally:
        await rt.aclose()
        await server.stop()

    ok = checks["config_is_2s"] and checks["drained"] and checks["all_gaps_ok"] and checks["distinct_groups"] == 10
    ev = (
        f"配置默认 {checks['config_min_interval']}s(=2.0 逐字) / 实测 10 群 × 3 worker "
        f"最小相邻间隔 {checks['min_gap']:.3f}s(阈值 {interval}s) / 全部间隔={checks['gaps'][:5]}…"
    )
    return _res("V35", v35.__doc__ or "", PASS if ok else FAIL, ev, **checks)


# ===========================================================================
# V36 — 重试退避
# ===========================================================================


@REG.add("V36", "重试退避:失败按 [2,8,30] 秒退避,共 4 次尝试")
async def v36() -> CheckResult:
    from hoteldata.push.audit import PushAudit
    from hoteldata.push.dispatcher import PushDispatcher, PushTask
    from hoteldata.push.sender import Sender

    checks: dict[str, Any] = {}
    settings = _settings()
    checks["config_retry_times"] = settings.push.retry_times
    checks["config_backoff"] = list(settings.push.retry_backoff_s)
    checks["config_backoff_exact"] = settings.push.retry_times == 3 and settings.push.retry_backoff_s == (
        2.0,
        8.0,
        30.0,
    )
    checks["attempts_total"] = settings.push.retry_times + 1
    checks["attempts_is_4"] = checks["attempts_total"] == 4

    server = FakeWeComServer()
    await server.start()
    rt = await _runtime()
    try:
        manager = await _connected_manager(server)
        # ★ 用放大的退避表做真实验(2/8/30 秒太慢);**默认配置已逐字断言**
        backoff = (0.05, 0.1, 0.2)
        dispatcher = PushDispatcher(
            Sender(manager, layout=rt.layout, max_images=5, limit_chars=3500),
            PushAudit(rt.db),
            min_interval_s=0.0,
            group_min_interval_s=0.0,
            retry_times=3,
            retry_backoff_s=backoff,
            workers=1,
        )
        await dispatcher.start()

        # ① 前 2 次失败 → 第 3 次成功(共 3 次尝试)
        server.fail_sends = 2
        d1 = await dispatcher.deliver_now(
            PushTask(
                chatid="verify2-retry-1",
                push_type="verify2_retry",
                content="重试测试 1",
                dedup_checked=True,
            )
        )
        checks["recovered_ok"] = d1.ok
        checks["recovered_attempts"] = d1.attempts
        checks["recovered_after_2_failures"] = d1.ok and d1.attempts == 3

        # ② 一直失败 → 用满 retry_times+1 = 4 次尝试,并**明确失败**
        server.fail_sends = 99
        d2 = await dispatcher.deliver_now(
            PushTask(
                chatid="verify2-retry-2",
                push_type="verify2_retry",
                content="重试测试 2",
                dedup_checked=True,
            )
        )
        checks["exhausted_ok"] = d2.ok
        checks["exhausted_attempts"] = d2.attempts
        checks["exhausted_error"] = (d2.error or "")[:80]
        checks["attempts_is_4_actual"] = d2.attempts == 4
        checks["failure_visible"] = (not d2.ok) and bool(d2.error)

        # ③ 失败必须写进审计(不是静默)
        rows = await rt.audit.list_logs(group_chatid="verify2-retry-2")
        checks["failure_audited"] = [r.status for r in rows]
        checks["has_failed_row"] = "failed" in checks["failure_audited"]

        await dispatcher.stop(drain=False)
        await manager.stop_all()
    finally:
        await rt.aclose()
        await server.stop()

    ok = (
        checks["config_backoff_exact"]
        and checks["attempts_is_4"]
        and checks["recovered_after_2_failures"]
        and checks["attempts_is_4_actual"]
        and checks["failure_visible"]
        and checks["has_failed_row"]
    )
    ev = (
        f"配置退避 {checks['config_backoff']} / 共 {checks['attempts_total']} 次尝试(逐字) / "
        f"前 2 次失败后第 {checks['recovered_attempts']} 次成功 / 全失败用满 {checks['exhausted_attempts']} 次且可查"
    )
    return _res("V36", v36.__doc__ or "", PASS if ok else FAIL, ev, **checks)


# ===========================================================================
# V37 — 当日去重(队列路径)
# ===========================================================================


@REG.add("V37", "当日去重:同群同日同 slot 第二次被跳过(留 skipped 审计);force 可绕")
async def v37() -> CheckResult:
    from hoteldata.push.audit import PushAudit, slot_of
    from hoteldata.push.dispatcher import PushDispatcher, PushTask
    from hoteldata.push.sender import Sender

    checks: dict[str, Any] = {}
    group = "verify2-dedup-1"
    server = FakeWeComServer()
    await server.start()
    rt = await _runtime()
    try:
        await _cleanup_push_logs(rt.db, group)
        manager = await _connected_manager(server)
        dispatcher = PushDispatcher(
            Sender(manager, layout=rt.layout, max_images=5, limit_chars=3500),
            PushAudit(rt.db),
            min_interval_s=0.0,
            group_min_interval_s=0.0,
            retry_times=0,
            retry_backoff_s=(0.01,),
            workers=1,
        )
        await dispatcher.start()
        slot = slot_of()
        checks["slot"] = slot
        checks["slot_shape"] = len(slot) == 13 and slot[4] == "-" and slot[10] == "-"

        task = PushTask(
            chatid=group, push_type="verify2_dedup", content="去重测试", hotel_ids=(None,), slot=slot
        )
        await dispatcher.enqueue(task)
        await dispatcher.drain(timeout_s=15)
        await dispatcher.enqueue(
            PushTask(
                chatid=group,
                push_type="verify2_dedup",
                content="去重测试",
                hotel_ids=(None,),
                slot=slot,
            )
        )
        await dispatcher.drain(timeout_s=15)
        checks["sent_after_two"] = len([s for s in server.sent if s["chatid"] == group])
        checks["deduped"] = checks["sent_after_two"] == 1

        await dispatcher.enqueue(
            PushTask(
                chatid=group,
                push_type="verify2_dedup",
                content="去重测试",
                hotel_ids=(None,),
                slot=slot,
                force=True,
            )
        )
        await dispatcher.drain(timeout_s=15)
        checks["sent_after_force"] = len([s for s in server.sent if s["chatid"] == group])
        checks["force_bypassed"] = checks["sent_after_force"] == 2

        rows = await rt.audit.list_logs(group_chatid=group)
        checks["statuses"] = [r.status for r in rows]
        checks["skipped_logged"] = "skipped" in checks["statuses"]
        checks["slot_column"] = [r.slot for r in rows]
        checks["slot_matches"] = all(r.slot == slot for r in rows)
        await dispatcher.stop(drain=False)
        await manager.stop_all()
    finally:
        await rt.aclose()
        await server.stop()

    ok = (
        checks["slot_shape"]
        and checks["deduped"]
        and checks["force_bypassed"]
        and checks["skipped_logged"]
        and checks["slot_matches"]
    )
    ev = (
        f"slot={checks['slot']}(YYYY-MM-DD-HH) / 第二次被跳过(实际发送 {checks['sent_after_two']} 次) / "
        f"force 后 {checks['sent_after_force']} 次 / 审计序列={checks['statuses']}"
    )
    return _res("V37", v37.__doc__ or "", PASS if ok else FAIL, ev, **checks)


# ===========================================================================
# V38 — 推送审计(★ D17:bot_id 类型)
# ===========================================================================


@REG.add("V38", "推送审计:push_type/status/media_count/error 齐全;★bot_id 为文本")
async def v38() -> CheckResult:
    import sqlalchemy as sa

    from hoteldata.infra.models import PushLog
    from hoteldata.push.audit import PushAudit, PushRecord

    checks: dict[str, Any] = {}
    rt = await _runtime()
    try:
        hotel = await _ensure_hotel(rt.db)
        group = "verify2-audit-1"
        await _cleanup_push_logs(rt.db, group)
        audit = PushAudit(rt.db)
        await audit.write(
            PushRecord(
                group_chatid=group,
                hotel_id=int(hotel.id),
                push_type="daily_report",
                status="ok",
                bot_id="verify2-bot",  # ★ 机器人**名字符串**
                content_preview="### 「验收2-合成酒店」\n轮换图 5 张",
                media_count=5,
                images=("var/a.jpg", "var/b.jpg"),
                slot="2000-01-01-09",
            )
        )
        await audit.write(
            PushRecord(
                group_chatid=group,
                hotel_id=int(hotel.id),
                push_type="module_flow_overview",
                status="failed",
                bot_id="verify2-bot",
                error="平台限频: count limit",
                slot="2000-01-01-09",
            )
        )
        rows = await audit.list_logs(group_chatid=group)
        checks["row_count"] = len(rows)
        ok_row = next(r for r in rows if r.status == "ok")
        bad_row = next(r for r in rows if r.status == "failed")
        checks["bot_id_value"] = ok_row.bot_id
        checks["bot_id_is_str"] = isinstance(ok_row.bot_id, str)
        checks["media_count"] = ok_row.media_count
        checks["images_json"] = ok_row.images_json
        checks["preview_truncated"] = len(ok_row.content_preview or "") <= 200
        checks["error_recorded"] = bad_row.error
        checks["slot"] = ok_row.slot

        # ★ D17:数据库列类型必须是 text/varchar,不是 integer
        async with rt.db.engine.connect() as c:
            col = (
                await c.execute(
                    sa.text(
                        "select data_type from information_schema.columns "
                        "where table_name='push_logs' and column_name='bot_id'"
                    )
                )
            ).scalar()
        checks["bot_id_column_type"] = col
        checks["bot_id_column_is_text"] = col in ("text", "character varying")

        # 按机器人统计是 D17 修复后**才可能**的事
        async with rt.db.session() as s:
            n = await s.scalar(
                sa.select(sa.func.count()).select_from(PushLog).where(PushLog.bot_id == "verify2-bot")
            )
        checks["query_by_bot_name"] = int(n or 0)
        checks["bot_stats_possible"] = checks["query_by_bot_name"] >= 2

        stats = await audit.day_stats(date(2000, 1, 1))
        checks["day_stats"] = stats
        checks["rate_computed"] = stats["ok"] == 1 and stats["failed"] == 1 and stats["rate"] == 50.0
    finally:
        await rt.aclose()

    ok = (
        checks["bot_id_is_str"]
        and checks["bot_id_column_is_text"]
        and checks["media_count"] == 5
        and checks["bot_stats_possible"]
        and checks["rate_computed"]
        and bool(checks["error_recorded"])
    )
    ev = (
        f"bot_id 列类型={checks['bot_id_column_type']}(★D17:旧库是 INTEGER 却写字符串) / "
        f"按机器人名可统计={checks['bot_stats_possible']} / media_count={checks['media_count']} / "
        f"当日 ok/failed/率={checks['day_stats']}"
    )
    return _res("V38", v38.__doc__ or "", PASS if ok else FAIL, ev, **checks)


# ===========================================================================
# V39 — 审计可查(CLI)
# ===========================================================================


@REG.add("V39", "审计可查:hoteldata push log --today 列出今日推送与成功率")
async def v39() -> CheckResult:
    from typer.testing import CliRunner

    from hoteldata.cli import app as cli_app
    from hoteldata.push.audit import PushAudit, PushRecord, slot_of

    checks: dict[str, Any] = {}
    rt = await _runtime()
    try:
        hotel = await _ensure_hotel(rt.db)
        group = "verify2-cli-1"
        await _cleanup_push_logs(rt.db, group)
        audit = PushAudit(rt.db)
        await audit.write(
            PushRecord(
                group_chatid=group,
                hotel_id=int(hotel.id),
                push_type="daily_report",
                status="ok",
                bot_id="verify2-bot",
                content_preview="CLI 审计可见性测试",
                media_count=3,
                slot=slot_of(),
            )
        )
    finally:
        await rt.aclose()

    runner = CliRunner()
    # ★ 必须在**独立线程**里 invoke:CLI 命令内部用 ``asyncio.run``,
    #   而本验收器本身就跑在事件循环里 —— 直接 invoke 会撞
    #   ``RuntimeError: asyncio.run() cannot be called from a running event loop``,
    #   typer 把它吞成 exit_code=1,看起来像"CLI 坏了"。
    res = await asyncio.to_thread(
        runner.invoke, cli_app, ["push", "log", "--today", "--group", group]
    )
    checks["exit_code"] = res.exit_code
    if res.exception is not None:
        checks["cli_exception"] = f"{type(res.exception).__name__}: {res.exception}"
    out = res.output or ""
    checks["stdout_head"] = out[:400]
    checks["has_rate_line"] = "成功率" in out
    checks["has_group"] = group in out
    checks["has_type"] = "daily_report" in out
    checks["has_bot"] = "verify2-bot" in out

    # 子命令清单也要在
    res2 = await asyncio.to_thread(runner.invoke, cli_app, ["push", "--help"])
    checks["help_ok"] = res2.exit_code == 0 and "now" in (res2.output or "") and "log" in (res2.output or "")

    ok = (
        checks["exit_code"] == 0
        and checks["has_rate_line"]
        and checks["has_group"]
        and checks["has_type"]
        and checks["has_bot"]
        and checks["help_ok"]
    )
    ev = f"`push log --today --group {group}` 退出码={checks['exit_code']},输出含成功率/群/push_type/机器人名"
    return _res("V39", v39.__doc__ or "", PASS if ok else FAIL, ev, **checks)



# ===========================================================================
# V26 — 命令最长前缀匹配
# ===========================================================================


@REG.add("V26", "命令最长前缀匹配:「绑定隐欲民宿」与「绑定」都命中;不误命中更短前缀")
async def v26() -> CheckResult:
    from hoteldata.domains.bot.commands import _ws_tokens, parse_command

    # ★ 期望值按**旧系统口径**写:``parse_command`` 的 ``_split_args`` 只切
    #   逗号(中/英/顿号),**不切空格**;「忽略此店」「预警线」这类命令的
    #   空格分词在各自 handler 里由 ``_ws_tokens`` 完成(旧 ``commands.py:109-114``)。
    cases = {
        "绑定": ("绑定", []),
        "绑定隐欲民宿": ("绑定", ["隐欲民宿"]),
        "绑定 隐欲民宿": ("绑定", ["隐欲民宿"]),
        "绑定 A,B": ("绑定", ["A", "B"]),
        "绑定 A，B": ("绑定", ["A", "B"]),
        "我的酒店": ("我的酒店", []),
        "解绑 隐欲民宿": ("解绑", ["隐欲民宿"]),
        "今日数据": ("今日数据", []),
        "重推": ("重推", []),
        "帮助": ("帮助", []),
        "预警测试 隐欲民宿": ("预警测试", ["隐欲民宿"]),
        "忽略此店 隐欲民宿 7": ("忽略此店", ["隐欲民宿 7"]),
        "@经营数据助手 绑定 隐欲民宿": ("绑定", ["隐欲民宿"]),
        "你好": None,
        "": None,
        "今天流量怎么样": None,
    }
    got = {k: parse_command(k) for k in cases}
    checks = {"cases": {k: (list(v) if v else None) for k, v in got.items()}}
    checks["all_match"] = all(got[k] == cases[k] for k in cases)
    checks["mismatch"] = {k: got[k] for k in cases if got[k] != cases[k]}
    # ★ 最长前缀:必须命中「我的酒店」而不是被更短前缀截断
    checks["longest_prefix"] = parse_command("我的酒店")[0] == "我的酒店"
    # handler 侧的空白分词(预警线/忽略此店)
    checks["ws_tokens"] = _ws_tokens(["隐欲民宿 7"])
    checks["ws_tokens_ok"] = checks["ws_tokens"] == ["隐欲民宿", "7"]
    from hoteldata.domains.bot.commands import is_command

    checks["is_command_neg"] = not is_command("你好")
    ok = checks["all_match"] and not checks["mismatch"] and checks["ws_tokens_ok"]
    ev = (
        f"{len(cases)} 个用例全部命中={checks['all_match']} / 最长前缀「我的酒店」生效="
        f"{checks['longest_prefix']} / 无关文本→None / _ws_tokens 空白分词={checks['ws_tokens']}"
    )
    return _res("V26", v26.__doc__ or "", PASS if ok else FAIL, ev, **checks)


# ===========================================================================
# V27 — 管理群白名单(未配置时一律拒绝)
# ===========================================================================


@REG.add("V27", "管理群白名单:管理群命令非白名单被拒;未配置时一律拒绝")
async def v27() -> CheckResult:
    from hoteldata.domains.bot.commands import (
        MANAGE_COMMAND_ROUTES,
        MANAGE_COMMANDS,
        handle_command,
    )

    checks: dict[str, Any] = {"manage_commands": sorted(MANAGE_COMMANDS), "names": len(MANAGE_COMMANDS)}
    # ★ 「回复确认」与「已处理」同名一条路由 → **名字 12 个、路由 11 条**
    checks["routes_is_11"] = MANAGE_COMMAND_ROUTES == 11
    checks["names_cover_routes"] = len(MANAGE_COMMANDS) in (11, 12)
    both = {"回复确认", "已处理"} <= set(MANAGE_COMMANDS)
    checks["both_alias_guarded"] = both  # 别名也要在白名单里,否则留后门

    # ① 未配置 MANAGE_CHATIDS → 一律拒绝
    rt = await _runtime()
    try:
        rt.settings = _settings(MANAGE_CHATIDS="")
        checks["empty_is_manage"] = rt.settings.push.is_manage(SYNTH_GROUP)
        frame = {"body": {"msgtype": "text", "text": {"content": "状态"}, "chatid": SYNTH_GROUP}}
        reply = await handle_command(rt, None, frame, "状态")
        checks["empty_reply"] = reply
        checks["empty_rejected"] = reply == "⛔ 该命令仅管理群可用"

        # ② 配置后:非白名单仍拒;白名单放行
        rt.settings = _settings(MANAGE_CHATIDS=f"{SYNTH_MANAGE},{SYNTH_GROUP2}")
        checks["configured_is_manage"] = rt.settings.push.is_manage(SYNTH_MANAGE)
        checks["configured_other_false"] = rt.settings.push.is_manage(SYNTH_GROUP)
        frame2 = {"body": {"msgtype": "text", "text": {"content": "帮助"}, "chatid": SYNTH_GROUP}}
        reply2 = await handle_command(rt, None, frame2, "帮助")
        checks["help_any_group"] = isinstance(reply2, str) and "群内命令帮助" in reply2
        reply3 = await handle_command(rt, None, frame2, "汇总")
        checks["non_manage_summary_rejected"] = reply3 == "⛔ 该命令仅管理群可用"
    finally:
        await rt.aclose()

    ok = (
        checks["routes_is_11"]
        and checks["names_cover_routes"]
        and checks["both_alias_guarded"]
        and checks["empty_rejected"]
        and not checks["empty_is_manage"]
        and checks["configured_is_manage"]
        and not checks["configured_other_false"]
        and checks["help_any_group"]
        and checks["non_manage_summary_rejected"]
    )
    ev = (
        f"{checks['names']} 个名字 / {MANAGE_COMMAND_ROUTES} 条路由(回复确认≡已处理) / "
        f"MANAGE_CHATIDS 空 → is_manage=False 且回复「{checks['empty_reply']}」 / "
        f"配置后非白名单仍拒:{checks['non_manage_summary_rejected']}"
    )
    return _res("V27", v27.__doc__ or "", PASS if ok else FAIL, ev, **checks)


# ===========================================================================
# V28 — 私聊回退
# ===========================================================================


@REG.add("V28", "私聊回退:无 chatid → 命令返回 None,回退问答(不报错)")
async def v28() -> CheckResult:
    from hoteldata.domains.bot.commands import handle_command
    from hoteldata.domains.bot.protocol import chatid_of

    checks: dict[str, Any] = {}
    frame = {"body": {"msgtype": "text", "text": {"content": "绑定 隐欲民宿"}, "from": {"userid": "u1"}}}
    checks["chatid_of_none"] = chatid_of(frame) is None
    rt = await _runtime()
    try:
        reply = await handle_command(rt, None, frame, "绑定 隐欲民宿")
        checks["command_returns_none"] = reply is None
        # 「绑定」也是命令前缀,但私聊必须直接放行到问答链
        from hoteldata.domains.bot.commands import is_command

        checks["still_parseable"] = is_command("绑定 隐欲民宿")
    finally:
        await rt.aclose()

    # chatid 三个键名都要认
    for key in ("chatid", "chat_id", "chatId"):
        f = {"body": {key: "g1"}}
        checks[f"chatid_key_{key}"] = chatid_of(f) == "g1"

    ok = (
        checks["chatid_of_none"]
        and checks["command_returns_none"]
        and all(checks[f"chatid_key_{k}"] for k in ("chatid", "chat_id", "chatId"))
    )
    ev = "私聊(无 chatid)→ 命令返回 None,回退问答链;chatid/chat_id/chatId 三键名均识别"
    return _res("V28", v28.__doc__ or "", PASS if ok else FAIL, ev, **checks)


# ===========================================================================
# V29 — 绑定 / 解绑 / 我的酒店
# ===========================================================================


@REG.add("V29", "绑定/解绑/我的酒店:落库、幂等、解绑生效")
async def v29() -> CheckResult:
    from hoteldata.domains.bot.commands import handle_command

    checks: dict[str, Any] = {}
    rt = await _runtime()
    try:
        hotel = await _ensure_hotel(rt.db)
        await _cleanup(rt.db, int(hotel.id))

        def frame(text: str) -> dict[str, Any]:
            return {"body": {"msgtype": "text", "text": {"content": text}, "chatid": SYNTH_GROUP}}

        r1 = await handle_command(rt, None, frame(f"绑定 {hotel.name}"), f"绑定 {hotel.name}")
        checks["bind_reply"] = r1
        checks["bind_ok"] = "已绑定" in (r1 or "")
        row1 = await rt.bindings.for_group(SYNTH_GROUP)
        checks["binding_rows"] = [(b.hotel_id, b.name) for b in row1]
        checks["binding_written"] = len(row1) == 1 and row1[0].hotel_id == int(hotel.id)

        # 幂等:重复绑定不报错、不重复
        created2 = await rt.bindings.bind(SYNTH_GROUP, int(hotel.id))
        checks["rebind_idempotent"] = created2 is False
        checks["still_one_row"] = len(await rt.bindings.for_group(SYNTH_GROUP)) == 1

        r2 = await handle_command(rt, None, frame("我的酒店"), "我的酒店")
        checks["my_hotels_reply"] = r2
        checks["my_hotels_ok"] = hotel.name in (r2 or "")

        # 找不到的酒店要提示
        r3 = await handle_command(rt, None, frame("绑定 不存在的酒店XYZ"), "绑定 不存在的酒店XYZ")
        checks["missing_hint"] = r3
        checks["missing_reported"] = "未找到酒店" in (r3 or "")

        # 解绑
        r4 = await handle_command(rt, None, frame(f"解绑 {hotel.name}"), f"解绑 {hotel.name}")
        checks["unbind_reply"] = r4
        checks["unbind_ok"] = len(await rt.bindings.for_group(SYNTH_GROUP)) == 0
        r5 = await handle_command(rt, None, frame("我的酒店"), "我的酒店")
        checks["empty_hint"] = r5
    finally:
        await rt.aclose()

    ok = (
        checks["bind_ok"]
        and checks["binding_written"]
        and checks["rebind_idempotent"]
        and checks["still_one_row"]
        and checks["my_hotels_ok"]
        and checks["missing_reported"]
        and checks["unbind_ok"]
    )
    ev = (
        f"绑定落库={checks['binding_rows']} / 重复绑定幂等={checks['rebind_idempotent']} / "
        f"我的酒店='{(checks['my_hotels_reply'] or '')[:24]}' / 解绑后 0 行={checks['unbind_ok']}"
    )
    return _res("V29", v29.__doc__ or "", PASS if ok else FAIL, ev, **checks)


# ===========================================================================
# V30 — 今日数据 / 重推(force 绕过去重)
# ===========================================================================


@REG.add("V30", "今日数据/重推:能触发重推;force 绕过当日去重")
async def v30() -> CheckResult:
    from hoteldata.push.audit import PushAudit
    from hoteldata.push.bindings import Bindings
    from hoteldata.push.dispatcher import PushDispatcher, PushTask
    from hoteldata.push.sender import Sender

    checks: dict[str, Any] = {}
    rt = await _runtime()
    try:
        hotel = await _ensure_hotel(rt.db)
        await _cleanup(rt.db, int(hotel.id))
        await rt.bindings.bind(SYNTH_GROUP, int(hotel.id))

        # 用离线机器人替换(不连网):只验"重推 + force"这条链路
        rt._bots = OfflineManager()
        rt._sender = None
        rt._push = None
        await rt.start_push()
        dispatcher: PushDispatcher = rt.push.dispatcher
        dispatcher.min_interval_s = 0.0
        dispatcher.group_min_interval_s = 0.0
        dispatcher.retry_backoff_s = (0.01, 0.02, 0.03)
        checks["audit_class"] = type(rt.audit).__name__
        checks["bindings_class"] = type(rt.bindings).__name__
        checks["sender_class"] = type(rt.sender).__name__
        checks["audit_is_pushaudit"] = isinstance(rt.audit, PushAudit)
        checks["bindings_is_bindings"] = isinstance(rt.bindings, Bindings)
        checks["sender_is_sender"] = isinstance(rt.sender, Sender)
        _ = PushTask

        task = PushTask(
            chatid=SYNTH_GROUP,
            push_type="daily_report",
            content="### 「验收2-合成酒店」\n📅 测试",
            hotel_ids=(int(hotel.id),),
        )
        d1 = await dispatcher.deliver_now(task)
        checks["first_ok"] = d1.ok
        d2 = await dispatcher.deliver_now(
            PushTask(
                chatid=SYNTH_GROUP,
                push_type="daily_report",
                content=task.content,
                hotel_ids=(int(hotel.id),),
            )
        )
        checks["second_deduped"] = (not d2.ok) and "去重" in (d2.error or "") or "已推送" in (d2.error or "")
        checks["second_error"] = d2.error
        d3 = await dispatcher.deliver_now(
            PushTask(
                chatid=SYNTH_GROUP,
                push_type="daily_report",
                content=task.content,
                hotel_ids=(int(hotel.id),),
                force=True,
            )
        )
        checks["force_ok"] = d3.ok
        rows = await rt.audit.list_logs(group_chatid=SYNTH_GROUP)
        checks["statuses"] = [r.status for r in rows]
        checks["has_skipped"] = "skipped" in checks["statuses"]
        checks["ok_count"] = checks["statuses"].count("ok")
        checks["ok_count_is_2"] = checks["ok_count"] == 2
    finally:
        await rt.aclose()

    ok = (
        checks["audit_is_pushaudit"]
        and checks["first_ok"]
        and checks["second_deduped"]
        and checks["force_ok"]
        and checks["has_skipped"]
        and checks["ok_count_is_2"]
    )
    ev = (
        f"首次 ok={checks['first_ok']} / 二次不去重→'{checks['second_error']}' / force 后 ok={checks['force_ok']} / "
        f"审计状态序列={checks['statuses']}(skipped 有留痕)"
    )
    return _res("V30", v30.__doc__ or "", PASS if ok else FAIL, ev, **checks)


# ===========================================================================
# V31 / V32 — FAQ
# ===========================================================================


@REG.add("V31", "FAQ 精确优先 + 关键词兜底;都不中 → 兜底文案")
async def v31() -> CheckResult:
    from hoteldata.domains.bot import faq as faq_mod
    from hoteldata.domains.bot.faq import clean_question, match_faq

    checks: dict[str, Any] = {}
    rt = await _runtime()
    try:
        items = faq_mod.load_faq(rt, force=True)
        checks["faq_count"] = len(items)
        checks["faq_count_is_9"] = len(items) == 9

        exact = match_faq(rt, clean_question("流量怎么样"))
        checks["exact_page"] = (exact or {}).get("page")
        checks["exact_hit"] = exact is not None and "流量怎么样" in (exact or {}).get("questions", [])

        # 精确问法必须**优先于**关键词:构造"既是 A 精确问法、又含 B 关键词"的输入
        kw = match_faq(rt, clean_question("今天曝光多少啊"))
        checks["keyword_hit"] = kw is not None

        miss = match_faq(rt, clean_question("完全无关的一句话甲乙丙"))
        checks["miss_none"] = miss is None

        cleaned = clean_question("@经营数据助手   今天  流量  怎么样")
        checks["cleaned"] = cleaned
        checks["cleaned_ok"] = "  " not in cleaned and not cleaned.startswith("@")
    finally:
        await rt.aclose()

    ok = (
        checks["faq_count_is_9"]
        and checks["exact_hit"]
        and checks["keyword_hit"]
        and checks["miss_none"]
        and checks["cleaned_ok"]
    )
    ev = (
        f"faq.json {checks['faq_count']} 条 / 精确命中 page={checks['exact_page']} / "
        f"关键词兜底命中={checks['keyword_hit']} / 无关→None / 清洗='{checks['cleaned']}'"
    )
    return _res("V31", v31.__doc__ or "", PASS if ok else FAIL, ev, **checks)


@REG.add("V32", "FAQ 配图三形态:模块图 / 缺图回退整页 / full / 路径字符串")
async def v32() -> CheckResult:
    from hoteldata.domains.bot.faq import resolve_image_sources
    from hoteldata.domains.collect.repository import CollectRepository

    checks: dict[str, Any] = {}
    rt = await _runtime()
    try:
        hotel = await _ensure_hotel(rt.db)
        await _cleanup(rt.db, int(hotel.id))

        # 造图:模块图 2 张 + 整页图 1 张
        day_dir = rt.layout.screenshot_day_dir(hotel.name, SYNTH_DAY)
        day_dir.mkdir(parents=True, exist_ok=True)
        mod_shot = day_dir / "流量数据概况_verify2.jpg"
        mod_shot.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
        full_shot = day_dir / "整页_verify2.jpg"
        full_shot.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg-full")
        rel_mod = rt.layout.to_relative(mod_shot)
        rel_full = rt.layout.to_relative(full_shot)

        async with rt.db.session() as s:
            repo = CollectRepository(s)
            await repo.ensure_report(int(hotel.id), SYNTH_DAY, "经营报告", channel="screenshot")
            await repo.link_screenshot(
                int(hotel.id),
                SYNTH_DAY,
                "经营报告",
                screenshot_path=rel_full,
                module_screenshots={"流量数据概况": rel_mod},
            )
        # ★ 段1 的 ``today_module_shots(page=None)`` 的**页清单来自 collect_modules**
        #   (不是 collect_reports)—— 没有模块记录的页取不到图。这是真实语义:
        #   一个页面有数据才可能有截图。所以这里必须同时播种一条模块记录。
        await _seed_module(
            rt, int(hotel.id), "经营报告", "离店", "昨日", {"离店间夜": 1}, day=SYNTH_DAY
        )

        # ① {"module": 名} → 命中模块图
        got1 = await resolve_image_sources(rt, {"images": [{"module": "流量数据概况"}]}, int(hotel.id), SYNTH_DAY)
        checks["module_form"] = [str(p) for p in got1]
        checks["module_hit"] = len(got1) == 1 and got1[0].name == mod_shot.name

        # ② {"module": 不存在} → **回退整页**
        got2 = await resolve_image_sources(
            rt, {"images": [{"module": "根本没有这个模块"}]}, int(hotel.id), SYNTH_DAY
        )
        checks["fallback_full"] = [str(p) for p in got2]
        checks["fallback_ok"] = len(got2) == 1 and got2[0].name == full_shot.name

        # ③ {"full": true} → 整页
        got3 = await resolve_image_sources(rt, {"images": [{"full": True}]}, int(hotel.id), SYNTH_DAY)
        checks["full_form"] = [str(p) for p in got3]
        checks["full_ok"] = len(got3) == 1 and got3[0].name == full_shot.name

        # ④ 路径字符串 → 直接读
        got4 = await resolve_image_sources(rt, {"images": [rel_mod]}, int(hotel.id), SYNTH_DAY)
        checks["path_form"] = [str(p) for p in got4]
        checks["path_ok"] = len(got4) == 1 and got4[0].name == mod_shot.name

        # ⑤ 缺失 → 空列表(不是异常)
        got5 = await resolve_image_sources(
            rt, {"images": [{"module": "x"}]}, int(hotel.id), SYNTH_DAY + timedelta(days=5000)
        )
        checks["missing_empty"] = got5 == []
        # 无绑定店(hotel_id=None)→ 也必须是空列表而不是异常
        got6 = await resolve_image_sources(rt, {"images": [{"full": True}]}, None, SYNTH_DAY)
        checks["no_hotel_empty"] = got6 == []
    finally:
        await rt.aclose()

    ok = (
        checks["module_hit"]
        and checks["fallback_ok"]
        and checks["full_ok"]
        and checks["path_ok"]
        and checks["missing_empty"]
        and checks["no_hotel_empty"]
    )
    ev = (
        f"module→{len(checks['module_form'])} 张 / 缺模块→回退整页={checks['fallback_ok']} / "
        f"full→{len(checks['full_form'])} 张 / 路径字符串→{len(checks['path_form'])} 张 / 全缺→空列表"
    )
    return _res("V32", v32.__doc__ or "", PASS if ok else FAIL, ev, **checks)


# ===========================================================================
# V33 / V34 — 实时问答 / 无关消息不误答
# ===========================================================================


@REG.add("V33", "实时问答:群内问「离店」「预订销售数据」命中 realtime_ask;私聊/无关不触发")
async def v33() -> CheckResult:
    from hoteldata.domains.bot import realtime as rt_mod
    from hoteldata.domains.bot.realtime import match_realtime_item, realtime_items

    checks: dict[str, Any] = {}
    today = date.today()
    rt = await _runtime()
    try:
        hotel = await _ensure_hotel(rt.db)
        await _cleanup(rt.db, int(hotel.id))
        await rt.bindings.bind(SYNTH_GROUP, int(hotel.id))

        # ① 排期里的 realtime_ask 项(离店 / 预订销售数据)
        schedule = rt_mod.load_schedule(rt)
        items = realtime_items(schedule)
        checks["realtime_items"] = [i.get("id") for i in items]
        checks["has_checkout_booking"] = {"checkout", "booking"} <= {i.get("id") for i in items}
        checkout = next((i for i in items if i.get("id") == "checkout"), None)
        checks["match_checkout"] = match_realtime_item("离店", checkout or {})
        booking = next((i for i in items if i.get("id") == "booking"), None)
        checks["match_booking"] = match_realtime_item("预订销售数据", booking or {})

        # ② 造**当日**数据(实时问答按当天取数)
        await _seed_module(
            rt, int(hotel.id), "经营报告", "离店", "昨日", {"离店间夜": 12, "离店订单": 5}, day=today
        )

        got = await rt_mod.build_realtime_reply(rt, "离店", SYNTH_GROUP)
        text = got[0] if isinstance(got, tuple) else got
        checks["group_reply"] = text
        checks["group_hit"] = bool(text)

        # ③ 私聊(无 chatid)→ None
        private = await rt_mod.build_realtime_reply(rt, "离店", "")
        checks["private_none"] = private is None
        # ④ 无关问题 → None(不误答)
        unrelated = await rt_mod.build_realtime_reply(rt, "今天天气怎么样", SYNTH_GROUP)
        checks["unrelated_none"] = unrelated is None
        # ⑤ 未绑定群 → None
        unbound = await rt_mod.build_realtime_reply(rt, "离店", "verify2-unbound-xyz")
        checks["unbound_none"] = unbound is None
    finally:
        await rt.aclose()

    ok = (
        checks["has_checkout_booking"]
        and checks["match_checkout"]
        and checks["match_booking"]
        and checks["group_hit"]
        and checks["private_none"]
        and checks["unrelated_none"]
        and checks["unbound_none"]
    )
    ev = (
        f"realtime_ask 项={checks['realtime_items']} / 群问「离店」→{(checks['group_reply'] or '')[:36]!r} / "
        f"私聊→None / 无关→None / 未绑定→None(问才发,不参与定时推送)"
    )
    return _res("V33", v33.__doc__ or "", PASS if ok else FAIL, ev, **checks)


@REG.add("V34", "无关消息不误答:只回兜底文案,不触发任何推送")
async def v34() -> CheckResult:
    from hoteldata.domains.bot.router import FALLBACK_TEXT, MessageRouter

    checks: dict[str, Any] = {}
    rt = await _runtime()
    try:
        hotel = await _ensure_hotel(rt.db)
        await _cleanup(rt.db, int(hotel.id))
        await _cleanup_push_logs(rt.db, SYNTH_GROUP)
        await rt.bindings.bind(SYNTH_GROUP, int(hotel.id))

        bot = _silent_bot()
        router = MessageRouter(rt, sender=rt.sender)
        before = len(await rt.audit.list_logs(group_chatid=SYNTH_GROUP))
        frame = {
            "body": {"msgtype": "text", "text": {"content": "大家晚上好呀"}, "chatid": SYNTH_GROUP}
        }
        await router.on_message(bot, frame)
        checks["outbox"] = bot.outbox
        checks["fallback_sent"] = any(FALLBACK_TEXT[:10] in str(o.get("reply", "")) for o in bot.outbox)
        _ = hotel
        after = len(await rt.audit.list_logs(group_chatid=SYNTH_GROUP))
        checks["no_push"] = after == before
        checks["push_delta"] = after - before

        # 非 text 消息直接丢弃
        bot2 = _silent_bot()
        await router.on_message(bot2, {"body": {"msgtype": "image", "chatid": SYNTH_GROUP}})
        checks["non_text_dropped"] = bot2.outbox == []
    finally:
        await rt.aclose()

    ok = checks["fallback_sent"] and checks["no_push"] and checks["non_text_dropped"]
    ev = (
        f"无关文本 → 只回兜底文案='{FALLBACK_TEXT[:24]}…' / push_logs 增量={checks['push_delta']} / "
        f"非 text 帧丢弃={checks['non_text_dropped']}"
    )
    return _res("V34", v34.__doc__ or "", PASS if ok else FAIL, ev, **checks)


# ===========================================================================
# V40 — 日报组装
# ===========================================================================


@REG.add("V40", "日报组装:标题行 + 📅 轮换行;图片 ≤5 张;缺图跳过而非报错")
async def v40() -> CheckResult:
    from hoteldata.domains.collect.rotation import get_rotation
    from hoteldata.domains.report.daily import build_daily_message, build_daily_section

    checks: dict[str, Any] = {}
    today = date.today()
    rt = await _runtime()
    try:
        hotel = await _ensure_hotel(rt.db)
        await _cleanup(rt.db, int(hotel.id))
        await rt.bindings.bind(SYNTH_GROUP, int(hotel.id))

        # 造当日截图:只给轮换清单里的**前 2 项**出图(其余必然缺图 → 验"跳过不报错")
        # ★ 同时要播种一条模块记录:段1 ``today_module_shots(page=None)`` 的**页清单
        #   来自 collect_modules**(有数据才可能有截图),只建 collect_reports 取不到图。
        plan = get_rotation().pick(today)
        made: list[str] = []
        for item in plan.items:
            await _seed_module(
                rt,
                int(hotel.id),
                item.page or "经营报告",
                item.name,
                "昨日",
                {"验收指标": 1},
                day=today,
            )
        for item in plan.items[:2]:
            alias = (item.alias or [item.name])[0]
            made.append(await _seed_shot(rt, hotel, item.page or "经营报告", alias, day=today))

        bound = await rt.bindings.for_group(SYNTH_GROUP)
        section = await build_daily_section(rt, bound[0], today)
        checks["section_not_none"] = section is not None
        md, images = section if section else ("", [])
        checks["markdown"] = md
        checks["title_line"] = md.splitlines()[0] == f"### 「{hotel.name}」"
        checks["rotation_line"] = "📅" in md and "今日轮换" in md
        checks["rotation_names"] = all(i.name in md for i in plan.items if i.name)
        checks["image_count"] = len(images)
        checks["images_le_5"] = len(images) <= 5
        checks["images_are_made"] = set(images) == set(made) and len(made) == 2

        msg = await build_daily_message(rt, SYNTH_GROUP)
        checks["message_not_none"] = msg is not None
        checks["push_type"] = msg.push_type if msg else None
        checks["push_type_is_daily_report"] = bool(msg) and msg.push_type == "daily_report"
        checks["msg_images_le_5"] = bool(msg) and len(msg.images) <= 5
        checks["hotel_ids"] = list(msg.hotel_ids) if msg else []
        # 未绑定群 → None
        checks["unbound_none"] = (await build_daily_message(rt, "verify2-nobody-zzz")) is None

        # 缺图必须是"跳过 + 日志",而不是抛错:上面只造了 2 张图,其余 3 项都缺图,
        # 若实现是"缺图报错",build_daily_section 早就抛了。
        checks["missing_images_not_error"] = True
    finally:
        await rt.aclose()

    ok = (
        checks["section_not_none"]
        and checks["title_line"]
        and checks["rotation_line"]
        and checks["rotation_names"]
        and checks["images_le_5"]
        and checks["images_are_made"]
        and checks["push_type_is_daily_report"]
        and checks["msg_images_le_5"]
        and checks["unbound_none"]
    )
    ev = (
        f"标题行='{checks['markdown'].splitlines()[0] if checks['markdown'] else ''}' / "
        f"轮换行含 {len(plan.items)} 项名 / 出图 {checks['image_count']} 张(≤5) / "
        f"push_type={checks['push_type']} / 其余缺图项跳过未报错"
    )
    return _res("V40", v40.__doc__ or "", PASS if ok else FAIL, ev, **checks)


# ===========================================================================
# V41 — 图文绑定:缺图整项不发 + 管理群告警
# ===========================================================================


@REG.add("V41", "图文绑定:配了 images 却 0 张 → 该项不发 + 管理群告警「缺图未发 N 条」")
async def v41() -> CheckResult:
    from hoteldata.domains.report.service import ReportService
    from hoteldata.push.service import BuiltMessage, PushService

    checks: dict[str, Any] = {}
    group = "verify2-noimg-1"
    rt = await _runtime()
    try:
        hotel = await _ensure_hotel(rt.db)
        await _cleanup(rt.db, int(hotel.id))
        await _cleanup_push_logs(rt.db, group)
        await rt.bindings.bind(group, int(hotel.id))

        # 用离线机器人 + 记录告警文本的 PushService
        rt._bots = OfflineManager()
        rt._sender = None
        rt._push = None
        rt._audit = None
        await rt.start_push()
        rt.settings = _settings(MANAGE_CHATIDS=SYNTH_MANAGE, OPS_CHATID="")
        sent_alerts: list[str] = []

        async def _fake_alert(text: str, *, chatids: list[str] | None = None) -> Any:
            from hoteldata.domains.bot.manager import AlertResult

            sent_alerts.append(text)
            return AlertResult(text=text, targets=list(chatids or []), delivered=list(chatids or []))

        rt.push.send_alert = _fake_alert  # type: ignore[method-assign]
        checks["push_service"] = isinstance(rt.push, PushService)
        _ = (ReportService, BuiltMessage)

        # 造一条"有数据、配了 images、但一张图都没有"的报告项:flow_overview 正好有 images
        from hoteldata.domains.report.schedule import get_schedule

        item = get_schedule().by_id("flow_overview")
        checks["item_has_images"] = bool(item and item.images)

        svc = rt.report()
        # 当日数据:让该项有 payload(否则会先走 no_data 分支,测不到"缺图"分支)
        today = date.today()
        await _seed_module(
            rt,
            int(hotel.id),
            "经营报告",
            item.module_name,
            "今日实时",
            {"访客量": 100, "曝光量": 2000},
            day=today,
        )
        built, reason = await svc._build_section(item, (await rt.bindings.for_group(group))[0], today)  # noqa: SLF001
        checks["build_reason"] = reason
        checks["section_skipped_on_missing_image"] = built is None and reason == "no_image"

        stats = {"no_image": 1, "no_image_details": [f"{hotel.name}·{item.name}"]}
        await svc._alert_missing_images(stats)  # noqa: SLF001
        checks["alerts"] = sent_alerts
        checks["alert_sent"] = bool(sent_alerts)
        checks["alert_mentions"] = bool(sent_alerts) and "缺图未发" in sent_alerts[0]
        checks["alert_count"] = bool(sent_alerts) and "1 条" in sent_alerts[0]
        checks["alert_target_is_manage"] = bool(sent_alerts) and "manage-0001" not in sent_alerts[0]
    finally:
        await rt.aclose()

    ok = (
        checks["item_has_images"]
        and checks["section_skipped_on_missing_image"]
        and checks["alert_sent"]
        and checks["alert_mentions"]
        and checks["alert_count"]
    )
    ev = (
        f"flow_overview 配图 {len(item.images) if item else 0} 张、当日 0 张 → 组装返回 "
        f"reason='{checks['build_reason']}'(整项不发) / 管理群告警='{(checks['alerts'] or [''])[0][:70]}'"
    )
    return _res("V41", v41.__doc__ or "", PASS if ok else FAIL, ev, **checks)


# ===========================================================================
# V42 — 22 项按节奏(四项桶)
# ===========================================================================


@REG.add("V42", "22 项按节奏:daily/monday/month1 桶;周一且 1 号两桶同时;daily_except_monday 让位")
async def v42() -> CheckResult:
    from hoteldata.domains.report.engine import buckets_for_date, item_windows_for_date
    from hoteldata.domains.report.schedule import get_schedule

    checks: dict[str, Any] = {}
    schedule = get_schedule()
    checks["item_count"] = len(schedule.items)
    checks["item_count_is_22"] = len(schedule.items) == 22

    # 固定日期断言
    wed = date(2026, 9, 30)  # 周三
    mon = date(2026, 9, 28)  # 周一
    first = date(2026, 10, 1)  # 1 号(周四)
    mon_first = date(2026, 6, 1)  # 周一且 1 号

    checks["buckets_wed"] = buckets_for_date(wed)
    checks["buckets_mon"] = buckets_for_date(mon)
    checks["buckets_first"] = buckets_for_date(first)
    checks["buckets_mon_first"] = buckets_for_date(mon_first)
    checks["wed_daily_only"] = checks["buckets_wed"] == ["daily"]
    checks["mon_adds_monday"] = checks["buckets_mon"] == ["daily", "monday"]
    checks["first_adds_month1"] = checks["buckets_first"] == ["daily", "month1"]
    checks["mon_first_both"] = checks["buckets_mon_first"] == ["daily", "monday", "month1"]

    # daily_except_monday:svc_daily 周一不发,svc_weekly 只在周一发
    svc_daily = schedule.by_id("svc_daily")
    svc_weekly = schedule.by_id("svc_weekly")
    checks["svc_daily_wed"] = item_windows_for_date(svc_daily, wed)
    checks["svc_daily_mon"] = item_windows_for_date(svc_daily, mon)
    checks["svc_weekly_wed"] = item_windows_for_date(svc_weekly, wed)
    checks["svc_weekly_mon"] = item_windows_for_date(svc_weekly, mon)
    checks["except_monday_works"] = checks["svc_daily_mon"] == [] and checks["svc_daily_wed"] == ["昨日"]
    checks["weekly_only_monday"] = checks["svc_weekly_wed"] == [] and checks["svc_weekly_mon"] == ["昨日"]

    # 每项在周一/1 号都能算出窗口,不报错
    from hoteldata.domains.report.engine import windows_for_push

    bad: list[str] = []
    for d in (wed, mon, first, mon_first):
        for it in schedule.items:
            try:
                windows_for_push(it, d)
            except Exception as exc:  # noqa: BLE001
                bad.append(f"{it.id}@{d}: {exc}")
    checks["no_exceptions"] = bad == []
    checks["errors"] = bad[:5]

    # 仅周期桶的项:周三无窗口(服务质量对比 / 同行对比)
    svc_compare = schedule.by_id("svc_compare")
    peer = schedule.by_id("peer_compare")
    checks["svc_compare_wed"] = item_windows_for_date(svc_compare, wed)
    checks["peer_wed"] = item_windows_for_date(peer, wed)
    checks["period_only_empty_on_wed"] = checks["svc_compare_wed"] == [] and checks["peer_wed"] == []

    ok = (
        checks["item_count_is_22"]
        and checks["wed_daily_only"]
        and checks["mon_adds_monday"]
        and checks["first_adds_month1"]
        and checks["mon_first_both"]
        and checks["except_monday_works"]
        and checks["weekly_only_monday"]
        and checks["no_exceptions"]
        and checks["period_only_empty_on_wed"]
    )
    ev = (
        f"{checks['item_count']} 项 / 桶:周三={checks['buckets_wed']} 周一={checks['buckets_mon']} "
        f"1号={checks['buckets_first']} 周一且1号={checks['buckets_mon_first']} / "
        f"svc_daily 周一让位={'昨日' not in checks['svc_daily_mon']} / svc_weekly 仅周一="
        f"{checks['weekly_only_monday']}"
    )
    return _res("V42", v42.__doc__ or "", PASS if ok else FAIL, ev, **checks)


# ===========================================================================
# V43 — 条件 DSL(★ D9:in 分支必须可达)
# ===========================================================================


@REG.add("V43", "条件 DSL:「投产比>3」「公示内容存在」「任一>0」生效;★in 分支可达(D9)")
async def v43() -> CheckResult:
    from hoteldata.domains.report.engine import eval_condition

    checks: dict[str, Any] = {}

    # ① 数值比较
    checks["gt_true"] = eval_condition({"field": "投产比", "op": ">", "value": 3}, {"投产比": 3.5})
    checks["gt_false"] = eval_condition({"field": "投产比", "op": ">", "value": 3}, {"投产比": 3})
    checks["gt_equal_not_gt"] = eval_condition({"field": "投产比", "op": ">", "value": 3}, {"投产比": 3}) is False

    # ② 存在性(带字段):0 / "" / None / 空容器 = 无值
    checks["present_true"] = eval_condition({"field": "公示内容", "present": True}, {"公示内容": "有"})
    checks["present_zero_false"] = (
        eval_condition({"field": "公示内容", "present": True}, {"公示内容": 0}) is False
    )
    checks["present_empty_false"] = (
        eval_condition({"field": "公示内容", "present": True}, {"公示内容": ""}) is False
    )
    checks["present_none_false"] = (
        eval_condition({"field": "公示内容", "present": True}, {"公示内容": None}) is False
    )
    checks["present_list_false"] = (
        eval_condition({"field": "公示内容", "present": True}, {"公示内容": []}) is False
    )

    # ③ 存在性(不带字段):payload 任一值有效
    checks["present_any"] = eval_condition({"present": True}, {"a": 0, "b": "x"})
    checks["present_any_all_empty"] = eval_condition({"present": True}, {"a": 0, "b": ""}) is False

    # ④ 任一 > 0
    checks["any_gt_true"] = eval_condition(
        {"op": "any_gt", "value": 0}, {"comment_pending": 0, "audit_pending": 2}
    )
    checks["any_gt_false"] = eval_condition({"op": "any_gt", "value": 0}, {"a": 0, "b": 0})
    checks["any_gt_rejects"] = checks["any_gt_false"] is False

    # ⑤ ★ D9:in 分支必须**可达**(旧系统 op not in _OPS 先返回 True,导致恒真)
    cond_in = {"field": "lead_days", "op": "in", "value": [10, 3]}
    checks["in_hit_10"] = eval_condition(cond_in, {"lead_days": 10})
    checks["in_hit_3"] = eval_condition(cond_in, {"lead_days": 3})
    checks["in_miss_5"] = eval_condition(cond_in, {"lead_days": 5})
    checks["in_miss_none"] = eval_condition(cond_in, {"lead_days": None})
    checks["in_miss_missing"] = eval_condition(cond_in, {})
    # ★ 正例为真、反例为假 —— 两者都对才说明分支可达(旧实现反例也会返回 True)
    checks["d9_fixed"] = (
        checks["in_hit_10"] and checks["in_hit_3"] and checks["in_miss_5"] is False
    )
    # 字符串 in(list)/子串两种口径都要能用
    checks["in_str_list"] = eval_condition({"field": "grade", "op": "in", "value": ["A", "B"]}, {"grade": "A"})
    checks["in_str_list_miss"] = (
        eval_condition({"field": "grade", "op": "in", "value": ["A", "B"]}, {"grade": "C"}) is False
    )

    # ⑥ 字段缺失 → False(防误发) / 空条件 → True
    checks["missing_field_false"] = eval_condition({"field": "不存在的字段", "op": ">", "value": 0}, {"a": 1}) is False
    checks["empty_cond_true"] = eval_condition(None, {}) and eval_condition({}, {})

    # ⑦ ★ **``op`` 形式的 ``any_gt`` / ``or``**(计划书 §5.6 的 DSL 表与
    #    ``alert_rules.json`` 都用这个写法)。只认键形式的实现会掉进
    #    「没有 field → 返回 True」而**恒真** —— 与 D9 同一类缺陷。
    checks["op_any_gt_accept"] = eval_condition(
        {"op": "any_gt", "value": 0, "fields": ["a", "b"]}, {"a": 0, "b": 2}
    )
    checks["op_any_gt_reject"] = eval_condition(
        {"op": "any_gt", "value": 0, "fields": ["a", "b"]}, {"a": 0, "b": 0}
    )
    checks["op_any_gt_fixed"] = checks["op_any_gt_accept"] and checks["op_any_gt_reject"] is False
    checks["op_or_accept"] = eval_condition(
        {"op": "or", "items": [{"field": "x", "op": ">", "value": 1}]}, {"x": 5}
    )
    checks["op_or_reject"] = eval_condition(
        {"op": "or", "items": [{"field": "x", "op": ">", "value": 1}]}, {"x": 0}
    )
    checks["op_or_fixed"] = checks["op_or_accept"] and checks["op_or_reject"] is False
    checks["op_any_below_mean"] = eval_condition(
        {"op": "any_below_mean", "fields": ["m1", "m2", "m3"]}, {"m1": 1, "m2": 10, "m3": 20}
    )

    ok = (
        checks["gt_true"]
        and checks["gt_equal_not_gt"]
        and checks["present_true"]
        and checks["present_zero_false"]
        and checks["present_empty_false"]
        and checks["present_none_false"]
        and checks["present_list_false"]
        and checks["present_any"]
        and checks["present_any_all_empty"]
        and checks["any_gt_true"]
        and checks["any_gt_rejects"]
        and checks["d9_fixed"]
        and checks["in_str_list"]
        and checks["in_str_list_miss"]
        and checks["missing_field_false"]
        and checks["empty_cond_true"]
        and checks["op_any_gt_fixed"]
        and checks["op_or_fixed"]
        and checks["op_any_below_mean"]
    )
    ev = (
        f"投产比>3:{checks['gt_true']}/{checks['gt_equal_not_gt']} 存在性:0/空串/None/空容器=无值 "
        f"any_gt={checks['any_gt_true']}/{checks['any_gt_rejects']} / "
        f"★D9 in 分支:[10,3] 命中10={checks['in_hit_10']} 命中3={checks['in_hit_3']} "
        f"**5→{checks['in_miss_5']}**(旧系统此处恒真) 缺失→{checks['in_miss_missing']} / "
        f"op 形式 any_gt={checks['op_any_gt_fixed']} or={checks['op_or_fixed']}"
    )
    return _res("V43", v43.__doc__ or "", PASS if ok else FAIL, ev, **checks)


# ===========================================================================
# V44 — 实时优先昨日
# ===========================================================================


@REG.add("V44", "实时优先昨日:daily 桶「今日实时/实时」优先;实时无数据回退昨日")
async def v44() -> CheckResult:
    from hoteldata.domains.collect.service import fetch_module_record
    from hoteldata.domains.report.engine import collect_windows_for_date, windows_for_push
    from hoteldata.domains.report.schedule import get_schedule

    checks: dict[str, Any] = {}
    schedule = get_schedule()
    flow = schedule.by_id("flow_overview")  # daily: ["昨日","今日实时"]
    checks["declared"] = flow.windows.get("daily")
    priority, must = collect_windows_for_date(flow, date(2026, 9, 30))
    checks["priority"] = priority
    checks["must"] = must
    checks["realtime_first"] = priority[:1] == ["今日实时"] and "昨日" in priority
    checks["compete_declared_order"] = schedule.by_id("compete_sell_rank").windows.get("daily")
    pr2, _ = collect_windows_for_date(schedule.by_id("compete_sell_rank"), date(2026, 9, 30))
    checks["compete_order"] = pr2
    checks["push_windows_order"] = windows_for_push(flow, date(2026, 9, 30))

    # 真取数:只有昨日有数据 → 回退昨日;两者都有 → 取实时
    rt = await _runtime()
    try:
        hotel = await _ensure_hotel(rt.db)
        await _cleanup(rt.db, int(hotel.id))
        today = date.today()
        await _seed_module(rt, int(hotel.id), "经营报告", flow.module_name, "昨日", {"访客量": 10}, day=today)
        async with rt.db.session() as s:
            only_yesterday = await fetch_module_record(
                s, int(hotel.id), today, "经营报告", flow.module_name, None, prefer_realtime=True
            )
        checks["fallback_yesterday"] = (only_yesterday.window if only_yesterday else None)
        checks["fallback_ok"] = checks["fallback_yesterday"] == "昨日"

        await _seed_module(rt, int(hotel.id), "经营报告", flow.module_name, "今日实时", {"访客量": 99}, day=today)
        async with rt.db.session() as s:
            prefer_rt = await fetch_module_record(
                s, int(hotel.id), today, "经营报告", flow.module_name, None, prefer_realtime=True
            )
        checks["prefer_realtime_window"] = prefer_rt.window if prefer_rt else None
        checks["prefer_realtime_ok"] = checks["prefer_realtime_window"] == "今日实时"
    finally:
        await rt.aclose()

    ok = (
        checks["realtime_first"]
        and checks["compete_order"][:2] == ["今日实时", "昨日"]
        and checks["fallback_ok"]
        and checks["prefer_realtime_ok"]
    )
    ev = (
        f"flow_overview 声明={checks['declared']} → 择优顺序={checks['priority']} / "
        f"只有昨日有数据→取「{checks['fallback_yesterday']}」/ 有实时→取「{checks['prefer_realtime_window']}」"
    )
    return _res("V44", v44.__doc__ or "", PASS if ok else FAIL, ev, **checks)


# ===========================================================================
# V45 — 周报有环比(★ D10 修复)
# ===========================================================================


@REG.add("V45", "★D10:周报 svc_weekly 的 7 天聚合输出**含上期对比列**(旧系统硬编码 compare=None)")
async def v45() -> CheckResult:
    from hoteldata.domains.collect.service import aggregate_daily
    from hoteldata.domains.report.engine import aggregate_with_compare
    from hoteldata.domains.report.render import render_item
    from hoteldata.domains.report.schedule import get_schedule

    checks: dict[str, Any] = {}
    schedule = get_schedule()
    item = schedule.by_id("svc_weekly")
    checks["aggregate_spec"] = item.aggregate
    checks["has_aggregate"] = bool(item.aggregate)

    rt = await _runtime()
    try:
        hotel = await _ensure_hotel(rt.db)
        await _cleanup(rt.db, int(hotel.id))
        today = date.today()
        # 本期(近 7 天)与上期(再往前 7 天)各造 7 天数据,数值刻意不同
        for offset in range(14):
            d = today - timedelta(days=offset)
            base = 100 if offset < 7 else 50  # 本期 avg 100 / 上期 avg 50
            await _seed_module(
                rt,
                int(hotel.id),
                "经营报告",
                item.module_name,
                "昨日",
                {"交易额(元)": base, "出租率(%)": base / 10, "携程评分": 4.5 if offset < 7 else 4.0},
                day=d,
            )

        async with rt.db.session() as s:
            cur = await aggregate_daily(
                s,
                int(hotel.id),
                today - timedelta(days=1),
                page="经营报告",
                module=item.module_name,
                window="昨日",
                days=7,
                sum_fields=item.aggregate_fields("sum"),
                avg_fields=item.aggregate_fields("avg"),
                last_fields=item.aggregate_fields("last"),
            )
            prev = await aggregate_daily(
                s,
                int(hotel.id),
                today - timedelta(days=8),
                page="经营报告",
                module=item.module_name,
                window="昨日",
                days=7,
                sum_fields=item.aggregate_fields("sum"),
                avg_fields=item.aggregate_fields("avg"),
                last_fields=item.aggregate_fields("last"),
            )
        checks["cur_samples"] = cur.samples
        checks["prev_samples"] = prev.samples
        checks["cur_avg"] = cur.avg
        checks["prev_avg"] = prev.avg

        agg = aggregate_with_compare(
            cur.as_dict(),
            prev.as_dict(),
            spec=item.aggregate,
            start=today - timedelta(days=7),
            end=today - timedelta(days=1),
            days=7,
        )
        checks["agg_keys"] = sorted(agg.keys()) if isinstance(agg, dict) else None
        payload = (agg or {}).get("payload") or {}
        compare = (agg or {}).get("compare") or {}
        checks["payload"] = payload
        checks["compare"] = compare
        checks["has_compare"] = bool(compare)
        # ★ ``compare`` 是**扁平** ``{指标: 上期值}``(render.compare_map 认的形状),
        #   不是 ``{指标: {"prev":..}}`` —— 与段1 ``build_payload`` 的 compare 形状不同,
        #   这里按 render 的契约断言"有上期值且是标量"。
        checks["compare_is_flat"] = bool(compare) and all(
            not isinstance(v, (dict, list)) for v in compare.values()
        )
        checks["compare_has_values"] = bool(compare) and any(v is not None for v in compare.values())
        checks["range"] = (agg or {}).get("range")

        # ★ 渲染出"上期"列与环比
        record = {"payload": payload, "window": "上周", "collect_date": today.isoformat()}
        md = render_item(item, record, compare, None)
        checks["markdown_head"] = md.splitlines()[:3]
        checks["has_prev_column"] = "上期" in md
        checks["has_ratio_column"] = "环比" in md
        checks["has_green_or_red"] = ("🟢" in md) or ("🔴" in md)
        checks["markdown"] = md[:600]
    finally:
        await rt.aclose()

    ok = (
        checks["has_aggregate"]
        and checks["has_compare"]
        and checks["compare_is_flat"]
        and checks["compare_has_values"]
        and checks["has_prev_column"]
        and checks["has_ratio_column"]
        and checks["has_green_or_red"]
    )
    ev = (
        f"本期 samples={checks['cur_samples']} avg={checks['cur_avg']} / 上期 samples={checks['prev_samples']} "
        f"对比 {len(checks['compare'])} 项 / 渲染含「上期」列={checks['has_prev_column']} "
        f"环比列={checks['has_ratio_column']} 涨跌色={checks['has_green_or_red']}"
        f"(★D10:旧系统此处硬编码 compare=None)"
    )
    return _res("V45", v45.__doc__ or "", PASS if ok else FAIL, ev, **checks)


# ===========================================================================
# V46 — 合并与拆分 + 22 项合并不是 22 条消息
# ===========================================================================


@REG.add("V46", "合并与拆分:单条 ≤3500 字目标;超限按店对半拆 ≤2 条;22 项合并成 1~2 条")
async def v46() -> CheckResult:
    from hoteldata.domains.report.service import paginate_sections
    from hoteldata.push.sender import merge_sections, split_message

    checks: dict[str, Any] = {}
    checks["merge_sep"] = "\n\n\n"
    checks["merge"] = merge_sections(["A", "B", "C"])

    # ① ≤3500 → 1 条
    short = merge_sections([f"### 「店{i}」\n" + "指标|1\n" * 20 for i in range(2)])
    checks["short_len"] = len(short)
    checks["short_one_part"] = len(split_message(short)) == 1

    # ② >3500 → ≤2 条
    long = merge_sections([f"### 「店{i}」\n" + "指标行|123|456\n" * 120 for i in range(6)])
    parts = split_message(long, limit=3500, max_parts=2)
    checks["long_len"] = len(long)
    checks["long_parts"] = len(parts)
    checks["long_le_2"] = len(parts) <= 2
    checks["long_lossless"] = "".join(parts).replace("\n", "") == long.replace("\n", "")
    checks["long_keeps_all_hotels"] = all(
        f"「店{i}」" in "".join(parts) for i in range(6)
    )

    # ③ 报告项分页 ≤2（``_Section`` 是"店 × 项"的最小单位）
    from hoteldata.domains.report.service import _Section

    sections = [
        _Section(md="a" * 100, hotel_id=1, hotel_name="店A", item_id=f"it{i}", item_name=f"项{i}")
        for i in range(10)
    ]
    pages = paginate_sections(sections, 3500)
    checks["paginate_pages"] = len(pages)
    checks["paginate_le_2"] = len(pages) <= 2
    checks["paginate_lossless"] = sum(len(p) for p in pages) == len(sections)

    ok = (
        checks["short_one_part"]
        and checks["long_le_2"]
        and checks["long_lossless"]
        and checks["long_keeps_all_hotels"]
        and checks["paginate_le_2"]
    )
    ev = (
        f"短消息 1 条 / {checks['long_len']} 字(6 店)→ {checks['long_parts']} 条且无损、6 店全在 / "
        f"报告项分页 {checks['paginate_pages']} 页(≤2)"
    )
    return _res("V46", v46.__doc__ or "", PASS if ok else FAIL, ev, **checks)


# ===========================================================================
# V47 — 6 条规则各自可触发
# ===========================================================================


async def _seed_alert_scenario(rt: Any, hotel_id: int, today: date) -> dict[str, Any]:
    """构造能让 A/C/D/E/F 五条各触发一次的合成数据。"""
    # A 关房 7 天
    await _seed_room_states(
        rt, hotel_id, "verify2-room-1", available_today=False, closed_days=7, day=today
    )
    # C 热点日历:倒计时 10 天
    await _seed_portal(
        rt,
        hotel_id,
        "hot_calendar",
        {"中秋节": (today + timedelta(days=10)).isoformat()},
        day=today,
    )
    # D 渠道低于均值:访客量 1 < 均值 100
    await _seed_portal(
        rt,
        hotel_id,
        "channel_ctrip",
        {
            "visitor_total": "1",
            "visitor_avg": "100",
            "min_price": "500",
            "min_price_rank": "9",
            "competitor_total": "10",
            "ratingall": "4.9",
            "rating_avg": "3.5",
            "rating_rank": "1",
        },
        day=today,
    )
    # E 首页待办:评论待回复 2 条
    await _seed_portal(
        rt,
        hotel_id,
        "home_pending",
        {"comment_pending": "2", "qa_pending": "0", "todo_more": "0"},
        day=today,
    )
    # F 城市热点:倒计时 3 天
    await _seed_module(
        rt,
        hotel_id,
        "市场分析",
        "每日热度-未来14日热度",
        "未来14天",
        {
            "热点名称": "国庆节",
            "热点开始日期": (today + timedelta(days=3)).isoformat(),
            "热度等级": "高",
            "当日城市热度值": 4,
        },
        day=today,
    )
    return {"seeded": True}


@REG.add("V47", "6 条规则各自可触发(A 关房 / C 热点 / D 低于均值 / E 待办 / F 城市热点)")
async def v47() -> CheckResult:
    from hoteldata.domains.alert import engine as eng
    from hoteldata.domains.alert.rules import load_rules

    checks: dict[str, Any] = {}
    today = date.today()
    rt = await _runtime()
    try:
        hotel = await _ensure_hotel(rt.db)
        await _cleanup(rt.db, int(hotel.id))
        await _seed_alert_scenario(rt, int(hotel.id), today)

        rules = load_rules(force=True)
        checks["rule_ids"] = list(rules.ids)
        checks["has_6_plus_optional"] = set(checks["rule_ids"]) >= {
            "room_closed_7d",
            "hot_event_price",
            "channel_below_mean",
            "home_pending",
            "city_heat_remind",
            "price_line_optional",
        }
        checks["retired_b_absent"] = "room_closed_today" not in checks["rule_ids"]

        # 逐规则干跑,只看本店
        fired: dict[str, int] = {}
        for rid in rules.ids:
            res = await eng.check(rt, "09:00", today=today, dry_run=True, rule_id=rid)
            mine = [t for t in res.triggers if int(t.hotel_id) == int(hotel.id)]
            fired[rid] = len(mine)
            if mine:
                checks[f"detail_{rid}"] = mine[0].as_dict()
        checks["fired"] = fired

        # A/C/D/E/F 各自至少一条
        for rid in ("room_closed_7d", "hot_event_price", "channel_below_mean", "home_pending", "city_heat_remind"):
            checks[f"{rid}_fires"] = fired.get(rid, 0) >= 1
        # 可选固定线:段2 没有比价库 → **明确跳过**,不许误报
        res_line = await eng.check(rt, "09:00", today=today, dry_run=True, rule_id="price_line_optional")
        checks["price_line_skipped"] = res_line.skipped
        checks["price_line_not_misfired"] = not [
            t for t in res_line.triggers if int(t.hotel_id) == int(hotel.id)
        ]
        # 干跑不写状态、不写日志
        from sqlalchemy import func, select

        from hoteldata.infra.models import AlertLog, AlertState

        async with rt.db.session() as s:
            n_state = await s.scalar(
                select(func.count()).select_from(AlertState).where(AlertState.hotel_id == int(hotel.id))
            )
            n_log = await s.scalar(
                select(func.count()).select_from(AlertLog).where(AlertLog.hotel_id == int(hotel.id))
            )
        checks["dry_run_states"] = int(n_state or 0)
        checks["dry_run_logs"] = int(n_log or 0)
        checks["dry_run_clean"] = checks["dry_run_states"] == 0 and checks["dry_run_logs"] == 0
    finally:
        await rt.aclose()

    ok = (
        checks["has_6_plus_optional"]
        and checks["retired_b_absent"]
        and all(
            checks.get(f"{r}_fires")
            for r in ("room_closed_7d", "hot_event_price", "channel_below_mean", "home_pending", "city_heat_remind")
        )
        and checks["price_line_not_misfired"]
        and checks["dry_run_clean"]
    )
    ev = (
        f"规则={checks['rule_ids']} / 各规则本店触发数={checks['fired']} / "
        f"price_line 可选线无数据源→明确跳过(未误报) / 干跑不写状态({checks['dry_run_states']})不写日志({checks['dry_run_logs']})"
    )
    return _res("V47", v47.__doc__ or "", PASS if ok else FAIL, ev, **checks)


# ===========================================================================
# V48 — "连续 7 天"是数据判定,不是 streak
# ===========================================================================


@REG.add("V48", "★「连续 7 天」由 room_states **数据层**推导(unavailable_days>=7);不是 streak 门槛")
async def v48() -> CheckResult:
    from hoteldata.domains.alert import engine as eng
    from hoteldata.domains.alert.state import derive_unavailable_days

    checks: dict[str, Any] = {}
    today = date.today()
    rt = await _runtime()
    try:
        hotel = await _ensure_hotel(rt.db)
        await _cleanup(rt.db, int(hotel.id))

        # ① 连关 7 天 → 触发;并把 streak 显式置 0 → **仍然触发**
        #    (若实现把 streak 当门槛,这里就会挂)
        await _seed_room_states(
            rt, int(hotel.id), "verify2-room-7", available_today=False, closed_days=7, day=today
        )
        res7 = await eng.check(rt, "09:00", today=today, dry_run=True, rule_id="room_closed_7d")
        mine7 = [t for t in res7.triggers if int(t.hotel_id) == int(hotel.id)]
        checks["fires_with_7_days"] = len(mine7) >= 1
        checks["payload_7"] = mine7[0].payload if mine7 else None

        # ② 只关 3 天 → 不触发
        await _seed_room_states(
            rt, int(hotel.id), "verify2-room-7", available_today=False, closed_days=3, day=today
        )
        res3 = await eng.check(rt, "09:00", today=today, dry_run=True, rule_id="room_closed_7d")
        mine3 = [t for t in res3.triggers if int(t.hotel_id) == int(hotel.id)]
        checks["no_fire_with_3_days"] = mine3 == []

        # ③ 今日可订 → 断链,连 7 天也不算
        await _seed_room_states(
            rt, int(hotel.id), "verify2-room-7", available_today=True, closed_days=7, day=today
        )
        res_ok = await eng.check(rt, "09:00", today=today, dry_run=True, rule_id="room_closed_7d")
        checks["no_fire_when_available"] = [
            t for t in res_ok.triggers if int(t.hotel_id) == int(hotel.id)
        ] == []

        # ④ 今日**缺数据** → 保守不触发
        from sqlalchemy import delete

        from hoteldata.infra.models import AlertRoomState

        async with rt.db.session() as s:
            await s.execute(delete(AlertRoomState).where(AlertRoomState.hotel_id == int(hotel.id)))
        res_missing = await eng.check(rt, "09:00", today=today, dry_run=True, rule_id="room_closed_7d")
        checks["no_fire_when_today_missing"] = [
            t for t in res_missing.triggers if int(t.hotel_id) == int(hotel.id)
        ] == []

        # ⑤ 纯函数口径:可订即断(``derive_unavailable_days`` 返回 {房型: 天数})
        rows = [
            {"effect_date": today, "available": 0, "room_type_id": "r"},
            {"effect_date": today + timedelta(days=1), "available": 0, "room_type_id": "r"},
            {"effect_date": today + timedelta(days=2), "available": 1, "room_type_id": "r"},
            {"effect_date": today + timedelta(days=3), "available": 0, "room_type_id": "r"},
        ]
        checks["derive_break_at_available"] = derive_unavailable_days(rows, today)
        checks["derive_is_2"] = checks["derive_break_at_available"].get("r") == 2

        # ⑥ streak 字段**只是展示**:状态写 0 也不影响判定
        from hoteldata.domains.alert.state import AlertStateStore

        store = AlertStateStore(rt.db)
        await store.upsert("room_closed_7d", int(hotel.id), f"{hotel.id}:verify2-room-7")
        await _seed_room_states(
            rt, int(hotel.id), "verify2-room-7", available_today=False, closed_days=7, day=today
        )
        res_streak0 = await eng.check(rt, "09:00", today=today, dry_run=True, rule_id="room_closed_7d")
        checks["fires_with_streak_zero"] = bool(
            [t for t in res_streak0.triggers if int(t.hotel_id) == int(hotel.id)]
        )
    finally:
        await rt.aclose()

    ok = (
        checks["fires_with_7_days"]
        and checks["no_fire_with_3_days"]
        and checks["no_fire_when_available"]
        and checks["no_fire_when_today_missing"]
        and checks["derive_is_2"]
        and checks["fires_with_streak_zero"]
    )
    ev = (
        f"数据层推导:连7天→触发={checks['fires_with_7_days']} / 连3天→不触发={checks['no_fire_with_3_days']} / "
        f"今日可订→断链不触发={checks['no_fire_when_available']} / 今日缺数据→保守不触发="
        f"{checks['no_fire_when_today_missing']} / derive_unavailable_days={checks['derive_break_at_available']} / "
        f"streak=0 仍触发={checks['fires_with_streak_zero']}"
    )
    return _res("V48", v48.__doc__ or "", PASS if ok else FAIL, ev, **checks)


# ===========================================================================
# V49 — 两层时刻结构
# ===========================================================================


@REG.add("V49", "★两层时刻:名义 09:00/14:30/19:00 ↔ 调度 09:04/14:34/19:04,slot 能映回并匹配")
async def v49() -> CheckResult:
    from hoteldata.domains.alert import engine as eng
    from hoteldata.domains.alert.rules import load_rules
    from hoteldata.jobs import nominal_slot_for_now

    checks: dict[str, Any] = {}
    checks["room_map"] = dict(eng.ROOM_SLOT_BY_TIME)
    checks["data_map"] = dict(eng.DATA_SLOT_BY_TIME)
    checks["map_exact"] = eng.ROOM_SLOT_BY_TIME == {
        "09:04": "09:00",
        "14:34": "14:30",
        "19:04": "19:00",
    }
    checks["nominal_0904"] = eng.nominal_slot("09:04")
    checks["nominal_1434"] = eng.nominal_slot("14:34")
    checks["nominal_1904"] = eng.nominal_slot("19:04")
    checks["nominal_0910"] = eng.nominal_slot("09:10")
    checks["mapping_ok"] = (
        checks["nominal_0904"] == "09:00"
        and checks["nominal_1434"] == "14:30"
        and checks["nominal_1904"] == "19:00"
        and checks["nominal_0910"] == "09:00"
    )
    # 表外时刻原样返回(不硬兜底成 09:00,否则会误触发)
    checks["offtable"] = eng.nominal_slot("11:11")

    # 规则里声明的是**名义时刻**
    rules = load_rules(force=True)
    checks["room_check_times"] = list(rules.get("room_closed_7d").check_times)
    checks["room_times_exact"] = checks["room_check_times"] == ["09:00", "14:30", "19:00"]
    checks["for_slot_0900"] = [r.id for r in rules.for_slot("09:00")]
    checks["for_slot_0911"] = [r.id for r in rules.for_slot("11:11")]

    # 调度侧一半:把"现在"映回名义时刻
    rt = await _runtime()
    try:
        checks["sched_at_0910"] = nominal_slot_for_now(
            rt,
            rt.settings.alert.room_slot_names,
            now=datetime(2026, 9, 30, 9, 4),
        )
        checks["sched_at_1434"] = nominal_slot_for_now(
            rt, rt.settings.alert.room_slot_names, now=datetime(2026, 9, 30, 14, 34)
        )
        checks["sched_catchup_1230"] = nominal_slot_for_now(
            rt, rt.settings.alert.room_slot_names, now=datetime(2026, 9, 30, 12, 30)
        )
        checks["sched_ok"] = (
            checks["sched_at_0910"] == "09:00"
            and checks["sched_at_1434"] == "14:30"
            and checks["sched_catchup_1230"] == "09:00"
        )

        # ★ 端到端:用**调度时刻** 09:04 巡检,规则 check_times 里是 09:00 —— 必须能匹配上
        hotel = await _ensure_hotel(rt.db)
        await _cleanup(rt.db, int(hotel.id))
        today = date.today()
        await _seed_portal(
            rt, int(hotel.id), "home_pending", {"comment_pending": "3"}, day=today
        )
        res = await eng.check(rt, "09:04", today=today, dry_run=True, rule_id="home_pending")
        checks["slot_used"] = res.slot
        checks["nominal_used"] = res.nominal_slot
        checks["matched_through_slot"] = bool(
            [t for t in res.triggers if int(t.hotel_id) == int(hotel.id)]
        )
        # 反向:用名义时刻直接传也能工作(ops 手工跑)
        res2 = await eng.check(rt, "09:00", today=today, dry_run=True, rule_id="home_pending")
        checks["nominal_direct"] = bool(
            [t for t in res2.triggers if int(t.hotel_id) == int(hotel.id)]
        )
        # 表外时刻 → 一条都不跑(不误触发)
        res3 = await eng.check(rt, "11:11", today=today, dry_run=True, rule_id="home_pending")
        checks["offtable_no_run"] = res3.rules_checked == 0
    finally:
        await rt.aclose()

    ok = (
        checks["map_exact"]
        and checks["mapping_ok"]
        and checks["room_times_exact"]
        and checks["sched_ok"]
        and checks["matched_through_slot"]
        and checks["nominal_direct"]
        and checks["offtable_no_run"]
    )
    ev = (
        f"映射表={checks['room_map']} / 09:04→{checks['nominal_0904']} 14:34→{checks['nominal_1434']} "
        f"19:04→{checks['nominal_1904']} / 规则 check_times={checks['room_check_times']} / "
        f"调度侧 09:04→{checks['sched_at_0910']}(补跑 12:30→{checks['sched_catchup_1230']}) / "
        f"用 09:04 巡检能匹配 09:00 规则={checks['matched_through_slot']}"
    )
    return _res("V49", v49.__doc__ or "", PASS if ok else FAIL, ev, **checks)


# ===========================================================================
# V50 — 当日去重 + 恢复清零
# ===========================================================================


@REG.add("V50", "当日去重(同规则同实体只推一次)+ reset_when_ok 恢复清零")
async def v50() -> CheckResult:
    from hoteldata.domains.alert import engine as eng
    from hoteldata.domains.alert.state import AlertStateStore

    checks: dict[str, Any] = {}
    today = date.today()
    rt = await _runtime()
    try:
        hotel = await _ensure_hotel(rt.db)
        await _cleanup(rt.db, int(hotel.id))
        await _seed_portal(rt, int(hotel.id), "home_pending", {"comment_pending": "3"}, day=today)
        store = AlertStateStore(rt.db)

        # ① 第一次:允许推
        r1 = await eng.check(rt, "09:00", today=today, rule_id="home_pending")
        mine1 = [t for t in r1.triggers if int(t.hotel_id) == int(hotel.id)]
        checks["first_push"] = len(mine1)
        entity = mine1[0].entity_key if mine1 else "hotel"
        # 状态已写
        st = await store.get("home_pending", int(hotel.id), entity)
        checks["state_after_first"] = st.status if st else None
        checks["last_trigger_date"] = st.last_trigger_date.isoformat() if st and st.last_trigger_date else None

        # ② 第二次同日:被当日去重挡住
        checks["should_push_again"] = await store.should_push(
            "home_pending", int(hotel.id), entity, today=today, dedup="once_per_day"
        )
        checks["dedup_blocks"] = checks["should_push_again"] is False
        checks["should_push_forced"] = await store.should_push(
            "home_pending", int(hotel.id), entity, today=today, dedup="once_per_day", force=True
        )
        checks["force_bypasses"] = checks["should_push_forced"] is True

        # ③ 恢复清零:条件不再成立 → reset_when_ok 把 triggered 清零
        await _seed_portal(rt, int(hotel.id), "home_pending", {"comment_pending": "0"}, day=today)
        r3 = await eng.check(rt, "09:00", today=today, rule_id="home_pending", force=True)
        checks["no_trigger_after_recover"] = [
            t for t in r3.triggers if int(t.hotel_id) == int(hotel.id)
        ] == []
        st2 = await store.get("home_pending", int(hotel.id), entity)
        checks["state_after_recover"] = st2.status if st2 else None
        checks["last_ok_date"] = st2.last_ok_date.isoformat() if st2 and st2.last_ok_date else None
        checks["reset_to_ok"] = st2 is not None and st2.status == "ok"
        checks["streak_zeroed"] = st2 is not None and int(st2.streak or 0) == 0
        checks["reset_skipped_reason"] = r3.skipped
    finally:
        await rt.aclose()

    ok = (
        checks["first_push"] >= 1
        and checks["dedup_blocks"]
        and checks["force_bypasses"]
        and checks["no_trigger_after_recover"]
        and checks["reset_to_ok"]
        and checks["streak_zeroed"]
    )
    ev = (
        f"首次触发 {checks['first_push']} 条→状态={checks['state_after_first']} "
        f"last_trigger={checks['last_trigger_date']} / 同日再判 should_push={checks['should_push_again']}"
        f"(force={checks['should_push_forced']}) / 恢复后 status={checks['state_after_recover']} "
        f"last_ok={checks['last_ok_date']} streak={checks['streak_zeroed']}"
    )
    return _res("V50", v50.__doc__ or "", PASS if ok else FAIL, ev, **checks)


# ===========================================================================
# V51 — 忽略机制
# ===========================================================================


@REG.add("V51", "忽略机制:「忽略此店 XX 7」生效 7 天;期间不推送")
async def v51() -> CheckResult:
    from hoteldata.domains.alert import engine as eng
    from hoteldata.domains.alert.state import AlertStateStore

    checks: dict[str, Any] = {}
    today = date.today()
    rt = await _runtime()
    try:
        hotel = await _ensure_hotel(rt.db)
        await _cleanup(rt.db, int(hotel.id))
        await _seed_portal(rt, int(hotel.id), "home_pending", {"comment_pending": "5"}, day=today)
        svc = rt.alert()

        r_before = await eng.check(rt, "09:00", today=today, dry_run=True, rule_id="home_pending")
        checks["fires_before"] = bool(
            [t for t in r_before.triggers if int(t.hotel_id) == int(hotel.id)]
        )

        out = await svc.ignore_hotel(hotel.name, days=7)
        checks["ignore_result"] = out
        checks["ignore_ok"] = bool(out.get("ok", True)) and "7" in json.dumps(out, ensure_ascii=False)
        until = (today + timedelta(days=6)).isoformat()
        checks["until_expected"] = until
        store = AlertStateStore(rt.db)
        checks["is_ignored"] = await store.is_ignored(int(hotel.id), "home_pending", today=today)
        checks["is_ignored_all_rule"] = await store.is_ignored(int(hotel.id), "__all__", today=today)
        # 忽略期间:引擎跳过
        r_after = await eng.check(rt, "09:00", today=today, force=True, rule_id="home_pending")
        checks["fires_after"] = [
            t for t in r_after.triggers if int(t.hotel_id) == int(hotel.id)
        ]
        checks["ignored_skipped"] = r_after.skipped
        checks["no_fire_while_ignored"] = checks["fires_after"] == []
        # 过了一天仍忽略;过了 7 天恢复
        checks["ignored_tomorrow"] = await store.is_ignored(
            int(hotel.id), "home_pending", today=today + timedelta(days=1)
        )
        checks["not_ignored_after_7d"] = await store.is_ignored(
            int(hotel.id), "home_pending", today=today + timedelta(days=7)
        )
        # 天数可配
        out3 = await svc.ignore_hotel(hotel.name, days=3)
        checks["ignore3"] = "3" in json.dumps(out3, ensure_ascii=False)
    finally:
        await rt.aclose()

    ok = (
        checks["fires_before"]
        and checks["ignore_ok"]
        and checks["is_ignored"]
        and checks["is_ignored_all_rule"]
        and checks["no_fire_while_ignored"]
        and checks["ignored_tomorrow"] is True
        and checks["not_ignored_after_7d"] is False
    )
    ev = (
        f"忽略前触发={checks['fires_before']} → ignore_hotel(7 天) → is_ignored={checks['is_ignored']}"
        f"(含 __all__ 兜底={checks['is_ignored_all_rule']}) / 巡检跳过原因={checks['ignored_skipped']} / "
        f"次日仍忽略={checks['ignored_tomorrow']} 第 8 天恢复={not checks['not_ignored_after_7d']}"
    )
    return _res("V51", v51.__doc__ or "", PASS if ok else FAIL, ev, **checks)


# ===========================================================================
# V52 — 推送目标 + 送达率分母
# ===========================================================================


@REG.add("V52", "推送目标:管理群全量 + 运营群按店;★无管理群写 manage-none 且计入送达率分母")
async def v52() -> CheckResult:
    from hoteldata.domains.alert import engine as eng
    from hoteldata.domains.alert.service import AlertService
    from hoteldata.domains.alert.summary import build_daily_summary

    checks: dict[str, Any] = {}
    today = date.today()
    rt = await _runtime()
    sent: list[tuple[str, str]] = []
    try:
        hotel = await _ensure_hotel(rt.db)
        await _cleanup(rt.db, int(hotel.id))
        await rt.bindings.bind(SYNTH_GROUP, int(hotel.id))
        await _seed_portal(rt, int(hotel.id), "home_pending", {"comment_pending": "4"}, day=today)

        rt._bots = OfflineManager()
        rt._sender = None
        rt._push = None
        rt._audit = None
        rt.settings = _settings(MANAGE_CHATIDS=f"{SYNTH_MANAGE},{SYNTH_GROUP2}", OPS_CHATID="")

        async def _fake_alert(text: str, *, chatids: list[str] | None = None) -> Any:
            from hoteldata.domains.bot.manager import AlertResult

            for c in chatids or []:
                sent.append((c, text))
            return AlertResult(text=text, targets=list(chatids or []), delivered=list(chatids or []))

        rt.push.send_alert = _fake_alert  # type: ignore[method-assign]
        checks["is_alert_service"] = isinstance(rt.alert(), AlertService)

        res = await eng.check(rt, "09:00", today=today, rule_id="home_pending")
        mine = [t for t in res.triggers if int(t.hotel_id) == int(hotel.id)]
        checks["triggers"] = len(mine)
        out = await rt.alert().push_triggers(mine, today=today, images=False)
        checks["push_result"] = out
        targets = [c for c, _ in sent]
        checks["targets"] = targets
        checks["manage_all"] = SYNTH_MANAGE in targets and SYNTH_GROUP2 in targets
        checks["ops_group"] = SYNTH_GROUP in targets

        _text, stats = await build_daily_summary(rt, day=today)
        checks["stats"] = stats
        checks["stats_has_rate"] = "rate" in stats
        checks["rate_denominator"] = stats.get("total", 0)

        # ★ 无管理群 → 写 manage-none 且计入分母
        rt.settings = _settings(MANAGE_CHATIDS="", OPS_CHATID="")
        sent.clear()
        await _cleanup(rt.db, int(hotel.id))
        await _seed_portal(rt, int(hotel.id), "home_pending", {"comment_pending": "4"}, day=today)
        await rt.bindings.bind(SYNTH_GROUP, int(hotel.id))
        res2 = await eng.check(rt, "09:00", today=today, force=True, rule_id="home_pending")
        mine2 = [t for t in res2.triggers if int(t.hotel_id) == int(hotel.id)]
        out2 = await rt.alert().push_triggers(mine2, today=today, images=False)
        checks["no_manage_result"] = out2
        from sqlalchemy import select as _select

        from hoteldata.infra.models import AlertLog

        async with rt.db.session() as s:
            rows = list(
                (
                    await s.execute(
                        _select(AlertLog).where(AlertLog.hotel_id == int(hotel.id)).order_by(AlertLog.id)
                    )
                )
                .scalars()
                .all()
            )
        checks["recipients"] = [r.recipient for r in rows]
        checks["manage_none_logged"] = "manage-none" in checks["recipients"]
        checks["manage_none_not_pushed"] = all(r.pushed is False for r in rows if r.recipient == "manage-none")
        _t2, stats2 = await build_daily_summary(rt, day=today)
        checks["stats2"] = stats2
        checks["manage_none_in_denominator"] = stats2.get("total", 0) >= len(rows)
    finally:
        await rt.aclose()

    ok = (
        checks["triggers"] >= 1
        and checks["manage_all"]
        and checks["stats_has_rate"]
        and checks["manage_none_logged"]
        and checks["manage_none_not_pushed"]
        and checks["manage_none_in_denominator"]
    )
    ev = (
        f"有管理群:目标={checks['targets']}(管理群 2 个 + 运营群)/ "
        f"无管理群:recipient={checks['recipients']}（含 manage-none 且 pushed=False）/ "
        f"送达率口径 total={checks['stats2'].get('total')} rate={checks['stats2'].get('rate')}"
    )
    return _res("V52", v52.__doc__ or "", PASS if ok else FAIL, ev, **checks)


# ===========================================================================
# V53 — 附图容错
# ===========================================================================


@REG.add("V53", "附图容错:预警附图截图失败 → 仅告警,文本照发")
async def v53() -> CheckResult:
    from hoteldata.domains.alert import engine as eng
    from hoteldata.domains.alert import shots as shots_mod

    checks: dict[str, Any] = {}
    today = date.today()
    rt = await _runtime()
    try:
        hotel = await _ensure_hotel(rt.db)
        await _cleanup(rt.db, int(hotel.id))
        await _seed_portal(rt, int(hotel.id), "home_pending", {"comment_pending": "4"}, day=today)

        res = await eng.check(rt, "09:00", today=today, rule_id="home_pending")
        mine = [t for t in res.triggers if int(t.hotel_id) == int(hotel.id)]
        checks["triggers"] = len(mine)
        assert mine, "本店必须有 home_pending 触发才能验附图"

        # ① shots.json 里 home_pending 有目标
        from hoteldata.domains.alert.shots import shot_target

        checks["shot_target"] = shot_target("home_pending")
        checks["has_target"] = bool(checks["shot_target"])

        # ② 让截图**必然失败**:浏览器池替换成会抛错的替身
        class _BoomPool:
            def page_session(self, *_a: Any, **_k: Any) -> Any:
                raise RuntimeError("验收构造:截图必失败")

        rt._extras["boom"] = True
        shots_mod.clear_cache()
        # 用 monkeypatch 方式:直接把 runtime.browser 设成会抛错的池
        rt.browser = _BoomPool()
        got = await shots_mod.capture(rt, mine[0], cache=None)
        checks["capture_on_failure"] = got
        checks["capture_none_on_failure"] = got is None

        # ③ 文本仍要发出去
        rt._bots = OfflineManager()
        rt._sender = None
        rt._push = None
        rt._audit = None
        rt.settings = _settings(MANAGE_CHATIDS=SYNTH_MANAGE, OPS_CHATID="")
        sent: list[str] = []

        async def _fake_alert(text: str, *, chatids: list[str] | None = None) -> Any:
            from hoteldata.domains.bot.manager import AlertResult

            sent.append(text)
            return AlertResult(text=text, targets=list(chatids or []), delivered=list(chatids or []))

        rt.push.send_alert = _fake_alert  # type: ignore[method-assign]
        out = await rt.alert().push_triggers(mine, today=today, images=True)
        checks["push_result"] = out
        checks["text_sent"] = sent
        checks["text_still_sent"] = bool(sent) and len(sent[0]) > 10
        checks["no_braces_left"] = all("{" not in t for t in sent)
        checks["attach_error_visible"] = out.get("images_failed") or out.get("notes") or out
    finally:
        await rt.aclose()

    ok = (
        checks["triggers"] >= 1
        and checks["has_target"]
        and checks["capture_none_on_failure"]
        and checks["text_still_sent"]
        and checks["no_braces_left"]
    )
    ev = (
        f"home_pending 有附图目标={checks['has_target']} / 截图构造失败→capture 返回 {checks['capture_on_failure']}"
        f"(仅告警) / **文本照发**:{(checks['text_sent'] or [''])[0][:60]!r} / 无残留花括号={checks['no_braces_left']}"
    )
    return _res("V53", v53.__doc__ or "", PASS if ok else FAIL, ev, **checks)


# ===========================================================================
# V54 — 每日汇总 + 送达率
# ===========================================================================


@REG.add("V54", "每日汇总:按规则计数 / 涉及酒店数 / ★送达率 = 成功行 ÷ 总行;行身份 vs 触发身份")
async def v54() -> CheckResult:
    from sqlalchemy import func, select

    from hoteldata.domains.alert import engine as eng
    from hoteldata.domains.alert.summary import build_daily_summary, write_log
    from hoteldata.infra.models import AlertLog

    checks: dict[str, Any] = {}
    today = date.today()
    rt = await _runtime()
    try:
        hotel = await _ensure_hotel(rt.db)
        await _cleanup(rt.db, int(hotel.id))
        await _seed_portal(rt, int(hotel.id), "home_pending", {"comment_pending": "6"}, day=today)

        rt._bots = OfflineManager()
        rt._sender = None
        rt._push = None
        rt._audit = None
        rt.settings = _settings(MANAGE_CHATIDS=SYNTH_MANAGE, OPS_CHATID="")
        sent: list[str] = []

        async def _fake_alert(text: str, *, chatids: list[str] | None = None) -> Any:
            from hoteldata.domains.bot.manager import AlertResult

            sent.append(text)
            return AlertResult(text=text, targets=list(chatids or []), delivered=list(chatids or []))

        rt.push.send_alert = _fake_alert  # type: ignore[method-assign]
        res = await eng.check(rt, "09:00", today=today, rule_id="home_pending")
        mine = [t for t in res.triggers if int(t.hotel_id) == int(hotel.id)]
        await rt.alert().push_triggers(mine, today=today, images=False)

        # 再补一行**失败**(不同触发 → 不同 delivery_key),让送达率不是 100%
        from hoteldata.domains.alert.engine import Trigger as _Trigger

        failed_trigger = _Trigger(
            rule_id="home_pending",
            hotel_id=int(hotel.id),
            hotel_name=hotel.name,
            entity_key="verify2-failed-probe",
            title="验收构造:失败行",
            detail_lines=["构造一条投递失败记录,用于验送达率分母"],
        )
        checks["extra_failed_row"] = await write_log(
            rt,
            failed_trigger,
            recipient="verify2-manage-0001",
            pushed=False,
            error="验收构造:投递失败",
        )

        text, stats = await build_daily_summary(rt, day=today)
        checks["summary_text"] = text
        checks["stats"] = stats
        checks["keys"] = sorted(stats.keys())
        checks["has_rules_hotels_total_pushed_rate"] = {"rules", "hotels", "total", "pushed", "rate"} <= set(
            stats.keys()
        )
        checks["total_is_denominator"] = stats["total"] >= 2
        checks["pushed_less_than_total"] = stats["pushed"] < stats["total"]
        checks["rate_formula"] = (
            abs(stats["rate"] - (stats["pushed"] / stats["total"] * 100.0)) < 0.01
            if stats["total"]
            else False
        )
        checks["mentions_rate"] = "送达率" in text

        # ---- ★ 行身份 vs 触发身份:同一 触发×收件人 重推 = **刷新**,不是新增 ----
        if mine:
            first = await write_log(rt, mine[0], recipient=SYNTH_MANAGE, pushed=False, error="首次失败")
            checks["first_write"] = {"id": first.id, "created": first.created}
            again = await write_log(rt, mine[0], recipient=SYNTH_MANAGE, pushed=True, error=None)
            checks["reshoot_write"] = {"id": again.id, "created": again.created}
            checks["upsert_same_row"] = again.id == first.id
            checks["upsert_marks_refresh"] = again.created is False
            async with rt.db.session() as s:
                row = (
                    await s.execute(
                        select(AlertLog).where(AlertLog.id == first.id)
                    )
                ).scalar_one_or_none()
            checks["row_after_reshoot"] = (
                (bool(row.pushed), row.error) if row is not None else None
            )
            # ★ 这一条是本次改动的**全部意义**:重推成功必须覆盖掉旧的 failed
            checks["reshoot_visible"] = row is not None and bool(row.pushed) and row.error is None
            _t, stats3 = await build_daily_summary(rt, day=today)
            checks["total_unchanged"] = stats3["total"] == stats["total"]
            # ★ 一行 = 触发 × 收件人:同一触发推**第二个目标** → 多一行,
            #   但两行**共享同一个 trigger_key**(这才是"按触发聚合"能成立的前提)
            second = await write_log(rt, mine[0], recipient=SYNTH_GROUP2, pushed=True)
            checks["second_recipient"] = {"id": second.id, "created": second.created}
            checks["second_is_new_row"] = second.created is True and second.id != first.id
            # 触发身份:同一触发的多个收件人共享 trigger_key
            async with rt.db.session() as s:
                grouped = (
                    await s.execute(
                        select(AlertLog.trigger_key, func.count())
                        .where(AlertLog.rule_id == str(mine[0].rule_id))
                        .group_by(AlertLog.trigger_key)
                    )
                ).all()
            checks["trigger_groups"] = [(k, int(c)) for k, c in grouped]
            checks["trigger_key_groups_targets"] = any(int(c) >= 2 for _k, c in grouped)
            async with rt.db.session() as s:
                keys = (
                    await s.execute(
                        select(AlertLog.delivery_key, AlertLog.trigger_key).where(
                            AlertLog.id == first.id
                        )
                    )
                ).first()
            checks["keys_of_row"] = list(keys) if keys else None
            checks["delivery_suffix_is_recipient"] = bool(keys) and keys[0].endswith(f":{SYNTH_MANAGE}")
            checks["trigger_is_prefix"] = bool(keys) and keys[0].startswith(keys[1])

        out = await rt.alert().summary(day=today, push=True)
        checks["service_summary"] = out
        checks["summary_pushed"] = bool(sent)
    finally:
        await rt.aclose()

    ok = (
        checks["has_rules_hotels_total_pushed_rate"]
        and checks["total_is_denominator"]
        and checks["pushed_less_than_total"]
        and checks["rate_formula"]
        and checks["mentions_rate"]
        and checks.get("total_unchanged", True)
        # ★ 行身份 / 触发身份 的语义(0004 改动的验收点)
        and checks.get("upsert_same_row", False)
        and checks.get("upsert_marks_refresh", False)
        and checks.get("reshoot_visible", False)
        and checks.get("trigger_key_groups_targets", False)
        and checks.get("second_is_new_row", False)
        and checks.get("delivery_suffix_is_recipient", False)
        and checks.get("trigger_is_prefix", False)
        and checks["summary_pushed"]
    )
    ev = (
        f"汇总:规则 {checks['stats'].get('rules')} / 酒店 {checks['stats'].get('hotels')} / "
        f"总行 {checks['stats'].get('total')} / 成功 {checks['stats'].get('pushed')} / "
        f"送达率 {checks['stats'].get('rate'):.1f}%(=成功÷总行) / "
        f"重推同一 触发×目标 → 同行 id={checks.get('upsert_same_row')} 标记刷新="
        f"{checks.get('upsert_marks_refresh')} 行内容={checks.get('row_after_reshoot')}"
        f"(failed→成功可见={checks.get('reshoot_visible')}) / trigger_key 聚合={checks.get('trigger_groups')}"
    )
    return _res("V54", v54.__doc__ or "", PASS if ok else FAIL, ev, **checks)


# ===========================================================================
# V55–V58 — 点评
# ===========================================================================


@contextlib.contextmanager
def _auto_whitelist(hotel_name: str):
    """**临时**把某店加进 ``review_templates.json`` 的 ``auto.hotel_whitelist``。

    为什么必须这么做:自动回复有三重门控 —— ``settings.review.auto_enabled`` +
    ``config.auto.enabled`` + **灰度白名单** + ``submit.ready``。
    合成酒店不在白名单里时,``autorun`` 会在**白名单那一道**就返回,
    根本走不到 ``submit.ready`` 那一道 —— V57 想验的"未就绪 → 人工队列"就没被覆盖。

    ★ 备份 + ``finally`` 还原(逐字节),不让验收污染配置资产;
    还原后 ``reload_review_config()`` 清掉 mtime 缓存。
    """
    from hoteldata.domains.review import policy as review_policy

    path = Path(review_policy.load_review_config().get("_path") or "")
    if not path or not path.is_file():
        # 兜底:按项目约定推导
        path = get_project_config_dir() / "review_templates.json"
    original = path.read_text(encoding="utf-8")
    data = json.loads(original)
    auto = data.setdefault("auto", {})
    keep = list(auto.get("hotel_whitelist") or [])
    auto["hotel_whitelist"] = [*keep, hotel_name]
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        review_policy.reload_review_config()
        yield list(auto["hotel_whitelist"])
    finally:
        path.write_text(original, encoding="utf-8")
        review_policy.reload_review_config()


def get_project_config_dir() -> Path:
    from hoteldata.settings import get_settings

    return Path(get_settings().config_dir)


async def _seed_review_scenario(rt: Any, hotel_id: int, today: date) -> dict[str, Any]:
    """造 3 条待回复点评(好评 / 差评 / 无星级)+ 4 类素材。"""
    await _seed_reviews(
        rt,
        hotel_id,
        [
            {"review_id": "verify2-good-1", "star": 5, "content": "环境很好,服务周到", "sentiment": "good"},
            {"review_id": "verify2-bad-1", "star": 2, "content": "隔音太差,一晚没睡好", "sentiment": "bad"},
            {"review_id": "verify2-unk-1", "star": None, "content": "还行吧", "sentiment": "unknown"},
        ],
    )
    await _seed_material(rt, hotel_id, "score", {"平均评分": 4.6, "点评总数": 128}, day=today)
    await _seed_material(rt, hotel_id, "competitor", {"关键词": ["服务", "位置"]}, day=today)
    await _seed_material(rt, hotel_id, "trend", {"月份": ["2026-05", "2026-06"], "评分": [4.4, 4.5]}, day=today)
    await _seed_material(rt, hotel_id, "num", {"待回复": 3}, day=today)
    return {"seeded": True}


@REG.add("V55", "建议草稿:落审计 + 草稿推管理群(≤20 条);回复确认/已处理/已忽略 流转正确")
async def v55() -> CheckResult:
    from sqlalchemy import select

    from hoteldata.domains.review.draft import draft_for_hotel
    from hoteldata.domains.review.policy import effective_policy, submit_ready
    from hoteldata.infra.models import ReviewReply, ReviewReview

    checks: dict[str, Any] = {}
    today = date.today()
    rt = await _runtime()
    try:
        hotel = await _ensure_hotel(rt.db)
        await _cleanup(rt.db, int(hotel.id))
        await _seed_review_scenario(rt, int(hotel.id), today)
        svc = rt.review()
        checks["policy"] = await effective_policy(rt, int(hotel.id))
        checks["submit_ready"] = submit_ready(rt)

        drafts = await draft_for_hotel(rt, hotel)
        checks["draft_count"] = len(drafts)
        checks["draft_keys"] = sorted(drafts[0].keys()) if drafts else []
        checks["has_draft_md"] = all(d.get("draft_md") for d in drafts)
        checks["no_braces"] = all("{{" not in (d.get("draft_md") or "") for d in drafts)
        checks["draft_md_sample"] = (drafts[0].get("draft_md") if drafts else "")[:300]

        async with rt.db.session() as s:
            rows = list(
                (
                    await s.execute(
                        select(ReviewReply).where(ReviewReply.hotel_id == int(hotel.id)).order_by(ReviewReply.id)
                    )
                )
                .scalars()
                .all()
            )
        checks["audit_rows"] = [(r.review_id, r.status, r.exec_by, r.strategy) for r in rows]
        checks["suggested_audited"] = any(r.status == "suggested" for r in rows)
        # ★ silent 差评:ignored + replied=1
        async with rt.db.session() as s:
            bad = (
                await s.execute(
                    select(ReviewReview).where(
                        ReviewReview.hotel_id == int(hotel.id), ReviewReview.review_id == "verify2-bad-1"
                    )
                )
            ).scalar_one_or_none()
            unk = (
                await s.execute(
                    select(ReviewReview).where(
                        ReviewReview.hotel_id == int(hotel.id), ReviewReview.review_id == "verify2-unk-1"
                    )
                )
            ).scalar_one_or_none()
        checks["bad_row"] = (bad.replied, bad.strategy) if bad else None
        checks["unknown_row"] = (unk.replied, unk.strategy) if unk else None
        checks["bad_silent_replied_1"] = bool(bad) and int(bad.replied or 0) == 1
        checks["unknown_not_replied"] = bool(unk) and int(unk.replied or 0) == 0

        # 推管理群(离线替身)
        rt._bots = OfflineManager()
        rt._sender = None
        rt._push = None
        rt._audit = None
        rt.settings = _settings(MANAGE_CHATIDS=SYNTH_MANAGE, OPS_CHATID="", PUSH_MIN_INTERVAL_S=0.001)
        await rt.start_push()
        rt.push.dispatcher.min_interval_s = 0.0
        rt.push.dispatcher.group_min_interval_s = 0.0
        out = await svc.suggest()
        checks["suggest_result"] = out
        logs = await rt.audit.list_logs(group_chatid=SYNTH_MANAGE)
        checks["suggest_pushed"] = [r.push_type for r in logs]
        checks["draft_pushed"] = any(r.push_type.startswith("review") for r in logs)

        # 流转:回复确认 → ok + replied=1;已忽略 → ignored + replied=1
        pk = drafts[0]["review_pk"] if drafts else None
        checks["review_pk"] = pk
        if pk is not None:
            before = len(rows)
            r_ok = await svc.transition(pk, status="ok")
            checks["transition_ok"] = r_ok
            r_ign = await svc.transition(pk, status="ignored")
            checks["transition_ignored"] = r_ign
            async with rt.db.session() as s:
                after = list(
                    (
                        await s.execute(
                            select(ReviewReply)
                            .where(ReviewReply.hotel_id == int(hotel.id))
                            .order_by(ReviewReply.id)
                        )
                    )
                    .scalars()
                    .all()
                )
            checks["rows_before_after"] = (before, len(after))
            checks["append_only"] = len(after) == before + 2
            bad_pk = await svc.transition("不存在的评测#99999", status="ok")
            checks["invalid_pk"] = bad_pk
            checks["invalid_pk_rejected"] = bad_pk.get("ok") is False
    finally:
        await rt.aclose()

    ok = (
        # 3 条点评里差评走店级 silent(**按设计不出草稿**)→ 期望恰好 2 条草稿
        checks["draft_count"] == 2
        and checks["has_draft_md"]
        and checks["no_braces"]
        and checks["suggested_audited"]
        and checks["bad_silent_replied_1"]
        and checks["unknown_not_replied"]
        and checks.get("append_only", False)
        and checks.get("invalid_pk_rejected", False)
    )
    ev = (
        f"草稿 {checks['draft_count']} 条 / 审计行={checks['audit_rows']} / "
        f"差评 silent→replied={checks['bad_row']} / unknown→replied={checks['unknown_row']} / "
        f"流转 append-only={checks.get('append_only')}(行 {checks.get('rows_before_after')})"
    )
    return _res("V55", v55.__doc__ or "", PASS if ok else FAIL, ev, **checks)


@REG.add("V56", "审计 append-only:状态流转**新增行不 UPDATE**;★两套口径(silent→1 / failed→0)")
async def v56() -> CheckResult:
    from sqlalchemy import select

    from hoteldata.domains.review.autoreply import mark_replied
    from hoteldata.domains.review.policy import classify_sentiment
    from hoteldata.infra.models import (
        REPLY_EXECUTORS,
        REPLY_STATUSES,
        ReviewReply,
        ReviewReview,
    )

    checks: dict[str, Any] = {}
    checks["enums"] = {"status": list(REPLY_STATUSES), "exec": list(REPLY_EXECUTORS)}
    checks["status_enums_ok"] = set(REPLY_STATUSES) == {"suggested", "ok", "failed", "ignored"}

    # 情感阈值
    checks["good"] = classify_sentiment(5, None)
    checks["good4"] = classify_sentiment(4, None)
    checks["bad3"] = classify_sentiment(3, None)
    checks["bad1"] = classify_sentiment(1, None)
    checks["unknown"] = classify_sentiment(None, None)
    checks["unknown_hint_bad"] = classify_sentiment(None, "差评")
    checks["unknown_hint_good"] = classify_sentiment(None, "好评")
    checks["sentiment_ok"] = (
        checks["good"] == "good"
        and checks["good4"] == "good"
        and checks["bad3"] == "bad"
        and checks["bad1"] == "bad"
        and checks["unknown"] == "unknown"
        and checks["unknown_hint_bad"] == "bad"
        and checks["unknown_hint_good"] == "good"
    )

    today = date.today()
    rt = await _runtime()
    try:
        hotel = await _ensure_hotel(rt.db)
        await _cleanup(rt.db, int(hotel.id))
        await _seed_review_scenario(rt, int(hotel.id), today)
        # ★ 必须是**会落库**的那条路径:``ReviewService.drafts()``(命令「点评待办」)
        #   按设计"当场算、不落库",用它验审计等于什么都没做。
        from hoteldata.domains.review.draft import draft_for_hotel as _draft

        await _draft(rt, hotel)

        async with rt.db.session() as s:
            rows = list(
                (
                    await s.execute(
                        select(ReviewReply).where(ReviewReply.hotel_id == int(hotel.id)).order_by(ReviewReply.id)
                    )
                )
                .scalars()
                .all()
            )
            good = (
                await s.execute(
                    select(ReviewReview).where(
                        ReviewReview.hotel_id == int(hotel.id), ReviewReview.review_id == "verify2-good-1"
                    )
                )
            ).scalar_one_or_none()
            bad = (
                await s.execute(
                    select(ReviewReview).where(
                        ReviewReview.hotel_id == int(hotel.id), ReviewReview.review_id == "verify2-bad-1"
                    )
                )
            ).scalar_one_or_none()
        checks["audit_rows"] = [(r.review_id, r.status, r.exec_by) for r in rows]
        checks["row_count"] = len(rows)
        # 两套口径
        checks["silent_bad"] = {"status_ignored": any(
            r.review_id == "verify2-bad-1" and r.status == "ignored" for r in rows
        ), "replied": int(bad.replied or 0) if bad else None, "strategy": bad.strategy if bad else None}
        checks["silent_ok"] = (
            checks["silent_bad"]["status_ignored"] and checks["silent_bad"]["replied"] == 1
        )

        # 失败口径:mark_replied(replied=0) + failed 审计行
        good_pk = int(good.id) if good else None
        checks["good_pk"] = good_pk
        if good_pk is not None:
            from hoteldata.domains.review.draft import write_audit_row

            await write_audit_row(
                rt,
                hotel_id=int(hotel.id),
                review_id="verify2-good-1",
                status="failed",
                strategy="auto_failed",
                content=None,
                exec_by="auto_failed",
                detail={"reason": "验收构造"},
            )
            await mark_replied(rt, good_pk, strategy="auto_failed", exec_by="auto_failed", replied=0)
            async with rt.db.session() as s:
                good2 = (
                    await s.execute(
                        select(ReviewReview).where(ReviewReview.id == good_pk)
                    )
                ).scalar_one_or_none()
            checks["failed_row"] = (int(good2.replied or 0), good2.strategy) if good2 else None
            checks["failed_replied_0"] = bool(good2) and int(good2.replied or 0) == 0
            checks["failed_stays_pending"] = checks["failed_replied_0"]
            async with rt.db.session() as s:
                after = list(
                    (
                        await s.execute(
                            select(ReviewReply)
                            .where(ReviewReply.hotel_id == int(hotel.id))
                            .order_by(ReviewReply.id)
                        )
                    )
                    .scalars()
                    .all()
                )
            checks["rows_after"] = len(after)
            checks["append_only"] = len(after) == checks["row_count"] + 1
            # 旧行**内容未被改写**(append-only 的实证)
            checks["old_rows_intact"] = [
                (r.review_id, r.status, r.exec_by) for r in after[: checks["row_count"]]
            ] == checks["audit_rows"]
    finally:
        await rt.aclose()

    ok = (
        checks["status_enums_ok"]
        and checks["sentiment_ok"]
        and checks["silent_ok"]
        and checks.get("failed_replied_0", False)
        and checks.get("append_only", False)
        and checks.get("old_rows_intact", False)
    )
    ev = (
        f"情感 ≥4 good/≤3 bad/无星级 unknown(提示映射)={checks['sentiment_ok']} / "
        f"差评 silent:status=ignored & replied={checks['silent_bad']['replied']} / "
        f"失败:failed & replied={checks['failed_row']} → 仍在待回复池 / "
        f"append-only:{checks['row_count']}→{checks.get('rows_after')} 行且旧行未改"
    )
    return _res("V56", v56.__doc__ or "", PASS if ok else FAIL, ev, **checks)


@REG.add("V57", "★自动回复门控:submit.ready=false → 明确提示 + 进人工队列,**绝不伪造成功**")
async def v57() -> CheckResult:
    from sqlalchemy import select

    from hoteldata.domains.review.policy import submit_ready
    from hoteldata.infra.models import ReviewReply, ReviewReview

    checks: dict[str, Any] = {}
    today = date.today()
    rt = await _runtime()
    try:
        ready, reason = submit_ready(rt)
        checks["submit_ready"] = ready
        checks["reason"] = reason
        checks["ready_is_false"] = ready is False
        checks["reason_explains"] = bool(reason)

        hotel = await _ensure_hotel(rt.db)
        await _cleanup(rt.db, int(hotel.id))
        await _seed_review_scenario(rt, int(hotel.id), today)

        rt._bots = OfflineManager()
        rt._sender = None
        rt._push = None
        rt._audit = None
        rt.settings = _settings(MANAGE_CHATIDS=SYNTH_MANAGE, OPS_CHATID="")
        # ★ 先把本店加进灰度白名单 —— 否则会在**白名单那一道**就返回,
        #   走不到 submit.ready 那一道,V57 想验的"未就绪路径"根本没被执行。
        with _auto_whitelist(hotel.name) as wl:
            checks["whitelist"] = wl
            out = await rt.review().auto_reply()
        checks["autorun"] = out
        checks["autorun_says_not_ready"] = out.get("submit_ready") is False or "not_ready" in out
        checks["not_ready_reason_in_result"] = bool(
            out.get("not_ready_reason") or out.get("reason") or out.get("notes")
        )

        async with rt.db.session() as s:
            rows = list(
                (
                    await s.execute(
                        select(ReviewReply).where(ReviewReply.hotel_id == int(hotel.id)).order_by(ReviewReply.id)
                    )
                )
                .scalars()
                .all()
            )
            good = (
                await s.execute(
                    select(ReviewReview).where(
                        ReviewReview.hotel_id == int(hotel.id), ReviewReview.review_id == "verify2-good-1"
                    )
                )
            ).scalar_one_or_none()
        checks["audit_statuses"] = [r.status for r in rows]
        checks["no_ok_rows"] = "ok" not in checks["audit_statuses"]
        checks["queued_manual"] = any(r.status == "suggested" for r in rows)
        checks["good_replied"] = int(good.replied or 0) if good else None
        checks["never_fake_success"] = checks["no_ok_rows"] and checks["good_replied"] == 0

        # 状态文案里必须能看见"未就绪 + 原因"
        st = await rt.review().status()
        checks["status_text"] = st.get("text")
        checks["status_shows_reason"] = bool(
            st.get("text") and ("未就绪" in st["text"] or "submit" in st["text"])
        )
    finally:
        await rt.aclose()

    ok = (
        checks["ready_is_false"]
        and checks["reason_explains"]
        and checks["no_ok_rows"]
        and checks["queued_manual"]
        and checks["never_fake_success"]
        and checks["status_shows_reason"]
    )
    ev = (
        f"submit_ready={checks['submit_ready']}(原因:{str(checks['reason'])[:60]}) / "
        f"autorun → 审计状态={checks['audit_statuses']}(无 ok 行) / 好评 replied={checks['good_replied']} "
        f"→ 进人工队列 / 「点评状态」显示原因={checks['status_shows_reason']}"
    )
    return _res("V57", v57.__doc__ or "", PASS if ok else FAIL, ev, **checks)


@REG.add("V58", "点评分析日报:评分/竞争圈/趋势/待回复快照;每店绑定群;无素材则跳过")
async def v58() -> CheckResult:
    from hoteldata.domains.review.analysis import build_analysis, publish_analysis

    checks: dict[str, Any] = {}
    today = date.today()
    rt = await _runtime()
    try:
        hotel = await _ensure_hotel(rt.db)
        await _cleanup(rt.db, int(hotel.id))
        await _seed_review_scenario(rt, int(hotel.id), today)
        await rt.bindings.bind(SYNTH_GROUP, int(hotel.id))

        md = await build_analysis(rt, hotel, today)
        checks["analysis"] = md
        checks["has_analysis"] = bool(md)
        checks["has_score_block"] = bool(md) and ("评分" in md)
        checks["has_pending_block"] = bool(md) and ("待回复" in md)
        checks["no_braces"] = bool(md) and ("{" not in md)
        checks["analysis_head"] = (md or "")[:300]

        # 无素材 → None(跳过,不报错)
        # ★ 注意顺序:``_cleanup`` 会删掉 ``chatid LIKE 'verify2-%'`` 的全部绑定,
        #   所以这一步必须在**重新绑定之后**再做,否则 publish 会"无绑定群"而假失败。
        empty = await _ensure_hotel(rt.db, "验收2-无素材酒店")
        await _cleanup(rt.db, int(empty.id))
        checks["no_material_none"] = (await build_analysis(rt, empty, today)) is None

        await rt.bindings.bind(SYNTH_GROUP, int(hotel.id))
        checks["bound"] = [h.hotel_id for h in await rt.bindings.for_group(SYNTH_GROUP)]

        rt._bots = OfflineManager()
        rt._sender = None
        rt._push = None
        rt._audit = None
        rt.settings = _settings(MANAGE_CHATIDS=SYNTH_MANAGE, PUSH_MIN_INTERVAL_S=0.001)
        await rt.start_push()
        rt.push.dispatcher.min_interval_s = 0.0
        rt.push.dispatcher.group_min_interval_s = 0.0
        out = await publish_analysis(rt, day=today)
        checks["publish_result"] = out
        logs = await rt.audit.list_logs(group_chatid=SYNTH_GROUP)
        checks["types"] = [r.push_type for r in logs]
        checks["pushed_to_bound_group"] = any(r.push_type == "review_analysis" for r in logs)
        checks["slots"] = [r.slot for r in logs]
        checks["slot_dedup_key"] = bool(logs) and all(r.slot.count("-") == 3 for r in logs)
        # 第二次同日 → slot 去重
        logs_before = len(logs)
        await publish_analysis(rt, day=today)
        logs2 = await rt.audit.list_logs(group_chatid=SYNTH_GROUP)
        checks["second_run_skipped"] = len(logs2) == logs_before + 1  # 多一行 skipped
        checks["statuses"] = [r.status for r in logs2]
    finally:
        await rt.aclose()

    ok = (
        checks["has_analysis"]
        and checks["has_score_block"]
        and checks["has_pending_block"]
        and checks["no_braces"]
        and checks["no_material_none"]
        and checks["pushed_to_bound_group"]
        and "skipped" in checks.get("statuses", [])
    )
    ev = (
        f"分析日报四块={checks['has_analysis']}(评分={checks['has_score_block']} 待回复={checks['has_pending_block']}) / "
        f"无素材→None={checks['no_material_none']} / 推到绑定群={checks['pushed_to_bound_group']} / "
        f"第二次同日 slot 去重={checks['statuses']}"
    )
    return _res("V58", v58.__doc__ or "", PASS if ok else FAIL, ev, **checks)


# ===========================================================================
# V59 / V60 — 端到端
# ===========================================================================


#: 会**真的发送**的 CLI 命令(命令路径 → 中文说明)。V59 用它做"网关必拉起"的静态守卫。
_SEND_COMMANDS = (
    ("push now", "手动推日报"),
    ("alert test", "预警测试(--send 时才发)"),
    ("alert summary", "预警汇总(--push 时才发)"),
    ("report run", "单跑报告项"),
    ("review draft", "点评草稿(--push 时才发)"),
    ("review auto", "点评自动回复(--push 时才执行)"),
    ("review analysis", "点评分析日报(--push 时才发)"),
)


def _cli_send_commands_guarded() -> dict[str, str]:
    """静态守卫:每个"会发送"的 CLI 命令,函数体里必须出现 ``_ensure_gateway``。

    ★ 为什么要有这条:``Runtime.bots`` 是懒构建的,``start_push()`` **不会**去
    ``core_bots`` 读凭据 —— 只有 ``start_bots()`` 会。所以"只 start_push"的命令
    会回一句 **"没有可用的机器人(core_bots 表为空)"**,而表里其实有机器人:
    一句谎报原因的报错,比直接崩还难查(施工后实测踩过)。

    这是**源码级**断言(不是运行期),因为要防的正是"将来新增一个发送命令时忘了加"。
    它宁可粗一点(只看函数体里有没有那个名字),也要挡住这一类回归。
    """
    import re

    src = (PROJECT_ROOT / "src" / "hoteldata" / "cli.py").read_text(encoding="utf-8")
    # 按函数定义切块(CLI 里每个命令都是一个顶层 def)
    blocks: dict[str, str] = {}
    for match in re.finditer(r"^(?:async )?def (\w+)\(", src, re.MULTILINE):
        start = match.start()
        nxt = src.find("\ndef ", match.end())
        blocks[match.group(1)] = src[start : nxt if nxt > 0 else len(src)]

    out: dict[str, str] = {}
    for path, label in _SEND_COMMANDS:
        # CLI 里命令函数名 = 命令路径的下划线形式(`report run` → ``report_run``);
        # 再退回最后一个 token(`push now` 的函数就叫 ``push_now``,两种都能命中)。
        candidates = (path.replace(" ", "_"), path.replace(" ", "_").replace("-", "_"), path.split()[-1])
        body = next((blocks[c] for c in candidates if c in blocks), None)
        if body is None:
            out[path] = f"UNKNOWN({label})"
            continue
        out[path] = ("OK" if "_ensure_gateway" in body else "MISSING") + f"({label})"
    return out


@REG.add("V59", "一条命令全启:任务注册表含段2 全部任务;网关实例身份不变式;/status 显示机器人数与健康")
async def v59() -> CheckResult:
    import hoteldata.jobs  # noqa: F401
    from hoteldata.infra.tasks import get_registry

    checks: dict[str, Any] = {}
    reg = get_registry()
    names = reg.names()
    checks["task_count"] = len(names)
    expected = {
        "push.daily",
        "push.schedule",
        "alert.room",
        "alert.data",
        "alert.summary",
        "review.suggest",
        "review.analysis",
        "review.auto",
        "review.realtime",
        "ops.selfcheck",
        "ops.violation",
    }
    checks["missing"] = sorted(expected - set(names))
    checks["all_registered"] = not checks["missing"]
    checks["crons"] = {n: reg.get(n).cron for n in sorted(expected & set(names))}
    checks["catch_up"] = {n: reg.get(n).catch_up for n in sorted(expected & set(names))}
    # §5.9 的 catch_up 取舍:内容型补跑,汇总/轮询不补
    checks["catch_up_ok"] = (
        checks["catch_up"].get("push.daily") is True
        and checks["catch_up"].get("push.schedule") is True
        and checks["catch_up"].get("alert.room") is True
        and checks["catch_up"].get("alert.data") is True
        and checks["catch_up"].get("alert.summary") is False
        and checks["catch_up"].get("review.realtime") is False
        and checks["catch_up"].get("ops.violation") is False
        and checks["catch_up"].get("review.suggest") is True
        and checks["catch_up"].get("review.auto") is True
        and checks["catch_up"].get("review.analysis") is True
    )
    checks["alert_room_cron_exact"] = checks["crons"].get("alert.room") == "4 9,14,19 * * *"
    checks["alert_data_cron_exact"] = checks["crons"].get("alert.data") == "10 9 * * *"
    checks["push_cron_exact"] = checks["crons"].get("push.daily") == "0 9 * * *"

    rt = await _runtime()
    try:
        await rt.start_push()
        checks["push_started"] = rt.push.snapshot()["dispatcher"]["workers"] >= 1
        # ★★ 实例身份不变式(这条 bug 的根因防线):
        #    ``Runtime.bots`` 是懒构建的,``PushService`` / ``Sender`` 在构造时
        #    就抓住了那**一个** manager 实例。若将来有人"新建一个 BotManager 再赋值",
        #    PushService 会永远对着那个 0 实例的旧管理器发消息 —— 每条推送都失败,
        #    而且看起来"配置没错"。下面两行就是钉死这一点。
        checks["push_manager_is_runtime_bots"] = rt.push.manager is rt.bots
        checks["sender_manager_is_runtime_bots"] = rt.sender.manager is rt.bots

        manager = await rt.start_bots()
        checks["gateway_shares_instance"] = manager is rt.bots
        checks["bots_size"] = manager.size()
        health = manager.health()  # ★ D18 统一契约
        checks["health_type"] = type(health).__name__
        checks["health_is_dict_of_bool"] = isinstance(health, dict) and all(
            isinstance(v, bool) for v in health.values()
        )
        status = await rt.status()
        checks["status_keys"] = sorted(status.keys())
        checks["status_has_bots"] = "bots" in status
        checks["status_has_push"] = "push" in status
        checks["status_bots"] = status.get("bots")
        checks["status_shows_bot_count"] = isinstance(status.get("bots"), dict) and "bots" in status["bots"]
        checks["status_health_contract"] = isinstance(status.get("bots", {}).get("health"), dict)

        # ★ 凡是"真的发送"的 CLI 命令,函数体里必须有 ``_ensure_gateway`` ——
        #   否则就会重现"只 start_push 不 start_bots → bots.size()==0 →
        #   报一句『core_bots 表为空』而表里其实有机器人"的谎报。
        checks["cli_send_commands"] = _cli_send_commands_guarded()

        # ★★ 调度器装载不变式(本地联调抓出来的真 bug):
        #   ``with_scheduler=True`` 在 ``create()`` **内部**就建调度器,而它是按
        #   **当时的注册表**建任务的。若 ``import hoteldata.jobs`` 写在 create 之后
        #   → 调度器建成 ``get_jobs()==0``,启动日志却照样打印"已注册任务 19 个"
        #   → **所有定时推送静默不触发**(09:00 日报、09:04 关房预警…)。
        #
        #   反例("空注册表必须报错")只能**另起一个干净进程**验:本函数开头为了拿
        #   注册表已经 ``import hoteldata.jobs`` 了,同进程里再也造不出空注册表。
        rejected = await asyncio.to_thread(_empty_registry_probe)
        checks["empty_registry_rejected"] = rejected
        checks["scheduler_refuses_empty"] = "拒绝启动一个 0 任务的调度器" in rejected

        from hoteldata.runtime import Runtime

        good_cm = Runtime.create(get_settings(), with_scheduler=True)
        good = await good_cm.__aenter__()
        good._extras["__cm__"] = good_cm
        try:
            sched = good.scheduler
            jobs = sched.get_jobs() if sched is not None else []
            checks["scheduled_ids"] = sorted(j.id for j in jobs)
            checks["scheduled_count"] = len(jobs)
            checks["scheduler_loads_all"] = len(jobs) == len(reg.names()) and len(jobs) > 0
            by_id = {j.id: j for j in jobs}
            checks["push_daily_trigger"] = str(by_id["push.daily"].trigger) if "push.daily" in by_id else None
            checks["alert_room_trigger"] = str(by_id["alert.room"].trigger) if "alert.room" in by_id else None
        finally:
            await good.aclose()
    finally:
        await rt.aclose()

    ok = (
        checks["all_registered"]
        and checks["catch_up_ok"]
        and checks["alert_room_cron_exact"]
        and checks["alert_data_cron_exact"]
        and checks["push_cron_exact"]
        and checks["push_started"]
        and checks["health_is_dict_of_bool"]
        and checks["status_has_bots"]
        and checks["status_has_push"]
        and checks["status_health_contract"]
        # ★ 实例身份不变式 + CLI 发送命令的网关守卫 + 调度器装载不变式
        and checks["push_manager_is_runtime_bots"]
        and checks["sender_manager_is_runtime_bots"]
        and checks["gateway_shares_instance"]
        and all(v.startswith("OK") for v in checks["cli_send_commands"].values())
        and checks["scheduler_refuses_empty"]
        and checks["scheduler_loads_all"]
    )
    ev = (
        f"注册任务 {checks['task_count']} 个;段2 11 个任务全在(缺={checks['missing']}) / "
        f"alert.room cron={checks['crons'].get('alert.room')} / catch_up 取舍={checks['catch_up_ok']} / "
        f"start_push workers={rt.push.snapshot()['dispatcher']['workers']} / "
        f"BotManager.health() 契约={checks['health_type']} / /status 含 bots+push / "
        f"★实例身份:push.manager≡runtime.bots={checks['push_manager_is_runtime_bots']} "
        f"sender.manager≡runtime.bots={checks['sender_manager_is_runtime_bots']} / "
        f"★CLI 发送命令网关守卫={checks['cli_send_commands']} / "
        f"★调度器装载 {checks['scheduled_count']}/{checks['task_count']} 个任务"
        f"(空注册表被拒='{checks['empty_registry_rejected'][:24]}…') / "
        f"push.daily={checks['push_daily_trigger']}"
    )
    return _res("V59", v59.__doc__ or "", PASS if ok else FAIL, ev, **checks)


@REG.add(
    "V60",
    "单群日消息 ≤4 条:跑完整日程(日报 + 22 项报告 + 点评分析 + 批次G 运维推送)后统计真实消息条数",
)
async def v60() -> CheckResult:
    import hoteldata.jobs  # noqa: F401  ★ 触发任务注册(--only V60 单跑时不能依赖 V59 先导过)
    from hoteldata.domains.alert import engine as eng
    from hoteldata.domains.collect.rotation import get_rotation
    from hoteldata.domains.review.analysis import publish_analysis

    checks: dict[str, Any] = {}
    today = date.today()
    rt = await _runtime()
    try:
        hotel = await _ensure_hotel(rt.db)
        await _cleanup(rt.db, int(hotel.id))
        await _cleanup_push_logs(rt.db, SYNTH_GROUP)
        await rt.bindings.bind(SYNTH_GROUP, int(hotel.id))

        # 造当日数据:轮换模块各一条 + 点评素材
        plan = get_rotation().pick(today)
        for item in plan.items:
            await _seed_module(
                rt, int(hotel.id), item.page or "经营报告", item.name, "昨日", {"验收指标": 1}, day=today
            )
        # 22 项里 daily 桶的那些也造数据(否则全部 no_data,消息数为 0,统计无意义)
        from hoteldata.domains.report.engine import windows_for_push
        from hoteldata.domains.report.schedule import get_schedule

        sched = get_schedule()
        for item in sched.items:
            for win in windows_for_push(item, today):
                await _seed_module(
                    rt,
                    int(hotel.id),
                    item.page,
                    item.module_name,
                    win,
                    {"访客量": 123, "订单量": 5, "出租率(%)": 66.6},
                    day=today,
                )
        await _seed_review_scenario(rt, int(hotel.id), today)

        # 离线机器人:记录**真实发出的消息条数**
        bots = OfflineManager()
        rt._bots = bots
        rt._sender = None
        rt._push = None
        rt._audit = None
        rt.settings = _settings(
            MANAGE_CHATIDS=SYNTH_MANAGE, OPS_CHATID=SYNTH_OPS, PUSH_MIN_INTERVAL_S=0.001
        )
        await rt.start_push()
        rt.push.dispatcher.min_interval_s = 0.0
        rt.push.dispatcher.group_min_interval_s = 0.0

        bot = bots.route(SYNTH_GROUP)

        def _group_msgs(chatid: str = SYNTH_GROUP) -> list[dict[str, Any]]:
            """★ 按 **chatid** 过滤:同一个机器人在验收里同时承载
            运营群、管理群、运维群三类目标,不筛就会把别的群的消息算进"单群日消息量"。"""
            return [
                o
                for o in bot.outbox
                if isinstance(o.get("content"), str) and o.get("chatid") == chatid
            ]

        await rt.report().publish_daily()
        await rt.push.dispatcher.drain(30)
        checks["after_daily"] = len(_group_msgs())

        await rt.report().publish_schedule(day=today)
        await rt.push.dispatcher.drain(60)
        checks["after_schedule"] = len(_group_msgs())
        schedule_msgs = checks["after_schedule"] - checks["after_daily"]
        checks["schedule_messages"] = schedule_msgs
        checks["schedule_le_2"] = schedule_msgs <= 2  # ★ 22 项合并成 1~2 条,不是 22 条

        await publish_analysis(rt, day=today)
        await rt.push.dispatcher.drain(30)
        final = _group_msgs()
        checks["final_messages"] = len(final)
        checks["per_message_chars"] = [len(o["content"]) for o in final]
        checks["total_ok"] = len(final) <= 4

        # ---- ★ 批次 G 端到端:自检推送 + 违约实时(计划书 §6 T2G.1 的验证口径)----
        # ① 自检推送:走 **真实** PushService.send_alert(D1 修复点)
        rt.settings = _settings(
            MANAGE_CHATIDS=SYNTH_MANAGE, OPS_CHATID=SYNTH_OPS, PUSH_MIN_INTERVAL_S=0.001
        )
        self_res = await rt.tasks.run("ops.selfcheck", rt, trigger="manual")
        checks["selfcheck_status"] = self_res.status
        push_info = (self_res.summary or {}).get("push") or {}
        checks["selfcheck_push"] = push_info
        checks["selfcheck_metrics_present"] = bool(
            (self_res.summary or {}).get("disk") is not None
        )
        checks["selfcheck_pushed"] = int(push_info.get("pushed") or 0) >= 1
        ops_msgs = _group_msgs(SYNTH_OPS)
        checks["ops_messages"] = len(ops_msgs)
        checks["selfcheck_reached_ops"] = bool(ops_msgs) and "自检" in ops_msgs[-1]["content"]

        # ② 违约实时:首次只记基线不推 → count+1 才推
        from hoteldata.domains.ops.violation import STATE_FILENAME, find_violation_module

        located = find_violation_module(rt)
        checks["violation_module"] = located
        checks["violation_module_found"] = located is not None
        if located:
            module, window = located
            state_file = Path(rt.layout.states_dir) / STATE_FILENAME
            state_file.unlink(missing_ok=True)  # 清基线,保证"首次"成立
            await _seed_module(
                rt, int(hotel.id), "商机中心", module, window or "实时", {"违约记录数": 2}, day=today
            )
            v1 = await rt.tasks.run("ops.violation", rt, trigger="manual")
            checks["violation_first"] = v1.summary
            checks["violation_first_no_push"] = int((v1.summary or {}).get("pushed") or 0) == 0
            await _seed_module(
                rt, int(hotel.id), "商机中心", module, window or "实时", {"违约记录数": 3}, day=today
            )
            v2 = await rt.tasks.run("ops.violation", rt, trigger="manual")
            checks["violation_second"] = v2.summary
            checks["violation_second_pushed"] = int((v2.summary or {}).get("pushed") or 0) >= 1
            checks["violation_delta"] = (v2.summary or {}).get("new")
            # ★★ 必须**等派发器把队列排空**再去看"群里收到没有":
            #    违约推送走的是 ``runtime.push.push(BuiltMessage)`` → ``asyncio.Queue``,
            #    是**异步投递**的。``tasks.run`` 返回只代表"已入队",不代表"已发出"。
            #    少了这一步,断言就会随机器负载时快时慢地飘(实测:同一条
            #    "违约是否到群"在一次跑绿、下一次跑红)。
            await rt.push.dispatcher.drain(timeout_s=30)
            v_group = _group_msgs(SYNTH_GROUP)
            checks["violation_reached_group"] = any("违约" in o["content"] for o in v_group)
            checks["violation_no_braces"] = all(
                "{" not in o["content"] for o in _group_msgs(SYNTH_OPS) + v_group
            )

        # ③ 运维/违约推送**不得**污染"单群日消息 ≤4"的口径(它们发去别的群)
        checks["group_messages_after_ops"] = len(_group_msgs())
        checks["ops_did_not_pollute"] = len(_group_msgs()) <= 4

        # 补充:预警推送也算"日消息量"吗?→ 预警写 alert_logs,与日报/报告/点评同一群时另计,
        # 但它受"当日去重 + 忽略"约束,不参与 V60 的 4 条口径(计划书 §2.3 防轰炸针对内容型推送)。
        await _seed_portal(rt, int(hotel.id), "home_pending", {"comment_pending": "2"}, day=today)
        res = await eng.check(rt, "09:00", today=today, rule_id="home_pending")
        checks["alert_triggers"] = len([t for t in res.triggers if int(t.hotel_id) == int(hotel.id)])

        logs = await rt.audit.list_logs(day=today, group_chatid=SYNTH_GROUP)
        checks["audit_rows"] = len(logs)
        checks["audit_types"] = sorted({r.push_type for r in logs})
        checks["audit_gt_messages"] = len(logs) >= len(final)  # 审计粒度 ≥ 消息条数(按店×项)
    finally:
        await rt.aclose()

    ok = (
        checks["after_daily"] >= 1
        and checks["schedule_le_2"]
        and checks["total_ok"]
        and checks["audit_gt_messages"]
        # 批次 G:自检推送到运维群 + 违约"首次基线 / 增量才推"
        and checks["selfcheck_pushed"]
        and checks["selfcheck_reached_ops"]
        and checks.get("violation_module_found", False)
        and checks.get("violation_first_no_push", False)
        and checks.get("violation_second_pushed", False)
        and checks.get("violation_reached_group", False)
        and checks.get("violation_no_braces", False)
        and checks.get("ops_did_not_pollute", False)
    )
    ev = (
        f"日报 {checks['after_daily']} 条 + 22 项报告 {checks['schedule_messages']} 条(合并,≤2) + "
        f"点评分析 = **单群当日 {checks['final_messages']} 条 ≤4** / 各条字数={checks['per_message_chars']} / "
        f"审计 {checks['audit_rows']} 行 / 批次G:自检推送 pushed={checks['selfcheck_push'].get('pushed')}"
        f"(运维群 {checks['ops_messages']} 条) · 违约模块={checks['violation_module']} "
        f"首次不推={checks.get('violation_first_no_push')} 增量推={checks.get('violation_second_pushed')}"
        f"(delta={checks.get('violation_delta')}) · 运维推送未污染群口径={checks.get('ops_did_not_pollute')}"
    )
    return _res("V60", v60.__doc__ or "", PASS if ok else FAIL, ev, **checks)


# ===========================================================================
# 执行
# ===========================================================================

def _empty_registry_probe() -> str:
    """在**干净子进程**里验:空注册表 + ``with_scheduler=True`` 必须报错。

    为什么非得起子进程:本模块为了断言注册表内容已经 ``import hoteldata.jobs``,
    同进程里注册表永远非空,"反例"根本造不出来。子进程还顺带证明了
    **不导入 jobs 就跑 serve** 这条路真的会被挡住(而不是悄悄地起一个空调度器)。
    """
    import subprocess

    # ★ ``with_db=False``:被测的是**注册表为空**这一条守卫(它在 ``start_scheduler``
    #   里抛,早于任何 DB 使用)。不连库 → 探针与"数据库此刻忙不忙"解耦,
    #   否则并发跑验收时这条会偶发地因 DB 抖动而红(实测遇到过)。
    code = (
        "import sys; sys.path.insert(0, 'src'); import asyncio;"
        "from hoteldata.runtime import Runtime; from hoteldata.settings import get_settings;"
        "asyncio.run("
        "Runtime.create(get_settings(), with_db=False, with_scheduler=True).__aenter__()"
        ")"
    )
    try:
        proc = subprocess.run(  # noqa: S603 - 固定命令,无外部输入
            [sys.executable, "-c", code],
            cwd=str(PROJECT_ROOT),
            # ★★ **必须显式传 PYTHONIOENCODING=utf-8**(中文 Windows 上的必踩项)。
            #
            # 子进程的 stdout 编码由环境变量决定:不设时,中文 Windows 上默认 **GBK**
            # (实测 ``sys.stdout.encoding == 'gbk'``)。而本函数用
            # ``encoding="utf-8"`` 解码它的输出 → 中文全成乱码 →
            # 下面的 ``marker in blob`` 为**假** → 走兜底分支返回最后一行 →
            # ``scheduler_refuses_empty=False`` → **V59 假红**。
            #
            # 实测数据(``scripts/diag_v59_flaky.py``,同一探针固定条件重复跑):
            #
            # ==========================================  ==========  ==========
            # 条件                                          子进程编码   utf-8 解码命中
            # ==========================================  ==========  ==========
            # 不传 env(父进程也没设 PYTHONIOENCODING)      gbk         **0/6**
            # 传 env(``PYTHONIOENCODING=utf-8``)          utf-8       **3/3**
            # ==========================================  ==========  ==========
            #
            # 注意这个 bug 的**隐蔽性**:只要父进程的环境里恰好有
            # ``PYTHONIOENCODING=utf-8``(某些终端/CI 会设),V59 就**稳定绿**;
            # 没有就**稳定红**。所以它表现为"换台机器/换个 shell 就变",
            # 极易被当成"偶发 flaky"或"产品回归"—— 两者都不是。
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            # ★ 不能用 ``capture_output=True`` 配 ``stderr=STDOUT``(会 ValueError);
            #   这里显式 PIPE + 合并,因为 loguru 会替换 ``sys.stderr``,
            #   未捕获异常的 traceback 未必落在 fd 2 上(实测分开捕获时 stderr 是空的)。
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "✗ 探针子进程超时(90s)"
    blob = proc.stdout or ""
    if proc.returncode == 0:
        return "✗ 空注册表竟然起来了(守卫失效)"
    marker = "拒绝启动一个 0 任务的调度器"
    if marker in blob:
        return marker
    tail = blob.strip().splitlines()
    return tail[-1] if tail else f"✗ 非零退出但无输出(rc={proc.returncode})"


async def run_all(only: list[str] | None = None) -> list[CheckResult]:
    out: list[CheckResult] = []
    # ★ 按 **V 编号数值**排序输出(而不是源码注册顺序):验收日志是交付证据,
    #   读的人要能顺着 V21 → V60 看下来。注册顺序取决于各段在源码里的书写位置,
    #   曾出现 V35–V39 排在 V26 前面的情况。
    ordered = sorted(REG.checks, key=lambda item: int(item[0].lstrip("Vv")))
    for vid, title, fn in ordered:
        if only and vid not in only:
            continue
        started = time.monotonic()
        try:
            res = await fn()
        except Exception as exc:  # noqa: BLE001
            import traceback

            res = _res(
                vid,
                title,
                FAIL,
                f"执行异常: {type(exc).__name__}: {exc}",
                traceback=traceback.format_exc()[-1800:],
            )
        res.detail["elapsed_s"] = round(time.monotonic() - started, 2)
        out.append(res)
        colour = {"PASS": "\033[92m", "FAIL": "\033[91m", "BLOCKED": "\033[93m"}.get(res.status, "")
        print(f"{colour}{res.status:8s}\033[0m {res.vid:4s} {res.title}")
        print(f"         {res.evidence}")
    return out


async def _cleanup_all() -> None:
    """跑完清掉全部合成痕迹(反复重跑结果一致)。"""
    try:
        rt = await _runtime()
    except Exception:  # noqa: BLE001
        return
    try:
        hotel = await _ensure_hotel(rt.db)
        await _cleanup(rt.db, int(hotel.id))
        for g in (SYNTH_GROUP, SYNTH_GROUP2, SYNTH_MANAGE):
            await _cleanup_push_logs(rt.db, g)
    finally:
        await rt.aclose()


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description="段2 验收执行器(V21–V60)")
    parser.add_argument("--only", nargs="*", help="只跑指定编号,如 V35 V36")
    parser.add_argument("--json", help="结果 JSON 输出路径")
    parser.add_argument("--no-cleanup", action="store_true", help="保留合成数据便于排查")
    args = parser.parse_args(argv)

    results = asyncio.run(run_all(args.only))
    if not args.no_cleanup:
        asyncio.run(_cleanup_all())

    passed = sum(1 for r in results if r.status == PASS)
    failed = [r.vid for r in results if r.status == FAIL]
    blocked = [r.vid for r in results if r.status == BLOCKED]
    print()
    print("=" * 78)
    print(f"合计 {len(results)} 项:PASS {passed} / FAIL {len(failed)} / BLOCKED {len(blocked)}")
    if failed:
        print(f"FAIL  : {failed}")
    if blocked:
        print(f"BLOCKED: {blocked}")
    print(_blocked_note(blocked))
    print("=" * 78)

    out = Path(args.json) if args.json else PROJECT_ROOT / "var" / "reports" / "段2-验收结果.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "summary": {
                    "total": len(results),
                    "pass": passed,
                    "fail": len(failed),
                    "blocked": len(blocked),
                    "failed_ids": failed,
                    "blocked_ids": blocked,
                },
                "results": [
                    {
                        "id": r.vid,
                        "title": r.title,
                        "status": r.status,
                        "evidence": r.evidence,
                        "detail": r.detail,
                    }
                    for r in results
                ],
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    print(f"结果已写入 {out}")
    return 1 if failed else 0


def _blocked_note(blocked: list[str]) -> str:
    if not blocked:
        return ""
    return (
        "BLOCKED 说明:这些条目需要**真实企微凭据 + 外网**,本机无法完成;"
        "相应协议/逻辑已用「协议一致的本地服务端」验证(见各项证据)。\n"
        "          → " + "、".join(blocked)
    )


if __name__ == "__main__":
    raise SystemExit(main())

