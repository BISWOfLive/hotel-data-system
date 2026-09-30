"""核心实体表:``core_accounts`` / ``core_hotels`` / ``sessions``。

对应旧库 ``accounts`` / ``hotels``,以及**新增**的 ``sessions``(登录态唯一索引)。

★ 关键变化(总纲 7.4 / 段1 §5.10):
  - ``accounts.storage_state_path`` **移出** —— 路径由 ``(platform, role, alias)``
    推导为 ``var/states/<platform>__<role>__<alias>.json``,**不存第二来源**。
    旧系统「四类登录态、四种存法、三个位置」的病根就在这里。
  - 所有时间列统一 ``timestamptz``(带时区),日期列用 ``date``。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, created_at_column

# ---------------------------------------------------------------------------
# ① core_accounts —— 账号库(供段2 后台复用)
# ---------------------------------------------------------------------------


class Account(Base):
    """平台账号。

    ``status`` 三态**继承旧系统实锤**(``storage/db.py:171``、``manage.py:137-138``):
    ``active`` / ``pending_login`` / ``blocked``。

    ⚠️ 注意区分:这三态是**账号的长期状态**;而登录管家单次登录动作的**结果码**
    (旧 ``login_manager.py:36-44`` 的 ``ok / captcha / manual_required / failed /
    timeout / blocked``)是另一个概念,落在 ``ops_login_events.detail`` 里,
    **不要混进本列**。
    """

    __tablename__ = "core_accounts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    alias: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)  # ctrip | meituan
    username_enc: Mapped[str] = mapped_column(Text, nullable=False)
    password_enc: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="active")
    is_multi: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    remark: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_column()

    hotels: Mapped[list[Hotel]] = relationship(back_populates="account")

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Account {self.alias} platform={self.platform} status={self.status}>"


# ---------------------------------------------------------------------------
# ② core_hotels
# ---------------------------------------------------------------------------


class Hotel(Base):
    """酒店。``ebk_hotel_id`` 是携程 eBooking 的酒店 ID(比价锚点直达用)。"""

    __tablename__ = "core_hotels"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    city: Mapped[str | None] = mapped_column(String(128))
    account_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("core_accounts.id", ondelete="SET NULL")
    )
    ebk_hotel_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="active")
    #: ★ 店级点评策略(段2 批次 F)。旧库 ``hotels.review_policy`` 是 TEXT 存 JSON,
    #: 群命令「点评策略 <店> 差评 silent|template」写这里。
    #: 策略优先级:本列(群命令写) > ``review_templates.json`` 的 ``overrides`` > 默认。
    review_policy: Mapped[dict | None] = mapped_column(
        JSONB, comment='{"good": "g01", "bad": "silent"|"template", "bad_template": "b01"}'
    )
    created_at: Mapped[datetime] = created_at_column()

    account: Mapped[Account | None] = relationship(back_populates="hotels")

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Hotel {self.name} account_id={self.account_id}>"


# ---------------------------------------------------------------------------
# ③ sessions —— ★ 登录态唯一索引(取代 storage_state_path 与散落文件)
# ---------------------------------------------------------------------------


class Session(Base):
    """登录态登记表。

    **不存 ``state_path``** —— 路径由 ``(platform, role, alias)`` 推导,避免第二来源。
    文件实体在 ``var/states/<platform>__<role>__<alias>.json``。

    ``role`` 取值:``ebooking``(携程商家后台) / ``merchant``(美团商家后台) /
    ``ota``(携程前台比价) / ``ota_meituan``(美团前台比价)。

    ``status`` 四态(长期会话状态,与「单次登录动作结果码」是两回事):
      - ``unknown`` 未探测
      - ``valid``   有效
      - ``stale``   有效但临近过期(需主动续登)
      - ``invalid`` 失效(需重登)
    """

    __tablename__ = "sessions"
    __table_args__ = (UniqueConstraint("platform", "role", "alias", name="uq_sessions_key"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    alias: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="unknown")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.platform, self.role, self.alias)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Session {self.platform}/{self.role}/{self.alias} status={self.status}>"


__all__ = ["Account", "Hotel", "Session"]
