"""T2F.4 点评分析日报 —— 四块纯数据(旧 ``app/review_analysis.py`` 逐字继承,V58)。

**四块内容(旧 ``review_analysis.py:155-171`` 的 ``render_analysis_md`` 逐字)**
==============================================================================

==============================  ==================================================  ==================
块                               数据源(``review_materials.kind``)                   旧实现行号
==============================  ==================================================  ==================
① 评分块                        ``score``(getCommentsScoreV2)                        ``100-116``
② 竞争圈关键词对比              ``competitor``(getCompetitorCommentStat)             ``119-139``
③ 趋势(近 6 月)               ``trend``(getCommentRateTrend)                       ``142-152``
④ 待回复快照                    本项目 ``review_reviews``(``replied != 1``)           ``163-168``
==============================  ==================================================  ==================

另外 ``num``(getCommentNumV2)的"待回复 X/共 Y 条、好评率"由①的 ``_fmt_score`` 一并渲染。

★ **无素材 → 返回 ``None``(跳过,不报错)**(V58 / 旧 ``build_hotel_analysis``
旧 ``review_analysis.py:185-186``):"数据没有"不是"任务失败"。**判空只看
score/competitor/trend 三块** —— 与旧实现**逐字一致**(只有 ``num`` 有数据时同样跳过)。

★ **不调用任何 LLM**(计划书 §3 / 旧模块 docstring:纯数据模板)。
渲染是 hardcode markdown,不引入模板引擎。

★ 推送口径(旧 ``run_review_analysis_all`` 旧 ``review_analysis.py:191-263``):
每店**绑定群**(``bindings.grouped()``,已过滤 ``paused``)+ ``push_type='review_analysis'``;
**当日 slot 去重由推送派发器做**(``PushTask.effective_slot()`` = ``YYYY-MM-DD-HH``,
``push/dispatcher.py`` 的 ``_is_duplicated``),本模块**不重复实现一遍去重** ——
否则会出现第二个去重来源,两处判定不一致时就会漏推或重推。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from loguru import logger
from sqlalchemy import select

from hoteldata.infra.models import Hotel, ReviewMaterial
from hoteldata.push.service import BuiltMessage

from .draft import pending_stats
from .policy import load_review_config, resolve_hotel

__all__ = [
    "build_analysis",
    "build_payload",
    "publish_analysis",
    "render_analysis_md",
]

#: 趋势块展示的月份数(旧 ``_fmt_trend`` 的 ``data[-6:]``)
TREND_MONTHS = 6
#: ``push_type``(A2-7 命名)
PUSH_TYPE = "review_analysis"


def _norm_date(day: Any) -> date:
    """归一采集日(``None`` → 今天;``date``/``datetime``/ISO 串都接受)。"""
    if day is None:
        return date.today()
    if isinstance(day, date) and not hasattr(day, "hour"):
        return day
    text = str(day)
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        logger.warning("点评分析采集日非法({!r}),按今天处理", day)
        return date.today()


# ---------------------------------------------------------------------------
# 素材聚合(旧 ``build_analysis_payload`` 旧 ``review_analysis.py:38-97``)
# ---------------------------------------------------------------------------


async def _latest_material(
    runtime: Any,
    hotel_id: int,
    kind: str,
    *,
    before: date | None = None,
) -> dict[str, Any] | None:
    """取某类素材的**最新一条**(可选"早于某日"界限,旧 ``latest_review_material``)。

    ⚠️ 段1 的 ``CollectRepository`` **没有** ``latest_review_material``
    (只有 ``latest_module`` / ``list_reviews`` / ``list_portal_columns`` / ``list_room_states``),
    所以这里在本域内直接读 ``review_materials`` —— 它是**批次 F 自己的素材表**,
    不违反"域之间不 join 别人的表"。
    """
    stmt = select(ReviewMaterial).where(
        ReviewMaterial.hotel_id == int(hotel_id),
        ReviewMaterial.kind == str(kind),
    )
    if before is not None:
        stmt = stmt.where(ReviewMaterial.collect_date < before)
    stmt = stmt.order_by(ReviewMaterial.collect_date.desc(), ReviewMaterial.id.desc()).limit(1)
    async with runtime.db.session() as session:
        row = (await session.execute(stmt)).scalars().first()
    if row is None:
        return None
    return {
        "collect_date": row.collect_date,
        "status": row.status,
        "channel": row.channel,
        "payload": row.payload_json if isinstance(row.payload_json, dict) else {},
    }


async def build_payload(runtime: Any, hotel_row: Any, day: Any = None) -> tuple[dict[str, Any], list[str]]:
    """聚合单店当日素材 → ``(payload, notes)``(素材缺项记 ``notes``,**不抛**)。"""
    target = _norm_date(day)
    payload: dict[str, Any] = {"source_date": target.isoformat()}
    notes: list[str] = []
    hotel_id = int(getattr(hotel_row, "id", hotel_row))

    score = await _latest_material(runtime, hotel_id, "score")
    competitor = await _latest_material(runtime, hotel_id, "competitor")
    trend = await _latest_material(runtime, hotel_id, "trend")
    num = await _latest_material(runtime, hotel_id, "num")
    if score is None:
        notes.append("score 素材缺失")
    else:
        payload["scores"] = score["payload"]
        payload["source_date"] = str(score["collect_date"])
    if competitor is None:
        notes.append("competitor 素材缺失")
    else:
        payload["competitor"] = competitor["payload"]
    if trend is None:
        notes.append("trend 素材缺失")
    else:
        payload["trend"] = trend["payload"]
    if num is None:
        notes.append("num 素材缺失")
    else:
        payload["num"] = num["payload"]

    rules = (load_review_config(settings=getattr(runtime, "settings", None)).get("rules")) or {}
    stats = await pending_stats(runtime, hotel_row)
    payload["today_reviews"] = {
        "pending_total": stats["pending"],
        "pending_by_sentiment": stats["by_sentiment"],
        "thresholds": {
            "good_min_star": rules.get("good_min_star", 4),
            "bad_max_star": rules.get("bad_max_star", 3),
        },
    }
    return payload, notes


# ---------------------------------------------------------------------------
# 渲染(旧 ``_fmt_score`` / ``_fmt_competitor`` / ``_fmt_trend`` 逐字)
# ---------------------------------------------------------------------------


def _fmt_score(payload: dict[str, Any]) -> str:
    """① 评分块(旧 ``review_analysis.py:100-116`` 逐字,含 ``num`` 的待回复/好评率)。"""
    scores = payload.get("scores") or {}
    parts: list[str] = []
    if scores.get("ctripRatingall") is not None:
        parts.append(f"携程 {scores['ctripRatingall']} 分")
    if scores.get("qunarRatingall") is not None:
        parts.append(f"去哪 {scores['qunarRatingall']} 分")
    if scores.get("ctripRatingAllRanking") is not None:
        parts.append(
            f"携程排位 {scores['ctripRatingAllRanking']}/{scores.get('competitorHotelTotal') or '?'}"
        )
    if scores.get("responseRate") is not None:
        parts.append(f"回复率 {round(float(scores['responseRate']) * 100, 1)}%")
    num = payload.get("num") or {}
    cnum = num.get("ctripCount") or {}
    if cnum:
        parts.append(
            f"待回复 {cnum.get('unReplyCount', 0)}/共 {cnum.get('commentCount', 0)} 条"
            f"(好评率 {round(float(cnum.get('goodRate') or 0) * 100, 1)}%)"
        )
    return "  评分:" + " / ".join(parts) if parts else "  评分:无素材"


def _fmt_competitor(payload: dict[str, Any]) -> list[str]:
    """② 竞争圈关键词对比(旧 ``review_analysis.py:119-139`` 逐字,含平台建议)。"""
    competitor = payload.get("competitor") or {}
    out: list[str] = []
    good = competitor.get("goodCompetitorCommentStatItemVo") or {}
    names = good.get("itemNameList") or []
    mine = good.get("myHotelQuantityList") or []
    others = good.get("competitorQuantityList") or []
    if names:
        lines = []
        for index, name in enumerate(names):
            my_value = mine[index] if index < len(mine) else "-"
            other_value = others[index] if index < len(others) else "-"
            lines.append(f"    · {name}:本店 {my_value} / 竞争圈 {other_value}")
        out.append("  好评关键词对比(本店 vs 竞争圈):")
        out.extend(lines[:8])
    for strategy in (competitor.get("suggestStrategies") or [])[:2]:
        if isinstance(strategy, dict) and strategy.get("content"):
            out.append(
                "  平台建议:"
                f"{str(strategy['content']).replace('<strong>', '').replace('</strong>', '')[:180]}"
            )
    if not out:
        out.append("  竞争圈对比:无素材")
    return out


def _fmt_trend(payload: dict[str, Any]) -> list[str]:
    """③ 趋势块(旧 ``review_analysis.py:142-152`` 逐字,近 6 月)。"""
    trend = payload.get("trend") or {}
    data = trend.get("data") if isinstance(trend, dict) else trend
    if not isinstance(data, list) or not data:
        return []
    lines = [f"  评分趋势(近 {len(data)} 个月):"]
    for item in data[-TREND_MONTHS:]:
        if isinstance(item, dict):
            lines.append(
                f"    · {item.get('errivalDate')}: 点评 {item.get('comments') or 0} 条,"
                f"点评率 {item.get('commentsRate') or 0}"
            )
    return lines


def render_analysis_md(hotel_name: str, payload: dict[str, Any]) -> str:
    """渲染四块正文(旧 ``render_analysis_md`` 旧 ``review_analysis.py:155-171`` 逐字)。"""
    source_date = payload.get("source_date") or ""
    lines = [f"📊 点评分析日报「{hotel_name}」 {source_date}"]
    lines.append(_fmt_score(payload))
    lines.extend(_fmt_competitor(payload))
    lines.extend(_fmt_trend(payload))
    reviews = payload.get("today_reviews") or {}
    if reviews:
        pending = reviews.get("pending_total", 0)
        by = reviews.get("pending_by_sentiment") or {}
        lines.append(
            f"  待回复:{pending} 条(好评 {by.get('good', 0)} / 差评 {by.get('bad', 0)}"
            f" / 无星级 {by.get('unknown', 0)})"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


async def build_analysis(runtime: Any, hotel_row_or_id: Any, day: Any = None) -> str | None:
    """单店分析日报正文;**无素材 → ``None``**(跳过,不报错,V58)。

    ★ 与旧 ``build_hotel_analysis`` 一致:判空**只看** ``scores`` / ``competitor`` /
    ``trend`` 三块(只有 ``num`` 不算"有素材")。
    """
    hotel = hotel_row_or_id
    if not (hasattr(hotel, "id") and hasattr(hotel, "name")):
        hotel = await resolve_hotel(runtime, hotel_row_or_id)
    if hotel is None:
        logger.warning("点评分析:酒店无法解析,跳过({!r})", hotel_row_or_id)
        return None
    payload, notes = await build_payload(runtime, hotel, day)
    if not payload.get("scores") and not payload.get("competitor") and not payload.get("trend"):
        logger.info("点评分析:「{}」无素材,跳过({})", hotel.name, "、".join(notes) or "无 notes")
        return None
    return render_analysis_md(str(hotel.name), payload)


async def publish_analysis(runtime: Any, *, day: Any = None) -> dict[str, Any]:
    """``review.analysis`` 任务:逐店推**绑定群**;无素材/无绑定群**跳过并留痕**。"""
    out: dict[str, Any] = {
        "ok": True,
        "enabled": bool(runtime.settings.review.analysis_enabled),
        "hotels": 0,
        "enqueued": 0,
        "skipped": 0,
        "notes": [],
        "errors": [],
    }
    if not runtime.settings.review.analysis_enabled:
        out["notes"].append("REVIEW_ANALYSIS_ENABLED=0(点评分析日报关闭)")
        logger.info("点评分析日报未启用(REVIEW_ANALYSIS_ENABLED=0)")
        return out

    target = _norm_date(day)
    groups = await runtime.bindings.grouped()
    async with runtime.db.session() as session:
        hotels = list(
            (await session.execute(select(Hotel).where(Hotel.status == "active").order_by(Hotel.id)))
            .scalars()
            .all()
        )

    for hotel in hotels:
        try:
            md = await build_analysis(runtime, hotel, target)
        except Exception as exc:  # noqa: BLE001 - 单店失败不阻断其余酒店
            out["errors"].append(f"{hotel.name}: {exc}")
            logger.error("点评分析渲染失败:店={} {}", hotel.name, exc)
            continue
        if md is None:
            out["skipped"] += 1
            continue
        chatids = [chatid for chatid, items in groups.items() if any(h.hotel_id == hotel.id for h in items)]
        if not chatids:
            out["skipped"] += 1
            out["notes"].append(f"{hotel.name}: 无绑定群(未推送)")
            logger.warning("点评分析:「{}」无绑定群,跳过推送", hotel.name)
            continue
        out["hotels"] += 1
        for chatid in chatids:
            await runtime.push.push(
                BuiltMessage(
                    chatid=chatid,
                    push_type=PUSH_TYPE,
                    content=md,
                    hotel_ids=(int(hotel.id),),
                    note="点评分析日报",
                    meta={"collect_date": target.isoformat()},
                )
            )
            out["enqueued"] += 1
    logger.info(
        "点评分析日报:酒店 {} / 入队 {} / 跳过 {} / 错误 {}",
        out["hotels"],
        out["enqueued"],
        out["skipped"],
        len(out["errors"]),
    )
    return out
