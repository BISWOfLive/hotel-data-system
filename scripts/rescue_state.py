"""抢救:从仍在运行的 ``login`` 进程手里抢下**尚未被回滚**的有效登录态。

背景
----
``_commit_state()`` 的顺序是「写文件 → 探活 → 不通过则回滚」。
在探活失败(旧代码缺 ``x-requested-with`` 头,必失败)的情况下,
**有效登录态仍然会短暂地写在文件里**,大约几百毫秒后才被旧文件覆盖回去。

本脚本高频轮询该文件,一旦发现它与"已知旧态"不同就立刻抓下来,
再用**修好的**探活验证。这样**不用重新登录**。

用法::

    .venv\\Scripts\\python.exe scripts\\rescue_state.py --alias ctrip001 [--seconds 120]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hoteldata.logging import configure_stdio  # noqa: E402
from hoteldata.settings import get_settings  # noqa: E402

__all__ = ["main", "rescue"]

#: 抓到的候选态先存这里,验证通过后才覆盖正式文件
RESCUE_SUFFIX = ".rescued"


def _fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def _auth_names(state: dict) -> list[str]:
    names = {c.get("name") for c in (state.get("cookies") or []) if isinstance(c, dict)}
    return sorted(names & {"usertoken", "usersign", "randomkey", "imislogin"})


async def rescue(alias: str, *, seconds: float, poll_ms: int) -> int:
    settings = get_settings()
    handle = (
        __import__("hoteldata.infra.session_store", fromlist=["SessionStore"])
        .SessionStore(settings)
        .handle("ctrip", "ebooking", alias)
    )
    path = handle.storage_state_path()
    if not path.exists():
        print(f"✗ 登录态文件不存在: {path}")
        return 2

    baseline = path.read_bytes()
    base_fp = _fingerprint(baseline)
    base_state = json.loads(baseline.decode("utf-8"))
    print(f"监视 {path.name}")
    print(f"  基线指纹 {base_fp}  大小 {len(baseline)}B  cookies {len(base_state.get('cookies') or [])}")
    print(f"  高频轮询 {poll_ms}ms,最多等 {seconds:.0f} 秒…\n")

    deadline = time.monotonic() + seconds
    seen: dict[str, int] = {}
    caught: bytes | None = None
    while time.monotonic() < deadline:
        try:
            raw = path.read_bytes()
        except OSError, PermissionError:
            await asyncio.sleep(poll_ms / 1000)
            continue
        fp = _fingerprint(raw)
        if fp == base_fp:
            await asyncio.sleep(poll_ms / 1000)
            continue
        seen[fp] = seen.get(fp, 0) + 1
        try:
            state = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError, UnicodeDecodeError:
            await asyncio.sleep(poll_ms / 1000)
            continue
        auth = _auth_names(state)
        n = len(state.get("cookies") or [])
        # 只要不是基线,就当作候选;有鉴权 cookie 的优先立刻落袋
        if caught is None or len(auth) >= len(_auth_names(json.loads(caught.decode("utf-8")))):
            caught = raw
            print(f"  抓到一个候选: {len(raw)}B  cookies={n}  鉴权={auth}  fp={fp}")
        if auth and len(raw) > len(baseline) * 0.5:
            # 有完整鉴权 cookie 且足够大 → 就是它了
            break
        await asyncio.sleep(poll_ms / 1000)

    if caught is None:
        print("\n✗ 没抓到候选。(login 进程可能已退出,或这次没进业务页)")
        print("  → 请改用:hoteldata login --platform ctrip --alias " + alias)
        return 1

    rescued = path.with_name(path.name + RESCUE_SUFFIX)
    rescued.write_bytes(caught)
    state = json.loads(caught.decode("utf-8"))
    print(f"\n候选已另存: {rescued.name}  ({len(caught)}B, cookies={len(state.get('cookies') or [])})")

    # ★ 用**修好的**探活验证候选:先把候选就位,再探活
    backup = path.with_name(path.name + ".pre-rescue")
    shutil.copy2(path, backup)
    path.write_bytes(caught)
    from hoteldata.domains.session.manager import LoginManager
    from hoteldata.infra.session_store import SessionStore

    mgr = LoginManager(settings, sessions=SessionStore(settings))
    ok = await mgr.probe_login_valid(handle)
    print(f"修好的探活结果 = {'VALID ✓' if ok else 'INVALID'}")
    if ok:
        backup.unlink(missing_ok=True)
        rescued.unlink(missing_ok=True)
        print("\n✓ 抢救成功!有效登录态已就位,不必重新登录。")
        print("  下一步:hoteldata sessions --check")
        return 0
    shutil.move(str(backup), str(path))
    print(f"\n✗ 候选未通过探活(仍是失效态)。候选留在 {rescued.name} 供排查。")
    print("  → 请重新登录:hoteldata login --platform ctrip --alias " + alias)
    return 1


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    p = argparse.ArgumentParser(description="抢救被回滚的有效登录态")
    p.add_argument("--alias", default="ctrip001")
    p.add_argument("--seconds", type=float, default=120.0)
    p.add_argument("--poll-ms", type=int, default=15)
    a = p.parse_args(argv)
    return asyncio.run(rescue(a.alias, seconds=a.seconds, poll_ms=a.poll_ms))


if __name__ == "__main__":
    raise SystemExit(main())
