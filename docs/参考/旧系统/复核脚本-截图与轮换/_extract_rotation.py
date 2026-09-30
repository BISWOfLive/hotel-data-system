# -*- coding: utf-8 -*-
"""只读勘察脚本：从旧系统 config/api_rules.json + config/push_rotation.json 提取
screenshot_modules 资产表、扩展键统计、轮换项与 sub_modules 的交集。
不修改旧系统任何文件。
"""
import io
import json
import sys
from pathlib import Path

OUT = Path(r"D:\AAAAaaaa\pypypypy\hotel-data-system\docs\参考\旧系统\_extract_out.txt")
_buf = io.StringIO()


class _Tee:
    def write(self, s):
        _buf.write(s)
        return len(s)

    def flush(self):
        pass


sys.stdout = _Tee()

OLD = Path(r"D:\AAAAaaaa\Pythooooooooooooon\hotel-data-system")
rules = json.loads((OLD / "config" / "api_rules.json").read_text(encoding="utf-8"))
rot = json.loads((OLD / "config" / "push_rotation.json").read_text(encoding="utf-8"))

EXT_KEYS = ["clicks", "tabs", "click", "skip_click_on", "require_text"]

print("=" * 100)
print("### A. api_rules.json pages[*].sub_modules[*].screenshot_modules[*] 全量资产")
print("=" * 100)
total = 0
ext_hits = {k: [] for k in EXT_KEYS}
other_keys = {}
rows = []
for page_name, cfg in (rules.get("pages") or {}).items():
    for sm in cfg.get("sub_modules") or []:
        mods = sm.get("screenshot_modules") or []
        for idx, m in enumerate(mods):
            total += 1
            rows.append((page_name, sm.get("name"), sm.get("url"), idx, m))
            for k in EXT_KEYS:
                if k in m:
                    ext_hits[k].append((page_name, sm.get("name"), m.get("name"), m[k]))
            for k in m:
                if k not in ("name", "selector") and k not in EXT_KEYS:
                    other_keys.setdefault(k, []).append((page_name, sm.get("name"), m.get("name"), m[k]))

for i, (pg, sub, url, idx, m) in enumerate(rows, 1):
    print(f"\n[{i:02d}] page={pg!r} sub_module={sub!r}")
    print(f"     sub_url={url!r}")
    print(f"     name={m.get('name')!r}")
    print(f"     selector={m.get('selector')!r}")
    for k in EXT_KEYS:
        if k in m:
            print(f"     {k}={m[k]!r}")
    for k, v in m.items():
        if k not in ("name", "selector") and k not in EXT_KEYS:
            print(f"     {k}={v!r}   <-- 未列出的扩展键")

print("\n" + "=" * 100)
print(f"screenshot_modules 条目总数 = {total}")
print("=" * 100)
print("\n### B. 扩展键出现次数与所属模块")
for k in EXT_KEYS:
    hits = ext_hits[k]
    print(f"\n{k}: {len(hits)} 次")
    for pg, sub, nm, val in hits:
        print(f"    page={pg} / sub_module={sub} / screenshot_module={nm} -> {val!r}")
print("\n### B2. 其它未声明的扩展键")
for k, hits in other_keys.items():
    print(f"\n{k}: {len(hits)} 次")
    for pg, sub, nm, val in hits:
        print(f"    page={pg} / sub_module={sub} / screenshot_module={nm} -> {val!r}")

print("\n" + "=" * 100)
print("### C. 轮换清单 vs api_rules sub_modules")
print("=" * 100)
sub_names = []
for page_name, cfg in (rules.get("pages") or {}).items():
    for sm in cfg.get("sub_modules") or []:
        if sm.get("name"):
            sub_names.append((page_name, sm["name"]))
sub_set = {n for _, n in sub_names}
print(f"api_rules sub_modules 总数 = {len(sub_names)}；去重 name 数 = {len(sub_set)}")

items = rot["items"]
print(f"\npush_rotation.json daily_count={rot.get('daily_count')} items 数={len(items)}")
print(f"{'#':>3} {'name':<24} {'page':<12} {'type':<18} {'in_sub':<7} {'alias'}")
missing = []
for i, it in enumerate(items, 1):
    nm = it.get("name")
    insub = nm in sub_set
    if not insub:
        missing.append(it)
    print(f"{i:>3} {nm:<24} {it.get('page'):<12} {it.get('type'):<18} {str(insub):<7} {it.get('alias')}")
print(f"\n不在 sub_modules 的轮换项 = {len(missing)} 项：")
for it in missing:
    print(f"    - name={it.get('name')!r} page={it.get('page')!r} type={it.get('type')!r} "
          f"collect={it.get('collect')!r} alias={it.get('alias')!r} url={it.get('url')!r} "
          f"shot_name={it.get('shot_name')!r}")

print("\n### C2. 轮换项 name 在 sub_modules 中的页面归属（同名跨页检查）")
for i, it in enumerate(items, 1):
    nm = it.get("name")
    pgs = [p for p, n in sub_names if n == nm]
    print(f"{i:>3} {nm:<24} 出现于页面={pgs} 清单声明 page={it.get('page')!r}")

print("\n### C3. 轮换项 name 与 screenshot_modules[*].name 的交集")
shot_names = []
for pg, cfg in (rules.get("pages") or {}).items():
    for sm in cfg.get("sub_modules") or []:
        for m in sm.get("screenshot_modules") or []:
            shot_names.append((pg, sm.get("name"), m.get("name")))
shot_set = {n for _, _, n in shot_names}
print(f"screenshot_modules name 去重数 = {len(shot_set)}")
for i, it in enumerate(items, 1):
    nm = it.get("name")
    aliases = it.get("alias") or [nm]
    hit = [a for a in aliases if a in shot_set]
    print(f"{i:>3} {nm:<24} alias={aliases} 命中 screenshot_modules.name={hit}")

print("\n### D. type 分布")
from collections import Counter
print(Counter(it.get("type") for it in items))
print("collect=true 的项:", [it.get("name") for it in items if it.get("collect")])

print("\n### E. 今日轮换（复刻 app/rotation.py:daily_rotation）")
from datetime import date
d = date.today()
n = min(max(rot["daily_count"], 1), len(items))
offset = d.toordinal() % len(items)
print(f"today={d} toordinal={d.toordinal()} len(items)={len(items)} offset={offset} n={n}")
todays = [items[(offset + k) % len(items)] for k in range(n)]
for k, it in enumerate(todays):
    print(f"  [{k}] {it.get('name')} / {it.get('page')} / {it.get('type')}")
print("今日轮换项中 collectible（type!=fullpage 或 collect=true）:",
      [it.get("name") for it in todays if it.get("type") != "fullpage" or it.get("collect")])

print("\n### F. report_schedule.json 中的模块名单（供截图需求裁剪对照）")
sched_path = OLD / "config" / "report_schedule.json"
if sched_path.exists():
    sch = json.loads(sched_path.read_text(encoding="utf-8"))
    sitems = sch.get("items") or []
    print(f"report_schedule items 数 = {len(sitems)}")
    for it in sitems:
        nm = it.get("name")
        print(f"  name={nm!r} windows={it.get('windows')!r} daily_except_monday={it.get('daily_except_monday')!r} "
              f"in_rotation={nm in {i.get('name') for i in items}} in_sub={nm in sub_set}")

sched_names = {it.get("name") for it in (json.loads(sched_path.read_text(encoding="utf-8")).get("items") or [])}
print("\n### G. 21 项轮换 name 是否在 report_schedule 中")
for i, it in enumerate(items, 1):
    print(f"{i:>3} {it.get('name'):<24} in_report_schedule={it.get('name') in sched_names}")

OUT.write_text(_buf.getvalue(), encoding="utf-8")
sys.stdout = sys.__stdout__
print("written:", OUT)
