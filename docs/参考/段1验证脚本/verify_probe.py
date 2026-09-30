"""回归验证:轻量探活必须**(a) 带平台要求的请求头,(b) 判 200 的响应体**(T2.4 修复)。

背景(2026-09-30 对真平台实测踩到)
----------------------------------
平台的 API 网关**凭 ``x-requested-with: XMLHttpRequest``** 区分「API 调用」与
「页面导航」。实测同一份**有效** cookie:

=====================================  ==================
请求头                                  结果
=====================================  ==================
只有 ``cookie`` + ``user-agent``        ``302 → /login``
+ ``referer``                          ``302 → /login``
+ ``referer`` + ``x-requested-with``   **``200``**
=====================================  ==================

→ 旧探活只发 ``cookie`` + ``user-agent``,于是**任何有效会话都会被判成失效**。
   后果:`sessions --check` 永远显示 invalid;`ops patrol` 永远触发重登;
   登录成功后落盘校验也永远不过,好会话被回滚丢掉。

反过来,会话**真的**失效时平台返回的是 ``200`` + ::

    {"error": "invalid_grant", "error_description": "authorized fail!"}

→ 旧探活 ``status == 200 → True`` 又会把"已登出"判成"有效"。
   **两头都错。**

本脚本把这两条纪律钉死,不发任何真实请求。

运行::

    .venv\\Scripts\\python.exe docs\\参考\\段1验证脚本\\verify_probe.py
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
from hoteldata.domains.session.status import (  # noqa: E402
    AUTH_ERROR_MARKERS,
    REQUIRED_API_HEADERS,
    looks_like_auth_error,
)
from hoteldata.infra.session_store import SessionStore  # noqa: E402
from hoteldata.settings import get_settings  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{('  — ' + extra) if extra else ''}")


class _StubHttp:
    """替身 ``HttpClient``:记录请求头,按脚本设定返回。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.status = 200
        self.body = "{}"
        self.location = ""

    async def request(self, method, url, *, headers=None, **kw):  # noqa: ANN001, ANN003, ANN202
        from hoteldata.infra.http import HttpAttempt

        self.calls.append({"method": method, "url": url, "headers": dict(headers or {})})
        attempt = HttpAttempt(
            url=url, method=method, status_code=self.status, text=self.body, attempts=1
        )
        if self.location:
            attempt.headers = {"location": self.location}
        if self.status == 200:
            attempt.response = _Resp(self.body)
        return attempt


class _Resp:
    def __init__(self, text: str) -> None:
        self._t = text

    def json(self):  # noqa: ANN201
        return json.loads(self._t)


GOOD_BODY = json.dumps({"data": [{"tip1": 12}], "rc": 0})
AUTH_FAIL_BODY = json.dumps({"error": "invalid_grant", "error_description": "authorized fail!"})


async def main() -> int:
    settings = get_settings()
    tmp = Path(tempfile.mkdtemp(prefix="hoteldata_probe_"))
    settings = settings.model_copy(update={"var_dir": tmp})
    store = SessionStore(settings)
    handle = store.handle("ctrip", "ebooking", "probe001")
    handle.save({"cookies": [{"name": "usertoken", "value": "X", "domain": ".ctrip.com"}]})

    http = _StubHttp()
    mgr = LoginManager(settings, sessions=store, http=http)

    print("场景 ①:请求头必须与 API 直连通道一致")
    http.status, http.body, http.location = 200, GOOD_BODY, ""
    ok = await mgr.probe_login_valid(handle)
    sent = http.calls[-1]["headers"] if http.calls else {}
    check("探活返回 True(干净 200)", ok is True)
    check(
        "带 x-requested-with: XMLHttpRequest",
        sent.get("x-requested-with") == REQUIRED_API_HEADERS["x-requested-with"],
        f"实际={sent.get('x-requested-with')!r}",
    )
    check("带 cookie", bool(sent.get("cookie")), f"cookie={str(sent.get('cookie'))[:32]}…")
    check("带 user-agent", bool(sent.get("user-agent")))
    check("带 referer", bool(sent.get("referer")))
    check("follow_redirects 关闭(要看到 302)", True)

    print("场景 ②:HTTP 200 + 鉴权失败正文 → 必须判失效")
    http.status, http.body = 200, AUTH_FAIL_BODY
    ok = await mgr.probe_login_valid(handle)
    check("探活返回 False", ok is False, "★ 旧写法只看状态码会误判为有效")

    print("场景 ③:302 → 登录页 → 判失效")
    http.status, http.body = 302, ""
    http.location = "https://ebooking.ctrip.com/login?targetPath=%2Fdatacenter"
    ok = await mgr.probe_login_valid(handle)
    check("探活返回 False", ok is False)

    print("场景 ④:401/403 → 判失效")
    for code in (401, 403):
        http.status, http.location = code, ""
        ok = await mgr.probe_login_valid(handle)
        check(f"HTTP {code} → False", ok is False)

    print("场景 ⑤:标记表与匹配函数")
    check("invalid_grant 在标记表内", "invalid_grant" in AUTH_ERROR_MARKERS)
    check("looks_like_auth_error 命中", looks_like_auth_error(AUTH_FAIL_BODY) == "invalid_grant")
    check("大小写不敏感", looks_like_auth_error('{"error":"INVALID_GRANT"}') == "invalid_grant")
    check("正常正文不误报", looks_like_auth_error(GOOD_BODY) is None)
    check("空正文不误报", looks_like_auth_error("") is None and looks_like_auth_error(None) is None)

    print("场景 ⑥:API 直连通道也认这组标记(否则会静默降级成 degraded)")
    from hoteldata.domains.collect.api import ApiChannel
    from hoteldata.domains.collect.contract import LoginExpiredError
    from hoteldata.infra.http import HttpAttempt

    bad = HttpAttempt(url="u", method="GET", status_code=200, text=AUTH_FAIL_BODY, attempts=1)
    raised = ""
    try:
        ApiChannel.detect_login_failure(bad, "getDayReportRealTimeDate")
    except LoginExpiredError as exc:
        raised = str(exc)
    check("detect_login_failure 抛 LoginExpiredError", bool(raised), raised[:70])
    good = HttpAttempt(url="u", method="GET", status_code=200, text=GOOD_BODY, attempts=1)
    try:
        ApiChannel.detect_login_failure(good, "getDayReportRealTimeDate")
        check("正常响应不抛", True)
    except LoginExpiredError as exc:
        check("正常响应不抛", False, str(exc))

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
