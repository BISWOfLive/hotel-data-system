"""段3 比价域模型(迁移 ``0005_segment3_compare``)。

三张表,对应旧系统 ``db/hotel_prices.db`` 的两张表 + 比价目标清单:

=========================================  ==========================  ==========================================
新表                                        旧表 / 旧载体                关键变化
=========================================  ==========================  ==========================================
:class:`CmpPriceComparison`  ``cmp_price_comparisons``   ``hotel_price_comparisons``  **+ UNIQUE(含 slot)**、**+ ``distance_km`` 真填**、+ ``price_scope``、+ ``price_rejected``、+ ``is_demo``、+ ``hotel_id``
:class:`CmpBatchRun`         ``cmp_batch_runs``          ``hotel_batch_runs``         **每日一行 + UPSERT**(旧系统同日 2 条 ``done``)
:class:`CmpPriceTarget`      ``cmp_price_targets``        ``config/compare_hotels.txt``  **txt → 表**(总纲 §7.3 修正)
=========================================  ==========================  ==========================================

★ D8 实测(旧库只读导出,见 ``docs/参考/段3-分析/_证据-旧系统比价库dump.txt``)
================================================================================

* ``hotel_price_comparisons`` **18 行 / 8 个唯一组合** → 在 ``(anchor_name, query_date, nights)`` 上
  **6 组重复、冗余 10 行**;其中 ``(隐欲民宿, 2026-08-26)`` 有 **3 条**;
* ``hotel_batch_runs`` **同日多条 ``done``**:08-25 有 **5 条**、08-26 有 **2 条**;
* 根因 ==== ``pusher.py:568`` 的 ``run_price_collect`` **硬编码 ``force=True``**
  —— 判重 SQL 本身是对的(模拟 ``exists_today`` 3/3 命中),但**每次都被绕过**;
  且两张表**都没有 UNIQUE 兜底**(``storage/prices.py:88-106`` 是 SELECT-then-INSERT)。

所以段3 的三条约束必须**写进 DDL 而不是写进代码纪律**(代码纪律会被绕过):

1. ``cmp_price_comparisons`` 唯一键**含 ``query_slot``** → 同日多次采集各留一份(V79),
   同 slot 重跑**幂等**(V78);
2. ``cmp_batch_runs`` 唯一键是 ``batch_date`` → 每日一行,尝试次数进 ``attempt``,
   **尝试级历史交给段1 的 ``job_runs``**(职责分离,不丢信息);
3. ``is_demo`` 是**显式列** —— 旧 ``exists_today`` 用
   ``payload NOT LIKE '%"source": "演示"%'`` 这种 LIKE 过滤(脆弱),段3 不再靠猜。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, created_at_column

__all__ = [
    "BATCH_STATUSES",
    "DEFAULT_NIGHTS",
    "TARGET_MODE_BOTH",
    "TARGET_MODES",
    "CmpBatchRun",
    "CmpPriceComparison",
    "CmpPriceTarget",
]

#: 批量状态(旧 ``hotel_batch_runs.status``:running | done | failed)
BATCH_STATUSES: tuple[str, ...] = ("running", "done", "failed")

#: 比价目标模式(**取代旧系统的两个 txt**:``compare_hotels.txt`` + ``price_targets.txt``)
#:
#: * ``batch`` —— 每日 01:00 批量清单(旧 ``compare_hotels.txt``);
#: * ``cron``  —— 08:30/13:30/17:30 定时采集(旧 ``price_targets.txt``);
#: * ``both``  —— ★ **两处都列了这家店**(实测旧清单里确实有这样的店)。
#:   它不是"新概念",而是"旧系统两个 txt 的交集"的显式化 ——
#:   含义是「既在批量清单、也在定时采集清单」,两个任务都要选到它。
TARGET_MODES: tuple[str, ...] = ("batch", "cron", "both")

#: ``both`` 的字面量(避免散落的魔法字符串)
TARGET_MODE_BOTH = "both"

DEFAULT_NIGHTS = 1


class CmpPriceComparison(Base):
    """一行 = **一家酒店在一个平台上的一次报价**。

    ★ 为什么唯一键含 ``room_type`` 与 ``query_slot``
    ==============================================

    * ``query_slot``(``YYYY-MM-DD-HHMM``)—— **同一天多次采集各留一份**,
      这样才看得出"价格一天怎么变"(V79);而同一 slot 重跑幂等(V78);
    * ``room_type`` —— 段3 当前只取列表页**起价**(``price_scope="from"``,``room_type=NULL``),
      但唯一键里预留它,将来做房型粒度时**不用再改约束**(改约束要重跑去重)。

    ``NULL`` 在 PG 的 UNIQUE 里**不相等** —— 所以 ``room_type=NULL`` 的多行
    **不会**被误判为重复。这正是这里能安全预留的原因。
    """

    __tablename__ = "cmp_price_comparisons"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    #: ★ 关联 ``core_hotels``(可空:目标清单里的酒店不一定已登记)。
    #: 旧表只有 ``anchor_name`` 文本 → 酒店改名即失联;这里补上 id。
    hotel_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("core_hotels.id", ondelete="SET NULL")
    )
    anchor_name: Mapped[str] = mapped_column(String(256), nullable=False)
    city: Mapped[str | None] = mapped_column(String(128))
    platform: Mapped[str] = mapped_column(String(32), nullable=False)  # ctrip | meituan

    query_date: Mapped[date] = mapped_column(Date, nullable=False)
    #: ``YYYY-MM-DD-HHMM`` —— 一天多次各留一份的依据
    query_slot: Mapped[str] = mapped_column(String(32), nullable=False)
    nights: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))

    hotel_name: Mapped[str] = mapped_column(String(256), nullable=False)
    room_type: Mapped[str | None] = mapped_column(String(128))
    price: Mapped[float | None] = mapped_column(Numeric(10, 2))
    #: ★ ``numeric(8,3)``:``6,3`` 只到 999.999 km,城市内比价够用但**跨城兜底会溢出**;
    #: 8 位给到 99999.999 km,任何真实距离都不会溢出。
    distance_km: Mapped[float | None] = mapped_column(Numeric(8, 3))
    #: ``api`` | ``card`` | ``city`` | ``none``(见 ``contract.CoordSource``)
    coord_source: Mapped[str | None] = mapped_column(String(16))
    #: ``api`` | ``dom`` | ``vision`` | ``manual``
    price_source: Mapped[str | None] = mapped_column(String(16))
    #: ★ ``from``(列表页起价)/ ``exact``(确定价)—— 旧系统无此概念
    price_scope: Mapped[str] = mapped_column(String(8), nullable=False, server_default=text("'from'"))
    need_manual_check: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    degraded: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    #: ★ 被券价过滤器丢掉的候选原文(丢弃必须可见)
    price_rejected: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    url: Mapped[str | None] = mapped_column(Text)
    score: Mapped[float | None] = mapped_column(Numeric(4, 2))
    reviews: Mapped[int | None] = mapped_column(Integer)

    #: ★ **显式列**,不用 LIKE 猜(旧 ``exists_today`` 的
    #: ``payload NOT LIKE '%"source": "演示"%'`` 是脆弱写法)
    is_demo: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    raw_json_path: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        # ★ D8 的正面修复:同日多次各留一份 + 同 slot 重跑幂等
        UniqueConstraint(
            "anchor_name",
            "platform",
            "hotel_name",
            "room_type",
            "nights",
            "query_slot",
            name="cmp_price_key",
        ),
        Index("cmp_price_lookup", "anchor_name", "query_date", "platform"),
        Index("cmp_price_hotel_day", "hotel_id", "query_date"),
        Index("cmp_price_slot", "query_date", "query_slot"),
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"<CmpPrice {self.platform}/{self.anchor_name} {self.hotel_name} "
            f"{self.price} slot={self.query_slot}>"
        )


class CmpBatchRun(Base):
    """批量比价运行记录 —— **每日一行**(旧系统同日出现 2 条 ``done``)。

    ★ 为什么是每日一行而不是每次一行
    ================================

    旧系统的痛点是**同日两条 ``done``** → "今天比过没"无法判断
    (实测 08-25 有 5 条、08-26 有 2 条)。

    段3 用「每日一行 + UPSERT + ``attempt`` 计数」消除歧义;
    **尝试级历史交给段1 的 ``job_runs``** —— 它本来就是干这个的。
    这是**职责分离**,不是丢信息。
    """

    __tablename__ = "cmp_batch_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    batch_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # running | done | failed
    hotels_total: Mapped[int | None] = mapped_column(Integer)
    hotels_ok: Mapped[int | None] = mapped_column(Integer)
    hotels_failed: Mapped[int | None] = mapped_column(Integer)
    hotels_skipped: Mapped[int | None] = mapped_column(Integer)
    #: 第几次尝试(每次 UPSERT 自增)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (UniqueConstraint("batch_date", name="cmp_batch_key"),)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<CmpBatchRun {self.batch_date} {self.status} attempt={self.attempt}>"


class CmpPriceTarget(Base):
    """比价目标(**取代旧系统两个 txt** —— 段3 §5.4 对总纲的一处修正)。

    总纲 §7.3 曾把 ``config/compare_hotels.txt`` 放在 ``config/``;
    段3 修正:**一个会经常增删酒店的清单是"业务实体",不是"领域规则"** ——
    总纲 §7.6 自己定的边界是「``config/*.json`` 放领域规则:只增不改义;PG 放业务实体:可日常增删改」。

    旧系统把它做成**两个** txt(``compare_hotels.txt`` 批量 +
    ``price_targets.txt`` 定时),正是"配置与业务实体混装"的老毛病。
    段3:**同一张表**,用 ``mode`` 区分。
    """

    __tablename__ = "cmp_price_targets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    #: ★ 关联 ``core_hotels``(可空:导入的历史清单里可能有没有登记的酒店)
    hotel_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("core_hotels.id", ondelete="SET NULL")
    )
    anchor_name: Mapped[str] = mapped_column(String(256), nullable=False)
    city: Mapped[str | None] = mapped_column(String(128))
    #: 参与哪些平台(默认两平台)
    platforms: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{ctrip,meituan}'")
    )
    #: ``batch``(批量清单)/ ``cron``(定时采集)—— 取代旧系统的两个 txt
    mode: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'batch'"))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    #: 携程锚点**直达**用(旧 ``ctrip.py:227/296`` 的 ``ebk_hotel_id`` 优先)
    ebk_hotel_id: Mapped[str | None] = mapped_column(String(64))
    nights: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    remark: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("anchor_name", "city", name="cmp_target_key"),
        Index("cmp_target_mode", "mode", "enabled"),
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<CmpPriceTarget {self.anchor_name} city={self.city} mode={self.mode}>"
