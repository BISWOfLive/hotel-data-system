"""把旧系统的登录态迁移到新布局(T2.1)。

**这是缓存迁移,不是数据迁移** —— 旧 ``storage_states/*.json`` 与根目录 3 个 json
**可以直接复制过来用,不必重新登录**(总纲 7.7)。

旧系统的「四种存法、三个位置」
------------------------------
=================================  ==========================================
旧位置                              新位置(由三元组推导)
=================================  ==========================================
``storage_states/<alias>.json``      ``var/states/ctrip__ebooking__<alias>.json``
``storage_state_ctrip.json``         ``var/states/ctrip__ota__ctrip.json``
``storage_state_meituan.json``       ``var/states/meituan__ota_meituan__meituan.json``
``storage_state.json``(旧单账号)     ``var/states/ctrip__ebooking__legacy.json``
DB 列 ``accounts.storage_state_path``  路径**不再入库**(由三元组推导)
=================================  ==========================================

用法::

    python scripts/migrate_states.py --from "D:\\...\\hotel-data-system" [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

# 允许从仓库根直接运行(无需 poetry install)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hoteldata.infra.session_store import (  # noqa: E402
    ROLE_EBOOKING,
    ROLE_OTA,
    ROLE_OTA_MEITUAN,
    SessionKey,
    SessionStore,
)
from hoteldata.logging import configure_stdio  # noqa: E402
from hoteldata.settings import get_settings  # noqa: E402

__all__ = ["build_plan", "main", "migrate"]


@dataclass
class MigrationPlan:
    """迁移计划(先算清楚再动手)。"""

    moves: list[tuple[Path, SessionKey]] = field(default_factory=list)
    skipped: list[tuple[Path, str]] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.moves)


#: 根目录固定文件 → 新三元组
_ROOT_FILES: tuple[tuple[str, SessionKey], ...] = (
    ("storage_state_ctrip.json", SessionKey("ctrip", ROLE_OTA, "ctrip")),
    ("storage_state_meituan.json", SessionKey("meituan", ROLE_OTA_MEITUAN, "meituan")),
    ("storage_state.json", SessionKey("ctrip", ROLE_EBOOKING, "legacy")),
)


def build_plan(old_root: Path) -> MigrationPlan:
    """扫描旧系统,产出迁移计划(不写任何文件)。"""
    plan = MigrationPlan()

    # ① storage_states/<alias>.json → ctrip__ebooking__<alias>.json
    states_dir = old_root / "storage_states"
    if states_dir.is_dir():
        for src in sorted(states_dir.glob("*.json")):
            alias = src.stem
            platform = "meituan" if alias.lower().startswith(("meituan", "mt")) else "ctrip"
            plan.moves.append((src, SessionKey(platform, ROLE_EBOOKING, alias)))

    # ② 根目录 3 个固定文件
    for name, key in _ROOT_FILES:
        src = old_root / name
        if src.exists():
            plan.moves.append((src, key))

    # ③ 去重(同一个目标只保留第一个来源)
    seen: set[tuple[str, str, str]] = set()
    deduped: list[tuple[Path, SessionKey]] = []
    for src, key in plan.moves:
        if key.pair in seen:
            plan.skipped.append((src, f"目标 {key} 已有来源"))
            continue
        seen.add(key.pair)
        if not _looks_like_state(src):
            plan.skipped.append((src, "不是合法 playwright storage_state(无 cookies 键)"))
            continue
        deduped.append((src, key))
    plan.moves = deduped
    return plan


def _looks_like_state(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False
    return isinstance(data, dict) and "cookies" in data


async def migrate(old_root: Path, *, dry_run: bool = False) -> int:
    settings = get_settings()
    settings.ensure_dirs()
    layout_store = SessionStore(settings)
    plan = build_plan(old_root)

    print(f"旧系统根目录: {old_root}")
    print(f"计划迁移 {plan.count} 个登录态;跳过 {len(plan.skipped)} 个")
    for src, reason in plan.skipped:
        print(f"  ! 跳过 {src.name}: {reason}")

    if plan.count == 0:
        print("没有可迁移的登录态。")
        return 1 if plan.skipped else 0

    db = None
    if not dry_run:
        from hoteldata.infra.db import Database

        db = Database(settings)
        layout_store = SessionStore(settings, db)

    for src, key in plan.moves:
        if dry_run:
            print(f"  [dry-run] {src}  →  {key}")
            continue
        data = json.loads(src.read_text(encoding="utf-8"))
        dst = layout_store.handle(key.platform, key.role, key.alias).save(data)
        cookies = len(data.get("cookies") or [])
        print(f"  ✓ {src.name}  →  {dst.name}  ({cookies} cookies)")
        await _register(db, key, src)

    if db is not None:
        await db.dispose()

    print()
    print("下一步:hoteldata sessions --check   # 逐个探活确认可用")
    return 0


async def _register(db: object, key: SessionKey, src: Path) -> None:
    """登记进 ``sessions`` 表(status=unknown,由探测更新)。"""
    if db is None:
        return
    from hoteldata.infra.session_store import SessionStore

    store = SessionStore(get_settings(), db)  # type: ignore[arg-type]
    await store.set_status(key, "unknown", file_mtime=src.stat().st_mtime)


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description="迁移旧系统登录态到新布局(var/states/)")
    parser.add_argument(
        "--from",
        dest="old_root",
        required=True,
        help="旧系统根目录(含 storage_states/ 与 storage_state*.json)",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印计划,不写文件")
    args = parser.parse_args(argv)

    old_root = Path(args.old_root).expanduser().resolve()
    if not old_root.is_dir():
        print(f"✗ 旧系统根目录不存在: {old_root}")
        return 2
    return asyncio.run(migrate(old_root, dry_run=args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
