"""临时验证脚本 — cleanup 的 dry-run 安全性 + 双保留期 + 白名单(留档于 ``_scratch/``,不进 src)。

跑法::

    .venv\\Scripts\\python.exe _scratch\\verify_cleanup.py

安全红线(脚本自己执行):
  1. 只碰 ``var\\`` 与 ``config\\DO_NOT_DELETE.txt`` 这两处**本脚本亲手创建**的位置 ——
     脚本维护一份 ``CREATED`` 集合(亲手创建的每个文件/目录的 resolve 路径);
  2. 实跑前把 dry-run 计划里的**每一条**与 ``CREATED`` 逐个比对,
     只要有一条不在里面就**立刻中止**(此时一个字节都还没删);
  3. 删除后断言 ``config\\`` 字节未变、``var\\states`` / ``var\\logs`` / ``var\\backup`` /
     ``var\\states_backup`` 指纹未变。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

PROJECT_ROOT = Path(r"D:\AAAAaaaa\pypypypy\hotel-data-system").resolve()
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from hoteldata.domains.ops import cleanup  # noqa: E402
from hoteldata.settings import Settings  # noqa: E402

VAR = (PROJECT_ROOT / "var").resolve()
CONFIG = (PROJECT_ROOT / "config").resolve()
MARKER = CONFIG / "DO_NOT_DELETE.txt"
HOTEL = "测试酒店"

TZ = None  # 由 preamble() 注入
CREATED: set[Path] = set()
FAILURES: list[str] = []


def ok(msg: str) -> None:
    print(f"  [PASS] {msg}")


def check(condition: bool, msg: str) -> None:
    if condition:
        ok(msg)
    else:
        FAILURES.append(msg)
        print(f"  [FAIL] {msg}")


def abort(msg: str) -> None:
    print(f"\n!!! 安全中止:{msg}")
    raise SystemExit(2)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def assert_under_var(path: Path, *, action: str) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(VAR):
        abort(f"{action} 的目标不在 var\\ 之下:{resolved}")
    print(f"  [安全] {action}: {resolved}")
    return resolved


def assert_marker(path: Path, *, action: str) -> Path:
    """``config\\DO_NOT_DELETE.txt`` 是本脚本唯一允许碰的 config\\ 路径。"""
    resolved = path.resolve()
    if resolved != MARKER or not resolved.is_relative_to(PROJECT_ROOT):
        abort(f"{action} 只允许针对 {MARKER},实际 {resolved}")
    print(f"  [安全] {action}: {resolved}")
    return resolved


def snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """目录树指纹:相对路径 → (字节数, mtime_ns)。"""
    state: dict[str, tuple[int, int]] = {}
    if not root.exists():
        return state
    for item in sorted(root.rglob("*")):
        if item.is_file():
            stat = item.stat()
            state[item.relative_to(root).as_posix()] = (stat.st_size, stat.st_mtime_ns)
    return state


def digest(state: dict[str, tuple[int, int]]) -> str:
    blob = json.dumps(sorted(state.items()), ensure_ascii=False).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def tree_lines(root: Path) -> list[str]:
    lines: list[str] = []
    for item in sorted(root.rglob("*")):
        if item.is_file():
            stat = item.stat()
            mt = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d")
            lines.append(f"    {item.relative_to(root).as_posix():<56} {stat.st_size:>7}B  mtime={mt}")
    return lines


def touch(path: Path, *, days_ago: int, payload: bytes = b"x") -> Path:
    """建文件并把 mtime 设到 ``days_ago`` 天前的中午(避开日界),登记进 ``CREATED``。"""
    assert_under_var(path, action="创建")
    path.parent.mkdir(parents=True, exist_ok=True)
    for parent in [path.parent, *path.parents]:
        if parent == VAR or not parent.is_relative_to(VAR):
            break
        CREATED.add(parent.resolve())
    path.write_bytes(payload)
    stamp = datetime.combine(date.today() - timedelta(days=days_ago), time(12, 0), tzinfo=TZ)
    os.utime(path, (stamp.timestamp(), stamp.timestamp()))
    CREATED.add(path.resolve())
    return path


def expected_action(day: date, today: date, data_days: int, shot_days: int) -> str:
    """双保留期的期望分类:``dir``=整目录删 / ``data``=只删数据留截图 / ``keep``=不动。"""
    if day < today - timedelta(days=shot_days):
        return "dir"
    if day < today - timedelta(days=data_days):
        return "data"
    return "keep"


# ---------------------------------------------------------------------------
# 0. 前置
# ---------------------------------------------------------------------------


def preamble() -> tuple[Settings, date]:
    print("=" * 100)
    print("cleanup dry-run 安全性验证(临时脚本,_scratch/verify_cleanup.py)")
    print("=" * 100)
    print(f"项目根  : {PROJECT_ROOT}")
    print(f"var\\    : {VAR}")
    print(f"config\\ : {CONFIG}")
    print(f"解释器  : {sys.executable}")
    settings = Settings(var_dir=VAR)
    global TZ
    TZ = settings.tzinfo
    today = datetime.now(settings.tzinfo).date()
    print(f"今天    : {today}(Asia/Shanghai)")
    print(
        f"保留期  : data={settings.ops.data_retention_days} 天 / "
        f"screenshot={settings.ops.screenshot_retention_days} 天 / "
        f"backup={settings.ops.backup_retention_days} 天 / "
        f"disk_min_free={settings.ops.disk_min_free_gb} GB"
    )
    if not CONFIG.is_dir():
        abort(f"config\\ 不存在:{CONFIG}")
    return settings, today


# ---------------------------------------------------------------------------
# 1. 造树
# ---------------------------------------------------------------------------


def build_tree(today: date) -> set[date]:
    """★ 只创建本脚本自己的假目录树(全部在 var\\ 下 + 一个 config\\ 标记文件)。"""
    print("\n[1] 造测试目录树(仅 var\\ 与 config\\DO_NOT_DELETE.txt)")
    raw = VAR / "raw" / HOTEL
    shots = VAR / "screenshots" / HOTEL
    window_day = today - timedelta(days=45)  # ∈ [now-90, now-30) → 只删数据、留截图
    old_day = today - timedelta(days=120)  # < now-90 → 整目录删
    dates = [date(2020, 1, 1), date(2025, 1, 1), today, window_day, old_day]

    # 任务指定的三个日期目录 + 两个动态日期目录
    touch(raw / "20200101" / "a.json", days_ago=2400, payload=b"ancient")
    touch(raw / "20200101" / "screenshots" / "shot.jpg", days_ago=2400, payload=b"jpg")
    touch(raw / "20250101" / "b.json", days_ago=600, payload=b"literal")
    touch(raw / "20250101" / "screenshots" / "shot.jpg", days_ago=600, payload=b"jpg")
    touch(raw / today.strftime("%Y%m%d") / "c.json", days_ago=0, payload=b"today")
    touch(raw / today.strftime("%Y%m%d") / "screenshots" / "shot.jpg", days_ago=0, payload=b"jpg")

    # 双保留期窗口 [now-90, now-30):raw/html/api 删,截图留
    key = window_day.strftime("%Y%m%d")
    touch(raw / key / "raw" / "payload.json", days_ago=45, payload=b"data")
    touch(raw / key / "html" / "dump.html", days_ago=45, payload=b"html")
    touch(raw / key / "api" / "api.json", days_ago=45, payload=b"api")
    touch(raw / key / "screenshots" / "keep.jpg", days_ago=45, payload=b"jpg")

    # < now-90:整目录删
    old_key = old_day.strftime("%Y%m%d")
    touch(raw / old_key / "raw" / "payload.json", days_ago=120, payload=b"old")
    touch(raw / old_key / "screenshots" / "gone.jpg", days_ago=120, payload=b"jpg")

    # var/screenshots 的日期目录
    touch(shots / key / "keep.jpg", days_ago=45, payload=b"jpg")
    touch(shots / old_key / "gone.jpg", days_ago=120, payload=b"jpg")

    # var/reports 与 var/ 平铺文件
    touch(VAR / "reports" / "报告_old.json", days_ago=100, payload=b"{}")
    touch(VAR / "reports" / "报告_new.json", days_ago=0, payload=b"{}")
    touch(VAR / "reports" / "勿删(勿删).json", days_ago=100, payload=b"{}")
    touch(VAR / "legacy_dump.html", days_ago=100, payload=b"<html>")
    touch(VAR / "keep_me.json", days_ago=0, payload=b"{}")

    # ★ 白名单:这些一个字节都不许动
    touch(VAR / "states" / "ctrip__ebooking__ctrip001.json", days_ago=100, payload=b'{"cookies": []}')
    touch(VAR / "logs" / "hoteldata_20200101.log", days_ago=800, payload=b"log")
    touch(VAR / "backup" / "hoteldata_20200101.dump", days_ago=2000, payload=b"PGDMP-fake")
    touch(VAR / "states_backup" / "trap.json", days_ago=800, payload=b"trap")
    assert_marker(MARKER, action="创建白名单标记")
    MARKER.write_text("这个文件不许被任何清理动作删掉\n", encoding="utf-8")
    print("\n  建出的树:")
    print("\n".join(tree_lines(VAR)))
    return set(dates)


# ---------------------------------------------------------------------------
# 2. 结构性白名单判定
# ---------------------------------------------------------------------------


def check_whitelist_matcher() -> None:
    print("\n[2] 结构性白名单判定(Path.is_relative_to,不是字符串前缀匹配)")
    roots = cleanup._forbidden_roots(PROJECT_ROOT)
    print(f"  白名单根:{[str(r) for r in roots]}")
    cases = [
        (VAR / "states" / "ctrip__ebooking__ctrip001.json", True, "var/states/<file>"),
        (VAR / "states_backup" / "trap.json", False, "var/states_backup/<file> 前缀像但不是子路径"),
        (CONFIG / "DO_NOT_DELETE.txt", True, "config/<file>"),
        (VAR / "logs" / "x.log", True, "var/logs/<file>(loguru 的 owner)"),
        (VAR / "backup" / "x.dump", True, "var/backup/<file>(backup.py 的 owner)"),
        (VAR / "raw" / HOTEL / "20200101" / "a.json", False, "var/raw/... (可清理)"),
    ]
    for path, expected, label in cases:
        got = cleanup._is_forbidden(path.resolve(), roots)
        check(got is expected, f"{label} → forbidden={got}(期望 {expected})")


# ---------------------------------------------------------------------------
# 3. dry-run
# ---------------------------------------------------------------------------


def planned(report: cleanup.CleanupReport) -> list[Path]:
    return [(PROJECT_ROOT / item.path).resolve() for item in report.items]


def assert_plan_is_scoped(report: cleanup.CleanupReport) -> None:
    """★ 实跑前的最后一道闸:计划里每一条都必须是本脚本亲手创建的对象。"""
    print(f"  本次计划 {len(report.items)} 条;逐条打印 resolve() 后的绝对路径:")
    for path in planned(report):
        assert_under_var(path, action="计划删除")
        if path not in CREATED:
            abort(f"计划项不是本脚本创建的对象(拒绝执行):{path}")
    ok(f"{len(report.items)} 条计划全部命中 CREATED 集合(无一越界) → 可以实跑")


def classify_plan(report: cleanup.CleanupReport, today: date, settings: Settings) -> None:
    """按双保留期逐日期目录核对"计划里的动作"是否符合期望分类。"""
    data_days = settings.ops.data_retention_days
    shot_days = settings.ops.screenshot_retention_days
    raw = VAR / "raw" / HOTEL
    rels = [item.path for item in report.items]

    def planned_rel(path: Path) -> bool:
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        return any(item == rel or item.startswith(rel + "/") for item in rels)

    for day in (date(2020, 1, 1), date(2025, 1, 1), today - timedelta(days=45), today - timedelta(days=120), today):
        day_dir = raw / day.strftime("%Y%m%d")
        expect = expected_action(day, today, data_days, shot_days)
        dir_planned = (day_dir.relative_to(PROJECT_ROOT).as_posix()) in rels
        shot_planned = planned_rel(day_dir / "screenshots")
        label = f"{day}(期望 {expect})"
        if expect == "dir":
            check(dir_planned, f"{label}:计划含**整目录**删除")
        elif expect == "data":
            check(not dir_planned, f"{label}:计划不含整个日期目录")
            check(planned_rel(day_dir / "raw") or planned_rel(day_dir / "html"), f"{label}:计划含 raw/html")
            check(not shot_planned, f"{label}:计划**不含截图**(90 天保留)")
        else:
            check(not planned_rel(day_dir), f"{label}:计划不含该目录任何内容")


def run_dry(settings: Settings, today: date, before: dict[str, tuple[int, int]]) -> cleanup.CleanupReport:
    print("\n[3] dry_run=True(一个字节都不许删)")
    report = asyncio.run(cleanup.run(settings, dry_run=True))
    print("  报告 JSON:")
    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    check(before == snapshot(VAR), "dry-run 后 var\\ 树指纹完全一致(未删任何文件)")
    check(report.freed_bytes == 0 and report.deleted == 0, "dry-run:freed_bytes=0 / deleted=0")
    check(report.planned_bytes > 0 and report.would_delete > 0, "dry-run 列出了待删项(would_delete>0)")
    check(not report.errors, f"dry-run 无错误(errors={report.errors})")
    rels = [item.path for item in report.items]
    check(not any("var/states/" in p for p in rels), "计划不含 var/states/(白名单)")
    check(not any("var/logs/" in p for p in rels), "计划不含 var/logs/(loguru owner)")
    check(not any("var/backup/" in p for p in rels), "计划不含 var/backup/(backup.py owner)")
    classify_plan(report, today, settings)
    assert_plan_is_scoped(report)
    return report


# ---------------------------------------------------------------------------
# 4. 实跑
# ---------------------------------------------------------------------------


def run_real(
    settings: Settings,
    today: date,
    config_before: dict[str, tuple[int, int]],
    whitelist_before: dict[str, dict[str, tuple[int, int]]],
) -> cleanup.CleanupReport:
    print("\n[4] dry_run=False(真删;范围已在上一步逐条断言)")
    report = asyncio.run(cleanup.run(settings, dry_run=False))
    print("  报告 JSON:")
    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))

    for path in planned(report):
        if path not in CREATED:
            abort(f"实跑删除了非本脚本创建的对象:{path}")
    check(report.deleted == report.would_delete, f"计划全部执行({report.deleted}/{report.would_delete})")
    check(report.freed_bytes > 0, f"释放了空间 freed_mb={report.freed_mb}")
    check(not report.errors, f"实跑无错误(errors={report.errors})")

    # ★ 白名单一个字节没变
    check(snapshot(CONFIG) == config_before, "config\\ 字节级未变(含 DO_NOT_DELETE.txt)")
    check(MARKER.exists(), "config\\DO_NOT_DELETE.txt 仍在")
    for label, snap in whitelist_before.items():
        check(snapshot(VAR / label) == snap, f"var/{label}/ 指纹未变({len(snap)} 个文件)")

    # ★ 双保留期结果
    raw = VAR / "raw" / HOTEL
    shots = VAR / "screenshots" / HOTEL
    key = (today - timedelta(days=45)).strftime("%Y%m%d")
    old_key = (today - timedelta(days=120)).strftime("%Y%m%d")
    today_key = today.strftime("%Y%m%d")
    check(not (raw / "20200101").exists(), "var/raw/测试酒店/20200101 已整删(远古)")
    check(not (raw / old_key).exists(), f"超 90 天目录 {old_key} 已整删")
    check((raw / key).is_dir(), f"窗口目录 {key} 仍在(只删数据)")
    check(not (raw / key / "raw").exists(), "窗口目录 raw/ 已删")
    check(not (raw / key / "html").exists(), "窗口目录 html/ 已删")
    check(not (raw / key / "api").exists(), "窗口目录 api/ 已删(属数据内容)")
    check((raw / key / "screenshots" / "keep.jpg").exists(), "★ 窗口目录内截图 keep.jpg **仍在**")
    check((raw / today_key / "c.json").exists(), "今天的数据仍在(未到期)")
    check((raw / today_key / "screenshots" / "shot.jpg").exists(), "今天的截图仍在")
    check((shots / key / "keep.jpg").exists(), f"var/screenshots 窗口目录 {key} 截图仍在")
    check(not (shots / old_key).exists(), f"var/screenshots 超 90 天目录 {old_key} 已删")

    # 平铺 / 报告
    check(not (VAR / "reports" / "报告_old.json").exists(), "过期报告文件已删")
    check((VAR / "reports" / "报告_new.json").exists(), "未过期报告文件保留")
    check((VAR / "reports" / "勿删(勿删).json").exists(), "★ 含「(勿删)」的报告文件保留")
    check(not (VAR / "legacy_dump.html").exists(), "var/ 平铺过期 html 已删")
    check((VAR / "keep_me.json").exists(), "var/ 平铺未过期 json 保留")
    check((VAR / "states_backup" / "trap.json").exists(), "var/states_backup/ 未被误伤(前缀相同也安全)")

    print("\n  清理后的树:")
    print("\n".join(tree_lines(VAR)))
    return report


# ---------------------------------------------------------------------------


def run_all_dry(settings: Settings, today: date) -> cleanup.CleanupReport:
    """``all_=True``(--all):忽略保留期,但**照样服从白名单**。"""
    print("\n[5] --all 的 dry-run(忽略保留期;白名单仍然生效)")
    report = asyncio.run(cleanup.run(settings, dry_run=True, all_=True))
    print("  报告 JSON:")
    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    rels = [item.path for item in report.items]
    check(
        not any(
            p.startswith(("var/states", "var/logs", "var/backup"))
            or p.startswith(("config", "db", "logs", "knowledge", "storage_states"))
            for p in rels
        ),
        "--all 计划不含任何白名单目录 / 他人 owner 目录",
    )
    check(
        not any(p.endswith(today.strftime("%Y%m%d")) for p in rels),
        "--all 仍不删「今天」的日期目录(截止线取今日 00:00)",
    )
    check(
        any(p.endswith((today - timedelta(days=45)).strftime("%Y%m%d")) for p in rels),
        "--all 把 [-90,-30) 窗口目录提升为整删",
    )
    check(report.dry_run and report.freed_bytes == 0, "--all 的 dry-run 同样一个字节不删")
    assert_plan_is_scoped(report)
    return report


def main() -> int:
    settings, today = preamble()
    dates = build_tree(today)
    config_before = snapshot(CONFIG)
    whitelist_before = {
        "states": snapshot(VAR / "states"),
        "logs": snapshot(VAR / "logs"),
        "backup": snapshot(VAR / "backup"),
        "states_backup": snapshot(VAR / "states_backup"),
    }
    before = snapshot(VAR)
    print(f"\n  造树后 var\\ 指纹: {digest(before)} (config 指纹={digest(config_before)})")
    print(f"  覆盖的日期目录: {sorted(d.isoformat() for d in dates)}")
    check_whitelist_matcher()
    run_dry(settings, today, before)
    run_real(settings, today, config_before, whitelist_before)
    run_all_dry(settings, today)

    print("\n" + "=" * 100)
    if FAILURES:
        print(f"结果:FAIL —— {len(FAILURES)} 条断言未通过")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("结果:全部断言通过(dry-run 安全 + 双保留期正确 + 白名单零改动 + --all 仍守白名单)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
