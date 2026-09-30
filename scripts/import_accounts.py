"""从旧系统 ``db/ebooking.db`` 导入账号与酒店(R7 对策 / 段1 联调必需)。

总纲 R7 说「账号/机器人/酒店/群绑定需重新录入」,并注明"``db/*.db`` 原样保留
以便随时补迁"。本脚本就是那个"补迁"入口 —— 没有它,段1 连一个可采集的
``(酒店, 账号)`` 组合都没有,``hoteldata collect`` 会一直报"没有可采集的酒店"。

**只读旧库**(``mode=ro``);凭据用**新项目的 Fernet 密钥重新加密**后入库
(旧密钥已继承到 ``config/secret.key``,所以能解开旧密文)。

用法::

    python scripts/import_accounts.py --from "D:\\...\\hotel-data-system" [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hoteldata.infra.crypto import get_cipher  # noqa: E402
from hoteldata.logging import configure_stdio  # noqa: E402
from hoteldata.settings import get_settings  # noqa: E402

__all__ = ["import_all", "main"]


@dataclass
class ImportPlan:
    accounts: list[dict] = field(default_factory=list)
    hotels: list[dict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def read_old(db_path: Path) -> ImportPlan:
    """只读旧库,读出账号与酒店。"""
    plan = ImportPlan()
    if not db_path.exists():
        plan.problems.append(f"旧库不存在: {db_path}")
        return plan
    conn = _connect_ro(db_path)
    try:
        tables = {r[0] for r in conn.execute("select name from sqlite_master where type='table'").fetchall()}
        if "accounts" in tables:
            # ⚠️ 旧 accounts 表**没有 platform 列**(实测只有 id/alias/username_enc/
            #    password_enc/storage_state_path/status/is_multi/last_login_at/
            #    last_check_at/remark/created_at),平台只能按别名前缀推断。
            cols = {r[1] for r in conn.execute("pragma table_info(accounts)")}
            want = [
                "id",
                "alias",
                "platform",
                "username_enc",
                "password_enc",
                "status",
                "is_multi",
                "remark",
            ]
            picked = [c for c in want if c in cols]
            for row in conn.execute(f"select {', '.join(picked)} from accounts order by alias"):
                plan.accounts.append(dict(row))
        else:
            plan.problems.append("旧库无 accounts 表")
        if "hotels" in tables:
            cols = {r[1] for r in conn.execute("pragma table_info(hotels)")}
            want = ["name", "city", "account_id", "ebk_hotel_id", "status"]
            picked = [c for c in want if c in cols]
            for row in conn.execute(f"select {', '.join(picked)} from hotels order by id"):
                plan.hotels.append(dict(row))
        else:
            plan.problems.append("旧库无 hotels 表")
    finally:
        conn.close()
    return plan


def _decode(value: str) -> str:
    """旧库里的日期存法之一是 ISO+微秒;``status`` 直接照搬。"""
    return (value or "").strip()


def _platform_of(alias: str) -> str:
    """按别名前缀推断平台(旧库没有 platform 列)。

    ``SessionStore.platform_for_alias`` 对无法识别的别名会抛错;导入是批量操作,
    这里**保守退化为 ctrip** 并交由人工核对(4 个旧账号全是 ``ctrip00N``)。
    """
    a = (alias or "").strip().lower()
    if a.startswith(("meituan", "mt")):
        return "meituan"
    return "ctrip"


async def import_all(old_root: Path, *, dry_run: bool = False, force: bool = False) -> int:
    settings = get_settings()
    settings.ensure_dirs()
    db_path = old_root / "db" / "ebooking.db"
    plan = read_old(db_path)
    for p in plan.problems:
        print(f"! {p}")
    if not plan.accounts:
        print("没有可导入的账号。")
        return 1

    cipher = get_cipher(settings)
    ok_creds = warn_creds = 0
    for acc in plan.accounts:
        try:
            cipher.decrypt(_decode(acc["username_enc"]))
            cipher.decrypt(_decode(acc["password_enc"]))
            ok_creds += 1
        except Exception:  # noqa: BLE001
            warn_creds += 1

    print(f"旧系统: {db_path}")
    print(f"  账号 {len(plan.accounts)} 个(凭据可解密 {ok_creds},不可解密 {warn_creds})")
    print(f"  酒店 {len(plan.hotels)} 家")
    if dry_run:
        for acc in plan.accounts:
            plat = acc.get("platform") or _platform_of(str(acc["alias"]))
            print(
                f"  [dry-run] account {acc['alias']} platform={plat} "
                f"(推断) status={acc.get('status')} 需登录态 storage_states\\{acc['alias']}.json"
            )
        for h in plan.hotels:
            print(
                f"  [dry-run] hotel {h['name']} ebk={h.get('ebk_hotel_id')} account_id={h.get('account_id')}"
            )
        return 0

    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from hoteldata.infra.db import Database
    from hoteldata.infra.models import Account, Hotel

    db = Database(settings)
    # 旧 account_id → 新 account_id
    id_map: dict[int, int] = {}
    created = updated = 0
    async with db.session() as s:
        for acc in plan.accounts:
            alias = str(acc["alias"])
            existing = (await s.execute(select(Account).where(Account.alias == alias))).scalar_one_or_none()
            # 凭据原样复制(旧 Fernet 密文用同一把密钥,可直接解 → 重新加密保证与 kid 策略一致)
            try:
                username = cipher.decrypt(_decode(acc["username_enc"]))
                password = cipher.decrypt(_decode(acc["password_enc"]))
            except Exception:  # noqa: BLE001
                print(f"  ! {alias} 凭据解密失败,跳过(需人工重录)")
                continue
            values = {
                "alias": alias,
                # 旧库无 platform 列 → 按别名前缀推断(ctrip* → ctrip;meituan* → meituan)
                "platform": str(acc.get("platform") or _platform_of(alias)),
                "username_enc": cipher.encrypt(username),
                "password_enc": cipher.encrypt(password),
                "status": str(acc["status"] or "active"),
                "is_multi": bool(acc.get("is_multi") or False),
                "remark": acc.get("remark"),
            }
            if existing is None:
                row = Account(**values)
                s.add(row)
                await s.flush()
                id_map[int(acc.get("id") or len(id_map))] = row.id
                created += 1
            else:
                for k, v in values.items():
                    setattr(existing, k, v)
                updated += 1
        await s.flush()

        # 酒店:按名称幂等
        for h in plan.hotels:
            name = str(h["name"])
            existing_h = (await s.execute(select(Hotel).where(Hotel.name == name))).scalar_one_or_none()
            old_acc_id = h.get("account_id")
            new_acc_id = id_map.get(int(old_acc_id)) if old_acc_id is not None else None
            if new_acc_id is None and existing_h is not None:
                new_acc_id = existing_h.account_id
            values_h = {
                "name": name,
                "city": h.get("city"),
                "ebk_hotel_id": (str(h["ebk_hotel_id"]) if h.get("ebk_hotel_id") else None),
                "status": str(h.get("status") or "active"),
                "account_id": new_acc_id,
            }
            if existing_h is None:
                s.add(Hotel(**values_h))
            else:
                for k, v in values_h.items():
                    setattr(existing_h, k, v)
    await db.dispose()
    print(f"✓ 导入完成:账号新建 {created} / 更新 {updated};酒店 {len(plan.hotels)} 家已对齐")
    print("下一步:python scripts/migrate_states.py --from <旧系统根目录>")
    _ = (pg_insert, force)
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description="从旧系统 ebooking.db 导入账号与酒店")
    parser.add_argument("--from", dest="old_root", required=True, help="旧系统根目录")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="已存在也覆盖")
    args = parser.parse_args(argv)
    old_root = Path(args.old_root).expanduser().resolve()
    if not old_root.is_dir():
        print(f"✗ 目录不存在: {old_root}")
        return 2
    return asyncio.run(import_all(old_root, dry_run=args.dry_run, force=args.force))


if __name__ == "__main__":
    raise SystemExit(main())
