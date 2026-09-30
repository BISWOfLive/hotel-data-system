"""★ 距离与坐标(段3 T3B.4 —— 修 D13)。

D13 是什么
==========

旧系统 ``distance_km`` **恒为 ``None``** —— 但这不是"忘了算",而是**三处独立断链叠加**:

1. 携程接口 ``ctGetNearbyHotelList`` 的响应里**本来就有** ``position.lat`` / ``position.lng``,
   旧 ``ctrip.py:402/404`` 却把 ``"coord_source": ""`` 与 ``"distance_km": None`` **写死**;
2. 美团列表卡片**本来就带距离文本**(「距您查询的酒店直线1.2公里」,实拍截图确证),
   旧 ``meituan.py:426-434`` 把它映射成 ``distance`` 字段,然后在 ``:601/605``
   **又把 ``distance_km`` 写死 ``None``** 丢掉;
3. 唯一真正会算距离的代码是 ``base.py:90-123`` 的 ``_fill_coords`` / ``_rank_hits`` ——
   而它们**只由** ``BasePlatform.collect()`` 调用,该方法被两个子类**完整覆写**
   → **死代码**。``geo.best_coord`` **全仓零调用**。

于是所谓"距离最近"从来没在排序。段3 把它接上线。

为什么不能照搬旧 ``geo.py``
==========================

旧 :func:`deep_find_coords` 把整棵 JSON 树里所有坐标对**拍平成一个列表**
``list[(lat, lng)]`` —— **无法与酒店逐条对齐**。这是"抓到了坐标却接不上线"的
技术原因:拿到了 12 个坐标,但不知道哪个属于哪家店。

段3 的做法:**坐标必须挂在它所属的那家酒店上**。
所以本模块提供的是

* :func:`coords_from_obj` —— **单个对象**内找坐标(不递归到别人的数据里);
* :func:`parse_distance_text` —— 从卡片文本解析「直线 N 公里」;
* :func:`to_km` / :func:`parse_distance_km` —— 单位归一化;
* :func:`anchor_coords` + :func:`attach_distances` —— 组装成"每家店都有距离"。

单位策略(★ 只认"公里"是一种静默失败)
==================================

平台文本有 ``公里`` / ``km`` / ``米`` / ``m`` 多种写法。**只按"公里"解析会把"800米"
整条丢掉**(而不是算错),报告里就少一家店。所以 :func:`to_km` 显式处理米制。
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from typing import Any

from loguru import logger

__all__ = [
    "EARTH_RADIUS_KM",
    "attach_distances",
    "best_coords",
    "coords_from_obj",
    "first_coords",
    "haversine_km",
    "parse_distance_km",
    "parse_distance_text",
    "sort_by_distance",
    "to_km",
]

#: 地球平均半径(公里)—— ★ 逐字继承旧 ``geo.py:10``
EARTH_RADIUS_KM = 6371.0088

#: 纬度键名候选(旧 ``geo.py:18``,逐字)
_LAT_KEYS = ("lat", "latitude", "wgs84lat")
#: 经度键名候选(旧 ``geo.py:19``,逐字)
_LNG_KEYS = ("lng", "lon", "longitude", "wgs84lng")


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """两点球面距离(公里)。★ 逐字继承旧 ``geo.py:8-15``(含 ``r=6371.0088``)。"""
    r = EARTH_RADIUS_KM
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _norm_coord(value: Any) -> float | None:
    """转 float;非法/非有限 → ``None``(旧 ``geo.py:22-29``)。"""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return f


def _valid_pair(lat: float | None, lng: float | None) -> tuple[float, float] | None:
    """经纬度范围守卫(旧 ``geo.py:48``):纬度 ±90、经度 ±180。"""
    if lat is None or lng is None:
        return None
    if not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
        return None
    return (round(lat, 6), round(lng, 6))


def coords_from_obj(obj: Any, *, depth: int = 3) -> tuple[float, float] | None:
    """★ **单个对象**里找坐标 —— 沿对象自身及其直接子节点下探 ``depth`` 层。

    与旧 ``deep_find_coords`` 的关键差别:**作用域限定在这一条记录内**。
    旧实现从根节点递归全树,拿回的是一堆无法归属的坐标;

    段3 只在一家酒店的记录范围内查找,所以拿到的坐标**必定属于这家店**。

    典型命中(携程真接口,已实测确证)::

        {"base": {...}, "position": {"lat": "37.165587", "lng": "119.946814"}}

    键名候选与"必须同时有 lat 和 lng"的判定**逐字继承**旧 ``geo.py:18-19,48``。
    """
    found = _walk_for_coords(obj, depth)
    return found[0] if found else None


def _walk_for_coords(node: Any, depth: int) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    if depth < 0:
        return out
    if isinstance(node, dict):
        lat = lng = None
        for key, value in node.items():
            lk = str(key).lower()
            if lk in _LAT_KEYS:
                lat = _norm_coord(value)
            elif lk in _LNG_KEYS:
                lng = _norm_coord(value)
        pair = _valid_pair(lat, lng)
        if pair is not None:
            out.append(pair)
        # ★ 只在"没有直接命中"时下探:一家店的记录里有坐标就用它,
        #   不继续往子节点捞(否则会捞到同级的其他酒店)。
        if not out:
            for value in node.values():
                out.extend(_walk_for_coords(value, depth - 1))
    elif isinstance(node, list):
        for value in node:
            out.extend(_walk_for_coords(value, depth - 1))
    return out


def first_coords(candidates: Iterable[tuple[float, float] | None]) -> tuple[float, float] | None:
    """取第一个非空坐标(旧 ``geo.py:90-92`` ``best_coord`` 的语义)。"""
    for item in candidates:
        if item is not None:
            return item
    return None


#: ``best_coord`` —— 旧名保留(旧 ``geo.py:90``),便于对照考古结论
best_coords = first_coords


# ---------------------------------------------------------------------------
# 距离文本 → 公里
# ---------------------------------------------------------------------------

#: 「直线 1.2 公里」「距酒店 800 米」「1.2km」「0.5KM」「1,2公里」…
#:
#: ★ 同时匹配 公里/km/千米 与 米/m/公尺 —— **只认公里会把「800米」整条丢掉**,
#:   那是静默少一家店,比算错更隐蔽。
_DIST_RE = re.compile(
    r"(?P<num>\d{1,4}(?:[.,]\d{1,3})?)\s*"
    r"(?P<unit>公里|千米|km|米|公尺|m)",
    re.IGNORECASE,
)


def to_km(value: float, unit: str) -> float | None:
    """数值 + 单位 → 公里。"""
    u = unit.strip().lower()
    if u in ("公里", "千米", "km"):
        return value
    if u in ("米", "公尺", "m"):
        return value / 1000.0
    return None


def parse_distance_km(text: str | None) -> float | None:
    """从一段文本里解析距离(公里)。取**第一个**命中。

    为什么要取第一个而不是最小值:卡片文本形如
    「距您查询的酒店直线1.2公里 · 近青城山火车站」—— 后面可能还有别的数字,
    但**距离总是出现在最前面**(「距…直线 N 公里」是平台的固定句式)。
    """
    if not text:
        return None
    m = _DIST_RE.search(str(text))
    if not m:
        return None
    raw = m.group("num").replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        return None
    if value <= 0 or value > 3000:
        return None
    km = to_km(value, m.group("unit"))
    if km is None:
        return None
    return round(km, 3)


def parse_distance_text(text: str | None) -> tuple[float, str] | None:
    """解析距离并返回 ``(公里, 命中原文)`` —— 原文用于报告与诊断。"""
    if not text:
        return None
    m = _DIST_RE.search(str(text))
    if not m:
        return None
    km = parse_distance_km(text)
    if km is None:
        return None
    return km, m.group(0)


def attach_distances(
    items: list[dict[str, Any]],
    anchor: tuple[float, float] | None,
    *,
    city_fallback: tuple[float, float] | None = None,
) -> list[dict[str, Any]]:
    """给每条记录补上 ``distance_km`` / ``coords`` / ``coord_source``。

    优先级(段3 §5.5 的三路提取,这里实现为**逐条**而不是全树):

    1. 记录自带坐标(接口字段)→ ``coord_source="api"`` → haversine;
    2. 记录自带距离文本(美团卡片)→ ``coord_source="card"``;
    3. 锚点坐标缺失但给了 ``city_fallback`` → ``coord_source="city"`` 且 **标 degraded**;
    4. 都没有 → ``distance_km=None`` + ``coord_source="none"``
       → 报告必须**显式标注「距离不可用」**(不许假装排过序)。

    就地修改并返回同一个列表(调用方持有同一批 dict)。
    """
    for item in items:
        if item.get("distance_km") is not None:
            continue

        coords = item.get("coords")
        if coords is None:
            coords = coords_from_obj(item)
            if coords is not None:
                item["coords"] = coords
                item.setdefault("coord_source", "api")

        # ① 有坐标 + 有锚点坐标 → 真算
        if coords is not None and anchor is not None:
            item["distance_km"] = round(haversine_km(anchor[0], anchor[1], coords[0], coords[1]), 3)
            item.setdefault("coord_source", "api")
            continue

        # ② 有距离文本(美团卡片)→ 直接用
        km = parse_distance_km(item.get("distance_text"))
        if km is not None:
            item["distance_km"] = km
            item["coord_source"] = "card"
            continue

        # ③ 锚点坐标缺失但有城市中心 → 降级(必须标出来)
        if coords is not None and anchor is None and city_fallback is not None:
            item["distance_km"] = round(
                haversine_km(city_fallback[0], city_fallback[1], coords[0], coords[1]), 3
            )
            item["coord_source"] = "city"
            item["degraded"] = True
            logger.debug("坐标降级到城市中心:{}", item.get("hotel_name") or item.get("name"))
            continue

        # ④ 拿不到 → **显式写 None**(不留空键:消费方若用 item["distance_km"]
        #    会炸 KeyError,而那是"报告生成到一半失败",比"距离不可用"糟得多)
        item["distance_km"] = None
        item.setdefault("coord_source", "none")

    return items


def sort_by_distance(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按距离升序;**距离为 None 的排最后**(旧 ``runner.py:113`` 同款语义)。

    旧系统这段排序逻辑本身是对的 —— 它从来没生效只是因为 ``distance_km`` 恒为 ``None``。
    """
    return sorted(
        items,
        key=lambda x: (
            x.get("distance_km") is None,
            x.get("distance_km") if x.get("distance_km") is not None else 1e9,
        ),
    )
