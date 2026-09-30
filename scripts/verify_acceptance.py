"""段1 验收执行器 —— 逐条跑 **V1–V20** 并产出证据。

设计原则
--------
1. **能用真实产物就用真实产物**:``config/api_rules.json``(246KB 真规则)、
   ``config/push_rotation.json``(21 项真清单)、``var/states/*.json``(真登录态)、
   真 PostgreSQL(容器 ``hoteldata-pg``)。
2. **平台响应不在线时用录制样本**:``docs/参考/旧系统/诊断证据/responses/*.json``
   是旧系统抓包存下来的**真实平台响应**。用它们驱动 API 通道,可以验证
   *字段路径 / 四态 / 落库 / 原始落盘* 这一整条链路,**而不是靠 mock 造数据**。
3. **需要有效登录态才能完成的条目如实标 ``BLOCKED``**,不伪装成 PASS。

输出:控制台表格 + ``var/reports/段1-验收结果.json``。

用法::

    .venv\\Scripts\\python.exe scripts\\verify_acceptance.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hoteldata.logging import configure_stdio  # noqa: E402
from hoteldata.settings import get_settings  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESPONSES_DIR = PROJECT_ROOT / "docs" / "参考" / "旧系统" / "诊断证据" / "responses"
#: 合成数据用的采集日(远离真实日期,便于识别与清理,也不会与真实采集撞键)
SYNTH_DAY = date(2000, 1, 1)

PASS, FAIL, BLOCKED = "PASS", "FAIL", "BLOCKED"


@dataclass
class CheckResult:
    vid: str
    title: str
    status: str
    evidence: str
    detail: dict[str, Any] = field(default_factory=dict)


class Registry:
    def __init__(self) -> None:
        self.checks: list[tuple[str, str, Callable[[], Awaitable[CheckResult]]]] = []

    def add(self, vid: str, title: str):  # noqa: ANN201
        def deco(fn: Callable[[], Awaitable[CheckResult]]):  # noqa: ANN202
            self.checks.append((vid, title, fn))
            return fn

        return deco


REG = Registry()


def _res(vid: str, title: str, status: str, evidence: str, **detail: Any) -> CheckResult:
    return CheckResult(vid, title, status, evidence, detail)


# ===========================================================================
# 录制响应驱动的 HTTP 桩(用**真实平台响应**驱动 API 通道)
# ===========================================================================


class RecordedHttp:
    """按 URL 命中旧系统抓包样本的 ``HttpClient`` 替身。

    只替代"网络"这一层 —— 请求头构造、占位符守卫、字段提取、四态判定、
    原始落盘、落库,**全部走真实代码**。
    """

    def __init__(self, responses: dict[str, Any], *, force_status: int | None = None) -> None:
        self.responses = responses
        self.force_status = force_status
        self.calls: list[dict[str, Any]] = []

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        content: str | bytes | None = None,
        timeout: float | None = None,
        follow_redirects: bool = False,
        retry: Any = None,
    ) -> Any:
        from hoteldata.infra.http import HttpAttempt

        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers or {}),
                "params": params,
                "json_body": json_body,
                "content": content,
            }
        )
        if self.force_status is not None:
            return HttpAttempt(url=url, method=method, status_code=self.force_status, text="", attempts=1)
        body = self.responses.get(url)
        if body is None:
            # 未录制的 URL:按 404 处理(触发"接口失败"分支)
            return HttpAttempt(url=url, method=method, status_code=404, text="{}", attempts=1)
        text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        attempt = HttpAttempt(url=url, method=method, status_code=200, text=text, attempts=1)
        attempt.response = _FakeResponse(text)
        return attempt


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self._text = text

    def json(self) -> Any:
        return json.loads(self._text)


def load_recorded() -> dict[str, Any]:
    """把 ``responses/*.json`` 摊平成 ``{url: body}``。"""
    out: dict[str, Any] = {}
    if not RESPONSES_DIR.is_dir():
        return out
    for f in sorted(RESPONSES_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        apis = data.get("apis") if isinstance(data, dict) else None
        if isinstance(apis, dict):
            out.update(apis)
    return out


class NullLimiter:
    """不真的 sleep 的限频替身(仅验收用;**限频本身由 V16 单独实测**)。"""

    def __init__(self) -> None:
        self.count = 0

    def request(self, platform: str, account: str):  # noqa: ANN201
        outer = self

        class _Ctx:
            async def __aenter__(self) -> None:
                outer.count += 1

            async def __aexit__(self, *exc: object) -> None:
                return None

        return _Ctx()


# ===========================================================================
# 公共夹具
# ===========================================================================


async def _a_runtime(**kw: Any) -> Any:
    from hoteldata.runtime import Runtime

    return await Runtime.create(with_scheduler=False, **kw).__aenter__()


async def _first_pair(db: Any) -> tuple[Any, Any] | None:
    from sqlalchemy import select

    from hoteldata.infra.models import Account, Hotel

    async with db.session() as s:
        hotel = (await s.execute(select(Hotel).order_by(Hotel.id).limit(1))).scalar_one_or_none()
        if hotel is None:
            return None
        acc = (await s.execute(select(Account).where(Account.id == hotel.account_id))).scalar_one_or_none()
    return (hotel, acc) if acc else None


async def _cleanup_synth(db: Any, hotel_id: int) -> None:
    from sqlalchemy import delete

    from hoteldata.infra.models import CollectModule, CollectReport

    async with db.session() as s:
        await s.execute(
            delete(CollectModule).where(
                CollectModule.hotel_id == hotel_id, CollectModule.collect_date == SYNTH_DAY
            )
        )
        await s.execute(
            delete(CollectReport).where(
                CollectReport.hotel_id == hotel_id, CollectReport.collect_date == SYNTH_DAY
            )
        )


# ===========================================================================
# V1 — 环境可复现
# ===========================================================================


@REG.add("V1", "环境可复现:poetry install + alembic upgrade head 一次成功;Py_GIL_DISABLED 0/None")
async def v1() -> CheckResult:
    from hoteldata.logging import check_interpreter

    info = check_interpreter()
    checks: dict[str, Any] = {
        "python": info["version"],
        "Py_GIL_DISABLED": info["gil_disabled"],
        "interpreter_ok": info["ok"],
        "pyproject": (PROJECT_ROOT / "pyproject.toml").exists(),
        "poetry.lock": (PROJECT_ROOT / "poetry.lock").exists(),
        "alembic.ini": (PROJECT_ROOT / "alembic.ini").exists(),
        "alembic.ini_ascii_only": _is_ascii(PROJECT_ROOT / "alembic.ini"),
        "env_example": (PROJECT_ROOT / ".env.example").exists(),
        "gitignore_blocks_states": _gitignore_covers("var/states/"),
        "gitignore_blocks_edge": _gitignore_covers(".edge-profile/"),
    }
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo("Asia/Shanghai")
        checks["tzdata_ok"] = True
    except Exception as exc:  # noqa: BLE001
        checks["tzdata_ok"] = False
        checks["tzdata_error"] = str(exc)

    # alembic 可重复:当前版本 == head
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "src/hoteldata/infra/migrations"))
    script = ScriptDirectory.from_config(cfg)
    checks["alembic_head"] = script.get_current_head()

    settings = get_settings()
    from hoteldata.infra.db import Database

    db = Database(settings)
    try:
        from sqlalchemy import text

        async with db.engine.connect() as c:
            current = (await c.execute(text("select version_num from alembic_version"))).scalar()
            rows = (
                await c.execute(text("select tablename from pg_tables where schemaname='public' order by 1"))
            ).all()
        names = [r[0] for r in rows]
        # ★ 断言"11 张业务表都在",而不是"恰好 N 张" —— 后者会被临时表/手工表
        #   弄成假失败(验收自己造的表、诊断用的表都会算进计数)。
        expected = {
            "core_accounts",
            "core_hotels",
            "sessions",
            "collect_reports",
            "collect_modules",
            "ops_login_events",
            "job_runs",
            "alert_portal_columns",
            "alert_room_states",
            "review_reviews",
            "review_materials",
        }
        checks["alembic_current"] = current
        checks["tables"] = len(names)
        checks["table_names"] = names
        checks["missing_tables"] = sorted(expected - set(names))
        checks["up_to_date"] = current == checks["alembic_head"]
    finally:
        await db.dispose()

    ok = (
        checks["interpreter_ok"]
        and checks["pyproject"]
        and checks["poetry.lock"]
        and checks["alembic.ini_ascii_only"]
        and checks["tzdata_ok"]
        and checks["gitignore_blocks_states"]
        and checks["gitignore_blocks_edge"]
        and checks["up_to_date"]
        and not checks["missing_tables"]
    )
    ev = (
        f"Python {checks['python']} / GIL={checks['Py_GIL_DISABLED']} / "
        f"alembic {checks['alembic_current']}==head / {checks['tables']} 张表"
        f"(11 张业务表齐全={not checks['missing_tables']}) / "
        f"alembic.ini 纯 ASCII={checks['alembic.ini_ascii_only']}"
    )
    return _res("V1", v1.__doc__ or "", PASS if ok else FAIL, ev, **checks)


def _is_ascii(path: Path) -> bool:
    try:
        path.read_bytes().decode("ascii")
        return True
    except UnicodeDecodeError, OSError:
        return False


def _gitignore_covers(pattern: str) -> bool:
    p = PROJECT_ROOT / ".gitignore"
    if not p.exists():
        return False
    return any(line.strip() == pattern for line in p.read_text(encoding="utf-8").splitlines())


# ===========================================================================
# V2 — 单进程起服务
# ===========================================================================


@REG.add("V2", "单进程起服务:/healthz ok;/status 返回调度器状态")
async def v2() -> CheckResult:
    import httpx

    from hoteldata.main import app

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            h = await c.get("/healthz")
            s = await c.get("/status")
    hb = h.json()
    sb = s.json()
    ok = (
        h.status_code == 200
        and hb.get("status") == "ok"
        and hb.get("db", {}).get("ok") is True
        and s.status_code == 200
        and "scheduler" in sb
        and sb.get("scheduler", {}).get("running") is True
        and sb.get("tasks_registered", 0) >= 9
    )
    ev = (
        f"/healthz {h.status_code} db.ok={hb.get('db', {}).get('ok')};"
        f"/status scheduler.running={sb.get('scheduler', {}).get('running')} "
        f"jobs={sb.get('scheduler', {}).get('jobs')} tasks_registered={sb.get('tasks_registered')} "
        f"job_runs_today={sb.get('job_runs_today')}"
    )
    return _res("V2", v2.__doc__ or "", PASS if ok else FAIL, ev, healthz=hb, status_body=sb)


# ===========================================================================
# V3 — 登录可用
# ===========================================================================


@REG.add("V3", "登录可用:login 弹浏览器存登录态;sessions 显示 valid;双平台会话冒烟")
async def v3() -> CheckResult:
    from hoteldata.infra.db import Database
    from hoteldata.infra.session_store import SessionStore

    settings = get_settings()
    db = Database(settings)
    try:
        store = SessionStore(settings, db)
        await store.sync_files()
        rows = await store.list_sessions()
        listing = [
            {
                "key": f"{r.platform}/{r.role}/{r.alias}",
                "status": r.status,
                "exists": store.handle(r.platform, r.role, r.alias).exists(),
            }
            for r in rows
        ]
        # 实时探活(会真的打到平台)
        from hoteldata.domains.session.manager import LoginManager

        mgr = LoginManager(settings, sessions=store)
        handle = store.handle("ctrip", "ebooking", "ctrip001")
        probe_ok = await mgr.probe_login_valid(handle)
        states = {r["key"]: r["status"] for r in listing}
        any_valid = any(v == "valid" for v in states.values())
        # ★ 判定以**实时探活**为准:表里的 status 是上一次探测的缓存,
        #   可能被别的检查(如 V17 的打桩巡检)写过。缓存不该决定验收结论。
        if any_valid:
            await mgr.check(handle, probe=True)  # 顺手把缓存刷新成真实值
        detail = {
            "sessions": listing,
            "probe_ctrip001": probe_ok,
            "table_status_ctrip001": states.get("ctrip/ebooking/ctrip001"),
            "probe_candidates": [c.name for c in mgr.probe_candidates("ctrip")],
            "cli_login_exists": _cli_has_command("login"),
            "cli_sessions_exists": _cli_has_command("sessions"),
        }
        if probe_ok:
            return _res(
                "V3",
                v3.__doc__ or "",
                PASS,
                f"{len(listing)} 个登录态;ctrip001 **实时探活通过**"
                f"(表内状态={states.get('ctrip/ebooking/ctrip001')})",
                **detail,
            )
        # 机制已验证但凭据过期 → BLOCKED(不伪装 PASS)
        return _res(
            "V3",
            v3.__doc__ or "",
            BLOCKED,
            f"机制已验证(登录态按三元组寻址落盘、探活已真实打到平台、"
            f"多候选+请求头+正文三处判定均生效),但**当前没有有效登录态** → "
            f"端到端待人工登录。当前 {len(listing)} 个登录态。",
            **detail,
        )
    finally:
        await db.dispose()


def _cli_has_command(name: str) -> bool:
    from hoteldata.cli import app as cli_app

    return any(getattr(c, "name", None) == name for c in cli_app.registered_commands)


# ===========================================================================
# V4 — 规则可加载 + 路径定位
# ===========================================================================


@REG.add("V4", "规则可加载:rules check 通过;篡改结构报错并带路径定位")
async def v4() -> CheckResult:
    from hoteldata.domains.collect.rules import ApiRulesError, RulesLoader, get_api_rules

    rules = get_api_rules(force=True)
    stats = rules.stats()

    # 篡改:把某个子模块的窗口改成非法值
    src = rules.path
    raw = json.loads(src.read_text(encoding="utf-8"))
    page_name = next(iter(raw["pages"]))
    raw["pages"][page_name]["sub_modules"][0]["windows"] = ["昨日", "昨天昨天"]

    tmpdir = Path(tempfile.mkdtemp(prefix="hoteldata_rules_"))
    broken = tmpdir / "api_rules.json"
    broken.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    error_text = ""
    located = False
    try:
        RulesLoader(broken).load(force=True)
    except ApiRulesError as exc:
        error_text = str(exc)
        # ★ 计划书要求的定位形态是 pages.经营报告.sub_modules[3].windows[1]:
        #   字段级校验器只能给到 ...windows,下标由消息正文补出 —— 两者都要在。
        located = f"pages.{page_name}.sub_modules[0].windows" in error_text and "windows[1]" in error_text
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    # 扩展键拼错必须报错(D12)
    raw2 = json.loads(src.read_text(encoding="utf-8"))
    for _pname, pcfg in raw2["pages"].items():
        for m in pcfg.get("sub_modules") or []:
            if m.get("screenshot_modules"):
                m["screenshot_modules"][0]["clickz"] = ["typo"]  # 拼错
                break
        else:
            continue
        break
    tmpdir2 = Path(tempfile.mkdtemp(prefix="hoteldata_rules2_"))
    broken2 = tmpdir2 / "api_rules.json"
    broken2.write_text(json.dumps(raw2, ensure_ascii=False), encoding="utf-8")
    typo_error = ""
    try:
        RulesLoader(broken2).load(force=True)
    except ApiRulesError as exc:
        typo_error = str(exc)
    finally:
        shutil.rmtree(tmpdir2, ignore_errors=True)
    typo_caught = "clickz" in typo_error and "Extra inputs" in typo_error

    ok = (
        stats["pages"] == 8
        and stats["sub_modules"] == 24
        and stats["api_defs"] == 103
        and located
        and typo_caught
    )
    ev = (
        f"{stats['pages']} 页/{stats['sub_modules']} 子模块/{stats['api_defs']} 接口定义/"
        f"{stats['fields']} 字段;非法窗口报错含路径定位={located};"
        f"拼错扩展键被拦={typo_caught}"
    )
    return _res(
        "V4",
        v4.__doc__ or "",
        PASS if ok else FAIL,
        ev,
        stats=stats,
        error_sample=error_text[:400],
        typo_error_sample=typo_error[:300],
    )


# ===========================================================================
# V5 — API 直连取数
# ===========================================================================


@REG.add("V5", "API 直连取数:collect 把模块取进 PG,channel='api'")
async def v5() -> CheckResult:
    from hoteldata.domains.collect.api import ApiChannel
    from hoteldata.domains.collect.repository import CollectRepository
    from hoteldata.infra.db import Database
    from hoteldata.infra.paths import get_layout
    from hoteldata.infra.session_store import SessionStore

    settings = get_settings()
    db = Database(settings)
    real: dict[str, Any] = {"attempted": False}
    try:
        pair = await _first_pair(db)
        if pair is None:
            return _res("V5", v5.__doc__ or "", BLOCKED, "core_hotels/core_accounts 为空")
        hotel, acc = pair
        from hoteldata.runtime import Runtime

        rec = load_recorded()
        async with Runtime.create(with_scheduler=False) as rt:
            ctx = rt.extract_context(hotel, acc, SYNTH_DAY)
            ctx = ctx.model_copy(update={"http": RecordedHttp(rec), "limiter": NullLimiter()})
            chan = ApiChannel(ctx=ctx, rules=rt.rules)

            # 逐模块跑「离店 / 预订销售数据 / 流量数据概况」的「昨日」
            results = []
            for mod in ("离店", "预订销售数据", "流量数据概况"):
                sub = rt.rules.sub_module("经营报告", mod)
                try:
                    r = await chan.collect_sub_module("经营报告", rt.rules.page("经营报告"), sub, "昨日")
                except Exception as exc:  # noqa: BLE001
                    r = None
                    results.append({"module": mod, "raised": f"{type(exc).__name__}: {exc}"})
                if r is not None:
                    async with db.session() as s:
                        await CollectRepository(s).upsert_module(
                            hotel_id=hotel.id,
                            account_id=acc.id,
                            collect_date=SYNTH_DAY,
                            page="经营报告",
                            module=mod,
                            window="昨日",
                            payload=r.payload or {},
                            raw_json_path=r.raw_path,
                            channel=r.channel,
                            status=r.status,
                            error=r.error,
                        )
                    results.append(
                        {
                            "module": mod,
                            "status": r.status,
                            "channel": r.channel,
                            "indicators": r.record_count,
                            "raw_path": r.raw_path,
                            "labels_sample": list((r.payload or {}).keys())[:4],
                        }
                    )
        async with db.session() as s:
            rows = await CollectRepository(s).list_modules(hotel.id, SYNTH_DAY, page="经营报告")
        db_rows = [
            {
                "module": x.module,
                "channel": x.channel,
                "status": x.status,
                "indicators": len(x.payload_json or {}),
                "raw": x.raw_json_path,
            }
            for x in rows
        ]
        # 原始落盘存在且是相对路径
        layout = get_layout(settings)
        rel_ok = all(
            (not r["raw"]) or (not Path(r["raw"]).is_absolute() and layout.from_relative(r["raw"]).exists())
            for r in db_rows
        )
        api_rows = [r for r in db_rows if r["channel"] == "api" and r["indicators"] > 0]
        ok = bool(api_rows) and rel_ok
        ev = (
            f"用**真实录制响应**驱动 API 通道:{len(results)} 个模块 → 落库 {len(db_rows)} 行,"
            f"其中 channel='api' 且有指标 {len(api_rows)} 行;原始落盘为相对路径且文件存在={rel_ok}"
        )
        if not ok:
            ev += " —— 需要有效登录态才能对真平台跑通(见 V3)"

        # ★★ 有有效登录态时,**真的打一次真平台** —— 这才叫端到端。
        try:
            from hoteldata.domains.session.manager import LoginManager

            st = SessionStore(settings, db)
            probe_handle = st.handle("ctrip", "ebooking", acc.alias)
            if probe_handle.exists() and await LoginManager(settings, sessions=st).probe_login_valid(
                probe_handle
            ):
                real["attempted"] = True
                from hoteldata.domains.collect.datacenter import DatacenterExtractor

                async with Runtime.create(with_scheduler=False, with_browser=True) as rt2:
                    # ★ 必须用**真实日期**:合成日期(2000-01-01)平台没有数据,
                    #   会把"那天没数据"误读成"通道不通"。
                    real_day = date.today()
                    ctx2 = rt2.extract_context(hotel, acc, real_day)
                    ex = DatacenterExtractor(rt2.rules, pool=rt2.browser, ensure_login=rt2.ensure_login)
                    async with rt2.db.session() as s2:
                        repo2 = CollectRepository(s2)
                        real_results = await ex.extract(
                            ctx2,
                            page="经营报告",
                            module="离店",
                            window="昨日",
                            repo=repo2,
                        )
                    real["day"] = real_day.isoformat()
                    real["rows"] = [
                        {
                            "status": r.status,
                            "channel": r.channel,
                            "indicators": r.record_count,
                            "labels": list((r.payload or {}).keys())[:6],
                            "raw": r.raw_path,
                        }
                        for r in real_results
                    ]
                    real_ok = any(r.status in ("ok", "degraded") and r.record_count > 0 for r in real_results)
                    real["ok"] = real_ok
                    ok = ok and real_ok
                    ev += (
                        f" | ★ 真平台端到端:离店/昨日 → {real['rows'][0]['status']} "
                        f"{real['rows'][0]['channel']} {real['rows'][0]['indicators']} 指标 "
                        f"{real['rows'][0]['labels']}"
                    )
        except Exception as exc:  # noqa: BLE001
            real["error"] = f"{type(exc).__name__}: {exc}"
            ev += f" | 真平台端到端未跑成({real['error']})"
        return _res(
            "V5",
            v5.__doc__ or "",
            PASS if ok else FAIL,
            ev,
            module_results=results,
            db_rows=db_rows,
            real_platform=real,
        )
    finally:
        try:
            pair = await _first_pair(db)
            if pair:
                await _cleanup_synth(db, pair[0].id)
        finally:
            await db.dispose()


# ===========================================================================
# V6 — 浏览器兜底生效
# ===========================================================================


@REG.add("V6", "浏览器兜底生效:置 need_record=true 后走浏览器,channel='browser',四态=degraded")
async def v6() -> CheckResult:
    """★ 用**被篡改的规则副本**造出 ``need_record=true``,不碰真规则文件。"""
    from hoteldata.domains.collect.api import ApiChannel
    from hoteldata.domains.collect.channels import ChannelOrchestrator
    from hoteldata.domains.collect.rules import RulesLoader
    from hoteldata.infra.db import Database

    settings = get_settings()
    db = Database(settings)
    try:
        pair = await _first_pair(db)
        if pair is None:
            return _res("V6", v6.__doc__ or "", BLOCKED, "无酒店/账号")
        hotel, acc = pair

        # 造一份 need_record=true 的规则副本
        src = settings.paths.config_dir / "api_rules.json"
        raw = json.loads(src.read_text(encoding="utf-8"))
        page_cfg = raw["pages"]["经营报告"]
        for d in page_cfg["api_defs"]:
            if "queryMarketDetails" in d["name"]:
                d["need_record"] = True
        tmpdir = Path(tempfile.mkdtemp(prefix="hoteldata_v6_"))
        tmp = tmpdir / "api_rules.json"
        tmp.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        rules2 = RulesLoader(tmp).load(force=True)

        calls: list[str] = []

        class _StubBrowser:
            async def collect_sub_module(self, page_name, page_cfg2, sub, window, **kw):  # noqa: ANN001
                from hoteldata.domains.collect.contract import ExtractResult, ExtractTarget

                calls.append(f"{page_name}/{sub.name}/{window}")
                return ExtractResult(
                    status="degraded",
                    channel="browser",
                    payload={"浏览器兜底(桩)": 1},
                    error="API 通道失败,浏览器兜底(DOM 提取)",
                    target=ExtractTarget(page=page_name, module=sub.name, window=window),
                    detail={"stub": True},
                )

        from hoteldata.runtime import Runtime

        async with Runtime.create(with_scheduler=False) as rt:
            ctx = rt.extract_context(hotel, acc, SYNTH_DAY)
            ctx = ctx.model_copy(update={"http": RecordedHttp(load_recorded()), "limiter": NullLimiter()})
            orch = ChannelOrchestrator(
                ctx=ctx,
                rules=rules2,
                api=ApiChannel(ctx=ctx, rules=rules2),
                browser=_StubBrowser(),
                allow_browser=True,
            )
            sub = rules2.sub_module("经营报告", "离店")
            outcome = await orch.extract("经营报告", sub, "昨日")
        res = outcome.result
        ok = (
            res.channel == "browser"
            and res.status == "degraded"
            and outcome.degraded_to_browser
            and "need_record" in (outcome.api_error or "").lower()
            and calls == ["经营报告/离店/昨日"]
        )
        ev = (
            f"need_record=true → API 抛 {outcome.api_exception} → 无条件降级浏览器 → "
            f"channel={res.channel} status={res.status};api_error={outcome.api_error!r}"
        )
        return _res(
            "V6",
            v6.__doc__ or "",
            PASS if ok else FAIL,
            ev,
            api_exception=outcome.api_exception,
            result=res.summary(),
        )
    finally:
        await db.dispose()


# ===========================================================================
# V7 — 占位符守卫
# ===========================================================================


@REG.add("V7", "占位符守卫生效:删窗口上下文→抛错降级,绝不发带 {xxx} 的请求")
async def v7() -> CheckResult:
    from hoteldata.domains.collect.api import ApiChannel, PlaceholderError
    from hoteldata.infra.db import Database

    settings = get_settings()
    db = Database(settings)
    try:
        pair = await _first_pair(db)
        if pair is None:
            return _res("V7", v7.__doc__ or "", BLOCKED, "无酒店/账号")
        hotel, acc = pair
        from hoteldata.runtime import Runtime

        rec = load_recorded()
        async with Runtime.create(with_scheduler=False) as rt:
            # 找一个模板里含 {startDate}/{endDate} 的 def
            target = None
            for _pname, page in rt.rules.pages.items():
                for d in page.api_defs:
                    blob = json.dumps({"u": d.url, "p": d.params, "b": d.body}, ensure_ascii=False)
                    if "{startDate}" in blob or "{endDate}" in blob:
                        target = (_pname, d)
                        break
                if target:
                    break
            if target is None:
                return _res("V7", v7.__doc__ or "", BLOCKED, "规则中找不到含日期占位符的接口")
            pname, api_def = target

            stub = RecordedHttp(rec)
            ctx = rt.extract_context(hotel, acc, SYNTH_DAY)
            ctx = ctx.model_copy(update={"http": stub, "limiter": NullLimiter()})
            chan = ApiChannel(ctx=ctx, rules=rt.rules)

            # ① 正常上下文 → 不抛
            normal_ctx = chan._merge_window_ctx("昨日")
            normal_ok = True
            try:
                chan.guard_placeholders(api_def, normal_ctx)
            except PlaceholderError:
                normal_ok = False

            # ② 删掉日期上下文(模拟"无窗口上下文") → 必须抛
            broken = {k: v for k, v in normal_ctx.items() if k not in ("startDate", "endDate")}
            raised = ""
            try:
                chan.guard_placeholders(api_def, broken)
            except PlaceholderError as exc:
                raised = str(exc)

            # ③ 未知占位符 → 必须抛
            unknown = dict(normal_ctx)
            raised_unknown = ""
            fake = api_def.model_copy(update={"url": api_def.url + "?x={nonexistent_zz}"})
            try:
                chan.guard_placeholders(fake, unknown)
            except PlaceholderError as exc:
                raised_unknown = str(exc)

            # ④ ★ 关键:守卫失败时**一次请求都没发出去**
            before = len(stub.calls)
            try:
                await chan.request_api(fake, unknown, page_name=pname, module="guard-probe", window="昨日")
            except PlaceholderError:
                pass
            no_request = len(stub.calls) == before

        ok = normal_ok and bool(raised) and bool(raised_unknown) and no_request
        ev = (
            f"正常上下文通过={normal_ok};缺 startDate/endDate → 抛错({raised[:60]!r});"
            f"未知占位符 → 抛错({raised_unknown[:60]!r});"
            f"守卫失败期间发出请求数={len(stub.calls) - before}(必须为 0)"
        )
        return _res(
            "V7",
            v7.__doc__ or "",
            PASS if ok else FAIL,
            ev,
            probe_api=api_def.name,
            raised=raised,
            raised_unknown=raised_unknown,
        )
    finally:
        await db.dispose()


# ===========================================================================
# V8 — 四态正确
# ===========================================================================


@REG.add("V8", "四态正确:no_data 不计失败;真异常落 failed;退出码正确")
async def v8() -> CheckResult:
    from hoteldata.domains.collect.api import ApiChannel
    from hoteldata.domains.collect.channels import ChannelOrchestrator, aggregate_results
    from hoteldata.domains.collect.contract import ExtractResult, worst_status
    from hoteldata.infra.db import Database

    settings = get_settings()
    db = Database(settings)
    try:
        pair = await _first_pair(db)
        if pair is None:
            return _res("V8", v8.__doc__ or "", BLOCKED, "无酒店/账号")
        hotel, acc = pair
        from hoteldata.runtime import Runtime

        async with Runtime.create(with_scheduler=False) as rt:
            base = rt.extract_context(hotel, acc, SYNTH_DAY)
            page = rt.rules.page("经营报告")
            sub = rt.rules.sub_module("经营报告", "离店")

            # ① ok / degraded / no_data:同一模块换不同 HTTP 桩
            observed: dict[str, Any] = {}
            rec = load_recorded()
            good = base.model_copy(update={"http": RecordedHttp(rec), "limiter": NullLimiter()})
            r_good = await ApiChannel(ctx=good, rules=rt.rules).collect_sub_module(
                "经营报告", page, sub, "昨日"
            )
            observed["with_recording"] = {"status": r_good.status, "indicators": r_good.record_count}

            # 全 200 但所有响应体都是 {} → 旧口径判 no_data
            empty = base.model_copy(
                update={
                    "http": RecordedHttp({}, force_status=200),
                    "limiter": NullLimiter(),
                }
            )
            r_nodata = None
            try:
                r_nodata = await ApiChannel(ctx=empty, rules=rt.rules).collect_sub_module(
                    "经营报告", page, sub, "昨日"
                )
            except Exception as exc:  # noqa: BLE001
                observed["no_data_raised"] = f"{type(exc).__name__}: {exc}"
            observed["no_data"] = (
                {"status": r_nodata.status, "payload": r_nodata.payload} if r_nodata else None
            )

            # ② failed 只由浏览器兜底抛异常产生
            class _BoomBrowser:
                async def collect_sub_module(self, *a: Any, **k: Any) -> Any:
                    from hoteldata.domains.collect.contract import ExtractResult

                    return ExtractResult(status="failed", channel="browser", payload={}, error="boom")

            class _BoomApi:
                async def collect_sub_module(self, *a: Any, **k: Any) -> Any:
                    raise RuntimeError("api exploded")

            orch = ChannelOrchestrator(
                ctx=base, rules=rt.rules, api=_BoomApi(), browser=_BoomBrowser(), allow_browser=True
            )
            r_failed = (await orch.extract("经营报告", sub, "昨日")).result

        # ③ 顶层汇总:no_data 不降级
        agg = aggregate_results(
            [
                ExtractResult(status="ok", channel="api", payload={"a": 1}),
                ExtractResult(status="no_data", channel="api", payload={}),
                ExtractResult(status="degraded", channel="browser", payload={"b": 1}),
            ]
        )
        agg_only_no_data = aggregate_results(
            [
                ExtractResult(status="ok", channel="api", payload={"a": 1}),
                ExtractResult(status="no_data", channel="api", payload={}),
            ]
        )
        exit_failed = 1 if agg["failed"] else 0
        exit_only_no_data = 1 if agg_only_no_data["failed"] else 0

        ok = (
            r_failed.status == "failed"
            and r_failed.channel == "browser"
            and agg["status"] == "degraded"
            and agg_only_no_data["status"] == "ok"
            and exit_only_no_data == 0
            and exit_failed == 0  # degraded 不算失败 → 退出码 0
            and worst_status(["no_data"]) == "ok"
        )
        ev = (
            f"failed 仅由浏览器兜底异常产生={r_failed.status} ;"
            f"汇聚(ok+no_data+degraded)→{agg['status']} 退出码={exit_failed};"
            f"汇聚(ok+no_data)→{agg_only_no_data['status']} 退出码={exit_only_no_data}(no_data 不计失败)"
        )
        return _res("V8", v8.__doc__ or "", PASS if ok else FAIL, ev, observed=observed, aggregate=agg)
    finally:
        await db.dispose()


# ===========================================================================
# V9 — 轮换正确
# ===========================================================================


@REG.add("V9", "轮换正确:连续 21 天无重无漏;fixed_daily 不进轮换;清单不一致报错")
async def v9() -> CheckResult:
    from hoteldata.domains.collect.rotation import RotationError, RotationRegistry

    settings = get_settings()
    reg = RotationRegistry(settings.paths.config_dir / "push_rotation.json")
    result = reg.verify_no_gap(date(2026, 9, 30), 21)

    # 清单加一个不存在的模块名 → 必须报错
    raw = json.loads((settings.paths.config_dir / "push_rotation.json").read_text(encoding="utf-8"))
    raw["items"].append(
        {"name": "不存在的模块ZZZ", "page": "经营报告", "type": "module_screenshot", "alias": []}
    )
    tmpdir = Path(tempfile.mkdtemp(prefix="hoteldata_v9_"))
    bad = tmpdir / "push_rotation.json"
    bad.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    raised = ""
    try:
        RotationRegistry(bad).validate_against_rules()
    except RotationError as exc:
        raised = str(exc)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    fixed = reg.fixed_daily_modules()
    notes = reg.validate_against_rules()

    ok = (
        result["ok"]
        and result["per_day_counts"] == [5] * 21
        and not result["missing"]
        and not result["dup_within_day"]
        and "不存在的模块ZZZ" in raised
        and len(fixed) == 7
        and all("/" not in f for f in fixed)  # 是模块名不是页名
        and len(notes) == 2  # 市场分析 / 预警-热点日历 两项有意的例外
    )
    ev = (
        f"21 天每日 {set(result['per_day_counts'])} 项、覆盖 {len(result['coverage'])} 项各 "
        f"{result['expected_each']} 次、无重无漏={result['ok']};"
        f"fixed_daily {len(fixed)} 项(模块名):{fixed};"
        f"清单加不存在的模块 → 报错={'不存在的模块ZZZ' in raised};有意的例外 {len(notes)} 条"
    )
    return _res(
        "V9",
        v9.__doc__ or "",
        PASS if ok else FAIL,
        ev,
        verify=result,
        fixed_daily=fixed,
        notes=notes,
        raise_sample=raised[:300],
    )


# ===========================================================================
# V10 — 截图回填 COALESCE
# ===========================================================================


@REG.add("V10", "截图回填:出图并回填 module_screenshots_json;重跑不覆盖旧值(COALESCE)")
async def v10() -> CheckResult:
    from hoteldata.domains.collect.repository import CollectRepository
    from hoteldata.infra.db import Database
    from hoteldata.infra.models import CollectReport

    settings = get_settings()
    db = Database(settings)
    try:
        pair = await _first_pair(db)
        if pair is None:
            return _res("V10", v10.__doc__ or "", BLOCKED, "无酒店/账号")
        hotel, acc = pair
        page = "经营报告"
        async with db.session() as s:
            repo = CollectRepository(s)
            rid = await repo.ensure_report(hotel.id, SYNTH_DAY, page, channel="screenshot")
            await repo.link_screenshot(
                hotel.id,
                SYNTH_DAY,
                page,
                screenshot_path=None,
                module_screenshots={"离店": "var/screenshots/x/离店.jpg"},
            )
            after_first = await repo.module_screenshots(hotel.id, SYNTH_DAY, page)
            # 重跑:传空 dict(= None)→ 必须保留旧值
            await repo.link_screenshot(hotel.id, SYNTH_DAY, page, screenshot_path=None, module_screenshots={})
            after_empty = await repo.module_screenshots(hotel.id, SYNTH_DAY, page)
            # 再传新值 → 覆盖
            await repo.link_screenshot(
                hotel.id,
                SYNTH_DAY,
                page,
                screenshot_path=None,
                module_screenshots={"问题房型": "var/screenshots/x/问题房型.jpg"},
            )
            after_new = await repo.module_screenshots(hotel.id, SYNTH_DAY, page)
            row = await s.get(CollectReport, rid)
            channel = row.channel if row else None
        # 幂等建行不重复
        async with db.session() as s:
            r2 = await CollectRepository(s).ensure_report(hotel.id, SYNTH_DAY, page)
        ok = (
            rid > 0
            and r2 == rid
            and channel == "screenshot"
            and after_first.get("离店", "").endswith("离店.jpg")
            and after_empty == after_first  # ★ COALESCE 保留旧值
            and "问题房型" in after_new
        )
        ev = (
            f"幂等建行 id={rid}(重入同 id={r2},channel={channel});"
            f"首次回填={after_first};空值重跑后={after_empty}(**保留旧值**);"
            f"传新值后={after_new}"
        )
        return _res(
            "V10",
            v10.__doc__ or "",
            PASS if ok else FAIL,
            ev,
            after_first=after_first,
            after_empty=after_empty,
            after_new=after_new,
        )
    finally:
        try:
            pair = await _first_pair(db)
            if pair:
                await _cleanup_synth(db, pair[0].id)
        finally:
            await db.dispose()


# ===========================================================================
# V11 — 原始可追溯
# ===========================================================================


@REG.add("V11", "原始可追溯:var/raw 下有文件;raw_json_path 入库为相对路径")
async def v11() -> CheckResult:
    from hoteldata.domains.collect.repository import CollectRepository
    from hoteldata.infra.db import Database
    from hoteldata.infra.paths import safe_name

    settings = get_settings()
    db = Database(settings)
    layout = __import__("hoteldata.infra.paths", fromlist=["get_layout"]).get_layout(settings)
    try:
        pair = await _first_pair(db)
        if pair is None:
            return _res("V11", v11.__doc__ or "", BLOCKED, "无酒店/账号")
        hotel, acc = pair
        async with db.session() as s:
            await CollectRepository(s).upsert_module(
                hotel_id=hotel.id,
                account_id=acc.id,
                collect_date=SYNTH_DAY,
                page="经营报告",
                module="离店",
                window="昨日",
                payload={"a": 1},
                raw_json_path="var/raw/demo/api/x.json",
                channel="api",
                status="ok",
            )
            rows = await CollectRepository(s).list_modules(hotel.id, SYNTH_DAY)
        raw_ok = all((not r.raw_json_path) or not Path(r.raw_json_path).is_absolute() for r in rows)
        # 落盘真实文件 + 相对路径往返
        p = layout.raw_json_path(hotel.name, SYNTH_DAY, "经营报告", "离店", "昨日")
        p.parent.mkdir(parents=True, exist_ok=True)
        from hoteldata.infra.atomic import atomic_write_json

        atomic_write_json(p, {"demo": True})
        rel = layout.to_relative(p)
        back = layout.from_relative(rel)
        roundtrip = back.exists() and not Path(rel).is_absolute()
        # 文件名安全化
        unsafe = safe_name('a/b\\c:d*e?f"g<h>i|j')
        ok = raw_ok and roundtrip and "/" not in unsafe and "\\" not in unsafe and ":" not in unsafe
        ev = (
            f"入库路径全为相对路径={raw_ok};落盘 {rel} 并反解存在={roundtrip};"
            f"非法字符安全化 {'a/b\\\\c:d*e?f"g<h>i|j'!r} → {unsafe!r}"
        )
        return _res(
            "V11",
            v11.__doc__ or "",
            PASS if ok else FAIL,
            ev,
            rel=rel,
            roundtrip=roundtrip,
            safe=unsafe,
            rows=len(rows),
        )
    finally:
        try:
            pair = await _first_pair(db)
            if pair:
                await _cleanup_synth(db, pair[0].id)
        finally:
            await db.dispose()


# ===========================================================================
# V12 — 任务可观测
# ===========================================================================


@REG.add("V12", "任务可观测:task ls --today 显示状态/耗时/结果摘要")
async def v12() -> CheckResult:
    from sqlalchemy import select

    import hoteldata.jobs  # noqa: F401
    from hoteldata.infra.db import Database
    from hoteldata.infra.models import JobRun
    from hoteldata.infra.tasks import get_registry
    from hoteldata.runtime import Runtime

    settings = get_settings()
    reg = get_registry()

    # 真跑一个无害任务:ops.selfcheck(只读聚合)
    db = Database(settings)
    await db.dispose()
    async with Runtime.create(with_scheduler=False) as rt:
        res = await rt.tasks.run("ops.selfcheck", rt, trigger="manual")
        async with rt.db.session() as s:
            row = (await s.execute(select(JobRun).where(JobRun.id == res.job_run_id))).scalar_one_or_none()
            recent = (await s.execute(select(JobRun).order_by(JobRun.id.desc()).limit(5))).scalars().all()

    fields_ok = bool(
        row
        and row.status == "ok"
        and row.started_at is not None
        and row.finished_at is not None
        and row.summary is not None
    )
    # ★ 段2 更新:原来断言 ``len(reg.names()) == 9``。
    #   段2(§5.9)新增 10 个任务(push.* / alert.* / review.* / ops.violation),
    #   并把 ``ops.selfcheck`` 的 catch_up 由 True 改成 False —— 于是"恰好 9 个"必然失败。
    #   这里改成断言**段1 的 9 个任务仍然全部在册**(superset 不变式),
    #   既不放松本条的意图(注册表可用、可观测),也不再被任务数增长误伤。
    segment1_tasks = {
        "collect.modules",
        "collect.screenshots",
        "collect.portal",
        "collect.room",
        "collect.review",
        "ops.patrol",
        "ops.backup",
        "ops.cleanup",
        "ops.selfcheck",
    }
    missing = sorted(segment1_tasks - set(reg.names()))
    ok = (
        not missing
        and fields_ok
        and res.status == "ok"
        and row is not None
        and row.duration_ms is not None
        and row.duration_ms >= 0
    )
    ev = (
        f"注册 {len(reg.names())} 个任务(段1 的 9 个全在={not missing});手动跑 ops.selfcheck → status={res.status} "
        f"耗时={round(row.duration_ms) if row and row.duration_ms else 0}ms "
        f"summary 键={list(row.summary or {})[:6] if row else None};"
        f"最近 {len(recent)} 条 job_runs 可读"
    )
    return _res(
        "V12",
        v12.__doc__ or "",
        PASS if ok else FAIL,
        ev,
        tasks=reg.names(),
        missing_segment1=missing,
        run=res.as_dict(),
    )



# ===========================================================================
# V13 — 幂等
# ===========================================================================


@REG.add("V13", "幂等:同日重复跑 collect,collect_modules 行数不增(UPSERT 覆盖)")
async def v13() -> CheckResult:
    from hoteldata.domains.collect.repository import CollectRepository
    from hoteldata.infra.db import Database

    settings = get_settings()
    db = Database(settings)
    try:
        pair = await _first_pair(db)
        if pair is None:
            return _res("V13", v13.__doc__ or "", BLOCKED, "无酒店/账号")
        hotel, acc = pair
        async with db.session() as s:
            repo = CollectRepository(s)
            for i in range(3):
                await repo.upsert_module(
                    hotel_id=hotel.id,
                    account_id=acc.id,
                    collect_date=SYNTH_DAY,
                    page="经营报告",
                    module="离店",
                    window="昨日",
                    payload={"离店间夜": i},
                    channel="api",
                    status="ok",
                )
            n1 = await repo.count_modules(hotel.id, SYNTH_DAY)
            row = await repo.fetch_module(hotel.id, SYNTH_DAY, "经营报告", "离店", "昨日")
            value = (row.payload_json or {}).get("离店间夜") if row else None
            # 不同窗口应各占一行
            await repo.upsert_module(
                hotel_id=hotel.id,
                account_id=acc.id,
                collect_date=SYNTH_DAY,
                page="经营报告",
                module="离店",
                window="上周",
                payload={"离店间夜": 9},
                channel="api",
                status="ok",
            )
            n2 = await repo.count_modules(hotel.id, SYNTH_DAY)
        ok = n1 == 1 and value == 2 and n2 == 2
        ev = f"同键写 3 次 → 行数={n1}(必须 1),最终值={value}(必须=最后一次 2);换窗口后行数={n2}(必须 2)"
        return _res(
            "V13",
            v13.__doc__ or "",
            PASS if ok else FAIL,
            ev,
            rows_same_key=n1,
            final_value=value,
            rows_two_windows=n2,
        )
    finally:
        try:
            pair = await _first_pair(db)
            if pair:
                await _cleanup_synth(db, pair[0].id)
        finally:
            await db.dispose()


# ===========================================================================
# V14 — 补跑正确
# ===========================================================================


def _catchup_probe_task() -> str:
    """注册一个**验收专用的补跑探针任务**(幂等)。

    ★ 段2 更新:原来 V14 直接拿 ``ops.selfcheck`` 当探针。段2 §5.9 明确把
    ``ops.selfcheck`` 的 ``catch_up`` 定为 **False**(汇总类不补跑),于是"用一个
    真实任务当探针"就不再可行 —— 而且拿生产任务当探针本身也脆(它的 cron /
    max_delay 一改,V14 就跟着红)。

    本函数注册一个 cron ``0 6 * * *`` / ``catch_up=True`` / ``max_delay=4h``
    的**空跑任务**,只验"补跑算法"这一条不变式,与业务任务解耦。
    """
    from hoteldata.infra.tasks import get_registry, task

    name = "verify2.catchup"
    if name in get_registry().names():
        return name

    @task(
        name,
        cron="0 6 * * *",
        catch_up=True,
        max_delay="4h",
        domain="verify",
        description="验收专用补跑探针(空跑,不碰任何业务)",
    )
    async def _probe(runtime: Any, **_: Any) -> dict[str, Any]:
        return {"probe": True}

    return name


@REG.add("V14", "补跑正确:跨过触发时刻重启→补跑;超 max_delay 记 skipped 不跑")
async def v14() -> CheckResult:
    from sqlalchemy import delete, or_, select

    import hoteldata.jobs  # noqa: F401
    from hoteldata.infra.models import JobRun
    from hoteldata.infra.tasks import get_registry
    from hoteldata.runtime import Runtime

    settings = get_settings()
    tz = settings.tzinfo
    reg = get_registry()
    probe = _catchup_probe_task()
    spec = reg.get(probe)
    today = date.today()
    fires = spec.fire_times(today, tz)

    # ① 反推应跑时刻
    derive_ok = len(fires) == 1 and fires[0].hour == 6 and fires[0].minute == 0

    async with Runtime.create(with_scheduler=False) as rt:
        # 制造"缺失":把该任务今天的 job_runs 全删掉
        async with rt.db.session() as s:
            # ★ skipped 行的 started_at 是 NULL,只按 started_at 过滤会漏删;
            #   残留行会让下一次 _claim 的 ON CONFLICT DO NOTHING 直接吞掉补跑
            #   —— 这正是"补跑看起来跑了、其实被幂等挡掉"的经典假象。
            await s.execute(
                delete(JobRun).where(
                    JobRun.task == probe,
                    or_(
                        JobRun.started_at >= datetime.combine(today, datetime.min.time(), tzinfo=tz),
                        JobRun.scheduled_at >= datetime.combine(today, datetime.min.time(), tzinfo=tz),
                    ),
                )
            )
        # ② 补跑:应为"缺失"→ 执行
        #    ★ 参考时刻要落在 max_delay(=4h)以内:06:00 的槽位用 06:30 补,
        #      延迟 30 分钟 → 正常补跑;若用 23:59 则是 18 小时延迟,
        #      按算法**应该**记 skipped(那正是另一条分支,见 ④)。
        fake_now = datetime.combine(today, datetime.min.time(), tzinfo=tz).replace(hour=6, minute=30)
        results = await rt.tasks.catch_up(rt, now=fake_now, only=[probe])
        ran = [r for r in results if r.trigger == "catchup"]
        statuses = [r.status for r in ran]

        # ③ 再补一次:已有 ok 记录 → 不该重复跑
        again = await rt.tasks.catch_up(rt, now=fake_now, only=[probe])

        # ④ 超 max_delay → skipped 不跑
        old_slot = datetime.combine(today, datetime.min.time(), tzinfo=tz).replace(hour=6, minute=0)
        # 造一个"很久以前到点、现在才补"的场景:直接把 scheduled_at 设成 10 小时前
        late = fake_now - timedelta(hours=10)
        async with rt.db.session() as s:
            await s.execute(delete(JobRun).where(JobRun.task == probe, JobRun.scheduled_at == late))
        # ★ **必须显式传 ``now=fake_now``**:``run()`` 默认拿**真实当前时间**去比
        #   max_delay。而 ``late`` 是按 ``fake_now`` 反推出来的 —— 两者不在同一根
        #   时间轴上时,这条断言会**随运行时刻飘**:实测 23:24 跑通过(真实时间比
        #   late 晚 2.9h < 4h → 不 skip → 断言失败?反之亦然),00:15 跑就红。
        #   传同一个参考时刻后,与"今天几点跑"彻底无关。
        late_res = await rt.tasks.run(
            probe, rt, trigger="catchup", scheduled_at=late, now=fake_now
        )
        async with rt.db.session() as s:
            late_row = (
                await s.execute(select(JobRun).where(JobRun.task == probe, JobRun.scheduled_at == late))
            ).scalar_one_or_none()
        late_skipped = (
            late_res.status == "skipped"
            and late_row is not None
            and late_row.status == "skipped"
            and "max_delay" in (late_row.error or "")
        )
        _ = old_slot

    ok = (
        derive_ok
        and bool(ran)
        and all(s == "ok" for s in statuses)
        and not any(r.trigger == "catchup" for r in again)
        and late_skipped
    )
    ev = (
        f"注册表反推今天应跑 {len(fires)} 次 {[f.strftime('%H:%M') for f in fires]};"
        f"删除记录后补跑 {len(ran)} 次 status={statuses};"
        f"再补跑 {len(again)} 次(应为 0);"
        f"超 max_delay 补跑 → {late_res.status}(记 skipped 不跑)={late_skipped}"
    )
    return _res(
        "V14",
        v14.__doc__ or "",
        PASS if ok else FAIL,
        ev,
        fires=[f.isoformat() for f in fires],
        first_catchup=[r.as_dict() for r in ran],
        second_catchup=len(again),
        late=late_res.as_dict(),
    )


# ===========================================================================
# V15 — 防重跑(advisory lock)
# ===========================================================================


@REG.add("V15", "防重跑:并发触发同一任务两次→第二次 skipped(advisory lock 生效)")
async def v15() -> CheckResult:
    from sqlalchemy import text

    import hoteldata.jobs  # noqa: F401
    from hoteldata.infra.db import advisory_key
    from hoteldata.runtime import Runtime

    async with Runtime.create(with_scheduler=False) as rt:
        # ① 直接用 advisory lock 验证"独占连接"语义
        key = "task:ops.selfcheck"
        async with rt.db.advisory_lock(key) as got1:
            # 另一条**独立连接**尝试拿同一把锁 → 必须失败
            async with rt.db.lock_connection() as conn:
                got2 = await conn.scalar(text("select pg_try_advisory_lock(:k)"), {"k": advisory_key(key)})
        lock_exclusive = bool(got1) and not bool(got2)

        # ② 端到端:并发跑同一任务两次
        import asyncio as _aio

        async def _run() -> Any:
            return await rt.tasks.run("ops.selfcheck", rt, trigger="manual")

        r1, r2 = await _aio.gather(_run(), _run())
        statuses = sorted([r1.status, r2.status])
        one_skipped = statuses.count("skipped") >= 1
        # 至少一个跑成功
        ok = lock_exclusive and "ok" in statuses and one_skipped
        ev = (
            f"advisory lock 独占性:持锁期间另一条连接取锁失败={lock_exclusive};"
            f"并发两次 manual 运行 → 状态={statuses}(至少一个 skipped)"
        )
        if not one_skipped:
            ev += (
                " —— 注:两次 manual 运行的 scheduled_at 为 NULL,防重靠 advisory lock;"
                "若两次恰好完全串行则都成功(不算失败)"
            )
        return _res(
            "V15",
            v15.__doc__ or "",
            PASS if ok else FAIL,
            ev,
            lock_exclusive=lock_exclusive,
            statuses=statuses,
            r1=r1.as_dict(),
            r2=r2.as_dict(),
        )
    # db 由 runtime 析构


# ===========================================================================
# V16 — 限频生效
# ===========================================================================


@REG.add("V16", "限频生效:请求间隔 ≥0.6s;跨账号可并发但平台级总速率不超上限")
async def v16() -> CheckResult:
    import asyncio

    from hoteldata.infra.rate_limit import RateLimiter
    from hoteldata.settings import get_settings as gs

    settings = gs()
    lim = RateLimiter(settings)
    stamps: list[float] = []
    lock = asyncio.Lock()

    async def one(account: str, n: int) -> None:
        for _ in range(n):
            async with lim.request("ctrip", account):
                async with lock:
                    stamps.append(time.monotonic())

    started = time.monotonic()
    await asyncio.gather(one("ctrip001", 6), one("ctrip002", 6), one("ctrip003", 6))
    elapsed = time.monotonic() - started
    stamps.sort()
    gaps = [b - a for a, b in zip(stamps, stamps[1:], strict=False)]
    min_gap = min(gaps) if gaps else 0.0
    expected_min = settings.rate_limit.interval_s * 0.95  # 5% 容差

    # ★ 账号级串行:同账号 6 次请求必须严格顺序(时间递增且不重叠)
    #    平台级:全局相邻间隔 ≥0.6s
    n = len(stamps)
    total_expected = (n - 1) * settings.rate_limit.interval_s
    ok = n == 18 and min_gap >= expected_min and elapsed >= total_expected * 0.9 and lim.stats.violations == 0
    ev = (
        f"3 账号 × 6 请求 = {n} 次,总耗时 {elapsed:.2f}s(理论下限 {total_expected:.2f}s);"
        f"平台级最小相邻间隔 {min_gap:.4f}s(要求 ≥{expected_min:.4f}s);"
        f"配置间隔 {settings.rate_limit.interval_s}s;违例 {lim.stats.violations} 次;"
        f"并发上限 MAX_CONCURRENT_ACCOUNTS={settings.rate_limit.max_concurrent_accounts}"
    )
    return _res(
        "V16",
        v16.__doc__ or "",
        PASS if ok else FAIL,
        ev,
        min_gap=round(min_gap, 4),
        elapsed=round(elapsed, 3),
        snapshot=lim.snapshot(),
    )


# ===========================================================================
# V17 — 巡检可用
# ===========================================================================


@REG.add("V17", "巡检可用:探测出失效登录态→触发重登→写 ops_login_events")
async def v17() -> CheckResult:
    from sqlalchemy import func, select

    import hoteldata.jobs  # noqa: F401
    from hoteldata.infra.models import LoginEvent
    from hoteldata.runtime import Runtime

    async with Runtime.create(with_scheduler=False) as rt:
        async def _expire_count() -> int:
            """只数 ``action='expire'`` 的事件。

            ★ 为什么按 action 计数,而不是数 login_events 总数:
            文档化的不变式是「**有效时不写 `expire`**,失效时必须写」。
            而一次巡检还可能写别的行 —— 例如 `relogin`(采集路径的
            ``ensure_valid`` 探活偶发失败后触发)或 `patrol/fail`(状态文件
            瞬时不可读)。用"总数变化"当判据会把这些**无关行**误判成不一致,
            实测导致过一次假 FAIL(V17 偶发)。按 `expire` 精确计数才是对的。
            """
            async with rt.db.session() as s:
                return int(
                    await s.scalar(
                        select(func.count())
                        .select_from(LoginEvent)
                        .where(LoginEvent.action == "expire")
                    )
                    or 0
                )

        before = await _expire_count()
        from hoteldata.domains.ops.patrol import patrol_once

        # ---- ① 真账号巡检(不自动重登,避免真去点平台登录) ----
        report = await patrol_once(rt, alias="ctrip001", do_relogin=False, do_renew=False)
        after_real = await _expire_count()
        # ★ 断言"**一致性**"而不是"必须失效":会话有效时就不该写 expire 事件。
        real_consistent = report.checked == 1 and (report.invalid == 0) == (
            after_real == before
        )

        # ---- ② 失效路径的**接线**验证:把探活打桩成 False,看是否写 expire ----
        #     (不能靠"真把会话弄失效"来测 —— 那会毁掉有效登录态)
        mgr = rt.login()
        original_probe = mgr.probe_login_valid

        async def _always_invalid(h, **kw):  # noqa: ANN001, ANN202
            return False

        mgr.probe_login_valid = _always_invalid  # type: ignore[method-assign]
        try:
            fail_report = await patrol_once(rt, alias="ctrip001", do_relogin=False, do_renew=False)
            after_fail = await _expire_count()
            async with rt.db.session() as s:
                rows = (
                    (await s.execute(select(LoginEvent).order_by(LoginEvent.id.desc()).limit(3)))
                    .scalars()
                    .all()
                )
        finally:
            mgr.probe_login_valid = original_probe  # type: ignore[method-assign]
            # ★ 打桩巡检已经把"假的 invalid"写进了 sessions 表 —— 必须刷新回真实状态,
            #   否则会污染**后续运行**的其他检查(曾把 V3 误判成 BLOCKED)。
            try:
                await mgr.check(rt.sessions.handle("ctrip", "ebooking", "ctrip001"), probe=True)
            except Exception as exc:  # noqa: BLE001
                print(f"  (刷新会话状态失败: {exc})")

        events = [{"action": r.action, "result": r.result, "detail": (r.detail or "")[:60]} for r in rows]
        fail_path_ok = (
            fail_report.invalid == 1
            and after_fail == after_real + 1  # ★ 失效必须**恰好**多一条 expire
            and any(r.action == "expire" for r in rows)
        )

        ok = real_consistent and fail_path_ok
        ev = (
            f"真账号巡检 → checked={report.checked} invalid={report.invalid}"
            f"(一致性={real_consistent});"
            f"打桩探活=False 后巡检 → invalid={fail_report.invalid} "
            f"expire 事件 {before}→{after_real}→{after_fail} 条"
            f"(失效时 +1={after_fail == after_real + 1})"
        )
        return _res(
            "V17",
            v17.__doc__ or "",
            PASS if ok else FAIL,
            ev,
            report=report.as_dict(),
            fail_report=fail_report.as_dict(),
            events=events,
        )
    # runtime 已析构


# ===========================================================================
# V18 — 清理安全
# ===========================================================================


@REG.add("V18", "清理安全:cleanup --dry-run 列出待删项;config/ 与 db/ 一个字节不动")
async def v18() -> CheckResult:
    from hoteldata.domains.ops.cleanup import run as run_cleanup

    settings = get_settings()
    cfg = settings.paths.config_dir
    fingerprint_before = _tree_fingerprint(cfg)

    report = await run_cleanup(settings, dry_run=True)
    fingerprint_after = _tree_fingerprint(cfg)
    data = report.as_dict()

    # dry-run 不得删除任何东西
    deleted_in_dry = data.get("deleted", 0)
    planned = data.get("would_delete", 0)
    # 白名单项不得出现在计划里
    forbidden_hits = [
        it
        for it in (data.get("items") or [])
        if any(
            tok in str(it.get("path", ""))
            for tok in (
                "\\config\\",
                "/config/",
                "\\db\\",
                "/db/",
                "\\var\\states",
                "/var/states",
                "\\logs\\",
                "/logs/",
            )
        )
    ]
    ok = (
        fingerprint_before == fingerprint_after
        and deleted_in_dry == 0
        and not forbidden_hits
        and "freed_mb" in data
    )
    ev = (
        f"dry-run:待删 {planned} 项、实删 {deleted_in_dry}(必须 0);"
        f"config/ 指纹前后一致={fingerprint_before == fingerprint_after};"
        f"计划中含白名单项 {len(forbidden_hits)} 个(必须 0);freed_mb={data.get('freed_mb')}"
    )
    return _res(
        "V18",
        v18.__doc__ or "",
        PASS if ok else FAIL,
        ev,
        planned=planned,
        forbidden_hits=forbidden_hits[:3],
        report_keys=sorted(data),
    )


def _tree_fingerprint(root: Path) -> dict[str, Any]:
    if not root.exists():
        return {}
    out: dict[str, Any] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = (p.stat().st_size, int(p.stat().st_mtime))
    return out


# ===========================================================================
# V19 — 冷备可用
# ===========================================================================


@REG.add("V19", "冷备可用:产出可打开的备份;同日重跑幂等")
async def v19() -> CheckResult:
    from hoteldata.domains.ops.backup import run as run_backup

    settings = get_settings()
    r1 = await run_backup(settings)
    d1 = r1.as_dict()
    r2 = await run_backup(settings)
    d2 = r2.as_dict()
    path = d1.get("path")
    size = Path(path).stat().st_size if path and Path(path).exists() else 0
    head = Path(path).read_bytes()[:5] if size else b""
    ok = (
        bool(d1.get("ok"))
        and size > 0
        and head == b"PGDMP"
        and bool(d2.get("skipped_already_done"))
        and size == (Path(d2.get("path")).stat().st_size if d2.get("path") else 0)
    )
    ev = (
        f"首次 → ok={d1.get('ok')} 文件={Path(path).name if path else None} {size}B "
        f"magic={head!r} 校验={d1.get('verified')}({d1.get('verify_method')});"
        f"同日重跑 → skipped_already_done={d2.get('skipped_already_done')} 大小不变="
        f"{size == (Path(d2.get('path')).stat().st_size if d2.get('path') else 0)};"
        f"保留策略 {d1.get('retention_days')} 天"
    )
    return _res("V19", v19.__doc__ or "", PASS if ok else FAIL, ev, first=d1, second=d2)


# ===========================================================================
# V20 — 批次 D 落库
# ===========================================================================


@REG.add("V20", "批次 D:预警三源/房态/点评各自能落库")
async def v20() -> CheckResult:
    from sqlalchemy import text as _t

    from hoteldata.domains.collect.repository import CollectRepository
    from hoteldata.infra.db import Database

    settings = get_settings()
    db = Database(settings)
    try:
        pair = await _first_pair(db)
        if pair is None:
            return _res("V20", v20.__doc__ or "", BLOCKED, "无酒店/账号")
        hotel, acc = pair

        # ① 预警三源:唯一键 UPSERT
        async with db.session() as s:
            repo = CollectRepository(s)
            n1 = await repo.upsert_portal_columns(
                [
                    {
                        "hotel_id": hotel.id,
                        "collect_date": SYNTH_DAY,
                        "page": "预警-渠道",
                        "column_name": "visitor_total",
                        "value": "123",
                        "source_api": "fetchVisitorTitleV2",
                    },
                    {
                        "hotel_id": hotel.id,
                        "collect_date": SYNTH_DAY,
                        "page": "预警-渠道",
                        "column_name": "min_price",
                        "value": "456",
                        "source_api": "queryHotelMinPriceV1",
                    },
                ]
            )
            n1b = await repo.upsert_portal_columns(
                [
                    {
                        "hotel_id": hotel.id,
                        "collect_date": SYNTH_DAY,
                        "page": "预警-渠道",
                        "column_name": "visitor_total",
                        "value": "999",
                        "source_api": "fetchVisitorTitleV2",
                    },
                ]
            )
            cols = await repo.list_portal_columns(hotel.id, SYNTH_DAY)

        # ② 房态:整批替换 + available 语义
        async with db.session() as s:
            repo = CollectRepository(s)
            await repo.replace_room_states(
                hotel.id,
                SYNTH_DAY,
                [
                    {
                        # ★ 售完(quantity=0)但 roomStatus=='G' → available 仍为 1
                        "room_name": "大床房",
                        "effect_date": SYNTH_DAY,
                        "status_code": "G",
                        "available": 1,
                        "quantity": 0,
                        "price": 399.0,
                    },
                    {
                        "room_name": "双床房",
                        "effect_date": SYNTH_DAY,
                        "status_code": "C",
                        "available": 0,
                        "quantity": 5,
                        "price": 299.0,
                    },
                ],
            )
            rooms1 = await repo.list_room_states(hotel.id, SYNTH_DAY)
            # 重跑整批替换 → 行数一致
            await repo.replace_room_states(
                hotel.id,
                SYNTH_DAY,
                [
                    {
                        "room_name": "大床房",
                        "effect_date": SYNTH_DAY,
                        "status_code": "G",
                        "available": 1,
                        "quantity": 3,
                        "price": 389.0,
                    },
                ],
            )
            rooms2 = await repo.list_room_states(hotel.id, SYNTH_DAY)

        # ③ 点评:UPSERT **不回溯**
        # ★ 三段必须落在**三个已提交的事务**里:若把"标记已回复"塞进采集所在的那个
        #   未提交事务的嵌套 session,UPDATE 看不到尚未提交的行 → 匹配 0 行 →
        #   后面的"不回溯"断言会因为"压根没标记过"而**假失败**。
        review_row = {
            "hotel_id": hotel.id,
            "review_id": "r-1",
            "user_name": "张三",
            "star": 5,
            "content": "很好",
            "comment_time": datetime(2026, 9, 1, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
            "sentiment": "good",
            "replied": 0,
            "strategy": "pending",
        }
        async with db.session() as s:
            repo = CollectRepository(s)
            await repo.upsert_reviews([review_row])
            rev1 = await repo.list_reviews(hotel.id)
        # 模拟运维把这条标成"已自动回复"(提交后再采)
        async with db.session() as s2:
            await s2.execute(
                _t(
                    "update review_reviews set replied=1, strategy='auto_ok' "
                    "where hotel_id=:h and review_id='r-1'"
                ),
                {"h": hotel.id},
            )
        # 再采集同一条(内容变了)→ replied / strategy **必须保持**
        async with db.session() as s:
            repo = CollectRepository(s)
            await repo.upsert_reviews([{**review_row, "content": "很好(采集又抓到一次)"}])
            rev2 = await repo.list_reviews(hotel.id)
            mats = await repo.upsert_review_materials(
                [
                    {
                        "hotel_id": hotel.id,
                        "collect_date": SYNTH_DAY,
                        "kind": "score",
                        "payload_json": {"v": 1},
                        "status": "ok",
                    },
                    {
                        "hotel_id": hotel.id,
                        "collect_date": SYNTH_DAY,
                        "kind": "num",
                        "payload_json": {"v": 2},
                        "status": "no_data",
                    },
                ]
            )
        r1 = rev1[0] if rev1 else None
        r2 = rev2[0] if rev2 else None
        not_backtracked = bool(
            r1 and r2 and r2.replied == 1 and r2.strategy == "auto_ok"
        )  # ★ 重采不重置 replied / strategy
        batch_replace_ok = len(rooms1) == 2 and len(rooms2) == 1
        portal_upsert_ok = n1 >= 1 and n1b >= 1 and len(cols) == 2 and any(c.value == "999" for c in cols)
        ok = portal_upsert_ok and batch_replace_ok and not_backtracked and mats >= 1
        ev = (
            f"预警三源写入 {n1}/{n1b} 行 → 表内 {len(cols)} 列(visitor_total 被覆盖为 999="
            f"{any(c.value == '999' for c in cols)});"
            f"房态整批替换 2 行 → 重跑 1 行={batch_replace_ok};"
            f"点评重采后 replied={getattr(r2, 'replied', None)} strategy={getattr(r2, 'strategy', None)}"
            f"(**不回溯**={not_backtracked});素材写入 {mats} 行"
        )
        return _res("V20", v20.__doc__ or "", PASS if ok else FAIL, ev)
    finally:
        try:
            pair = await _first_pair(db)
            if pair:
                await _cleanup_extractors(db, pair[0].id)
        finally:
            await db.dispose()


async def _cleanup_extractors(db: Any, hotel_id: int) -> None:
    from sqlalchemy import delete, text

    from hoteldata.infra.models.extractors import (
        AlertPortalColumn,
        AlertRoomState,
        ReviewMaterial,
    )

    async with db.session() as s:
        await s.execute(
            delete(AlertPortalColumn).where(
                AlertPortalColumn.hotel_id == hotel_id,
                AlertPortalColumn.collect_date == SYNTH_DAY,
            )
        )
        await s.execute(
            delete(AlertRoomState).where(
                AlertRoomState.hotel_id == hotel_id,
                AlertRoomState.collect_date == SYNTH_DAY,
            )
        )
        await s.execute(
            delete(ReviewMaterial).where(
                ReviewMaterial.hotel_id == hotel_id,
                ReviewMaterial.collect_date == SYNTH_DAY,
            )
        )
        await s.execute(
            text("delete from review_reviews where hotel_id=:h and review_id like 'r-%'"),
            {"h": hotel_id},
        )
    await _cleanup_synth(db, hotel_id)


# ===========================================================================
# 执行
# ===========================================================================


async def run_all(only: list[str] | None = None) -> list[CheckResult]:
    out: list[CheckResult] = []
    for vid, title, fn in REG.checks:
        if only and vid not in only:
            continue
        started = time.monotonic()
        try:
            res = await fn()
        except Exception as exc:  # noqa: BLE001
            import traceback

            res = _res(
                vid,
                title,
                FAIL,
                f"执行异常: {type(exc).__name__}: {exc}",
                traceback=traceback.format_exc()[-1500:],
            )
        res.detail["elapsed_s"] = round(time.monotonic() - started, 2)
        out.append(res)
        colour = {"PASS": "\033[92m", "FAIL": "\033[91m", "BLOCKED": "\033[93m"}.get(res.status, "")
        print(f"{colour}{res.status:8s}\033[0m {res.vid:4s} {res.title}")
        print(f"         {res.evidence}")
    return out


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description="段1 验收执行器(V1–V20)")
    parser.add_argument("--only", nargs="*", help="只跑指定编号,如 V1 V9")
    parser.add_argument("--json", help="结果 JSON 输出路径")
    args = parser.parse_args(argv)

    results = asyncio.run(run_all(args.only))
    passed = sum(1 for r in results if r.status == PASS)
    failed = [r.vid for r in results if r.status == FAIL]
    blocked = [r.vid for r in results if r.status == BLOCKED]
    print()
    print("=" * 78)
    print(f"合计 {len(results)} 项:PASS {passed} / FAIL {len(failed)} / BLOCKED {len(blocked)}")
    if failed:
        print(f"FAIL  : {failed}")
    if blocked:
        print(f"BLOCKED: {blocked}")
    print("=" * 78)

    out = Path(args.json) if args.json else PROJECT_ROOT / "var" / "reports" / "段1-验收结果.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "summary": {
                    "total": len(results),
                    "pass": passed,
                    "fail": len(failed),
                    "blocked": len(blocked),
                    "failed_ids": failed,
                    "blocked_ids": blocked,
                },
                "results": [
                    {
                        "id": r.vid,
                        "title": r.title,
                        "status": r.status,
                        "evidence": r.evidence,
                        "detail": r.detail,
                    }
                    for r in results
                ],
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    print(f"结果已写入 {out}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
