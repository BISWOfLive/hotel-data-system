"""采集表:``collect_reports``(页面级 + 截图回填目标) 与 ``collect_modules``(★ 核心产出)。

对应旧库 ``collect_reports`` / ``module_records``。

★ 关键变化:
  - ``payload_json`` 旧系统是 **TEXT**,这里改 **JSONB**(旧库 12 列 JSON 存成 TEXT,
    过滤靠 ``LIKE``)。
  - ``collect_modules`` 五列唯一约束(旧库**早已存在**,不是新增改进)。
  - 🚫 **不再接受 ``ebooking_reports`` 复活** —— 旧系统 ``storage/db.py:37`` 的
    ``ReportStore`` 被 ``app/knowledge.py:73-77`` 调用,一旦 FAQ 路径执行就会
    ``CREATE TABLE IF NOT EXISTS ebooking_reports`` 把已退役的表建回主库(D5)。
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Date,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, created_at_column

#: ``channel`` 合法值(旧系统实际只写这四种;``auto`` 这个"开关"在代码里根本不存在)
EXTRACT_CHANNELS = ("api", "browser", "screenshot", "fullpage")

#: 四态(``no_data`` **不算失败**)
EXTRACT_STATUSES = ("ok", "degraded", "no_data", "failed")


class CollectReport(Base):
    """页面级采集记录 + 截图回填目标。

    ``module_screenshots_json`` 的**键名必须与旧系统完全一致**,否则段2 取不到图
    (段1 T5.4 红线)。

    ``raw_json_path`` / ``html_path`` 存**相对路径**(A19 遗产:`collectors/layout.py`
    的 ``to_relative`` / ``from_relative`` 语义),绝对路径换机器即失效。
    """

    __tablename__ = "collect_reports"
    __table_args__ = (UniqueConstraint("hotel_id", "collect_date", "page", name="uq_collect_reports_key"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    hotel_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("core_hotels.id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("core_accounts.id", ondelete="SET NULL")
    )
    collect_date: Mapped[date] = mapped_column(Date, nullable=False)
    page: Mapped[str] = mapped_column(String(128), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)  # api|browser|screenshot|fullpage
    indicators_json: Mapped[dict | None] = mapped_column(JSONB)
    modules_json: Mapped[dict | None] = mapped_column(JSONB)
    module_screenshots_json: Mapped[dict | None] = mapped_column(JSONB)
    screenshot_path: Mapped[str | None] = mapped_column(Text)
    raw_json_path: Mapped[str | None] = mapped_column(Text)
    html_path: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_column()


class CollectModule(Base):
    """★ 模块级结构化数据 —— 段1 的核心产出,段2 报告引擎的数据源。

    唯一键 ``(hotel_id, collect_date, page, module, window)``:
    同日重跑即 **UPSERT 覆盖**(幂等),行数不增。
    """

    __tablename__ = "collect_modules"
    __table_args__ = (
        UniqueConstraint(
            "hotel_id",
            "collect_date",
            "page",
            "module",
            "window",
            name="uq_collect_modules_key",
        ),
        Index("ix_collect_modules_lookup", "hotel_id", "collect_date", "page", "module"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    hotel_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("core_hotels.id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("core_accounts.id", ondelete="SET NULL")
    )
    collect_date: Mapped[date] = mapped_column(Date, nullable=False)
    page: Mapped[str] = mapped_column(String(128), nullable=False)
    module: Mapped[str] = mapped_column(String(256), nullable=False)
    window: Mapped[str] = mapped_column(String(64), nullable=False)
    #: ★ JSONB(旧系统 TEXT)。``payload`` 结构:{label: value} 或 {"fields": {...}, ...}
    payload_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    raw_json_path: Mapped[str | None] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(String(32), nullable=False, server_default="api")
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="ok")
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_column()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"<CollectModule {self.collect_date} {self.page}/{self.module}/{self.window} "
            f"{self.channel}:{self.status}>"
        )


__all__ = [
    "EXTRACT_CHANNELS",
    "EXTRACT_STATUSES",
    "CollectModule",
    "CollectReport",
]
