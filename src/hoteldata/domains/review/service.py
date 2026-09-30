"""点评交互域服务(T2F.2/T2F.3/T2F.4 的编排层)—— 任务与群命令的唯一入口。

**为什么要有这一层**
====================

``domains/review/`` 里 ``policy`` / ``templates`` / ``draft`` / ``autoreply`` /
``analysis`` 各自只做一件事;调度任务(``jobs.py``)、CLI 与群命令
(``domains/bot/commands.py``)**都只认本模块**。旧系统把这些编排散在
``app/review_reply.py`` 的 ``run_review_suggest_all`` / ``auto_reply_run_all`` /
``run_review_realtime`` 与 ``commands.py`` 的四个 ``_review_*_reply`` 里,
新架构收成一处,顺便保证**每个返回值都是 JSON 可序列化的 dict**
(直接进 ``job_runs.summary``,CLI/命令都能原样打印)。

方法 ↔ 任务 ↔ 命令对应表(计划书 §5.9 / 附录 A)
==============================================

==========================  ====================  ====================================
方法                         任务 / 命令            说明
==========================  ====================  ====================================
:meth:`suggest`              ``review.suggest``    逐店草稿 → 合并一条 → 管理群(≤20 条)
:meth:`auto_reply`           ``review.auto``       自动回复(门控;仅好评)
:meth:`analysis`             ``review.analysis``   点评分析日报 → 每店绑定群
:meth:`realtime`             ``review.realtime``   刷新待回复 → 自动回复 → 结果推绑定群
:meth:`drafts`               「点评待办」           当场算,不落库
:meth:`status`               「点评状态」           待回复/审计计数/自动模式(含就绪原因)
:meth:`set_policy`           「点评策略」           写 ``core_hotels.review_policy``
:meth:`transition`           「回复确认」「已处理」「已忽略」  审计行 + ``replied``
:meth:`effective_policy`     —                     三层优先级合并结果
==========================  ====================  ====================================

★ 每个方法都带 ``text`` 字段(可直接发群),同时保留结构化字段 ——
命令层既可以原样转发 ``text``,也可以自行排版。

★★ 本层必须守住的两条口径(详述见 ``draft.py`` / ``autoreply.py`` 的模块 docstring)
==================================================================================

① **append-only**:``review_replies`` 流转**新增行、不 UPDATE**;先落审计行再推送。
② **两套口径**:差评店级 ``silent`` → ``ignored`` + ``replied=1``(**业务已处理完**);
   自动回复失败 → ``failed`` + ``replied=0``(**技术失败,仍在待回复池**)。
   :meth:`transition` 的 ``ok`` 与 ``ignored`` **都**写 ``replied=1``。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from loguru import logger
from sqlalchemy import func, select

from hoteldata.infra.models import (
    REPLY_STATUSES,
    Account,
    Hotel,
    ReviewReply,
    ReviewReview,
)
from hoteldata.push.service import BuiltMessage

from . import policy as policy_mod
from .analysis import publish_analysis
from .autoreply import autorun, mark_replied
from .draft import (
    check_drafted,
    draft_for_hotel_detailed,
    pending_drafts_of,
    pending_stats,
    write_audit_row,
)
from .policy import auto_whitelist, load_review_config, resolve_hotel
from .policy import effective_policy as merge_policy

__all__ = ["ReviewService", "SUGGEST_PUSH_TYPE", "REALTIME_PUSH_TYPE"]

#: 点评草稿推送类型(A2-7 命名)
SUGGEST_PUSH_TYPE = "review_suggest"
#: 实时回复结果推送类型
REALTIME_PUSH_TYPE = "review_realtime"
#: 单条草稿消息最多带几条草稿(计划书批次 F:≤20 条,超出截断并提示)
SUGGEST_MAX_DRAFTS = 20


class ReviewService:
    """点评交互域服务(任务 / CLI / 群命令共用)。"""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    # ==================================================================
    # 任务:review.suggest
    # ==================================================================

    async def suggest(self) -> dict[str, Any]:
        """``review.suggest``:逐店出草稿 → **合并成一条** → 推管理群(草稿 ≤20 条)。

        顺序严格:``draft_for_hotel_detailed`` **先落审计行**,全部落完才推群
        (口径 ①:崩在中间时留下的证据是"打算推什么")。
        """
        out: dict[str, Any] = {
            "ok": True,
            "enabled": bool(self.runtime.settings.review.suggest_enabled),
            "hotels": 0,
            "drafts": 0,
            "silent": 0,
            "manual": 0,
            "enqueued": 0,
            "truncated": 0,
            "errors": [],
            "notes": [],
        }
        if not self.runtime.settings.review.suggest_enabled:
            out["notes"].append("REVIEW_SUGGEST_ENABLED=0(点评建议草稿关闭)")
            logger.info("点评建议草稿未启用(REVIEW_SUGGEST_ENABLED=0)")
            return out

        drafts: list[dict[str, Any]] = []
        hotel_ids: list[int] = []
        for hotel in await self._active_hotels():
            try:
                items, stats = await draft_for_hotel_detailed(self.runtime, hotel)
            except Exception as exc:  # noqa: BLE001 - 单店失败不阻断其余酒店
                out["errors"].append(f"{hotel.name}: {exc}")
                logger.error("点评草稿生成失败:店={} {}", hotel.name, exc)
                continue
            out["hotels"] += 1
            out["silent"] += int(stats.get("silent", 0))
            out["manual"] += int(stats.get("manual", 0))
            out["errors"].extend(stats.get("errors") or [])
            if items:
                hotel_ids.append(int(hotel.id))
            drafts.extend(items)

        out["drafts"] = len(drafts)
        if not drafts:
            logger.info("点评草稿:本期无新草稿(全部已处理/无待回复)")
            return out

        shown = drafts[:SUGGEST_MAX_DRAFTS]
        out["truncated"] = max(0, len(drafts) - len(shown))
        lines = [f"📝 点评草稿({len(drafts)} 条待回复)"]
        lines.extend(item["draft_md"] for item in shown)
        if out["truncated"]:
            lines.append(f"(还有 {out['truncated']} 条未展示,用「点评待办 <酒店名>」查看)")
        content = "\n\n".join(lines)

        manage = list(self.runtime.settings.push.manage_chatids)
        if not manage:
            out["ok"] = False
            out["errors"].append("未配置 MANAGE_CHATIDS(点评草稿未推送)")
            logger.error("点评草稿已生成 {} 条,但未配置 MANAGE_CHATIDS,推送失败(不静默)", len(drafts))
            return out
        for chatid in manage:
            await self.runtime.push.push(
                BuiltMessage(
                    chatid=chatid,
                    push_type=SUGGEST_PUSH_TYPE,
                    content=content,
                    hotel_ids=tuple(dict.fromkeys(hotel_ids)),
                    note=f"点评草稿 {len(drafts)} 条",
                )
            )
            out["enqueued"] += 1
        logger.info(
            "点评草稿:酒店 {} / 草稿 {} / 静默 {} / 入队管理群 {}",
            out["hotels"],
            out["drafts"],
            out["silent"],
            out["enqueued"],
        )
        return out

    # ==================================================================
    # 任务:review.auto / review.analysis
    # ==================================================================

    async def auto_reply(self) -> dict[str, Any]:
        """``review.auto``:自动回复(**门控**;仅好评;未就绪 → 人工队列,绝不伪造成功)。"""
        result = await autorun(self.runtime)
        return dict(result)

    async def analysis(self) -> dict[str, Any]:
        """``review.analysis``:点评分析日报(每店绑定群;slot 去重由派发器做)。"""
        return dict(await publish_analysis(self.runtime))

    # ==================================================================
    # 任务:review.realtime
    # ==================================================================

    async def realtime(self) -> dict[str, Any]:
        """``review.realtime``:刷新待回复列表 → 自动回复 → 已回复结果实时推绑定群。

        ★ **刷新待回复列表**走段1 的点评提取器
        (:class:`~hoteldata.domains.collect.review.ReviewExtractor`,**惰性 import**,
        计划书 §1.4 的"只消费不重写"):只调它、只落它给的 ``records``,
        本域不写任何提取逻辑。
        """
        out: dict[str, Any] = {
            "ok": True,
            "enabled": bool(self.runtime.settings.review.realtime_enabled),
            "collected_hotels": 0,
            "collected_rows": 0,
            "replied": 0,
            "failed": 0,
            "queued_manual": 0,
            "pushed": 0,
            "submit_ready": False,
            "notes": [],
            "errors": [],
        }
        if not self.runtime.settings.review.realtime_enabled:
            out["notes"].append("REVIEW_REALTIME_ENABLED=0(实时回复轮询关闭)")
            logger.info("点评实时回复未启用(REVIEW_REALTIME_ENABLED=0)")
            return out

        hotels = await self._active_hotels()
        refresh = await self._refresh_pending(hotels)
        out["collected_hotels"] = refresh["hotels"]
        out["collected_rows"] = refresh["rows"]
        out["errors"].extend(refresh["errors"])

        auto = await autorun(self.runtime, hotels=hotels)
        out["replied"] = int(auto.get("replied", 0))
        out["failed"] = int(auto.get("failed", 0))
        out["queued_manual"] = int(auto.get("queued_manual", 0))
        out["submit_ready"] = bool(auto.get("submit_ready"))
        out["not_ready_reason"] = auto.get("not_ready_reason") or ""
        out["errors"].extend(auto.get("errors") or [])
        out["notes"].extend(auto.get("notes") or [])
        if not auto.get("ok", True):
            out["ok"] = False

        details = auto.get("replied_details") or []
        if details:
            groups = await self.runtime.bindings.grouped()
            for detail in details:
                hotel_id = detail.get("hotel_id")
                chatids = [
                    chatid for chatid, items in groups.items() if any(h.hotel_id == hotel_id for h in items)
                ]
                if not chatids:
                    out["notes"].append(f"{detail.get('hotel')}: 无绑定群(回复结果未推送)")
                    continue
                content = (
                    f"📝 新增点评 ★{detail.get('star', '?')}「{detail.get('content') or '…'}」\n"
                    f"✅ 已自动回复:「{detail.get('body') or ''}」"
                )
                for chatid in chatids:
                    await self.runtime.push.push(
                        BuiltMessage(
                            chatid=chatid,
                            push_type=REALTIME_PUSH_TYPE,
                            content=content,
                            hotel_ids=(int(hotel_id),),
                            note="点评实时回复结果",
                        )
                    )
                    out["pushed"] += 1
        logger.info(
            "点评实时:采集店 {} / 已回 {} / 失败 {} / 人工队列 {} / 推送 {}",
            out["collected_hotels"],
            out["replied"],
            out["failed"],
            out["queued_manual"],
            out["pushed"],
        )
        return out

    async def _refresh_pending(self, hotels: list[Hotel]) -> dict[str, Any]:
        """用段1 的点评提取器刷新待回复列表(**惰性 import**,失败不影响其余酒店)。"""
        result: dict[str, Any] = {"hotels": 0, "rows": 0, "errors": []}
        try:
            from hoteldata.domains.collect.repository import CollectRepository
            from hoteldata.domains.collect.review import ReviewExtractor
        except ImportError as exc:  # pragma: no cover - 段1 未就绪时的降级
            logger.warning("点评提取器不可用,跳过刷新: {}", exc)
            result["errors"].append(f"点评提取器不可用: {exc}")
            return result

        extractor = _make_extractor(ReviewExtractor, self.runtime)
        async with self.runtime.db.session() as session:
            accounts = {a.id: a for a in (await session.execute(select(Account))).scalars().all()}
        day = date.today()
        for hotel in hotels:
            account = accounts.get(hotel.account_id) if hotel.account_id else None
            if account is None:
                logger.warning("点评刷新:酒店 {} 未绑定账号,跳过", hotel.name)
                continue
            try:
                ctx = self.runtime.extract_context(hotel, account, day)
                results = await extractor.extract(ctx)
                async with self.runtime.db.session() as session:
                    repo = CollectRepository(session)
                    for res in results:
                        if not res.records:
                            continue
                        # ★ 素材结果带 detail["kind"],待回复列表那条不带(段1 契约)
                        if (res.detail or {}).get("kind"):
                            await repo.upsert_review_materials(res.records)
                        else:
                            await repo.upsert_reviews(res.records)
                            result["rows"] += len(res.records)
                result["hotels"] += 1
            except Exception as exc:  # noqa: BLE001 - 刷新失败不阻断自动回复流程
                result["errors"].append(f"{hotel.name}: {exc}")
                logger.warning("点评刷新失败(店={}): {}", hotel.name, exc)
        return result

    # ==================================================================
    # 命令:点评待办 / 点评状态 / 点评策略
    # ==================================================================

    async def drafts(self, hotel_name: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        """「点评待办 [酒店名]」:**当场算,不落库**;返回每店的待办(list[dict])。

        每个元素含 ``text``(可直接发群,旧 ``_review_todo_reply`` 文案)、
        ``items``(新草稿)、``already_drafted``(已生成过草稿、等人工处理的条数)。
        """
        hotels = await self._target_hotels(hotel_name)
        if not hotels:
            return [{"ok": False, "error": f"酒店不存在: {hotel_name}"}] if hotel_name else []
        out: list[dict[str, Any]] = []
        for hotel in hotels:
            items = await pending_drafts_of(self.runtime, hotel, limit=limit, include_drafted=True)
            fresh = [item for item in items if not item.get("drafted")]
            waiting = [item for item in items if item.get("drafted")]
            lines = [f"📝 点评草稿「{hotel.name}」({len(fresh)} 条,仅预览不推送)"]
            if fresh:
                lines.extend(item["draft_md"] for item in fresh[:limit])
            else:
                lines.append(f"  (无新草稿;另有 {len(waiting)} 条已生成草稿待人工处理)")
            out.append(
                {
                    "ok": True,
                    "hotel": str(hotel.name),
                    "hotel_id": int(hotel.id),
                    "count": len(fresh),
                    "already_drafted": len(waiting),
                    "items": fresh,
                    "text": "\n\n".join(lines),
                }
            )
        return out

    async def status(self, hotel_name: str | None = None) -> dict[str, Any]:
        """「点评状态 [酒店名]」:待回复 + 审计分状态计数 + 自动模式状态(**含就绪原因**)。"""
        config = load_review_config(settings=self.runtime.settings)
        auto = config.get("auto") or {}
        ready, reason = policy_mod.submit_ready(self.runtime)
        whitelist = auto_whitelist(config)
        auto_txt = (
            f"启用(灰度店: {'、'.join(whitelist) or '无'})" if auto.get("enabled") else "未启用(auto.enabled=false)"
        )

        hotels = await self._target_hotels(hotel_name)
        hotel_ids = [int(h.id) for h in hotels] if hotel_name else None
        audit = await self._audit_counts(hotel_ids)

        if hotel_name:
            if not hotels:
                return {"ok": False, "error": f"酒店不存在: {hotel_name}"}
            hotel = hotels[0]
            stats = await pending_stats(self.runtime, hotel)
            policy = await merge_policy(self.runtime, hotel)
            text = "\n".join(
                [
                    f"📌 「{hotel.name}」点评状态",
                    f"  待回复:{stats['pending']} 条",
                    self._audit_line(audit),
                    f"  店级策略:{self._policy_text(policy)}",
                    f"  自动模式:{auto_txt}",
                    f"  通道:{'就绪' if ready else '未就绪'} — {reason}",
                ]
            )
            return {
                "ok": True,
                "hotel": str(hotel.name),
                "hotel_id": int(hotel.id),
                "pending": stats["pending"],
                "pending_by_sentiment": stats["by_sentiment"],
                "audit": audit,
                "policy": policy,
                "auto": {
                    "enabled": bool(auto.get("enabled")),
                    "whitelist": whitelist,
                    "settings_enabled": bool(self.runtime.settings.review.auto_enabled),
                    "submit_ready": ready,
                    "submit_ready_reason": reason,
                },
                "text": text,
            }

        pending = 0
        for hotel in hotels:
            pending += int((await pending_stats(self.runtime, hotel))["pending"])
        text = "\n".join(
            [
                "📌 全店点评状态(总)",
                f"  待回复:{pending} 条",
                self._audit_line(audit),
                f"  自动模式:{auto_txt}",
                f"  通道:{'就绪' if ready else '未就绪'} — {reason}",
            ]
        )
        return {
            "ok": True,
            "hotel": None,
            "hotels": len(hotels),
            "pending": pending,
            "audit": audit,
            "auto": {
                "enabled": bool(auto.get("enabled")),
                "whitelist": whitelist,
                "settings_enabled": bool(self.runtime.settings.review.auto_enabled),
                "submit_ready": ready,
                "submit_ready_reason": reason,
            },
            "text": text,
        }

    async def set_policy(self, hotel_name: str, changes: dict[str, Any]) -> dict[str, Any]:
        """「点评策略 <酒店名> …」:写 ``core_hotels.review_policy``(JSONB)。"""
        hotel = await resolve_hotel(self.runtime, hotel_name or "")
        if hotel is None:
            return {"ok": False, "error": f"酒店不存在: {hotel_name}"}
        if not changes:
            return {
                "ok": False,
                "error": "请使用:点评策略 <酒店名> [好评 <模板id>] [差评 silent|template [模板id]]",
            }
        result = await policy_mod.set_policy(self.runtime, int(hotel.id), changes)
        if not result.get("ok"):
            return result
        policy = result["review_policy"]
        return {
            "ok": True,
            "hotel": str(hotel.name),
            "hotel_id": int(hotel.id),
            "policy": policy,
            "text": f"✅ 「{hotel.name}」点评策略:{self._policy_text(policy)}",
        }

    # ==================================================================
    # 命令:回复确认 / 已处理 / 已忽略
    # ==================================================================

    async def transition(self, reply_pk: Any, status: str, exec_by: str = "manage") -> dict[str, Any]:
        """状态流转:``suggested`` → ``ok`` / ``ignored``(**新增审计行 + ``replied=1``**)。

        * ``status='ok'``(回复确认 / 已处理)→ 审计行 ``ok`` + ``replied=1``;
        * ``status='ignored'``(已忽略)→ 审计行 ``ignored`` + ★ **``replied=1``**
          (口径 ②:业务决定"不回复" = 已处理完,不再进待回复池);
        * ``reply_pk`` 非法 / 不存在 → ``{"ok": False, "error": ...}``(不抛)。
        """
        try:
            pk = int(str(reply_pk).lstrip("#").strip())
        except (TypeError, ValueError):
            return {"ok": False, "error": f"评测 id 非法: {reply_pk!r}"}
        if status not in ("ok", "ignored"):
            return {"ok": False, "error": f"状态非法: {status}(应为 ok/ignored)"}

        async with self.runtime.db.session() as session:
            review = await session.scalar(select(ReviewReview).where(ReviewReview.id == pk))
            if review is None:
                return {"ok": False, "error": f"点评不存在: #{pk}"}
            hotel_id = int(review.hotel_id)
            review_id = str(review.review_id)
            content = str(review.content or "")

        latest = await self._latest_audit(hotel_id, review_id)
        strategy = str((latest or {}).get("strategy") or "") or None
        body = str((latest or {}).get("content") or "") or content
        final_strategy = strategy or status

        await write_audit_row(
            self.runtime,
            hotel_id=hotel_id,
            review_id=review_id,
            status=status,
            strategy=final_strategy,
            content=body,
            exec_by=exec_by,
            detail={
                "review_pk": pk,
                "previous_strategy": strategy,
                "previous_audit_id": (latest or {}).get("id"),
                "note": "人工流转(回复确认/已处理 → ok;已忽略 → ignored);两态均 replied=1",
            },
        )
        await mark_replied(self.runtime, pk, strategy=final_strategy, exec_by=exec_by, replied=1)
        logger.info("点评流转:#{} status={} strategy={} by={}", pk, status, final_strategy, exec_by)
        return {
            "ok": True,
            "review_pk": pk,
            "review_id": review_id,
            "hotel_id": hotel_id,
            "status": status,
            "strategy": final_strategy,
            "text": f"✅ 点评 #{pk} 已标记为「{status}」(策略={final_strategy})",
        }

    # ==================================================================
    # 查询
    # ==================================================================

    async def effective_policy(self, hotel_name: str) -> dict[str, Any]:
        """三层优先级合并后的店级策略(查不到酒店 → 带 ``error`` 的空结果)。"""
        hotel = await resolve_hotel(self.runtime, hotel_name or "")
        if hotel is None:
            return {"ok": False, "error": f"酒店不存在: {hotel_name}"}
        policy = await merge_policy(self.runtime, hotel)
        return {"ok": True, "hotel": str(hotel.name), "hotel_id": int(hotel.id), "policy": policy}

    async def snapshot(self) -> dict[str, Any]:
        """服务级摘要(``/status``、CLI ``review status`` 用)。"""
        config = load_review_config(settings=self.runtime.settings)
        ready, reason = policy_mod.submit_ready(self.runtime)
        status = await self.status(None)
        return {
            "hotels": status.get("hotels", 0),
            "pending": status.get("pending", 0),
            "audit": status.get("audit", {}),
            "auto": status.get("auto", {}),
            "submit_ready": ready,
            "submit_ready_reason": reason,
            "auto_whitelist": auto_whitelist(config),
            "settings": {
                "suggest_enabled": bool(self.runtime.settings.review.suggest_enabled),
                "analysis_enabled": bool(self.runtime.settings.review.analysis_enabled),
                "auto_enabled": bool(self.runtime.settings.review.auto_enabled),
                "realtime_enabled": bool(self.runtime.settings.review.realtime_enabled),
                "realtime_interval_min": int(self.runtime.settings.review.realtime_interval_min),
            },
        }

    # ==================================================================
    # 内部工具
    # ==================================================================

    async def _active_hotels(self) -> list[Hotel]:
        async with self.runtime.db.session() as session:
            return list(
                (await session.execute(select(Hotel).where(Hotel.status == "active").order_by(Hotel.id)))
                .scalars()
                .all()
            )

    async def _target_hotels(self, hotel_name: str | None) -> list[Hotel]:
        if not hotel_name:
            return await self._active_hotels()
        hotel = await resolve_hotel(self.runtime, hotel_name)
        return [hotel] if hotel is not None else []

    async def _audit_counts(self, hotel_ids: list[int] | None = None) -> dict[str, int]:
        """审计分状态计数(四种状态都补齐 0,便于直接展示)。"""
        stmt = select(ReviewReply.status, func.count()).group_by(ReviewReply.status)
        if hotel_ids:
            stmt = stmt.where(ReviewReply.hotel_id.in_(hotel_ids))
        async with self.runtime.db.session() as session:
            rows = (await session.execute(stmt)).all()
        counts = dict.fromkeys(REPLY_STATUSES, 0)
        for status, count in rows:
            counts[str(status)] = int(count)
        counts["total"] = sum(v for k, v in counts.items() if k != "total")
        return counts

    async def _latest_audit(self, hotel_id: int, review_id: str) -> dict[str, Any] | None:
        """该点评最新一条审计行(append-only 表里 ``id`` 最大的那条)。"""
        stmt = (
            select(ReviewReply)
            .where(ReviewReply.hotel_id == int(hotel_id), ReviewReply.review_id == str(review_id))
            .order_by(ReviewReply.id.desc())
            .limit(1)
        )
        async with self.runtime.db.session() as session:
            row = (await session.execute(stmt)).scalars().first()
        if row is None:
            return None
        return {
            "id": int(row.id),
            "status": row.status,
            "strategy": row.strategy,
            "content": row.content,
            "exec_by": row.exec_by,
        }

    @staticmethod
    def _audit_line(audit: dict[str, int]) -> str:
        """旧 ``commands.py:495-496`` 的审计行文案(逐字)。"""
        return (
            f"  审计:建议中 {audit.get('suggested', 0)} / 已回复 {audit.get('ok', 0)}"
            f" / 已忽略 {audit.get('ignored', 0)} / 自动失败 {audit.get('failed', 0)}"
        )

    @staticmethod
    def _policy_text(policy: dict[str, Any]) -> str:
        """旧 ``commands.py:490-492`` 的策略文案(逐字)。"""
        text = f"好评={policy.get('good')} / 差评={policy.get('bad')}"
        if policy.get("bad_template"):
            text += f"(bad_template={policy.get('bad_template')})"
        return text


def _make_extractor(extractor_cls: Any, runtime: Any) -> Any:
    """构造段1 点评提取器。

    段1 当前的 :class:`ReviewExtractor` **没有 ``__init__`` 参数**,
    但 ``jobs.py`` 的调用形态是 ``ReviewExtractor(runtime.rules)`` —— 两种写法都兼容,
    以免并行开发期间任一侧改动导致刷新整条链路失效(失败会被记进 ``errors``,不静默)。
    """
    try:
        return extractor_cls()
    except TypeError:
        return extractor_cls(getattr(runtime, "rules", None))


async def check_drafted_for(runtime: Any, hotel_id: Any, review_id: Any) -> bool:
    """便捷转发(命令层想直接判"是否已草稿"时用,避免 import 到 ``draft`` 内部)。"""
    return await check_drafted(runtime, hotel_id, review_id)
