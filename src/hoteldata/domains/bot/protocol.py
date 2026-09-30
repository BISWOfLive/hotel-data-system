"""★★ 企微智能机器人**协议层** —— A1 级遗产,**逐字节继承,禁止改动**(段2 §4.1)。

为什么单独立一个文件(P1 对策)
==============================

企微的这套协议是**逆向得来的**(移植自官方 ``@wecom/aibot-node-sdk``),
没有公开文档可查。**错一个字段名就断连**,而且断连时报的是模糊的 ack 错误。

段2 的风险表 P1 给出的对策第一条就是:**协议集中在一个 ``protocol.py``,改一处**。
所以本模块是全项目**唯一**知道帧长什么样的地方:

  * :mod:`hoteldata.domains.bot.client` 只管"连接/收发/重连"这些**传输**动作;
  * :mod:`hoteldata.domains.bot.manager` 只管"多实例路由/健康";
  * 业务域完全不碰帧。

A1 级常量清单(逐字对照段2 §4.1)
================================

======================================  ==========================================
资产                                     值
======================================  ==========================================
A1-1 WS 地址                              ``wss://openws.work.weixin.qq.com``
A1-2 帧格式                               ``{"cmd", "headers": {"req_id"}, "body"}``
A1-3 ``req_id``                           ``{cmd}_{毫秒}_{6位随机}``
A1-4 **10 个命令字**                      ``CMD_*``(见下)
A1-5 认证                                 ``aibot_subscribe`` + ``{bot_id, secret}``,
                                          超时 10s,校验 ``ack.errcode == 0``
A1-6 **ack 匹配**                         ack 帧**无 cmd**,按 ``headers.req_id``
                                          唤醒挂起方;迟到回执按前缀**静默忽略**
A1-7 心跳                                 ``ping`` / ``body=null`` / 间隔 30s /
                                          单次超时 6s / **连丢 2 次判死**
A1-9 收帧                                 ``settimeout(20)``;**先滤控制字符(保留 ``\\n``)
                                          再 ``json.loads``**
A1-13 图文限制                             **无 news 卡片类型** → 图文 = md 文本 +
                                          **逐张**图片消息
A1-14 素材上传                             ``init{type,filename,total_size,total_chunks,md5}``
                                          → ``body.upload_id``;
                                          ``chunk{upload_id,chunk_index,base64_data}``
                                          **512KB/片、>100 片报错**;
                                          ``finish{upload_id}`` → ``body.media_id``
A1-15 多机器人路由                        ``md5(chatid).hexdigest()[:8] % n``
                                          → ``sorted(names)[idx]``
A1-16 机器人容量                          ``capacity_per_bot=10``(仅告警,不硬拦)
======================================  ==========================================

★ **收帧为什么必须先滤控制字符**:企微服务端偶发在 JSON 里掺入 ``\\x00``-``\\x1f``
的原始控制字节,``json.loads`` 会直接抛。旧系统 (``aibot.py:143``) 的解法是

.. code-block:: python

    cleaned = "".join(ch for ch in raw if ch >= " " or ch == "\\n")

—— 注意 ``ch >= " "`` 已经把 ``\\n`` 也滤掉了,所以**必须显式保留 ``\\n``**,
否则多行 markdown 会被压成一行。本模块逐字复刻这一行。

★ **为什么必须有"迟到回执静默忽略"**:``_send_wait`` 超时后会从 ``_pending``
里摘掉自己,但服务端的 ack 可能**在超时之后**才到。若不按 ``req_id`` 前缀识别,
这一帧会被当成"未知帧"刷 warning 日志 —— 一晚上能刷几万行。
"""

from __future__ import annotations

import json
import random
import time
from typing import Any

__all__ = [
    "ACK_TIMEOUT_S",
    "CHUNK_SIZE",
    "CMD_CALLBACK",
    "CMD_EVENT_CALLBACK",
    "CMD_HEARTBEAT",
    "CMD_RESPONSE",
    "CMD_RESPONSE_WELCOME",
    "CMD_SEND_MSG",
    "CMD_SUBSCRIBE",
    "CMD_UPLOAD_CHUNK",
    "CMD_UPLOAD_FINISH",
    "CMD_UPLOAD_INIT",
    "COMMANDS",
    "HEARTBEAT_INTERVAL_S",
    "HEARTBEAT_TIMEOUT_S",
    "KNOWN_ACK_PREFIXES",
    "MAX_CHUNKS",
    "MAX_MISSED_PONG",
    "RECV_TIMEOUT_S",
    "SUBSCRIBE_TIMEOUT_S",
    "WS_URL",
    "AiBotError",
    "build_frame",
    "cmd_of",
    "is_ack",
    "new_stream_id",
    "parse_frame",
    "AiBotError",
    "build_frame",
    "clean_raw",
    "cmd_of",
    "dumps_frame",
    "errcode_of",
    "errmsg_of",
    "is_ack",
    "is_late_ack",
    "new_stream_id",
    "parse_frame",
    "raise_for_errcode",
    "req_id",
    "req_id_of",
]

# ---------------------------------------------------------------------------
# A1-1 / A1-7 / A1-14:常量(逐字继承,不许调整)
# ---------------------------------------------------------------------------

#: A1-1 WS 地址
WS_URL = "wss://openws.work.weixin.qq.com"

#: A1-4 命令字(10 个,一个不多一个不少)
CMD_SUBSCRIBE = "aibot_subscribe"
CMD_HEARTBEAT = "ping"
CMD_RESPONSE = "aibot_respond_msg"
CMD_RESPONSE_WELCOME = "aibot_respond_welcome_msg"
CMD_SEND_MSG = "aibot_send_msg"
CMD_UPLOAD_INIT = "aibot_upload_media_init"
CMD_UPLOAD_CHUNK = "aibot_upload_media_chunk"
CMD_UPLOAD_FINISH = "aibot_upload_media_finish"
CMD_CALLBACK = "aibot_msg_callback"
CMD_EVENT_CALLBACK = "aibot_event_callback"

#: 10 个命令字(顺序与官方 SDK 一致)
COMMANDS: tuple[str, ...] = (
    CMD_SUBSCRIBE,
    CMD_HEARTBEAT,
    CMD_RESPONSE,
    CMD_RESPONSE_WELCOME,
    CMD_SEND_MSG,
    CMD_UPLOAD_INIT,
    CMD_UPLOAD_CHUNK,
    CMD_UPLOAD_FINISH,
    CMD_CALLBACK,
    CMD_EVENT_CALLBACK,
)

#: A1-7 心跳:间隔 30 秒,单次超时 6 秒,连丢 2 次判死
HEARTBEAT_INTERVAL_S = 30.0
HEARTBEAT_TIMEOUT_S = 6.0
MAX_MISSED_PONG = 2

#: A1-5 订阅超时 10 秒
SUBSCRIBE_TIMEOUT_S = 10.0

#: A1-9 收帧超时 20 秒
RECV_TIMEOUT_S = 20.0

#: 一般 ack 等待超时(素材上传等;订阅与心跳各用自己的超时)
ACK_TIMEOUT_S = 15.0

#: A1-14 素材分片 512KB / 上限 100 片
CHUNK_SIZE = 512 * 1024
MAX_CHUNKS = 100

#: A1-6 迟到回执识别用的**命令前缀表**
#: (只含"会等 ack"的命令;``aibot_msg_callback`` / ``aibot_event_callback`` 是入站帧,不算)
KNOWN_ACK_PREFIXES: tuple[str, ...] = (
    CMD_SUBSCRIBE,
    CMD_HEARTBEAT,
    CMD_RESPONSE,
    CMD_RESPONSE_WELCOME,
    CMD_SEND_MSG,
    CMD_UPLOAD_INIT,
    CMD_UPLOAD_CHUNK,
    CMD_UPLOAD_FINISH,
)


class AiBotError(RuntimeError):
    """企微协议层错误(回执错误 / 超时 / 发送失败)。

    旧系统是 ``class AiBotError(Exception): pass``;新实现继承 ``RuntimeError``
    以便与"参数错误"(``ValueError``)区分。
    """


# ---------------------------------------------------------------------------
# A1-3 req_id
# ---------------------------------------------------------------------------


def req_id(cmd: str) -> str:
    """A1-3:``{cmd}_{毫秒}_{6位随机}``。

    .. code-block:: python

        f"{cmd}_{int(time.time() * 1000)}_{random.randint(100000, 999999)}"

    6 位随机的取值区间是 ``[100000, 999999]`` —— **不是** ``[0, 999999]``,
    所以永远是 6 位数。这一条旧系统如此,照抄。
    """
    return f"{cmd}_{int(time.time() * 1000)}_{random.randint(100000, 999999)}"


def new_stream_id() -> str:
    """流式回复的 ``stream.id``(旧系统用 ``_req_id("stream")``)。"""
    return req_id("stream")


# ---------------------------------------------------------------------------
# A1-2 帧编解码
# ---------------------------------------------------------------------------


def build_frame(cmd: str, body: Any, *, rid: str | None = None) -> dict[str, Any]:
    """A1-2:构造出站帧 ``{"cmd", "headers": {"req_id"}, "body"}``。"""
    return {"cmd": cmd, "headers": {"req_id": rid or req_id(cmd)}, "body": body}


def dumps_frame(frame: dict[str, Any]) -> str:
    """出站帧 → 文本(``ensure_ascii=False`` 保留中文,便于抓包核对)。"""
    return json.dumps(frame, ensure_ascii=False)


def clean_raw(raw: str) -> str:
    """A1-9 收帧预处理:**滤控制字符,但保留 ``\\n``**。

    逐字复刻旧 ``aibot.py:143``::

        cleaned = "".join(ch for ch in raw if ch >= " " or ch == "\\n")

    为什么 ``or ch == "\\n"`` 不能省:``"\\n"`` 的码点是 ``0x0A < 0x20``,
    ``ch >= " "`` 会把它一并滤掉。服务端在 JSON 里用 ``\\n`` **转义**换行,
    所以正常帧不受影响;但**原始换行**会出现在
    (a) 帧之间的空白,(b) 平台偶发多行拼接 —— 把它们压掉会让
    "本该失败的帧"变成"能解析但语义错的帧",比直接报错更难查。

    旧系统把这行内联在收帧循环里,新实现**抽成函数**只有一个目的:
    **可以被验收直接断言**(V21),不然这条 A 级约束只能靠读代码相信。
    """
    return "".join(ch for ch in raw if ch >= " " or ch == "\n")


def parse_frame(raw: str | bytes) -> dict[str, Any] | None:
    """A1-9:收帧预处理 → dict;解析失败返回 ``None``(调用方记日志,不抛)。

    ★ **先滤控制字符(保留 ``\\n``)再 ``json.loads``** —— 见 :func:`clean_raw`。
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        frame = json.loads(clean_raw(raw))
    except (ValueError, TypeError):
        return None
    return frame if isinstance(frame, dict) else None


# ---------------------------------------------------------------------------
# A1-6 帧判定
# ---------------------------------------------------------------------------


def cmd_of(frame: dict[str, Any]) -> str:
    """帧的 ``cmd``;ack 帧没有 ``cmd`` → 返回空串。"""
    value = frame.get("cmd")
    return value if isinstance(value, str) else ""


def req_id_of(frame: dict[str, Any]) -> str:
    """帧的 ``headers.req_id``;缺失 → 空串。"""
    headers = frame.get("headers")
    if not isinstance(headers, dict):
        return ""
    value = headers.get("req_id")
    return value if isinstance(value, str) else ""


def is_ack(frame: dict[str, Any]) -> bool:
    """ack 帧判定:**没有 ``cmd``**(A1-6)。

    ``errcode`` 在**顶层**(``{"errcode": 0, "errmsg": "", "headers": {...}}``),
    不在 ``body`` 里 —— 这一点与直觉相反,照抄旧系统。
    """
    return not cmd_of(frame)


def is_late_ack(frame: dict[str, Any]) -> bool:
    """迟到回执判定(A1-6):无 ``cmd`` 且 ``req_id`` 前缀是我们发过的命令。

    命中 → **静默忽略**(``logger.debug``),不算"未知帧"。
    """
    if not is_ack(frame):
        return False
    rid = req_id_of(frame)
    return bool(rid) and rid.startswith(KNOWN_ACK_PREFIXES)


def errcode_of(frame: dict[str, Any]) -> int:
    """帧的 ``errcode``(缺失按 ``0`` 处理 —— 有些 ack 只回 ``errmsg``)。"""
    value = frame.get("errcode")
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def errmsg_of(frame: dict[str, Any]) -> str:
    """帧的 ``errmsg``(缺失回退 ``body.errmsg``,再退回整帧字符串)。"""
    value = frame.get("errmsg")
    if isinstance(value, str) and value:
        return value
    body = frame.get("body")
    if isinstance(body, dict):
        inner = body.get("errmsg")
        if isinstance(inner, str) and inner:
            return inner
    return str(frame)[:200]


def raise_for_errcode(frame: dict[str, Any], cmd: str) -> dict[str, Any]:
    """``errcode != 0`` → 抛 :class:`AiBotError`(A1-5 的 ``ack.errcode == 0`` 校验)。"""
    code = errcode_of(frame)
    if code != 0:
        raise AiBotError(f"{cmd} 回执错误 {code}: {errmsg_of(frame)}")
    return frame


def body_of(frame: dict[str, Any]) -> dict[str, Any]:
    """``body`` 归一成 dict(缺失/非 dict → 空 dict)。"""
    body = frame.get("body")
    return body if isinstance(body, dict) else {}


def text_content_of(frame: dict[str, Any]) -> str:
    """入站消息帧的正文:``body.text.content``(非文本消息 → 空串)。"""
    body = body_of(frame)
    if body.get("msgtype") != "text":
        return ""
    text = body.get("text")
    if not isinstance(text, dict):
        return ""
    value = text.get("content")
    return value if isinstance(value, str) else ""


def chatid_of(frame: dict[str, Any]) -> str | None:
    """入站帧的群 ``chatid``。

    ★ 必须试三个键名(``chatid`` / ``chat_id`` / ``chatId``)—— 旧系统
    (``app/__init__.py:114`` 与 ``app/pusher.py:84``)**两处各写了一遍**这个循环,
    且两处顺序一致。私聊消息三个键**都没有** → 返回 ``None``
    (命令链据此回退到问答链,V28)。
    """
    body = body_of(frame)
    for key in ("chatid", "chat_id", "chatId"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def userid_of(frame: dict[str, Any]) -> str | None:
    """入站帧的 ``body.from.userid``。"""
    sender = body_of(frame).get("from")
    if not isinstance(sender, dict):
        return None
    value = sender.get("userid")
    return value if isinstance(value, str) and value else None


def event_type_of(frame: dict[str, Any]) -> str:
    """事件帧的 ``body.event.eventtype``(如 ``enter_chat``)。"""
    event = body_of(frame).get("event")
    if not isinstance(event, dict):
        return ""
    value = event.get("eventtype")
    return value if isinstance(value, str) else ""


# ---------------------------------------------------------------------------
# 出站消息体构造(A1-10 / A1-11 / A1-12 / A1-13)
# ---------------------------------------------------------------------------


def stream_reply_body(content: str, *, finish: bool = True, stream_id: str | None = None) -> dict[str, Any]:
    """A1-10 回复体:``{"msgtype":"stream","stream":{"id","finish","content"}}``。"""
    return {
        "msgtype": "stream",
        "stream": {
            "id": stream_id or new_stream_id(),
            "finish": finish,
            "content": content,
        },
    }


def text_body(content: str) -> dict[str, Any]:
    """A1-11 欢迎语体:``{"msgtype":"text","text":{"content"}}``。"""
    return {"msgtype": "text", "text": {"content": content}}


def markdown_body(content: str) -> dict[str, Any]:
    """A1-12 主动推送体:``{"msgtype":"markdown","markdown":{"content"}}``。"""
    return {"msgtype": "markdown", "markdown": {"content": content}}


def media_body(media_type: str, media_id: str) -> dict[str, Any]:
    """A1-13 图片/文件体(★ **没有 news 卡片类型**,图文只能用这个逐张发)。"""
    return {"msgtype": media_type, media_type: {"media_id": media_id}}
