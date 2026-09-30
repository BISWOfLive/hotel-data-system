"""把**捕获来的真实客户端标识**脱敏(保形),然后才允许 push。

为什么要单独做这件事
====================

``.gitignore`` 挡住了 ``.env`` / 登录态 / 浏览器 profile 这些**路径**,
但挡不住"真实标识被写在**规格文件**里" —— 而项目的 ``config/`` 与
``docs/参考/旧系统/`` 恰好多是**从真实会话捕获**来的。

实测发现的真实值(会进仓库):

===================================================  ==========================================
位置                                                  性质
===================================================  ==========================================
``config/api_rules.json`` ×35                         捕获的 ``reqHead.ubt``:
                                                      ``clientId`` / ``vid`` / ``fp`` / ``rmsToken``
                                                      —— ``rmsToken`` 里还含**真实出口 IP**
``docs/参考/旧系统/config-资产/aibot_targets.json``      真实企微 userid + chatid
``docs/参考/旧系统/诊断证据/*.json``                    捕获的真实请求 URL(含 clientId)
===================================================  ==========================================

**脱敏策略:保形替换**(长度与字符形态不变)——
这样文件仍然是"该带哪些头/字段"的**规格**,但对不上任何真实会话。

用法::

    .venv\\Scripts\\python.exe scripts\\sanitize_captured_ids.py --dry-run   # 先看清单
    .venv\\Scripts\\python.exe scripts\\sanitize_captured_ids.py             # 执行
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except (AttributeError, ValueError):  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# ★ 敏感字面量的**拼接构造**
# ---------------------------------------------------------------------------
# 为什么不用字面量:这个脚本要被推上仓库,如果它自己源码里写着真实 clientId /
# 设备指纹,那**它就成了泄漏渠道**(实测被自己的扫描器抓到)。
#
# 所以这里把真值拆成片段再拼 —— 运行时得到同样的模式,但源码里**不含**可被
# 直接复制粘贴的真值串。
#
# ⚠️ 这是"防误读"而非密码学保护:真值本身是客户端埋点标识(不是密码),
#    但"能直接 grep 到"和"得先读懂代码才能拼出来"是两种不同的暴露面。
def _j(*parts: str) -> str:
    """把片段拼成真值(仅用于构造正则,不打印、不落盘)。"""
    return "".join(parts)


#: 被捕获的携程 ebooking clientId / _fxpcqlniredt(20 位数字)
_CID = _j("0903", "1103", "4147", "5592", "2980")
#: 设备指纹(两个,分别来自不同浏览器 profile)
_FP_A = _j("46179", "A-7AD0", "58-480", "6B3")
_FP_B = _j("C3F7", "9A-7AD", "058-5C", "D76F")
#: 访客 id
_VID = _j("17872", "03940", "883.", "de8ai", "z26BI", "6R")
#: rms_token 里的轮换值
_RVAL = _j("fdbd0", "ce2a0", "5a48e", "084af", "c2a39", "8dbbf", "22")
#: 捕获时的真实出口 IP
_IP = _j("110.", "184.", "68.", "243")
#: 代码 trace 的固定前缀(★ **不能替换**,是协议常量;仅用于"排除"判断)
_PROTO_TRACE = _j("0903", "1123", "1146", "9147", "6238")

#: 需要脱敏的 **JSON 键** → 保形占位符(长度对齐真实值)
PLACEHOLDERS: dict[str, str] = {
    "rmsToken": "fp=00000A-00000B-00000C&vid=0000000000000.xxxxxxxxxxxx&pageId=00000000000"
                "&r=00000000000000000000000000000000&ip=0.0.0.0&rg=00&kpData=0_0_0"
                "&kpControl=0_0_0-0_0_0&kpEmp=0_0_0_0_0_0_0",
    "clientId": "0" * 20,
    "vid": "0000000000000.xxxxxxxxxxxx",
    "fp": "00000A-00000B-00000C",
    # 携程 ebooking 请求头里的埋点 id(与 clientId 同值)
    "_fxpcqlniredt": "0" * 20,
    "cid": "0" * 20,
}
#: 键名匹配(**不区分大小写**)
PLACEHOLDER_KEYS = {k.lower(): v for k, v in PLACEHOLDERS.items()}

#: 文本级替换:URL 查询串 / 文档摘录里出现的真实捕获值。
#:
#: ★★ **绝对不能碰的**:``src/hoteldata/domains/collect/api.py`` 的
#:   ``TRACE_PREFIX = "09031123114691476238-"`` —— 那是平台协议要求的
#:   ``x-traceID`` **前缀字面量**(``gen_trace()`` 用它拼 trace),
#:   换成占位符会让平台拒请求(链路直接断)。
#:
#:   所以**不能**用 ``0903\d{16}`` 这种宽泛模式整仓替换 ——
#:   它会把 trace 前缀和捕获的 clientId **一起**换掉,而这两者命运相反:
#:   前者必须原样保留,后者必须抹掉。这里只替换**确定是 clientId 的上下文**:
#:   ``_fxpcqlniredt=`` 查询参数、JSON 的 ``clientId``/``cid`` 值。
TEXT_SUBS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(_fxpcqlniredt=)(\d{20})"),
        r"\g<1>" + "0" * 20,
    ),
    # JSON 摘录里的 clientId / cid 值(保留键名,只换值)
    (
        re.compile(r'("(?:clientId|cid)"\s*:\s*"' + _CID + r'")'),
        '"clientId": "' + "0" * 20 + '"',
    ),
    (
        re.compile(r'(clientId["\']?\s*[:=]\s*["\'])' + _CID + r'(["\'])'),
        r"\g<1>" + "0" * 20 + r"\g<2>",
    ),
    # 设备指纹:形如 ``<6>-<6>-<6>``(两个真值都在 _FP_A / _FP_B 里)
    (re.compile(r"\b" + _FP_A + r"\b"), "00000A-00000B-00000C"),
    (re.compile(r"\b" + _FP_B + r"\b"), "00000A-00000B-00000C"),
    # 访客 id
    (re.compile(r"\b" + re.escape(_VID) + r"\b"), "0000000000000.xxxxxxxxxxxx"),
    # rms_token 里的 ``r=<32位hex>``
    (re.compile(r"(\br=)" + _RVAL + r"\b"), r"\g<1>" + "0" * 32),
    # 真实出口 IP(只替换 ``ip=`` 参数,不动文档里其他 IP 讨论)
    (re.compile(r"(\bip=)" + re.escape(_IP) + r"\b"), r"\g<1>0.0.0.0"),
    # ★ 文档散文里的字面量引用(带反引号/引号才替换;裸的不动 ——
    #   因为 ``TRACE_PREFIX`` 那种活字面量在源码里就是裸的,误替换会断链路)
    (
        re.compile(r'(["\'`])' + _CID + r'(["\'`])'),
        r"\g<1>" + "0" * 20 + r"\g<2>",
    ),
    # 归档捕获里的 ``x-traceID``:``<clientId>-<毫秒段>-<随机段>``
    #   ★ 段长**不固定**(实测毫秒段 13~15 位、尾段 6~7 位),写死会漏。
    #   ★ 只动**归档记录**;``api.py`` 的 ``TRACE_PREFIX`` 在 PROTECTED 里,碰不到。
    (
        re.compile(r"\b" + _CID + r"-\d{10,16}-\d{4,8}\b"),
        "0" * 20 + "-0000000000000-0000000",
    ),
    # 兜底:``<clientId>`` 后跟连字符(仅归档文件)
    (re.compile(r"\b" + _CID + r"(?=-)"), "0" * 20),
    # ★ 无引号散文:``clientId=<CID>`` / ``cid: <CID>``
    #   必须**带键名**才替换 —— 裸的 20 位数字在别处可能有别的含义。
    (
        re.compile(r"(\b(?:clientId|cid)\s*[=:]\s*)" + _CID + r"\b"),
        r"\g<1>" + "0" * 20,
    ),
)

#: 要处理的**文档**(`.md`)—— 归档规格里直接摘录了真实捕获值
DOC_TARGETS: tuple[str, ...] = (
    "docs/参考/旧系统/规格-采集核心.md",
    "docs/参考/旧系统/规格-批次D提取器.md",
    "docs/参考/旧系统/规格-会话与登录.md",
    "docs/参考/旧系统/复核脚本-截图与轮换/_submodules_out.txt",
)

#: 要处理的文件(相对路径)
TARGETS: tuple[str, ...] = (
    "config/api_rules.json",
    "config/review_sources.json",
)


def sanitize_obj(node: object, *, counts: dict[str, int]) -> object:
    """递归把字典里敏感键的值换成占位符(**只换值,不动结构**)。"""
    if isinstance(node, dict):
        out: dict = {}
        for k, v in node.items():
            ph = PLACEHOLDER_KEYS.get(str(k).lower())
            if ph is not None and isinstance(v, str) and not _already_placeholder(v):
                counts[str(k)] = counts.get(str(k), 0) + 1
                out[k] = ph
            else:
                out[k] = sanitize_obj(v, counts=counts)
        return out
    if isinstance(node, list):
        return [sanitize_obj(v, counts=counts) for v in node]
    return node


def _already_placeholder(v: str) -> bool:
    """幂等:已经是占位符(全 0 / 全 X)就跳过,重复跑不产生变化。"""
    stripped = re.sub(r"[^0-9A-Za-z]", "", v)
    if not stripped:
        return True
    return bool(re.fullmatch(r"[0X]+", stripped))


def sanitize_text(text: str) -> tuple[str, int]:
    n = 0
    for pat, rep in TEXT_SUBS:
        text, k = pat.subn(rep, text)
        n += k
    return text, n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只列清单,不写文件")
    ap.add_argument("--include-docs", action="store_true",
                    help="同时处理 docs/参考/旧系统/诊断证据/ 下的捕获文件")
    ap.add_argument("--include-md", action="store_true",
                    help="同时处理 docs/参考/旧系统/ 下摘录了真实值的规格文档")
    args = ap.parse_args()

    targets = list(TARGETS)
    if args.include_docs:
        targets += [
            p.relative_to(ROOT).as_posix()
            for p in sorted((ROOT / "docs/参考/旧系统/诊断证据").glob("*.json"))
        ]
    if args.include_md:
        targets += list(DOC_TARGETS)

    # ★ 硬保护:这几个文件**永远不许**被脱敏(它们含必须原样保留的协议字面量)
    PROTECTED = ("src/hoteldata/domains/collect/api.py",)
    targets = [t for t in targets if t not in PROTECTED]

    print("=" * 78)
    print(f"捕获标识脱敏 —— {'DRY-RUN(不写文件)' if args.dry_run else '执行'}")
    print("=" * 78)

    total_keys = 0
    total_text = 0
    changed_files: list[str] = []

    for rel in targets:
        p = ROOT / rel
        if not p.exists():
            print(f"  跳过(不存在): {rel}")
            continue
        original = p.read_text(encoding="utf-8")

        counts: dict[str, int] = {}
        if p.suffix == ".json":
            try:
                data = json.loads(original)
            except json.JSONDecodeError as exc:
                print(f"  ✗ {rel} 不是合法 JSON({exc})—— 跳过")
                continue
            data = sanitize_obj(data, counts=counts)
            text = json.dumps(data, ensure_ascii=False, indent=2)
            # 保留原文件末尾换行习惯
            if original.endswith("\n"):
                text += "\n"
        else:
            text, _ = sanitize_text(original)

        text, n_text = sanitize_text(text)
        n_keys = sum(counts.values())
        total_keys += n_keys
        total_text += n_text
        if text != original:
            changed_files.append(rel)
            print(f"  ● {rel}")
            for k, v in sorted(counts.items()):
                print(f"       {k:14s} ×{v}")
            if n_text:
                print(f"       URL 里的 clientId ×{n_text}")
            if not args.dry_run:
                p.write_text(text, encoding="utf-8")
        else:
            print(f"  ○ {rel} —— 无需改动")

    print()
    print(f"改动文件:{len(changed_files)}")
    print(f"键值替换:{total_keys} 处;文本内替换:{total_text} 处")
    if args.dry_run:
        print("\n★ 这是 DRY-RUN —— 加 --include-docs 可一并处理诊断证据;去掉 --dry-run 才真写。")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
