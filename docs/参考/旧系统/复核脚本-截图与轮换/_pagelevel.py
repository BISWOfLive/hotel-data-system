# -*- coding: utf-8 -*-
import io
import json
import sys
from pathlib import Path

OLD = Path(r"D:\AAAAaaaa\Pythooooooooooooon\hotel-data-system")
OUT = Path(r"D:\AAAAaaaa\pypypypy\hotel-data-system\docs\参考\旧系统\_pagelevel_out.txt")
rules = json.loads((OLD / "config" / "api_rules.json").read_text(encoding="utf-8"))
buf = io.StringIO()
for p, cfg in rules["pages"].items():
    buf.write(f"page={p!r}\n  page_level_screenshot_modules="
              f"{json.dumps(cfg.get('screenshot_modules'), ensure_ascii=False)}\n")
OUT.write_text(buf.getvalue(), encoding="utf-8")
print("ok")
