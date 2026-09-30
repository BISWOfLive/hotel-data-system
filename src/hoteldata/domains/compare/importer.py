"""旧比价清单导入(T3A.3)—— ``compare_hotels.txt`` + ``price_targets.txt`` → 表。

★ 为什么会有这一步(段3 §5.4 对总纲的一处修正)
============================================

总纲 §7.3 曾把 ``config/compare_hotels.txt`` 放在 ``config/``;段3 修正为**表**
(``cmp_price_targets``)。理由与总纲 §7.6 自己的原则冲突有关:

> 总纲定的边界是:**``config/*.json`` 放"领域规则:只增不改义";PG 放"业务实体:可日常增删改"**。
> 一个**会经常增删酒店**的清单,显然是**业务实体**而不是规则。

旧系统把它做成**两个** txt(``compare_hotels.txt`` 批量 + ``price_targets.txt`` 定时),
正是"配置与业务实体混装"的老毛病。段3 合成一张表,用 ``mode`` 区分 ——
所以导入要**同时读两个文件**,分别打成 ``batch`` / ``cron``。

★ 实测:两个文件都是 **GBK** 编码
================================

直接 ``read_text()``(默认 UTF-8)会 ``UnicodeDecodeError`` 或读出乱码。
实测用 **GB18030** 读正确(GB18030 是 GBK 的超集,能覆盖这两个文件)。
段3 按 ``utf-8 → gb18030`` 顺序试,而不是写死一种 —— 因为**手工编辑过的
txt 完全可能是 UTF-8**(用户拿记事本改过就会存成 UTF-8)。

★ 行格式(实测)
==============

::

    # 开头是注释
    隐欲民宿|莱州
    盛铂仕丹酒店(青城山景区高铁站店)|都江堰
    嫣杭民宿|开封
    静荷民宿(蒙自市政府店)|蒙自

即 ``酒店名|城市``;**没有 ``|`` 的行**视为"只有酒店名"(城市走默认)。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

__all__ = [
    "LEGACY_FILES",
    "default_import_sources",
    "parse_target_file",
    "parse_target_files",
]

#: 旧文件名 → 对应的 ``mode``(段3 §5.4 的映射)
LEGACY_FILES: dict[str, str] = {
    "compare_hotels.txt": "batch",
    "price_targets.txt": "cron",
}

#: 读取编码顺序(实测文件是 GBK;但手工编辑过的可能是 UTF-8)
_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030")


def _read_text(path: Path) -> str:
    """按候选编码依次尝试读取(失败即换下一种)。"""
    last: Exception | None = None
    for enc in _ENCODINGS:
        try:
            return path.read_text(encoding=enc)
        except (UnicodeDecodeError, LookupError) as exc:
            last = exc
            continue
    logger.warning("无法读取 {}:{}", path, last)
    return ""


def parse_target_file(path: Path, mode: str) -> list[dict[str, Any]]:
    """解析一个清单文件 → 目标 dict 列表。

    规则:

    * ``#`` 开头的行 → 注释,跳过;
    * 空行 → 跳过;
    * 含 ``|`` → ``酒店名|城市``(两段都 strip);
    * 不含 ``|`` → 只有酒店名,城市为 ``None``;
    * 同一个文件内**同名同城去重**(保留首次出现)。
    """
    text = _read_text(path)
    if not text:
        return []

    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None]] = set()
    for raw in text.splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        name, sep, city = line.partition("|")
        anchor = name.strip()
        city_v = city.strip() if sep else ""
        if not anchor:
            continue
        city_norm = city_v or None
        key = (anchor, city_norm)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "anchor_name": anchor,
                "city": city_norm,
                "mode": mode,
                "platforms": ["ctrip", "meituan"],
                "nights": 1,
                "enabled": True,
                "remark": f"imported from {path.name}",
            }
        )
    logger.info("从 {} 解析出 {} 个目标(mode={})", path.name, len(out), mode)
    return out


def parse_target_files(root: Path | str) -> list[dict[str, Any]]:
    """解析旧系统根目录下的两个清单(**合并去重,但保留"两处都列"的事实**)。

    ``root`` 是**旧系统根目录**(``config/`` 的父目录)。

    ★ 关键:**同一个酒店同时出现在两个文件里,不能只留一个 mode**。

    实测旧清单::

        compare_hotels.txt   隐欲民宿|莱州 · 盛铂仕丹酒店|都江堰 · 嫣杭民宿|开封 · 静荷民宿|蒙自
        price_targets.txt    盛铂仕丹酒店|都江堰

    盛铂仕丹**两处都有** —— 它在旧系统里的含义是:
    「它既在 01:00 的批量清单里,**也**在 08:30/13:30/17:30 的定时采集里」。

    如果按"同名去重只留先到的"处理,它就只剩 ``mode='batch'``,
    于是 ``compare.collect``(定时采集)**一个目标都没有** ——
    而段3 的 09:00 日报比价段**正是靠定时采集喂的数据**(P8 的 slot 语义)。
    那会让"日报里有价格段"这条验收(V82)在数据层就断掉。

    所以:同名同城**合并为一条** ``mode='both'``,两个任务都能选到它。
    """
    base = Path(root)
    config = base / "config" if (base / "config").is_dir() else base

    by_key: dict[tuple[str, str | None], dict[str, Any]] = {}
    for fname, mode in LEGACY_FILES.items():
        path = config / fname
        if not path.exists():
            logger.debug("旧清单不存在,跳过:{}", path)
            continue
        for item in parse_target_file(path, mode):
            key = (item["anchor_name"], item["city"])
            existing = by_key.get(key)
            if existing is None:
                by_key[key] = item
                continue
            # 已存在 → 合并 mode
            modes = {existing["mode"], item["mode"]}
            existing["mode"] = "both" if len(modes) > 1 else existing["mode"]
            existing["remark"] = f"{existing.get('remark', '')} + {item.get('remark', '')}".strip()
            logger.debug(
                "目标两处都列,合并为 mode={}:{} / {}",
                existing["mode"],
                item["anchor_name"],
                item["city"],
            )
    return list(by_key.values())


def default_import_sources(root: Path | str) -> dict[str, Path]:
    """列出实际存在的旧清单文件(供 CLI 打印"将导入哪些")。"""
    base = Path(root)
    config = base / "config" if (base / "config").is_dir() else base
    out: dict[str, Path] = {}
    for fname in LEGACY_FILES:
        path = config / fname
        if path.exists():
            out[fname] = path
    return out
