"""T2F.2 建议草稿 —— 待回复 → 审计行 → 草稿文案(旧 ``review_reply.py:193-341`` 逐字继承)。

**本模块是 ``review_replies`` 的唯一写入点**
==========================================

★★ **口径 ①:append-only + 先落审计行再推送**
--------------------------------------------

``review_replies`` 的状态流转**只 INSERT,永不 UPDATE**(迁移 ``0003_segment2_push``
在表语义里写死了这条纪律)。本模块提供全工程唯一的写入函数
:func:`write_audit_row`,它**只有 INSERT 一条语句** —— 想 UPDATE 也无从下手。

顺序也是口径的一部分:**先落审计行,再生成/推送草稿**。进程崩在中间时,
库里至少留下"我们打算做什么"的证据(旧系统同样如此;
调用方 ``service.suggest()`` 在草稿全部落库后才推群)。

★★ **口径 ②:两套口径必须分清**(总纲 §3.6 / 附录 D)
--------------------------------------------------

================================  ==================  ================  ============
情况                                ``status``          ``reviews.replied``  还算待回复?
================================  ==================  ================  ============
差评店级 ``silent``                 ``ignored``         **1**             否
自动回复失败                        ``failed``          **0**             **是**
================================  ==================  ================  ============

> 这不是笔误:``silent`` 是**业务决定"不回复"**(已处理完);
> ``failed`` 是**技术失败**(点评仍等待处理)。把 ``failed`` 也标 ``replied=1``
> 会让点评**永远消失**。V56 专测这两条,实现位置:
> 本模块 :func:`draft_for_hotel_detailed` 的 ``silent`` 分支(ignored + replied=1)
> 与 :mod:`hoteldata.domains.review.autoreply` 的失败分支(failed + replied=0)。

★ **口径 ④:差评的两种归宿**(旧 ``decide_action``)
--------------------------------------------------

* 店级 ``silent`` → **不出草稿**,直接落 ``ignored`` 审计 + ``replied=1``(业务已处理);
* 店级 ``template`` → 出**草稿**(人工确认后回),★ **差评一律不进自动**;
* ``unknown``(无星级)→ 出**人工待办**(``suggested`` 审计行,内容为空),
  由人工确认星级后再处理:**unknown 永不自动回复**。

★ **口径 ⑥:``config.rules.draft_limit``(默认 10)** 是"每次最多出多少条**新**草稿",
``silent`` 条目**不占额度**(它不是草稿,是策略执行)。

**防重**(旧 ``check_drafted``,旧 ``review_reply.py:225-228``)
============================================================

该 ``review_id`` 已有 ``suggested`` / ``ok`` / ``ignored`` 审计行 → **跳过**,
不重复推、不重复打扰管理群。``failed`` **不算已处理**:它仍在待回复池里,
下次草稿流程可以再捞出来进人工队列。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from loguru import logger
from sqlalchemy import exists, select

from hoteldata.domains.collect.repository import CollectRepository
from hoteldata.infra.models import REPLY_EXECUTORS, REPLY_STATUSES, REVIEW_SENTIMENTS, ReviewReply

from .policy import (
    classify_sentiment,
    decide_action,
    effective_policy,
    load_review_config,
    resolve_hotel,
)
from .templates import build_draft_md, render_reply

__all__ = [
    "DRAFTED_STATUSES",
    "ReviewRow",
    "check_drafted",
    "draft_for_hotel",
    "draft_for_hotel_detailed",
    "load_pending_reviews",
    "pending_stats",
    "pending_drafts_of",
    "plan_for_hotel",
    "write_audit_row",
]

#: 视为"已生成草稿 / 已处理"的审计状态(旧 ``check_drafted`` 逐字;``failed`` **不在**其中)
DRAFTED_STATUSES: tuple[str, ...] = ("suggested", "ok", "ignored")


@dataclass(slots=True)
class ReviewRow:
    """一条待回复点评的**值快照**(不持有 ORM 身份,杜绝 ``DetachedInstanceError``)。"""

    pk: int
    hotel_id: int
    review_id: str
    hotel_name: str
    user_name: str | None
    star: int | None
    content: str
    sentiment: str
    replied: int
    strategy: str | None = None
    comment_time: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "review_pk": self.pk,
            "hotel_id": self.hotel_id,
            "review_id": self.review_id,
            "hotel_name": self.hotel_name,
            "user_name": self.user_name,
            "star": self.star,
            "content": self.content,
            "sentiment": self.sentiment,
            "replied": self.replied,
            "strategy": self.strategy,
            "comment_time": self.comment_time.isoformat() if self.comment_time else None,
        }


# ---------------------------------------------------------------------------
# 审计写入(全工程唯一)
# ---------------------------------------------------------------------------


async def write_audit_row(
    runtime: Any,
    *,
    hotel_id: int,
    review_id: str,
    status: str,
    strategy: str | None = None,
    content: str = "",
    exec_by: str = "draft",
    detail: dict[str, Any] | None = None,
) -> int:
    """追加一行 ``review_replies`` 审计(**只有 INSERT,没有 UPDATE**)。

    枚举先校验后落库:状态/执行者写错会让审计失去追溯价值,
    所以这里**宁可抛错也不写脏行**(``REPLY_STATUSES`` / ``REPLY_EXECUTORS``)。
    """
    if status not in REPLY_STATUSES:
        raise ValueError(f"review_replies.status 非法: {status!r};合法值 {REPLY_STATUSES}")
    if exec_by not in REPLY_EXECUTORS:
        raise ValueError(f"review_replies.exec_by 非法: {exec_by!r};合法值 {REPLY_EXECUTORS}")
    async with runtime.db.session() as session:
        row = ReviewReply(
            hotel_id=int(hotel_id),
            review_id=str(review_id),
            status=str(status),
            strategy=strategy or None,
            content=content or "",
            exec_by=str(exec_by),
            detail_json=detail or None,
        )
        session.add(row)
        await session.flush()
        row_id = int(row.id)
    logger.info(
        "点评审计新增:#{} 店={} 点评={} status={} strategy={} by={}",
        row_id,
        hotel_id,
        review_id,
        status,
        strategy,
        exec_by,
    )
    return row_id


async def check_drafted(runtime: Any, hotel_id: Any, review_id: Any) -> bool:
    """该点评是否**已有** ``suggested`` / ``ok`` / ``ignored`` 审计行(防重复草稿)。

    逐字继承旧 ``check_drafted``(旧 ``review_reply.py:225-228``)的判定集合。
    注意是"**存在**任一行"而不是"最新一行":append-only 表里
    「先 suggested 后 failed」的点评仍应视为已进过人工队列,不再重复出草稿。
    """
    stmt = select(
        exists().where(
            ReviewReply.hotel_id == int(hotel_id),
            ReviewReply.review_id == str(review_id),
            ReviewReply.status.in_(DRAFTED_STATUSES),
        )
    )
    async with runtime.db.session() as session:
        return bool(await session.scalar(stmt))


# ---------------------------------------------------------------------------
# 取数
# ---------------------------------------------------------------------------


async def load_pending_reviews(
    runtime: Any,
    hotel: Any,
    *,
    limit: int | None = None,
) -> list[ReviewRow]:
    """待回复池:**``replied != 1``** 且 ``sentiment`` 属于三态(口径:池子定义)。

    ★ 走段1 的查询契约 :meth:`CollectRepository.list_reviews`(段2 不自己写 join),
    但``replied`` 的过滤放在**本侧**完成:契约允许 ``replied`` 为 ``NULL``
    (建表默认 0,采集侧**永不写**该列),``replied != 1`` 语义上要把 ``NULL``
    也算作待回复 —— SQL 的 ``!=`` 会把 ``NULL`` 排除掉,故这里显式按
    ``int(replied or 0) != 1`` 判(Python 侧兜底,与口径逐字一致)。
    """
    hotel_id = int(getattr(hotel, "id", hotel) if not isinstance(hotel, dict) else hotel["id"])
    hotel_name = str(getattr(hotel, "name", "") if not isinstance(hotel, dict) else hotel.get("name") or "")
    async with runtime.db.session() as session:
        rows = await CollectRepository(session).list_reviews(hotel_id)
        pending = [
            ReviewRow(
                pk=int(row.id),
                hotel_id=int(row.hotel_id),
                review_id=str(row.review_id),
                hotel_name=hotel_name,
                user_name=row.user_name,
                star=row.star,
                content=str(row.content or ""),
                sentiment=str(row.sentiment or "") or classify_sentiment(row.star),
                replied=int(row.replied or 0),
                strategy=row.strategy,
                comment_time=row.comment_time,
            )
            for row in rows
            if int(row.replied or 0) != 1
        ]
    if limit is not None:
        return pending[: max(0, int(limit))]
    return pending


async def pending_stats(runtime: Any, hotel: Any) -> dict[str, Any]:
    """待回复快照:``{"pending": n, "by_sentiment": {...}}``(点评状态 / 分析日报共用)。"""
    rows = await load_pending_reviews(runtime, hotel)
    by = dict.fromkeys(REVIEW_SENTIMENTS, 0)
    for row in rows:
        key = row.sentiment if row.sentiment in by else "unknown"
        by[key] += 1
    return {"pending": len(rows), "by_sentiment": by}


# ---------------------------------------------------------------------------
# 计划(纯函数,不写库)—— 「点评待办」命令与草稿流程共用
# ---------------------------------------------------------------------------


async def plan_for_hotel(
    runtime: Any,
    hotel_row: Any,
    *,
    limit: int | None = None,
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """逐条待回复点评的**动作计划**(不写库,可随时重算)。

    返回 ``[{"review_id", "review_pk", "sentiment", "star", "strategy", "template_id",
    "action", "draft_md", "drafted", "content", "review"}]``;``action`` ∈
    ``{"silent", "suggest"}``(``auto`` 由自动回复任务处理,不在此列,防双流程 ——
    旧 ``plan_pending`` 同口径)。

    ``limit`` 只约束**新草稿**条数(默认 ``config.rules.draft_limit``=10);
    ``silent`` 条目与已草稿条目**不占额度**。
    """
    config = config or load_review_config(settings=getattr(runtime, "settings", None))
    rules = config.get("rules") or {}
    cap = int(rules.get("draft_limit", 10)) if limit is None else int(limit)
    policy = await effective_policy(runtime, hotel_row)
    hotel_name = str(getattr(hotel_row, "name", "") or "")

    out: list[dict[str, Any]] = []
    drafted_count = 0
    for review in await load_pending_reviews(runtime, hotel_row):
        drafted = await check_drafted(runtime, review.hotel_id, review.review_id)
        decision = decide_action(policy, review, config)
        action = decision["action"]
        if action == "silent":
            # ★ 口径 ②/④:差评店级 silent = 业务决定"不回复" → ignored + replied=1
            out.append(
                {
                    "review_id": review.review_id,
                    "review_pk": review.pk,
                    "sentiment": review.sentiment,
                    "star": review.star,
                    "strategy": "silent",
                    "template_id": None,
                    "action": "silent",
                    "draft_md": None,
                    "drafted": drafted,
                    "content": "",
                    "reason": decision["reason"],
                }
            )
            continue
        if drafted:
            continue  # ★ 防重:已 suggested/ok/ignored → 不再出草稿
        if drafted_count >= cap:
            # 额度用尽:只停止"再出草稿",**不跳过 silent 策略执行**
            # (它是业务决定,不该被草稿条数上限饿死)。
            logger.info("酒店 {} 草稿已达上限 {} 条,其余草稿留待下次(策略执行不受限)", hotel_name, cap)
            continue
        drafted_count += 1
        rendered = render_reply(policy, review, config, hotel_name=hotel_name)
        if rendered is None:
            # 只可能是 unknown(无星级):★ action='manual' —— **不是** silent,
            # 绝不标 ignored/replied=1,点评必须留在待回复池等人确认星级(口径 ③)。
            template_id, body = None, ""
        else:
            template_id, body = rendered
        out.append(
            {
                "review_id": review.review_id,
                "review_pk": review.pk,
                "sentiment": review.sentiment,
                "star": review.star,
                "strategy": template_id or "manual",
                "template_id": template_id,
                "action": "manual" if action == "manual" else "suggest",
                "draft_md": build_draft_md(review, hotel_row, template_id, body),
                "drafted": False,
                "content": body,
                "reason": decision["reason"],
            }
        )
    return out


# ---------------------------------------------------------------------------
# 草稿(写库)
# ---------------------------------------------------------------------------


async def draft_for_hotel_detailed(
    runtime: Any,
    hotel_row: Any,
    *,
    limit: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """生成某店草稿并落审计行,返回 ``(drafts, stats)``。

    逐条(动作由 :func:`~hoteldata.domains.review.policy.decide_action` 判定):

    * ``silent``(差评 + 店级 silent)→ 落 ``ignored`` 审计(``exec_by='silent'``)
      并 ``replied=1``(口径 ②:业务决定不回复 = 已处理);
    * ``suggest``(好评 / 差评 template)→ **先落 ``suggested`` 审计行**
      (``exec_by='draft'``)**再**产出 ``draft_md``(口径 ①);
    * ``manual``(无星级 unknown)→ 同样落 ``suggested``(内容为空),
      但**绝不** ``replied=1``:点评留在待回复池等人确认星级(口径 ③)。

    单条失败**只计入 errors,不中断**其余点评(旧系统同样的容错粒度)。
    """
    config = load_review_config(settings=getattr(runtime, "settings", None))
    plans = await plan_for_hotel(runtime, hotel_row, limit=limit, config=config)
    hotel_name = str(getattr(hotel_row, "name", "") or "")
    hotel_id = int(hotel_row.id)

    drafts: list[dict[str, Any]] = []
    stats: dict[str, Any] = {
        "hotel": hotel_name,
        "hotel_id": hotel_id,
        "planned": len(plans),
        "drafts": 0,
        "silent": 0,
        "manual": 0,
        "skipped_already_drafted": 0,
        "errors": [],
    }
    for plan in plans:
        if plan["action"] == "silent":
            try:
                await write_audit_row(
                    runtime,
                    hotel_id=hotel_id,
                    review_id=plan["review_id"],
                    status="ignored",
                    strategy="silent",
                    content=plan["content"],
                    exec_by="silent",
                    detail={
                        "reason": "店级策略差评统一不回复(甲方口径)",
                        "sentiment": plan["sentiment"],
                        "star": plan["star"],
                        "review_pk": plan["review_pk"],
                    },
                )
                await _mark_replied(runtime, plan["review_pk"], strategy="silent", exec_by="silent")
                stats["silent"] += 1
            except Exception as exc:  # noqa: BLE001 - 单条失败不阻断其余点评
                stats["errors"].append(f"review#{plan['review_pk']} silent 落库失败: {exc}")
                logger.error("点评 silent 落库失败:店={} #{} {}", hotel_name, plan["review_pk"], exc)
            continue

        try:
            await write_audit_row(
                runtime,
                hotel_id=hotel_id,
                review_id=plan["review_id"],
                status="suggested",
                strategy=plan["template_id"],
                content=plan["content"],
                exec_by="draft",
                detail={
                    "action": plan["action"],
                    "reason": plan.get("reason"),
                    "manual_confirm_star": plan["action"] == "manual",
                    "sentiment": plan["sentiment"],
                    "star": plan["star"],
                    "review_pk": plan["review_pk"],
                    "draft_md": plan["draft_md"],
                },
            )
        except Exception as exc:  # noqa: BLE001
            stats["errors"].append(f"review#{plan['review_pk']} 审计写入失败: {exc}")
            logger.error("点评草稿审计写入失败:店={} #{} {}", hotel_name, plan["review_pk"], exc)
            continue

        drafts.append(
            {
                "review_id": plan["review_id"],
                "review_pk": plan["review_pk"],
                "sentiment": plan["sentiment"],
                "star": plan["star"],
                "draft_md": plan["draft_md"],
                "strategy": plan["strategy"],
                "content": plan["content"],
            }
        )
        if plan["action"] == "manual":
            stats["manual"] += 1
    stats["drafts"] = len(drafts)
    if drafts or stats["silent"]:
        logger.info(
            "点评草稿:店={} 草稿={}(其中人工确认 {} 条) 静默={} 计划={}",
            hotel_name,
            len(drafts),
            stats["manual"],
            stats["silent"],
            stats["planned"],
        )
    return drafts, stats


async def draft_for_hotel(
    runtime: Any,
    hotel_row: Any,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """T2F.2 主入口:某店草稿列表(落审计行;``silent`` 条目**不出现**在返回值里)。

    返回键**固定**为 ``review_id`` / ``review_pk`` / ``sentiment`` / ``star`` /
    ``draft_md`` / ``strategy`` / ``content``(契约见计划书批次 F);
    需要 ``silent`` 计数与错误明细时用 :func:`draft_for_hotel_detailed`。
    """
    drafts, _stats = await draft_for_hotel_detailed(runtime, hotel_row, limit=limit)
    return drafts


async def pending_drafts_of(
    runtime: Any,
    hotel_row_or_id: Any,
    *,
    limit: int | None = None,
    include_drafted: bool = False,
) -> list[dict[str, Any]]:
    """「点评待办」命令用:**当场算,不落库**(无任何副作用)。

    ``include_drafted=False``(默认)→ 只给"还没有草稿"的条目,
    与旧 ``pending_drafts_for_hotel`` 同口径;``True`` → 连已落过 ``suggested``
    行的一起给(命令里用来告诉操作者"还有 N 条已生成草稿待处理")。
    """
    hotel = hotel_row_or_id
    if not hasattr(hotel, "id") or not hasattr(hotel, "name"):
        hotel = await resolve_hotel(runtime, hotel_row_or_id)
    if hotel is None:
        return []
    plans = await plan_for_hotel(runtime, hotel, limit=limit)
    out: list[dict[str, Any]] = []
    for plan in plans:
        if plan["action"] == "silent":
            continue  # silent 是"不回",不是待办
        if plan["drafted"] and not include_drafted:
            continue
        out.append(
            {
                "review_id": plan["review_id"],
                "review_pk": plan["review_pk"],
                "sentiment": plan["sentiment"],
                "star": plan["star"],
                "draft_md": plan["draft_md"],
                "strategy": plan["strategy"],
                "content": plan["content"],
                "drafted": plan["drafted"],
            }
        )
    return out


async def _mark_replied(runtime: Any, review_pk: int, *, strategy: str, exec_by: str) -> None:
    """``replied=1`` 的唯一入口在 :mod:`~hoteldata.domains.review.autoreply`。

    这里**函数内 import** 是为了打破 ``draft ↔ autoreply`` 的模块级循环
    (autoreply 需要本模块的 ``write_audit_row`` / ``check_drafted``)。
    """
    from .autoreply import mark_replied

    await mark_replied(runtime, review_pk, strategy=strategy, exec_by=exec_by)
