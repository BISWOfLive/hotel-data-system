"""FAQ 知识库匹配与配图解析(T2B.4)。

FAQ 是什么
==========

``knowledge/faq.json``(9 条,A3-3 级遗产)是**甲方最常问的那几个问题**:
流量/收益/服务/漏斗/建议/全部数据/竞争圈/市场分析/用户行为。
它短、稳定、几乎不改 —— 所以放在 JSON 里让运营改,而不是写在代码里。

匹配顺序(保证"正确")
=====================

1. **精确**:问题与 ``item.questions`` 中任一问法**完全一致**(去 ``@``、压空白后比较);
2. **包含**:问题包含 ``item.keywords`` 中任一关键词。

> 为什么"精确在前":``questions`` 是运营逐条写下来的**真实问法**,
> 命中它就是确定命中;``keywords`` 是兜底网,容易误伤
> (旧系统把「服务怎么样」写进 questions,而 keywords 里的「服务」
> 会吃掉「竞争圈服务对比」这类别的意图)。

热加载(改 faq.json 不重启)
===========================

按 **mtime + size** 判重:文件被改 → 下一次匹配自动加载新内容。
为什么不是"每次读文件":群消息是高频入口,300 个群同时问会变成磁盘 IO 风暴;
为什么不是"只加载一次":运营改完 FAQ 必须重启服务才能生效 —— 那是运维事故的温床。

配图三形态(★ 计划书 §6 T2B.4)
==============================

======================  ============================================================
形态                     行为
======================  ============================================================
``{"module": "流量数据概况"}``  取**当日**该模块截图(:func:`today_module_shots` +
                        ``ModuleShots.pick``);**模块图缺失 → 回退整页** ``screenshot_path``
``{"full": true}``      整页截图 ``screenshot_path``
``"var/shots/x.png"``   固定路径图片(经 ``layout.from_relative`` 换算)
======================  ============================================================

★ **三形态全部要判 ``exists()``;找不到 → 返回空列表(V32),不抛异常**。
FAQ 命中却因为缺图把整条消息链打断,是比"少一张图"严重得多的故障。

★ ``{{指标名}}`` 占位符机制**保留**(旧 ``knowledge.build_reply``):
现存 9 条没有占位符 → 空转;真出现占位符时从 ``payload`` 取,取不到留空。
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

from loguru import logger

from hoteldata.domains.bot.commands import _strip_at_mention

__all__ = [
    "build_reply",
    "clean_question",
    "faq_path",
    "load_faq",
    "match_faq",
    "resolve_image_sources",
]

#: FAQ 文件缓存:``(mtime, size) -> list[item]``
_FAQ_CACHE: dict[tuple[float, int], list[dict[str, Any]]] = {}


def faq_path(runtime: Any) -> Path:
    """FAQ 路径 = ``settings.paths.project_root / "knowledge" / "faq.json"``。"""
    return Path(runtime.settings.paths.project_root) / "knowledge" / "faq.json"


def load_faq(runtime: Any, *, force: bool = False) -> list[dict[str, Any]]:
    """读 ``knowledge/faq.json``;★ **按 mtime 热加载**(改文件不重启)。

    * 文件不存在 / 解析失败 → 记 warning 并返回 ``[]``(**不抛**);
    * ``force=True`` → 绕过缓存(CLI / 自检用);
    * 缓存键是 ``(mtime, size)``:同一文件重复读取走内存,
      文件被改(哪怕 mtime 粒度不够)size 变了也会重新加载。
    """
    path = faq_path(runtime)
    try:
        stat = path.stat()
    except OSError as exc:
        logger.warning("FAQ 文件不可读: {} ({})", path, exc)
        return []
    key = (stat.st_mtime, stat.st_size)
    if not force and key in _FAQ_CACHE:
        return _FAQ_CACHE[key]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("FAQ 加载失败: {} ({})", path, exc)
        return []
    if isinstance(data, list):
        items = [x for x in data if isinstance(x, dict)]
    else:
        logger.warning("FAQ 顶层不是数组(实际 {}),按空表处理: {}", type(data).__name__, path)
        items = []
    _FAQ_CACHE.clear()  # 只留最新一份,避免运营改十次攒十个缓存
    _FAQ_CACHE[key] = items
    logger.debug("FAQ 已加载: {} 条 ({})", len(items), path)
    return items


def clean_question(text: str) -> str:
    """去 ``@`` 提及 → **压缩空白** → strip(旧 ``knowledge.clean_question`` 的口径)。

    旧实现是 ``re.sub(r"\\s+", "", text)``(**全删**空白),新架构改成**压缩成一个空格**:
    全删会把「我的 酒店」和「我的酒店」混为一谈,也会让 ``questions`` 里的
    空格差异变成不可见的匹配歧义。压缩后语义等价、且可读。
    """
    stripped = _strip_at_mention((text or "").strip())
    return re.sub(r"\s+", " ", stripped).strip()


def _norm(text: str) -> str:
    """比较用归一:去所有空白(``questions`` 之间只看内容,不看空格排版)。"""
    return re.sub(r"\s+", "", text or "")


def _exact_match(question: str, item: dict[str, Any]) -> bool:
    """``item.questions`` 里是否有与 ``question`` **完全一致**的问法(忽略空白)。"""
    for q in item.get("questions") or []:
        if q and _norm(str(q)) == question:
            return True
    return False


def match_faq(runtime: Any, question: str) -> dict[str, Any] | None:
    """匹配 FAQ:**精确 ``questions`` 优先 → ``keywords`` 包含兜底**;不中 → ``None``。

    两个问题都先过 :func:`clean_question`(去 ``@`` + 压空白),
    与旧 ``knowledge.match_faq`` 的顺序**逐条一致**。
    """
    q = _norm(clean_question(question))
    if not q:
        return None
    faq = load_faq(runtime)
    for item in faq:
        if _exact_match(q, item):
            return item
    for item in faq:
        for keyword in item.get("keywords") or []:
            if keyword and _norm(str(keyword)) in q:
                return item
    return None


# ---------------------------------------------------------------------------
# 回复渲染
# ---------------------------------------------------------------------------


def build_reply(item: dict[str, Any], payload: dict[str, Any] | None = None) -> str:
    """渲染回复文本:``{{指标名}}`` 占位符从 ``payload`` 取,取不到**留空**。

    旧 ``knowledge.build_reply`` 取不到时**原样保留** ``{{指标名}}`` ——
    于是群里就会出现"今日流量 {{流量}} "这种半成品(信息泄露 + 显得机器人坏了)。
    新口径:取不到 → **替换成空串**,并在 ``payload`` 里补一条 ``missing`` 记录,
    域层据此可见(不静默,但也不破坏文案)。

    ``payload`` 可以是 ``{"indicators": {...}}`` 或**直接的指标字典**,两种都吃。
    """
    text = item.get("reply") or "（无回复内容）"
    indicators = payload if isinstance(payload, dict) else {}
    if isinstance(indicators.get("indicators"), dict):
        indicators = indicators["indicators"]
    missing: list[str] = []

    def _repl(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        value = indicators.get(key)
        if value is None:
            missing.append(key)
            return ""
        return str(value)

    out = re.sub(r"\{\{([^{}]+)\}\}", _repl, text)
    if missing and isinstance(payload, dict):
        payload.setdefault("missing", []).extend(missing)
        logger.warning("FAQ 占位符缺数据,已留空: {}", "、".join(missing))
    return out


# ---------------------------------------------------------------------------
# 配图三形态
# ---------------------------------------------------------------------------


def _existing(path: Path | None) -> Path | None:
    """存在性判定(``None`` / ``OSError`` 都算不存在)。"""
    if path is None:
        return None
    try:
        return path if path.exists() else None
    except OSError:  # pragma: no cover - 极端路径
        return None


async def _module_shot(runtime: Any, hotel_id: int, day: date, page: str | None) -> Any:
    """取当日模块截图索引(段1 契约 ``today_module_shots``)。

    返回 ``ModuleShots | None``。**只允许走** ``domains.collect.service``,
    🚫 不许 import 提取器实现、也不许自己写 SQL 去读截图表(硬约束 3)。
    """
    from hoteldata.domains.collect.service import today_module_shots

    async with runtime.db.session() as session:
        shots = await today_module_shots(session, hotel_id, day, page)
    return shots[0] if shots else None


async def resolve_image_sources(
    runtime: Any, item: dict[str, Any], hotel_id: int | None, day: date | None = None
) -> list[Path]:
    """解析 FAQ 回复要附带的图片(★ **三形态**,缺失回退,找不到返回**空列表**)。

    ======================  ==================================================
    ``images`` 条目          行为
    ======================  ==================================================
    ``{"module": 名}``      当日模块图 → **缺失回退整页** ``screenshot_path``
    ``{"full": true}``      整页图 ``screenshot_path``
    ``"相对/绝对路径"``      经 ``layout.from_relative`` 直接读
    ======================  ==================================================

    * ``hotel_id`` 为 ``None``(群没绑店)→ ``full`` 与 ``module`` 形态**无从取图**,
      直接跳过并记日志(路径字符串形态仍可用);
    * ``day`` 缺省 = 今天;
    * ★ **一律判 ``exists()``**;缺图返回空列表,**不抛异常**(V32)。
    """
    entries = item.get("images") or []
    if not entries:
        return []
    target_day = day or date.today()
    layout = getattr(runtime, "layout", None)
    sources: list[Path] = []
    shots: Any = None
    shots_loaded = False

    async def _shots() -> Any:
        """懒取一次当日截图索引(多条 images 共用,不重复查库)。"""
        nonlocal shots, shots_loaded
        if not shots_loaded:
            shots_loaded = True
            if hotel_id is None:
                shots = None
            else:
                try:
                    shots = await _module_shot(runtime, int(hotel_id), target_day, None)
                except Exception as exc:  # noqa: BLE001 - 取图失败不该打断回复
                    logger.warning("取当日模块截图失败(店 {}): {}", hotel_id, exc)
                    shots = None
        return shots

    for entry in entries:
        if isinstance(entry, dict) and entry.get("full"):
            # 形态②:整页图
            current = await _shots()
            full = _existing(_as_path(getattr(current, "screenshot_path", None), layout))
            if full is not None:
                sources.append(full)
            else:
                logger.warning("无整页截图可提供（店 {} / {}）", hotel_id, target_day.isoformat())
            continue
        if isinstance(entry, dict) and entry.get("module"):
            # 形态①:当日模块图,缺失回退整页
            name = str(entry["module"])
            current = await _shots()
            module_path = _existing(_as_path(current.pick(name) if current else None, layout))
            if module_path is not None:
                sources.append(module_path)
                continue
            full = _existing(_as_path(getattr(current, "screenshot_path", None), layout))
            if full is not None:
                logger.warning("模块「{}」截图缺失,回退整页截图: {}", name, full)
                sources.append(full)
            else:
                logger.warning("模块「{}」截图缺失且无整页截图可回退（店 {}）", name, hotel_id)
            continue
        if isinstance(entry, str):
            # 形态③:固定路径
            path = _existing(_as_path(entry, layout))
            if path is not None:
                sources.append(path)
            else:
                logger.warning("FAQ 固定图片不存在,跳过: {}", entry)
            continue
        logger.warning("FAQ images 条目形态无法识别,跳过: {!r}", entry)
    return sources


def _as_path(value: Any, layout: Any) -> Path | None:
    """字符串 / ``Path`` → 绝对 ``Path``(相对路径经 ``layout.from_relative``)。"""
    if value is None:
        return None
    raw = str(value)
    if not raw:
        return None
    path = Path(raw)
    if path.is_absolute():
        return path
    if layout is not None:
        try:
            return Path(layout.from_relative(raw))
        except Exception as exc:  # noqa: BLE001 - 换算失败退回原路径
            logger.warning("路径换算失败({}): {}", raw, exc)
    return path
