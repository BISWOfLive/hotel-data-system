"""发送原语(T2A.5)—— 文本 / 图片 / 长文拆分。

三条硬口径
==========

① **单条消息 ≤ 3500 字,超限"按店对半拆"为 ≤2 条**(A2-6)。
   日报支持一群多店,多店段用 ``\\n\\n\\n`` 连接(A2-5)。超限时**在店边界上拆**
   —— 绝不从一句话中间切断(那是把 markdown 表格劈成两半的最快方式)。

② **每群每次附图上限 5 张**(A2-4)。超出直接截断并**记 warning**
   (静默丢图是"截了但没发"这类故障的温床)。

③ **图片逐张发**(A1-13:企微**没有 news 卡片消息类型**),
   文本一条 + 图片 N 条,顺序固定(文本在前)。

★ ``media_count`` 记**实际发出**的张数,不是计划张数。
旧系统审计里写的是 ``len(images)`` 计划值,所以"图缺失被静默跳过"在审计里
完全看不出来 —— 这与 V41「缺图要告知」是同一个病根的两种表现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from hoteldata.domains.bot.client import BotClient
from hoteldata.domains.bot.manager import BotManager
from hoteldata.domains.bot.uploader import MediaUploader

__all__ = [
    "Delivery",
    "Sender",
    "merge_sections",
    "split_message",
]

#: 多店段之间的连接符(A2-5 逐字:三个换行)
SECTION_SEP = "\n\n\n"


def merge_sections(sections: list[str]) -> str:
    """多店内容合并成 **1 条**消息(A2-5)。"""
    return SECTION_SEP.join(s for s in sections if s)


def split_message(content: str, *, limit: int = 3500, max_parts: int = 2) -> list[str]:
    """长文拆分(A2-6:目标 ≤3500 字/条,超限**按店对半拆 ≤2 条,不截断不丢内容**)。

    ★ **"≤3500"是目标,"≤2 条"与"不丢内容"是硬约束** —— 三者不可能同时满足时,
    牺牲的是字数上限:

      * 内容 8709 字 + 6 个店段 → 2 条各 ~4350 字(**超过 3500,但两条都在、内容全在**);
      * 若为了凑 3500 而拆成 3 条,就直接违反 V46 的"≤2 条";
      * 若为了凑 3500 而截断,就违反 V25 的"不截断、不丢内容"。

    旧系统 ``report_push._merge_kept`` 的取舍与此一致(按店对半分两半,不管字数),
    这里只是把"分得尽量均匀"补上 —— 旧实现按**段数**对半,一段长一段短时会一边撑爆。

    算法:
      1. ``len(content) <= limit`` → 原样一条;
      2. 否则按 :data:`SECTION_SEP` 切段(**店边界**),用
         :func:`_balanced_cut` 均分成 ``min(max_parts, ceil(总长/limit))`` 组;
      3. 只有一个段(没有店边界可言)时,退化为**文本均分**,切点优先落在换行处。

    ★ 返回值长度**恒 ``<= max_parts``**(V25/V46 都断言这一条)。
    """
    if len(content) <= limit:
        return [content]

    sections = content.split(SECTION_SEP)
    wanted = min(max_parts, max(1, -(-len(content) // limit)))
    if len(sections) > 1:
        groups = _balanced_cut(sections, wanted)
        return [SECTION_SEP.join(g) for g in groups]

    # 单段:没有店边界,只能按文本均分
    return _split_text(content, wanted, limit=limit)


def _balanced_cut(sections: list[str], parts: int) -> list[list[str]]:
    """把 ``sections`` **连续**均分成 ``parts`` 组(尽量等长)。

    连续(而不是交叉)是必须的:段的顺序 = 店的顺序,打乱会让
    "「A 店」的指标表"和"「B 店」的标题行"拼在一起(串台,P9)。
    """
    if parts <= 1 or len(sections) <= 1:
        return [list(sections)]
    total = sum(len(s) for s in sections) + len(SECTION_SEP) * (len(sections) - 1)
    target = total / parts
    groups: list[list[str]] = []
    cur: list[str] = []
    cur_len = 0
    remaining_groups = parts
    for idx, sec in enumerate(sections):
        extra = len(sec) + (len(SECTION_SEP) if cur else 0)
        left_sections = len(sections) - idx
        # 还能开新组、且当前组已够长、且剩下的段够填满剩余组 → 收口
        if (
            cur
            and len(groups) < parts - 1
            and cur_len + extra > target
            and left_sections >= (remaining_groups - 1)
        ):
            groups.append(cur)
            remaining_groups -= 1
            cur, cur_len = [], 0
            extra = len(sec)
        cur.append(sec)
        cur_len += extra
    if cur:
        groups.append(cur)
    return groups


def _split_text(text: str, parts: int, *, limit: int) -> list[str]:
    """无店边界时的文本均分:切点优先落在换行处(不劈开一行表格)。"""
    if parts <= 1:
        return [text]
    size = -(-len(text) // parts)
    out: list[str] = []
    rest = text
    while rest and len(out) < parts - 1:
        cut = min(size, len(rest))
        nl = rest.rfind("\n", 0, min(cut + 200, len(rest)))
        if nl > size // 2:
            cut = nl
        out.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    if rest:
        out.append(rest)
    _ = limit  # limit 只用于"是否需要拆"的判断,已在调用方完成
    return out


@dataclass(slots=True)
class Delivery:
    """一次群投递的结果(派发器据此写审计)。"""

    ok: bool = False
    bot_id: str = ""
    parts: int = 0
    media_count: int = 0
    images_sent: list[str] = field(default_factory=list)
    error: str | None = None
    attempts: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "bot_id": self.bot_id,
            "parts": self.parts,
            "media_count": self.media_count,
            "images": self.images_sent,
            "error": self.error,
            "attempts": self.attempts,
        }


class Sender:
    """发送原语。**不含限频、不含重试** —— 那是 :mod:`hoteldata.push.dispatcher` 的职责。"""

    def __init__(
        self,
        manager: BotManager | None,
        *,
        layout: Any = None,
        uploader: MediaUploader | None = None,
        max_images: int = 5,
        limit_chars: int = 3500,
    ) -> None:
        self.manager = manager
        self.layout = layout
        self.uploader = uploader or MediaUploader()
        self.max_images = int(max_images)
        self.limit_chars = int(limit_chars)

    # ------------------------------------------------------------------
    # 路径换算
    # ------------------------------------------------------------------

    def resolve(self, image: str | Path) -> Path:
        """图片路径归一:相对路径按 ``Layout.from_relative`` 换算(A19 遗产)。"""
        p = Path(image)
        if p.is_absolute():
            return p
        if self.layout is not None:
            return Path(self.layout.from_relative(str(image)))
        return p

    # ------------------------------------------------------------------
    # 文本
    # ------------------------------------------------------------------

    async def send_text(self, chatid: str, content: str) -> tuple[BotClient, int]:
        """给群发文本(自动按 3500 字拆分)。返回 ``(机器人, 实际发出的条数)``。"""
        bot = self._bot_for(chatid)
        parts = split_message(content, limit=self.limit_chars)
        for part in parts:
            await bot.send_markdown(chatid, part)
        if len(parts) > 1:
            logger.info("群 {} 长文本拆为 {} 条(共 {} 字)", chatid, len(parts), len(content))
        return bot, len(parts)

    async def send_images(self, chatid: str, images: list[str] | tuple[str, ...]) -> tuple[BotClient, list[str]]:
        """逐张发图(A1-13),返回**实际发出**的图片列表。"""
        bot = self._bot_for(chatid)
        sent: list[str] = []
        picked = list(images)[: self.max_images]
        if len(images) > self.max_images:
            logger.warning(
                "群 {} 附图 {} 张超过上限 {},已截断(A2-4)", chatid, len(images), self.max_images
            )
        for raw in picked:
            path = self.resolve(raw)
            if not path.exists():
                logger.warning("推送图缺失,跳过: {}", raw)
                continue
            try:
                media_id = await self.uploader.upload(bot, path)
                await bot.send_media(chatid, "image", media_id)
                sent.append(str(raw))
            except Exception as exc:  # noqa: BLE001 - 单张失败不影响其余图与文本
                logger.error("群 {} 图片发送失败({}): {}", chatid, raw, exc)
        return bot, sent

    # ------------------------------------------------------------------
    # 组合投递
    # ------------------------------------------------------------------

    async def deliver(self, chatid: str, content: str, images: list[str] | tuple[str, ...] = ()) -> Delivery:
        """文本 + 图片一起投(文本在前)。**异常不吞**,交给派发器决定重试。"""
        if self.manager is None or self.manager.size() == 0:
            return Delivery(ok=False, error="没有可用的机器人(core_bots 表为空)")
        bot = self._bot_for(chatid)
        parts = split_message(content, limit=self.limit_chars)
        for part in parts:
            await bot.send_markdown(chatid, part)
        sent: list[str] = []
        if images:
            _, sent = await self.send_images(chatid, images)
        return Delivery(
            ok=True,
            bot_id=bot.name,
            parts=len(parts),
            media_count=len(sent),
            images_sent=sent,
        )

    # ------------------------------------------------------------------
    # 回复(入站帧)
    # ------------------------------------------------------------------

    async def reply_text(self, bot: BotClient | None, frame: dict[str, Any], content: str) -> bool:
        """回复文本流。★ **回填入站 ``req_id``**(否则群内看不到回复)。"""
        if bot is None:
            logger.warning("回复失败:无可用机器人")
            return False
        parts = split_message(content, limit=self.limit_chars)
        for idx, part in enumerate(parts):
            await bot.reply_stream(frame, part, finish=idx == len(parts) - 1)
        return True

    async def reply_images(
        self, bot: BotClient | None, frame: dict[str, Any], images: list[str] | tuple[str, ...]
    ) -> int:
        """回复图片(逐张)。返回实际发出张数。"""
        if bot is None or not images:
            return 0
        sent = 0
        for raw in list(images)[: self.max_images]:
            path = self.resolve(raw)
            if not path.exists():
                logger.warning("回复图缺失,跳过: {}", raw)
                continue
            try:
                media_id = await self.uploader.upload(bot, path)
                await bot.reply_media(frame, "image", media_id)
                sent += 1
            except Exception as exc:  # noqa: BLE001
                logger.error("回复图片失败({}): {}", raw, exc)
        return sent

    async def reply_welcome(self, bot: BotClient | None, frame: dict[str, Any], content: str) -> bool:
        """会话欢迎语(A1-11:须在 ``enter_chat`` 后 5 秒内,所以**只发一条、不重试**)。"""
        if bot is None:
            return False
        await bot.reply_welcome(frame, content)
        return True

    # ------------------------------------------------------------------

    def _bot_for(self, chatid: str) -> BotClient:
        assert self.manager is not None
        bot = self.manager.route(chatid)
        if bot is None:  # pragma: no cover - deliver 已挡在前面
            raise RuntimeError("没有可用的机器人(core_bots 表为空)")
        return bot

    def stats(self) -> dict[str, Any]:
        return {"uploader": self.uploader.stats(), "max_images": self.max_images}
