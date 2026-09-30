"""接口录制工具(S1 对策)—— ``need_record=true`` 机制的配套。

**为什么需要它**
----------------
段1 的 API 直连通道依赖 ``config/api_rules.json`` 里那些**实测捕获值**:
``clientId`` / ``fp`` / ``vid`` / 514 字 ``rmsToken`` / 各接口的 params 与 body 模板。
这些**不可推导**,只能录下来。

当平台改签名/加指纹时,``need_record=true`` 会让未校准的接口**直接降级浏览器**
(不猜)。而这个脚本负责把新接口重新录出来,让人工核对后回填规则。

它做三件事:
  1. 开一个真浏览器(复用登录态),挂上响应监听;
  2. 你手工在页面里点几下,脚本把命中的请求 **URL / method / headers / params / body /
     响应体** 落成一份 JSON;
  3. 输出可直接粘贴进 ``api_rules.json`` 的 ``api_defs[]`` 草稿(带 ``need_record: true``
     —— **草稿默认未校准**,人工核对后再改成 false)。

用法::

    .venv\\Scripts\\python.exe scripts\\record_apis.py --alias ctrip001 --out var/raw/recording.json
    # 浏览器打开后,在页面里操作;按 Ctrl+C 结束并落盘
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hoteldata.logging import configure_stdio  # noqa: E402
from hoteldata.settings import get_settings  # noqa: E402

__all__ = ["main", "record"]

#: 只录这些内容的接口(过滤静态资源,避免录一堆 .js/.css)
SKIP_SUBSTRINGS = (
    ".js",
    ".css",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".woff",
    ".woff2",
    ".ttf",
    ".ico",
    ".map",
    "sockjs",
    "websocket",
    "hotel.gif",
)
#: 只录这些域的请求
DEFAULT_HOSTS = ("ebooking.ctrip.com", "ebkgrowth.ctrip.com", "toolcenter")
#: 响应体超过这个长度就截断(避免把整页 HTML 录进去)
MAX_BODY_CHARS = 200_000


def _should_record(url: str, hosts: tuple[str, ...]) -> bool:
    low = url.lower()
    if any(s in low for s in SKIP_SUBSTRINGS):
        return False
    if not any(h in low for h in hosts):
        return False
    return True


def _parse_maybe_json(text: str) -> Any:
    if not text:
        return None
    stripped = text.lstrip()
    if not stripped.startswith(("{", "[")):
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _draft_api_def(rec: dict[str, Any]) -> dict[str, Any]:
    """把一条录制记录转成 ``api_defs[]`` 草稿。

    ★ **默认 ``need_record: true``** —— 草稿是"未校准"的,必须人工核对字段路径后
    再改成 ``false``。这正是 S1 的对策:未校准的接口直接降级浏览器,不猜。
    """
    def_name = rec["url"].rstrip("/").rsplit("/", 1)[-1].split("?")[0] or "api"
    body = rec.get("request_body")
    params = rec.get("request_params") or None
    if isinstance(body, str):
        parsed = _parse_maybe_json(body)
        if parsed is None and "=" in body and "&" in body:
            from urllib.parse import parse_qsl

            parsed = dict(parse_qsl(body))
        body = parsed
    draft: dict[str, Any] = {
        "name": def_name,
        "method": rec["method"],
        "url": rec["url"].split("?")[0],
        "need_record": True,  # ← 人工核对后改 false
        "note": f"录制于 {rec['recorded_at']};原 URL 含 query,参数见 params",
    }
    if params:
        draft["params"] = params
    if body:
        draft["body"] = body
    return draft


async def record(
    alias: str,
    *,
    role: str = "ebooking",
    out: Path,
    hosts: tuple[str, ...] = DEFAULT_HOSTS,
    entry: str | None = None,
    max_seconds: float | None = None,
) -> int:
    settings = get_settings()
    settings.ensure_dirs()

    from hoteldata.infra.browser import BrowserPool
    from hoteldata.infra.session_store import SessionStore

    store = SessionStore(settings)
    handle = store.handle_for_alias(alias, role) if role == "ebooking" else store.handle("ctrip", role, alias)
    if not handle.exists():
        print(f"✗ 登录态不存在: {handle.storage_state_path()}")
        print("  先运行:hoteldata login --platform ctrip --alias " + alias)
        return 2

    pool = BrowserPool(settings)
    records: list[dict[str, Any]] = []
    started = time.monotonic()

    def on_response(resp: Any) -> None:
        try:
            url = resp.url
            if not _should_record(url, hosts):
                return
            req = resp.request
            rec = {
                "recorded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "url": url,
                "method": req.method,
                "status": resp.status,
                "resource_type": req.resource_type,
                "request_headers": dict(req.headers),
                "request_params": _query_to_dict(url),
                "request_body": req.post_data,
                "response_headers": dict(resp.headers),
            }

            def _fill(body_text: str) -> None:
                rec["response_body"] = (body_text or "")[:MAX_BODY_CHARS]
                parsed = _parse_maybe_json(rec["response_body"])
                rec["response_json"] = parsed
                records.append(rec)
                print(f"  录到 {rec['method']:4s} {resp.status} {url[:110]}")

            asyncio.create_task(_capture(resp, _fill))
        except Exception as exc:  # noqa: BLE001 - 录制不该影响页面
            print(f"  ! 录制回调异常: {exc}")

    print(f"登录态: {handle.storage_state_path()}")
    print("浏览器已打开。请在页面里正常操作(点击/切页签),脚本会记录命中的接口。")
    print("结束时按 Ctrl+C。\n")
    try:
        async with pool.page_session(handle) as (_b, _c, page):
            page.on("response", on_response)
            target = entry or "https://ebooking.ctrip.com/"
            await page.goto(target, wait_until="domcontentloaded")
            deadline = (time.monotonic() + max_seconds) if max_seconds else None
            while deadline is None or time.monotonic() < deadline:
                await asyncio.sleep(0.5)
    except KeyboardInterrupt, asyncio.CancelledError:
        pass
    finally:
        with suppress(Exception):
            await pool.close()

    elapsed = round(time.monotonic() - started, 1)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "alias": alias,
        "role": role,
        "elapsed_s": elapsed,
        "count": len(records),
        "records": records,
        "api_defs_draft": _dedupe_drafts(records),
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✓ 录制 {len(records)} 条接口 → {out}")
    print(f"  可粘贴进 api_rules.json 的 api_defs 草稿 {len(payload['api_defs_draft'])} 条")
    print("  ⚠️ 草稿默认 need_record=true(未校准)。人工核对 fields[].path 后再改 false。")
    return 0


async def _capture(resp: Any, sink: Any) -> None:
    try:
        text = await resp.text()
    except Exception:  # noqa: BLE001
        text = ""
    sink(text)


def _query_to_dict(url: str) -> dict[str, str]:
    from urllib.parse import parse_qsl, urlsplit

    return dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))


def _dedupe_drafts(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for rec in records:
        if not rec.get("response_json"):
            continue  # 只要 JSON 接口
        key = (rec["method"], rec["url"].split("?")[0])
        if key in seen:
            continue
        seen.add(key)
        out.append(_draft_api_def(rec))
    return out


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description="录制 eBooking 内部接口(产出 api_defs 草稿)")
    parser.add_argument("--alias", required=True, help="账号别名,如 ctrip001")
    parser.add_argument("--role", default="ebooking", help="ebooking | ota")
    parser.add_argument("--out", default="var/raw/recording.json", help="输出 JSON 路径")
    parser.add_argument("--entry", default=None, help="起始 URL(默认 eBooking 首页)")
    parser.add_argument("--seconds", type=float, default=None, help="自动停止秒数(默认手动 Ctrl+C)")
    parser.add_argument("--hosts", nargs="*", default=list(DEFAULT_HOSTS), help="只录这些域")
    args = parser.parse_args(argv)

    try:
        return asyncio.run(
            record(
                args.alias,
                role=args.role,
                out=Path(args.out),
                hosts=tuple(args.hosts),
                entry=args.entry,
                max_seconds=args.seconds,
            )
        )
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
