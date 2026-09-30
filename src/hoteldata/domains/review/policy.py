"""T2F.1 策略层 —— 点评「回 / 不回」的决策(批次 F 的地基)。

**为什么要有这一层**
====================

旧系统把"判定"散在 ``app/review_reply.py`` 的 ``effective_policy`` /
``decide_action`` / ``is_auto_ready`` 三个函数里(旧 ``review_reply.py:106-173``)。
新架构把**配置读取 + 三层优先级 + 门控判定**收在本模块,``draft`` / ``autoreply`` /
``service`` 只消费结论,不再各自解释配置 —— 否则立刻出现"第二个来源"。

★★ 本模块承载的口径(计划书 §5.8 / 附录 D / 迁移 ``0003_segment2_push``)
=====================================================================

① **策略优先级**:``core_hotels.review_policy``(群命令「点评策略」写的 JSONB)
   > ``config/review_templates.json`` 的 ``overrides[店名]`` > 默认
   ``{"good": "g01", "bad": "silent"}``。
   (旧 ``review_reply.py:106-128`` 逐字;旧库该列是 TEXT 存 JSON,新库是 JSONB。)

② **情感阈值**:星级 ``>= 4`` → ``good``、``<= 3`` → ``bad``、**无星级 → ``unknown``**;
   ``unknown`` 时用 ``score.commentLevel`` 的 ``好评/差评`` 兜底;
   ★ **``unknown`` 永不自动回复**(宁可漏不可错,防自动错回)。
   (旧 ``review_reply.py:78-92`` / 附录 D 逐字;批次 D 的
   :func:`hoteldata.domains.collect.review.classify_sentiment` 是同一口径的落库侧实现,
   本模块的实现服务**决策侧** —— 两边都读同一份阈值来源,不新增第三种说法。)

③ **自动回复门控** = ``auto.enabled`` + 灰度店白名单 ``auto.hotel_whitelist`` +
   ``config/review_sources.json`` 的 ``submit.ready`` **且** ``submit.api.url`` 非空。

   ⚠️ **现实提醒(计划书 §5.8 逐字)**:点评的**提交接口从未被捕获**
   (旧 ``docs/回复通道研究结论.md``),所以 **``submit.ready`` 当前必然为 false**。
   段2 的自动回复**在可预见的时间内一直走「未就绪 → 人工队列」** ——
   这不是 bug,是已知边界;V57 专门验这个行为**正确**(明确提示 + 进人工队列,
   **绝不伪造成功**)。

④ **差评一律不进自动**:``auto.only_sentiment="both"`` 是旧配置里的历史选项,
   段2 口径固定为"自动只处理好评"(§5.8 / 附录 D)。本模块只把该配置读出来并
   **在 ``warn_legacy_only_sentiment`` 里出声**,不改变行为。

配置热加载沿用旧系统纪律(旧 ``review_reply.py:43-72``):按 **mtime** 缓存,
文件缺失/非法 → **回落默认配置并打 warning**(自动模式默认关闭),
绝不因为配置文件坏掉而让进程崩 —— 但**也绝不静默**。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import select

from hoteldata.infra.models import REPLY_EXECUTORS, REPLY_STATUSES, REVIEW_SENTIMENTS, Hotel

__all__ = [
    "DEFAULT_AUTO",
    "DEFAULT_POLICY",
    "DEFAULT_RULES",
    "auto_whitelist",
    "classify_sentiment",
    "decide_action",
    "effective_policy",
    "is_auto_ready",
    "load_review_config",
    "load_review_sources",
    "reload_review_config",
    "resolve_hotel",
    "review_field",
    "sentiment_of",
    "set_policy",
    "submit_ready",
    "warn_legacy_only_sentiment",
]

#: 模板缺失时的兜底规则(旧 ``review_reply.py:27-32`` 逐字,与 ``review_templates.json`` 同值)
DEFAULT_RULES: dict[str, Any] = {
    "good_min_star": 4,
    "bad_max_star": 3,
    "reply_interval_s": 120,
    "draft_limit": 10,
}

#: ``auto`` 段兜底(旧 ``review_reply.py:33`` 逐字;``enabled=False`` = 灰度未开)
DEFAULT_AUTO: dict[str, Any] = {"enabled": False, "hotel_whitelist": [], "only_sentiment": "good"}

#: 默认店级策略(旧 ``review_reply.py:103`` 逐字:好评走 g01,差评统一不回复)
DEFAULT_POLICY: dict[str, Any] = {"good": "g01", "bad": "silent"}

#: ``score.commentLevel`` → 情感(旧 ``review_sources.json`` 的 ``sentiment_hint_map`` 同值)。
#: 这里保留一份**纯函数兜底表**,是为了让 :func:`classify_sentiment` 可以脱离配置文件单测;
#: 真正消费配置映射的是批次 D 的提取器(落库侧),两边映射值一致。
DEFAULT_HINT_MAP: dict[str, str] = {
    "好评": "good",
    "差评": "bad",
    "中评": "unknown",
    "good": "good",
    "bad": "bad",
    "unknown": "unknown",
}

_TEMPLATES_FILENAME = "review_templates.json"
_SOURCES_FILENAME = "review_sources.json"

#: ``{绝对路径: (mtime, 解析结果)}`` —— mtime 热加载缓存(旧系统每次读盘,新库按 mtime)
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


# ---------------------------------------------------------------------------
# 配置加载(mtime 热加载 + 失败回落默认)
# ---------------------------------------------------------------------------


def _config_dir(settings: Any = None) -> Path:
    """配置目录:优先调用方传入的 ``settings``,否则用进程单例。"""
    if settings is not None:
        return Path(settings.paths.config_dir)
    from hoteldata.settings import get_settings

    return Path(get_settings().paths.config_dir)


def _read_json_cached(path: Path, *, force: bool = False) -> dict[str, Any] | None:
    """按 mtime 读 JSON;文件缺失/非法 → ``None``(调用方负责回落默认)。"""
    key = str(path)
    try:
        mtime = path.stat().st_mtime
    except OSError as exc:
        logger.warning("{} 读取失败({}),按未配置降级", path.name, exc)
        _CACHE.pop(key, None)
        return None
    cached = _CACHE.get(key)
    if not force and cached is not None and cached[0] == mtime:
        return copy.deepcopy(cached[1])
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("{} 解析失败({}),按未配置降级", path.name, exc)
        _CACHE.pop(key, None)
        return None
    if not isinstance(raw, dict):
        logger.warning("{} 顶层不是对象({}),按未配置降级", path.name, type(raw).__name__)
        _CACHE.pop(key, None)
        return None
    _CACHE[key] = (mtime, copy.deepcopy(raw))
    return raw


def load_review_config(*, force: bool = False, settings: Any = None) -> dict[str, Any]:
    """读 ``config/review_templates.json``(**mtime 热加载**;缺失/非法 → 默认配置)。

    返回 ``{"templates", "overrides", "rules", "auto", "path"}`` ——
    键名与旧 ``review_reply.py:43-61`` 完全一致,段2 各模块只认这五个键。

    ``rules`` / ``auto`` 与默认值**逐键合并**(配置文件只写想覆盖的那几项)。
    """
    path = _config_dir(settings) / _TEMPLATES_FILENAME
    raw = _read_json_cached(path, force=force) or {}

    templates = raw.get("templates")
    overrides = raw.get("overrides")
    rules = dict(DEFAULT_RULES)
    rules.update(raw.get("rules") or {})
    auto = dict(DEFAULT_AUTO)
    auto.update(raw.get("auto") or {})
    return {
        "templates": templates if isinstance(templates, dict) else {},
        "overrides": overrides if isinstance(overrides, dict) else {},
        "rules": rules,
        "auto": auto,
        "path": str(path),
    }


def load_review_sources(*, force: bool = False, settings: Any = None) -> dict[str, Any]:
    """读 ``config/review_sources.json``;缺失 → ``{}``(通道按未就绪降级)。

    ★ **只读**。本模块**不写**这两个配置文件:可变状态不进 ``config/``
    (``settings.py`` 三条禁令之二)。
    """
    path = _config_dir(settings) / _SOURCES_FILENAME
    return _read_json_cached(path, force=force) or {}


def reload_review_config() -> None:
    """清空 mtime 缓存(改完配置当场生效;CLI/命令用)。"""
    _CACHE.clear()


# ---------------------------------------------------------------------------
# ② 情感阈值(纯函数,单测对象)
# ---------------------------------------------------------------------------


def classify_sentiment(
    star: Any,
    hint: Any = None,
    *,
    rules: dict[str, Any] | None = None,
    hint_map: dict[str, str] | None = None,
) -> str:
    """星级 → ``good`` / ``bad`` / ``unknown``(附录 D 的阈值口径,逐字)。

    * ``star >= good_min_star(4)`` → ``good``
    * ``star <= bad_max_star(3)``  → ``bad``
    * **无星级 / 星级不可解析** → 用 ``hint``(平台 ``score.commentLevel``,
      ``好评``/``差评``)兜底;兜不到 → ``unknown``

    ★ ``unknown`` **永不自动回复**(口径 ②):宁可漏不可错。
    容忍 ``"4.2"`` 这类字符串星级(与批次 D 提取器的 ``int(float(...))`` 同口径)。
    """
    active_rules = rules or DEFAULT_RULES
    mine = {**DEFAULT_HINT_MAP, **(hint_map or {})}

    value: int | None = None
    if star is not None and str(star).strip() != "":
        try:
            value = int(float(str(star).strip()))
        except (TypeError, ValueError):
            value = None
    if value is not None:
        if value >= int(active_rules.get("good_min_star", 4)):
            return "good"
        if value <= int(active_rules.get("bad_max_star", 3)):
            return "bad"
        return "unknown"

    text = str(hint or "").strip()
    if not text:
        return "unknown"
    if text in REVIEW_SENTIMENTS:
        return text
    for key, mapped in mine.items():
        if key and key in text:
            return mapped if mapped in REVIEW_SENTIMENTS else "unknown"
    return "unknown"


def review_field(obj: Any, key: str, default: Any = None) -> Any:
    """点评行的兼容取值:ORM 行 / ``dict`` / dataclass(三处在用同一套字段名)。

    放在 ``policy`` 而不是 ``templates``:两个模块都要用,而 ``templates`` 已经依赖
    ``policy``(单向),反过来 import 会成环。
    """
    if obj is None:
        return default
    if isinstance(obj, dict):
        value = obj.get(key, default)
    else:
        value = getattr(obj, key, default)
    return default if value is None else value


def sentiment_of(review_row: Any, *, rules: dict[str, Any] | None = None, hint: Any = None) -> str:
    """点评行的情感(**先信库里那一列,缺失/非法才现算**,口径 ②)。"""
    value = str(review_field(review_row, "sentiment", "") or "")
    if value in REVIEW_SENTIMENTS:
        return value
    if hint is None:
        hint = review_field(review_row, "comment_level", None)
    return classify_sentiment(review_field(review_row, "star", None), hint, rules=rules)


def decide_action(
    policy: dict[str, Any],
    review_row: Any,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """单条点评的动作判定(旧 ``decide_action`` 旧 ``review_reply.py:131-159`` 逐字)。

    返回 ``{"action", "template_id", "reason"}``,``action`` 三态:

    ==============  ====================================================================
    ``silent``      差评 + 店级 ``silent`` → **统一不回复**(落 ``ignored`` + ``replied=1``)
    ``suggest``     好评(模板)/ 差评 ``template``(差评模板)→ 出**草稿**给人工确认
    ``manual``      无星级 ``unknown`` → 出**人工待办**(★ 永不自动、**绝不标成已回复**)
    ==============  ====================================================================

    ★ ``manual`` 与 ``silent`` **必须分开**:``silent`` 是"业务决定不回复"(=已处理完),
    ``unknown`` 是"还没判清楚"(=仍待处理)。把 ``unknown`` 也标 ``ignored``/``replied=1``
    会让无星级点评**永远退出待回复池**,人工再也看不到它。
    """
    cfg = config if config is not None else load_review_config()
    sentiment = sentiment_of(review_row)
    if sentiment == "good":
        return {
            "action": "suggest",
            "template_id": str(policy.get("good") or _first_template_id(cfg, "g") or "g01"),
            "reason": "suggest: 好评模板(自动未就绪 → 人工确认)",
        }
    if sentiment == "bad":
        mode = str(policy.get("bad") or "silent").strip().lower()
        if mode == "silent":
            return {
                "action": "silent",
                "template_id": "silent",
                "reason": "silent: 店级策略差评统一不回复(甲方口径,落 ignored + replied=1)",
            }
        return {
            "action": "suggest",
            "template_id": str(policy.get("bad_template") or _first_template_id(cfg, "b") or "b01"),
            "reason": "suggest: 差评模板策略(店级);★ 差评一律不进自动",
        }
    return {
        "action": "manual",
        "template_id": None,
        "reason": "manual: 无星级,需人工确认星级(宁可漏不可错)",
    }


# ---------------------------------------------------------------------------
# ① 三层优先级合并
# ---------------------------------------------------------------------------


def _hotel_name(hotel: Any) -> str:
    if hotel is None:
        return ""
    if isinstance(hotel, dict):
        return str(hotel.get("name") or "")
    return str(getattr(hotel, "name", "") or "")


def _hotel_id(hotel: Any) -> int | None:
    if hotel is None:
        return None
    raw = hotel.get("id") if isinstance(hotel, dict) else getattr(hotel, "id", None)
    if raw is None:
        raw = hotel.get("hotel_id") if isinstance(hotel, dict) else getattr(hotel, "hotel_id", None)
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _db_policy(hotel: Any) -> dict[str, Any] | None:
    """取行对象/字典上的 ``review_policy``(已是 ORM 行且可信时用)。"""
    raw = hotel.get("review_policy") if isinstance(hotel, dict) else getattr(hotel, "review_policy", None)
    return dict(raw) if isinstance(raw, dict) else None


async def _stored_policy(runtime: Any, hotel: Any, fallback: Any = None) -> dict[str, Any] | None:
    """读 ``core_hotels.review_policy`` —— **一律回库**,不用内存里的快照。

    ★ 为什么不直接用传进来的 ORM 行的 ``review_policy``:换会话后那是个**可能过期的快照**
    (session 关闭 → 对象 detach,属性不再刷新)。「点评策略」命令刚刚写完、同一个进程
    又拿旧的酒店行来算策略时,会算回旧值(实测踩到)。策略是**决策输入**,
    宁可多一次主键查询,也不接受脏读。
    """
    hotel_id = _hotel_id(hotel)
    if hotel_id is None:
        hotel_id = _hotel_id(fallback)
    if hotel_id is not None:
        async with runtime.db.session() as session:
            raw = await session.scalar(select(Hotel.review_policy).where(Hotel.id == hotel_id))
        return dict(raw) if isinstance(raw, dict) else None
    return _db_policy(hotel) or _db_policy(fallback)


async def resolve_hotel(runtime: Any, hotel: Any) -> Hotel | None:
    """把 ``id`` / 店名 / ORM 行 / 绑定值对象 归一为 ``core_hotels`` ORM 行。

    读不到 → ``None``(调用方必须**出声**,不许静默当成"没有待办")。
    """
    if isinstance(hotel, Hotel):
        return hotel
    if hotel is None:
        return None

    hotel_id = _hotel_id(hotel)
    name = _hotel_name(hotel)
    if hotel_id is None and isinstance(hotel, str) and not hotel.strip().isdigit():
        name = hotel.strip()
    if hotel_id is None and isinstance(hotel, int):
        hotel_id = hotel

    async with runtime.db.session() as session:
        if hotel_id is not None:
            row = await session.scalar(select(Hotel).where(Hotel.id == hotel_id))
            if row is not None:
                return row
        if name:
            return await session.scalar(select(Hotel).where(Hotel.name == name))
    return None


async def effective_policy(runtime: Any, hotel_row_or_id: Any) -> dict[str, Any]:
    """**三层优先级合并**后的店级策略(旧 ``review_reply.py:106-128`` 逐字顺序)。

    返回 ``{"good": 模板id, "bad": "silent"|"template", "bad_template": 模板id|None}``:

    1. 默认 ``{"good": "g01", "bad": "silent"}``(甲方口径:差评统一不回复);
    2. ``config.overrides[店名]`` 覆盖;
    3. ``core_hotels.review_policy``(**群命令写,优先级最高**)覆盖;
    4. ``bad == "template"`` 且未给 ``bad_template`` → 取配置里首个 ``b*`` 模板(旧
       ``_first_template_id``),再兜 ``b01``;``bad == "silent"`` → ``bad_template=None``。
    """
    config = load_review_config(settings=getattr(runtime, "settings", None))
    policy = dict(DEFAULT_POLICY)

    hotel = hotel_row_or_id if isinstance(hotel_row_or_id, Hotel) else None
    if hotel is None:
        hotel = await resolve_hotel(runtime, hotel_row_or_id)

    name = _hotel_name(hotel) or (hotel_row_or_id if isinstance(hotel_row_or_id, str) else "")
    override = (config.get("overrides") or {}).get(name)
    if isinstance(override, dict):
        policy.update(override)

    stored = await _stored_policy(runtime, hotel, hotel_row_or_id)
    if stored:
        policy.update(stored)

    return _normalize_policy(policy, config)


def _normalize_policy(policy: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """补齐/纠正策略字段(保证返回形状恒定,调用方不必再判空)。"""
    out = dict(policy)
    mode = str(out.get("bad") or "silent").strip().lower()
    if mode not in ("silent", "template"):
        logger.warning("店级点评策略 bad={!r} 非法,按 silent 处理(防错回)", out.get("bad"))
        mode = "silent"
    out["bad"] = mode
    if mode == "silent":
        out["bad_template"] = None
    elif not out.get("bad_template"):
        out["bad_template"] = _first_template_id(config, "b") or "b01"
    if not out.get("good"):
        out["good"] = _first_template_id(config, "g") or "g01"
    return out


def _first_template_id(config: dict[str, Any], prefix: str) -> str | None:
    """按前缀取第一个模板 id(旧 ``review_reply.py:95-100`` 逐字,含 ``sorted`` 稳定序)。"""
    for tid in sorted(str(k) for k in (config.get("templates") or {})):
        if tid.startswith(prefix):
            return tid
    return None


async def set_policy(runtime: Any, hotel_id: Any, changes: dict[str, Any]) -> dict[str, Any]:
    """写 ``core_hotels.review_policy``(JSONB)—— 群命令「点评策略」的唯一落库点。

    与旧 ``commands.py:539-542`` 一致:写入的是 **``effective_policy`` 合并 ``changes``
    之后的生效快照**(不是只写 delta)。这样做的代价是"把 config overrides 固化进库",
    属于**有意继承**的旧行为 —— 若某个店既在 ``overrides`` 里、又被命令改过一次,
    此后 ``overrides`` 对该店**不再生效**;要恢复必须以命令再改一次(或清空该店
    ``review_policy``)。

    返回 ``{"ok", "hotel_id", "review_policy"}`` 或 ``{"ok": False, "error": ...}``。
    """
    if not isinstance(changes, dict) or not changes:
        return {"ok": False, "error": "策略变更不能为空(changes={})"}
    unknown = set(changes) - {"good", "bad", "bad_template"}
    if unknown:
        return {"ok": False, "error": f"策略键非法: {sorted(unknown)}(应为 good/bad/bad_template)"}
    if "bad" in changes:
        mode = str(changes["bad"] or "").strip().lower()
        if mode not in ("silent", "template"):
            return {"ok": False, "error": f"差评策略非法: {changes['bad']}(应为 silent|template)"}
        changes = {**changes, "bad": mode}

    hotel = await resolve_hotel(runtime, hotel_id)
    if hotel is None:
        return {"ok": False, "error": f"酒店不存在: {hotel_id}"}

    config = load_review_config(settings=getattr(runtime, "settings", None))
    policy = dict(await effective_policy(runtime, hotel))
    policy.update(changes)
    policy = _normalize_policy(policy, config)

    templates = config.get("templates") or {}
    for key in ("good", "bad_template"):
        tid = policy.get(key)
        if tid and tid not in templates:
            logger.warning("点评策略 {}={} 不在 review_templates.json 的模板库里(推送时按缺失处理)", key, tid)

    async with runtime.db.session() as session:
        row = await session.scalar(select(Hotel).where(Hotel.id == int(hotel.id)))
        if row is None:
            return {"ok": False, "error": f"酒店不存在: {hotel_id}"}
        row.review_policy = dict(policy)

    logger.info(
        "点评策略已更新:店={} good={} bad={} bad_template={}",
        hotel.name,
        policy.get("good"),
        policy.get("bad"),
        policy.get("bad_template"),
    )
    return {"ok": True, "hotel_id": int(hotel.id), "hotel": str(hotel.name), "review_policy": policy}


# ---------------------------------------------------------------------------
# ③ 自动回复门控
# ---------------------------------------------------------------------------


def submit_ready(runtime: Any = None) -> tuple[bool, str]:
    """``review_sources.json`` 的 ``submit`` 通道是否就绪(``(ready, 原因文案)``)。

    ★ **两者都就绪才算 ready**:``submit.ready === true`` **且** ``submit.api.url`` 非空。
    只就绪一半 → 返回 ``(False, 原因)`` —— 原因文案会出现在「点评状态」与自动回复的
    汇总里,**不许静默返回 False**。

    ⚠️ 当前 ``submit.ready`` **必然为 false**(提交接口从未被捕获,
    旧 ``docs/回复通道研究结论.md``)→ 自动回复长期走「未就绪 → 人工队列」(V57)。
    """
    settings = getattr(runtime, "settings", None)
    sources = load_review_sources(settings=settings)
    submit = sources.get("submit") if isinstance(sources.get("submit"), dict) else {}
    api = submit.get("api") if isinstance(submit.get("api"), dict) else {}
    rpa = submit.get("rpa") if isinstance(submit.get("rpa"), dict) else {}
    url = str(api.get("url") or "").strip()
    flag = bool(submit.get("ready"))

    if not flag:
        return (
            False,
            "submit.ready=false(回复提交接口未捕获,见 docs/回复通道研究结论.md §4)"
            f";note={str(submit.get('note') or '')[:80]}",
        )
    if not url:
        rpa_note = "RPA 已启用但自动回复通道只认接口直连" if rpa.get("enabled") else "RPA 未启用"
        return False, f"submit.ready=true 但 submit.api.url 为空(端点未配置);{rpa_note}"
    return True, f"提交通道就绪:{str(api.get('method') or 'POST').upper()} {url}"


def auto_whitelist(config: dict[str, Any] | None = None) -> list[str]:
    """灰度店白名单(``auto.hotel_whitelist``,**顺序保留**,空表示无灰度店)。"""
    cfg = config if config is not None else load_review_config()
    raw = (cfg.get("auto") or {}).get("hotel_whitelist") or []
    out: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def is_auto_ready(config: dict[str, Any], sources: dict[str, Any], hotel_name: str) -> bool:
    """纯函数门控:``auto.enabled`` + 灰度店 + ``submit.ready``(旧 ``review_reply.py:162-173``)。

    ★ 与 :func:`submit_ready` 的区别:本函数是**纯判定**(配置对象进、布尔出),
    给单测与 ``decide_action`` 用;``submit_ready`` 是**带原因文案的运行时读数**。
    两者的 ``ready`` 语义必须一致 —— 所以这里同样要求 ``submit.api.url`` 非空。
    """
    auto = config.get("auto") or {}
    if not auto.get("enabled"):
        return False
    if str(hotel_name or "") not in [str(h) for h in (auto.get("hotel_whitelist") or [])]:
        return False
    submit = sources.get("submit") or {}
    if not submit.get("ready"):
        return False
    api = submit.get("api") or {}
    return bool(str(api.get("url") or "").strip())


def warn_legacy_only_sentiment(config: dict[str, Any]) -> str:
    """读出 ``auto.only_sentiment`` 并对历史值出声。

    旧配置写 ``"both"``(差评也自动回),但**段2 口径固定「差评一律不进自动」**
    (计划书 §5.8 / 附录 D;差评的店级 ``silent`` 是甲方业务决定)。
    本函数只负责**让这件事可见**,不改变行为。
    """
    mode = str((config.get("auto") or {}).get("only_sentiment") or "good").strip().lower()
    if mode not in ("good", "both"):
        logger.warning("auto.only_sentiment={!r} 非法,按 good 处理", mode)
        return "good"
    if mode == "both":
        logger.warning(
            "auto.only_sentiment=both(旧配置):段2 口径固定「差评一律不进自动」,"
            "该配置**不改变行为**(差评由店级 silent/template 决定是否出草稿)"
        )
    return mode


def audit_enums() -> dict[str, tuple[str, ...]]:
    """回传审计枚举(供自测/CLI 展示,避免消费方硬编码字符串)。"""
    return {"status": REPLY_STATUSES, "exec_by": REPLY_EXECUTORS, "sentiment": REVIEW_SENTIMENTS}
