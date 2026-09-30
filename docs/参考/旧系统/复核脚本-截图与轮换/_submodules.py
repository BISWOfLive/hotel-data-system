# -*- coding: utf-8 -*-
"""只读复核 2：api_rules pages/sub_modules 全表 + 点评分析归属 + screenshot_modules 覆盖。"""
import io
import json
import sys
from pathlib import Path

OLD = Path(r"D:\AAAAaaaa\Pythooooooooooooon\hotel-data-system")
OUT = Path(r"D:\AAAAaaaa\pypypypy\hotel-data-system\docs\参考\旧系统\_submodules_out.txt")
_buf = io.StringIO()


class _Tee:
    def write(self, s):
        _buf.write(s)
        return len(s)

    def flush(self):
        pass


sys.stdout = _Tee()

rules = json.loads((OLD / "config" / "api_rules.json").read_text(encoding="utf-8"))
pages = rules.get("pages") or {}
print(f"pages 数 = {len(pages)}")
print("pages 键（原样）:", list(pages.keys()))

print("\n### sub_modules 全表（24 条）")
n = 0
for pname, cfg in pages.items():
    for sm in cfg.get("sub_modules") or []:
        n += 1
        mods = sm.get("screenshot_modules") or []
        print(f"[{n:02d}] page={pname!r} name={sm.get('name')!r}")
        print(f"      url={sm.get('url')!r}")
        print(f"      fixed_daily={sm.get('fixed_daily')!r} scope={cfg.get('scope')!r} "
              f"windows={sm.get('windows')!r} nav={sm.get('nav')!r}")
        print(f"      screenshot_modules 条目数={len(mods)} names={[m.get('name') for m in mods]}")
print(f"\nsub_modules 总数 = {n}")

print("\n### 每个 page 的顶层键")
for pname, cfg in pages.items():
    keys = [k for k in cfg.keys()]
    print(f"page={pname!r} keys={keys}")

print("\n### 顶层键")
print(list(rules.keys()))
print("\n### _comment / 注释类键")
for k, v in rules.items():
    if k.startswith("_") or k == "pages":
        if k != "pages":
            print(f"{k} = {v!r}")

print("\n### fixed_daily 子模块")
for pname, cfg in pages.items():
    for sm in cfg.get("sub_modules") or []:
        if sm.get("fixed_daily"):
            print(f"page={pname!r} name={sm.get('name')!r} fixed_daily={sm.get('fixed_daily')!r}")

print("\n### 点评页 sub_modules 明细")
for pname, cfg in pages.items():
    if "点评" in str(pname):
        print(f"page={pname!r} url={cfg.get('url')!r}")
        for sm in cfg.get("sub_modules") or []:
            print(f"   name={sm.get('name')!r} url={sm.get('url')!r} nav={sm.get('nav')!r} "
                  f"screenshot_modules={sm.get('screenshot_modules')!r}")

OUT.write_text(_buf.getvalue(), encoding="utf-8")
sys.stdout = sys.__stdout__
print("written:", OUT)
