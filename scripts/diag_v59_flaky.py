"""V59 脆弱性定量实验:同一探针在固定条件下跑 N 次,统计红/绿。

背景
====

用户诊断:``_empty_registry_probe`` 用 ``encoding="utf-8"`` 解码子进程输出,
而中文 Windows 上子进程按 **GBK** 写 → 中文标记串变乱码 → ``marker in blob`` 为假
→ V59 假红。修法是给 ``subprocess.run`` 传 ``env={**os.environ, "PYTHONIOENCODING": "utf-8"}``。

但我**自己观察到过 V59 时红时绿**(全量跑红、单跑绿、再全量又绿),
所以"确定性 GBK 假红"这个结论与我的观察不符 —— 需要定量确认到底是不是随机的。

本脚本做的事
============

**不改任何产品代码**,只复刻探针逻辑,在**同一条件**下重复跑,并逐次报告:

* 子进程退出码;
* 原始字节按 **utf-8** 解码后,标记串是否命中(即当前验收器的判法);
* 原始字节按 **gbk** 解码后,标记串是否命中(对照);
* 当前 ``PYTHONIOENCODING`` 环境变量值;
* 子进程实际用的 stdout 编码(让子进程自己打印)。

跑法::

    .venv\\Scripts\\python.exe scripts\\diag_v59_flaky.py            # 10 次
    .venv\\Scripts\\python.exe scripts\\diag_v59_flaky.py --times 30
    .venv\\Scripts\\python.exe scripts\\diag_v59_flaky.py --with-env # 模拟"修好"之后
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except (AttributeError, ValueError):  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parents[1]
MARKER = "拒绝启动一个 0 任务的调度器"

#: 与验收器**完全一致**的探针代码(故意不多打印任何东西 ——
#: 第一版为了让子进程自报编码,加了一句 ``print('CHILD_STDOUT_ENCODING=...')``,
#: 结果"探测字符串"本身出现在子进程回显的**代码**里,解析时抓错行。
#: 教训:往被测对象里插探针字符串,会让"探测"与"内容"混淆。
PROBE = (
    "import sys; sys.path.insert(0, 'src'); import asyncio;"
    "from hoteldata.runtime import Runtime; from hoteldata.settings import get_settings;"
    "asyncio.run("
    "Runtime.create(get_settings(), with_db=False, with_scheduler=True).__aenter__()"
    ")"
)

#: 单独取子进程编码用的**独立**代码(与探针分开,避免污染)
ENC_PROBE = "import sys; print('ENC:' + str(sys.stdout.encoding))"


def child_encoding(*, inject_env: bool) -> str:
    """单独跑一次**独立**代码,取子进程的 stdout 编码(不污染探针输出)。"""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"} if inject_env else None
    proc = subprocess.run(  # noqa: S603 - 固定命令
        [sys.executable, "-c", ENC_PROBE],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=60,
        check=False,
    )
    blob = (proc.stdout or b"").decode("ascii", errors="replace")
    for line in blob.splitlines():
        if line.startswith("ENC:"):
            return line[4:].strip()
    return "?"


def run_once(*, inject_env: bool) -> dict:
    env = None
    if inject_env:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(  # noqa: S603 - 固定命令
        [sys.executable, "-c", PROBE],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
        check=False,
    )
    raw = proc.stdout or b""
    out: dict = {"rc": proc.returncode, "size": len(raw)}
    # ★ 键名用下划线(``hit_utf8``)而不是 ``f"hit_{enc}"`` ——
    #   后者会生成 ``hit_utf-8``(带连字符),与读取处的 ``hit_utf8`` 不一致。
    for enc, key in (("utf-8", "hit_utf8"), ("gbk", "hit_gbk")):
        try:
            blob = raw.decode(enc, errors="replace")
        except LookupError:  # pragma: no cover
            blob = ""
        out[key] = MARKER in blob
        if enc == "utf-8":
            out["tail"] = " ".join(blob.split())[-160:]
    out["child_enc"] = child_encoding(inject_env=inject_env)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--times", type=int, default=10)
    ap.add_argument("--with-env", action="store_true", help="给子进程注入 PYTHONIOENCODING=utf-8")
    args = ap.parse_args()

    print("=" * 78)
    print(f"V59 探针脆弱性实验 —— {args.times} 次")
    print(f"父进程 PYTHONIOENCODING = {os.environ.get('PYTHONIOENCODING')!r}")
    print(f"注入 env = {args.with_env}")
    print("=" * 78)
    print(f"  {'#':>3s} {'rc':>4s} {'子进程编码':>10s} {'utf8命中':>9s} {'gbk命中':>8s}")
    print("  " + "-" * 44)

    stats = {"rc1": 0, "hit_utf8": 0, "hit_gbk": 0, "enc": {}}
    for i in range(1, args.times + 1):
        r = run_once(inject_env=args.with_env)
        stats["rc1"] += 1 if r["rc"] == 1 else 0
        stats["hit_utf8"] += 1 if r["hit_utf8"] else 0
        stats["hit_gbk"] += 1 if r["hit_gbk"] else 0
        enc = r.get("child_enc", "?")
        stats["enc"][enc] = stats["enc"].get(enc, 0) + 1
        print(f"  {i:3d} {r['rc']:>4d} {enc:>10s} "
              f"{('是' if r['hit_utf8'] else '否'):>9s} {('是' if r['hit_gbk'] else '否'):>8s}")

    print("\n" + "=" * 78)
    print(f"  子进程 rc=1(守卫生效)      : {stats['rc1']}/{args.times}")
    print(f"  ★ 按 utf-8 解码能命中标记    : {stats['hit_utf8']}/{args.times}   ← 验收器判据")
    print(f"  按 gbk 解码能命中标记        : {stats['hit_gbk']}/{args.times}")
    print(f"  子进程实际 stdout 编码分布   : {stats['enc']}")
    print()
    if stats["hit_utf8"] == args.times:
        print("★ 结论:本条件下 **稳定命中** → V59 应稳定 PASS")
    elif stats["hit_utf8"] == 0:
        print("★ 结论:本条件下 **稳定不命中** → V59 应稳定 FAIL(确定性,非随机)")
    else:
        print("★ 结论:本条件下 **时红时绿** → 存在竞态/环境漂移,不是纯编码问题")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
