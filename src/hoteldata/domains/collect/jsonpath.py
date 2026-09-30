"""``fields[].path`` 的 JSON 取值 —— **A 级遗产**。

旧系统 ``collectors/api_collector.py`` 的 ``_json_get``:

  * 路径按 ``.`` 分段;
  * 段形如 ``[N]`` 时按**下标**处理;
  * **必须写 ``data.[0].tip1``** —— ``data[0].x`` 会返回 ``None``(旧实现不解析
    粘在名字后面的下标),这是实测踩过的坑,不要"顺手优化"成兼容两种写法,
    否则历史规则会走到与旧系统不同的分支。

> ⚠️ 旧实现对**负下标**(如 ``[-99]``)会抛未捕获的 ``IndexError``。
> 新实现按"越界 → ``None``"处理并记录,不让一条坏规则炸掉整批采集
> (这是有意的健壮性改进,不改变合法路径的语义)。
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["MISSING", "json_get", "json_get_with_flag"]

#: 路径不存在的哨兵(与"值为 None"区分:旧语义里两者都返回 None,
#: 但 ``no_data_path`` 判定需要区分"路径不存在"与"值是空值")
MISSING: Any = object()

_INDEX_RE = re.compile(r"^\[(-?\d+)\]$")


def _iter_segments(path: str) -> list[str]:
    return [seg for seg in str(path or "").split(".") if seg != ""]


def json_get(data: Any, path: str, default: Any = None) -> Any:
    """按点号路径取值;失败返回 ``default``。

    >>> json_get({"data": [{"tip1": 3}]}, "data.[0].tip1")
    3
    >>> json_get({"data": [{"tip1": 3}]}, "data[0].tip1") is None
    True
    """
    value = _walk(data, path)
    return default if value is MISSING else value


def json_get_with_flag(data: Any, path: str) -> tuple[Any, bool]:
    """返回值与"路径是否存在"标志(``no_data_path`` 判定需要)。"""
    value = _walk(data, path)
    if value is MISSING:
        return None, False
    return value, True


def _walk(data: Any, path: str) -> Any:
    cur = data
    for seg in _iter_segments(path):
        if cur is None:
            return MISSING
        m = _INDEX_RE.match(seg)
        if m:
            if not isinstance(cur, (list, tuple)):
                return MISSING
            idx = int(m.group(1))
            if idx < 0 or idx >= len(cur):
                return MISSING
            cur = cur[idx]
            continue
        if isinstance(cur, dict):
            if seg not in cur:
                return MISSING
            cur = cur[seg]
            continue
        if isinstance(cur, (list, tuple)):
            # 列表上的非下标段:对每个元素取该键(容错,旧实现直接失败)
            collected = []
            for item in cur:
                if isinstance(item, dict) and seg in item:
                    collected.append(item[seg])
            if not collected:
                return MISSING
            cur = collected
            continue
        return MISSING
    return cur
