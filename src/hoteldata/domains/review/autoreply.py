"""T2F.3 自动回复(**门控**)—— 旧 ``review_reply.py:378-646`` 的门控与失败口径逐字继承。

★★ 口径 ⑤:就绪 = 三重门控,缺一不可
====================================

``settings.review.auto_enabled``(``REVIEW_AUTO_ENABLED``)
  **且** ``review_templates.json`` 的 ``auto.enabled``
  **且** 店名在灰度白名单 ``auto.hotel_whitelist``
  **且** ``review_sources.json`` 的 ``submit.ready`` + ``submit.api.url``
  (见 :func:`hoteldata.domains.review.policy.submit_ready`)。

⚠️ **现实提醒(V57,计划书 §5.8 逐字)**
------------------------------------

点评的**提交接口从未被捕获**(旧 ``docs/回复通道研究结论.md``),
所以 **``submit.ready`` 当前必然为 false** —— 自动回复**在可预见的时间内一直走
「未就绪 → 人工队列」**。这不是 bug,是已知边界。本模块对该路径的要求是:

1. **明确提示**:``logger.warning`` 逐店打出原因,汇总里带
   ``submit_ready=False`` + ``not_ready_reason`` + ``queued_manual`` 计数;
2. **进人工队列**:为每条好评落 ``suggested`` 审计行(``exec_by='draft'``),
   和草稿流程走同一条人工通道;
3. ★ **绝不伪造成功**:未就绪路径**不写** ``status='ok'``、**不动** ``reviews.replied``、
   不调用任何提交通道 —— 汇总里 ``replied`` 恒为 0。

就绪路径(通道补抓包之后才会走到)按 §5.8 表:

* **只处理 ``good``**;差评一律不进自动(店级 ``silent`` 由 ``draft`` 流程落 ``ignored``);
* **先落审计行**(``status='ok'``, ``exec_by='auto'``)**再调提交通道**
  —— 崩在中间时至少留下"打算回复什么"的证据;提交失败再追加一行
  ``status='failed'`` / ``strategy='auto_failed'`` / ``exec_by='auto_failed'``;
* **整店间隔 ≥ ``rules.reply_interval_s``(120 秒)**:用 :func:`asyncio.sleep` 兑现
  (不是像旧系统那样直接跳过 —— 计划书要的是"间隔 ≥120 秒"这个**保证**);
* **失败当日不重试**:失败行 + ``replied=0`` 让点评**留在待回复池**(口径 ②),
  但下一次运行会因"已有审计行"被跳过,不会反复打平台接口。

★★ 口径 ②:失败 = ``failed`` + ``auto_failed`` + **``replied=0``**
===============================================================

``failed`` 是**技术失败**,点评仍在等待处理(留在待回复池);
把它标成 ``replied=1`` 会让点评**永远消失**。V56 专测这条 ——
实现位置就是本模块 :func:`autorun` 的失败分支(调用
:func:`mark_replied` 时显式传 ``replied=0``)。

★ ``reviews.replied`` / ``reviews.strategy`` **只能由 :func:`mark_replied` 写**
================================================================================

这两列是"回复流程的状态",采集侧的 UPSERT **不回溯**它们
(迁移 ``0002_batch_d_extractors`` 的两条独立机制)。全工程唯一的写入口就是
:func:`mark_replied` —— 集中在一处,才不会出现"某个角落把 replied 刷回 0/1"。
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

from loguru import logger
from sqlalchemy import func, select, update

from hoteldata.infra.models import Hotel, ReviewReply, ReviewReview

from .draft import check_drafted, load_pending_reviews, write_audit_row
from .policy import (
    auto_whitelist,
    effective_policy,
    load_review_config,
    resolve_hotel,
    submit_ready,
    warn_legacy_only_sentiment,
)
from .templates import render_reply

__all__ = [
    "ReplyChannelError",
    "ReplyChannelNotReady",
    "autorun",
    "mark_replied",
    "submit_via_api",
]


class ReplyChannelError(RuntimeError):
    """回复通道不可用 / 平台拒答(自动回复按"失败 → 人工队列"处理)。"""


class ReplyChannelNotReady(ReplyChannelError):
    """通道未就绪(``submit.ready=false`` 或端点未配置)。"""


# ---------------------------------------------------------------------------
# ★ 唯一允许写 reviews.replied / reviews.strategy 的地方
# ---------------------------------------------------------------------------


async def mark_replied(
    runtime: Any,
    review_pk: Any,
    *,
    strategy: str,
    exec_by: str,
    replied: int = 1,
) -> None:
    """写 ``review_reviews.replied`` / ``strategy``(**全工程唯一入口**)。

    ``replied=1``:已回复(``ok``)或业务决定不回复(``ignored``/``silent``)
    —— 两者都算**已处理完**,不再进待回复池。

    ``replied=0`` 只用于**自动回复失败**:``strategy='auto_failed'``,
    点评**留在待回复池**(口径 ②;``exec_by`` 只进日志,不落 reviews 表 ——
    该表没有这一列,执行者证据在 ``review_replies.exec_by`` 上)。
    """
    pk = int(review_pk)
    async with runtime.db.session() as session:
        result = await session.execute(
            update(ReviewReview)
            .where(ReviewReview.id == pk)
            .values(replied=int(replied), strategy=str(strategy))
        )
    if not result.rowcount:
        logger.error("点评 replied 更新命中 0 行:pk={}(点评可能已被删除)", pk)
    logger.info("点评 #{} replied={} strategy={} by={}", pk, int(replied), strategy, exec_by)


# ---------------------------------------------------------------------------
# 提交通道(接口直连;RPA 未实现)
# ---------------------------------------------------------------------------


def _render_body(body: Any, subs: dict[str, str]) -> Any:
    """递归替换 ``body_template`` 里的 ``{comment_id}`` / ``{reply_token}`` / ``{content}``。

    逐字继承旧 ``ApiReplyChannel._render_body``(旧 ``review_reply.py:399-416``):
    字符串替换、list/dict 递归、其余原样。
    """
    if isinstance(body, str):
        out = body
        for key, value in subs.items():
            out = out.replace("{" + key + "}", str(value or ""))
        return out
    if isinstance(body, list):
        return [_render_body(item, subs) for item in body]
    if isinstance(body, dict):
        return {key: _render_body(value, subs) for key, value in body.items()}
    return body


async def submit_via_api(runtime: Any, review: Any, content: str, hotel: Any) -> dict[str, Any]:
    """接口直连提交(端点/body 模板取 ``review_sources.json submit.api``)。

    平台常见封装 ``rcode != 0`` 视为失败(旧 ``review_reply.py:452-455`` 逐字)。
    ★ ``reply_token`` 当前**没有来源**(提交接口未捕获,平台回复页的 token 也没抓过),
    所以 ``body_template`` 里若引用它,发出去的是空串 —— 这是**已知缺口**,
    属于"补抓包"任务的一部分(``submit.ready`` 未就绪时本函数根本不会被调用)。
    """
    from .policy import load_review_sources

    sources = load_review_sources(settings=getattr(runtime, "settings", None))
    submit = sources.get("submit") or {}
    if not submit.get("ready"):
        raise ReplyChannelNotReady("submit.ready=false(提交接口未捕获,见 docs/回复通道研究结论.md §4)")
    api = submit.get("api") or {}
    url = str(api.get("url") or "").strip()
    if not url:
        raise ReplyChannelNotReady("submit.api.url 为空(接口直连端点未配置)")
    if (submit.get("rpa") or {}).get("enabled") and not url:
        raise ReplyChannelNotReady("RPA 通道未实现(旧 docs/回复通道研究结论.md §2)")

    subs = {
        "comment_id": str(getattr(review, "review_id", "") or ""),
        "reply_token": str(getattr(review, "reply_token", "") or ""),
        "content": content,
        "hotel_id": str(getattr(hotel, "id", "") or ""),
        "hotel_name": str(getattr(hotel, "name", "") or ""),
    }
    payload = _render_body(api.get("body_template") or {}, subs)
    headers = {
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125 Safari/537.36"
        ),
        "referer": str(api.get("referer") or "https://ebooking.ctrip.com/comment/commentList?microJump=true"),
        "x-requested-with": "XMLHttpRequest",
        "content-type": "application/json;charset=UTF-8",
    }
    attempt = await runtime.http.request(
        str(api.get("method") or "POST"),
        url,
        headers=headers,
        json_body=payload,
    )
    if attempt.error:
        raise ReplyChannelError(f"提交请求失败: {url}({attempt.error})")
    if (attempt.status_code or 0) >= 400:
        raise ReplyChannelError(f"提交返回 {attempt.status_code}: {url}")
    data = None
    try:
        import json

        data = json.loads(attempt.text or "")
    except ValueError:
        data = None
    if isinstance(data, dict):
        rcode = data.get("rcode") if "rcode" in data else data.get("code")
        if rcode not in (None, 0, 200):
            raise ReplyChannelError(f"平台拒答 rcode={rcode}: {str(data.get('msg') or data)[:120]}")
    return {"ok": True, "status_code": attempt.status_code, "raw": data}


# ---------------------------------------------------------------------------
# 间隔控制(整店 ≥ rules.reply_interval_s)
# ---------------------------------------------------------------------------


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _last_auto_at(runtime: Any, hotel_id: int) -> datetime | None:
    """该店最近一次**成功的自动回复**时间(从 append-only 审计行里读,重启不丢)。

    旧系统把间隔状态写在 ``config/review_auto_state.json``(旧
    ``review_reply.py:489-516``)—— **可变状态不进 ``config/``**(``settings.py`` 禁令 2),
    新实现直接查 ``review_replies``:``status='ok' and exec_by='auto'`` 的最大 ``created_at``。
    """
    stmt = select(func.max(ReviewReply.created_at)).where(
        ReviewReply.hotel_id == int(hotel_id),
        ReviewReply.status == "ok",
        ReviewReply.exec_by == "auto",
    )
    async with runtime.db.session() as session:
        value = await session.scalar(stmt)
    return value if isinstance(value, datetime) else None


async def _pace(last_monotonic: float | None, interval: float) -> float:
    """距上次整店回复不足 ``interval`` 秒 → 睡够(返回实际等待秒数)。"""
    if last_monotonic is None or interval <= 0:
        return 0.0
    remaining = interval - (time.monotonic() - last_monotonic)
    if remaining <= 0:
        return 0.0
    wait = min(remaining, interval)
    logger.info("自动回复整店间隔控制:等待 {:.1f}s(阈值 {}s)", wait, interval)
    await asyncio.sleep(wait)
    return round(wait, 1)


# ---------------------------------------------------------------------------
# 候选与目标
# ---------------------------------------------------------------------------


async def _target_hotels(runtime: Any, hotels: Any) -> list[Hotel]:
    """目标酒店:显式传入 → 逐个归一;``None`` → 全部 ``active`` 酒店。"""
    if hotels is None:
        async with runtime.db.session() as session:
            rows = (
                (await session.execute(select(Hotel).where(Hotel.status == "active").order_by(Hotel.id)))
                .scalars()
                .all()
            )
        return list(rows)
    out: list[Hotel] = []
    for item in hotels:
        row = item if isinstance(item, Hotel) else await resolve_hotel(runtime, item)
        if row is None:
            logger.warning("自动回复目标酒店无法解析,跳过: {!r}", item)
            continue
        out.append(row)
    return out


async def _good_candidates(runtime: Any, hotel: Hotel) -> list[Any]:
    """候选 = 待回复池里 ``sentiment == 'good'`` 且**尚未进过人工/自动流程**的点评。

    ★ **差评一律不进自动**(计划书 §5.8 / 附录 D):``bad`` 与 ``unknown``
    连候选都不进 —— 它们由草稿/人工通道处理。
    """
    out = []
    for review in await load_pending_reviews(runtime, hotel):
        if review.sentiment != "good":
            continue
        if await check_drafted(runtime, review.hotel_id, review.review_id):
            continue
        out.append(review)
    return out


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


async def autorun(runtime: Any, *, hotels: Any = None) -> dict[str, Any]:
    """``review.auto`` 任务:逐店自动回复(**仅好评、灰度店、通道就绪**)。

    返回的 dict **JSON 可序列化**(直接进 ``job_runs.summary``)::

        {"ok", "enabled", "hotels", "candidates", "replied", "failed",
         "not_ready", "queued_manual", "submit_ready", "not_ready_reason",
         "skipped_not_whitelisted", "skipped_interval", "waited_s",
         "replied_details", "notes", "errors"}
    """
    settings = runtime.settings
    out: dict[str, Any] = {
        "ok": True,
        "enabled": False,
        "hotels": 0,
        "candidates": 0,
        "replied": 0,
        "failed": 0,
        "not_ready": 0,
        "queued_manual": 0,
        "submit_ready": False,
        "not_ready_reason": "",
        "skipped_not_whitelisted": 0,
        "skipped_interval": 0,
        "waited_s": 0.0,
        "replied_details": [],
        "notes": [],
        "errors": [],
    }

    if not settings.review.auto_enabled:
        out["notes"].append("REVIEW_AUTO_ENABLED=0(自动回复总开关关闭)")
        logger.info("点评自动回复未启用(REVIEW_AUTO_ENABLED=0)")
        return out

    config = load_review_config(settings=settings)
    auto = config.get("auto") or {}
    if not auto.get("enabled"):
        out["notes"].append("review_templates.json 的 auto.enabled=false(灰度未开)")
        logger.info("点评自动回复未启用(auto.enabled=false)")
        return out
    warn_legacy_only_sentiment(config)

    ready, reason = submit_ready(runtime)
    out["enabled"] = True
    out["submit_ready"] = ready
    out["not_ready_reason"] = "" if ready else reason

    whitelist = auto_whitelist(config)
    if not whitelist:
        out["notes"].append("auto.hotel_whitelist 为空 → 无灰度店,自动回复不执行")
        logger.warning("点评自动回复未执行:auto.hotel_whitelist 为空(没有灰度店)")
        return out
    out["whitelist"] = whitelist

    rules = config.get("rules") or {}
    interval = float(rules.get("reply_interval_s", 120) or 120)
    targets = await _target_hotels(runtime, hotels)

    for hotel in targets:
        if str(hotel.name) not in whitelist:
            out["skipped_not_whitelisted"] += 1
            continue
        out["hotels"] += 1
        policy = await effective_policy(runtime, hotel)
        candidates = await _good_candidates(runtime, hotel)
        out["candidates"] += len(candidates)
        if not candidates:
            continue

        # ---------------- ⑤ 未就绪:明确提示 + 进人工队列(绝不伪造成功)----------------
        if not ready:
            queued = await _queue_manual(runtime, hotel, candidates, policy, config, reason)
            out["not_ready"] += queued
            out["queued_manual"] += queued
            logger.warning(
                "点评自动回复未就绪:「{}」{} 条好评已进人工队列(不伪造成功);reason={}",
                hotel.name,
                queued,
                reason,
            )
            continue

        # ---------------- 就绪:仅好评,先落审计行再提交,整店间隔 ≥120s ----------------
        last_at = await _last_auto_at(runtime, hotel.id)
        last_monotonic: float | None = None
        if last_at is not None:
            elapsed = (datetime.now(UTC) - _aware(last_at)).total_seconds()
            last_monotonic = time.monotonic() - max(0.0, elapsed)
        for review in candidates:
            waited = await _pace(last_monotonic, interval)
            out["waited_s"] = round(float(out["waited_s"]) + waited, 1)
            rendered = render_reply(policy, review, config, hotel_name=str(hotel.name))
            template_id = str(policy.get("good") or "g01")
            body = ""
            if rendered is not None:
                template_id, body = rendered
            if not body:
                await _mark_failed(
                    runtime,
                    hotel_id=int(hotel.id),
                    review=review,
                    strategy="auto_failed",
                    content="",
                    detail={"reason": f"template_missing:{template_id}"},
                )
                out["failed"] += 1
                out["errors"].append(f"#{review.pk} template_missing:{template_id}")
                last_monotonic = time.monotonic()
                continue

            # ★ 先落审计行(status='ok', exec_by='auto')—— 崩溃时留下"打算回复什么"的证据
            audit_id = await write_audit_row(
                runtime,
                hotel_id=int(hotel.id),
                review_id=review.review_id,
                status="ok",
                strategy=template_id,
                content=body,
                exec_by="auto",
                detail={
                    "stage": "pre_submit",
                    "channel": reason,
                    "review_pk": review.pk,
                    "star": review.star,
                    "note": "提交前落库;提交失败会追加 status=failed 行修正",
                },
            )
            try:
                receipt = await submit_via_api(runtime, review, body, hotel)
            except Exception as exc:  # noqa: BLE001 - 任何通道异常都按失败处理
                await _mark_failed(
                    runtime,
                    hotel_id=int(hotel.id),
                    review=review,
                    strategy="auto_failed",
                    content=body,
                    detail={
                        "reason": f"{type(exc).__name__}: {exc}"[:300],
                        "optimistic_audit_id": audit_id,
                        "template_id": template_id,
                    },
                )
                out["failed"] += 1
                out["errors"].append(f"#{review.pk} {type(exc).__name__}: {str(exc)[:160]}")
                logger.error("点评自动回复失败:店={} #{} {}", hotel.name, review.pk, exc)
                last_monotonic = time.monotonic()
                continue

            await mark_replied(runtime, review.pk, strategy=template_id, exec_by="auto")
            out["replied"] += 1
            out["replied_details"].append(
                {
                    "hotel_id": int(hotel.id),
                    "hotel": str(hotel.name),
                    "review_pk": review.pk,
                    "review_id": review.review_id,
                    "star": review.star,
                    "content": review.content[:60],
                    "body": body,
                    "template": template_id,
                    "receipt": receipt.get("status_code"),
                }
            )
            last_monotonic = time.monotonic()
            logger.info("点评自动回复成功:店={} #{} 模板={}", hotel.name, review.pk, template_id)

    if out["failed"]:
        out["ok"] = False
    if not ready:
        out["notes"].append(
            "自动回复通道未就绪:仅入人工队列(不写 status='ok'、不动 reviews.replied)"
        )
    logger.info(
        "点评自动回复汇总:灰度店 {} / 候选 {} / 已回 {} / 失败 {} / 未就绪入队 {}",
        out["hotels"],
        out["candidates"],
        out["replied"],
        out["failed"],
        out["queued_manual"],
    )
    return out


async def _queue_manual(
    runtime: Any,
    hotel: Hotel,
    candidates: list[Any],
    policy: dict[str, Any],
    config: dict[str, Any],
    reason: str,
) -> int:
    """未就绪路径:为每条好评落 ``suggested`` 审计行(**人工队列**)。

    ★ 只写 ``suggested``(``exec_by='draft'``,与草稿流程同一条通道),
    **不写 ok、不动 replied** —— 这正是 V57 要验的"明确提示 + 进人工队列,
    绝不伪造成功"。
    """
    queued = 0
    for review in candidates:
        body = ""
        template_id = str(policy.get("good") or "g01")
        rendered = render_reply(policy, review, config, hotel_name=str(hotel.name))
        if rendered is not None:
            template_id, body = rendered
        await write_audit_row(
            runtime,
            hotel_id=int(hotel.id),
            review_id=review.review_id,
            status="suggested",
            strategy=template_id,
            content=body,
            exec_by="draft",
            detail={
                "manual_queue": True,
                "reason": reason,
                "submit_ready": False,
                "review_pk": review.pk,
                "star": review.star,
                "note": "自动回复通道未就绪,转人工队列(绝不伪造成功)",
            },
        )
        queued += 1
    return queued


async def _mark_failed(
    runtime: Any,
    *,
    hotel_id: int,
    review: Any,
    strategy: str,
    content: str,
    detail: dict[str, Any],
) -> None:
    """失败收口:追加 ``failed`` 审计行 + ``replied=0``(口径 ②,点评留在待回复池)。"""
    await write_audit_row(
        runtime,
        hotel_id=hotel_id,
        review_id=review.review_id,
        status="failed",
        strategy=strategy,
        content=content,
        exec_by="auto_failed",
        detail={**detail, "review_pk": review.pk, "star": review.star},
    )
    await mark_replied(runtime, review.pk, strategy=strategy, exec_by="auto_failed", replied=0)
