"""``review_replies`` —— 点评回复审计(**append-only**)。

段2 批次 F(T2F.2 / T2F.3)。对应旧库 ``review_replies``。

★★ **append-only 是硬纪律,不是风格**
=====================================

旧系统 ``review_reply.py`` 的状态流转**只 INSERT,从不 UPDATE**。
新库把这条纪律写进表结构语义:

  1. 状态流转时**新增一行**,不 UPDATE 旧行 → 全量可追溯"谁在什么时候把
     哪条点评从什么状态改成了什么状态";
  2. 推送前**先落审计行**再投递 → 崩溃时至少留下"打算做什么"的证据
     (旧系统同样如此)。

★★ **两套口径必须分清**(总纲 §3.6 / 附录 D)
============================================

================================  ==================  ================  ============
情况                              ``status``          ``reviews.replied``  还算待回复?
================================  ==================  ================  ============
差评店级 ``silent``               ``ignored``         **1**             否
自动回复失败                      ``failed``          **0**             **是**(留在池里)
================================  ==================  ================  ============

> 这不是笔误:``silent`` 是**业务决定"不回复"**,语义上等于已处理完;
> ``failed`` 是**技术失败**,点评仍在等待处理。
> V56 专门验这两条口径;把 ``failed`` 也标 ``replied=1`` 会让点评永远消失。

★ ``review_id`` 是平台 ``commentId``(缺失时为内容指纹 ``h+sha1[:16]``),
**不是** ``review_reviews.id`` 主键 —— 平台 id 才是跨系统可核对的那个。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, created_at_column

__all__ = [
    "REPLY_EXECUTORS",
    "REPLY_STATUSES",
    "ReviewReply",
]

#: ``review_replies.status`` 取值(append-only 的状态机,每次流转新增一行)
#:   - ``suggested`` 建议草稿已生成(仅建议模式)
#:   - ``ok``        已确认/已自动回复成功
#:   - ``failed``    自动回复失败 → **仍在待回复池**(``reviews.replied=0``)
#:   - ``ignored``   确认不回复(店级 silent,或人工「已忽略」)
REPLY_STATUSES = ("suggested", "ok", "failed", "ignored")

#: ``review_replies.exec_by`` 取值
REPLY_EXECUTORS = ("draft", "auto", "auto_failed", "manage", "silent")


class ReviewReply(Base):
    """点评回复审计行(**append-only,不 UPDATE**)。"""

    __tablename__ = "review_replies"
    __table_args__ = (
        Index("ix_review_replies_review", "hotel_id", "review_id", "id"),
        Index("ix_review_replies_status", "hotel_id", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    hotel_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("core_hotels.id", ondelete="CASCADE"), nullable=False
    )
    review_id: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="平台 commentId(或内容指纹 h+sha1[:16])"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="suggested / ok / failed / ignored"
    )
    strategy: Mapped[str | None] = mapped_column(
        String(64), comment="模板 id(g01/b01/b03)/ silent / auto_failed"
    )
    content: Mapped[str | None] = mapped_column(Text, comment="实际回复内容(草稿或已发出)")
    exec_by: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default="draft",
        comment="draft / auto / auto_failed / manage / silent",
    )
    detail_json: Mapped[dict | None] = mapped_column(
        JSONB, comment="补充证据:失败原因 / 平台回执 / 情感与星级快照"
    )
    created_at: Mapped[datetime] = created_at_column()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"<ReviewReply h{self.hotel_id} {self.review_id} {self.status} "
            f"strategy={self.strategy} by={self.exec_by}>"
        )
