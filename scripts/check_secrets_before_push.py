"""推送前凭据扫描 —— **在 git add 之前**跑,把不该上传的东西找出来。

为什么必须有这一步
==================

旧系统吃过的亏(总纲 D2/D3,已写进 `.gitignore` 的红线注释):

* ``storage_states/`` 下 **4 个真实酒店账号会话 Cookie** 被 git 跟踪;
* ``.edge-profile/`` **1,936 个含凭据的浏览器配置文件**被跟踪。

新项目的 ``.gitignore`` 堵住了这两条**路径**,但路径规则挡不住
"凭据被写在**别的**文件里" —— 例如:

* 归档的旧系统配置(``config/*.json``)里可能带真实 ChatID / 机器人 key;
* 文档里可能粘了真实账号 / 密码;
* 某个脚本里可能硬编码了 token。

本脚本按**内容特征**扫(不是按路径),把命中处按**风险等级**列出来。

风险分级
========

* **🔴 阻断**:明确的凭据字面量(密码、secret、token、api key 的值)
  —— 必须处理才能推。
* **🟡 复核**:像凭据的东西(ChatID、webhook、手机号、邮箱、内网地址)
  —— 需要人看一眼是不是真的。
* **🟢 提示**:被 .gitignore 覆盖、本来就不会进仓库的文件(只确认规则生效)。

用法::

    .venv\\Scripts\\python.exe scripts\\check_secrets_before_push.py
    .venv\\Scripts\\python.exe scripts\\check_secrets_before_push.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except (AttributeError, ValueError):  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parents[1]

#: 不扫的目录(二进制 / 第三方 / 虚拟环境)
SKIP_DIRS = {
    ".git", ".venv", ".venv314-bak", "__pycache__", ".ruff_cache", ".pytest_cache",
    "node_modules", ".idea", ".vscode",
}
#: 不扫的扩展名(二进制)
SKIP_EXT = {
    ".pyc", ".pyo", ".so", ".dll", ".exe", ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".ico", ".zip", ".gz", ".whl", ".dump", ".db", ".bin", ".pdf", ".xlsx", ".docx",
}

#: 🔴 明确凭据:``<关键词> = <看起来是真值>``
RED_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("明文口令赋值", re.compile(
        r"""(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|app[_-]?secret|access[_-]?key)
            \s*[:=]\s*["']([^"'\s{}<>$]{6,})["']""", re.X)),
    ("Fernet/PG 连接串含口令", re.compile(
        r"""(?i)(postgres(?:ql)?|mysql|redis|mongodb)://[^:/\s"']+:[^@\s"']{3,}@""")),
    ("Bearer / Basic 认证头", re.compile(r"""(?i)\b(bearer|basic)\s+[A-Za-z0-9._\-]{16,}""")),
    ("私钥块", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("微信/企微 webhook key", re.compile(
        r"""(?i)qyapi\.weixin\.qq\.com/cgi-bin/webhook/send\?key=[0-9a-f\-]{16,}""")),
    ("智谱/OpenAI 风格 key", re.compile(r"""\b[0-9a-f]{32}\.[A-Za-z0-9]{12,}\b""")),
)

#: 🟡 需复核:像标识符的东西(不一定是凭据,但值得看一眼)
YELLOW_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("企微 chatid 形态", re.compile(r"""\bwr[A-Za-z0-9_\-]{16,}\b""")),
    ("机器人 key 形态(20+ 位 hex)", re.compile(r"""\b[0-9a-f]{20,}\b""")),
    ("手机号", re.compile(r"""(?<![0-9a-f])1[3-9]\d{9}(?![0-9a-f])""")),
    ("邮箱", re.compile(r"""[\w.+-]+@[\w-]+\.[\w.]{2,}""")),
    ("疑似真实账号别名", re.compile(r"""(?i)\b(ctrip|meituan)\d{3,}\b""")),
)

#: 🟢 必须被 .gitignore 覆盖的路径(存在即确认规则生效)
MUST_BE_IGNORED = (
    ".env", "config/secret.key", "var/", "var/states/", "logs/",
    "storage_state.json", ".edge-profile/",
)


#: 不扫的**具体文件**(机器生成 / 只有哈希,扫了是纯噪音)
SKIP_FILES = {
    "poetry.lock",      # 全是 sha256 哈希 —— hex 里必然撞出"手机号/长 hex"形态
    "package-lock.json",
    "pnpm-lock.yaml",
}


def iter_files() -> list[Path]:
    out: list[Path] = []
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        if p.name in SKIP_FILES:
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.suffix.lower() in SKIP_EXT:
            continue
        # 大小上限(避免误扫大文件)
        try:
            if p.stat().st_size > 2_000_000:
                continue
        except OSError:
            continue
        out.append(p)
    return out


def scan_file(p: Path) -> tuple[list[dict], list[dict]]:
    red: list[dict] = []
    yellow: list[dict] = []
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return red, yellow
    rel = p.relative_to(ROOT).as_posix()
    for i, line in enumerate(text.splitlines(), 1):
        # ★ 允许**人工审阅后**显式标注放行(例如把口令打码再拼 DSN 的代码,
        #   它长得像连接串但值就是 ``***``)。见 ``backup.py`` 的 ``redacted_url``。
        if "secretscan:ignore" in line:
            continue
        # 跳过明显的占位/示例(<...>、xxx、your_、example)
        low = line.lower()
        if any(t in low for t in ("<your", "your_", "example.com", "changeme", "xxx",
                                  "placeholder", "----", "fake-", "test-pw-123")):
            continue
        for name, pat in RED_PATTERNS:
            m = pat.search(line)
            if m:
                red.append({"file": rel, "line": i, "kind": name,
                            "snippet": _censor(line.strip())[:160]})
        for name, pat in YELLOW_PATTERNS:
            m = pat.search(line)
            if m:
                yellow.append({"file": rel, "line": i, "kind": name,
                               "snippet": _censor(line.strip())[:160]})
    return red, yellow


_CENSOR_KEYS = re.compile(
    r"""(?i)(password|passwd|pwd|secret|token|api[_-]?key|app[_-]?secret|access[_-]?key)
        (\s*[:=]\s*)(["']?)([^"'\s,}]{2,})""", re.X)
#: 报告里也要打码的**标识符**(否则扫描报告本身成了泄漏渠道 —— 实测踩过:
#: 第一版把命中的真实 chatid 原样写进 var/_secretscan.json)
_CENSOR_IDS = (
    re.compile(r"""\bwr[A-Za-z0-9_\-]{16,}\b"""),          # 企微 chatid
    re.compile(r"""\b1[3-9]\d{9}\b"""),                     # 手机号
    re.compile(r"""\b[0-9a-f]{20,}\b"""),                   # 长 hex(设备/trace/机器人 key 形态)
)


def _censor(line: str) -> str:
    """把行里的**值/标识符**打码,只留头部 —— 报告本身不能成为泄密渠道。"""
    def _sub(m: re.Match[str]) -> str:
        v = m.group(4)
        return f"{m.group(1)}{m.group(2)}{m.group(3)}{v[:2]}***[已打码 {len(v)} 字符]"

    out = _CENSOR_KEYS.sub(_sub, line)
    for pat in _CENSOR_IDS:
        out = pat.sub(lambda m: m.group(0)[:6] + "***", out)
    return out


def git_check_ignored() -> list[dict]:
    """用 ``git check-ignore`` 确认红线路径真的被忽略(只对已有 .git 的仓库有效)。"""
    out: list[dict] = []
    if not (ROOT / ".git").exists():
        return out
    for rel in MUST_BE_IGNORED:
        p = ROOT / rel.rstrip("/")
        if not p.exists():
            continue
        try:
            r = subprocess.run(  # noqa: S603
                ["git", "check-ignore", "-q", rel],
                cwd=str(ROOT), capture_output=True, timeout=30, check=False,
            )
            out.append({"path": rel, "ignored": r.returncode == 0})
        except Exception as exc:  # noqa: BLE001
            out.append({"path": rel, "ignored": None, "error": str(exc)})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="", help="把结果写到该 JSON 文件")
    ap.add_argument("--max", type=int, default=40, help="每类最多打印多少条")
    args = ap.parse_args()

    files = iter_files()
    print("=" * 78)
    print(f"推送前凭据扫描 —— 扫了 {len(files)} 个文本文件")
    print("=" * 78)

    red_all: list[dict] = []
    yellow_all: list[dict] = []
    for p in files:
        r, y = scan_file(p)
        red_all.extend(r)
        yellow_all.extend(y)

    print(f"\n🔴 阻断项(明确凭据字面量):{len(red_all)}")
    for item in red_all[: args.max]:
        print(f"   {item['file']}:{item['line']}  [{item['kind']}]")
        print(f"       {item['snippet']}")
    if len(red_all) > args.max:
        print(f"   … 另有 {len(red_all) - args.max} 条")

    print(f"\n🟡 需复核项(像凭据/标识符):{len(yellow_all)}")
    # 按文件聚合,避免刷屏
    by_file: dict[str, list[dict]] = {}
    for item in yellow_all:
        by_file.setdefault(item["file"], []).append(item)
    for f, items in sorted(by_file.items(), key=lambda kv: -len(kv[1]))[: args.max]:
        kinds = sorted({i["kind"] for i in items})
        print(f"   {f}  ×{len(items)}  {kinds}")
        print(f"       {items[0]['snippet']}")

    ignored = git_check_ignored()
    if ignored:
        print("\n🟢 红线路径的忽略状态(git check-ignore):")
        for it in ignored:
            mark = "✓ 已忽略" if it["ignored"] else "✗ **未忽略!**"
            print(f"   {it['path']:26s} {mark}")
    else:
        print("\n🟢 尚未 git init,跳过 check-ignore(init 后请再跑一次)")

    print("\n" + "=" * 78)
    if red_all:
        print(f"❌ 有 {len(red_all)} 条阻断项 —— **处理之前不要 push**")
    else:
        print("✅ 未发现明确凭据字面量")
    if yellow_all:
        print(f"⚠️  有 {len(yellow_all)} 条需复核项 —— 请人工看一眼上面的文件清单")
    print("=" * 78)

    if args.json:
        Path(args.json).write_text(
            json.dumps({"red": red_all, "yellow": yellow_all, "ignored": ignored},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"结果已写入 {args.json}")

    return 1 if red_all else 0


if __name__ == "__main__":
    sys.exit(main())
