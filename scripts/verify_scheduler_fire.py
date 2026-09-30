"""★ 推送任务「到点真的会响吗」—— 本机全量可执行性检查。

为什么单独一个脚本
==================

* 验收器(V21–V60 / 段3)验的是**每条契约对不对**;
* ``local_e2e.py`` 验的是**链路连起来能不能用**;
* 但两者都**没有验过"APScheduler 到点真的会把任务叫起来"**:
  V59 只断言 ``scheduler.get_jobs() == N``(装进去了),``local_e2e`` 是**手工**
  ``rt.tasks.run(...)``(绕过了调度器)。

而"定时推送"的成败全在这一步 —— **装进去了不等于会响**。

本脚本把**全部 12 个推送类任务**的 cron 临时改成"每分钟",起**真调度器**等它自己响,
再沿着"cron 触发 → ``run()``(advisory lock + ``job_runs``)→ 领域逻辑 →
派发器 → WebSocket → ``push_logs``/``alert_logs``"一路验到底。

★ 刻意**不**碰的 4 个任务:``collect.modules`` / ``collect.portal`` / ``collect.review``
/ ``collect.room`` / ``collect.screenshots`` / ``compare.collect`` / ``ops.backup``
/ ``ops.cleanup`` / ``ops.patrol`` —— 它们会去**真实平台**取数或动文件,
不属于"推送"链路,本机不该被这个脚本触发。

验的是什么
==========

========================================  ==========================================
断言                                       含义
========================================  ==========================================
到点**之前**没有提前发                      不是"启动即跑一遍"的错觉
12 个推送任务**全部**出现 ``job_runs`` 行   真调度器逐个叫起来了(``trigger='schedule'``)
  且都跑到终态(ok/failed/skipped)          不是卡在 pending
``push.daily`` 到点之前之后的对比           报出每个任务的真实状态与耗时
日报真的到群:markdown + 逐张图              A1-12/A1-13,消息真的走出去了
预警真的到群 + ``alert_logs`` 逐目标留痕    送达率口径的两个键各司其职
``push_logs`` ok 行,slot = 当前小时        去重键正确
同一计划时刻再触发 → skipped                幂等(不重复轰炸)
========================================  ==========================================

用法::

    .venv\\Scripts\\python.exe scripts\\verify_scheduler_fire.py
    .venv\\Scripts\\python.exe scripts\\verify_scheduler_fire.py --keep
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from verify_acceptance2 import (  # noqa: E402
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

HOTEL = "调度-推送测试酒店"
GROUP = "sched-group-0001"
OPS = "sched-ops-0001"

#: ★ 全部**推送类**任务(会被临时改成"每分钟"逐个验到点触发)。
#: 不在这里的任务都是采集/备份类 —— 它们要连真实平台或动文件,本机不该被本脚本触发。
PUSH_TASKS = (
    "push.daily",  # 日报
    "push.schedule",  # 22 项按节奏
    "alert.room",  # 关房/房态预警
    "alert.data",  # 数据类预警
    "alert.summary",  # 预警汇总 + 送达率
    "review.suggest",  # 点评草稿
    "review.realtime",  # 点评实时应答
    "review.auto",  # 点评自动回复
    "review.analysis",  # 点评分析日报
    "ops.violation",  # 违约实时监控
    "ops.selfcheck",  # 每日自检(推送壳)
    "compare.push",  # 段3 比价推送
)

#: 日报必须真的送到群里的那个(深验文本 + 图片)
MUST_DELIVER = "push.daily"

_results: list[tuple[str, bool, str]] = []


def step(name: str, ok: bool, evidence: str = "") -> None:
    _results.append((name, bool(ok), evidence))
    icon = "\033[92m✓\033[0m" if ok else "\033[91m✗\033[0m"
    print(f"  {icon} {name}" + (f"\n      {evidence}" if evidence else ""))


def patch_crons(names: tuple[str, ...], cron: str) -> dict[str, Any]:
    """★ 临时把这些已注册任务的 cron 换成"每分钟"。

    ``TaskSpec`` 是 frozen dataclass → 用 ``dataclasses.replace`` 造新 spec,
    再换掉注册表 ``specs`` 里的项。这是**刻意伸手进内部**:不这么做就只能等真
    09:00 / 14:00 才能验一次"cron 触发",而这一步恰恰是本脚本要验的东西。
    返回原 spec,便于还原。
    """
    from hoteldata.infra.tasks import get_registry

    reg = get_registry()
    originals: dict[str, Any] = {}
    for name in names:
        originals[name] = reg.specs[name]
        reg.specs[name] = dataclasses.replace(originals[name], cron=cron)
    return originals


async def run(keep: bool = False) -> int:
    from sqlalchemy import delete, select

    from hoteldata.infra.crypto import get_cipher
    from hoteldata.infra.models import AlertLog, Bot, Hotel, JobRun, PushLog
    from hoteldata.runtime import Runtime

    today = date.today()
    server = FakeWeComServer()
    await server.start()
    print(f"\n本地企微服务端已起:{server.url}")

    settings = reload_settings(
        AIBOT_WS_URL=server.url,
        AIBOT_HEARTBEAT_S=5.0,
        MANAGE_CHATIDS=GROUP,
        OPS_CHATID=OPS,
        PUSH_MIN_INTERVAL_S=0.05,
        PUSH_GROUP_MIN_INTERVAL_S=0.05,
    )

    # ★ 装配顺序必须与 ``hoteldata serve`` 的 lifespan **完全一致**:
    #   先 import jobs(注册任务)→ 再 create(with_scheduler=True)
    import hoteldata.jobs  # noqa: F401

    originals = patch_crons(PUSH_TASKS, "* * * * *")

    cm = Runtime.create(settings, with_scheduler=True)
    rt = await cm.__aenter__()
    rt._extras["__cm__"] = cm
    hotel_id: int | None = None
    exit_code = 1
    try:
        # ---------- 夹具 ----------
        cipher = get_cipher(rt.settings)
        async with rt.db.session() as s:
            s.add(
                Bot(
                    name="__sched_bot__",
                    bot_id_enc=cipher.encrypt("sched-bot-id"),
                    secret_enc=cipher.encrypt("sched-secret"),
                )
            )
        manager = await rt.start_bots()
        await rt.start_push()
        rt.push.dispatcher.min_interval_s = 0.05
        rt.push.dispatcher.group_min_interval_s = 0.05
        for _ in range(80):
            if manager.online:
                break
            await asyncio.sleep(0.05)

        hotel = await _ensure_hotel(rt.db, HOTEL)
        hotel_id = int(hotel.id)
        await _cleanup(rt.db, hotel_id)
        async with rt.db.session() as s:
            await s.execute(delete(PushLog).where(PushLog.group_chatid.in_([GROUP, OPS])))
            await s.execute(delete(AlertLog).where(AlertLog.recipient.in_([GROUP, OPS])))

        # 当日数据 + 截图 + 首页待办(让日报有内容、预警有触发源)
        from hoteldata.domains.collect.rotation import get_rotation

        plan = get_rotation().pick(today)
        for item in plan.items:
            await _seed_module(
                rt, hotel_id, item.page or "经营报告", item.name, "昨日", {"验收指标": 1}, day=today
            )
        for item in plan.items[:2]:
            alias = (item.alias or [item.name])[0]
            await _seed_shot(rt, hotel, item.page or "经营报告", alias, day=today)
        await _seed_module(rt, hotel_id, "经营报告", "离店", "昨日", {"离店间夜": 12}, day=today)
        await _seed_portal(rt, hotel_id, "home_pending", {"comment_pending": "3"}, day=today)
        await rt.bindings.bind(GROUP, hotel_id)

        # ---------- ① 装配 ----------
        print("\n【1】装配(与 hoteldata serve 的 lifespan 同序)")
        jobs = rt.scheduler.get_jobs()
        scheduled_ids = sorted(j.id for j in jobs)
        missing = sorted(set(PUSH_TASKS) - set(scheduled_ids))
        next_run = min((j.next_run_time for j in jobs if j.id in PUSH_TASKS), default=None)
        step(
            "先 import jobs 再 create(with_scheduler=True) → 调度器装载全部任务",
            len(jobs) == len(rt.tasks.names()) and len(jobs) > 0,
            f"get_jobs()={len(jobs)} / registry={len(rt.tasks.names())}",
        )
        step(
            f"{len(PUSH_TASKS)} 个推送任务已装载并将于下一分钟触发",
            not missing,
            f"缺失={missing or '无'} / 下次触发={next_run}",
        )
        step(
            "网关在线 + 群已绑定当日数据",
            bool(manager.online)
            and [h.hotel_id for h in await rt.bindings.for_group(GROUP)] == [hotel_id],
            f"health={manager.health()} / 当日轮换 {len(plan.items)} 项 / 造图 2 张 / 已埋预警触发源",
        )

        # ---------- ② 到点之前:不该提前跑 ----------
        before_msgs = len(server.sent_to(GROUP))
        async with rt.db.session() as s:
            from sqlalchemy import func as _func

            max_run_before = int(await s.scalar(_func.max(JobRun.id)) or 0)
        await asyncio.sleep(2.0)
        step(
            "启动后 2 秒内**没有**提前跑(等的是 cron,不是启动)",
            len(server.sent_to(GROUP)) == before_msgs,
            f"群内消息 {before_msgs} → {len(server.sent_to(GROUP))}(应为不变)",
        )

        # ---------- ③ 等真调度器到点把它们叫起来 ----------
        print(f"\n【2】等 APScheduler 到点触发 {len(PUSH_TASKS)} 个推送任务(最长 150 秒)")
        t0 = time.monotonic()
        rows: list[Any] = []
        while time.monotonic() - t0 < 150:
            async with rt.db.session() as s:
                rows = list(
                    (
                        await s.execute(
                            select(JobRun)
                            .where(JobRun.task.in_(PUSH_TASKS), JobRun.id > max_run_before)
                            .order_by(JobRun.id)
                        )
                    )
                    .scalars()
                    .all()
                )
            if len({r.task for r in rows}) >= len(PUSH_TASKS):
                break
            await asyncio.sleep(0.5)
        waited = time.monotonic() - t0
        fired = {r.task for r in rows}
        step(
            f"★ {len(PUSH_TASKS)} 个推送任务**全部被 cron 叫起来了**",
            len(fired) >= len(PUSH_TASKS),
            f"等待 {waited:.1f}s;触发 {len(fired)}/{len(PUSH_TASKS)} 个;"
            f"未触发={sorted(set(PUSH_TASKS) - fired) or '无'}",
        )
        step(
            "触发行都是 trigger=schedule(不是手工/catchup)",
            rows and all(r.trigger == "schedule" for r in rows),
            f"trigger 取值={sorted({r.trigger for r in rows})}",
        )

        # 等全部跑到终态
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            async with rt.db.session() as s:
                rows = list(
                    (
                        await s.execute(
                            select(JobRun)
                            .where(JobRun.task.in_(PUSH_TASKS), JobRun.id > max_run_before)
                            .order_by(JobRun.id)
                        )
                    )
                    .scalars()
                    .all()
                )
            if rows and all(r.status not in ("pending", "running") for r in rows):
                break
            await asyncio.sleep(0.3)

        latest: dict[str, Any] = {}
        for r in rows:
            latest[r.task] = r  # 同任务多次触发时取最后一条
        table = {
            name: (
                f"{latest[name].status}/{round(latest[name].duration_ms or 0)}ms"
                if name in latest
                else "**未触发**"
            )
            for name in PUSH_TASKS
        }
        pending = [n for n, v in table.items() if v == "**未触发**" or v.startswith(("pending", "running"))]
        failed = [n for n, v in table.items() if v.startswith("failed")]
        step(
            "全部跑到终态(没有卡在 pending/running)",
            not pending,
            f"未到终态={pending or '无'} / 逐任务(状态/耗时)={table}",
        )
        step(
            "到点触发**无失败**(failed 为空)",
            not failed,
            f"failed={failed or '无'};" + " / ".join(
                f"{n}:{(latest[n].summary or {})!s}" for n in PUSH_TASKS if n in latest
            )[:400],
        )

        # ---------- ④ 消息真的走出去了 ----------
        print("\n【3】消息真的推出去了吗")
        await rt.push.dispatcher.drain(timeout_s=60)
        got = await server.await_sent(GROUP, count=before_msgs + 1, timeout_s=30)
        for _ in range(80):
            if server.media_to(GROUP):
                break
            await asyncio.sleep(0.25)
        texts = server.texts_to(GROUP)
        media = server.media_to(GROUP)
        joined = "\n".join(texts)
        step(
            "群在 WebSocket 上收到主动推送(aibot_send_msg)",
            len(got) > before_msgs,
            f"群内消息 {before_msgs} → {len(got)} 条(派发器队列已排空)",
        )
        step(
            "日报真的到群:标题行 + 📅 轮换行",
            f"### 「{HOTEL}」" in joined and "今日轮换" in joined,
            next((ln for ln in joined.splitlines() if "今日轮换" in ln), joined[:60]),
        )
        step(
            "日报附图逐张走 image 消息(A1-13:无 news 卡片)",
            len(media) >= 1 and "news" not in str(server.sent_to(GROUP)),
            f"media_id={media[:3]}(共 {len(media)} 张,上传 init "
            f"{len([u for u in server.uploads if u['cmd'] == 'init'])} 次)",
        )

        # ---------- ⑤ 预警审计 ----------
        print("\n【4】预警与审计")
        alc: list[Any] = []
        for _ in range(60):
            async with rt.db.session() as s:
                alc = list(
                    (
                        await s.execute(
                            select(AlertLog).order_by(AlertLog.id.desc()).limit(20)
                        )
                    )
                    .scalars()
                    .all()
                )
            if alc:
                break
            await asyncio.sleep(0.25)
        step(
            "预警落 alert_logs,两个键各司其职",
            bool(alc) and all(a.delivery_key and a.trigger_key for a in alc),
            f"{[(a.recipient, a.pushed) for a in alc[:4]]} / "
            f"delivery_key 以 trigger_key 为前缀="
            f"{all(a.delivery_key.startswith(a.trigger_key) for a in alc)}",
        )

        logs: list[Any] = []
        for _ in range(60):
            async with rt.db.session() as s:
                logs = list(
                    (
                        await s.execute(
                            select(PushLog)
                            .where(PushLog.group_chatid == GROUP)
                            .order_by(PushLog.id.desc())
                        )
                    )
                    .scalars()
                    .all()
                )
            if any(r.status == "ok" for r in logs):
                break
            await asyncio.sleep(0.25)
        slot_now = datetime.now(settings.tzinfo).strftime("%Y-%m-%d-%H")
        ok_rows = [r for r in logs if r.status == "ok"]
        step(
            "push_logs 有 ok 行,slot = 当前小时(去重键)",
            bool(ok_rows) and any(r.slot == slot_now for r in ok_rows),
            f"{[(r.push_type, r.status, r.slot, r.media_count) for r in logs[:4]]} / 期望 slot={slot_now}",
        )
        step(
            "审计里的 bot_id 是文本(D17)",
            bool(ok_rows) and isinstance(ok_rows[0].bot_id, str),
            f"bot_id={ok_rows[0].bot_id!r} type={type(ok_rows[0].bot_id).__name__}" if ok_rows else "无",
        )

        # 幂等:同一计划时刻的第二次触发应被 (task, scheduled_at) 唯一索引挡成 skipped
        row = latest.get(MUST_DELIVER)
        second = await rt.tasks.run(
            MUST_DELIVER, rt, trigger="schedule", scheduled_at=row.scheduled_at if row else None
        )
        step(
            f"{MUST_DELIVER} 同一计划时刻再触发 → skipped(幂等,不重复轰炸)",
            second.status == "skipped",
            f"第二次 run status={second.status}(scheduled_at="
            f"{row.scheduled_at.astimezone() if row and row.scheduled_at else '-'})",
        )
        stats = await rt.audit.day_stats(today)
        step(
            "当日推送审计可汇总",
            stats["total"] >= 1,
            f"ok={stats['ok']} failed={stats['failed']} skipped={stats['skipped']} "
            f"成功率={stats['rate']:.1f}%",
        )
        exit_code = 0 if all(ok for _n, ok, _e in _results) else 1
    finally:
        from hoteldata.infra.tasks import get_registry

        for name, spec in originals.items():  # 还原 cron
            get_registry().specs[name] = spec
        await rt.aclose()
        await server.stop()
        if not keep:
            cm2 = Runtime.create(get_settings(), with_scheduler=False)
            rt2 = await cm2.__aenter__()
            try:
                async with rt2.db.session() as s:
                    await s.execute(delete(Bot).where(Bot.name == "__sched_bot__"))
                    if hotel_id is not None:
                        await s.execute(delete(PushLog).where(PushLog.group_chatid.in_([GROUP, OPS])))
                        await s.execute(
                            delete(AlertLog).where(AlertLog.recipient.in_([GROUP, OPS]))
                        )
                if hotel_id is not None:
                    await _cleanup(rt2.db, hotel_id)
                    async with rt2.db.session() as s:
                        await s.execute(delete(Hotel).where(Hotel.name == HOTEL))
            finally:
                await rt2.aclose()
        else:
            print("\n(--keep:合成数据保留,便于自己翻库)")
    return exit_code


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description="推送任务到点触发可执行性检查(真 APScheduler)")
    parser.add_argument("--keep", action="store_true", help="保留合成数据便于翻库")
    args = parser.parse_args(argv)

    started = time.monotonic()
    code = asyncio.run(run(keep=args.keep))
    passed = sum(1 for _n, ok, _e in _results if ok)
    total = len(_results)
    print("\n" + "=" * 78)
    print(f"推送任务到点触发:{passed} / {total} 步通过,耗时 {time.monotonic() - started:.1f}s")
    failed = [n for n, ok, _e in _results if not ok]
    if failed:
        print(f"未通过:{failed}")
    print("=" * 78)
    return code if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
