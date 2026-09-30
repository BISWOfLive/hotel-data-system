"""★ 段2 **本地端到端联调** —— 用真 WebSocket 把整条链路跑一遍。

与 ``verify_acceptance2.py`` 的分工
===================================

* 验收器回答"**每一条契约对不对**"(V21–V60,逐项断言,含负例);
* 本脚本回答"**这些东西连起来能不能用**" —— 装配、入站消息、回复、定时推送、
  预警、审计,全部走**同一根 WebSocket**,像真群里发生的那样。

它替掉的是**真实企微服务器**,不是业务代码:本地起的
:class:`FakeWeComServer` 逐字节按 :mod:`hoteldata.domains.bot.protocol`
收发帧,而被测的这一侧是完全真实的
``Runtime`` → ``BotManager`` → ``BotClient`` → ``MessageRouter`` →
``commands`` / ``report`` / ``alert`` → ``PushService`` → ``push_logs`` / ``alert_logs``。

覆盖的场景(每条都打点)
=======================

1. **网关上线** —— ``start_bots()`` 真连 WS、订阅成功;
2. **入站消息 → 回复** —— 群里发 8 条,验证**回复帧回填了入站 ``req_id``**(A1-10)、
   命令最长前缀、管理群白名单、私聊回退、实时问答、FAQ 兜底;
3. **「今日数据」触发真实日报** —— 文本 + **逐张图片**(A1-13:无 news 卡片);
4. **定时推送** —— 直接跑 ``push.daily`` 任务,验证同一根 WS 上收到日报;
5. **预警** —— 真跑 ``alert.data``,验证**管理群 + 运维群都收到**,且落 ``alert_logs``;
6. **审计** —— ``push_logs`` / ``alert_logs`` / ``job_runs`` 三张表都能查到本次联调;
7. **断电** —— 关掉本地服务端 → 客户端应判死并重连 → 恢复后再上线。

用法::

    .venv\\Scripts\\python.exe scripts\\local_e2e.py
    .venv\\Scripts\\python.exe scripts\\local_e2e.py --keep    # 保留合成数据便于翻库
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# ★ 复用验收器里那个"逐字节实现企微帧格式"的本地服务端 —— 不重复实现一遍协议,
#   免得两边漂移(联调与验收对着**同一个**协议替身)。
from verify_acceptance2 import (  # noqa: E402
    SYNTH_GROUP2,
    SYNTH_MANAGE,
    SYNTH_OPS,
    FakeWeComServer,
    _cleanup,
    _ensure_hotel,
    _seed_module,
    _seed_portal,
    _seed_shot,
)

from hoteldata.logging import configure_stdio  # noqa: E402
from hoteldata.settings import get_settings, reload_settings  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
E2E_GROUP = "e2e-group-0001"
#: 一个**不在** ``MANAGE_CHATIDS`` 里的群(验白名单拒绝要用它)
E2E_PLAIN_GROUP = "e2e-plain-0001"
#: 掉线期推送探针用的群 —— ★ 必须一起清理,否则上一轮留下的 `ok` 行会让
#: 这一轮的探针被**当日去重**挡掉("重跑结果不一致"的经典来源)
E2E_PROBE_GROUP = "e2e-offline-probe"
E2E_HOTEL = "联调-测试酒店"

_steps: list[tuple[str, bool, str]] = []


def step(name: str, ok: bool, evidence: str = "") -> None:
    """打一个联调步骤点(顺序即时间线)。"""
    _steps.append((name, bool(ok), evidence))
    icon = "\033[92m✓\033[0m" if ok else "\033[91m✗\033[0m"
    print(f"  {icon} {name}" + (f"\n      {evidence}" if evidence else ""))


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def _registry_names() -> list[str]:
    """当前任务注册表的任务名(需先 ``import hoteldata.jobs``)。"""
    import hoteldata.jobs  # noqa: F401
    from hoteldata.infra.tasks import get_registry

    return get_registry().names()


async def _scheduled_ids() -> list[str]:
    """``hoteldata serve`` 那条装配路径上,调度器**真正装载**的任务 id。

    ★ 与 :func:`_registry_names` 对比就是本条联调的要点:旧代码这两者
    一个是 19、一个是 **0**,而启动日志只打印前者。

    ★ 本函数是 ``async`` 的:要在**已经运行的事件循环里** await。
    写成同步 + ``asyncio.run`` 会撞 "cannot be called from a running event loop"
    —— CLI 的 CliRunner 那次踩的是同一个坑。
    """
    from hoteldata.runtime import Runtime as _RT

    cm = _RT.create(get_settings(), with_scheduler=True)
    rt = await cm.__aenter__()
    rt._extras["__cm__"] = cm
    try:
        sched = rt.scheduler
        return sorted(j.id for j in (sched.get_jobs() if sched is not None else []))
    finally:
        await rt.aclose()


class _Bots:
    """临时插一行机器人(假凭据,真连接本地 WS),用完删掉。"""

    def __init__(self, name: str = "__e2e_bot__") -> None:
        self.name = name
        self.id: int | None = None

    async def create(self, rt: Any) -> None:
        from hoteldata.infra.crypto import get_cipher
        from hoteldata.infra.models import Bot

        cipher = get_cipher(rt.settings)
        async with rt.db.session() as s:
            bot = Bot(
                name=self.name,
                bot_id_enc=cipher.encrypt("e2e-bot-id"),
                secret_enc=cipher.encrypt("e2e-secret"),
            )
            s.add(bot)
            await s.flush()
            self.id = int(bot.id)

    async def drop(self, rt: Any) -> None:
        from sqlalchemy import delete

        from hoteldata.infra.models import Bot

        async with rt.db.session() as s:
            await s.execute(delete(Bot).where(Bot.name == self.name))


async def _seed_today(rt: Any, hotel: Any) -> dict[str, Any]:
    """给合成酒店造**今天**的数据 + 轮换图,让日报/预警/报告都有真内容。"""
    from hoteldata.domains.collect.rotation import get_rotation

    hotel_id = int(hotel.id)
    today = date.today()
    plan = get_rotation().pick(today)
    # ★ 轮换清单里的 page 可能不在 api_rules(如「违约看板/违规中心」整页项)——
    #   播种模块记录前先确认该页在规则里,否则会造出一条永远取不到的无主记录。
    from hoteldata.domains.collect.service import today_module_shots  # noqa: F401

    for item in plan.items:
        page = item.page or "经营报告"
        if _page_known(rt, page):
            await _seed_module(rt, hotel_id, page, item.name, "昨日", {"验收指标": 1}, day=today)
    made: list[str] = []
    for item in plan.items[:2]:
        alias = (item.alias or [item.name])[0]
        made.append(await _seed_shot(rt, hotel, item.page or "经营报告", alias, day=today))
    # 「离店」用于实时问答;首页待办用于预警
    await _seed_module(rt, hotel_id, "经营报告", "离店", "昨日", {"离店间夜": 12}, day=today)
    await _seed_portal(rt, hotel_id, "home_pending", {"comment_pending": "3"}, day=today)
    return {"rotation_items": [i.name for i in plan.items], "shots": made}


def _page_known(rt: Any, page: str) -> bool:
    """该页名是否出现在 ``api_rules.json`` 里(轮换清单里有整页项不是采集页)。"""
    try:
        return any(p == page for p in rt.rules.pages)
    except Exception:  # noqa: BLE001 - 规则接口有变动就别拦着联调
        return True


# ---------------------------------------------------------------------------
# 联调主流程
# ---------------------------------------------------------------------------


async def run(keep: bool = False) -> int:
    from hoteldata.infra.models import AlertLog, PushLog  # noqa: F401
    from hoteldata.runtime import Runtime

    server = FakeWeComServer()
    await server.start()
    print(f"\n本地企微服务端已起:{server.url}\n")

    # ★ 装配顺序与 ``hoteldata serve`` 的 lifespan 完全一致:
    #   start_bots() → start_push() → (调度器) → 补跑
    settings = reload_settings(
        AIBOT_WS_URL=server.url,
        AIBOT_HEARTBEAT_S=1.0,  # 加快心跳,便于验判死重连
        AIBOT_HEARTBEAT_TIMEOUT_S=0.4,
        MANAGE_CHATIDS=f"{SYNTH_MANAGE},{E2E_GROUP}",
        OPS_CHATID=SYNTH_OPS,
        PUSH_MIN_INTERVAL_S=0.05,  # 联调不按生产的 2.0s 等(那条由 V35 单独断言)
        PUSH_GROUP_MIN_INTERVAL_S=0.05,
    )
    cm = Runtime.create(settings, with_scheduler=False)
    rt = await cm.__aenter__()
    rt._extras["__cm__"] = cm
    bots = _Bots()
    exit_code = 1
    try:
        # ==============================================================
        print("【0】装配顺序(★ 这条正是本地联调抓出来的那个 bug)")
        # ==============================================================
        # `hoteldata serve` 走的是 ``Runtime.create(with_scheduler=True)``,而调度器
        # 是**按当时的注册表**建任务的。旧代码把 `import hoteldata.jobs` 写在
        # ``async with`` 里面 → 调度器拿空注册表建成 ``get_jobs()==0``,
        # 启动日志却照样打印"已注册任务 19 个" → **所有定时推送静默不触发**。
        from hoteldata.runtime import Runtime as _RT

        cm_bad = _RT.create(get_settings(), with_scheduler=True)
        rejected = ""
        try:
            rt_bad = await cm_bad.__aenter__()
        except RuntimeError as exc:
            rejected = str(exc).splitlines()[0]
        else:
            await rt_bad.aclose()
        step(
            "空注册表 → 拒绝启动 0 任务的调度器(启动即失败,不静默降级)",
            bool(rejected),
            rejected or "✗ 没拦住",
        )
        # 正例:**先**导入 jobs(触发 @task 注册),再 create → 调度器装满
        registry = _registry_names()  # 本函数内部会 `import hoteldata.jobs`
        scheduled = await _scheduled_ids()
        step(
            "先 import jobs 再 create(with_scheduler=True) → 调度器装载全部任务",
            len(scheduled) == len(registry) and len(scheduled) > 0,
            f"scheduler.get_jobs()={len(scheduled)} / registry={len(registry)}",
        )

        # ==============================================================
        print("\n【1】装配与上线")
        # ==============================================================
        await bots.create(rt)
        manager = await rt.start_bots()
        await rt.start_push()
        rt.push.dispatcher.min_interval_s = 0.05
        rt.push.dispatcher.group_min_interval_s = 0.05
        rt.push.dispatcher.retry_backoff_s = (0.05, 0.1, 0.2)

        for _ in range(80):
            if manager.online:
                break
            await asyncio.sleep(0.05)
        health = manager.health()
        step(
            "start_bots() 真连本地 WS 并订阅成功",
            bool(manager.online) and health == {"__e2e_bot__": True},
            f"health={health}(D18 统一契约 dict[str,bool]) / subscribe_count={server.subscribe_count}",
        )
        step(
            "PushService 与网关共用同一实例(§4.14 根因防线)",
            rt.push.manager is rt.bots and rt.sender.manager is rt.bots,
            "push.manager is runtime.bots ✓ / sender.manager is runtime.bots ✓",
        )

        # ==============================================================
        print("\n【2】造数据 + 绑群")
        # ==============================================================
        hotel = await _ensure_hotel(rt.db, E2E_HOTEL)
        await _cleanup(rt.db, int(hotel.id))
        # ★ 先清掉**上一轮**遗留的探针行:否则 (群,类型,slot) 已有 ok 行 →
        #   这一轮的探针会被当日去重挡掉,表现为"重跑结果不一致"。
        from sqlalchemy import delete as _delete

        from hoteldata.infra.models import PushLog as _PushLog

        async with rt.db.session() as s:
            for g in (E2E_GROUP, SYNTH_MANAGE, SYNTH_OPS, E2E_PROBE_GROUP, E2E_PLAIN_GROUP):
                await s.execute(_delete(_PushLog).where(_PushLog.group_chatid == g))
        seed = await _seed_today(rt, hotel)
        await rt.bindings.bind(E2E_GROUP, int(hotel.id))
        step(
            f"绑定 {E2E_GROUP} → 「{E2E_HOTEL}」",
            [h.hotel_id for h in await rt.bindings.for_group(E2E_GROUP)] == [int(hotel.id)],
            f"当日轮换 {len(seed['rotation_items'])} 项 / 造图 {len(seed['shots'])} 张",
        )

        # ==============================================================
        print("\n【3】入站消息 → 回复(★ 验回填 req_id)")
        # ==============================================================
        async def ask(text: str, chatid: str | None = E2E_GROUP, *, wait: float = 15.0) -> tuple[str, str]:
            rid = await server.push_inbound(text, chatid=chatid)
            await server.await_reply(rid, timeout_s=wait)
            return rid, server.reply_text(rid)

        rid, reply = await ask("帮助")
        step(
            "「帮助」→ 19 行命令帮助(逐字继承)",
            "群内命令帮助" in reply and "预警线" in reply,
            f"回复 {len(reply)} 字;req_id 回填={'是' if server.replies_to(rid) else '否'}"
            f"(入站 {rid})",
        )

        # ★ A1-10:每一条回复都必须带**该条入站**的 req_id
        ok_echo = all(
            all(f["headers"]["req_id"] == r for f in server.replies_to(r))
            for r in [rid]
        )
        step("★ 回复帧回填入站 req_id(A1-10)", ok_echo, f"aibot_respond_msg.headers.req_id == {rid}")

        _, reply = await ask(f"绑定 {E2E_HOTEL}")
        step("「绑定 <酒店>」幂等命中", "已绑定" in reply, reply.strip().splitlines()[0][:60])

        _, reply = await ask("我的酒店")
        step("「我的酒店」列出本群绑定", E2E_HOTEL in reply, reply.strip()[:60])

        _, reply = await ask("状态", E2E_PLAIN_GROUP)
        step(
            "「状态」在**非**管理群被拒(白名单生效)",
            "仅管理群可用" in reply,
            f"非管理群 {E2E_PLAIN_GROUP} → {reply.strip()[:40]}",
        )

        _, reply = await ask("状态", SYNTH_MANAGE)
        step(
            "「状态」在管理群可用且机器人健康正确(D18)",
            "系统状态" in reply and "机器人" in reply,
            reply.strip().replace("\n", " | ")[:110],
        )

        _, reply = await ask("离店")
        step(
            "「离店」实时问答(问才发)",
            "离店" in reply and ("12" in reply or "间夜" in reply),
            reply.strip().replace("\n", " | ")[:90],
        )

        _, reply = await ask("完全无关的一句话甲乙丙丁")
        step(
            "无关消息 → 只回兜底文案",
            "暂未找到相关内容" in reply,
            reply.strip()[:50],
        )

        _, reply = await ask("绑定 隐欲民宿", chatid=None)  # 私聊
        step(
            "私聊(无 chatid)→ 命令不生效,回退问答",
            "暂未找到相关内容" in reply,
            "命令链回 None → 走 FAQ → 兜底",
        )

        ev_rid = await server.push_event("enter_chat")
        frames = await server.await_reply(ev_rid, timeout_s=15.0)
        welcome = ""
        for f in frames:
            body = f.get("body") or {}
            if body.get("msgtype") == "text":
                welcome = str((body.get("text") or {}).get("content") or "")
        used_welcome_cmd = bool(frames) and frames[0].get("cmd") == "aibot_respond_welcome_msg"
        step(
            "enter_chat → 5 秒内回欢迎语(A1-11)",
            used_welcome_cmd and "经营数据助手" in welcome,
            f"cmd={frames[0].get('cmd') if frames else None} / {welcome.strip()[:60] or '(未收到)'}",
        )

        # ==============================================================
        print("\n【4】「今日数据」→ 真实日报(文本 + 逐张图)")
        # ==============================================================
        before = len(server.sent_to(E2E_GROUP))
        rid, reply = await ask("今日数据", wait=25.0)
        sent = await server.await_sent(E2E_GROUP, count=before + 1, timeout_s=25.0)
        texts = server.texts_to(E2E_GROUP)
        media = server.media_to(E2E_GROUP)
        joined = "\n".join(texts)
        step(
            "「今日数据」触发真实日报投递",
            len(sent) > before and f"「{E2E_HOTEL}」" in joined,
            f"回执={reply.strip()[:40]!r} / 群内收到 {len(sent) - before} 条消息",
        )
        step(
            "日报标题行 + 📅 轮换行",
            f"### 「{E2E_HOTEL}」" in joined and "今日轮换" in joined,
            next((ln for ln in joined.splitlines() if "今日轮换" in ln), "")[:80],
        )
        step(
            "日报附图 ≥1 张且走 image 消息(A1-13:无 news 卡片)",
            len(media) >= 1 and "news" not in str(server.sent_to(E2E_GROUP)),
            f"media_id={media[:3]}(共 {len(media)} 张;上传 {len([u for u in server.uploads if u['cmd'] == 'init'])} 次)",
        )

        # ==============================================================
        print("\n【5】定时推送(push.daily 任务)")
        # ==============================================================
        from hoteldata.infra.models import PushLog

        before_cnt = len(server.sent_to(E2E_GROUP))
        # ★ ``force=True``:【4】的「今日数据」已经在本小时 slot 推过一次,
        #   不 force 的话这里会被**正确地**去重掉(去重本身另有一步专测)。
        res = await rt.tasks.run("push.daily", rt, trigger="manual", kwargs={"force": True})
        await server.await_sent(E2E_GROUP, count=before_cnt + 1, timeout_s=25.0)
        after_cnt = len(server.sent_to(E2E_GROUP))
        step(
            "push.daily 任务 → 群内收到日报",
            res.status == "ok" and after_cnt > before_cnt,
            f"job_runs summary={res.summary}",
        )
        async with rt.db.session() as s:
            from sqlalchemy import select

            rows = list(
                (
                    await s.execute(
                        select(PushLog)
                        .where(PushLog.group_chatid == E2E_GROUP)
                        .order_by(PushLog.id.desc())
                        .limit(5)
                    )
                )
                .scalars()
                .all()
            )
        step(
            "push_logs 有审计且 bot_id 为文本(D17)",
            bool(rows) and isinstance(rows[0].bot_id, str),
            f"{[(r.push_type, r.status, r.bot_id, r.media_count) for r in rows[:3]]}",
        )

        # 去重:不带 force 再跑一次 → 应记 skipped(不静默)
        before_dup = len(server.sent_to(E2E_GROUP))
        again = await rt.tasks.run("push.daily", rt, trigger="manual")
        await asyncio.sleep(0.6)
        async with rt.db.session() as s:
            skipped = list(
                (
                    await s.execute(
                        select(PushLog).where(
                            PushLog.group_chatid == E2E_GROUP, PushLog.status == "skipped"
                        )
                    )
                )
                .scalars()
                .all()
            )
        step(
            "当日去重命中 → 写 skipped 留痕、且不再发消息(不静默)",
            bool(skipped) and len(server.sent_to(E2E_GROUP)) == before_dup,
            f"第二次 push.daily(不 force):报告服务层 enqueued={(again.summary or {}).get('enqueued')} "
            f"→ 派发器层去重,审计新增 {len(skipped)} 行 skipped / 群内新增 "
            f"{len(server.sent_to(E2E_GROUP)) - before_dup} 条(应为 0)",
        )

        # ==============================================================
        print("\n【6】预警 → 管理群 + 运维群 + alert_logs")
        # ==============================================================
        from hoteldata.infra.models import AlertLog

        before_m = len(server.sent_to(SYNTH_MANAGE))
        # 记下本次巡检前的最大 alert_logs id —— 只统计**这一轮**产生的行
        async with rt.db.session() as s:
            from sqlalchemy import func as _func

            max_id_before = int(await s.scalar(_func.max(AlertLog.id)) or 0)
        ares = await rt.tasks.run("alert.data", rt, trigger="manual", kwargs={"force": True})
        await server.await_sent(SYNTH_MANAGE, count=before_m + 1, timeout_s=30.0)
        m_texts = server.texts_to(SYNTH_MANAGE)
        async with rt.db.session() as s:
            new_logs = list(
                (
                    await s.execute(
                        select(AlertLog).where(AlertLog.id > max_id_before).order_by(AlertLog.id)
                    )
                )
                .scalars()
                .all()
            )
        recipients = sorted({r.recipient for r in new_logs})
        step(
            "alert.data 任务执行",
            ares.status == "ok",
            f"triggers={(ares.summary or {}).get('trigger_count')} "
            f"pushed={(ares.summary or {}).get('pushed')} 本轮 alert_logs {len(new_logs)} 行",
        )
        step(
            "预警送达**管理群 + 该店绑定运营群**(计划书 §5.7 的目标口径)",
            len(m_texts) > 0 and any("预警" in t for t in m_texts) and E2E_GROUP in recipients,
            f"管理群 {len(m_texts)} 条 / 本轮收件人={recipients} "
            f"(规则 push.to 的 'ops' = 该店绑定运营群,**不是** OPS_CHATID)",
        )

        # OPS_CHATID 走的是**运维告警**通道(send_alert),与预警推送是两条路:
        # 机器人掉线 / 自检 —— 也正是 D1 的修复点。
        before_o = len(server.sent_to(SYNTH_OPS))
        alert_res = await rt.push.send_alert("联调:运维告警通道自检(D1 路径)")
        await server.await_sent(SYNTH_OPS, count=before_o + 1, timeout_s=15.0)
        o_texts = server.texts_to(SYNTH_OPS)
        step(
            "运维告警 → 管理群 + OPS_CHATID(★ D1:遍历在线机器人,返回逐群结果)",
            len(o_texts) > before_o and alert_res.ok and SYNTH_OPS in alert_res.delivered,
            f"AlertResult ok={alert_res.ok} delivered={alert_res.delivered} failed={alert_res.failed} "
            f"← 逐群结果(不是裸 bool);OPS_CHATID ∈ delivered ✓",
        )
        step(
            "预警文案无残留花括号",
            all("{" not in t for t in m_texts + o_texts),
            "占位符全部被清空",
        )
        async with rt.db.session() as s:
            alogs = list(
                (
                    await s.execute(
                        select(AlertLog).order_by(AlertLog.id.desc()).limit(6)
                    )
                )
                .scalars()
                .all()
            )
        step(
            "alert_logs 逐目标留痕 + 两个键各司其职",
            bool(alogs) and all(a.delivery_key and a.trigger_key for a in alogs),
            f"{[(a.recipient, a.pushed) for a in alogs[:4]]} / "
            f"delivery_key 以 trigger_key 为前缀="
            f"{all(a.delivery_key.startswith(a.trigger_key) for a in alogs)}",
        )

        # ==============================================================
        print("\n【7】断线 → 判死 → 重连(★ 这一步抓出过真 bug)")
        # ==============================================================
        connects_before = server.connects
        dropped = await server.drop_connections()
        # ① 掉线后 **必须立刻**对外报离线(不能继续报"在线")
        went_offline = False
        for _ in range(200):
            await asyncio.sleep(0.05)
            if not manager.health().get("__e2e_bot__", True):
                went_offline = True
                break
        step(
            "掉线后 health 立刻变「离线」(不继续谎报在线)",
            went_offline,
            f"掐断 {dropped} 条连接 → manager.health()={manager.health()} "
            f"(旧实现这里会一直报 True:ready/ws 只在**连上**时复位)",
        )
        # ② 期间推送应**失败并留审计**,而不是挂在死 socket 上
        from hoteldata.push.dispatcher import PushTask as _PushTask

        probe = await rt.push.dispatcher.deliver_now(
            _PushTask(
                chatid=E2E_PROBE_GROUP,
                push_type="e2e_offline_probe",
                content="掉线期间的推送探针",
                dedup_checked=True,
            )
        )
        step(
            "掉线期间的推送 → 明确失败(不静默、不挂起)",
            (not probe.ok) and bool(probe.error),
            f"ok={probe.ok} error={(probe.error or '')[:60]}",
        )
        # ③ 应自动重连回来(监听还在,只是连接被掐断)
        reconnected = False
        for _ in range(600):
            await asyncio.sleep(0.05)
            if server.connects > connects_before:
                reconnected = True
                break
        for _ in range(200):
            if manager.online:
                break
            await asyncio.sleep(0.05)
        step(
            "自动重连并重新订阅 → 恢复在线",
            reconnected and bool(manager.online),
            f"connects {connects_before} → {server.connects}(退避 min(2^n,30)s)/ health={manager.health()}",
        )
        # ④ 恢复后推送应重新可用
        recovered = await rt.push.dispatcher.deliver_now(
            _PushTask(
                chatid=E2E_PROBE_GROUP,
                push_type="e2e_offline_probe",
                content="恢复后的推送探针",
                dedup_checked=True,
            )
        )
        step(
            "恢复后推送重新可用",
            recovered.ok,
            f"ok={recovered.ok} bot={recovered.bot_id} error={(recovered.error or '')[:40]}",
        )

        # ==============================================================
        print("\n【8】审计总览")
        # ==============================================================
        stats = await rt.audit.day_stats(date.today())
        step(
            "当日推送审计(成功率口径)",
            stats["total"] >= 1,
            f"ok={stats['ok']} failed={stats['failed']} skipped={stats['skipped']} 成功率={stats['rate']:.1f}%",
        )
        status = await rt.status()
        step(
            "/status 暴露 bots + push 两段",
            "bots" in status and "push" in status,
            f"bots={status['bots']['bots']} 在线={status['bots']['online']} "
            f"pending={status['push']['dispatcher']['pending']}",
        )
        exit_code = 0 if all(ok for _n, ok, _e in _steps) else 1
    finally:
        await rt.aclose()
        await server.stop()
        # 清理
        if not keep:
            from sqlalchemy import delete

            from hoteldata.infra.models import PushLog

            cm2 = Runtime.create(get_settings(), with_scheduler=False)
            rt2 = await cm2.__aenter__()
            try:
                await bots.drop(rt2)
                h = await _ensure_hotel(rt2.db, E2E_HOTEL)
                await _cleanup(rt2.db, int(h.id))
                async with rt2.db.session() as s:
                    for g in (E2E_GROUP, E2E_PLAIN_GROUP, E2E_PROBE_GROUP, SYNTH_MANAGE, SYNTH_OPS, SYNTH_GROUP2):
                        await s.execute(delete(PushLog).where(PushLog.group_chatid == g))
            finally:
                await rt2.aclose()
        else:
            print("\n(--keep:合成数据保留,便于自己翻库)")
    return exit_code


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description="段2 本地端到端联调(真 WebSocket,假企微服务端)")
    parser.add_argument("--keep", action="store_true", help="保留合成数据便于翻库")
    args = parser.parse_args(argv)

    started = time.monotonic()
    code = asyncio.run(run(keep=args.keep))
    passed = sum(1 for _n, ok, _e in _steps if ok)
    total = len(_steps)
    print("\n" + "=" * 78)
    print(f"本地联调:{passed} / {total} 步通过,耗时 {time.monotonic() - started:.1f}s")
    failed = [n for n, ok, _e in _steps if not ok]
    if failed:
        print(f"未通过:{failed}")
    print("=" * 78)
    return code if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
