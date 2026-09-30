"""预警域入口(任务与命令的单一门面)—— :class:`AlertService`。

职责(计划书 §5.7 / §6 批次 E)
==============================

::

    engine.check ──→ render.render_trigger ──→ shots.capture ──→ push_triggers ──→ alert_logs
      判定+状态          文案                     附图              投递+留痕

``check()`` 就是这四步的组合;其余方法对应命令:「预警测试」(``dry_run``)、
「忽略此店 X N」(``ignore_hotel``)、「预警线」(``set_lines`` / ``lines``)、
「状态」(``status``)、每日汇总(``summary``)。

推送目标(★ T2E.5 / V52)
=========================

====================  ====================================================================
目标                   规则
====================  ====================================================================
管理群                 ``settings.push.manage_chatids`` **全量**,一个群一条
运营群                 该店**绑定的群**(``Bindings.groups_of_hotel``,已过滤 ``paused``)
无管理群               **照写一行** ``recipient='manage-none'`` / ``pushed=false``,
                       并计入送达率分母(V52)
====================  ====================================================================

★ **"回退 ``hotels.group_chatid``" 在段2 的 schema 下不可用**
------------------------------------------------------------

旧系统 ``alert_push.ops_chatids_for_hotel``(``:132-142``)在"没有群绑定"时回退到
``hotels.group_chatid``。新架构里这个列**不存在** —— ``core_hotels``
(``infra/models/core.py:74-95``)只有 ``id/name/city/account_id/ebk_hotel_id/status/
review_policy/created_at``:绑定关系被独立到 ``core_group_bindings``
(计划书 §4.2「取消 hotels.group_chatid 单列,群绑定独立成表」)。

所以本模块**明确记 warning 并只用绑定群**:没有绑定就是"这家店没进任何群",
不该悄悄发到一条没人维护的历史 chatid 上。要接回退路径必须先给
``core_hotels`` 加列 —— 那是段3 的 schema 变更,不在段2 范围。

``send_alert``(D1)与 ``health``(D18)如何被消费
================================================

* **D1**:所有出站都走 ``PushService.send_alert``(内部遍历**在线**机器人),
  它返回 :class:`AlertResult`(**逐群结果**:``delivered`` / ``failed`` / ``error``),
  **绝不是裸 bool**。本模块用 ``delivered``/``failed`` 逐 target 写 ``alert_logs``:
  哪个群没送到、为什么没送到,日志里查得到。
* **D18**:``BotManager.health()`` 返回 ``dict[str, bool]``(``{机器人名: 在线}``)。
  旧系统消费方读的是不存在的 ``health["online"]`` → 恒为假。本模块的 ``status()``
  用 ``sum(health.values())`` 数在线数,**不按名字取键**。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import select

from hoteldata.domains.alert import engine, render, shots
from hoteldata.domains.alert.rules import (
    AlertRule,
    hotel_lines,
    load_lines,
    load_rules,
    set_alert_lines,
)
from hoteldata.domains.alert.shots import load_shots
from hoteldata.domains.alert.state import AlertStateStore
from hoteldata.domains.alert.summary import RECIPIENT_NO_MANAGE, build_daily_summary, write_log
from hoteldata.infra.models import Hotel
from hoteldata.push.bindings import Bindings
from hoteldata.push.service import PushService

__all__ = ["AlertService"]


class AlertService:
    """预警域的门面(任务 ``alert.room`` / ``alert.data`` / ``alert.summary`` 与命令共用)。"""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.state = AlertStateStore(runtime.db)

    # ==================================================================
    # 依赖(冻结契约是 ``runtime.push`` / ``runtime.bindings``;缺失时兜底装配)
    # ==================================================================

    @property
    def push(self) -> PushService:
        """推送服务(``runtime.push``;未装配时**就地建一个**并回挂)。

        注:兜底装配出来的 ``PushService`` 没有 ``BotManager``(``manager=None``),
        此时 ``send_alert`` 会返回**带 error 的结果**(不是假装成功),
        每一行 ``alert_logs`` 都会带上"BotManager 未装配" —— 静默失败被挡住。
        """
        push = getattr(self.runtime, "push", None)
        if push is not None:
            return push
        from hoteldata.push.audit import PushAudit

        push = PushService(
            settings=self.runtime.settings,
            audit=PushAudit(self.runtime.db),
            bindings=self.bindings,
        )
        self.runtime.push = push
        logger.warning("Runtime 未装配 push,PushService 已就地构建(无 BotManager,告警将记失败)")
        return push

    @property
    def bindings(self) -> Bindings:
        """群绑定(``runtime.bindings``;未装配时就地建一个)。"""
        bindings = getattr(self.runtime, "bindings", None)
        if bindings is not None:
            return bindings
        bindings = Bindings(self.runtime.db)
        self.runtime.bindings = bindings
        return bindings

    # ==================================================================
    # 巡检 + 推送
    # ==================================================================

    async def check(
        self,
        slot: str,
        *,
        today: date | None = None,
        dry_run: bool = False,
        rule_id: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """巡检入口(任务 ``alert.room`` ``"09:04"`` / ``alert.data`` ``"09:10"``)。

        ``slot`` 是**调度层时刻**;引擎会映回名义时刻再匹配 ``check_times``(V49)。
        ``dry_run=True``(**命令「预警测试」**)只判定、**不写状态/不写日志/不发送**。

        返回引擎汇总 dict + ``pushed`` / ``failed`` / ``logged`` / ``images``
        (直接进 ``job_runs.summary``)。
        """
        if not bool(self.runtime.settings.alert.enabled):
            logger.info("预警已关闭(ALERT_ENABLED=0),slot={} 不巡检", slot)
            return {"ok": True, "skipped": "ALERT_ENABLED=0", "slot": slot}
        day = today or date.today()
        result = await engine.check(
            self.runtime, slot, today=day, dry_run=dry_run, rule_id=rule_id, force=force
        )
        out = result.as_dict()
        out["ok"] = True
        out["dry_run"] = bool(dry_run)
        if dry_run:
            out.update({"pushed": 0, "failed": 0, "logged": 0, "images": 0, "manage_none": 0})
            return out
        pushed = await self.push_triggers(result.triggers, today=day, images=True)
        out.update(
            {
                "pushed": pushed["pushed"],
                "failed": pushed["failed"],
                "logged": pushed["logged"],
                "images": pushed["images"],
                "manage_none": pushed["manage_none"],
                # ★ 原为 ``deduped_logs``(DO NOTHING 时代的"被去重挡掉几行")。
                #   改成 UPSERT 后不再有"被挡掉"的行,取而代之的是**刷新**了几行
                #   —— 重推/补跑刷旧行时这个数 >0,是"重推真的生效了"的证据。
                "refreshed_logs": pushed["refreshed_logs"],
            }
        )
        if pushed["errors"]:
            out.setdefault("errors", []).extend(pushed["errors"])
        return out

    async def push_triggers(
        self, triggers: list[Any], *, today: date, images: bool = True
    ) -> dict[str, Any]:
        """渲染 → 附图 → 投递(管理群全量 + 该店运营群)→ **逐目标写一行 ``alert_logs``**。

        图片失败**不影响文本**(V53):``shots.capture`` 失败返回 ``None``,这里照常发。

        ``logged`` = 本次写了多少行(新建 + 刷新);``refreshed_logs`` = 其中**刷新**
        旧行的数量(重推/补跑同一 触发×目标 时 >0)。
        """
        out: dict[str, Any] = {
            "triggers": len(triggers),
            "pushed": 0,
            "failed": 0,
            "logged": 0,
            "refreshed_logs": 0,
            "manage_none": 0,
            "images": 0,
            "errors": [],
        }
        if not triggers:
            return out
        push = self.push
        rule_set = load_rules()
        for trigger in triggers:
            try:
                rule = rule_set.get(str(getattr(trigger, "rule_id", "")))
                targets = await self._resolve_targets(trigger, rule)
                if not targets:
                    out["errors"].append(
                        f"{trigger.rule_id}|{trigger.hotel_name}: 无任何推送目标(管理群与绑定群均空)"
                    )
                    logger.warning(
                        "预警无推送目标:规则={} 店={}(管理群 {} 个 / 该店绑定群 0 个)",
                        trigger.rule_id,
                        trigger.hotel_name,
                        len(self.runtime.settings.push.manage_chatids),
                    )
                text = render.render_trigger(trigger, settings=self.runtime.settings)
                image_list: list[str] = []
                if images:
                    shot = await shots.capture(self.runtime, trigger, day=today)
                    if shot:
                        image_list = [shot]
                        out["images"] += 1
                stamp = datetime.now()
                if not self.runtime.settings.push.manage_chatids:
                    # ★ V52:无管理群也要留一行,计入送达率分母(否则"没人可发"= 100%)
                    written = await write_log(
                        self.runtime,
                        trigger,
                        recipient=RECIPIENT_NO_MANAGE,
                        pushed=False,
                        error="未配置管理群(MANAGE_CHATIDS)",
                        images=image_list,
                        pushed_at=stamp,
                        content=text,
                        day=today,
                    )
                    out["manage_none"] += 1
                    out["logged"] += 1
                    if not written.created:
                        out["refreshed_logs"] += 1
                    logger.warning(
                        "预警未配管理群:规则={} 店={}(已记 manage-none 行,计入送达率分母)",
                        trigger.rule_id,
                        trigger.hotel_name,
                    )
                if not targets:
                    continue
                chatids = [c for c, _ in targets]
                result = await push.send_alert(text, chatids=chatids)
                delivered = set(result.delivered)
                failed = dict(result.failed)
                trigger.chatids = chatids
                for chatid, _kind in targets:
                    ok = chatid in delivered
                    error = None if ok else (failed.get(chatid) or result.error or "未送达")
                    # ★ ``write_log`` 是 UPSERT:同 (触发×目标) 重推会**刷新**该行,
                    #   而不是被 DO NOTHING 丢掉 —— 于是"重推成功了但日志还写着 failed"
                    #   这个坑不存在了。``created`` 区分新建/刷新,计数不撒谎。
                    written = await write_log(
                        self.runtime,
                        trigger,
                        recipient=chatid,
                        pushed=ok,
                        error=error,
                        images=image_list,
                        pushed_at=stamp if ok else None,
                        content=text,
                        day=today,
                    )
                    out["logged"] += 1
                    if not written.created:
                        out["refreshed_logs"] += 1
                    if ok:
                        out["pushed"] += 1
                    else:
                        out["failed"] += 1
                        out["errors"].append(f"{trigger.rule_id}|{chatid}: {error}")
                        logger.warning(
                            "预警推送失败:规则={} 店={} 群={} err={}",
                            trigger.rule_id,
                            trigger.hotel_name,
                            chatid,
                            error,
                        )
            except Exception as exc:  # noqa: BLE001 - 单条触发失败不阻断其余
                logger.exception("预警推送异常:rule={}", getattr(trigger, "rule_id", "?"))
                out["errors"].append(f"{getattr(trigger, 'rule_id', '?')}: {exc}")
        logger.info(
            "预警推送汇总:触发 {} 成功 {} 失败 {} 日志 {} 图 {}",
            out["triggers"],
            out["pushed"],
            out["failed"],
            out["logged"],
            out["images"],
        )
        return out

    async def _resolve_targets(self, trigger: Any, rule: AlertRule | None) -> list[tuple[str, str]]:
        """解析推送目标 → ``[(chatid, "manage"|"ops")]``(去重保序,管理群在前)。"""
        chatids = list(self.runtime.settings.push.manage_chatids)
        targets: list[tuple[str, str]] = []
        if rule is None or rule.wants("group"):
            targets.extend((c, "manage") for c in chatids)
        if rule is None or rule.wants("ops"):
            ops = await self.bindings.groups_of_hotel(int(getattr(trigger, "hotel_id", 0) or 0))
            if not ops:
                # ★ 回退路径不可用:core_hotels **没有** group_chatid 列(见模块 docstring)
                logger.warning(
                    "店「{}」无绑定运营群;core_hotels 无 group_chatid 列可回退(schema 差异),"
                    "本次只推管理群",
                    getattr(trigger, "hotel_name", ""),
                )
            targets.extend((c, "ops") for c in ops)
        seen: dict[str, str] = {}
        for chatid, kind in targets:
            if chatid and chatid not in seen:
                seen[chatid] = kind
        return list(seen.items())

    # ==================================================================
    # 每日汇总
    # ==================================================================

    async def summary(self, *, day: date | None = None, push: bool = True) -> dict[str, Any]:
        """每日汇总(任务 ``alert.summary``,09:30,``catch_up=False``)。

        汇总本身推管理群(**每个群各一条**);送达率统计**只算预警 ``alert_logs``** ——
        汇总自己不进 ``alert_logs``:该表 ``hotel_id`` 是 NOT NULL(整表行都归属某家店),
        把"全局汇总"塞进去会污染送达率分母。
        """
        target = day or date.today()
        text, stats = await build_daily_summary(self.runtime, day=target)
        out: dict[str, Any] = {"ok": True, "day": target.isoformat(), "stats": stats, "text": text}
        chatids = list(self.runtime.settings.push.manage_chatids)
        if not push:
            out["pushed"] = False
            return out
        if not chatids:
            logger.warning("预警汇总无管理群可推(MANAGE_CHATIDS 为空):统计已生成")
            out.update({"pushed": False, "sent": 0, "failed": 0, "error": "未配置管理群(MANAGE_CHATIDS)"})
            return out
        result = await self.push.send_alert(text, chatids=chatids)
        out.update(
            {
                "pushed": bool(result.delivered),
                "sent": len(result.delivered),
                "failed": len(result.failed),
                "targets": chatids,
                "errors": [f"{c}: {e}" for c, e in result.failed],
            }
        )
        if result.error:
            out["error"] = result.error
        if not result.delivered:
            logger.error("预警汇总未送达任何管理群:{}", result.error or result.failed)
        return out

    # ==================================================================
    # 状态 / 忽略 / 预警线
    # ==================================================================

    async def status(self, hotel_name: str | None = None) -> dict[str, Any]:
        """「状态」命令用:规则 / 酒店 / 状态行 / 忽略 / 预警线 / 附图目标 / 机器人健康。

        ★ D18:``health`` 是 ``{机器人名: bool}`` —— 这里 ``sum(health.values())`` 数在线数,
        **不写 ``health["online"]``**(旧系统就是这么拿到"永远 0 台在线"的)。
        """
        rule_set = load_rules()
        hotels = await self.hotels(hotel_name)
        hotel_ids = [int(h.id) for h in hotels]
        states: list[dict[str, Any]] = []
        for hotel_id in hotel_ids:
            for row in await self.state.list_states(hotel_id=hotel_id):
                states.append(
                    {
                        "hotel_id": hotel_id,
                        "rule_id": row.rule_id,
                        "entity_key": row.entity_key,
                        "status": row.status,
                        "streak": row.streak,
                        "last_trigger_date": row.last_trigger_date.isoformat()
                        if row.last_trigger_date
                        else None,
                        "ignored_until": row.ignored_until.isoformat() if row.ignored_until else None,
                    }
                )
        snapshot: dict[str, Any] = {}
        try:
            snapshot = self.push.snapshot()
        except Exception as exc:  # noqa: BLE001 - 状态查询不该因为推送层异常而失败
            logger.warning("读取推送快照失败:{}", exc)
        health = ((snapshot.get("bots") or {}).get("health")) or {}
        shot_targets = load_shots(settings=self.runtime.settings)
        return {
            "enabled": bool(self.runtime.settings.alert.enabled),
            "rules": [
                {
                    "id": r.id,
                    "name": r.name,
                    "check_times": list(r.check_times),
                    "template": r.template,
                    "dedup": r.dedup,
                    "to": list(r.to),
                    "has_shot": r.id in shot_targets,
                }
                for r in rule_set.rules
            ],
            "rule_warnings": list(rule_set.warnings),
            "slot_map": dict(engine.ROOM_SLOT_BY_TIME) | dict(engine.DATA_SLOT_BY_TIME),
            "hotels": [{"id": int(h.id), "name": str(h.name), "city": h.city} for h in hotels],
            "states": states,
            "lines": load_lines(settings=self.runtime.settings).get("hotels") or {},
            "bots": {
                # ★ D18:dict[str, bool] → 数在线数,不按 "online" 之类不存在的键取值
                "total": len(health),
                "online": sum(1 for v in health.values() if v),
                "names": sorted(health),
            },
        }

    async def hotels(self, hotel_name: str | None = None) -> list[Hotel]:
        """列酒店(``hotel_name`` 为空 = 全部;否则先精确匹配,再模糊匹配)。"""
        async with self.runtime.db.session() as s:
            if not hotel_name:
                stmt = select(Hotel).order_by(Hotel.id)
                return list((await s.execute(stmt)).scalars().all())
            exact = list(
                (await s.execute(select(Hotel).where(Hotel.name == hotel_name))).scalars().all()
            )
            if exact:
                return exact
            like = list(
                (
                    await s.execute(
                        select(Hotel).where(Hotel.name.ilike(f"%{hotel_name}%")).order_by(Hotel.id)
                    )
                )
                .scalars()
                .all()
            )
            return like

    async def ignore_hotel(self, hotel_name: str, days: int = 7) -> dict[str, Any]:
        """「忽略此店 X N」:对该店**每条规则** + ``__all__`` 写 ``__hotel__`` 忽略行。

        ``days=7`` → ``until = today + 6``(含当天共 7 天),与
        ``ignored_until >= today`` 的判定配套。
        """
        hotels = await self.hotels(hotel_name)
        if not hotels:
            return {"ok": False, "error": f"未找到酒店「{hotel_name}」"}
        if len(hotels) > 1:
            return {
                "ok": False,
                "error": f"「{hotel_name}」匹配到 {len(hotels)} 家店,请用完整店名",
                "hotels": [str(h.name) for h in hotels],
            }
        hotel = hotels[0]
        span = max(1, int(days))
        until = date.today() + timedelta(days=span - 1)
        rule_ids = [r.id for r in load_rules().rules]
        count = await self.state.ignore_hotel(int(hotel.id), until, rule_ids)
        return {
            "ok": True,
            "hotel": str(hotel.name),
            "hotel_id": int(hotel.id),
            "days": span,
            "until": until.isoformat(),
            "rules": count,
        }

    async def set_lines(
        self, hotel_name: str, high: float | None, low: float | None
    ) -> bool:
        """命令「预警线」:写 ``alert_lines.json``(两者都 ``None`` = 取消该店预警线)。"""
        return set_alert_lines(
            hotel_name, high, low, settings=self.runtime.settings
        )

    async def lines(self) -> dict[str, Any]:
        """读 ``alert_lines.json`` 全文(命令「预警线」的查询分支)。"""
        return load_lines(settings=self.runtime.settings)

    async def hotel_line(self, hotel_name: str) -> tuple[float | None, float | None]:
        """某店的 ``(high_line, low_line)``。"""
        return hotel_lines(hotel_name, settings=self.runtime.settings)
