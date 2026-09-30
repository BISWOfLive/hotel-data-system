"""批次 D 四张表:预警三源 / 房态 / 点评(T4.1 / T4.2 / T4.3)。

对应旧库 ``portal_columns`` / ``room_states`` / ``reviews`` / ``review_materials``
(``docs/参考/旧系统/规格-批次D提取器.md`` §4 的完整 DDL 逐字搬,只做**方言与项目约定**
两层转换):

  ① :class:`AlertPortalColumn`  ← 旧 ``portal_columns``     13 列,唯一键四元组
  ② :class:`AlertRoomState`     ← 旧 ``room_states``        13 列,**整批替换**语义
  ③ :class:`ReviewReview`       ← 旧 ``reviews``            11 列,唯一键 ``(hotel_id, review_id)``
  ④ :class:`ReviewMaterial`     ← 旧 ``review_materials``   10 列,唯一键三元组

**转换约定(全批统一,不是逐表拍脑袋)**

============================  ==========================================================
旧(SQLite)                    新(PostgreSQL)
============================  ==========================================================
``INTEGER PRIMARY KEY AUTOINCREMENT``  ``BigInteger`` + ``autoincrement``
``TEXT`` 存 JSON                ``JSONB``(旧列名保留在列注释里;项目约定 JSON 列一律
                                ``*_json`` 后缀 —— ``detail`` → ``detail_json``)
``TEXT 'YYYY-MM-DD'``           ``Date``
``TEXT`` 时间戳(点评时间)       ``DateTime(timezone=True)``(「不用 4 种时间存法」)
``DEFAULT (datetime('now','localtime'))``  ``server_default=now()``(timestamptz)
``REFERENCES hotels(id)``       ``ForeignKey("core_hotels.id", ondelete="CASCADE")``
============================  ==========================================================

★ **与旧 DDL 的一处有意差异**:旧 ``reviews`` / ``review_materials`` 的 ``hotel_id``
**没有** ``REFERENCES``(规格 §4.3 注明"这是有意的")。新库按项目外键纪律(「域之间不直接
join 别人的表」的前提是外键把一致性交给 DB)统一补 ``core_hotels.id`` 外键 + ``CASCADE``,
与 ``collect_reports`` / ``collect_modules`` 一致。

★ **口径注释一律搬过来**——它们是业务事实,不是装饰:
  - ``AlertRoomState.available``:``available=1 当且仅当 roomStatus=='G'
    (售完但 canUsedQuantity=0 仍算可订)``
  - ``ReviewMaterial.kind`` 实际 **4 类**(``score`` / ``competitor`` / ``trend`` / **``num``**);
    旧建表注释只写 3 类,是**错的**(规格 §6.4 漂移 D-5)。
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, created_at_column

__all__ = [
    "PORTAL_PAGES",
    "REVIEW_KINDS",
    "REVIEW_SENTIMENTS",
    "AlertPortalColumn",
    "AlertRoomState",
    "ReviewMaterial",
    "ReviewReview",
]

#: ``alert_portal_columns.page`` 的四个合法值(旧系统实库实测 8+6+5+2 = 21 行)
PORTAL_PAGES = ("channel_ctrip", "channel_qunar", "home_pending", "hot_calendar")

#: ``review_materials.kind`` 的**四**个合法值 —— 旧注释只写 3 类(漏 ``num``),见模块 docstring
REVIEW_KINDS = ("score", "competitor", "trend", "num")

#: ``review_reviews.sentiment`` 三态(旧 ``classify_sentiment``:宁可漏不可错)
REVIEW_SENTIMENTS = ("good", "bad", "unknown")


class AlertPortalColumn(Base):
    """预警采集列(渠道三指标 / 首页待办计数 / 热点日历事件)。

    口径(旧 ``storage/db.py:308-309`` 逐字):「采集列(渠道三指标/首页待办计数/热点日历事件)。
    页 = channel_ctrip / channel_qunar / hot_calendar / home_pending;value 统一 TEXT;
    detail 存补充 JSON(来源接口/排名/日期等)。」

    ``value`` **统一 TEXT** 是硬口径:同一列要同时承载
      * 整数计数(``visitor_total`` / ``comment_pending`` …),
      * 浮点评分(``ratingall`` / ``rating_avg``),
      * ISO 日期串(``hot_calendar`` 的 ``2026-09-25``),
      * 缺字段占位**空串**;
    读出后**必须自行 cast**。旧表结构上**没有** ``value_int`` / ``value_real`` / ``value_date``。

    ★ 代码路径上 ``value`` **永不写 NULL**:``None`` 一律变 ``""``
    (旧 ``portal_columns.py:135-137`` 的 ``_to_str``)。列本身可空只是历史遗留。

    ★ ``detail_json`` 的旧列名是 ``detail``(TEXT 存 JSON),这里按项目约定改名并转 JSONB;
    写库时用 ``json.dumps(..., ensure_ascii=False)`` 的中文可读性由 JSONB 天然保证。
    """

    __tablename__ = "alert_portal_columns"
    __table_args__ = (
        UniqueConstraint(
            "hotel_id",
            "collect_date",
            "page",
            "column_name",
            name="uq_alert_portal_columns_key",
        ),
        Index("ix_alert_portal_columns_lookup", "hotel_id", "collect_date", "page"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    hotel_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("core_hotels.id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("core_accounts.id", ondelete="SET NULL")
    )
    collect_date: Mapped[date] = mapped_column(Date, nullable=False)
    page: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="channel_ctrip / channel_qunar / hot_calendar / home_pending",
    )
    column_name: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
        comment="字段名(如 visitor_total / rating_avg / comment_pending / 中秋节)",
    )
    value: Mapped[str | None] = mapped_column(
        Text, comment="统一 str() 落库;缺字段 → 空串(代码路径不写 NULL)"
    )
    detail_json: Mapped[dict | None] = mapped_column(
        JSONB, comment='旧列 detail(TEXT),JSON:{"source_api": "...", "note": "...", ...}'
    )
    raw_json_path: Mapped[str | None] = mapped_column(Text, comment="原始响应相对路径")
    channel: Mapped[str] = mapped_column(String(32), nullable=False, server_default="api")
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="ok")
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_column()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<AlertPortalColumn {self.collect_date} {self.page}/{self.column_name}={self.value!r}>"


class AlertRoomState(Base):
    """房态(房型×日期)。

    ★★ ``available = 1`` 当且仅当 ``roomStatus == 'G'``(开房=可订);
    ``'N'`` 等 = 关房 = 不可订;**售完(``canUsedQuantity == 0`` 且 ``roomStatus == 'G'``)
    仍视为可订**(``available=1``,不误报未开房)**。

    → **关房与售满是正交的两件事,绝不可用 ``quantity`` 推 ``available``**。
    实库反证(旧 ``room_states`` 225 行):``available=1 且 quantity=0`` **28 行**,
    而 ``available=0`` 的 29 行里只有 2 行 ``quantity=0``。

    旧建表口径注释逐字(``storage/db.py:330``):「房态(房型×日期;available=1 iff
    roomStatus=='G'(开房);售完(canUsedQuantity=0)不算关房)」。
    旧列级注释逐字:``-- 1=可订(开房) 0=不可订(关房)`` / ``-- 原始 roomStatus('G'/'N'/...)``
    / ``-- 可售数量(canUsedQuantity)`` / ``-- roomPriceResult 均价(冗余参考)``。

    ★ **整批替换语义**:同 ``(hotel_id, collect_date)`` 的行由
    :meth:`hoteldata.domains.collect.repository.CollectRepository.replace_room_states`
    在**同一事务内先 DELETE 再 INSERT**(重跑结果一致)。因此本表**不建唯一约束**
    —— 唯一约束会掩盖"忘了先删"的 bug,本表的幂等由替换语义本身保证。
    只保留 ``(hotel_id, collect_date)`` 索引(删/查都以这两个键为入口)。
    """

    __tablename__ = "alert_room_states"
    __table_args__ = (Index("ix_alert_room_states_day", "hotel_id", "collect_date"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    hotel_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("core_hotels.id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("core_accounts.id", ondelete="SET NULL")
    )
    collect_date: Mapped[date] = mapped_column(Date, nullable=False, comment="采集日")
    room_type_id: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="售卖房型 ID(字符串化:str(roomTypeID))"
    )
    room_name: Mapped[str | None] = mapped_column(Text, comment="房型中文名(HTML 实体已解码)")
    effect_date: Mapped[date] = mapped_column(Date, nullable=False, comment="生效日")
    available: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment=(
            "1=可订(开房) 0=不可订(关房);"
            "available=1 当且仅当 roomStatus=='G'(售完但 canUsedQuantity=0 仍算可订)"
        ),
    )
    status_code: Mapped[str | None] = mapped_column(
        String(16), comment="原始 roomStatus('G'/'N'/...);组内**首个非 G**"
    )
    quantity: Mapped[int | None] = mapped_column(
        Integer, comment="可售数量(canUsedQuantity);同键多条取组内 max"
    )
    price: Mapped[float | None] = mapped_column(
        Float, comment="roomPriceResult 均价(冗余参考);多 ratePlan 取 min"
    )
    raw_json_path: Mapped[str | None] = mapped_column(Text, comment="原始响应相对路径")
    created_at: Mapped[datetime] = created_at_column()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"<AlertRoomState {self.collect_date} {self.room_type_id}@{self.effect_date}"
            f" available={self.available} status={self.status_code}>"
        )


class ReviewReview(Base):
    """待回复点评(``catalogTab=NotFeedBack``)。

    旧建表口径注释逐字(``storage/db.py:389-390``):「reviews:待回复点评(upsert;星级→
    sentiment good(≥4星)/bad(≤3星)/unknown(无星级,宁可漏不可错);replied=1 后不回溯)。」

    ★★ **UPSERT 不回溯 = 两条独立机制,缺一条就破坏语义**
    (旧 ``storage/db.py:1169-1195`` / 规格 §3.9):

      ① ``replied`` **根本不在 ``INSERT`` 列清单里** → 新行走本列默认 ``0``;冲突行走
         ``DO UPDATE SET``,而 ``DO UPDATE SET`` 里**同样没有** ``replied``
         → **已回复状态永久保持**,采集重跑不可能把它刷回 0。
      ② ``strategy`` **在 ``INSERT`` 列清单里、但被 ``DO UPDATE SET`` 显式排除**
         → 冲突时传入的 ``strategy`` **被丢弃**。``strategy`` 只能由回复流程
         (``mark_review_replied``)写入;实库反证:``replied=0`` 而 ``strategy="auto_failed"``
         的行存在,说明采集重跑没把它清空。

    ★ ``comment_time``:旧列是 TEXT(``'YYYY-MM-DD HH:MM:SS'``),这里改
    ``timestamptz`` —— 因为旧 ``parse_addtime`` 用 ``datetime.fromtimestamp(ms/1000)``
    **依赖宿主机时区**(正则吞掉 ``+0800`` 却未使用,规格 §3.5 ★ 重写警示),
    跑在 UTC 会得到不同的时间字符串。新实现**显式按 Asia/Shanghai** 转换后写
    ``timestamptz``,任何时区的机器读出来都是同一时刻。

    ★ ``fetched_at`` 保留旧列名(语义是"最近一次采集刷新时间",不是"创建时间"),
    每次 UPSERT 冲突都会刷新(旧 ``DO UPDATE SET ... fetched_at=datetime('now','localtime')``)。
    """

    __tablename__ = "review_reviews"
    __table_args__ = (
        UniqueConstraint("hotel_id", "review_id", name="uq_review_reviews_review_id"),
        Index("ix_review_reviews_pending", "hotel_id", "replied", "sentiment"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    hotel_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("core_hotels.id", ondelete="CASCADE"), nullable=False
    )
    review_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="平台点评 id(commentId);缺失时为内容指纹 'h'+sha1[:16]",
    )
    user_name: Mapped[str | None] = mapped_column(String(256), comment="评价人(平台侧已脱敏,如 M253349****)")
    star: Mapped[int | None] = mapped_column(
        Integer, comment="星级(1~5);来源 score.avgScoreSimple,int(float(...));空=unknown"
    )
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="点评原文")
    sentiment: Mapped[str | None] = mapped_column(
        String(16),
        server_default="good",
        comment="good(≥4星)/ bad(≤3星)/ unknown(无星级)",
    )
    replied: Mapped[int | None] = mapped_column(
        Integer,
        server_default="0",
        comment="1=已回复(ok/ignored/silent) 0=待处理;★ 采集 UPSERT 永不写本列",
    )
    strategy: Mapped[str | None] = mapped_column(
        String(64), comment="处理策略快照:模板id/silent/auto_failed;★ 采集 UPSERT 不覆盖"
    )
    comment_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="点评时间(旧 addtime /Date(ms+0800)/ 显式按 Asia/Shanghai 解析)"
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"<ReviewReview {self.hotel_id}/{self.review_id} star={self.star}"
            f" sentiment={self.sentiment} replied={self.replied}>"
        )


class ReviewMaterial(Base):
    """点评分析每日素材缓存,唯一键 ``(hotel_id, collect_date, kind)``。

    旧建表口径注释逐字(``storage/db.py:426-427``):「review_materials:点评分析每日素材
    缓存(getCommentsScoreV2 评分/getCompetitorCommentStat 对比建议/getCommentRateTrend
    趋势;kind=score|competitor|trend;不复用 module_records)」。

    ★ 但**实际写 4 类**:上面的注释漏了 ``num``(``getCommentNumV2`` 计数素材),
    实库有 ``kind=num`` **4 行**(规格 §6.4 漂移 D-5 / §7.3 检查项 9)。
    本模型的 ``kind`` 注释按**代码与实库事实**写 4 类。

    ★ ``payload_json`` **NOT NULL**:旧系统在 ``payload is None`` 时写 JSON 字面串
    ``"null"``(``json.dumps(None)``),**永不写 SQL NULL**。新实现同样保证
    (见 :meth:`CollectRepository.upsert_review_materials`)。

    ★ ``status`` 除 ``ok`` / ``degraded`` / ``failed`` 外,实际还会写 **``no_data``**
    (``"ok" if payload else "no_data"``)—— 旧建表注释只声明 3 值(规格 §6.3 漂移 C-17)。
    """

    __tablename__ = "review_materials"
    __table_args__ = (
        UniqueConstraint("hotel_id", "collect_date", "kind", name="uq_review_materials_key"),
        Index("ix_review_materials_lookup", "hotel_id", "collect_date", "kind"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    hotel_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("core_hotels.id", ondelete="CASCADE"), nullable=False
    )
    collect_date: Mapped[date] = mapped_column(Date, nullable=False, comment="素材采集日")
    kind: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="score / competitor / trend / num(★ 实为 4 类,旧注释漏 num)",
    )
    payload_json: Mapped[dict | None] = mapped_column(
        JSONB, nullable=False, comment="结构化素材;None → JSON 字面量 null(NOT NULL)"
    )
    raw_json_path: Mapped[str | None] = mapped_column(Text, comment="原始响应相对路径")
    channel: Mapped[str] = mapped_column(String(32), nullable=False, server_default="api")
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default="ok",
        comment="ok / degraded / no_data / failed(旧注释漏 no_data)",
    )
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_column()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<ReviewMaterial {self.collect_date} kind={self.kind} status={self.status}>"
