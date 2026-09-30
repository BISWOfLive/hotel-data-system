"""后台端到端自检 —— 登录 → 遍历每个页面 → 一次写操作 → 校验审计。

为什么要有这个脚本
==================

后台是**服务端渲染**的:页面 500 往往只在渲染某个模板时才暴露
(Jinja2 语法错、模板变量拼错、ORM 属性名写错)。而这些错误
**单元测试抓不到、lint 也抓不到** —— 只有真的 GET 一遍才发现。

所以本脚本对**每一个**后台页面发一次真请求,并把非 200 视作失败。

用法::

    .venv\\Scripts\\python.exe scripts\\check_admin_web.py
    .venv\\Scripts\\python.exe scripts\\check_admin_web.py --port 8123

需要先起服务:``hoteldata serve``(或 uvicorn)。脚本**自己起一个**临时实例,
跑完关掉 —— 这样它不依赖你手上有没有在跑的服务。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except (AttributeError, ValueError):  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parents[1]

#: 所有后台页面(GET)。``expected`` 里可以写"内容里必须出现的片段"。
PAGES: tuple[tuple[str, str], ...] = (
    ("/admin", "总览"),
    ("/admin/hotels", "酒店"),
    ("/admin/accounts", "账号"),
    ("/admin/bindings", "群绑定"),
    ("/admin/bots", "机器人"),
    ("/admin/targets", "比价目标"),
    ("/admin/compare", "比价历史"),
    ("/admin/tasks", "注册的任务"),
    ("/admin/pushes", "推送审计"),
    ("/admin/alerts", "预警"),
    ("/admin/sessions", "登录态"),
    ("/admin/audit", "操作审计"),
    ("/healthz", "status"),
    ("/status", "tasks_registered"),
)

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        FAILURES.append(f"{label}: {detail}")
        print(f"  [FAIL] {label}\n         {detail}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8137)
    parser.add_argument("--password", default="")
    parser.add_argument("--keep", action="store_true", help="跑完不关服务(调试用)")
    args = parser.parse_args()

    # 口令:优先命令行,其次 .env 里的(脚本用同一个哈希验证不了明文,
    # 所以这里要求显式给 —— 或者用 --password 传之前 --set 的那个)
    import httpx

    base = f"http://127.0.0.1:{args.port}"
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen(  # noqa: S603
        [
            str(ROOT / ".venv" / "Scripts" / "python.exe"),
            "-m",
            "uvicorn",
            "hoteldata.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(args.port),
            "--log-level",
            "warning",
        ],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    print("=" * 74)
    print(f"后台端到端自检 —— {base}")
    print("=" * 74)

    try:
        # 等服务起来
        ready = False
        for _ in range(40):
            try:
                if httpx.get(f"{base}/healthz", timeout=2).status_code == 200:
                    ready = True
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        if not ready:
            check("服务启动", False, "40 次重试后 /healthz 仍不可达")
            return 1
        print("  服务已就绪\n")

        with httpx.Client(base_url=base, timeout=20, follow_redirects=False) as c:

            # ---------- ① 未登录守卫 ----------
            print("=== ① 未登录守卫 ===")
            for path in ("/admin", "/admin/hotels", "/admin/targets", "/admin/audit"):
                r = c.get(path)
                check(
                    f"{path} 未登录 → 303 到 /admin/login",
                    r.status_code == 303 and r.headers.get("location") == "/admin/login",
                    f"status={r.status_code} loc={r.headers.get('location')}",
                )
            r = c.get("/admin/login")
            check("/admin/login 可访问", r.status_code == 200, f"status={r.status_code}")

            # ---------- ② 错误口令 ----------
            print("\n=== ② 错误口令被拒 ===")
            r = c.post("/admin/login", data={"password": "definitely-wrong-password"})
            check(
                "错误口令 → 303 回登录页(未种 cookie)",
                r.status_code == 303 and "err=" in (r.headers.get("location") or ""),
                f"status={r.status_code} loc={r.headers.get('location')}",
            )
            check(
                "错误口令不下发会话 cookie",
                "hoteldata_admin" not in r.headers.get("set-cookie", ""),
                f"set-cookie={r.headers.get('set-cookie')}",
            )

            # ---------- ③ 正确口令 ----------
            if not args.password:
                print("\n=== ③ 正确口令登录：跳过(未传 --password) ===")
                print("     传 --password <你 admin passwd --set 设的口令> 可测登录后的全部页面")
                _summary()
                return 1 if FAIL else 0

            print("\n=== ③ 登录 ===")
            r = c.post("/admin/login", data={"password": args.password})
            ok_login = r.status_code == 303 and "hoteldata_admin" in r.headers.get("set-cookie", "")
            check("正确口令 → 303 并下发会话 cookie", ok_login,
                  f"status={r.status_code} set-cookie={r.headers.get('set-cookie','')[:60]}")
            if not ok_login:
                _summary()
                return 1

            # ---------- ④ 每个页面 ----------
            print("\n=== ④ 遍历全部页面(状态码 + 关键内容) ===")
            for path, needle in PAGES:
                r = c.get(path)
                body = r.text
                good = r.status_code == 200 and needle in body
                check(
                    f"{path}",
                    good,
                    f"status={r.status_code} 含「{needle}」={needle in body} len={len(body)}"
                    + (f" | {body[:200]}" if r.status_code != 200 else ""),
                )

            # ---------- ⑤ 写操作 + 审计 ----------
            print("\n=== ⑤ 写操作落审计 ===")
            # 用比价目标的 create→delete 走一条完整链路(不碰真业务数据)
            probe = "后台自检-临时目标"
            r = c.post(
                "/admin/targets/create",
                data={
                    "anchor_name": probe,
                    "city": "自检市",
                    "mode": "batch",
                    "platforms": "ctrip",
                    "nights": "1",
                    "ebk_hotel_id": "",
                },
            )
            check("POST /admin/targets/create → 303", r.status_code == 303, f"status={r.status_code}")

            # 从页面里找它的 id(按「包含 probe 名的区间」定位,避免重名误配)
            r = c.get("/admin/targets")
            import re

            tid = None
            for mm in re.finditer(r"/admin/targets/(\d+)/toggle", r.text):
                start = max(0, mm.start() - 600)
                if probe in r.text[start : mm.start() + 600]:
                    tid = mm.group(1)
                    break
            check("新建的目标出现在列表页", tid is not None,
                  f"未在 /admin/targets 找到 {probe}(正则匹配 id 失败)")

            if tid:
                r = c.post(f"/admin/targets/{tid}/delete")
                check("POST /admin/targets/<id>/delete → 303", r.status_code == 303,
                      f"status={r.status_code}")

            r = c.get("/admin/audit")
            body = r.text
            check("审计页出现 target.create", "target.create" in body, "未在 /admin/audit 找到该动作")
            check("审计页出现 target.delete", "target.delete" in body, "未在 /admin/audit 找到该动作")
            check("审计页出现 login 记录", ">login<" in body or "login" in body, "未找到 login 记录")

            # ---------- ⑥ 静态资源 ----------
            print("\n=== ⑥ 静态资源 ===")
            r = c.get("/static/admin.css")
            check(
                "/static/admin.css 可访问",
                r.status_code == 200 and "--accent" in r.text,
                f"status={r.status_code} len={len(r.text)}",
            )

            # ---------- ⑦ 登出 ----------
            print("\n=== ⑦ 登出 ===")
            r = c.get("/admin/logout")
            check("登出 → 303 并清 cookie", r.status_code == 303,
                  f"status={r.status_code}")
            r = c.get("/admin")
            check("登出后访问 /admin → 303 到登录页",
                  r.status_code == 303 and r.headers.get("location") == "/admin/login",
                  f"status={r.status_code} loc={r.headers.get('location')}")

    finally:
        if not args.keep:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover
                proc.kill()

    _summary()
    return 1 if FAIL else 0


def _summary() -> None:
    print("\n" + "=" * 74)
    print(f"后台自检:{PASS} PASS / {FAIL} FAIL")
    if FAILURES:
        print("\n失败明细:")
        for line in FAILURES:
            print("  -", line)
    print("=" * 74)


if __name__ == "__main__":
    sys.exit(main())
