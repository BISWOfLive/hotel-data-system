"""★ 任务注册表 —— **时刻表唯一来源**(总纲 7.5 / 段1 §5.9)。

🚫 **时刻表不进 ``.env``**。这里的 ``@task(cron=...)`` 就是唯一事实源;
``.env`` 只保留 ``*_ENABLED``。

段1 注册的任务
--------------
=====================  ==============  =========  =========  ==========================
任务名                  cron            catch_up   max_delay  说明
=====================  ==============  =========  =========  ==========================
``collect.modules``    ``40 0 * * *``  ✅         6h         当日轮换模块提取
``collect.screenshots`` ``30 5 * * *`` ✅         6h         模块截图(**错峰**,与取数分离)
``collect.portal``     ``0 9 * * *``   ✅         6h         预警三源(轮换日;批次 D)
``collect.room``       ``0 1 * * *``   ✅         6h         房态每日固定(批次 D)
``collect.review``     ``45 8 * * *``  ✅         6h         点评提取(批次 D)
``ops.patrol``         ``30 2 * * *``  ✅         4h         登录巡检(**串行**)
``ops.backup``         ``30 3 * * *``  ❌         —          冷备(补跑无意义)
``ops.cleanup``        ``0 4 * * *``   ❌         —          清理
``ops.selfcheck``      ``0 6 * * *``   ❌         —          自检指标 + **推运维群**(段2 T2G.1)
=====================  ==============  =========  =========  ==========================

★ **段2 注册的任务(§5.9)**
--------------------------

=====================  ===================  =========  =========  ==============================
任务名                  cron                 catch_up   max_delay  说明
=====================  ===================  =========  =========  ==============================
``push.daily``         ``0 9 * * *``        ✅         6h         日报(标题行 + ≤5 张轮换图)
``push.schedule``      ``0 9 * * *``        ✅         6h         22 项报告节奏
``alert.room``         ``4 9,14,19 * * *``  ✅         4h         关房预警(名义 09:00/14:30/19:00)
``alert.data``         ``10 9 * * *``       ✅         6h         数据预警(名义 09:00)
``alert.summary``      ``30 9 * * *``       ❌         —          每日汇总(**过时无意义**)
``review.suggest``     ``50 8 * * *``       ✅         4h         点评建议草稿
``review.analysis``    ``0 9 * * *``        ✅         6h         点评分析日报
``review.auto``        ``40 9 * * *``       ✅         4h         自动回复(仅好评;门控)
``review.realtime``    ``0 8-23 * * *``     ❌         —          实时回复轮询
``ops.violation``      ``0 * * * *``        ❌         —          违约实时监听(变化才推)
=====================  ===================  =========  =========  ==============================

**``catch_up`` 的选择原则**(§5.9 逐字):内容型推送(``push.*`` / ``alert.*`` /
``review.suggest|analysis|auto``)**可补跑**(晚推比不推好);
**汇总与轮询类不补**(``alert.summary`` / ``review.realtime`` / ``ops.violation``,过时无意义);
``ops.backup`` / ``ops.cleanup`` 同样不补。

> ★ **两层时刻结构在 cron 里就体现出来了**:``alert.room`` 的 cron 是
> **09:04 / 14:34 / 19:04**(运维层错峰 +4 分钟),而规则里声明的是
> **09:00 / 14:30 / 19:00**(业务语义层名义时刻)。
> 任务把**实际时刻**作为 slot 传给引擎,引擎再用 ``ROOM_SLOT_BY_TIME``
> **映回名义时刻**去匹配 ``check_times`` —— 合并两层,slot 匹配立刻失效(V49)。
>
> ★ **``review.realtime`` 的 cron 说明**:§5.9 写"8–23 点每 59 分钟",
> 但 cron 的 ``*/59`` 会展开成 ``[0, 59]`` 两分钟 —— 上一小时后一分钟与下一小时
> 第一分钟只隔 **1 分钟**,比"每 59 分钟"更糟。这里取**等价的整点小时轮询**
> ``0 8-23 * * *``(间隔 60 分钟),并在 README 里注明这处与文档的字面差异。

> 为什么 ``ops.backup`` / ``ops.cleanup`` **不补跑**:它们没有窗口语义,
> 18:00 补跑一次 04:00 的清理没有任何意义,反而增加"补跑把状态搞乱"的风险。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import select

from hoteldata.infra.models import Account, Hotel
from hoteldata.infra.tasks import task

__all__ = ["active_pairs", "register_all"]


async def active_pairs(runtime: Any) -> list[tuple[Hotel, Account]]:
    """取全部"活跃酒店 × 其绑定账号"。

    没有绑定账号的酒店**跳过并记日志** —— 静默跳过正是旧系统最擅长的失败方式。
    """
    async with runtime.db.session() as s:
        hotels = list(
            (await s.execute(select(Hotel).where(Hotel.status == "active").order_by(Hotel.id)))
            .scalars()
            .all()
        )
        accounts = {a.id: a for a in (await s.execute(select(Account))).scalars().all()}
    out: list[tuple[Hotel, Account]] = []
    for hotel in hotels:
        acc = accounts.get(hotel.account_id) if hotel.account_id else None
        if acc is None:
            logger.warning("酒店 {} 未绑定账号,跳过", hotel.name)
            continue
        if acc.status == "blocked":
            logger.warning("酒店 {} 的账号 {} 已受限,跳过", hotel.name, acc.alias)
            continue
        out.append((hotel, acc))
    return out


def _module_lookup(repo: Any, hotel_id: int) -> Any:
    """给 :class:PortalExtractor 注入「取模块最新一条」的回调。

    `audit_pending` / `violation_pending` 两列由**模块记录派生**(不调接口),
    而 :class:ExtractContext 里没有 DB 会话 —— 只能注入。用工厂函数而不是
    lambda,是为了让闭包明确绑定当次迭代的 `repo`(ruff B023)。
    """

    async def lookup(module: str, window: str | None = None) -> Any:
        return await repo.latest_module(hotel_id, module, window)

    return lookup


# ---------------------------------------------------------------------------
# 采集域
# ---------------------------------------------------------------------------


@task(
    "collect.modules",
    cron="40 0 * * *",
    catch_up=True,
    max_delay="6h",
    domain="collect",
    description="当日轮换模块提取(API 直连优先,失败降级浏览器)",
)
async def collect_modules(runtime: Any, *, hotel_id: int | None = None, **_: Any) -> dict[str, Any]:
    """当日轮换模块提取。"""
    from hoteldata.domains.collect.channels import aggregate_results
    from hoteldata.domains.collect.datacenter import (
        CollectorStats,
        DatacenterExtractor,
        resolve_targets,
    )
    from hoteldata.domains.collect.repository import CollectRepository

    pairs = await active_pairs(runtime)
    if hotel_id is not None:
        pairs = [p for p in pairs if p[0].id == hotel_id]
    if not pairs:
        return {"hotels": 0, "note": "没有可采集的酒店(检查 core_hotels.account_id)"}

    extractor = DatacenterExtractor(
        runtime.rules,
        pool=runtime.browser,
        ensure_login=runtime.ensure_login,
        allow_browser=runtime.browser is not None,
    )
    stats = CollectorStats()
    per_hotel: dict[str, Any] = {}
    day = date.today()

    # 默认只采当日轮换(+ fixed_daily);COLLECT_ALL_WINDOWS=1 时采每模块声明的全部窗口
    all_windows = runtime.settings.collect.all_windows
    targets = None if all_windows else resolve_targets(runtime.rules, rotation=True, day=day)

    for hotel, account in pairs:
        ctx = runtime.extract_context(hotel, account, day)
        async with runtime.db.session() as s:
            repo = CollectRepository(s)
            results = await extractor.extract(
                ctx,
                targets=targets,
                rotation=targets is None,
                all_windows=all_windows,
                day=day,
                repo=repo,
                stats=stats,
            )
        summary = aggregate_results(results)
        per_hotel[hotel.name] = summary
        logger.info(
            "酒店 {} 采集完成: {} 项,状态 {}",
            hotel.name,
            summary["total"],
            summary["status"],
        )

    out = {
        "hotels": len(pairs),
        **stats.as_dict(),
        "per_hotel": per_hotel,
        "rotation_offset": runtime.rotation.pick(day).offset,
    }
    logger.info(
        "collect.modules 汇总: {}",
        {k: out[k] for k in ("total", "ok", "degraded", "no_data", "failed")},
    )
    return out


@task(
    "collect.screenshots",
    cron="30 5 * * *",
    catch_up=True,
    max_delay="6h",
    domain="collect",
    description="模块截图(独立任务,与取数错峰)",
    enabled_flag="screenshot_enabled",
)
async def collect_screenshots(runtime: Any, *, hotel_id: int | None = None, **_: Any) -> dict[str, Any]:
    """模块截图并回填 ``collect_reports``。"""
    if not runtime.settings.screenshot.enabled:
        return {"skipped": "SCREENSHOT_ENABLED=0"}
    from hoteldata.domains.collect.repository import CollectRepository
    from hoteldata.domains.collect.screenshot import Screenshoter, screenshot_demand_modules

    pairs = await active_pairs(runtime)
    if hotel_id is not None:
        pairs = [p for p in pairs if p[0].id == hotel_id]
    if not pairs:
        return {"hotels": 0}

    if runtime.browser is None:
        await runtime.start_browser()

    day = date.today()
    # ★ 只截「当日轮换里属于模块采集的项」(fullpage / 预警* 走别的通道)
    targets = screenshot_demand_modules(day, rules=runtime.rules)
    shooter = Screenshoter(settings=runtime.settings, rules=runtime.rules, pool=runtime.browser)
    out: dict[str, Any] = {"hotels": len(pairs), "targets": len(targets), "per_hotel": {}}
    for hotel, account in pairs:
        ctx = runtime.extract_context(hotel, account, day)
        async with runtime.db.session() as s:
            repo = CollectRepository(s)
            results = await shooter.run(ctx, targets, repo=repo)
        out["per_hotel"][hotel.name] = {
            "shots": sum(r.record_count for r in results),
            "failed": sum(1 for r in results if r.status == "failed"),
        }
    logger.info("collect.screenshots 汇总: {}", out["per_hotel"])
    return out


@task(
    "collect.portal",
    cron="0 9 * * *",
    catch_up=True,
    max_delay="6h",
    domain="collect",
    description="预警三源提取(渠道/首页待办/热点日历;轮换日)",
)
async def collect_portal(runtime: Any, *, hotel_id: int | None = None, **_: Any) -> dict[str, Any]:
    """预警三源提取。**只在轮换清单含「预警*」项的日子跑**(旧 ``scheduler.py:72`` 前缀特判)。"""
    from hoteldata.domains.collect.portal import PortalExtractor
    from hoteldata.domains.collect.repository import CollectRepository

    day = date.today()
    plan = runtime.rotation.pick(day)
    if not plan.alert_names:
        logger.info("今日({})轮换清单无「预警*」项,跳过预警三源采集", day)
        return {"skipped": "今日轮换无预警项", "offset": plan.offset}

    pairs = await active_pairs(runtime)
    if hotel_id is not None:
        pairs = [p for p in pairs if p[0].id == hotel_id]
    extractor = PortalExtractor()  # ★ 无参构造(该类无 __init__,规则由内部按需读取)
    out: dict[str, Any] = {"hotels": len(pairs), "per_hotel": {}}
    for hotel, account in pairs:
        ctx = runtime.extract_context(hotel, account, day)
        # ★ 提取器不持有 DB 会话(硬约束):`audit_pending` / `violation_pending`
        #   两列是**模块记录派生**的,必须注入回调;回调在会话存活期内被 await,
        #   所以整个 extract 都要在这个 with 块里。
        async with runtime.db.session() as s:
            repo = CollectRepository(s)
            results = await extractor.extract(ctx, latest_module=_module_lookup(repo, ctx.hotel_id))
            for res in results:
                if res.records:
                    await repo.upsert_portal_columns(res.records)
        out["per_hotel"][hotel.name] = {
            "sources": len(results),
            "columns": sum(len(r.records or []) for r in results),
            "failed": sum(1 for r in results if r.status == "failed"),
        }
    return out


@task(
    "collect.room",
    cron="0 1 * * *",
    catch_up=True,
    max_delay="6h",
    domain="collect",
    description="房态提取(房型 × 日期网格;整批替换)",
)
async def collect_room(runtime: Any, *, hotel_id: int | None = None, **_: Any) -> dict[str, Any]:
    """房态提取。★ ``available=1`` 当且仅当 ``roomStatus=='G'``。"""
    from hoteldata.domains.collect.repository import CollectRepository
    from hoteldata.domains.collect.room import RoomExtractor

    pairs = await active_pairs(runtime)
    if hotel_id is not None:
        pairs = [p for p in pairs if p[0].id == hotel_id]
    day = date.today()
    extractor = RoomExtractor()  # ★ 无参构造
    out: dict[str, Any] = {"hotels": len(pairs), "per_hotel": {}}
    for hotel, account in pairs:
        ctx = runtime.extract_context(hotel, account, day)
        results = await extractor.extract(ctx)
        rows = [r for res in results for r in (res.records or [])]
        # ★ 只有 records 非空才写库:replace_room_states 是同事务 DELETE+INSERT,
        #   传空 rows 会把当天数据**清空**(降级路径必须不碰库)。
        ok = bool(rows)
        if ok:
            async with runtime.db.session() as s:
                await CollectRepository(s).replace_room_states(ctx.hotel_id, day, rows)
        out["per_hotel"][hotel.name] = {
            "rows": len(rows),
            "written": ok,
            "failed": sum(1 for r in results if r.status == "failed"),
        }
    return out


@task(
    "collect.review",
    cron="45 8 * * *",
    catch_up=True,
    max_delay="6h",
    domain="collect",
    description="点评提取(待回复列表 + 4 类素材)",
)
async def collect_review(runtime: Any, *, hotel_id: int | None = None, **_: Any) -> dict[str, Any]:
    """点评提取。★ UPSERT **不回溯**(绝不重置 ``replied`` / ``strategy``)。"""
    from hoteldata.domains.collect.repository import CollectRepository
    from hoteldata.domains.collect.review import ReviewExtractor

    pairs = await active_pairs(runtime)
    if hotel_id is not None:
        pairs = [p for p in pairs if p[0].id == hotel_id]
    day = date.today()
    extractor = ReviewExtractor()  # ★ 无参构造
    out: dict[str, Any] = {"hotels": len(pairs), "per_hotel": {}}
    for hotel, account in pairs:
        ctx = runtime.extract_context(hotel, account, day)
        results = await extractor.extract(ctx)
        async with runtime.db.session() as s:
            repo = CollectRepository(s)
            for res in results:
                if not res.records:
                    continue
                # ★ 区分口径:素材结果的 detail 带 "kind"(score/competitor/trend/num),
                #   待回复列表那条不带。materials 即使 status=no_data 也**仍要落库**。
                if (res.detail or {}).get("kind"):
                    await repo.upsert_review_materials(res.records)
                else:
                    await repo.upsert_reviews(res.records)
        out["per_hotel"][hotel.name] = {
            "sources": len(results),
            "reviews": sum(len(r.records or []) for r in results if (r.detail or {}).get("kind") == "review"),
            "failed": sum(1 for r in results if r.status == "failed"),
        }
    return out


# ---------------------------------------------------------------------------
# 运维域
# ---------------------------------------------------------------------------


@task(
    "ops.patrol",
    cron="30 2 * * *",
    catch_up=True,
    max_delay="4h",
    domain="ops",
    description="登录巡检(串行:登录动作不能并发)",
)
async def ops_patrol(runtime: Any, **_: Any) -> dict[str, Any]:
    """登录巡检。"""
    from hoteldata.domains.ops.patrol import patrol_once

    report = await patrol_once(runtime)
    return report.as_dict()


@task(
    "ops.backup",
    cron="30 3 * * *",
    catch_up=False,
    domain="ops",
    description="PG 冷备(补跑无意义)",
)
async def ops_backup(runtime: Any, **_: Any) -> dict[str, Any]:
    """PG 冷备。"""
    from hoteldata.domains.ops.backup import run as run_backup

    report = await run_backup(runtime.settings)
    return report.as_dict()


@task(
    "ops.cleanup",
    cron="0 4 * * *",
    catch_up=False,
    domain="ops",
    description="磁盘清理(双保留期 + 禁止目录白名单)",
)
async def ops_cleanup(runtime: Any, *, dry_run: bool = False, all_: bool = False, **_: Any) -> dict[str, Any]:
    """磁盘清理。**任务里永远走真实删除;``--dry-run`` 只在 CLI 手动用。**"""
    from hoteldata.domains.ops.cleanup import run as run_cleanup

    report = await run_cleanup(runtime.settings, dry_run=dry_run, all_=all_)
    return report.as_dict()


@task(
    "ops.selfcheck",
    cron="0 6 * * *",
    catch_up=False,
    domain="ops",
    description="每日自检指标 + 推运维群(段2 T2G.1;汇总类不补跑)",
)
async def ops_selfcheck(runtime: Any, **_: Any) -> dict[str, Any]:
    """自检 + 推送。

    段1 只出指标;段2 把指标**组织成文案推运维群**(``OPS_CHATID``)。

    ★ **刻意不加 ``enabled_flag``**:``infra/tasks.py::build_scheduler`` 遇到
    ``enabled_flag`` 为假会把任务**整个跳过注册** —— 于是 ``OPS_SELFCHECK_PUSH=0``
    会连带"自检指标也算不出来、``job_runs`` 里也没有记录",而这两件事本不该绑在一起
    (指标是可观测性的底线,推送才是可选项)。

    所以推送开关**只在 ``selfcheck_push.push_report()`` 内部生效**
    (``settings.ops_push.selfcheck_push_enabled``):关掉时返回
    ``{"pushed": 0, "error": "OPS_SELFCHECK_PUSH=0(推送关闭)"}`` 并记日志,
    **指标照算、照落 ``job_runs``**。
    """
    from hoteldata.domains.ops.selfcheck import run as run_selfcheck

    report = await run_selfcheck(runtime.settings, db=runtime.db)
    out = report.as_dict()
    try:
        from hoteldata.domains.ops.selfcheck_push import push_report

        pushed = await push_report(runtime, report)
        out["push"] = pushed
    except Exception as exc:  # noqa: BLE001 - 推送失败不影响指标(指标本身仍要落 job_runs)
        logger.error("自检推送失败: {}", exc)
        out["push"] = {"pushed": 0, "error": f"{type(exc).__name__}: {exc}"}
    return out


def register_all() -> list[str]:
    """触发导入以完成注册(供 ``task ls`` 与 ``serve`` 调用)。"""
    from hoteldata.infra.tasks import get_registry

    return get_registry().names()


# ---------------------------------------------------------------------------
# ★ 段2 · 两层时刻结构里的「slot 解析」
# ---------------------------------------------------------------------------


def nominal_slot_for_now(
    runtime: Any,
    nominals: tuple[str, ...] | list[str],
    *,
    offset_min: int = 4,
    now: datetime | None = None,
) -> str:
    """★ 把「现在」映回**名义时刻**(业务语义层)。

    ``alert.room`` 的 cron 是 ``4 9,14,19 * * *``(运维层错峰 +4 分钟),
    而规则里声明的是 ``09:00 / 14:30 / 19:00``(业务语义层)。
    任务把**实际时刻**作为 slot 传给引擎,引擎再用 ``ROOM_SLOT_BY_TIME`` 映回名义时刻。

    本函数做的是**同一件事的调度侧一半**:挑出"当前这一次触发对应哪个名义时刻":

      * 首选:``名义时刻 + offset_min`` 的 HH:MM 与当前 HH:MM **完全相等**
        (正常定时触发的情形,最常见);
      * 兜底:取**当日已经过点**的最近一个名义时刻 —— 覆盖"补跑"(catch-up)场景:
        09:04 那次漏了,12:30 补跑时仍应算作 09:00 那一次。

    找不到(例如凌晨 03:00 手动跑) → 返回**第一个名义时刻**,由引擎的
    ``check_times`` 匹配去决定要不要真跑(不匹配就跳过,不误报)。
    """
    tz = runtime.settings.tzinfo
    current = now or datetime.now(tz)
    if current.tzinfo is None:  # pragma: no cover - 防御:naive datetime 一律按进程时区解释
        current = current.replace(tzinfo=tz)
    hhmm = current.strftime("%H:%M")

    parsed: list[time] = []
    for raw in nominals:
        hh, _, mm = str(raw).partition(":")
        try:
            parsed.append(time(int(hh), int(mm)))
        except ValueError:  # pragma: no cover - settings 校验已挡住
            continue
    if not parsed:
        return hhmm

    # ① 精确命中「名义 + 错峰」
    for t in parsed:
        shifted = (
            datetime.combine(current.date(), t, tzinfo=tz) + timedelta(minutes=offset_min)
        ).strftime("%H:%M")
        if shifted == hhmm:
            return t.strftime("%H:%M")

    # ② 兜底:当日已过点的最近一个名义时刻(补跑)
    passed = [t for t in parsed if t <= current.time()]
    if passed:
        return max(passed).strftime("%H:%M")
    return min(parsed).strftime("%H:%M")


# ---------------------------------------------------------------------------
# ★ 段2 · 推送(批次 C / D)
# ---------------------------------------------------------------------------


@task(
    "push.daily",
    cron="0 9 * * *",
    catch_up=True,
    max_delay="6h",
    domain="push",
    description="日报推送(标题行 + ≤5 张轮换图;一群多店合并为 1 条)",
)
async def push_daily(runtime: Any, *, force: bool = False, **_: Any) -> dict[str, Any]:
    """09:00 日报推送。**组装在 ``domains/report/daily.py``,投递在 ``push/``。**"""
    return await runtime.report().publish_daily(force=force)


@task(
    "push.schedule",
    cron="0 9 * * *",
    catch_up=True,
    max_delay="6h",
    domain="push",
    description="22 项报告节奏推送(四项桶 + 条件 DSL + 渲染契约)",
)
async def push_schedule(runtime: Any, *, force: bool = False, **_: Any) -> dict[str, Any]:
    """22 项报告按节奏推送。"""
    return await runtime.report().publish_schedule(force=force)


# ---------------------------------------------------------------------------
# ★ 段2 · 预警(批次 E)
# ---------------------------------------------------------------------------


@task(
    "alert.room",
    cron="4 9,14,19 * * *",
    catch_up=True,
    max_delay="4h",
    domain="alert",
    description="关房预警(名义 09:00/14:30/19:00;调度层 +4 分钟错峰)",
    enabled_flag="alert_enabled",
)
async def alert_room(runtime: Any, **_: Any) -> dict[str, Any]:
    """关房预警三档巡检。★ slot 是**实际时刻**,引擎负责映回名义时刻(V49)。"""
    slot = nominal_slot_for_now(runtime, runtime.settings.alert.room_slot_names)
    return await runtime.alert().check(slot)


@task(
    "alert.data",
    cron="10 9 * * *",
    catch_up=True,
    max_delay="6h",
    domain="alert",
    description="数据预警(渠道/首页待办/热点日历/城市热点;名义 09:00)",
    enabled_flag="alert_enabled",
)
async def alert_data(runtime: Any, **_: Any) -> dict[str, Any]:
    """数据类预警(名义 09:00)。"""
    slot = nominal_slot_for_now(runtime, runtime.settings.alert.data_slot_names, offset_min=10)
    return await runtime.alert().check(slot)


@task(
    "alert.summary",
    cron="30 9 * * *",
    catch_up=False,
    domain="alert",
    description="预警每日汇总 + 送达率(推管理群;汇总类不补跑)",
    enabled_flag="alert_enabled",
)
async def alert_summary(runtime: Any, **_: Any) -> dict[str, Any]:
    """09:30 预警汇总。**送达率 = 成功行 ÷ 总行**(无管理群也计入分母)。"""
    return await runtime.alert().summary()


# ---------------------------------------------------------------------------
# ★ 段2 · 点评(批次 F)
# ---------------------------------------------------------------------------


@task(
    "review.suggest",
    cron="50 8 * * *",
    catch_up=True,
    max_delay="4h",
    domain="review",
    description="点评建议草稿(逐条落审计 → 草稿推管理群 ≤20 条)",
    enabled_flag="review_suggest_enabled",
)
async def review_suggest(runtime: Any, **_: Any) -> dict[str, Any]:
    """08:50 建议草稿。"""
    return await runtime.review().suggest()


@task(
    "review.analysis",
    cron="0 9 * * *",
    catch_up=True,
    max_delay="6h",
    domain="review",
    description="点评分析日报(评分/竞争圈/趋势/待回复快照;纯数据模板)",
    enabled_flag="review_analysis_enabled",
)
async def review_analysis(runtime: Any, **_: Any) -> dict[str, Any]:
    """09:00 点评分析日报。"""
    return await runtime.review().analysis()


@task(
    "review.auto",
    cron="40 9 * * *",
    catch_up=True,
    max_delay="4h",
    domain="review",
    description="点评自动回复(仅好评;门控:auto.enabled + 灰度 + submit.ready)",
    enabled_flag="review_auto_enabled",
)
async def review_auto(runtime: Any, **_: Any) -> dict[str, Any]:
    """09:40 自动回复。**未就绪 → 明确提示 + 进人工队列,绝不伪造成功**(V57)。"""
    return await runtime.review().auto_reply()


@task(
    "review.realtime",
    cron="0 8-23 * * *",
    catch_up=False,
    domain="review",
    description="点评实时回复轮询(8–23 点整点;过时无意义,不补跑)",
    enabled_flag="review_realtime_enabled",
)
async def review_realtime(runtime: Any, **_: Any) -> dict[str, Any]:
    """点评实时轮询。"""
    return await runtime.review().realtime()


# ---------------------------------------------------------------------------
# ★ 段2 · 违约实时(批次 G)
# ---------------------------------------------------------------------------


@task(
    "ops.violation",
    cron="0 * * * *",
    catch_up=False,
    domain="ops",
    description="违约实时监听(count 增加才推;首次只记基线)",
    enabled_flag="violation_realtime_enabled",
)
async def ops_violation(runtime: Any, **_: Any) -> dict[str, Any]:
    """违约实时监听。"""
    from hoteldata.domains.ops.violation import run as run_violation

    return await run_violation(runtime)


# ---------------------------------------------------------------------------
# ★ 段3 · 比价(计划书 §5.10;**时刻表只在这里**)
# ---------------------------------------------------------------------------
#
# ====================  ========================  =========  =========  ==================================
# 任务名                 cron                      catch_up   max_delay  说明
# ====================  ========================  =========  =========  ==================================
# ``compare.batch``     ``0 1 * * *``             ✅         6h         清单批量比价(旧 01:00 hotel_batch)
# ``compare.collect``   ``30 8,13,17 * * *``      ✅         6h         定时采集存档(3 次/天)
# ``compare.push``      ``0 14,18 * * *``         ✅         6h         独立推送(纯文字)
# ====================  ========================  =========  =========  ==================================
#
# ★ **09:00 的比价不单独推** —— 它通过日报钩子合并进日报(计划书 §5.10 末句)。
#   采集点是 08:30,日报是 09:00,按 P8 的 slot 语义(取当日最近一个已完成的 slot),
#   08:30 那次正好被 09:00 的日报读到 —— 这就是"09:00 日报里有价格段"的完整链路。
#
# ★ **三个时刻全部写在注册表里,一个都不进 ``.env``** ——
#   计划书附录 A 列的 ``PRICE_COLLECT_TIMES`` / ``PRICE_PUSH_TIMES`` /
#   ``HOTEL_BATCH_TIME`` **不迁移**,直接由本注册表承担(总纲 §7.5 的唯一来源)。
#
# ★ ``catch_up``:三个都是**内容型**(晚采比不采好、晚推比不推好),所以都补;
#   ``max_delay=6h`` 与段2 的 ``push.daily`` 一致 —— 再晚窗口就错位了。


@task(
    "compare.batch",
    cron="0 1 * * *",
    catch_up=True,
    max_delay="6h",
    domain="compare",
    description="清单批量比价(cmp_price_targets mode=batch;单店失败不阻断)",
    enabled_flag="compare_enabled",
)
async def compare_batch(runtime: Any, *, force: bool = False, **_: Any) -> dict[str, Any]:
    """01:00 批量比价。判重按 **B27「今天有没有真数据」**,``force`` 可绕。"""
    return await runtime.compare().run_batch(force=force)


@task(
    "compare.collect",
    cron="30 8,13,17 * * *",
    catch_up=True,
    max_delay="6h",
    domain="compare",
    description="定时采集存档(08:30/13:30/17:30;09:00 那次供日报合并)",
    enabled_flag="compare_enabled",
)
async def compare_collect(runtime: Any, **_: Any) -> dict[str, Any]:
    """三次定时采集(只存档 + 出报告,**不推送**)。"""
    return await runtime.compare().run_collect()


@task(
    "compare.push",
    cron="0 14,18 * * *",
    catch_up=True,
    max_delay="6h",
    domain="compare",
    description="比价独立推送(14:00/18:00 **纯文字**;push_type=price_compare)",
    enabled_flag="compare_enabled",
)
async def compare_push(runtime: Any, *, force: bool = False, **_: Any) -> dict[str, Any]:
    """14:00 / 18:00 独立推送。"""
    return await runtime.compare().push_price(force=force)
