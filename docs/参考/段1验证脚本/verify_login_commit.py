"""回归验证:登录态落盘必须**先探活、不通过就回滚**(T2.4 修复)。

背景(2026-09-30 实测踩到)
--------------------------
旧写法只看"页面正文里有没有业务关键词"就宣布登录成功,并**直接覆盖登录态文件**。
而 eBooking 的登录是多步的(账号 → 验证码/滑块 → 下发 ``usertoken`` /
``usersign`` / ``randomkey`` / ``imislogin``)。**中间态页面看起来也像登录了** ——
于是写出一个"有 ``w_tuid`` 却没有 ``usertoken``"的假登录态,还把旧文件覆盖掉。
症状:命令打印"登录成功"、文件看着有内容、**接口全部 302**。

本脚本用桩把这条纪律钉死,不需要真账号、不发任何真实登录请求。

运行::

    .venv\\Scripts\\python.exe docs\\参考\\段1验证脚本\\verify_login_commit.py
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from hoteldata.domains.session.manager import LoginManager  # noqa: E402
from hoteldata.domains.session.status import ActionCode  # noqa: E402
from hoteldata.infra.session_store import SessionStore  # noqa: E402
from hoteldata.settings import get_settings  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{('  — ' + extra) if extra else ''}")


class _FakeContext:
    """只实现 ``storage_state()``。"""

    def __init__(self, state: dict) -> None:
        self._state = state

    async def storage_state(self) -> dict:
        return self._state


OLD_STATE = {"cookies": [{"name": "usertoken", "value": "OLD", "domain": ".ctrip.com"}], "origins": []}
# 中间态:有 w_tuid 但**没有** usertoken/usersign
FAKE_STATE = {
    "cookies": [{"name": "w_tuid", "value": "X" * 249, "domain": "ebooking.ctrip.com"}],
    "origins": [{"origin": "https://ebooking.ctrip.com"}],
}
REAL_STATE = {
    "cookies": [
        {"name": "usertoken", "value": "NEW", "domain": ".ctrip.com"},
        {"name": "usersign", "value": "S" * 546, "domain": ".ctrip.com"},
        {"name": "imislogin", "value": "true", "domain": ".ctrip.com"},
    ],
    "origins": [],
}


async def main() -> int:
    settings = get_settings()
    tmp = Path(tempfile.mkdtemp(prefix="hoteldata_login_commit_"))
    # 把 states 目录指到临时目录 —— 绝不碰真实的 var/states/
    settings = settings.model_copy(update={"var_dir": tmp})

    store = SessionStore(settings)
    handle = store.handle("ctrip", "ebooking", "ctrip001")
    handle.storage_state_path().parent.mkdir(parents=True, exist_ok=True)
    backup_path = handle.storage_state_path().with_name(handle.storage_state_path().name + ".bak")

    mgr = LoginManager(settings, sessions=store)
    calls = {"probe": 0}

    async def fake_probe(h, **kw):  # noqa: ANN001, ANN202
        calls["probe"] += 1
        return fake_probe.result  # type: ignore[attr-defined]

    mgr.probe_login_valid = fake_probe  # type: ignore[method-assign]

    print("场景 ①:页面像登录了,但**探活不通过** → 必须回滚,不写假登录态")
    handle.save(OLD_STATE)
    before = json.loads(handle.storage_state_path().read_text(encoding="utf-8"))
    fake_probe.result = False  # type: ignore[attr-defined]
    attempt = await mgr._commit_state(_FakeContext(FAKE_STATE), handle, reason="测试:假登录态")
    after = json.loads(handle.storage_state_path().read_text(encoding="utf-8"))
    check("返回值是 None(不宣布成功)", attempt is None)
    check("探活确实被调用", calls["probe"] >= 1, f"calls={calls['probe']}")
    check(
        "旧登录态被完整回滚",
        after == before,
        f"cookies={[c['name'] for c in after.get('cookies') or []]}",
    )
    check("没有留下 .bak 残留", not backup_path.exists())

    print("场景 ②:原本没有登录态文件 + 探活不通过 → 不得留下任何文件")
    handle.storage_state_path().unlink(missing_ok=True)
    fake_probe.result = False  # type: ignore[attr-defined]
    attempt = await mgr._commit_state(_FakeContext(FAKE_STATE), handle, reason="测试:无旧文件")
    check("返回值是 None", attempt is None)
    check("不留下假登录态文件", not handle.storage_state_path().exists())

    print("场景 ③:探活通过 → 落盘并清掉备份")
    handle.save(OLD_STATE)
    fake_probe.result = True  # type: ignore[attr-defined]
    attempt = await mgr._commit_state(_FakeContext(REAL_STATE), handle, reason="测试:真登录")
    final = json.loads(handle.storage_state_path().read_text(encoding="utf-8"))
    names = [c["name"] for c in final.get("cookies") or []]
    check("返回 ActionCode.OK", attempt is not None and attempt.code is ActionCode.OK)
    check("新登录态已落盘", "usertoken" in names and "usersign" in names, f"cookies={names}")
    check("备份已清理", not backup_path.exists())
    check("cookie 计数正确", attempt is not None and attempt.cookies == 3)

    shutil.rmtree(tmp, ignore_errors=True)
    print()
    print(f"合计 {len(PASS) + len(FAIL)} 项:PASS {len(PASS)} / FAIL {len(FAIL)}")
    if FAIL:
        print("FAIL:", FAIL)
    return 1 if FAIL else 0


if __name__ == "__main__":
    from hoteldata.logging import configure_stdio

    configure_stdio()
    raise SystemExit(asyncio.run(main()))
