"""T2G.1(前半)自检**推送壳** —— 消费段1 指标,组织文案,推运维群。

**为什么不在这里重写聚合**
==========================

段1 的 :mod:`hoteldata.domains.ops.selfcheck` **只出指标、不推送**(段1 §1.3 的边界),
它的 :class:`~hoteldata.domains.ops.selfcheck.SelfCheckReport` 就是"灰度观察表"的列
(B26:旧 ``app/selfcheck.py:22-29`` 的字段)。本模块**只读复用**它的
:func:`~hoteldata.domains.ops.selfcheck.run`,**不重复写任何聚合口径** ——
两处口径必然漂移,而自检数字是运维判断的唯一依据。

文案结构逐字对照旧 ``app/selfcheck.py:97-115`` 的 ``build_markdown``
(✅ **每日自检** / > 时间 / 磁盘 / 账号·酒店 / 机器人 / 昨日推送 / 昨日采集 /
登录失效(24h) / 今日已推 / --- / 数据来源)。旧文案里取自**旧库**的三项在新架构换源,
但**行文与顺序不动**:

==========================  ==========================================================
旧指标(旧 ``selfcheck.py``)   新来源(段2)
==========================  ==========================================================
``bots_online`` / ``bots_total``  :meth:`BotManager.health` / :meth:`BotManager.size`
``push_yesterday_*``               :meth:`PushAudit.day_stats`(昨日 ``slot`` 前缀)
``login_expired_24h``              ``ops_login_events`` 近 24h(``result='fail'``/失效动作)
==========================  ==========================================================

★★ D1 修复:告警**必须走 ``runtime.push.send_alert``**
====================================================

旧系统 ``app/__init__.py:196`` 的 ``set_bot(None)`` 让 ``pusher.py:42`` 的
``send_alert`` **恒返回 False** —— 多机器人模式下**登录失效、机器人掉线等告警根本发不出去**
(段2 §4.4 缺陷 D1,风险 P3「出事了没人知道」)。

本模块因此:

  1. 只调 :meth:`hoteldata.push.service.PushService.send_alert`,**不碰任何单例**;
  2. 消费它返回的 :class:`~hoteldata.domains.bot.manager.AlertResult`(**逐群结果**,
     不是裸 ``bool``)—— ``delivered`` / ``failed`` 全部进返回值与日志;
  3. ``OPS_CHATID`` 未配置时 **明确 warning + 返回 ``{"pushed": 0, "error": "未配置 OPS_CHATID"}``**,
     **绝不静默**(静默失败 0 次/周是段2 的头号质量目标)。

未配置机器人 / 一台都不在线时 ``AlertResult.ok=False`` 且带 ``error`` ——
本模块会 ``logger.error`` 并把 ``error`` 原样带回,不吞。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import func, select

from hoteldata.infra.models import LoginEvent

__all__ = ["build_markdown", "push_report", "run_and_push"]


def _disk_line(disk: dict[str, Any]) -> str:
    free = disk.get("free_gb")
    text = f"- 磁盘可用:**{free} GB**"
    if disk.get("below_min_free"):
        text += " ⚠️ 低于阈值!"
    if disk.get("error"):
        text += f" ⚠️ 探测失败({str(disk['error'])[:40]})"
    return text


def _bots_line(bots: dict[str, Any]) -> str:
    total = int(bots.get("total") or 0)
    online = int(bots.get("online") or 0)
    text = f"- 机器人:**{online}/{total}** 在线"
    if not total:
        text += " ⚠️ 无机器人实例(检查 core_bots / AIBOT_ENABLED)"
    elif online != total:
        text += " ⚠️ 有掉线(告警已发)"
    return text


def build_markdown(
    report: dict[str, Any],
    *,
    bots: dict[str, Any] | None = None,
    push_stats: dict[str, Any] | None = None,
    login_expired_24h: int | None = None,
) -> str:
    """自检文案(旧 ``app/selfcheck.py:97-115`` 的结构逐字,数据源换新库)。"""
    disk = report.get("disk") or {}
    accounts = report.get("accounts") or {}
    hotels = report.get("hotels") or {}
    sessions = report.get("sessions") or {}
    collect = report.get("collect_yesterday") or {}
    jobs = report.get("jobs_today") or {}
    stats = push_stats or {}
    bot_info = bots or {}

    push_ok = int(stats.get("ok") or 0)
    push_failed = int(stats.get("failed") or 0)
    push_total = push_ok + push_failed
    push_rate = float(stats.get("rate") or 0.0)
    collect_ok = int(collect.get("ok") or 0) + int(collect.get("no_data") or 0)
    collect_total = int(collect.get("total") or 0)
    expired = sessions.get("need_renewal", 0) if login_expired_24h is None else login_expired_24h

    lines = [
        "✅ **每日自检**",
        f"> 时间:{str(report.get('checked_at') or '')[:19]}",
        _disk_line(disk),
        f"- 账号/酒店:`{accounts.get('by_status', {}).get('active', 0)}/{accounts.get('total', 0)}` 活跃"
        f"|酒店 `{hotels.get('by_status', {}).get('active', 0)}`",
        _bots_line(bot_info),
        f"- 昨日推送:{push_ok}/{push_total} 成功(**{push_rate:.1f}%**)",
        f"- 昨日采集:{collect_ok}/{collect_total} 成功"
        f"(**{float(collect.get('success_rate') or 0) * 100:.1f}%**,no_data 不算失败)",
        f"- 登录失效(24h):{expired}",
        f"- 今日任务:{jobs.get('total', 0)} 条"
        f"(失败 {jobs.get('by_status', {}).get('failed', 0)})",
        "---",
        "> 数据来源:段1 自检指标(ops.selfcheck)+ 推送审计(push_logs);灰度观察按周归档",
    ]
    for label in ("disk", "accounts", "hotels", "sessions", "collect_yesterday", "jobs_today"):
        payload = report.get(label)
        if isinstance(payload, dict) and payload.get("error"):
            lines.insert(-2, f"- ⚠️ {label} 指标异常:{str(payload['error'])[:80]}")
    for note in report.get("notes") or []:
        lines.insert(-2, f"- ℹ️ {note}")
    return "\n".join(lines)


async def _bot_health(runtime: Any) -> dict[str, Any]:
    """机器人在线情况(``BotManager.health() -> {名字: bool}``,D18 的统一契约)。"""
    info: dict[str, Any] = {"total": 0, "online": 0, "health": {}, "note": ""}
    manager = getattr(runtime, "bots", None)
    if manager is None:  # pragma: no cover - runtime 一定装配了 bots 属性
        info["note"] = "BotManager 未装配"
        logger.error("自检:BotManager 未装配,机器人指标不可用(不静默)")
        return info
    try:
        health = dict(manager.health())
    except Exception as exc:  # noqa: BLE001 - 自检不许因单个指标崩
        info["note"] = f"health 读取失败: {exc}"
        logger.error("自检:机器人健康读取失败 {}", exc)
        return info
    info["health"] = {str(k): bool(v) for k, v in health.items()}
    info["total"] = int(manager.size()) if hasattr(manager, "size") else len(health)
    info["online"] = sum(1 for value in health.values() if value)
    if not info["total"]:
        info["note"] = "无机器人实例(网关未启动或 core_bots 为空)"
        logger.warning("自检:机器人实例为 0,推送会失败(检查 AIBOT_ENABLED / core_bots)")
    return info


async def _login_expired_24h(runtime: Any) -> int:
    """近 24h 登录失效数(``ops_login_events``;旧 ``selfcheck.py:80-85`` 同口径)。

    ``created_at`` 是 ``timestamptz`` —— 界限值必须带时区(用 ``settings.tzinfo``),
    传裸 ``datetime`` 会被 asyncpg 拒(旧库是 TEXT,没有这个坑)。
    """
    since = datetime.now(runtime.settings.tzinfo) - timedelta(days=1)
    stmt = select(func.count()).select_from(LoginEvent).where(
        LoginEvent.created_at >= since,
        LoginEvent.result != "ok",
    )
    try:
        async with runtime.db.session() as session:
            return int(await session.scalar(stmt) or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("自检:登录失效计数失败 {}", exc)
        return 0


async def push_report(runtime: Any, report: Any = None) -> dict[str, Any]:
    """把自检报告组织成文案并推 ``OPS_CHATID``;返回**推送结果 dict**。

    ``report=None`` 时自己调段1 的 ``run()``(``jobs.py`` 走的是"指标已算好、
    只推一次"的形态,所以本函数**接受外部报告**,不重复聚合)。

    ★ 受 ``settings.ops_push.selfcheck_push_enabled`` 控制:关闭 → **只返回指标**,
    不推送(也不报错)。
    """
    settings = runtime.settings
    out: dict[str, Any] = {
        "enabled": bool(settings.ops_push.selfcheck_push_enabled),
        "pushed": 0,
        "targets": [],
        "delivered": [],
        "failed": [],
        "error": None,
        "markdown": "",
    }
    if report is None:
        from hoteldata.domains.ops.selfcheck import run as run_selfcheck

        report = await run_selfcheck(settings, db=runtime.db)
    data = report.as_dict() if hasattr(report, "as_dict") else dict(report or {})

    bots = await _bot_health(runtime)
    yesterday = date.today() - timedelta(days=1)
    try:
        push_stats = await runtime.push.audit.day_stats(yesterday)
    except Exception as exc:  # noqa: BLE001
        logger.warning("自检:昨日推送审计读取失败 {}", exc)
        push_stats = {"ok": 0, "failed": 0, "skipped": 0, "total": 0, "rate": 0.0}
    expired = await _login_expired_24h(runtime)

    markdown = build_markdown(
        data, bots=bots, push_stats=push_stats, login_expired_24h=expired
    )
    out["markdown"] = markdown
    out["bots"] = bots
    out["push_stats_yesterday"] = push_stats
    out["login_expired_24h"] = expired

    ops_chatid = str(settings.push.ops_chatid or "").strip()
    if not ops_chatid:
        # ★ 明确 warning + 明确 error 字段,绝不静默(旧系统在这里"什么都没发生")
        logger.warning("自检消息未推送:未配置 OPS_CHATID(配置后生效);内容:{}", markdown.replace("\n", " ")[:150])
        out["error"] = "未配置 OPS_CHATID"
        return out
    if not out["enabled"]:
        logger.info("自检消息未推送:OPS_SELFCHECK_PUSH=0(只返回指标);内容:{}", markdown.replace("\n", " ")[:150])
        out["error"] = "OPS_SELFCHECK_PUSH=0(推送关闭)"
        return out

    out["targets"] = [ops_chatid]
    # ★ D1 修复:走 PushService.send_alert(遍历在线机器人,返回逐群结果)
    result = await runtime.push.send_alert(markdown, chatids=[ops_chatid])
    delivered = [str(x) for x in getattr(result, "delivered", []) or []]
    failed = [(str(t), str(e)) for t, e in (getattr(result, "failed", []) or [])]
    out["delivered"] = delivered
    out["failed"] = [{"target": t, "error": e} for t, e in failed]
    out["error"] = getattr(result, "error", None)
    out["pushed"] = len(delivered)
    if out["pushed"]:
        logger.info("自检消息已推送运维群:{} 个目标", out["pushed"])
    else:
        logger.error(
            "自检消息未送达任何运维群:error={} targets={} failed={}",
            out["error"] or "全部失败",
            out["targets"],
            out["failed"],
        )
    return out


async def run_and_push(runtime: Any) -> dict[str, Any]:
    """``ops.selfcheck`` 的完整入口:段1 指标 → 文案 → 推运维群。

    返回 ``{"ok", "metrics", "markdown", "push"}``;``push`` 就是
    :func:`push_report` 的结果(``pushed=0`` 时**必定**带 ``error`` 说明原因)。

    ``ok`` 的算法**不撒谎**:指标无错 **且**(推送关闭 **或** 至少一个群收到)。
    "推送开着、却一条都没发出去"必须体现在 ``ok=False`` 上(静默失败 0 次/周)。
    """
    from hoteldata.domains.ops.selfcheck import run as run_selfcheck

    report = await run_selfcheck(runtime.settings, db=runtime.db)
    metrics = report.as_dict()
    push = await push_report(runtime, report)
    push_ok = (not push["enabled"]) or push["pushed"] > 0
    return {
        "ok": bool(report.ok) and push_ok,
        "metrics": metrics,
        "markdown": push.get("markdown", ""),
        "push": push,
    }
