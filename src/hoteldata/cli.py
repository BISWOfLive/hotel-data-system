"""统一 CLI(typer)—— 替代旧系统 ``run.py``(13 子命令)+ ``hotel.py``(4)+ ``manage.py``。

> 旧系统有**三套入口、职责重叠**,三者都能操作账号/酒店/机器人(结构性问题 #1/#10)。
> 段1 收敛成一条命令。

**退出码约定**(V8):任一模块 ``failed`` → 退出码 **1**;``no_data`` **不算失败**。

命令清单(段1 附录 A)
--------------------
``serve`` · ``db ping|upgrade|downgrade`` · ``rules check`` · ``login`` · ``sessions`` ·
``collect [--rotation|--screenshot|portal|room|review]`` · ``rotation`` ·
``task ls|run|log`` · ``ops patrol|backup|cleanup|selfcheck``
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import date, datetime
from typing import Any

import typer
from sqlalchemy import select

from hoteldata import __version__
from hoteldata.logging import configure_stdio, interpreter_banner, setup_logging
from hoteldata.runtime import Runtime
from hoteldata.settings import get_settings

__all__ = ["app"]

app = typer.Typer(
    name="hoteldata",
    help="酒店经营数据自动化系统 · 段1 提取功能",
    no_args_is_help=True,
    add_completion=False,
)
db_app = typer.Typer(help="数据库:ping / upgrade / downgrade", no_args_is_help=True)
rules_app = typer.Typer(help="规则:check", no_args_is_help=True)
task_app = typer.Typer(help="任务:ls / run / log", no_args_is_help=True)
ops_app = typer.Typer(help="运维:patrol / backup / cleanup / selfcheck", no_args_is_help=True)
collect_app = typer.Typer(
    help="提取:默认全量;加子命令跑批次 D / 截图",
    no_args_is_help=False,
    invoke_without_command=True,
)
# ---- 段2 ----
bots_app = typer.Typer(help="机器人:list / add / delete / health", no_args_is_help=True)
push_app = typer.Typer(help="推送:now / log", no_args_is_help=True)
bind_app = typer.Typer(help="群绑定:list / add / remove", no_args_is_help=True)
alert_app = typer.Typer(help="预警:test / status / summary", no_args_is_help=True)
review_app = typer.Typer(help="点评:draft / auto / analysis / status", no_args_is_help=True)
report_app = typer.Typer(help="报告:run / ls", no_args_is_help=True)
#: 段3:比价(T3G.1)。``compare`` / ``compare-batch`` 直接挂在根 app 上(§6 T3G.1 的命令名)。
price_app = typer.Typer(help="比价辅助:login / probe / history / push", no_args_is_help=True)
targets_app = typer.Typer(help="比价目标:add / list / set / remove / import", no_args_is_help=True)
app.add_typer(db_app, name="db")
app.add_typer(rules_app, name="rules")
app.add_typer(task_app, name="task")
app.add_typer(ops_app, name="ops")
app.add_typer(collect_app, name="collect")
app.add_typer(bots_app, name="bots")
app.add_typer(push_app, name="push")
app.add_typer(bind_app, name="bind")
app.add_typer(alert_app, name="alert")
app.add_typer(review_app, name="review")
app.add_typer(report_app, name="report")
app.add_typer(price_app, name="price")
app.add_typer(targets_app, name="targets")



# ---------------------------------------------------------------------------
# 公共工具
# ---------------------------------------------------------------------------


def _configure_stdio() -> None:
    """薄包装:实现见 :func:`hoteldata.logging.configure_stdio`。"""
    configure_stdio()


def _run(coro: Any) -> Any:
    """跑一个协程(CLI 是同步入口)。"""
    return asyncio.run(coro)


def _echo(msg: str = "") -> None:
    typer.echo(msg)


def _ok(msg: str) -> None:
    typer.secho(f"✓ {msg}", fg=typer.colors.GREEN)


def _warn(msg: str) -> None:
    typer.secho(f"! {msg}", fg=typer.colors.YELLOW)


def _err(msg: str) -> None:
    typer.secho(f"✗ {msg}", fg=typer.colors.RED, err=True)


def _json(data: Any) -> None:
    typer.echo(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _status_color(status: str) -> str:
    return {
        "ok": typer.colors.GREEN,
        "degraded": typer.colors.YELLOW,
        "no_data": typer.colors.CYAN,
        "failed": typer.colors.RED,
    }.get(status, typer.colors.WHITE)


def _progress_line(result: Any) -> None:
    """★ 每完成一个模块打一行(旧系统踩过"卡死感",T3.10)。"""
    tgt = result.target
    label = f"{tgt.page} · {tgt.module} · {tgt.window}" if tgt else "?"
    typer.secho(
        f"  {label:52s} {result.status:9s} {result.channel:9s} {result.record_count:4d} 指标",
        fg=_status_color(result.status),
    )


async def _ensure_gateway(rt: Runtime) -> Any:
    """★ **凡是要"真的发送"的 CLI 命令,都必须先拉起机器人网关。**

    ★★ 这里修的是一个真实缺陷(施工后实测发现):

    ``Runtime.bots`` 是**懒构建**的。``await rt.start_push()`` 只会构造
    ``PushService`` 并把 ``manager=self.bots`` 传进去 —— 那一刻
    ``_bots`` 是**空的** ``BotManager([])``,而**只有** ``start_bots()``
    才会去 ``core_bots`` 读凭据、``add()`` 进那个实例。

    所以"只 start_push 不 start_bots"的命令会在 ``Sender.deliver`` 里被
    ``manager.size() == 0`` 挡下,回一句
    **"没有可用的机器人(core_bots 表为空)"** —— 而表里其实有机器人。
    一句谎报原因的报错,比直接崩还难查。

    实测(插一个假凭据 bot 行):

    ==================================  ==============  ==========================
    装配顺序                              bots.size()     投递结果
    ==================================  ==============  ==========================
    只 ``start_push()``                    **0**          ``(False, 没有可用的机器人…)``
    ``start_bots()`` + ``start_push()``    **1**          真正去连服务器
    ==================================  ==============  ==========================

    ``core_bots`` 真的为空时**不报错、只警告** —— 让命令继续跑完,由
    ``push_logs`` 里那行 ``failed`` 审计如实记录(段2 的纪律:不静默,也不假装成功)。
    """
    manager = await rt.start_bots()
    if manager.size() == 0:
        _warn(
            "core_bots 表为空 → 本次发送会失败,并写入一行 failed 审计"
            "(这是有意的:不静默)。先 `hoteldata bots add` 添加机器人。"
        )
    await rt.start_push()
    return manager


def _print_version(ctx: typer.Context, _param: typer.CallbackParam, value: bool) -> None:
    if value and not ctx.resilient_parsing:
        typer.echo(f"hoteldata {__version__}")
        typer.echo(interpreter_banner())
        raise typer.Exit()


@app.callback()
def _main(
    version: bool = typer.Option(
        False, "--version", "-V", callback=_print_version, is_eager=True, help="显示版本与环境自检"
    ),
) -> None:
    """酒店经营数据自动化系统 · 段1「提取功能」。"""
    _configure_stdio()
    setup_logging()


# ===========================================================================
# serve
# ===========================================================================


@app.command("serve")
def serve(
    host: str | None = typer.Option(None, help="监听地址(默认取 .env 的 HOST)"),
    port: int | None = typer.Option(None, help="端口(默认取 .env 的 PORT)"),
    reload: bool = typer.Option(False, "--reload", help="开发热重载"),
) -> None:
    """★ 启动全部服务(主入口):Web + 调度器 + 启动补跑。"""
    import uvicorn

    s = get_settings()
    _echo(interpreter_banner())
    _echo(f"配置: {s.safe_repr()}")
    _echo(f"提示: 健康检查 http://{host or s.web.host}:{port or s.web.port}/healthz")
    uvicorn.run(
        "hoteldata.main:app",
        host=host or s.web.host,
        port=port or s.web.port,
        reload=reload,
        log_config=None,
    )


# ===========================================================================
# db
# ===========================================================================


@db_app.command("ping")
def db_ping() -> None:
    """连通性检查(返回 PG 版本)。"""

    async def _go() -> None:
        async with Runtime.create(with_scheduler=False) as rt:
            info = await rt.db.server_info()
            version = await rt.db.ping()
            _ok(f"数据库可达: {info['database']} (user={info['user']}, tz={info['timezone']})")
            _echo(f"  已建表 {info['tables']} 张")
            _echo(f"  {version.split(',')[0]}")

    _run(_go())


@db_app.command("upgrade")
def db_upgrade(revision: str = typer.Argument("head")) -> None:
    """执行 Alembic 迁移(DDL 只走 Alembic)。"""
    from alembic import command
    from alembic.config import Config

    s = get_settings()
    cfg = Config(str(s.paths.project_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(s.paths.project_root / "src/hoteldata/infra/migrations"))
    command.upgrade(cfg, revision)
    _ok(f"已迁移到 {revision}")
    _ = s


@db_app.command("downgrade")
def db_downgrade(revision: str = typer.Argument("base")) -> None:
    """回滚 Alembic 迁移。"""
    from alembic import command
    from alembic.config import Config

    s = get_settings()
    cfg = Config(str(s.paths.project_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(s.paths.project_root / "src/hoteldata/infra/migrations"))
    command.downgrade(cfg, revision)
    _ok(f"已回滚到 {revision}")


# ===========================================================================
# rules
# ===========================================================================


@rules_app.command("check")
def rules_check(show_stats: bool = typer.Option(True, "--stats/--no-stats")) -> None:
    """校验 api_rules.json(带**路径定位**)+ 轮换清单一致性。"""
    from hoteldata.domains.collect.rotation import RotationError, get_rotation
    from hoteldata.domains.collect.rules import ApiRulesError, get_api_rules

    try:
        rules = get_api_rules(force=True)
    except ApiRulesError as exc:
        _err(str(exc))
        raise typer.Exit(code=2) from exc
    _ok(f"api_rules 校验通过: {rules.path}")
    if show_stats:
        for key, value in rules.stats().items():
            _echo(f"  {key:22s} {value}")

    try:
        reg = get_rotation()
        notes = reg.validate_against_rules()
        plan = reg.pick(date.today())
        _ok(f"轮换清单校验通过: {reg.path}")
        _echo(f"  今日 offset={plan.offset} / 共 {plan.total_items} 项 / 取 {len(plan.items)} 项")
        for line in notes:
            _warn(f"  有意的例外:{line}")
        _echo(f"  fixed_daily(不进轮换,每日固定采 {len(plan.fixed_daily)} 项): {plan.fixed_daily}")
    except RotationError as exc:
        _err(str(exc))
        raise typer.Exit(code=2) from exc


# ===========================================================================
# login / sessions
# ===========================================================================


@app.command("login")
def login(
    platform: str = typer.Option("ctrip", help="平台:ctrip | meituan"),
    alias: str = typer.Option(..., help="账号别名(如 ctrip001)"),
    role: str = typer.Option("ebooking", help="角色:ebooking | merchant | ota"),
    interactive: bool = typer.Option(True, "--interactive/--auto", help="是否等待人工完成"),
) -> None:
    """人工登录:弹出**有头**浏览器并保存登录态。"""

    async def _go() -> int:
        async with Runtime.create(with_scheduler=False, with_browser=True) as rt:
            handle = rt.sessions.handle(platform, role, alias)
            credentials = await _credentials(rt, alias)
            manager = rt.login()
            attempt = (
                await manager.interactive_login(handle, credentials)
                if interactive
                else await manager.auto_login(handle, credentials)
            )
            if attempt.ok:
                await rt.sessions.set_status(
                    handle.key, "valid", last_login_at=datetime.now(), file_mtime=handle.mtime()
                )
                _ok(f"登录成功({attempt.code}),登录态已保存: {handle.storage_state_path()}")
                _echo(f"  cookies={attempt.cookies}")
                return 0
            _err(f"登录未成功({attempt.code}): {attempt.detail}")
            await rt.sessions.set_status(handle.key, "invalid")
            return 1

    raise typer.Exit(code=_run(_go()))


@app.command("sessions")
def sessions(
    check: bool = typer.Option(False, "--check", help="逐个探活(约 5 秒/个)"),
    show_all: bool = typer.Option(False, "--all", help="显示未登记的登录态文件"),
) -> None:
    """查看登录态与有效性。"""

    async def _go() -> int:
        async with Runtime.create(with_scheduler=False) as rt:
            await rt.sessions.sync_files()
            rows = await rt.sessions.list_sessions()
            if not rows and not show_all:
                _warn("sessions 表为空;可运行 scripts/migrate_states.py 迁移旧登录态")
                return 0
            typer.secho(
                f"{'平台':8s} {'角色':10s} {'别名':14s} {'状态':9s} {'文件':5s} {'cookies':8s} {'天':6s} {'需续登':6s}",
                bold=True,
            )
            for row in rows:
                handle = rt.sessions.handle(row.platform, row.role, row.alias)
                desc = rt.sessions.describe(handle)
                state = row.status
                if check:
                    state = str(await rt.login().check(handle))
                typer.secho(
                    f"{row.platform:8s} {row.role:10s} {row.alias:14s} {state:9s} "
                    f"{'是' if desc['exists'] else '否':5s} {desc['cookies']:<8d} "
                    f"{(desc['age_days'] or 0):<6.2f} {'是' if desc['need_renewal'] else '否':6s}",
                    fg=typer.colors.GREEN if state == "valid" else typer.colors.YELLOW,
                )
            return 0

    raise typer.Exit(code=_run(_go()))


async def _credentials(rt: Runtime, alias: str) -> tuple[str, str] | None:
    """从 ``core_accounts`` 取并解密凭据;账号不存在返回 ``None``(转人工)。"""
    from hoteldata.infra.crypto import get_cipher
    from hoteldata.infra.models import Account

    async with rt.db.session() as s:
        acc = (await s.execute(select(Account).where(Account.alias == alias))).scalar_one_or_none()
    if acc is None:
        _warn(f"core_accounts 无 {alias};将只能人工登录")
        return None
    try:
        cipher = get_cipher(rt.settings)
        return (cipher.decrypt(acc.username_enc), cipher.decrypt(acc.password_enc))
    except Exception as exc:  # noqa: BLE001
        _warn(f"凭据解密失败({exc});将只能人工登录")
        return None


# ===========================================================================
# collect
# ===========================================================================


@collect_app.callback(invoke_without_command=True)
def collect_default(
    ctx: typer.Context,
    hotel: str | None = typer.Option(None, "--hotel", "-h", help="酒店名或 id;缺省=全部活跃酒店"),
    page: str | None = typer.Option(None, "--page", "-p", help="采集页(如 经营报告)"),
    module: str | None = typer.Option(None, "--module", "-m", help="子模块名"),
    window: str | None = typer.Option(None, "--window", "-w", help="数据窗口(如 昨日)"),
    rotation: bool = typer.Option(False, "--rotation", help="只采当日轮换(+ fixed_daily)"),
    all_windows: bool = typer.Option(False, "--all-windows", help="每模块采全部 9 个窗口"),
    screenshot: bool = typer.Option(False, "--screenshot", help="只截图(不取数)"),
    day: str | None = typer.Option(None, "--date", help="采集日 YYYY-MM-DD(默认今天)"),
    no_browser: bool = typer.Option(False, "--no-browser", help="禁用浏览器兜底(只跑 API)"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="不打进度行"),
) -> None:
    """提取(默认按规则的窗口采集;`--rotation` 只采当日轮换)。"""
    if ctx.invoked_subcommand is not None:
        return

    async def _go() -> int:
        async with Runtime.create(with_scheduler=False, with_browser=not no_browser) as rt:
            if screenshot:
                return await _do_screenshot(rt, hotel, day)
            return await _do_collect(
                rt,
                hotel,
                page,
                module,
                window,
                rotation,
                all_windows,
                day,
                allow_browser=not no_browser,
                quiet=quiet,
            )

    raise typer.Exit(code=_run(_go()))


async def _resolve_hotels(rt: Runtime, selector: str | None) -> list[tuple[Any, Any]]:
    from hoteldata.infra.models import Account, Hotel

    async with rt.db.session() as s:
        stmt = select(Hotel).where(Hotel.status == "active").order_by(Hotel.id)
        hotels = list((await s.execute(stmt)).scalars().all())
        accounts = {a.id: a for a in (await s.execute(select(Account))).scalars().all()}
    if selector:
        sel = selector.strip()
        picked = [h for h in hotels if h.name == sel or str(h.id) == sel or sel in (h.name or "")]
        if not picked:
            _err(f"找不到酒店 {selector!r}(活跃酒店 {len(hotels)} 家)")
            return []
        hotels = picked
    out = []
    for h in hotels:
        acc = accounts.get(h.account_id) if h.account_id else None
        if acc is None:
            _warn(f"酒店 {h.name} 未绑定账号,跳过")
            continue
        out.append((h, acc))
    return out


async def _do_collect(
    rt: Runtime,
    hotel: str | None,
    page: str | None,
    module: str | None,
    window: str | None,
    rotation: bool,
    all_windows: bool,
    day: str | None,
    *,
    allow_browser: bool,
    quiet: bool,
) -> int:
    from hoteldata.domains.collect.channels import aggregate_results
    from hoteldata.domains.collect.datacenter import CollectorStats, DatacenterExtractor
    from hoteldata.domains.collect.repository import CollectRepository
    from hoteldata.domains.collect.windows import parse_collect_date

    pairs = await _resolve_hotels(rt, hotel)
    if not pairs:
        return 1
    day_d = parse_collect_date(day)
    extractor = DatacenterExtractor(
        rt.rules,
        pool=rt.browser,
        ensure_login=rt.ensure_login,
        allow_browser=allow_browser and rt.browser is not None,
    )
    stats = CollectorStats()
    any_failed = False
    for h, acc in pairs:
        _echo(f"\n== {h.name}(账号 {acc.alias}){'' if not rotation else ' · 当日轮换'} ==")
        ctx = rt.extract_context(h, acc, day_d)
        async with rt.db.session() as s:
            repo = CollectRepository(s)
            results = await extractor.extract(
                ctx,
                page=page,
                module=module,
                window=window,
                rotation=rotation,
                all_windows=all_windows,
                day=day_d,
                repo=repo,
                stats=stats,
                on_progress=None if quiet else _progress_line,
            )
        summary = aggregate_results(results)
        lines = {k: v for k, v in summary.items() if k not in ("per_hotel",)}
        typer.secho(
            f"  汇总: {json.dumps(lines, ensure_ascii=False)}",
            fg=typer.colors.RED if summary["failed"] else typer.colors.GREEN,
        )
        if summary["failed"]:
            any_failed = True
    _echo(f"\n总汇总: {json.dumps(stats.as_dict(), ensure_ascii=False)}")
    return 1 if any_failed else 0


async def _do_screenshot(rt: Runtime, hotel: str | None, day: str | None) -> int:
    from hoteldata.domains.collect.repository import CollectRepository
    from hoteldata.domains.collect.screenshot import Screenshoter, screenshot_demand_modules
    from hoteldata.domains.collect.windows import parse_collect_date

    if rt.browser is None:
        await rt.start_browser()
    pairs = await _resolve_hotels(rt, hotel)
    if not pairs:
        return 1
    day_d = parse_collect_date(day)
    targets = screenshot_demand_modules(day_d, rules=rt.rules)
    _echo(f"截图目标 {len(targets)} 个: {[f'{p}/{m}' for p, m in targets]}")
    shooter = Screenshoter(settings=rt.settings, rules=rt.rules, pool=rt.browser)
    any_failed = False
    for h, acc in pairs:
        ctx = rt.extract_context(h, acc, day_d)
        async with rt.db.session() as s:
            repo = CollectRepository(s)
            results = await shooter.run(ctx, targets, repo=repo)
        shots = sum(r.record_count for r in results)
        failed = sum(1 for r in results if r.status == "failed")
        typer.secho(
            f"  {h.name}: 出图 {shots} 张,失败 {failed}",
            fg=typer.colors.RED if failed else typer.colors.GREEN,
        )
        any_failed = any_failed or bool(failed)
    return 1 if any_failed else 0


@collect_app.command("portal")
def collect_portal(
    hotel: str | None = typer.Option(None, "--hotel", "-h"),
    day: str | None = typer.Option(None, "--date"),
) -> None:
    """批次 D:预警三源提取(渠道 / 首页待办 / 热点日历)。"""

    async def _go() -> int:
        from hoteldata.domains.collect.portal import PortalExtractor
        from hoteldata.domains.collect.repository import CollectRepository
        from hoteldata.domains.collect.windows import parse_collect_date

        async with Runtime.create(with_scheduler=False) as rt:
            pairs = await _resolve_hotels(rt, hotel)
            if not pairs:
                return 1
            day_d = parse_collect_date(day)
            extractor = PortalExtractor()  # ★ 无参构造(该类无 __init__)
            failed = 0
            for h, acc in pairs:
                ctx = rt.extract_context(h, acc, day_d)
                results = await extractor.extract(ctx)
                async with rt.db.session() as s:
                    repo = CollectRepository(s)
                    written = 0
                    for res in results:
                        if res.records:
                            written += await repo.upsert_portal_columns(res.records)
                failed += sum(1 for r in results if r.status == "failed")
                typer.secho(
                    f"  {h.name}: {len(results)} 源 → {written} 列",
                    fg=typer.colors.GREEN if not failed else typer.colors.YELLOW,
                )
                for res in results:
                    if res.error:
                        _warn(f"    {res.target}: {res.error}")
            return 1 if failed else 0

    raise typer.Exit(code=_run(_go()))


@collect_app.command("room")
def collect_room(
    hotel: str | None = typer.Option(None, "--hotel", "-h"),
    day: str | None = typer.Option(None, "--date"),
) -> None:
    """批次 D:房态提取(★ ``available=1`` iff ``roomStatus=='G'``)。"""

    async def _go() -> int:
        from hoteldata.domains.collect.repository import CollectRepository
        from hoteldata.domains.collect.room import RoomExtractor
        from hoteldata.domains.collect.windows import parse_collect_date

        async with Runtime.create(with_scheduler=False) as rt:
            pairs = await _resolve_hotels(rt, hotel)
            if not pairs:
                return 1
            day_d = parse_collect_date(day)
            extractor = RoomExtractor()  # ★ 无参构造
            rc = 0
            for h, acc in pairs:
                ctx = rt.extract_context(h, acc, day_d)
                results = await extractor.extract(ctx)
                rows = [r for res in results for r in (res.records or [])]
                ok = all(r.status != "failed" for r in results) and bool(rows)
                if ok:
                    async with rt.db.session() as s:
                        repo = CollectRepository(s)
                        await repo.replace_room_states(ctx.hotel_id, day_d, rows)
                typer.secho(
                    f"  {h.name}: {len(rows)} 行,{'已写入(整批替换)' if ok else '降级未写库'}",
                    fg=typer.colors.GREEN if ok else typer.colors.YELLOW,
                )
                rc = rc or (0 if ok else 1)
            return rc

    raise typer.Exit(code=_run(_go()))


@collect_app.command("review")
def collect_review(
    hotel: str | None = typer.Option(None, "--hotel", "-h"),
    day: str | None = typer.Option(None, "--date"),
) -> None:
    """批次 D:点评提取(★ UPSERT **不回溯**)。"""

    async def _go() -> int:
        from hoteldata.domains.collect.repository import CollectRepository
        from hoteldata.domains.collect.review import ReviewExtractor
        from hoteldata.domains.collect.windows import parse_collect_date

        async with Runtime.create(with_scheduler=False) as rt:
            pairs = await _resolve_hotels(rt, hotel)
            if not pairs:
                return 1
            day_d = parse_collect_date(day)
            extractor = ReviewExtractor()  # ★ 无参构造
            failures = 0
            for h, acc in pairs:
                ctx = rt.extract_context(h, acc, day_d)
                results = await extractor.extract(ctx)
                async with rt.db.session() as s:
                    repo = CollectRepository(s)
                    n_rev = n_mat = 0
                    for res in results:
                        kind = (res.detail or {}).get("kind")
                        if kind == "review" and res.records:
                            n_rev += await repo.upsert_reviews(res.records)
                        elif kind == "material" and res.records:
                            n_mat += await repo.upsert_review_materials(res.records)
                failures += sum(1 for r in results if r.status == "failed")
                typer.secho(f"  {h.name}: 点评 {n_rev} 条,素材 {n_mat} 行")
            return 1 if failures else 0

    raise typer.Exit(code=_run(_go()))


# ===========================================================================
# rotation
# ===========================================================================


@app.command("rotation")
def rotation(
    day: str | None = typer.Option(None, "--date", help="YYYY-MM-DD(默认今天)"),
    verify: bool = typer.Option(False, "--verify", help="验证连续 21 天无重无漏(V9)"),
    days: int = typer.Option(21, "--days", help="--verify 的天数"),
) -> None:
    """预览某日轮换清单。"""
    from hoteldata.domains.collect.rotation import get_rotation
    from hoteldata.domains.collect.windows import parse_collect_date

    reg = get_rotation()
    day_d = parse_collect_date(day)
    plan = reg.pick(day_d)
    _echo(f"日期 {plan.day}  offset={plan.offset} / 共 {plan.total_items} 项 / 每日取 {len(plan.items)} 项")
    typer.secho(f"{'#':4s} {'名称':26s} {'页':10s} {'类型':18s} alias", bold=True)
    for item in plan.items:
        idx = plan.index_note.get(item.name, -1)
        typer.secho(f"{idx:<4d} {item.name:26s} {item.page:10s} {item.type:18s} {','.join(item.alias)}")
    _echo()
    _echo(f"fixed_daily(每日固定采,不进轮换 {len(plan.fixed_daily)} 项): {plan.fixed_daily}")
    _echo(f"当日实际采模块 {len(plan.effective_modules())} 个: {plan.effective_modules()}")
    notes = reg.validate_against_rules()
    if notes:
        _echo("\n有意的例外(不算失效):")
        for n in notes:
            _warn(f"  {n}")
    if verify:
        result = reg.verify_no_gap(day_d, days)
        _echo()
        _echo(json.dumps(result, ensure_ascii=False, indent=2))
        if not result["ok"]:
            _err("轮换验证未通过")
            raise typer.Exit(code=1)
        _ok(f"连续 {days} 天无重无漏")


# ===========================================================================
# task
# ===========================================================================


@task_app.command("ls")
def task_ls(
    today: bool = typer.Option(False, "--today", help="附今日运行状态"),
) -> None:
    """列出全部任务与 cron(**时刻表唯一来源**)。"""
    import hoteldata.jobs  # noqa: F401
    from hoteldata.infra.tasks import get_registry

    reg = get_registry()
    typer.secho(f"{'任务':24s} {'cron':14s} {'补跑':5s} {'max_delay':10s} 域", bold=True)
    for spec in reg.all():
        delay = str(spec.max_delay) if spec.max_delay else "-"
        typer.secho(
            f"{spec.name:24s} {spec.cron:14s} {'是' if spec.catch_up else '否':5s} {delay:10s} {spec.domain}"
        )
    _echo(f"\n共 {len(reg.names())} 个任务:{reg.names()}")

    if today:

        async def _go() -> None:
            r = get_registry()
            async with Runtime.create(with_scheduler=False) as rt:
                tz = rt.settings.tzinfo
                start = datetime.combine(date.today(), datetime.min.time(), tzinfo=tz)
                from hoteldata.infra.models import JobRun

                async with rt.db.session() as s:
                    rows = (
                        (
                            await s.execute(
                                select(JobRun).where(JobRun.started_at >= start).order_by(JobRun.started_at)
                            )
                        )
                        .scalars()
                        .all()
                    )
                if not rows:
                    _warn("今日还没有 job_runs 记录")
                    return
                typer.secho(
                    f"\n{'任务':22s} {'状态':9s} {'触发':9s} {'计划':20s} {'耗时ms':9s} 摘要",
                    bold=True,
                )
                for row in rows:
                    dur = row.duration_ms
                    typer.secho(
                        f"{row.task:22s} {row.status:9s} {row.trigger:9s} "
                        f"{(row.scheduled_at.strftime('%m-%d %H:%M') if row.scheduled_at else '-'):20s} "
                        f"{(round(dur) if dur else 0):<9d} {json.dumps(row.summary or {}, ensure_ascii=False)[:60]}",
                        fg=_status_color(row.status if row.status != "running" else "degraded"),
                    )
                _ = r

        _run(_go())


@task_app.command("run")
def task_run(
    name: str = typer.Argument(..., help="任务名,如 collect.modules"),
    hotel: str | None = typer.Option(None, "--hotel", "-h", help="只跑某家酒店(id)"),
    with_browser: bool = typer.Option(True, "--browser/--no-browser"),
) -> None:
    """手动运行任务(``scheduled_at=NULL``,不受唯一索引阻挡)。"""
    import hoteldata.jobs  # noqa: F401

    async def _go() -> int:
        async with Runtime.create(with_scheduler=False, with_browser=with_browser) as rt:
            kwargs: dict[str, Any] = {}
            if hotel:
                kwargs["hotel_id"] = int(hotel)
            res = await rt.tasks.run(name, rt, trigger="manual", kwargs=kwargs)
            _json(res.as_dict())
            return 0 if res.status == "ok" else 1

    raise typer.Exit(code=_run(_go()))


@task_app.command("log")
def task_log(
    name: str = typer.Argument(..., help="任务名"),
    limit: int = typer.Option(20, "--limit", "-n"),
) -> None:
    """查看任务历史。"""
    from hoteldata.infra.models import JobRun

    async def _go() -> None:
        async with Runtime.create(with_scheduler=False) as rt:
            async with rt.db.session() as s:
                rows = (
                    (
                        await s.execute(
                            select(JobRun).where(JobRun.task == name).order_by(JobRun.id.desc()).limit(limit)
                        )
                    )
                    .scalars()
                    .all()
                )
            if not rows:
                _warn(f"{name} 无历史记录")
                return
            for row in rows:
                typer.secho(
                    f"#{row.id:<6d} {row.status:9s} {row.trigger:9s} "
                    f"{(row.scheduled_at.strftime('%Y-%m-%d %H:%M') if row.scheduled_at else '-'):18s} "
                    f"{json.dumps(row.summary or {}, ensure_ascii=False)[:70]}",
                    fg=_status_color(row.status),
                )
                if row.error:
                    _echo(f"        error: {row.error[:160]}")

    _run(_go())


# ===========================================================================
# ops
# ===========================================================================


@ops_app.command("patrol")
def ops_patrol(
    alias: str | None = typer.Option(None, "--alias", help="只巡检某别名"),
    no_relogin: bool = typer.Option(False, "--no-relogin", help="不自动重登,只探测"),
) -> None:
    """登录巡检(串行)。"""
    import hoteldata.jobs  # noqa: F401

    async def _go() -> int:
        async with Runtime.create(with_scheduler=False, with_browser=not no_relogin) as rt:
            from hoteldata.domains.ops.patrol import patrol_once

            report = await patrol_once(rt, alias=alias, do_relogin=not no_relogin)
            _json(report.as_dict())
            return 0 if report.relogin_failed == 0 else 1

    raise typer.Exit(code=_run(_go()))


@ops_app.command("backup")
def ops_backup(retention: int | None = typer.Option(None, "--retention", help="保留天数")) -> None:
    """PG 冷备(同日重跑幂等)。"""

    async def _go() -> int:
        from hoteldata.domains.ops.backup import run as run_backup

        report = await run_backup(get_settings(), retention_days=retention)
        _json(report.as_dict())
        return 0 if report.ok else 1

    raise typer.Exit(code=_run(_go()))


@ops_app.command("cleanup")
def ops_cleanup(
    dry_run: bool = typer.Option(True, "--dry-run/--no-dry-run", help="默认 dry-run(安全)"),
    all_: bool = typer.Option(False, "--all", help="忽略保留期,清理全部可清理项"),
) -> None:
    """磁盘清理(**默认 dry-run**;``config/`` 与 ``db/`` 一个字节不动)。"""

    async def _go() -> int:
        from hoteldata.domains.ops.cleanup import run as run_cleanup

        report = await run_cleanup(get_settings(), dry_run=dry_run, all_=all_)
        _json(report.as_dict())
        return 0 if not report.errors else 1

    raise typer.Exit(code=_run(_go()))


@ops_app.command("selfcheck")
def ops_selfcheck(
    no_send: bool = typer.Option(True, "--no-send/--send", help="段1 只出指标,不推送"),
) -> None:
    """自检指标(**只读聚合**,推送留给段2)。"""

    async def _go() -> int:
        from hoteldata.domains.ops.selfcheck import run as run_selfcheck

        async with Runtime.create(with_scheduler=False) as rt:
            report = await run_selfcheck(rt.settings, db=rt.db)
        _json(report.as_dict())
        _ = no_send
        return 0

    raise typer.Exit(code=_run(_go()))


# ===========================================================================
# ★ 段2 · bots(机器人管理)
# ===========================================================================


@bots_app.command("list")
def bots_list() -> None:
    """列出 ``core_bots``(**不显示凭据**)。"""
    from hoteldata.infra.models import Bot

    async def _go() -> None:
        async with Runtime.create(with_scheduler=False) as rt:
            async with rt.db.session() as s:
                rows = list((await s.execute(select(Bot).order_by(Bot.name))).scalars().all())
            if not rows:
                _warn("core_bots 表为空;用 `hoteldata bots add --name <名> --bot-id <id> --secret <密钥>` 添加")
                return
            typer.secho(f"{'名称':16s} {'状态':9s} {'容量':6s} 备注", bold=True)
            for row in rows:
                typer.secho(
                    f"{row.name:16s} {row.status:9s} {row.capacity_per_bot:<6d} {row.remark or ''}",
                    fg=typer.colors.GREEN if row.status == "active" else typer.colors.YELLOW,
                )

    _run(_go())


@bots_app.command("add")
def bots_add(
    name: str = typer.Option(..., "--name", help="机器人名(唯一;审计里的 bot_id 就是它)"),
    bot_id: str = typer.Option(..., "--bot-id", help="企微机器人 bot_id"),
    secret: str = typer.Option(..., "--secret", help="企微机器人 secret"),
    capacity: int = typer.Option(10, "--capacity", help="建议承载群数(仅告警)"),
    remark: str = typer.Option("", "--remark"),
) -> None:
    """新增机器人(**凭据 Fernet 加密后落库,明文不落盘**)。"""
    from hoteldata.infra.crypto import get_cipher
    from hoteldata.infra.models import Bot

    async def _go() -> int:
        async with Runtime.create(with_scheduler=False) as rt:
            cipher = get_cipher(rt.settings)
            async with rt.db.session() as s:
                exists = (await s.execute(select(Bot).where(Bot.name == name))).scalar_one_or_none()
                if exists is not None:
                    _err(f"机器人 {name} 已存在")
                    return 1
                s.add(
                    Bot(
                        name=name,
                        bot_id_enc=cipher.encrypt(bot_id),
                        secret_enc=cipher.encrypt(secret),
                        capacity_per_bot=capacity,
                        remark=remark or None,
                    )
                )
            _ok(f"已添加机器人 {name}")
            return 0

    raise typer.Exit(code=_run(_go()))


@bots_app.command("delete")
def bots_delete(name: str = typer.Argument(..., help="机器人名")) -> None:
    """删除机器人。"""
    from hoteldata.infra.models import Bot

    async def _go() -> int:
        async with Runtime.create(with_scheduler=False) as rt:
            async with rt.db.session() as s:
                row = (await s.execute(select(Bot).where(Bot.name == name))).scalar_one_or_none()
                if row is None:
                    _err(f"机器人 {name} 不存在")
                    return 1
                await s.delete(row)
            _ok(f"已删除机器人 {name}")
            return 0

    raise typer.Exit(code=_run(_go()))


@bots_app.command("health")
def bots_health(wait: float = typer.Option(3.0, "--wait", help="建立连接后的等待秒数")) -> None:
    """★ 真实连一次并显示每个实例的连接状态(V23)。

    消费的是 :meth:`BotManager.health` 的**统一契约** ``dict[str, bool]``
    —— 修 D18 后,这里与「状态」命令读到的是同一份数据。
    """
    import asyncio as _asyncio

    async def _go() -> int:
        async with Runtime.create(with_scheduler=False) as rt:
            manager = await rt.start_bots()
            if manager.size() == 0:
                _warn("机器人实例为 0(core_bots 为空或 AIBOT_ENABLED=0)")
                return 1
            await _asyncio.sleep(max(0.0, wait))
            health = manager.health()  # ★ 统一契约
            for name, online in health.items():
                typer.secho(
                    f"{name:16s} {'在线' if online else '离线'}",
                    fg=typer.colors.GREEN if online else typer.colors.RED,
                )
            online_n = sum(1 for v in health.values() if v)
            _echo(f"\n在线 {online_n} / {len(health)}")
            return 0 if online_n else 1

    raise typer.Exit(code=_run(_go()))


# ===========================================================================
# ★ 段2 · push(推送)
# ===========================================================================


@push_app.command("now")
def push_now(
    group: str = typer.Option(..., "--group", "-g", help="群 chatid"),
    force: bool = typer.Option(False, "--force", help="绕过当日去重"),
) -> None:
    """手动推一次日报(V37;真实发送)。"""
    from hoteldata.domains.report.daily import build_daily_message

    async def _go() -> int:
        async with Runtime.create(with_scheduler=False) as rt:
            await _ensure_gateway(rt)  # ★ 真的发送 → 必须先把机器人网关拉起来
            msg = await build_daily_message(rt, group)
            if msg is None:
                _err(f"群 {group} 未绑定任何酒店(先发「绑定 <酒店名>」或 `hoteldata bind add`)")
                return 1
            delivery = await rt.push.push(msg, force=force, now=True)
            _json(delivery.as_dict() if delivery else {})
            return 0 if delivery and delivery.ok else 1

    raise typer.Exit(code=_run(_go()))


@push_app.command("log")
def push_log(
    today: bool = typer.Option(False, "--today", help="只看今天"),
    group: str | None = typer.Option(None, "--group", "-g", help="按群过滤"),
    hotel: str | None = typer.Option(None, "--hotel", "-h", help="按酒店 id 过滤"),
    push_type: str | None = typer.Option(None, "--type", help="按 push_type 过滤"),
    limit: int = typer.Option(50, "--limit", "-n"),
) -> None:
    """推送审计(V39)。"""
    day = date.today() if today else None

    async def _go() -> None:
        async with Runtime.create(with_scheduler=False) as rt:
            rows = await rt.audit.list_logs(
                day=day,
                group_chatid=group,
                hotel_id=int(hotel) if hotel else None,
                push_type=push_type,
                limit=limit,
            )
            stats = await rt.audit.day_stats(day)
            _echo(
                f"今日:ok={stats['ok']} failed={stats['failed']} skipped={stats['skipped']} "
                f"成功率={stats['rate']:.1f}%"
            )
            if not rows:
                _warn("没有匹配的推送记录")
                return
            typer.secho(
                f"\n{'时间':20s} {'群':26s} {'店':5s} {'robot':12s} {'类型':22s} {'状态':8s} 图",
                bold=True,
            )
            for row in rows:
                ts = row.pushed_at.strftime("%m-%d %H:%M:%S") if row.pushed_at else "-"
                typer.secho(
                    f"{ts:20s} {row.group_chatid[:26]:26s} {str(row.hotel_id or '-'):5s} "
                    f"{(row.bot_id or '')[:12]:12s} {row.push_type[:22]:22s} {row.status:8s} "
                    f"{row.media_count}",
                    fg=_status_color("ok" if row.status == "ok" else row.status),
                )
                if row.error:
                    _echo(f"      error: {row.error[:140]}")

    _run(_go())


# ===========================================================================
# ★ 段2 · bind(群绑定)
# ===========================================================================


@bind_app.command("list")
def bind_list(group: str | None = typer.Option(None, "--group", "-g", help="只看某个群")) -> None:
    """列出群 ↔ 酒店绑定(V29)。"""
    async def _go() -> None:
        async with Runtime.create(with_scheduler=False) as rt:
            if group:
                rows = await rt.bindings.for_group(group, include_paused=True)
            else:
                rows = await rt.bindings.list_all()
            if not rows:
                _warn("没有任何绑定")
                return
            typer.secho(f"{'群':34s} {'店':5s} {'酒店名':24s} {'暂停':5s} 城市", bold=True)
            for row in rows:
                typer.secho(
                    f"{row.chatid[:34]:34s} {row.hotel_id:<5d} {row.name[:24]:24s} "
                    f"{'是' if row.paused else '否':5s} {row.city or ''}"
                )

    _run(_go())


@bind_app.command("add")
def bind_add(
    group: str = typer.Option(..., "--group", "-g", help="群 chatid"),
    hotel: str = typer.Option(..., "--hotel", "-h", help="酒店名或 id"),
) -> None:
    """绑定(**幂等**)。"""
    async def _go() -> int:
        from hoteldata.infra.models import Hotel

        async with Runtime.create(with_scheduler=False) as rt:
            async with rt.db.session() as s:
                stmt = select(Hotel)
                stmt = stmt.where(Hotel.id == int(hotel)) if hotel.isdigit() else stmt.where(Hotel.name == hotel)
                row = (await s.execute(stmt)).scalar_one_or_none()
            if row is None:
                _err(f"找不到酒店 {hotel!r}")
                return 1
            created = await rt.bindings.bind(group, int(row.id))
            _ok(f"{'已绑定' if created else '本来就已绑定(幂等)'}:{group} → {row.name}")
            return 0

    raise typer.Exit(code=_run(_go()))


@bind_app.command("remove")
def bind_remove(
    group: str = typer.Option(..., "--group", "-g", help="群 chatid"),
    hotel: str | None = typer.Option(None, "--hotel", "-h", help="酒店名或 id;缺省=解绑该群全部"),
) -> None:
    """解绑。"""
    async def _go() -> int:
        from hoteldata.infra.models import Hotel

        async with Runtime.create(with_scheduler=False) as rt:
            hotel_id: int | None = None
            if hotel:
                async with rt.db.session() as s:
                    stmt = select(Hotel)
                    stmt = (
                        stmt.where(Hotel.id == int(hotel)) if hotel.isdigit() else stmt.where(Hotel.name == hotel)
                    )
                    row = (await s.execute(stmt)).scalar_one_or_none()
                if row is None:
                    _err(f"找不到酒店 {hotel!r}")
                    return 1
                hotel_id = int(row.id)
            n = await rt.bindings.unbind(group, hotel_id)
            _ok(f"已解绑 {n} 条")
            return 0 if n else 1

    raise typer.Exit(code=_run(_go()))


# ===========================================================================
# ★ 段2 · alert(预警)
# ===========================================================================


@alert_app.command("test")
def alert_test(
    rule: str | None = typer.Option(None, "--rule", help="只跑某条规则 id"),
    hotel: str | None = typer.Option(None, "--hotel", "-h", help="只看某家酒店"),
    slot: str = typer.Option("09:00", "--slot", help="名义时刻(HH:MM)"),
    send: bool = typer.Option(False, "--send", help="真的发送(默认干跑,不发送不写状态)"),
) -> None:
    """★ 预警干跑(V47/V48):默认 **dry-run** —— 不发送、不写状态、不写日志。"""
    async def _go() -> int:
        async with Runtime.create(with_scheduler=False) as rt:
            if send:
                # 只有真的要发的时候才拉网关:干跑连一次服务器是白等
                await _ensure_gateway(rt)
            result = await rt.alert().check(slot, dry_run=not send, rule_id=rule)
            if hotel:
                # ★ 按店过滤放在 CLI 侧:引擎的 check() 是"全酒店巡检"(它不该知道
                #   "我只想看一家店"这种调试意图),所以不做成引擎参数。
                triggers = [
                    t for t in result.get("triggers", []) if hotel in (t.get("hotel_name") or "")
                ]
                result["triggers"] = triggers
                result["trigger_count"] = len(triggers)
                result["filtered_by"] = hotel
            _json(result)
            return 0

    raise typer.Exit(code=_run(_go()))


@alert_app.command("status")
def alert_status(hotel: str | None = typer.Option(None, "--hotel", "-h", help="酒店名")) -> None:
    """预警状态(V50/V51:去重 / 恢复清零 / 忽略)。"""
    async def _go() -> None:
        async with Runtime.create(with_scheduler=False) as rt:
            _json(await rt.alert().status(hotel))

    _run(_go())


@alert_app.command("summary")
def alert_summary(
    push: bool = typer.Option(False, "--push", help="真的推管理群(默认只算不推)"),
) -> None:
    """每日汇总 + **送达率**(V54)。"""
    async def _go() -> None:
        async with Runtime.create(with_scheduler=False) as rt:
            if push:
                await _ensure_gateway(rt)
            _json(await rt.alert().summary(push=push))

    _run(_go())


# ===========================================================================
# ★ 段2 · review(点评)
# ===========================================================================


@review_app.command("draft")
def review_draft(
    hotel: str | None = typer.Option(None, "--hotel", "-h", help="只做某家店"),
    push: bool = typer.Option(False, "--push", help="真的推管理群"),
) -> None:
    """生成建议草稿(V55/V56)。"""
    async def _go() -> None:
        async with Runtime.create(with_scheduler=False) as rt:
            if push:
                await _ensure_gateway(rt)
                _json(await rt.review().suggest())
            else:
                _json(await rt.review().drafts(hotel))

    _run(_go())


@review_app.command("auto")
def review_auto(push: bool = typer.Option(False, "--push", help="真的执行")) -> None:
    """自动回复(V57:★ 当前必定走"未就绪 → 人工队列"路径)。"""
    async def _go() -> None:
        async with Runtime.create(with_scheduler=False) as rt:
            if push:
                await _ensure_gateway(rt)
                _json(await rt.review().auto_reply())
            else:
                _json(await rt.review().status())

    _run(_go())


@review_app.command("analysis")
def review_analysis(
    hotel: str | None = typer.Option(None, "--hotel", "-h", help="只渲染某家店"),
    push: bool = typer.Option(False, "--push", help="真的推群"),
) -> None:
    """点评分析日报(V58)。"""
    async def _go() -> None:
        async with Runtime.create(with_scheduler=False) as rt:
            if push:
                await _ensure_gateway(rt)
                _json(await rt.review().analysis())
            else:
                _json(await rt.review().status(hotel))

    _run(_go())


@review_app.command("status")
def review_status(hotel: str | None = typer.Option(None, "--hotel", "-h")) -> None:
    """点评状态(待回复 / 审计分布 / 自动模式门控原因)。"""
    async def _go() -> None:
        async with Runtime.create(with_scheduler=False) as rt:
            _json(await rt.review().status(hotel))

    _run(_go())


# ===========================================================================
# ★ 段2 · report(报告)
# ===========================================================================


@report_app.command("ls")
def report_ls(day: str | None = typer.Option(None, "--date", help="YYYY-MM-DD(默认今天)")) -> None:
    """列出 22 项与当日桶/窗口。"""
    from hoteldata.domains.collect.windows import parse_collect_date
    from hoteldata.domains.report.engine import buckets_for_date, windows_for_push
    from hoteldata.domains.report.schedule import get_schedule

    d = parse_collect_date(day)
    schedule = get_schedule()
    buckets = buckets_for_date(d)
    typer.secho(f"日期 {d}(桶:{'、'.join(buckets)})", bold=True)
    typer.secho(f"{'id':22s} {'名称':24s} {'页':10s} 当日窗口", bold=True)
    for item in schedule.items:
        windows = windows_for_push(item, d)
        typer.secho(
            f"{item.id:22s} {item.name[:24]:24s} {item.page[:10]:10s} "
            f"{'、'.join(windows) if windows else '(今日不发)'}"
        )
    typer.secho(
        f"\n共 {len(schedule.items)} 项;当日应发 "
        f"{len([i for i in schedule.items if windows_for_push(i, d)])} 项",
        bold=True,
    )


@report_app.command("run")
def report_run(
    item_id: str = typer.Argument(..., help="报告项 id,如 svc_weekly"),
    force: bool = typer.Option(True, "--force/--no-force", help="绕过当日去重(默认开)"),
    hotel: str | None = typer.Option(None, "--hotel", "-h", help="只跑某家酒店 id"),
) -> None:
    """单跑某一报告项(V45/V46)。

    ★ ``--force`` 缺省为**开**(``force=True``):CLI 是人工调试入口,
    "我就是要现在看一眼" 的意图远多于"尊重当日去重"。
    """
    async def _go() -> int:
        async with Runtime.create(with_scheduler=False) as rt:
            await _ensure_gateway(rt)  # ★ 报告项要真的推出去
            result = await rt.report().run_item(
                item_id, force=force, hotel_id=int(hotel) if hotel else None
            )
            _json(result)
            return 0 if result.get("ok") else 1

    raise typer.Exit(code=_run(_go()))


# ===========================================================================
# ★ 段3 · 比价(T3G.1)
# ===========================================================================


def _print_compare_result(result: Any, *, show_report: bool = True) -> None:
    """打印一次比价结果(CLI 通用)。"""
    from hoteldata.domains.compare.report import PLATFORM_LABELS, fmt_price, group_by_hotel

    anchor = result.anchor
    _echo()
    _ok(f"「{anchor.name}」比价完成({result.query_slot})")
    if anchor.self_price is not None:
        plat = PLATFORM_LABELS.get(anchor.self_price_platform or "", anchor.self_price_platform or "")
        _echo(f"  本店价: {fmt_price(anchor.self_price)}({plat})")
    if not result.quotes:
        _warn("  未取到任何附近酒店")
        return

    with_dist = sum(1 for q in result.quotes if q.distance_km is not None)
    _echo(
        f"  报价 {len(result.quotes)} 条;距离可用 {with_dist}/{len(result.quotes)}"
        + ("" if with_dist else "  ⚠️ 距离不可用,已按平台顺序")
    )

    # ★ **一家店一行,每个平台一个价** —— 与报告/推送同一份分组逻辑。
    #   旧输出是"一条报价一行",于是同一家店在两个平台各占一行、
    #   要人自己对着看差多少 —— 那恰恰是比价最该一眼看到的东西。
    groups = group_by_hotel(result.quotes)
    plats: list[str] = []
    for g in groups:
        for p in g.platforms:
            if p not in plats:
                plats.append(p)
    header = "  " + " " * 3 + f"{'酒店':36s}{'距锚点':>9s}  " + "  ".join(
        f"{PLATFORM_LABELS.get(p, p):>12s}" for p in plats
    ) + "   价差"
    _echo(header)
    for i, g in enumerate(groups, 1):
        dist = f"{g.distance_km:.2f}km" if g.distance_km is not None else "     —"
        cells: list[str] = []
        for p in plats:
            q = g.by_platform.get(p)
            if q is None or q.price is None:
                cells.append("           —")
                continue
            text = fmt_price(q.price) + ("起" if q.price_scope == "from" else "")
            if g.cheaper == p:
                text += "⭐"
            if q.need_manual_check:
                text += "⚠"
            cells.append(f"{text:>12s}")
        spread = f"  ¥{g.spread:g}({PLATFORM_LABELS.get(g.cheaper or '', '')}省)" if g.spread else ""
        name = g.hotel_name if len(g.hotel_name) <= 36 else g.hotel_name[:35] + "…"
        typer.secho(f"  {i:2d} {name:36s}{dist:>9s}  " + "  ".join(cells) + spread)
    if len(plats) >= 2:
        _echo("     ⭐ = 该店两平台里更便宜的一边;— = 该平台没有这家店")
    if result.notes:
        _echo()
        for note in result.notes:
            if note.startswith("报告:"):
                _ok(f"  {note}")
            else:
                _warn(f"  {note}")
    if show_report:
        _echo("\n(报告已写入 var/reports/compare/<锚点>/)")


@app.command("compare")
def compare_one(
    name: str = typer.Option(..., "--name", "-n", help="锚点酒店名"),
    city: str | None = typer.Option(None, "--city", "-c", help="城市(清单里优先)"),
    platform: str | None = typer.Option(
        None, "--platform", "-p", help="只跑某平台:ctrip | meituan(默认全部)"
    ),
    nights: int = typer.Option(1, "--nights", help="住几晚"),
    rooms: int | None = typer.Option(None, "--rooms", help="每个平台保留几条报价"),
    nearby: int | None = typer.Option(None, "--nearby", help="附近取几家(默认 HOTEL_NEARBY_COUNT)"),
    date_: str | None = typer.Option(None, "--date", help="入住日期 YYYY-MM-DD(默认今天)"),
    demo: bool = typer.Option(False, "--demo", help="演示模式(落库标 is_demo,不污染真实数据)"),
    no_save: bool = typer.Option(False, "--no-save", help="只出报告不落库"),
    headless: bool | None = typer.Option(None, "--headless/--headed", help="临时覆盖有头/无头"),
) -> None:
    """★ 单店比价:定位锚点 → 取附近 N 家报价 → 出 md/html 报告 → 存档。

    段3 的唯一验收入口(计划书 §2.1):

        hoteldata compare --name "隐欲民宿"
    """
    from datetime import date as _date

    from hoteldata.domains.compare.contract import PriceFatalError

    async def _go() -> int:
        async with Runtime.create(with_scheduler=False) as rt:
            day = _date.fromisoformat(date_) if date_ else None
            plats = [platform] if platform else None
            try:
                result = await rt.compare().compare(
                    name,
                    city=city,
                    platforms=plats,
                    nights=nights,
                    quote_count=rooms,
                    nearby_count=nearby,
                    day=day,
                    demo=demo,
                    persist=not no_save,
                )
            except PriceFatalError as exc:
                _err(f"比价失败(不可重试):{exc}")
                return 1
            _print_compare_result(result)
            return 0 if result.quotes else 1

    raise typer.Exit(code=_run(_go()))


@app.command("compare-batch")
def compare_batch(
    force: bool = typer.Option(False, "--force", help="绕过「今天有没有真数据」判重"),
    city: str | None = typer.Option(None, "--city", help="只跑某城市"),
    day: str | None = typer.Option(None, "--date", help="目标日期 YYYY-MM-DD(默认今天)"),
) -> None:
    """批量比价:遍历 ``cmp_price_targets(mode='batch')``。**单店失败不阻断**。"""
    from datetime import date as _date

    async def _go() -> int:
        async with Runtime.create(with_scheduler=False) as rt:
            target = _date.fromisoformat(day) if day else None
            summary = await rt.compare().run_batch(day=target, force=force)
            _json(summary)
            ok = int(summary.get("ok", 0))
            failed = int(summary.get("failed", 0))
            if summary.get("skipped"):
                _warn(f"已跳过:{summary.get('reason')}")
                return 0
            (_ok if not failed else _warn)(
                f"{summary.get('hotels', 0)} 家:成功 {ok},失败 {failed};"
                f"cmp_batch_runs 该日 {summary.get('cmp_batch_rows', '?')} 行"
            )
            return 0 if failed == 0 else 1

    raise typer.Exit(code=_run(_go()))


# ---- price 子命令 ----


@price_app.command("login")
def price_login(
    platform: str = typer.Option(
        "ctrip", "--platform", "-p", help="ctrip(携程前台)| meituan(美团前台)"
    ),
    alias: str | None = typer.Option(None, "--alias", help="登录态别名(默认与平台同名)"),
    timeout: float = typer.Option(600.0, "--timeout", help="人工登录等待秒数"),
    hotel_id: str = typer.Option(
        "", "--hotel-id", help="用一个具体酒店的详情页做登录验证(推荐:会显示价格列)"
    ),
    name: str = typer.Option("", "--name", help="用酒店名自动查 ebk_hotel_id 作为验证页"),
) -> None:
    """★ 前台登录(``role='ota'`` / ``'ota_meituan'``)—— 有头浏览器里人工完成。

    ★ 这两个角色值**不是段3 起的** —— 段1 建 ``sessions`` 表时就写明了
      (``infra/models/core.py:112-113``)。段3 只是第一个真正使用它们的域。

    ★ 为什么不复用段1 的 ``interactive_login``:那个走的是 **``ebooking.ctrip.com``
      (商家后台)**,而比价要的是**公开前台**(``hotels.ctrip.com``)——
      两者是不同的 cookie 域。这里打开
      :data:`~hoteldata.domains.compare.FRONT_DESK_ENTRY_URLS` 里的前台入口,
      等人工登录完成后**回写登录态**。

    ★ **检测靠"能不能打开锚点详情页",不靠关键词**:前台页面本来就没有商家后台那套
      业务关键词(``BUSINESS_TEXT_KEYWORDS``),用关键词判会一直判"未登录"。
      所以这里用**真实用途**验证 —— 能渲染出页面就算登录成功。
    """
    import asyncio as _asyncio
    import datetime as _dt
    import time as _time

    from hoteldata.domains.compare import FRONT_DESK_ENTRY_URLS

    role = "ota" if platform == "ctrip" else "ota_meituan"
    use_alias = alias or platform
    entry = FRONT_DESK_ENTRY_URLS.get(platform)
    if entry is None:
        _err(f"未知平台 {platform};可选:{sorted(FRONT_DESK_ENTRY_URLS)}")
        raise typer.Exit(code=1)

    # 验证页用的 hotelId:显式给了就用;否则按酒店名查库
    resolved_hotel_id = hotel_id.strip()
    if not resolved_hotel_id and name.strip():
        try:
            async def _lookup() -> str:
                from sqlalchemy import select

                from hoteldata.infra.models import Hotel
                from hoteldata.runtime import Runtime

                async with Runtime.create(with_browser=False) as rt:
                    async with rt.db.session() as s:
                        row = (
                            await s.execute(select(Hotel).where(Hotel.name == name.strip()))
                        ).scalars().first()
                    return str(row.ebk_hotel_id) if row and row.ebk_hotel_id else ""

            resolved_hotel_id = _run(_lookup())
        except Exception as exc:  # noqa: BLE001
            _warn(f"按名称查 ebk_hotel_id 失败({exc}),用列表页做验证")

    async def _go() -> int:
        async with Runtime.create(with_browser=True) as rt:
            handle = rt.sessions.handle(platform, role, use_alias)
            _echo(f"打开 {platform} **前台**登录页:{entry}")
            _echo(f"登录态将写入:{handle.storage_state_path()}(role={role} alias={use_alias})")
            _warn(
                f"请在浏览器里完成登录(含滑块);程序最多等待 {timeout:.0f} 秒,"
                "**不会自动破解验证码**"
            )
            _echo(
                "  ★ 判据是**页面上有没有「登录看低价」按钮** —— 只看 cookie 数量会误判"
                "(匿名访问也会种下几十个 cookie)"
            )
            deadline = _time.monotonic() + timeout
            last_tip = 0.0
            # ★ 验证页:必须是一个**会显示价格列**的页面,否则「登录看低价」不出现、
            #   判据就失效。用详情页(带 hotelId 时)或列表页兜底。
            verify_url = entry
            if platform == "ctrip" and resolved_hotel_id:
                today = _dt.date.today()
                verify_url = (
                    f"https://hotels.ctrip.com/hotels/detail/?hotelId={hotel_id}"
                    f"&checkin={today.isoformat()}"
                    f"&checkout={(today + _dt.timedelta(days=1)).isoformat()}"
                )
            async with rt.browser.page_session(handle) as (_b, context, page):
                try:
                    await page.goto(entry, wait_until="domcontentloaded", timeout=60000)
                except Exception as exc:  # noqa: BLE001
                    _warn(f"导航告警(继续等待人工登录): {exc}")
                _echo(f"  登录后请让浏览器停在此页;验证页:{verify_url[:80]}")
                logged_in = False
                while _time.monotonic() < deadline:
                    snapshot = await _login_snapshot(page, platform)
                    if snapshot["logged_in"]:
                        logged_in = True
                        _ok(f"已登录(依据:{snapshot['reason']})")
                        break
                    now = _time.monotonic()
                    if now - last_tip >= 10.0:
                        last_tip = now
                        _echo(
                            f"  等待人工登录…剩余 {deadline - now:.0f} 秒"
                            f"(当前判定:{snapshot['reason']};"
                            f"{snapshot['cookies']} 个 cookie,url={snapshot['url'][:44]})"
                        )
                    await _asyncio.sleep(2.0)

                # ★ 二次确认:到**会显示价格**的页面上复验一次。
                #   登录页本身可能不出现「登录看低价」,所以必须换页确认。
                if logged_in and verify_url != entry:
                    try:
                        await page.goto(verify_url, wait_until="domcontentloaded", timeout=60000)
                        await _asyncio.sleep(12.0)
                        confirm = await _login_snapshot(page, platform)
                    except Exception as exc:  # noqa: BLE001
                        _warn(f"复验导航失败({exc}),以首次判定为准")
                        confirm = {"logged_in": True, "reason": "首次判定", "tickets": []}
                    if not confirm["logged_in"]:
                        logged_in = False
                        _err(
                            f"复验失败:在会显示价格的页面上仍判定为未登录"
                            f"({confirm['reason']})。登录没有真正生效"
                        )

                if not logged_in:
                    _err(
                        "登录未在超时内完成。请确认在浏览器里**真的完成了登录**"
                        "(输入账号密码并处理滑块),而不只是打开了页面。"
                    )
                    return 1
                # ★ 回写登录态(段1 的 page_session 也支持 save_state_to,
                #   这里显式再存一次以便立刻给出反馈)
                state = await context.storage_state()
                path = handle.storage_state_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                import json as _json

                path.write_text(_json.dumps(state, ensure_ascii=False), encoding="utf-8")
                # ★ 段1 的 sessions 表是**状态登记**(路径由三元组推导,不入库)
                from datetime import UTC as _UTC

                await rt.sessions.set_status(
                    handle.key,
                    "valid",
                    last_login_at=datetime.now(_UTC),
                    last_check_at=datetime.now(_UTC),
                    file_mtime=path.stat().st_mtime,
                )
            _ok(f"前台登录态已保存:cookies={len(state.get('cookies') or [])} → {path.name}")
            return 0

    raise typer.Exit(code=_run(_go()))


#: 未登录时携程详情页/列表页会出现的**页面级**标志文案(实测)
_CTRIP_LOGGED_OUT_MARKERS = (
    "登录看低价",
    "登录查看低价",
    "登录后查看",
)
#: 携程登录后的票类 cookie(实测匿名没有)
_CTRIP_TICKET_COOKIES = ("cticket", "login_type", "AHeadUserInfo", "login_uid", "ct_uid")


async def _login_snapshot(page: Any, platform: str = "ctrip") -> dict[str, Any]:
    """判断前台是否已登录 —— **以页面自身的信号为准**。

    ★ 这里踩过一个坑(实测两次),值得写下来:

    **第一版**:``bool(names & strong) or len(cookies) >= 20``。
    携程前台**匿名访问**就种 76 个 cookie(``ANON``/``BAIDUID``/``MUID``/``SRCHUID``…),
    于是"未登录"被判成"已登录"、登录态被写盘并标 ``valid`` ——
    而详情页上每个房型显示的还是「**登录看低价**」。

    **第二版**:把票据收窄成一个表。但表里混进了 ``token`` 这种**通用名**
    (站点自己的分析 cookie 也叫 token),照样误判。

    **结论**:**不要猜 cookie 名**。最有权威的信号是**页面自己**——
    未登录时携程会在价格列渲染「登录看低价」按钮。cookie 只作为辅助。

    判据(任一命中即算**未**登录):

    1. URL 在 ``passport`` / ``ilogin`` 等登录域上;
    2. 页面正文出现 :data:`_CTRIP_LOGGED_OUT_MARKERS` 里的标志文案。

    辅助(仅当两条都看不出时才用):命中真实票据 cookie。
    **绝不使用"cookie 数量"当信号** —— 它判的是"浏览器来过这个站"。
    """
    try:
        cookies = await page.context.cookies()
        names = {str(c.get("name") or "") for c in cookies}
        url = (page.url or "").lower()
    except Exception:  # noqa: BLE001
        return {"logged_in": False, "cookies": 0, "url": "", "tickets": [], "reason": "读取失败"}

    # ① 在登录域上 → 明确未登录
    if any(m in url for m in ("passport", "ilogin", "login.")):
        return {"logged_in": False, "cookies": len(cookies), "url": page.url or "",
                "tickets": [], "reason": "URL 在登录域"}

    # ② ★ 页面正文的"请登录"标志(最权威)
    try:
        body = await page.locator("body").inner_text(timeout=5000)
    except Exception:  # noqa: BLE001
        body = ""
    markers = _CTRIP_LOGGED_OUT_MARKERS if platform == "ctrip" else ("登录", "立即登录")
    for m in markers:
        if m in (body or ""):
            return {"logged_in": False, "cookies": len(cookies), "url": page.url or "",
                    "tickets": [], "reason": f"页面出现「{m}」"}

    # ③ 辅助:真实票据 cookie
    if platform == "ctrip":
        hit = sorted(names & set(_CTRIP_TICKET_COOKIES))
    else:
        hit = sorted(names & {"token", "edm", "mtgsig"})
    if hit:
        return {"logged_in": True, "cookies": len(cookies), "url": page.url or "",
                "tickets": hit, "reason": f"票据 {hit}"}

    # ④ 页面没有"请登录"文案、也没有票据 → 保守判为**未登录**(宁可让人重登)
    return {"logged_in": False, "cookies": len(cookies), "url": page.url or "",
            "tickets": [], "reason": "未发现登录票据(保守判定)"}


@price_app.command("probe")
def price_probe(
    platform: str = typer.Option("ctrip", "--platform", "-p", help="ctrip | meituan"),
    name: str = typer.Option(..., "--name", "-n", help="锚点酒店名"),
    city: str | None = typer.Option(None, "--city", "-c"),
) -> None:
    """★ 页面结构探测(诊断)—— 平台改版时**逐条选择器报命中数**。

    段3 风险 T1(前台改版)的对策:一眼看出**是哪一条选择器失效了**,
    而不是只得到"取不到价"这种没有信息量的结论。
    """
    from hoteldata.domains.compare import load_platforms
    from hoteldata.domains.compare.platforms.selectors import PROBE_TARGETS

    async def _go() -> int:
        load_platforms()
        rows = PROBE_TARGETS.get(platform)
        if not rows:
            _err(f"未知平台 {platform};可选:{sorted(PROBE_TARGETS)}")
            return 1
        async with Runtime.create(with_browser=True, with_scheduler=False) as rt:
            handle = rt.sessions.handle(
                platform, "ota" if platform == "ctrip" else "ota_meituan", platform
            )
            # ★ 段1 浏览器池的 ``page_session`` 产出的是**三元组**
            #   ``(browser, context, page)``(B14 遗产,形状不许改)
            async with rt.browser.page_session(handle) as (_browser, _context, page):
                url = (
                    "https://hotels.ctrip.com/"
                    if platform == "ctrip"
                    else "https://i.meituan.com/awp/h5/hotel/search/search.html"
                )
                _echo(f"打开 {url}")
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                except Exception as exc:  # noqa: BLE001
                    _warn(f"导航告警(继续探测): {exc}")
                _echo()
                for label, sel in rows:
                    try:
                        n = await page.locator(sel).count()
                    except Exception as exc:  # noqa: BLE001
                        _err(f"  {label:12s} {sel[:52]:52s} 取值失败: {exc}")
                        continue
                    fn = _ok if n else _warn
                    fn(f"  {label:12s} {sel[:52]:52s} 命中 {n}")
            return 0

    raise typer.Exit(code=_run(_go()))


@price_app.command("history")
def price_history(
    name: str = typer.Option(..., "--name", "-n", help="锚点酒店名"),
    days: int = typer.Option(7, "--days", help="看最近几天"),
    demo: bool = typer.Option(False, "--demo", help="包含演示数据"),
) -> None:
    """历史价格查询(按天 + slot 汇总)。"""
    async def _go() -> int:
        async with Runtime.create(with_browser=False) as rt:
            data = await rt.compare().history(name, days=days, include_demo=demo)
            rows = data.get("days") or []
            if not rows:
                _warn(f"「{name}」暂无历史记录")
                return 1
            _echo(f"「{name}」最近 {len(rows)} 天:")
            for row in rows:
                slots = "/".join(s.rsplit("-", 1)[-1] for s in row["slots"])
                _echo(
                    f"  {row['date']}  采集 {len(row['slots'])} 次({slots})  "
                    f"记录 {row['rows']} 条  最低价 {row['min_price'] if row['min_price'] is not None else '—'}"
                )
            return 0

    raise typer.Exit(code=_run(_go()))


@price_app.command("push")
def price_push_cmd(
    slot: str | None = typer.Option(None, "--slot", help="指定 slot(默认当日最近一次)"),
    day: str | None = typer.Option(None, "--date", help="目标日期 YYYY-MM-DD"),
    force: bool = typer.Option(False, "--force", help="绕过当日去重"),
) -> None:
    """手动触发比价独立推送(14:00/18:00 的等价命令)。"""
    from datetime import date as _date

    async def _go() -> int:
        async with Runtime.create(with_scheduler=False) as rt:
            await _ensure_gateway(rt)
            result = await rt.compare().push_price(
                day=_date.fromisoformat(day) if day else None, slot=slot, force=force
            )
            _json(result)
            return 0

    raise typer.Exit(code=_run(_go()))


# ---- targets 子命令 ----


@targets_app.command("add")
def targets_add(
    name: str = typer.Option(..., "--name", "-n", help="锚点酒店名"),
    city: str | None = typer.Option(None, "--city", "-c"),
    mode: str = typer.Option("batch", "--mode", help="batch(批量清单)| cron(定时采集)"),
    platforms: str = typer.Option("ctrip,meituan", "--platforms", help="参与平台,逗号分隔"),
    nights: int = typer.Option(1, "--nights"),
    ebk_hotel_id: str | None = typer.Option(None, "--ebk-id", help="携程 ebk_hotel_id(直达用)"),
) -> None:
    """新增比价目标(取代旧 ``compare_hotels.txt`` / ``price_targets.txt``)。"""
    async def _go() -> int:
        from hoteldata.domains.compare.repository import CompareRepository

        async with Runtime.create(with_browser=False) as rt:
            async with rt.db.session() as session:
                repo = CompareRepository(session)
                tid = await repo.upsert_target(
                    anchor_name=name,
                    city=city,
                    mode=mode,
                    platforms=[p.strip() for p in platforms.split(",") if p.strip()],
                    nights=nights,
                    ebk_hotel_id=ebk_hotel_id,
                )
                await repo.backfill_target_hotel_ids()
                await session.commit()
            _ok(f"目标已写入(id={tid}):{name} city={city} mode={mode}")
            return 0

    raise typer.Exit(code=_run(_go()))


@targets_app.command("list")
def targets_list(
    mode: str | None = typer.Option(None, "--mode", help="只看某个模式"),
    all_: bool = typer.Option(False, "--all", help="含已停用的"),
) -> None:
    """列出比价目标。"""
    async def _go() -> int:
        from hoteldata.domains.compare import load_platforms
        from hoteldata.domains.compare.registry import available_platforms
        from hoteldata.domains.compare.repository import CompareRepository

        load_platforms()
        async with Runtime.create(with_browser=False) as rt:
            async with rt.db.session() as session:
                repo = CompareRepository(session)
                if all_:
                    from sqlalchemy import select

                    from hoteldata.infra.models import CmpPriceTarget

                    rows = list((await session.execute(select(CmpPriceTarget))).scalars().all())
                else:
                    rows = await repo.list_targets(mode=mode)
            if not rows:
                _warn("无比价目标。用 `hoteldata targets add --name <酒店> [--city <城市>]` 添加")
                _echo(f"已注册平台:{list(available_platforms())}")
                return 1
            _echo(f"{'酒店':30s} {'城市':10s} {'模式':8s} {'平台':18s} {'启用':4s} ebk_id")
            for r in rows:
                _echo(
                    f"{r.anchor_name[:30]:30s} {(r.city or '—')[:10]:10s} {r.mode:8s} "
                    f"{','.join(r.platforms or [])[:18]:18s} "
                    f"{'是' if r.enabled else '否':4s} {r.ebk_hotel_id or '—'}"
                )
            return 0

    raise typer.Exit(code=_run(_go()))


@targets_app.command("set")
def targets_set(
    name: str = typer.Option(..., "--name", "-n"),
    city: str | None = typer.Option(None, "--city", "-c"),
    enable: bool | None = typer.Option(None, "--enable/--disable", help="启用/停用"),
    ebk_hotel_id: str | None = typer.Option(None, "--ebk-id"),
    mode: str | None = typer.Option(None, "--mode"),
    platforms: str | None = typer.Option(None, "--platforms"),
    nights: int | None = typer.Option(None, "--nights"),
) -> None:
    """修改比价目标(启用/停用、改城市、改平台、补 ebk_id)。"""
    async def _go() -> int:
        from hoteldata.domains.compare.repository import CompareRepository

        async with Runtime.create(with_browser=False) as rt:
            async with rt.db.session() as session:
                repo = CompareRepository(session)
                if enable is not None:
                    n = await repo.set_target_enabled(name, enable, city=city)
                    await session.commit()
                    if n == 0 and not any(
                        v is not None for v in (ebk_hotel_id, mode, platforms, nights)
                    ):
                        _err(f"未找到目标:{name}(city={city})")
                        return 1
                    if n:
                        _ok(f"{'已启用' if enable else '已停用'} {name}({n} 行)")
                if any(v is not None for v in (ebk_hotel_id, mode, platforms, nights)):
                    tid = await repo.upsert_target(
                        anchor_name=name,
                        city=city,
                        mode=mode or "batch",
                        platforms=[p.strip() for p in platforms.split(",") if p.strip()]
                        if platforms
                        else None,
                        nights=nights or 1,
                        ebk_hotel_id=ebk_hotel_id,
                    )
                    await repo.backfill_target_hotel_ids()
                    await session.commit()
                    _ok(f"目标已更新(id={tid}):{name}")
            return 0

    raise typer.Exit(code=_run(_go()))


@targets_app.command("remove")
def targets_remove(
    name: str = typer.Option(..., "--name", "-n"),
    city: str | None = typer.Option(None, "--city", "-c"),
) -> None:
    """删除比价目标。"""
    async def _go() -> int:
        from hoteldata.domains.compare.repository import CompareRepository

        async with Runtime.create(with_browser=False) as rt:
            async with rt.db.session() as session:
                repo = CompareRepository(session)
                n = await repo.remove_target(name, city=city)
                await session.commit()
            if n == 0:
                _err(f"未找到目标:{name}")
                return 1
            _ok(f"已删除 {n} 行:{name}")
            return 0

    raise typer.Exit(code=_run(_go()))


@targets_app.command("import")
def targets_import(
    from_dir: str = typer.Option(
        ..., "--from", help="旧系统根目录(读 config/compare_hotels.txt + price_targets.txt)"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="只解析并打印,不写库"),
) -> None:
    """★ 一次性导入旧系统的两个 txt(T3A.3 的导入脚本入口)。

    ``compare_hotels.txt`` → ``mode='batch'``;``price_targets.txt`` → ``mode='cron'``。
    两个文件**都是 GBK 编码**(实测),解析时按 GB18030 读。
    """
    from pathlib import Path

    from hoteldata.domains.compare.importer import parse_target_files

    root = Path(from_dir)
    parsed = parse_target_files(root)
    if not parsed:
        _warn(f"未在 {root / 'config'} 找到可解析的清单(compare_hotels.txt / price_targets.txt)")
        return
    _echo(f"解析出 {len(parsed)} 个目标:")
    for item in parsed:
        _echo(f"  {item['anchor_name']:34s} city={item['city'] or '—':10s} mode={item['mode']}")
    if dry_run:
        _warn("--dry-run:未写库")
        return

    async def _go() -> int:
        from hoteldata.domains.compare.repository import CompareRepository

        async with Runtime.create(with_browser=False) as rt:
            async with rt.db.session() as session:
                repo = CompareRepository(session)
                for item in parsed:
                    await repo.upsert_target(**item)
                filled = await repo.backfill_target_hotel_ids()
                await session.commit()
            _ok(f"已导入 {len(parsed)} 个目标;回填 hotel_id/ebk_hotel_id {filled} 行")
            return 0

    raise typer.Exit(code=_run(_go()))


# ===========================================================================
# ★ 后台(Phase 3;总纲 §7.8)
# ===========================================================================

admin_app = typer.Typer(help="后台:passwd / hash", no_args_is_help=True)
app.add_typer(admin_app, name="admin")


def _update_env_key(path: Any, key: str, value: str) -> str:
    """在 ``.env`` 里写入/替换一个键(**保留其他行与注释**)。

    返回动作说明(新增 / 已更新)。
    """
    from pathlib import Path

    p = Path(path)
    lines: list[str] = []
    if p.exists():
        lines = p.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    done = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        if stripped.split("=", 1)[0].strip().upper() == key.upper():
            out.append(f"{key}={value}")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"{key}={value}")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(out) + "\n", encoding="utf-8")
    return "已更新" if done else "已新增"


@admin_app.command("passwd")
def admin_passwd(
    set_: str | None = typer.Option(None, "--set", help="设置新口令(会写入 .env 的 ADMIN_PASSWORD_HASH)"),
    print_only: bool = typer.Option(False, "--print", help="只打印哈希,不写 .env"),
) -> None:
    """设置后台统一口令(**不存明文**)。

    ★ 口令用 PBKDF2-HMAC-SHA256 / 600,000 次迭代加盐哈希,写入 ``.env`` 的
      ``ADMIN_PASSWORD_HASH``。后台在**未设口令时拒绝一切登录**(安全默认)。
    """
    from hoteldata.web.auth import hash_password

    if not set_:
        _warn("用法:hoteldata admin passwd --set <新口令>")
        _echo("  · 哈希写入 .env 的 ADMIN_PASSWORD_HASH;不存明文")
        _echo("  · 后台**未设口令时拒绝一切登录**(安全默认,不是放行)")
        _echo("  · 改完**需重启** hoteldata serve(设置是进程启动时读的)")
        raise typer.Exit(code=1)

    if len(set_) < 6:
        _err("口令太短(至少 6 位)")
        raise typer.Exit(code=1)

    digest = hash_password(set_)
    if print_only:
        _echo(digest)
        raise typer.Exit(code=0)

    from hoteldata.settings import ENV_FILE

    action = _update_env_key(ENV_FILE, "ADMIN_PASSWORD_HASH", digest)
    _ok(f"后台口令已设置({action} {ENV_FILE})")
    _warn("★ 需重启 hoteldata serve 才生效")
    _echo(f"  访问 http://127.0.0.1:{get_settings().web.port}/admin")


@admin_app.command("hash")
def admin_hash(password: str = typer.Option(..., "--password", prompt=True, hide_input=True)) -> None:
    """只算哈希(不写文件),便于手工填到别处。"""
    from hoteldata.web.auth import hash_password

    _echo(hash_password(password))


# ===========================================================================
# 其它
# ===========================================================================


@app.command("env")
def env_check() -> None:
    """环境自检(Python / tzdata / Py_GIL_DISABLED / 目录 / 配置资产 / DB)。"""
    s = get_settings()
    _echo(interpreter_banner())
    try:
        from zoneinfo import ZoneInfo

        _ok(f"时区可解析: {ZoneInfo(s.tz)}")
    except Exception as exc:  # noqa: BLE001
        _err(f"时区不可解析({exc});请确认已安装 tzdata")
    _echo(f"项目根: {s.paths.project_root}")
    _echo(f"config : {s.paths.config_dir}")
    _echo(f"var    : {s.paths.var_dir}")

    required = ("api_rules.json", "push_rotation.json")
    optional = (
        "report_schedule.json",
        "alert_rules.json",
        "alert_shots.json",
        "alert_lines.json",
        "review_templates.json",
        "review_sources.json",
        "city_hierarchy.json",
    )
    missing_required = 0
    for name in required:
        p = s.paths.config_dir / name
        if p.exists():
            _ok(f"[必需] {name}: 存在")
        else:
            _err(f"[必需] {name}: 缺失 ({p})")
            missing_required += 1
    for name in optional:
        p = s.paths.config_dir / name
        (_ok if p.exists() else _warn)(f"[段2] {name}: {'存在' if p.exists() else '缺失'}")
    prompts = s.paths.config_dir / "prompts"
    n_prompts = len(list(prompts.glob("*.md"))) if prompts.exists() else 0
    (_ok if n_prompts else _warn)(f"[段2] prompts/*.md: {n_prompts} 个模板")
    faq = s.paths.project_root / "knowledge" / "faq.json"
    (_ok if faq.exists() else _warn)(f"[段2] knowledge/faq.json: {'存在' if faq.exists() else '缺失'}")

    _echo(f"配置: {s.safe_repr()}")
    _echo(
        f"段2: 机器人={s.bot.ws_url} 限频={s.push.min_interval_s}s "
        f"管理群={len(s.push.manage_chatids)}个 运维群={'有' if s.push.ops_chatid else '无'}"
    )
    if not s.push.manage_chatids:
        _warn("MANAGE_CHATIDS 为空 → 11 条管理群命令**一律拒绝**(这是有意的安全默认,不是 bug)")
    raise typer.Exit(code=0 if s.paths.config_dir.exists() and not missing_required else 1)



def run() -> None:  # pragma: no cover - poetry script 入口
    _configure_stdio()
    app()


if __name__ == "__main__":  # pragma: no cover
    _configure_stdio()
    sys.exit(app())
