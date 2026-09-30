"""预警附图(T2E.6)—— ``alert_shots.json`` 的 5 个目标 + **失败仅告警**。

★ V53 就是验这一条
==================

**截图失败 → 只告警,文本照发。** 图片是"锦上添花",不是触发的必要条件:
为了截一张图而整条预警不发,是比"没配图"严重得多的故障。
所以本模块**不抛异常**(``Screenshoter.shot_url`` 本身也是失败返回 ``None``,
见段1 ``domains/collect/screenshot.py:1022-1024``),任何异常都收敛成 ``None``。

哪些规则**没有图**(旧系统同,不是缺陷)
========================================

``config/alert_shots.json`` 只配了 5 个目标:``hot_event_price`` / ``channel_below_mean`` /
``home_pending`` / ``room_closed_7d`` / ``room_closed_today``(后者已退役)。
**F(``city_heat_remind``)与固定线(``price_line_optional``)本来就没有配图**
→ 查不到配置返回 ``None`` 是**正常路径**,只记 ``debug``,不当失败。

进程内缓存
==========

键 ``(rule_id, hotel_id, url)``:同日同店同规则的图**只截一次**。
一次巡检里同一家店可能命中多条规则(甚至多次调用),而 ``alert.data`` 与
``alert.room`` 是两个 job —— 缓存把"同一天重复截图"挡在进程内,
不落盘、不进库(截图文件本身由段1 的 ``var/screenshots/<店>/<日期>/`` 管理)。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from loguru import logger

from hoteldata.settings import Settings

__all__ = [
    "CACHE",
    "SHOTS_FILENAME",
    "capture",
    "clear_cache",
    "load_shots",
    "shot_target",
    "shots_path",
]

#: 进程内缓存:``{(rule_id, hotel_id, url): 相对路径 | None}``
CACHE: dict[tuple[str, int, str], str | None] = {}

#: 附图目标配置文件名(``config/`` 下,与 ``alert_rules.json`` 同级)
SHOTS_FILENAME = "alert_shots.json"


def shots_path(
    settings: Settings | None = None, *, config_dir: Path | str | None = None
) -> Path:
    """``config/alert_shots.json`` 路径(与 ``rules.rules_path`` 同源解析)。"""
    from hoteldata.domains.alert.rules import config_dir_of

    return config_dir_of(settings, config_dir) / SHOTS_FILENAME


def load_shots(
    *, settings: Settings | None = None, config_dir: Path | str | None = None
) -> dict[str, dict[str, str]]:
    """``alert_shots.json`` → ``{rule_id: {"url":..., "name":...}}``。

    缺失 / 非法 → ``{}``(附图是可选增强,**不是**触发条件);F 规则与固定线**无图**
    (旧系统同),查不到就是 ``None``,属正常。

    ★ 本函数的读取归 ``shots.py``(而不是 ``rules.py``):它是本配置**唯一的消费者**。
    """
    path = shots_path(settings, config_dir=config_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("预警附图配置不可用:{} ({})", path, exc)
        return {}
    targets = (data or {}).get("targets") if isinstance(data, dict) else None
    if not isinstance(targets, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for rule_id, cfg in targets.items():
        if isinstance(cfg, dict) and cfg.get("url"):
            out[str(rule_id)] = {"url": str(cfg["url"]), "name": str(cfg.get("name") or rule_id)}
    return out


def shot_target(
    rule_id: str, *, settings: Settings | None = None, config_dir: Path | str | None = None
) -> dict[str, str] | None:
    """单条规则的附图目标(无配置 → ``None``)。"""
    return load_shots(settings=settings, config_dir=config_dir).get(rule_id)


def clear_cache() -> int:
    """清空缓存(命令「预警测试」重跑时用)。返回清掉的条数。"""
    count = len(CACHE)
    CACHE.clear()
    return count


async def capture(
    runtime: Any,
    trigger: Any,
    *,
    cache: dict[tuple[str, int, str], str | None] | None = None,
    day: date | None = None,
) -> str | None:
    """给一条 Trigger 截一张现场图;返回**相对路径**,任何失败返回 ``None``。

    * 无 ``alert_shots.json`` 配置 → ``None``(F 与固定线的正常路径);
    * 浏览器池不可用 → ``logger.warning`` + ``None``(文本照发);
    * 截图异常 → ``logger.warning`` + ``None``(★ V53);
    * 命中缓存 → 直接返回缓存值(含缓存的 ``None``,避免对同一个坏 URL 反复重试)。
    """
    rule_id = str(getattr(trigger, "rule_id", "") or "")
    hotel_id = int(getattr(trigger, "hotel_id", 0) or 0)
    settings = getattr(runtime, "settings", None)
    target = shot_target(rule_id, settings=settings)
    if not target:
        logger.debug("预警附图:规则 {} 无附图配置(F 与固定线无图属正常)", rule_id)
        return None

    url = target["url"]
    name = target["name"]
    store = CACHE if cache is None else cache
    key = (rule_id, hotel_id, url)
    if key in store:
        logger.debug("预警附图:命中缓存 {} → {}", key, store[key])
        return store[key]

    try:
        from hoteldata.domains.collect.screenshot import Screenshoter  # 段1 能力(只读)
        from hoteldata.infra.models import Account, Hotel

        if getattr(runtime, "browser", None) is None:
            await runtime.start_browser()
        if getattr(runtime, "browser", None) is None:  # pragma: no cover - 池启动失败
            logger.warning("预警附图:浏览器池不可用,规则 {} 跳过截图(文本照发)", rule_id)
            store[key] = None
            return None

        async with runtime.db.session() as s:
            hotel = await s.get(Hotel, hotel_id)
            account = None
            if hotel is not None and hotel.account_id:
                account = await s.get(Account, hotel.account_id)
        if hotel is None:
            logger.warning("预警附图:酒店不存在 hotel_id={},跳过截图", hotel_id)
            store[key] = None
            return None

        ctx = runtime.extract_context(hotel, account, day or date.today())
        shooter = Screenshoter(
            settings=runtime.settings,
            rules=getattr(runtime, "rules", None),
            pool=runtime.browser,
        )
        shot = await shooter.shot_url(ctx, url, name)
        if not shot:
            logger.warning("预警附图:规则 {} 截图失败(shot_url=None),文本照发", rule_id)
        else:
            logger.success("预警附图:规则 {} 截图成功 {}", rule_id, shot)
        store[key] = shot
        return shot
    except Exception as exc:  # noqa: BLE001 - ★ V53:附图失败只告警,绝不阻断文本推送
        logger.warning("预警附图:规则 {} 截图异常({}),文本照发", rule_id, exc)
        store[key] = None
        return None
