"""推送共享层的三张表:``core_bots`` / ``core_group_bindings`` / ``push_logs``。

段2 批次 A(T2A.7 / T2A.8)。对应旧库 ``bots`` / ``group_bindings`` / ``push_logs``。

★ 三处**有意差异**(都是修旧系统的已实测缺陷,不是风格偏好)
================================================================

① ``push_logs.bot_id`` 旧库是 ``INTEGER`` 而代码写的是**机器人名字符串**
   (旧 ``pusher.py`` 的 ``bot_id=(getattr(bot, "bot_name", None) or "legacy")``)
   → SQLite 动态类型不报错,但审计数据不可用,**无法按机器人统计**。
   这里直接建 ``Text``(D17 修复;V38 专门验"类型正确")。

② ``push_logs`` 新增 **``slot`` 列**(``YYYY-MM-DD-HH``)。
   旧系统去重靠 ``substr(pushed_at,1,10)`` + ``substr(pushed_at,12,2)`` **两次字符串切片**,
   既慢又依赖 ``pushed_at`` 的格式恒定。新库把时段键**显式落列**并建索引,
   去重查询变成一次等值比较。

③ ``core_group_bindings`` 保留 ``UNIQUE(chatid, hotel_id)``(旧库本来就有,
   靠 ``INSERT OR IGNORE`` 保证幂等)并**新增** ``paused`` —— 旧系统把"暂停"
   塞在 ``hotels.status='paused'`` 上,导致"暂停某店的日报"会连带停掉该店全部业务。
   本表把"群×店"这一条绑定关系自己的暂停态独立出来。

**外键与删除**:``core_group_bindings.hotel_id`` 用 ``CASCADE``(酒店没了,绑定无意义);
``push_logs.hotel_id`` 用 ``SET NULL`` —— **审计行永不删**,酒店删除后仍要能查历史推送。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, created_at_column

__all__ = [
    "BOT_STATUSES",
    "PUSH_LOG_STATUSES",
    "Bot",
    "GroupBinding",
    "PushLog",
]

#: ``core_bots.status`` 取值(与 ``core_accounts.status`` 同构,但语义独立)
BOT_STATUSES = ("active", "paused")

#: ``push_logs.status`` 取值
#: ``skipped`` = 当日 slot 已推过(去重命中),**不是失败**,但仍要留痕
#: (旧系统的去重是"静默跳过",查不到任何记录 —— 这正是"静默失败"的温床)。
PUSH_LOG_STATUSES = ("ok", "failed", "skipped")


class Bot(Base):
    """企业微信智能机器人(多实例)。

    凭据密文由 :mod:`hoteldata.infra.crypto` 的 Fernet 单例加解密,
    **明文永不落库**。

    ``capacity_per_bot`` 默认 10(甲方口径:30 机器人分摊 300 群);
    超出**只告警不硬拦**(旧系统语义,继承)。
    """

    __tablename__ = "core_bots"
    __table_args__ = (Index("ix_core_bots_status", "status"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    bot_id_enc: Mapped[str] = mapped_column(Text, nullable=False)
    secret_enc: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="active")
    capacity_per_bot: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="10", comment="建议承载群数;超出只告警不硬拦"
    )
    remark: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_column()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Bot {self.name} status={self.status}>"


class GroupBinding(Base):
    """群 ↔ 酒店绑定(支持一群多店)。

    ``UNIQUE(chatid, hotel_id)`` 保证 ``绑定`` 命令**幂等**(重复绑定不报错、不重复);
    ``paused`` 是**这条绑定自己的**暂停态,与酒店业务状态解耦。
    """

    __tablename__ = "core_group_bindings"
    __table_args__ = (
        UniqueConstraint("chatid", "hotel_id", name="uq_core_group_bindings_key"),
        Index("ix_core_group_bindings_chatid", "chatid"),
        Index("ix_core_group_bindings_hotel", "hotel_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chatid: Mapped[str] = mapped_column(String(128), nullable=False, comment="企微群 chatid")
    hotel_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("core_hotels.id", ondelete="CASCADE"), nullable=False
    )
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = created_at_column()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<GroupBinding {self.chatid} -> hotel {self.hotel_id} paused={self.paused}>"


class PushLog(Base):
    """推送审计(每条 = 一次「群 × 店 × 推送类型」的投递结果)。

    ★ **告警不走这张表** —— 预警写 ``alert_logs``(语义分离,A2-8)。
    理由:预警的送达率口径(成功行 ÷ 总行,**无管理群也要计入分母**)
    与普通推送的"成败"不是一回事,混在一张表里会让两边都算不清。

    ``slot`` 是**去重时段键** ``YYYY-MM-DD-HH``:同一 ``(群, 店, push_type, slot)``
    已有 ``status='ok'`` 行 → 跳过(写一行 ``skipped`` 留痕);``force`` 可绕。

    ``media_count`` 记**实际发出**的图片数(不是计划数)——
    旧系统这里记的是 ``len(images)`` 计划值,缺图静默丢时审计看不出来。
    """

    __tablename__ = "push_logs"
    __table_args__ = (
        Index("ix_push_logs_slot", "group_chatid", "hotel_id", "push_type", "slot"),
        Index("ix_push_logs_day", "slot"),
        Index("ix_push_logs_type", "push_type", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    hotel_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("core_hotels.id", ondelete="SET NULL"), comment="审计不随酒店删除而消失"
    )
    group_chatid: Mapped[str] = mapped_column(String(128), nullable=False)
    #: ★ **Text**(D17 修复):旧库是 INTEGER 而代码写机器人名字符串。
    bot_id: Mapped[str] = mapped_column(Text, nullable=False, comment="机器人名(文本,D17 修复)")
    push_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="daily_report / module_{id} / review_analysis / price_compare / ops_alert …",
    )
    content_preview: Mapped[str | None] = mapped_column(Text, comment="内容前 200 字(便于人工核对)")
    media_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0", comment="实际发出图片数")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="ok / failed / skipped(去重命中)"
    )
    error: Mapped[str | None] = mapped_column(Text)
    images_json: Mapped[list | None] = mapped_column(JSONB, comment="实际发出的图片相对路径列表")
    slot: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="去重时段键 YYYY-MM-DD-HH"
    )
    pushed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = created_at_column()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"<PushLog {self.slot} {self.group_chatid} h{self.hotel_id} "
            f"{self.push_type} {self.status} by={self.bot_id}>"
        )
