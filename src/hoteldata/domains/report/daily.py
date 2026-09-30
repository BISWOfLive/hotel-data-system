"""日报组装(T2C.1 + T2C.2)—— 标题行 + 轮换行 + 取图 + 热点日历 + 比价钩子。

计划书 §5.5 的流程**逐条**落在这里::

    build_daily_message(group_chatid)
      ├─ 取该群绑定的全部酒店(支持一群多店)
      ├─ 每店:
      │    ├─ 标题行:`### 「店名」`
      │    ├─ 轮换行:`📅 日期 · 今日轮换:A / B / C / D / E`
      │    ├─ 取图:today_module_shots(段1 service)+ 按 alias 顺序 pick
      │    │    └─ ★ 缺图跳过,不报错
      │    ├─ 含「预警-热点日历」项时附热点日历一览
      │    └─ ★ 比价段:price_section 钩子(段3 注入)
      ├─ 多店用 \\n\\n\\n 合并成 1 条
      └─ images 截断到 ≤5 张(settings.push.max_images)

**为什么组装不放 service.py**
==========================

旧系统把日报组装塞在 ``app/pusher.py`` 里(``_hotel_daily_section``,
``pusher.py:424-451``),与投递、限频、读表混在一个 647 行的文件里 ——
这正是计划书 §1.3 点名要避免的"上帝模块"。新架构的分工:

* ``daily.py``(本模块)—— **只组装内容**:markdown 与图片路径,不发送、不写审计;
* ``service.py`` —— 编排(哪个群发什么、去重、合并);
* ``push/`` —— 投递(限频/重试/审计)。

三处**逐字继承旧系统**的地方
==========================

1. 标题行与轮换行的文案(``pusher.py:432``):``### 「店名」`` + ``📅 日期 · 今日轮换:A/B/C/D/E``;
2. 取图按 ``alias`` 顺序、**缺图只记 info 不报错**(§5.5:缺图不是错误);
3. 热点日历一览的文案与"最多 5 行"(``pusher.py:389-421``)。

★ 热点日历**只用段1 的公开仓储**读
=================================

计划书 §1.3 禁止段2 直接写 SQL 去 join 提取表。热点日历读的是
``alert_portal_columns``,所以走 :class:`~hoteldata.domains.collect.repository.CollectRepository`
的公开读方法 ``list_portal_columns``(段1 明确标注为"段2 / CLI 自检用")。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from loguru import logger

from hoteldata.domains.collect.rotation import get_rotation
from hoteldata.domains.collect.service import today_module_shots
from hoteldata.push.bindings import BoundHotel
from hoteldata.push.sender import merge_sections
from hoteldata.push.service import BuiltMessage

__all__ = [
    "build_daily_message",
    "build_daily_section",
    "hot_calendar_lines",
    "rotation_line",
]

#: 热点日历页名(旧 ``pusher.py:397`` ``page="hot_calendar"``)
HOT_CALENDAR_PAGE = "hot_calendar"
#: 热点日历一览最多几行(旧 ``pusher.py:389`` ``limit: int = 5``)
HOT_CALENDAR_LIMIT = 5
#: 热点日历段标题(旧 ``pusher.py:421`` 逐字)
HOT_CALENDAR_TITLE = "🌡 **热点日历标注**"


# ---------------------------------------------------------------------------
# 轮换行
# ---------------------------------------------------------------------------


def rotation_line(plan: Any, day: date) -> str:
    """轮换行(旧 ``pusher.py:432`` 逐字):``📅 2026-09-30 · 今日轮换:A / B / C / D / E``。"""
    names = list(getattr(plan, "names", []) or [])
    return f"📅 {day.isoformat()} · 今日轮换:{' / '.join(names)}"


async def hot_calendar_lines(runtime: Any, hotel_id: int, day: date) -> str:
    """热点日历一览(旧 ``pusher.py:389-421``):读 ``alert_portal_columns`` 当日最新列。

    输出 ``• {名称} · {首日}（剩 N 天）``,最多 :data:`HOT_CALENDAR_LIMIT` 行;
    无数据 → ``""``(不是错误)。

    ★ 只走段1 公开仓储 ``CollectRepository.list_portal_columns``,自己不写 SQL。
    """
    from hoteldata.domains.collect.repository import CollectRepository

    try:
        async with runtime.db.session() as session:
            rows = await CollectRepository(session).list_portal_columns(
                hotel_id, day, page=HOT_CALENDAR_PAGE
            )
    except Exception as exc:  # noqa: BLE001 - 热点日历是附加信息,读不到不影响日报
        logger.warning("热点日历读取失败(hotel_id={} day={}): {}", hotel_id, day, exc)
        return ""

    lines: list[str] = []
    for row in rows:
        name = str(getattr(row, "column_name", "") or "")
        first = str(getattr(row, "value", "") or "")
        if not name or not first:
            continue
        try:
            remain = (date.fromisoformat(first) - day).days
        except ValueError:
            remain = None
        if remain is None:
            tail = ""
        elif remain >= 0:
            tail = f"（剩 {remain} 天）"
        else:
            tail = "（已开始）"
        lines.append(f"• {name} · {first}{tail}")
        if len(lines) >= HOT_CALENDAR_LIMIT:
            break
    if not lines:
        return ""
    return HOT_CALENDAR_TITLE + "\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# 单店段(T2C.1)
# ---------------------------------------------------------------------------


async def _rotation_images(runtime: Any, hotel_id: int, day: date, plan: Any) -> list[str]:
    """按轮换清单的 ``alias`` 顺序取当日模块截图;**缺图跳过,不报错**(§5.5)。

    取图入口是段1 的 :func:`~hoteldata.domains.collect.service.today_module_shots`
    —— 键名 = ``screenshot_modules[*].name``,``alias`` 缺省回退 ``name``
    (旧 ``rotation.pick_screenshot``,``rotation.py:76-85``)。
    """
    try:
        async with runtime.db.session() as session:
            groups = await today_module_shots(session, hotel_id, day)
    except Exception as exc:  # noqa: BLE001 - 取图失败 = 无图,日报照发(§5.5)
        logger.warning("日报取图失败(hotel_id={} day={}): {}", hotel_id, day, exc)
        return []

    found: list[str] = []
    for item in getattr(plan, "items", []) or []:
        aliases = list(getattr(item, "alias", None) or []) or [getattr(item, "name", "")]
        hit: str | None = None
        for group in groups:
            hit = group.pick(*aliases)
            if hit:
                break
        if not hit:
            logger.info(
                "[{}] 轮换模块「{}」当日无图(未采集/无数据/截图失败),跳过该项",
                hotel_id,
                getattr(item, "name", ""),
            )
            continue
        if hit not in found:
            found.append(hit)
    return found


async def build_daily_section(
    runtime: Any,
    hotel: BoundHotel,
    day: date,
    *,
    price_section: str | None = None,
) -> tuple[str, list[str]] | None:
    """组装**一家店**的日报段 → ``(markdown, 图片相对路径)``;酒店名为空 → ``None``。

    * 标题行 ``### 「店名」``;轮换行取段1 同一份 ``push_rotation.json``(21 项 / daily_count=5);
    * 当日轮换含「预警*」项 → 附热点日历一览;
    * 图片 ≤ ``settings.push.max_images``(默认 5;多店合并时还要再截一次,见 daily_message);
    * ★ ``price_section`` 是**段3 的显式钩子**(计划书 §1.4):段2 阶段恒 ``None``,
      段3 实现 ``domains/compare/service.py::build_price_section()`` 后按店注入,
      **不必回头改本文件**(避免"改一处漏三处")。
    """
    name = str(getattr(hotel, "name", "") or "")
    if not name:
        logger.warning("日报组装跳过:绑定酒店无名称(hotel_id={})", getattr(hotel, "hotel_id", None))
        return None
    hotel_id = int(getattr(hotel, "hotel_id", 0) or 0)

    rotation = getattr(runtime, "rotation", None) or get_rotation()
    plan = rotation.pick(day)

    lines = [f"### 「{name}」", rotation_line(plan, day)]
    markdown = "\n".join(lines)

    if getattr(plan, "alert_names", None):
        note = await hot_calendar_lines(runtime, hotel_id, day)
        if note:
            markdown += "\n\n" + note

    images = await _rotation_images(runtime, hotel_id, day, plan)

    if price_section:
        # ★ 段3 钩子(计划书 §1.4):位置就在这里,段2 不注入
        markdown += "\n\n" + price_section
    return markdown, images


# ---------------------------------------------------------------------------
# 群消息(T2C.1 + T2C.2)
# ---------------------------------------------------------------------------


async def build_daily_message(
    runtime: Any,
    chatid: str,
    *,
    price_section: str | None = None,
    day: date | None = None,
) -> BuiltMessage | None:
    """某群的日报(**1 条**):``build_daily_section`` × 绑定酒店 → ``\\n\\n\\n`` 合并。

    * 群**没有绑定酒店** → ``None``(调用方计入 ``empty``,**不发空消息**);
    * **逐店独立组装**(按 ``hotel_id`` 逐店取数,**不共享上下文**)—— 防串台(P9);
    * ``push_type="daily_report"``(A2-7),slot 去重由派发器做(V37);
    * ``hotel_ids`` = 全部店(派发器按店写审计,A2-8 之外的口径)。
    """
    settings = runtime.settings
    target_day = day or datetime.now(settings.tzinfo).date()
    hotels: list[BoundHotel] = await runtime.bindings.for_group(chatid)
    if not hotels:
        logger.info("群 {} 未绑定酒店,日报跳过", chatid)
        return None

    sections: list[str] = []
    images: list[str] = []
    hotel_ids: list[int] = []
    for hotel in hotels:
        built = await build_daily_section(runtime, hotel, target_day, price_section=price_section)
        if built is None:
            continue
        markdown, shots = built
        sections.append(markdown)
        images.extend(shots)
        hotel_ids.append(int(getattr(hotel, "hotel_id", 0) or 0))
    if not sections:
        return None

    limit = int(settings.push.max_images)
    if len(images) > limit:
        logger.warning("群 {} 日报附图 {} 张超过上限 {},已截断(A2-4)", chatid, len(images), limit)
        images = images[:limit]
    return BuiltMessage(
        chatid=chatid,
        push_type="daily_report",
        content=merge_sections(sections),
        images=tuple(images),
        hotel_ids=tuple(hotel_ids),
        note=f"日报 {len(sections)} 店",
    )
