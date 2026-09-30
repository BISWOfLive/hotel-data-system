# -*- coding: utf-8 -*-
"""只读复核：轮换窗口分布 / 「每日实际只采 3 项」/ 周期长度 / 失效项机制路径。"""
import io
import json
import sys
from datetime import date, timedelta
from pathlib import Path

OLD = Path(r"D:\AAAAaaaa\Pythooooooooooooon\hotel-data-system")
OUT = Path(r"D:\AAAAaaaa\pypypypy\hotel-data-system\docs\参考\旧系统\_verify_out.txt")
_buf = io.StringIO()


class _Tee:
    def write(self, s):
        _buf.write(s)
        return len(s)

    def flush(self):
        pass


sys.stdout = _Tee()

rules = json.loads((OLD / "config" / "api_rules.json").read_text(encoding="utf-8"))
rot = json.loads((OLD / "config" / "push_rotation.json").read_text(encoding="utf-8"))
items = rot["items"]
known = set()
for cfg in (rules.get("pages") or {}).values():
    for sm in cfg.get("sub_modules") or []:
        if sm.get("name"):
            known.add(sm["name"])

print("### 1. 周期长度")
print(f"len(items)={len(items)} daily_count={rot['daily_count']}")
print(f"offset 周期 = len(items)/gcd(daily_count? no, 1, len) -> 21 天（每日 +1）")
# 实际:offset = ordinal % len -> 每日 +1 -> 21 天回到同一窗口
seen = {}
for k in range(21):
    d = date(2026, 9, 1) + timedelta(days=k)
    o = d.toordinal() % len(items)
    seen.setdefault(o, []).append(d)
print(f"21 天内出现的不同 offset 数 = {len(seen)}（0..20 全覆盖 = {sorted(seen)==list(range(21))}）")

print("\n### 2. 每个 offset 的窗口构成（复刻 collectible_items / scheduler 口径）")


def collectible(its):
    return [it for it in its if it.get("type") != "fullpage" or it.get("collect")]


print(f"{'off':>3} {'窗口5项':<60} {'collectible':<5} {'预警项':<5} {'scheduler有效':<6} {'module_items':<5} {'页数':<4} shot_names")
rows = []
for o in range(len(items)):
    win = [items[(o + k) % len(items)] for k in range(5)]
    ci = collectible(win)
    alert = [it for it in ci if (it.get("name") or "").startswith("预警")]
    nonalert = [it for it in ci if it not in alert]
    valid = [it for it in nonalert if it.get("name") in known]
    invalid = [it for it in nonalert if it.get("name") not in known]
    mod_items = [it for it in win if it.get("type") != "fullpage"]
    pages = sorted({it["page"] for it in mod_items if it.get("page")})
    shot_names = sorted({a for it in mod_items for a in (it.get("alias") or [it.get("name") or ""]) if a})
    rows.append((o, win, ci, alert, valid, invalid, mod_items, pages, shot_names))
    print(f"{o:>3} {'/'.join(i['name'] for i in win):<60} {len(ci):<5} {len(alert):<5} "
          f"{len(valid):<6} {len(mod_items):<5} {len(pages):<4} {shot_names}")
    if invalid:
        print(f"      ⚠ scheduler 路径 unknown（会告警跳过）: {[i['name'] for i in invalid]}")
    # run.py --rotation 路径：无预警特判
    rvalid = [it for it in ci if it.get("name") in known]
    rinvalid = [it for it in ci if it.get("name") not in known]
    if rinvalid:
        print(f"      ⚠ run.py --rotation 路径 unknown: {[i['name'] for i in rinvalid]}")

print("\n### 2b. 统计")
from collections import Counter
c_sched = Counter(len(r[4]) for r in rows)
c_mod = Counter(len(r[6]) for r in rows)
c_fp_nocollect = Counter(sum(1 for it in r[1] if it.get("type") == "fullpage" and not it.get("collect")) for r in rows)
print(f"scheduler 路径有效采集项数分布 = {dict(sorted(c_sched.items()))}")
print(f"module_items（截图模块项）数分布 = {dict(sorted(c_mod.items()))}")
print(f"窗口内『fullpage 且无 collect』项数分布 = {dict(sorted(c_fp_nocollect.items()))}")
print(f"→ 「每日实际只采 3 项」成立的天数比例 = "
      f"{sum(v for k, v in c_sched.items() if k == 3)}/21")

print("\n### 3. 失效两项的机制路径逐条核对")
for nm in ("市场分析", "预警-热点日历", "点评分析", "用户行为"):
    it = next(i for i in items if i.get("name") == nm)
    print(f"\n- {nm}: type={it.get('type')!r} collect={it.get('collect')!r} alias={it.get('alias')!r}")
    print(f"    in api_rules.sub_modules[*].name = {nm in known}")
    print(f"    collectible_items 保留 = {bool(it.get('type') != 'fullpage' or it.get('collect'))}")
    print(f"    会被 scheduler.py:72 startswith('预警') 摘出 = {nm.startswith('预警')}")
    print(f"    → 到达 split_known_items（scheduler 路径）= "
          f"{bool(it.get('type') != 'fullpage' or it.get('collect')) and not nm.startswith('预警')}")
    print(f"    → 到达 split_known_items（run.py --rotation 路径）= "
          f"{bool(it.get('type') != 'fullpage' or it.get('collect'))}")
    print(f"    → 截图路径（pusher._take_fullpage_shot 走 shot_url）= {it.get('type') == 'fullpage'}")

print("\n### 4. 三类/几类 type 实际取值")
print(Counter(it.get("type") for it in items))
print("fullpage 且 collect=true:", [it["name"] for it in items if it.get("type") == "fullpage" and it.get("collect")])
print("fullpage 且无 collect:", [it["name"] for it in items if it.get("type") == "fullpage" and not it.get("collect")])
print("带 url 的项:", [(it["name"], it.get("url")) for it in items if it.get("url")])
print("带 shot_name 的项:", [(it["name"], it.get("shot_name")) for it in items if it.get("shot_name")])

print("\n### 5. 全部 21 项字段形状（逐项原样）")
for i, it in enumerate(items, 1):
    print(f"[{i:02d}] {json.dumps(it, ensure_ascii=False)}")

OUT.write_text(_buf.getvalue(), encoding="utf-8")
sys.stdout = sys.__stdout__
print("written:", OUT)
