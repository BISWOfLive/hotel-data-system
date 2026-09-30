"""临时验证脚本 — backup(真实容器 pg_dump)+ selfcheck(影子库只读聚合)。

跑法::

    .venv\\Scripts\\python.exe _scratch\\verify_backup_selfcheck.py

安全边界:
  * backup 只写 ``var\\backup\\``(它自己的 owner 目录),不动其它任何位置;
  * selfcheck 的影子库 ``hoteldata_probe`` 是本脚本**新建**的,验完立刻 DROP,
    **不碰**真实的 ``hoteldata`` 库(它此刻还没有表,那是 T1.4 的事);
  * selfcheck 用 ``var_dir`` 指向 ``_scratch\\probe_var``,不污染项目 ``var\\``。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(r"D:\AAAAaaaa\pypypypy\hotel-data-system").resolve()
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from hoteldata.domains.ops import backup, selfcheck  # noqa: E402
from hoteldata.infra.models import Base  # noqa: E402
from hoteldata.settings import Settings  # noqa: E402

PROBE_DB = "hoteldata_probe"
PROBE_VAR = PROJECT_ROOT / "_scratch" / "probe_var"
BACKUP_DIR = PROJECT_ROOT / "var" / "backup"
FAILURES: list[str] = []


def check(condition: bool, msg: str) -> None:
    if condition:
        print(f"  [PASS] {msg}")
    else:
        FAILURES.append(msg)
        print(f"  [FAIL] {msg}")


def show(title: str, payload: object) -> None:
    print(f"\n{title}")
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def probe_url(base: str, dbname: str) -> str:
    return base.rsplit("/", 1)[0] + "/" + dbname


# ---------------------------------------------------------------------------
# A. backup(真实容器)
# ---------------------------------------------------------------------------


async def verify_backup(settings: Settings, today: date) -> None:
    print("=" * 100)
    print("[A] 冷备:docker exec hoteldata-pg pg_dump -Fc → var/backup(T6.6 / V19)")
    print("=" * 100)
    fixture = BACKUP_DIR / "hoteldata_20200101.dump"  # 假旧备:验证轮转
    today_dump = BACKUP_DIR / f"hoteldata_{today:%Y%m%d}.dump"
    # 这两个都是本脚本自己(本轮或上一轮)在 backup 域造出来的产物,先复位以便两条路径都能验到
    for stale in (fixture, today_dump):
        if stale.exists():
            print(f"  [安全] 复位本脚本自己的产物:{stale}")
            stale.unlink()
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    fixture.write_bytes(b"stale-fixture")
    print(f"  轮转测试夹具:{fixture}(已重建,10 字节)")

    report = await backup.run(settings)
    show("  第 1 次运行报告:", report.as_dict())
    check(report.ok, "ok=True(产出成功)")
    check(report.verified, f"verified=True(校验方式 {report.verify_method} / {report.verify_detail})")
    check(
        report.verify_method is not None and "pg_restore --list(docker)" in report.verify_method,
        "用容器内 pg_restore --list 深校验(不是只看文件头)",
    )
    check(bool(report.method and report.method.startswith("docker:")), f"走容器 pg_dump(method={report.method})")
    check(report.bytes > 0 and report.retained >= 1, f"产出 {report.size_mb} MB;当前保留 {report.retained} 份")
    check(not report.errors, f"无错误(errors={report.errors})")
    check(not fixture.exists(), "★ 轮转删掉了 7 天前的旧备夹具")

    target = PROJECT_ROOT / (report.path or "")
    check(target.exists() and target.stat().st_size > 0, f"文件在盘上:{report.path}")

    print("\n  手工再验一次「能列出内容」(V19 口径):")
    with target.open("rb") as handle:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            "-i",
            backup.DOCKER_CONTAINER,
            "pg_restore",
            "--list",
            stdin=handle,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
    lines = [line for line in out.decode("utf-8", "replace").splitlines() if line.strip()]
    print(f"    rc={proc.returncode} 条目={len(lines)} 首行={lines[0] if lines else '(空)'}")
    check(proc.returncode == 0 and len(lines) > 0, "pg_restore --list 能列出 TOC 内容")
    if err:
        print(f"    stderr={err.decode('utf-8', 'replace').strip()[:200]}")

    print("\n  第 2 次运行(同日幂等):")
    again = await backup.run(settings)
    show("  第 2 次运行报告:", again.as_dict())
    check(again.ok and again.skipped_already_done, "skipped_already_done=True 且 ok=True(不重复产出、不报错)")
    check(again.bytes == report.bytes, "字节数与第 1 次一致(未被重写)")

    print("\n  失败路径:容器名不存在 + 本机 pg_dump 不在 PATH → 必须 ok=False(不假装成功)")
    broken = await backup.run(settings, now=today + timedelta(days=1), docker_container="no-such-container")
    show("  失败路径报告:", broken.as_dict())
    check(not broken.ok, "ok=False")
    check(bool(broken.errors), f"errors 非空:{broken.errors[:1]}")
    check(
        all(not attempt.get("ok") for attempt in broken.attempts),
        f"两次尝试都记为失败:{[a.get('method') for a in broken.attempts]}",
    )
    leftover = BACKUP_DIR / f"hoteldata_{(today + timedelta(days=1)).strftime('%Y%m%d')}.dump"
    check(not leftover.exists(), "失败时不留半截文件(.part 已清理)")


# ---------------------------------------------------------------------------
# B. selfcheck(影子库)
# ---------------------------------------------------------------------------


async def with_probe_db(settings: Settings, body) -> None:
    admin_url = probe_url(settings.db.url, "postgres")
    admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{PROBE_DB}" WITH (FORCE)'))
        await conn.execute(text(f'CREATE DATABASE "{PROBE_DB}"'))
    print(f"  [安全] 新建影子库 {PROBE_DB}(验完即 DROP,不碰 hoteldata 真库)")
    probe = create_async_engine(probe_url(settings.db.url, PROBE_DB))
    try:
        async with probe.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await body(probe)
    finally:
        await probe.dispose()
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{PROBE_DB}" WITH (FORCE)'))
        await admin.dispose()
        print(f"  [安全] 影子库 {PROBE_DB} 已 DROP")


async def seed(probe) -> None:
    today = datetime.now().date()
    yesterday = today - timedelta(days=1)
    statements = [
        "INSERT INTO core_accounts (alias, platform, username_enc, password_enc, status) VALUES "
        "('ctrip001','ctrip','u1','p1','active'), ('ctrip002','ctrip','u2','p2','active'), "
        "('meituan001','meituan','u3','p3','blocked')",
        "INSERT INTO core_hotels (name, status) VALUES ('测试酒店A','active'), ('测试酒店B','archived')",
        "INSERT INTO sessions (platform, role, alias, status) VALUES "
        "('ctrip','ebooking','ctrip001','valid'), ('ctrip','ebooking','ctrip002','stale'), "
        "('meituan','merchant','meituan001','invalid')",
        'INSERT INTO collect_modules (hotel_id, collect_date, page, module, "window", payload_json, channel, status) VALUES '
        f"(1, '{yesterday}', '经营报告', 'm1', '今日', '{{}}', 'api', 'ok'),"
        f"(1, '{yesterday}', '经营报告', 'm2', '昨日', '{{}}', 'api', 'ok'),"
        f"(1, '{yesterday}', '经营报告', 'm3', '近7日', '{{}}', 'api', 'ok'),"
        f"(1, '{yesterday}', '经营报告', 'm4', '近30日', '{{}}', 'api', 'no_data'),"
        f"(2, '{yesterday}', '经营报告', 'm5', '今日', '{{}}', 'browser', 'failed')",
        "INSERT INTO job_runs (task, status, trigger, started_at) VALUES "
        f"('collect','ok','schedule','{today} 05:00:00+08'),"
        f"('cleanup','failed','schedule','{today} 04:00:00+08')",
    ]
    async with probe.begin() as conn:
        for sql in statements:
            await conn.execute(text(sql))


async def verify_selfcheck(settings: Settings) -> None:
    print("\n" + "=" * 100)
    print("[B] 自检:只读聚合(T6.6 / B26),影子库 hoteldata_probe")
    print("=" * 100)

    probe_var = PROBE_VAR
    states = probe_var / "states"
    states.mkdir(parents=True, exist_ok=True)
    old_state = states / "ctrip__ebooking__ctrip001.json"
    old_state.write_text('{"cookies": [{"name": "a", "value": "b"}]}', encoding="utf-8")
    fresh_state = states / "ctrip__ebooking__ctrip002.json"
    fresh_state.write_text('{"cookies": []}', encoding="utf-8")
    long_ago = (datetime.now() - timedelta(days=40)).timestamp()
    os.utime(old_state, (long_ago, long_ago))
    print(f"  var_dir 指向 {probe_var}(登录态:1 个 40 天前 + 1 个刚写)")
    check(len(list(states.glob("*.json"))) == 2, "var/states 下 2 个 *.json")

    probe_settings = Settings(
        **{**settings.model_dump(), "var_dir": probe_var, "db_url": probe_url(settings.db.url, PROBE_DB)}
    )

    async def body(probe) -> None:
        await seed(probe)
        async with probe.connect() as conn:
            before = {
                table: int(await conn.scalar(text(f"SELECT count(*) FROM {table}")))
                for table in ("core_accounts", "core_hotels", "sessions", "collect_modules", "job_runs")
            }
        print(f"  造数完成:{before}")

        report = await selfcheck.run(probe_settings)
        show("  自检报告:", report.as_dict())

        check(not report.errors, f"★ 全部只读聚合成功(errors={report.errors})")
        check(report.disk.total_gb > 0 and report.disk.min_free_gb == settings.ops.disk_min_free_gb, "磁盘指标")
        check(
            set(report.dirs) == {"raw", "screenshots", "reports", "backup", "logs"},
            f"目录体积指标齐全:{sorted(report.dirs)}",
        )
        check(len(report.dirs["raw"].path) > 0 and isinstance(report.dirs["raw"].mb, float), "目录指标含 MB/文件数")
        check(report.accounts.total == 3, f"账号总数=3(实际 {report.accounts.total})")
        check(
            report.accounts.by_status == {"active": 2, "blocked": 1},
            f"账号按 status 分组 {report.accounts.by_status}",
        )
        check(report.hotels.total == 2 and report.hotels.by_status.get("active") == 1, "酒店 2 家 / active 1 家")
        check(report.sessions.files == 2 and report.sessions.handles == 2, "登录态文件数=2")
        check(report.sessions.total == 3 and report.sessions.by_status.get("invalid") == 1, "sessions 表按状态分组")
        check(report.sessions.need_renewal == 1, f"★ need_renewal=1(40 天前那个;实际 {report.sessions.need_renewal})")

        collect = report.collect_yesterday
        assert collect is not None
        check(collect.total == 5, f"昨日提取 5 行(实际 {collect.total})")
        check(collect.ok == 3 and collect.no_data == 1 and collect.failed == 1, "四态计数 ok=3/no_data=1/failed=1")
        check(collect.success_rate == 0.8, f"★ success_rate=0.8(no_data 不算失败;实际 {collect.success_rate})")
        check(collect.hotels_covered == 2, f"覆盖酒店数 2(实际 {collect.hotels_covered})")
        check(report.jobs_today.total == 2 and report.jobs_today.by_status.get("failed") == 1, "今日任务按状态分组")
        check(bool(report.interpreter.get("version")) and "gil_disabled" in report.interpreter, "解释器指标")

        async with probe.connect() as conn:
            after = {
                table: int(await conn.scalar(text(f"SELECT count(*) FROM {table}")))
                for table in ("core_accounts", "core_hotels", "sessions", "collect_modules", "job_runs")
            }
        check(before == after, f"★ 自检前后行数不变(只读,未写库):{after}")

    await with_probe_db(settings, body)


# ---------------------------------------------------------------------------


async def main() -> int:
    settings = Settings()
    today = datetime.now(settings.tzinfo).date()
    print(f"项目根 {PROJECT_ROOT}")
    print(f"DB_URL {settings.db.url}")
    print(f"今天   {today}")
    await verify_backup(settings, today)
    await verify_selfcheck(settings)
    print("\n" + "=" * 100)
    if FAILURES:
        print(f"结果:FAIL —— {len(FAILURES)} 条断言未通过")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("结果:全部断言通过(冷备可打开 + 同日幂等 + 失败不假装成功 + 自检只读聚合口径正确)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
