# -*- coding: utf-8 -*-
"""复核 used_keys 去重键 (target_url, selector) 是否存在真重复对。"""
import json
from collections import defaultdict
from pathlib import Path

OLD = Path(r"D:\AAAAaaaa\Pythooooooooooooon\hotel-data-system")
rules = json.loads((OLD / "config" / "api_rules.json").read_text(encoding="utf-8"))
OUT = Path(r"D:\AAAAaaaa\pypypypy\hotel-data-system\docs\参考\旧系统\_usedkeys_out.txt")

groups = defaultdict(list)
for pg, cfg in (rules.get("pages") or {}).items():
    for sm in cfg.get("sub_modules") or []:
        for m in sm.get("screenshot_modules") or []:
            groups[(sm.get("url"), m.get("selector"))].append(
                (pg, sm.get("name"), m.get("name")))

lines = []
dup = 0
for k, v in groups.items():
    if len(v) > 1:
        dup += 1
        lines.append(f"重复键 url={k[0]!r}\n  selector={k[1]!r}\n  命中模块={v}")
lines.append(f"\n不同 (sub_url, selector) 键数 = {len(groups)}；重复键组数 = {dup}")
lines.append(f"screenshot_modules 条目总数 = {sum(len(v) for v in groups.values())}")
# 同 selector 不同 url
sel_only = defaultdict(list)
for pg, cfg in (rules.get("pages") or {}).items():
    for sm in cfg.get("sub_modules") or []:
        for m in sm.get("screenshot_modules") or []:
            sel_only[m.get("selector")].append((pg, sm.get("name"), m.get("name"), sm.get("url")))
lines.append("\n按 selector 单键分组（跨页/跨子模块同选择器）:")
for k, v in sel_only.items():
    if len(v) > 1:
        lines.append(f"  selector={k!r} -> {v}")
OUT.write_text("\n".join(lines), encoding="utf-8")
print("ok")
