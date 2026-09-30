"""实时问答(「问才发」,T2B.5)。

什么叫"问才发"
==============

``config/report_schedule.json`` 里有 **22 项报告**,其中两项打了
``"realtime_ask": true``:

* ``checkout`` —— 「离店」
* ``booking`` —— 「预订销售数据」

这两项**不进 09:00 调度**(V33),只在群成员**问到**的时候才取最新数据回复。

为什么(vs 定时推)
=================

这两项数据在一天里变十几次(离店数、预订销售额),定时推的话:
要么推得太早(数据还没长齐),要么推得太勤(群里刷屏)。
甲方口径:**问才发** —— 谁关心谁问,问了就给最新的。

命中判定(逐字继承旧 ``realtime_qa._match``)
===========================================

* ``item['name']`` 去掉尾部括号后缀(``"服务概览(日报)"`` → ``"服务概览"``)后**包含**于问题;
* 或 :data:`ALIASES` 里的别名包含于问题(``离店``/``退房``、``预订``/``销售数据``)。

★ **只处理第一个命中项**,且**无关问题返回 ``None``(不误答,V34)** ——
"什么都答一句"比"答不出来"更伤信任。

一群多店
========

取该群**全部**绑定酒店(``bindings.for_group``),每店一段,
用 :func:`hoteldata.push.sender.merge_sections`(``\\n\\n\\n``)合并成**一条**消息(A2-5)。

🚫 本模块**不参与定时推送**,也不自己读采集表:数据由报告域
``runtime.report().realtime_answer(question, chatid)`` 提供(惰性 import,
报告域缺席 → 返回 ``None``,消息链继续往下走 FAQ)。
"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path
from typing import Any

from loguru import logger

from hoteldata.push.sender import merge_sections

__all__ = [
    "ALIASES",
    "build_realtime_reply",
    "load_schedule",
    "match_realtime_item",
    "realtime_items",
    "strip_suffix",
]

#: 问才发项别名(键=``item['name']``;旧 ``realtime_qa.ALIASES`` 逐字继承)。
#: 只有这两条需要别名 —— 其余项用 ``name`` 本身匹配即可。
ALIASES: dict[str, tuple[str, ...]] = {
    "离店": ("离店", "退房"),
    "预订销售数据": ("预订", "销售数据"),
}

#: ``report_schedule.json`` 的 mtime 缓存(配置可能被运营改动,热加载)
_SCHEDULE_CACHE: dict[str, Any] = {"key": None, "data": None}


def strip_suffix(name: str) -> str:
    """去掉名称尾部括号后缀(中文/英文括号):``"服务概览(日报)"`` → ``"服务概览"``。"""
    return re.split(r"[（(]", name or "", maxsplit=1)[0].strip()


def load_schedule(runtime: Any) -> dict[str, Any]:
    """读 ``report_schedule.json``(**优先走报告域,回退直读 config**)。

    为什么两条路:

    1. 报告域已就绪(``domains/report``)→ 用它的 ``load_schedule()``:
       它是 22 项校验的**唯一权威**,实时问答必须与定时推送看同一份解析结果;
    2. 报告域缺席(并行开发期间 / 该域被裁剪)→ 直读
       ``settings.paths.config_dir / "report_schedule.json"``。

    两条路都失败 → 返回 ``{}``(调用方按"无实时项"处理,返回 ``None`` 不误答)。
    按 **mtime** 缓存,改配置不必重启。
    """
    path = Path(runtime.settings.paths.config_dir) / "report_schedule.json"
    try:
        stat = path.stat()
        key: Any = (str(path), stat.st_mtime, stat.st_size)
    except OSError:
        key = None
    if key is not None and _SCHEDULE_CACHE.get("key") == key:
        cached = _SCHEDULE_CACHE.get("data")
        return cached if isinstance(cached, dict) else {}
    data = _load_schedule_via_report(runtime)
    if data is None:
        data = _load_schedule_from_file(path)
    _SCHEDULE_CACHE["key"] = key
    _SCHEDULE_CACHE["data"] = data
    return data


def _load_schedule_via_report(runtime: Any) -> dict[str, Any] | None:
    """报告域路径(惰性 import;域缺席返回 ``None``,由调用方回退)。"""
    try:
        from hoteldata.domains.report.schedule import load_schedule as _domain_load
    except (ImportError, AttributeError):
        return None
    try:
        data = _domain_load(runtime)
    except TypeError:
        # 域层签名可能是无参的 —— 兼容一次,不让"参数约定"变成不可用
        try:
            data = _domain_load()
        except Exception as exc:  # noqa: BLE001
            logger.warning("报告域 schedule 加载失败: {}", exc)
            return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("报告域 schedule 加载失败: {}", exc)
        return None
    return data if isinstance(data, dict) else None


def _load_schedule_from_file(path: Path) -> dict[str, Any]:
    """直读 ``config/report_schedule.json``(失败 → 空 dict,不抛)。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("报告节奏配置不可读: {} ({})", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def realtime_items(schedule: dict[str, Any]) -> list[dict[str, Any]]:
    """筛出 ``realtime_ask == true`` 的项(顺序 = 配置顺序)。"""
    return [it for it in (schedule.get("items") or []) if isinstance(it, dict) and it.get("realtime_ask")]


def match_realtime_item(content: str, item: dict[str, Any]) -> bool:
    """内容是否命中该实时项(``name`` 去括号后缀 / ``ALIASES`` 任一;旧 ``_match`` 逐字)。"""
    name = str(item.get("name") or "").strip()
    base = strip_suffix(name)
    if base and base in content:
        return True
    for alias in ALIASES.get(name, ()) or ALIASES.get(base, ()):
        if alias and alias in content:
            return True
    return False


def _prefix_hotel(name: str) -> str:
    """单店段落前缀(旧 ``realtime_qa`` 的 ``f"**「{店名}」**\\n"``)。"""
    return f"**「{name}」**"


def _split(result: Any) -> tuple[str, list[Any]]:
    """报告域返回形态归一:``str`` / ``(text, images)`` / ``None`` → ``(text, images)``。"""
    if result is None:
        return "", []
    if isinstance(result, tuple):
        text = str(result[0]) if result and result[0] is not None else ""
        images = list(result[1]) if len(result) > 1 and result[1] else []
        return text, images
    return str(result), []


def _per_hotel_mode(runtime: Any) -> bool:
    """报告域的 ``realtime_answer`` 是否支持**按店作答**(一群多店时逐店问)。"""
    try:
        service = runtime.report()
    except (ImportError, AttributeError):
        return False
    answer = getattr(service, "realtime_answer", None)
    return bool(callable(answer) and _accepts_hotel(answer))


async def _ask_report(
    runtime: Any, question: str, chatid: str, hotel_name: str | None = None
) -> Any:
    """委托报告域作答;域缺席 → ``None``(消息链继续走 FAQ)。

    约定调用:

    * ``runtime.report().realtime_answer(question, chatid)`` —— 基础签名;
    * 若该方法**还接受第三个参数**(店名),一群多店时按店逐个问,
      这样每段的内容才**不会串店**(报告域的取数必须知道是哪家店)。

    返回 ``str``(纯数据段落)或 ``(str, list[Path])``(带图)。
    """
    try:
        service = runtime.report()
    except (ImportError, AttributeError) as exc:
        logger.info("报告域未就绪,实时问答跳过: {}", exc)
        return None
    answer = getattr(service, "realtime_answer", None)
    if not callable(answer):
        logger.info("报告域未提供 realtime_answer,实时问答跳过")
        return None
    #: 报告域是否按店作答(签名里有第三个位置参数)—— 决定一群多店时是否逐店问
    per_hotel = _accepts_hotel(answer)
    result = answer(question, chatid, hotel_name) if (per_hotel and hotel_name) else answer(question, chatid)
    if inspect.isawaitable(result):
        result = await result
    return result


def _accepts_hotel(method: Any) -> bool:
    """``realtime_answer`` 是否接受第三个位置参数(店名)。"""
    try:
        params = [
            p
            for p in inspect.signature(method).parameters.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
    except (TypeError, ValueError):  # pragma: no cover - 内建/C 实现
        return False
    return len(params) >= 3


async def build_realtime_reply(
    runtime: Any, question: str, chatid: str | None = None
) -> str | tuple[str, list[Any]] | None:
    """群内"问才发"回复;**无命中 / 无数据 / 域缺席 → ``None``**。

    步骤:
      1. **仅群聊**(``chatid`` 非空;私聊直接 ``None``);
      2. 命中 ``realtime_ask`` 项或别名,只取**第一个**命中项;
      3. 取该群**全部**绑定酒店(一群多店 → 每店一段,``merge_sections`` 合并为 1 条);
      4. 委托 ``runtime.report().realtime_answer(question, chatid)``(惰性 import);
      5. 报告域未就绪 / 返回空 → ``None``(**不误答,V34**;由消息链回落 FAQ)。
    """
    if not question or not chatid:
        return None
    schedule = load_schedule(runtime)
    items = realtime_items(schedule)
    if not items:
        return None
    hit = next((it for it in items if match_realtime_item(question, it)), None)
    if hit is None:
        return None

    try:
        bound = await runtime.bindings.for_group(chatid)
    except Exception as exc:  # noqa: BLE001 - 绑定查询失败按"无数据"处理,不误答
        logger.warning("实时问答读群绑定失败: chatid={} err={}", chatid, exc)
        return None
    if not bound:
        logger.info("实时问答命中「{}」但本群未绑定酒店: {}", hit.get("name"), chatid)
        return None

    hotel_names = [h.name for h in bound]
    per_hotel = _per_hotel_mode(runtime)
    # 一群一店:一次问完;一群多店且报告域支持按店作答:逐店问(见下)
    result = await _ask_report(runtime, question, chatid, hotel_names[0] if len(hotel_names) == 1 else None)
    text, images = _split(result)
    if per_hotel and len(bound) > 1:
        # 逐店问:每店一段,段与段之间用 ``\n\n\n`` 合并成 1 条(A2-5)
        sections: list[str] = []
        images = []
        for hotel in bound:
            one_text, one_images = _split(await _ask_report(runtime, question, chatid, hotel.name))
            if not one_text:
                continue
            sections.append(f"{_prefix_hotel(hotel.name)}\n{one_text}")
            images.extend(one_images)
        if not sections:
            return None
        merged = merge_sections(sections)
    else:
        if not text:
            return None
        label = hotel_names[0] if len(hotel_names) == 1 else "、".join(hotel_names)
        merged = merge_sections([f"{_prefix_hotel(label)}\n{text}"])
    logger.info(
        "实时问答命中: item={} chatid={} 店={} 图={} 张",
        hit.get("id") or hit.get("name"),
        chatid,
        len(bound),
        len(images),
    )
    return (merged, images) if images else merged
