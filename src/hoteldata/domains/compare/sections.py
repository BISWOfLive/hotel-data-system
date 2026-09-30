"""★ 对段2 的契约(段3 T3F.2 / V82)—— 比价段注入日报。

计划书 §1.4 的钩子**已经躺在段2 里了**(段2 阶段恒 ``None``),
段3 只要把 ``price_section`` 传进去 —— **段2 的日报组装代码一行不用改**。

但计划书写的签名与段2 已落地的形状**对不上**(段3 P7):

=========================================  ==================================================
计划书 §1.4                                 段2 实际落地(``runtime.py:351-357``)
=========================================  ==================================================
``build_price_section(hotel_id, date)``     ``daily_builder(chatid, price_section)``
**按店**返回一段                             **一个群一个字符串**
=========================================  ==================================================

一群多店时,需要有人把 N 家店的段拼成**那一个字符串**。

★ 旧系统**已经解决过这个问题** —— ``app/price_push.py:126-132`` 的
``compare_md_for_groups``:首行 ``📊 **比价汇总**（{n} 家酒店）`` + 逐店段 ``\\n\\n`` 拼接。
段3 **逐字继承这个形状**(:func:`build_group_price_section`),只是数据源从 SQLite 换成 PG。

★ 本模块同时提供三个层级的入口
=============================

===============================================  ==========================================
函数                                               谁用
===============================================  ==========================================
:func:`build_price_section`                       单店(CLI / 自检 / 验收)
:func:`build_group_price_section`                 **日报钩子**(一群多店)
:func:`latest_result_for`                         从库里读回某店最近一次比价
===============================================  ==========================================

★ slot 语义(P8)
===============

计划书说"返回当日 **09:00** slot 的比价段",但采集点是 **08:30 / 13:30 / 17:30** ——
**09:00 不对应任何采集点**。段3 的语义是「**取当日最近一个已完成的 slot**」,
并在段首**显示该 slot 的实际时刻**(避免"看起来是实时价"的误导)。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from loguru import logger

from hoteldata.domains.compare.contract import HotelQuote
from hoteldata.domains.compare.report import build_group_price_text, build_price_text
from hoteldata.domains.compare.repository import CompareRepository

__all__ = [
    "PriceSectionResult",
    "build_group_price_section",
    "build_price_section",
    "latest_quotes_for",
]


@dataclass(slots=True)
class PriceSectionResult:
    """一次比价段组装的产物(便于 CLI/验收断言,而不是只返回一个字符串)。"""

    text: str
    anchor_name: str
    slot: str = ""
    quotes: int = 0
    priced: int = 0
    distance_available: bool = False
    reason: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.text) and self.priced > 0


async def latest_quotes_for(
    session: Any,
    *,
    anchor_name: str,
    day: date,
    slot: str | None = None,
    include_demo: bool = False,
) -> tuple[list[HotelQuote], str]:
    """从库里读回某店某日某 slot 的报价 → ``(quotes, slot)``。

    ★ 读的是**段3 自己的表**(``cmp_price_comparisons``),不 join 任何别人的表
      —— 所以调用方必须先把 ``hotel_id`` 换成 ``anchor_name``
      (日报钩子从 ``BoundHotel`` 拿到名字,天然满足)。

    ★ **默认排除演示数据**(``is_demo=False``):日报推给客户的必须是真数据。
    """
    repo = CompareRepository(session)
    use_slot = slot
    if not use_slot:
        use_slot = await repo.latest_slot(
            anchor_name=anchor_name, query_date=day, include_demo=include_demo
        )
    if not use_slot:
        return [], ""

    rows = await repo.list_quotes(
        anchor_name=anchor_name,
        query_date=day,
        slot=use_slot,
        include_demo=include_demo,
        limit=200,
    )
    quotes: list[HotelQuote] = []
    for row in rows:
        quotes.append(
            HotelQuote(
                hotel_name=row.hotel_name,
                hotel_id=None,
                price=float(row.price) if row.price is not None else None,
                price_scope=row.price_scope or "from",  # type: ignore[arg-type]
                room_type=row.room_type,
                distance_km=float(row.distance_km) if row.distance_km is not None else None,
                coord_source=row.coord_source or "none",  # type: ignore[arg-type]
                price_source=row.price_source or "dom",  # type: ignore[arg-type]
                need_manual_check=bool(row.need_manual_check),
                degraded=bool(row.degraded),
                price_rejected=list(row.price_rejected or []),
                url=row.url,
                score=float(row.score) if row.score is not None else None,
                reviews=row.reviews,
                raw={"platform": row.platform},
            )
        )
    return quotes, use_slot


async def build_price_section(
    session: Any,
    *,
    anchor_name: str,
    day: date,
    slot: str | None = None,
    quote_count: int = 3,
    include_demo: bool = False,
) -> PriceSectionResult:
    """**单店**比价文字段(计划书 §1.4 的 ``build_price_section`` 的单店形态)。

    无数据 → ``text=""``(**日报里不出现比价段**,不是显示"暂无比价记录")。
    """
    quotes, use_slot = await latest_quotes_for(
        session, anchor_name=anchor_name, day=day, slot=slot, include_demo=include_demo
    )
    if not quotes:
        return PriceSectionResult(text="", anchor_name=anchor_name, reason="当日无比价记录")

    priced = [q for q in quotes if q.price is not None]
    text = build_price_text(
        anchor_name=anchor_name,
        quotes=quotes,
        query_date=day,
        slot=use_slot,
        quote_count=quote_count,
        distance_available=any(q.distance_km is not None for q in quotes),
    )
    return PriceSectionResult(
        text=text,
        anchor_name=anchor_name,
        slot=use_slot,
        quotes=len(quotes),
        priced=len(priced),
        distance_available=any(q.distance_km is not None for q in quotes),
    )


async def build_group_price_section(
    session: Any,
    *,
    hotel_names: list[str],
    day: date,
    slot: str | None = None,
    quote_count: int = 3,
    include_demo: bool = False,
) -> str | None:
    """★ **日报钩子**:一个群(可能绑定多店)→ **一个字符串**。

    形状逐字继承旧 ``app/price_push.py:126-132``(``compare_md_for_groups``)::

        📊 **比价汇总**（2 家酒店）

        📊 **隐欲民宿 比价**(2026-08-27 17:30)
        **携程**：...
        **美团**：...
        > 数据来源：携程/美团实时采集

        📊 **盛铂仕丹酒店 比价**(...)
        ...

    没有**任何**一家店有比价数据 → ``None`` → **日报里不出现比价段**
    (计划书 §1.4 明文:「无数据返回 ``None``(日报里不出现比价段)」)。

    ★ 这个方法就是段3 P7 的答案:段2 的 ``daily_builder(chatid, price_section)``
      只接受一个群级字符串,而本函数把 N 家店的段拼成那一个 ——
      **段2 代码一行不改**(V82 用 SHA256 基线证明)。
    """
    sections: list[str] = []
    for name in hotel_names:
        if not name:
            continue
        try:
            one = await build_price_section(
                session,
                anchor_name=name,
                day=day,
                slot=slot,
                quote_count=quote_count,
                include_demo=include_demo,
            )
        except Exception as exc:  # noqa: BLE001 - 单店读失败不该让整个日报没有比价段
            logger.warning("比价段组装失败(hotel={}): {}", name, exc)
            continue
        if one.text and one.priced > 0:
            sections.append(one.text)

    if not sections:
        return None
    if len(sections) == 1:
        return sections[0]
    return build_group_price_text(sections=sections, hotel_count=len(sections))


def now_day(settings: Any = None) -> date:
    """当天(按进程时区)。"""
    if settings is not None:
        return datetime.now(settings.tzinfo).date()
    return date.today()
