"""预警每日汇总与日志写入(T2E.7 / V52 / V54)。

送达率口径(**整个段2 最容易算错的一处**)
==========================================

::

    送达率 = alert_logs 里 pushed=true 的行数 ÷ alert_logs 的**总行数**

两个"必须":
  * **无管理群时也要写一行** ``recipient='manage-none'`` + ``pushed=false`` 并
    **计入分母**。否则"没人可发"会被算成 100% 送达 —— 这正是甲方最怕的
    **静默失败**(计划书 §5.7 表格末行 / V52);
  * **每推一个目标写一行**(A2-8:预警写 ``alert_logs``,**不是** ``push_logs``)。
    推 3 个管理群 + 2 个运营群 = 5 行,成功 4 行 → 80%。

★ 两个键,两个概念(行 = 触发 × 收件人)
=======================================

===============  ===========================================  ==================
键                值                                            职责
===============  ===========================================  ==================
``delivery_key``  ``rule:hotel:entity:YYYY-MM-DD:recipient``     **行身份**(UNIQUE)
``trigger_key``   ``rule:hotel:entity:YYYY-MM-DD``               **触发身份**(按它聚合)
===============  ===========================================  ==================

**为什么行必须带收件人**:送达率的定义本身就要求如此 —— ``manage-none`` 是一个
*收件人值*。一行代表一个触发的话,那条"推 3 个目标"的记录写 ``true`` 会让
B/C 的失败在日志里消失(D1 同类),写 ``false`` 会抹掉 A 的成功并低估分母。

**为什么冲突策略是 UPSERT 而不是 DO NOTHING**:``check(force=True)`` 会绕过触发级
去重真的重发;``DO NOTHING`` 会把这次重发的结果丢掉 —— 表里仍留着上一次的
``failed`` 行,**运维看到"一直失败",实际早就恢复了**。改成 ``DO UPDATE`` 后:

  * 稳态仍是 **1 行 / 触发×目标** → 送达率分母不变、依旧准确;
  * 最后一次投递结果胜出 → **重推的恢复可见**;
  * 逐目标的部分失败照旧留痕(每个目标一行,各带自己的 ``error``)。

★ 触发级的当日去重**不在本模块**,由
:meth:`~hoteldata.domains.alert.state.AlertStateStore.should_push` 承担
(``last_trigger_date == today`` + ``dedup="once_per_day"``,``force`` 可绕)。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from loguru import logger
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from hoteldata.infra.models import AlertLog

__all__ = [
    "RECIPIENT_NO_MANAGE",
    "LogWrite",
    "build_daily_summary",
    "delivery_key_of",
    "trigger_key_of",
    "write_log",
]


@dataclass(frozen=True, slots=True)
class LogWrite:
    """一次 :func:`write_log` 的结果。

    ``created=True`` = 本次**新建**了一行(该触发×目标今天第一次投递);
    ``created=False`` = 该行已存在,本次是**刷新**(重推/补跑),最后一次结果胜出。

    ★ 为什么要区分:``DO NOTHING`` 改成 UPSERT 之后,"写了几行"不再等于
    "日志里的行数增量"。把它们混为一谈会让"重推到底有没有生效"从计数上看不出来
    —— 而这正是本次改动的**目的**,不能在计数上又把它抹掉。
    """

    id: int
    created: bool


#: UPSERT 时要刷新的列 —— **投递结果类**字段(状态、错误、附图、快照、时间)。
#:
#: ``id`` 当然不动;``rule_id`` / ``hotel_id`` / ``entity_key`` / ``log_date`` /
#: ``delivery_key`` / ``trigger_key`` 是**键的一部分**,同冲突就意味着它们已相等,
#: 刷不刷都一样(不刷更省)。``title`` / ``detail`` 是渲染文案,重推时可能因为
#: 模板或数据变化而不同,所以**也要刷新**(否则重推后会留着旧文案)。
_REFRESH_COLUMNS = (
    "title",
    "detail",
    "pushed",
    "error",
    "images_json",
    "pushed_at",
    "payload_json",
)


#: 无管理群时的收件人标记(旧 ``alert_push.push_trigger``:230)
RECIPIENT_NO_MANAGE = "manage-none"


def trigger_key_of(rule_id: str, hotel_id: int, entity_key: str, day: date) -> str:
    """**触发身份**:``rule:hotel:entity:YYYY-MM-DD``(计划书 §5.7 的「日志去重键」)。

    一个触发推给 N 个目标 → **N 行共享同一个 ``trigger_key``**,按它聚合就能回答
    "这条预警推给了谁、谁失败了"。
    """
    return f"{rule_id}:{hotel_id}:{entity_key}:{day.isoformat()}"


def delivery_key_of(
    rule_id: str, hotel_id: int, entity_key: str, day: date, recipient: str = ""
) -> str:
    """**行身份**:``rule:hotel:entity:YYYY-MM-DD:recipient``。

    ``recipient`` 缺省时退化为 :func:`trigger_key_of` 的形态(便于调用方在
    "还不知道收件人"时先算一个键)。
    """
    base = trigger_key_of(rule_id, hotel_id, entity_key, day)
    return f"{base}:{recipient}" if recipient else base


async def build_daily_summary(runtime: Any, *, day: date | None = None) -> tuple[str, dict[str, Any]]:
    """当日 ``alert_logs`` → ``(markdown 文案, stats)``。

    ``stats = {"rules": n, "hotels": n, "total": n, "pushed": n, "rate": 百分比}``
    (另附 ``failed`` 与 ``rate_ratio`` 便于消费方二选一)。

    ★ ``rate`` 是**百分数**(旧 ``alert_push.build_daily_summary``:306 的口径,
    与文案里的 ``送达率 87.5%`` 一致);``rate_ratio`` 是 0~1 的比值。
    """
    target = day or date.today()
    async with runtime.db.session() as s:
        rows = list(
            (await s.execute(select(AlertLog).where(AlertLog.log_date == target))).scalars().all()
        )

    lines = [f"📊 预警汇总 {target.isoformat()}"]
    if not rows:
        lines.append("- 今日无预警记录")
        stats = {"rules": 0, "hotels": 0, "total": 0, "pushed": 0, "failed": 0, "rate": 0.0}
        stats["rate_ratio"] = 0.0
        return "\n".join(lines), stats

    by_rule: dict[str, int] = {}
    hotels: set[int] = set()
    pushed = 0
    for row in rows:
        rid = str(row.rule_id or "?")
        by_rule[rid] = by_rule.get(rid, 0) + 1
        if row.hotel_id:
            hotels.add(int(row.hotel_id))
        if bool(row.pushed):
            pushed += 1
    total = len(rows)
    for rid, count in sorted(by_rule.items(), key=lambda kv: -kv[1]):
        lines.append(f"- {rid}: {count} 条")
    lines.append(f"- 涉及酒店: {len(hotels)} 家")
    lines.append(f"- 发送：成功 {pushed} / 失败 {total - pushed}（送达率 {pushed / total * 100:.1f}%）")

    stats = {
        "rules": len(by_rule),
        "hotels": len(hotels),
        "total": total,
        "pushed": pushed,
        "failed": total - pushed,
        "rate": pushed / total * 100.0,
        "rate_ratio": pushed / total,
        "day": target.isoformat(),
    }
    return "\n".join(lines), stats


async def write_log(
    runtime: Any,
    trigger: Any,
    *,
    recipient: str,
    pushed: bool,
    error: str | None = None,
    images: list[str] | None = None,
    pushed_at: datetime | None = None,
    content: str | None = None,
    day: date | None = None,
) -> LogWrite:
    """写一行 ``alert_logs``(每推一个目标一行)。

    * ``delivery_key`` 冲突 → ``ON CONFLICT DO UPDATE``(**最后一次投递结果胜出**)。
      稳态仍是 1 行/触发×目标,但 ``check(force=True)`` 的**重推结果不会被丢掉**
      —— 这是"重推成功了、日志里却还写着 failed"那个坑的修法(见模块 docstring);
    * 返回 :class:`LogWrite`(``id`` + ``created``)—— 新建与刷新都返回,**永不返回
      ``None``**(没有"什么都没写"的第三种结果);
    * ``day`` 缺省取 ``pushed_at`` 的日期,再缺省取今天 ——
      ``log_date`` 是**统计与去重的键**,必须与调用方的巡检日一致;
    * ``content`` 是渲染后的正文(存 ``detail`` 列前 500 字,旧口径 ``md[:500]``);
    * ``images`` 是附图**相对路径**列表(进 ``images_json``)。

    ★ 异常**不吞**:这里不再有"冲突就静默返回"的分支,任何失败都该让调用方看见
    (``push_triggers`` 会把它计进 ``failed`` 并记日志)—— 少写一行日志本身就是
    静默失败。
    """
    stamp = pushed_at or datetime.now()
    log_day = day or stamp.date() or date.today()
    rule_id = str(getattr(trigger, "rule_id", "") or "")
    hotel_id = int(getattr(trigger, "hotel_id", 0) or 0)
    entity_key = str(getattr(trigger, "entity_key", "") or "")
    detail = content
    if detail is None:
        lines = getattr(trigger, "detail_lines", None) or []
        detail = "\n".join(str(x) for x in lines) if not isinstance(lines, str) else lines

    delivery_key = delivery_key_of(rule_id, hotel_id, entity_key, log_day, recipient)
    values = {
        "rule_id": rule_id,
        "hotel_id": hotel_id,
        "entity_key": entity_key,
        "delivery_key": delivery_key,
        "trigger_key": trigger_key_of(rule_id, hotel_id, entity_key, log_day),
        "log_date": log_day,
        "title": str(getattr(trigger, "title", "") or "")[:500],
        "detail": (detail or "")[:500],
        "recipient": recipient,
        "pushed": bool(pushed),
        "images_json": list(images) if images else None,
        "error": (str(error)[:300] if error else None),
        "payload_json": dict(getattr(trigger, "payload", None) or {}),
        "pushed_at": stamp if pushed else None,
    }
    stmt = pg_insert(AlertLog).values(**values)
    # ★ ``xmax = 0`` ⇒ 这一行是**本次 INSERT 进来的**;``xmax <> 0`` ⇒ 走的是
    #   DO UPDATE 分支。这是 PG 的**实现细节**,但用得很广(9.x ~ 16 稳定),
    #   而且这里**只用来给计数器打标**——行的正确性完全由 UNIQUE 索引保证,
    #   拿不到标记也不会写错数据。因此这个依赖是安全的。
    stmt = stmt.on_conflict_do_update(
        index_elements=["delivery_key"],
        set_={col: getattr(stmt.excluded, col) for col in _REFRESH_COLUMNS},
    ).returning(AlertLog.id, text("(xmax = 0) AS inserted"))
    async with runtime.db.session() as s:
        row = (await s.execute(stmt)).first()
    if row is None:  # pragma: no cover - RETURNING 在 PG 上必然给行
        raise RuntimeError(f"写 alert_logs 未返回行:{delivery_key}")
    written = LogWrite(id=int(row[0]), created=bool(row[1]))
    logger.debug(
        "预警日志已写:{} -> recipient={} pushed={} {}",
        delivery_key,
        recipient,
        bool(pushed),
        "新建" if written.created else "刷新(UPSERT:最后一次结果胜出)",
    )
    return written

