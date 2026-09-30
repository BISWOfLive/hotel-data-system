"""预警两张表:``alert_states``(状态机) 与 ``alert_logs``(日志 + 送达率口径)。

段2 批次 E(T2E.2 / T2E.7)。对应旧库 ``alert_states`` / ``alert_logs``。

★ 关键修复(旧库已被写脏的根因)
================================

旧库 ``alert_logs`` 的日志键**没有 UNIQUE**(总纲 §4.1 明确标注 ⚠️)。
新库在迁移 ``0003`` 补上唯一约束,并在 ``0004`` 把"行身份"与"触发身份"
拆成两列(``delivery_key`` / ``trigger_key``)—— 完整理由见 :class:`AlertLog` 的 docstring。
理由与比价库的 6 组重复行同源:应用层 ``SELECT-then-INSERT`` 在并发下必然漏,
唯一约束是**数据库兜底**的那一层。

★ 三条必须理解的语义(重写最易踩的地方,总纲 §3.5)
==================================================

1. **"连续 7 天"不在状态机里,在数据里。**
   ``unavailable_days`` 由引擎从 ``alert_room_states`` **逐日推导**(可订即断;
   今日缺数据 → 保守不触发)。本表的 ``streak`` **只是展示用计数,不是推送门槛**。
   若改成"streak >= 7 才推",语义直接漂移(V48 专门验这条)。

2. **两层时刻结构。**
   规则里的 ``check_times`` 是**名义时刻**(09:00/14:30/19:00,业务语义层);
   调度层错峰 +4 分钟(09:04/14:34/19:04);引擎再用 slot 映射**映回名义时刻**
   去匹配 ``check_times``。把两层合并成一层,slot 匹配立刻失效(V49)。

3. **"忽略此店"写 ``entity_key='__hotel__'``** —— 对该店每条规则各写一行,
   外加一行 ``rule_id='__all__'`` 兜底(规则清单变了也不会漏)。
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
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
    "ALERT_STATE_STATUSES",
    "ALERT_HOTEL_ENTITY",
    "ALERT_ALL_RULE",
    "AlertLog",
    "AlertState",
]

#: ``alert_states.status`` 取值
#: ``triggered`` = 触发中;``ok`` = 已恢复(``reset_when_ok`` 清零);``ignored`` = 被忽略
ALERT_STATE_STATUSES = ("ok", "triggered", "ignored")

#: 「忽略此店」写的实体键(与真实实体键不会冲突 —— 实体键不会是 ``__`` 开头)
ALERT_HOTEL_ENTITY = "__hotel__"

#: 「忽略此店」额外写的兜底规则 id(规则清单变化时也不会漏)
ALERT_ALL_RULE = "__all__"


class AlertState(Base):
    """预警状态机(``UNIQUE(rule_id, hotel_id, entity_key)``)。

    ★ ``streak`` **只是展示用**。真正的"连续 N 天"判定在引擎里由
    ``alert_room_states`` 的逐日数据推导(见模块 docstring 第 1 条)。
    """

    __tablename__ = "alert_states"
    __table_args__ = (
        UniqueConstraint("rule_id", "hotel_id", "entity_key", name="uq_alert_states_key"),
        Index("ix_alert_states_hotel", "hotel_id", "rule_id"),
        Index("ix_alert_states_ignored", "hotel_id", "ignored_until"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    rule_id: Mapped[str] = mapped_column(String(64), nullable=False)
    hotel_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("core_hotels.id", ondelete="CASCADE"), nullable=False
    )
    entity_key: Mapped[str] = mapped_column(
        String(256), nullable=False, comment="房型 id / 事件名+日期 / __hotel__(整店)"
    )
    streak: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", comment="★ 仅展示用计数,不是推送门槛"
    )
    last_trigger_date: Mapped[date | None] = mapped_column(Date, comment="当日去重依据")
    last_ok_date: Mapped[date | None] = mapped_column(Date, comment="最近一次恢复日")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="ok")
    ignored_until: Mapped[date | None] = mapped_column(Date, comment="忽略至(含当天)")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"<AlertState {self.rule_id} h{self.hotel_id} {self.entity_key} "
            f"streak={self.streak} {self.status}>"
        )


class AlertLog(Base):
    """预警推送日志 —— **送达率口径的唯一来源**。

    ★★ **两个键,两个概念**(段2 施工中修正过一次,原因见下)
    ======================================================

    ==================  ==========================================  ==================
    列                   值                                            职责
    ==================  ==========================================  ==================
    ``delivery_key``     ``rule:hotel:entity:YYYY-MM-DD:recipient``     **行身份**
                         **UNIQUE**
    ``trigger_key``      ``rule:hotel:entity:YYYY-MM-DD``               **触发身份**
                         (普通索引)                                     (按触发聚合)
    ==================  ==========================================  ==================

    **为什么行必须带收件人**:计划书 §5.7 自己把送达率定义成
    「成功行 ÷ 总行;**无管理群也要计入分母**(写 ``recipient='manage-none'``)」
    —— ``manage-none`` 是一个**收件人值**,不是布尔标志。若一行代表一个触发,
    那条**推 3 个目标只留 1 行**的行既不能写 ``pushed=true``(B/C 两目标的失败
    会在日志里彻底消失,正是 D1「出事了没人知道」的同一类病),也不能写
    ``false``(A 的成功被抹掉、分母被低估)。**没有第三种写法。**

    **为什么行身份那列不叫 ``dedup_key``**:真正的"当日去重"是**触发级**的,
    由 :class:`AlertState` 的 ``should_push`` + ``last_trigger_date`` 承担。
    把 ``recipient`` 拼进去之后,"去重键"这个名字就是在说谎。所以拆成两列:
    行身份叫 ``delivery_key``、触发身份叫 ``trigger_key``
    (后者才是计划书里 ``rule:hotel:entity:date`` 那个"日志去重键")。

    **为什么冲突策略是 UPSERT 而不是 DO NOTHING**:``check(force=True)`` 会绕过
    触发级去重真的重发,但 ``DO NOTHING`` 会让这次重发的结果被丢掉 ——
    表里仍留着上一次的 ``failed`` 行,运维看到的是"一直失败",实际早就恢复了。
    改成 ``DO UPDATE`` 之后:稳态仍是 **1 行 / 触发×目标**(分母不变、依旧准确),
    重推的恢复**可见**,逐目标的部分失败照旧留痕。

    **送达率** = ``count(pushed=true) / count(*)``;★ **无管理群时也要写一行**
    ``recipient='manage-none'`` + ``pushed=false`` 并**计入分母** ——
    否则"没人可发"会被算成 100% 送达。
    """

    __tablename__ = "alert_logs"
    __table_args__ = (
        UniqueConstraint("delivery_key", name="uq_alert_logs_delivery_key"),
        Index("ix_alert_logs_day", "log_date"),
        Index("ix_alert_logs_rule", "rule_id", "log_date"),
        Index("ix_alert_logs_hotel", "hotel_id", "log_date"),
        #: 按**触发**聚合(「今日预警状态」/ 复盘"这条预警推给了哪些目标")
        Index("ix_alert_logs_trigger", "trigger_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    rule_id: Mapped[str] = mapped_column(String(64), nullable=False)
    hotel_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("core_hotels.id", ondelete="CASCADE"), nullable=False
    )
    entity_key: Mapped[str] = mapped_column(String(256), nullable=False)
    delivery_key: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        comment="行身份 rule:hotel:entity:YYYY-MM-DD:recipient(★ UNIQUE;已重推则刷新为最后一次结果)",
    )
    trigger_key: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        comment="触发身份 rule:hotel:entity:YYYY-MM-DD(计划书 §5.7 的「日志去重键」,按它聚合)",
    )
    log_date: Mapped[date] = mapped_column(Date, nullable=False, comment="日志日(去重与统计的键)")
    title: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[str | None] = mapped_column(Text)
    recipient: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
        comment="管理群 chatid / 运营群 chatid / manage-none(无管理群,计入分母)",
    )
    pushed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    images_json: Mapped[list | None] = mapped_column(JSONB, comment="附图相对路径")
    error: Mapped[str | None] = mapped_column(Text)
    payload_json: Mapped[dict | None] = mapped_column(JSONB, comment="触发时的数据快照(便于复盘误报)")
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<AlertLog {self.delivery_key} pushed={self.pushed}>"
