"""★ 声明式平台注册表(段3 T3A.2)。

旧系统(段3 §4.3)
================

``comparator/platforms/__init__.py`` 里注册表是**硬编码**的
(``create_platform`` 一串 ``if name == "ctrip": ... elif name == "meituan": ...``)
→ **新增一个平台要改 runner 代码**,这正是"改一处漏三处"的来源之一。

段3 的写法
==========

.. code-block:: python

    @register
    class CtripPlatform:
        name = "ctrip"
        ...

    # 新增平台 = 加一个类 + 一行装饰器;runner **一行不改**

两条纪律
========

1. **未知平台抛错,不静默返回 ``None``** —— 旧系统那种"拿不到就返回空"的写法
   会让配置里把 ``hoteL_platforms`` 拼错变成"今天没有附近酒店"。
2. **重复注册抛错** —— 两个类抢同一个 ``name`` 是装配错误,必须启动即失败。
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from hoteldata.domains.compare.contract import PLATFORMS

__all__ = [
    "PlatformNotRegistered",
    "available_platforms",
    "create_platform",
    "get_platform_class",
    "register",
    "resolve_platforms",
]


class PlatformNotRegistered(LookupError):
    """请求了一个没注册的平台。**抛错,不返回 None。**"""


#: ``{平台名: 平台类}`` —— 唯一注册表
_REGISTRY: dict[str, type[Any]] = {}


def register(cls: type[Any]) -> type[Any]:
    """``@register`` 装饰器:把一个平台类登记进注册表。

    校验:

    * 必须有非空 ``name``(否则无法寻址);
    * ``name`` 不能重复(重复 = 装配错误);
    * ``name`` 必须是契约里声明的平台之一(挡住拼写错误,如 ``"ctrp"``)。

    ★ 第三条是**故意加的严格性**:旧系统 ``HOTEL_PLATFORMS`` 拼错只会静默少跑一个平台。
    """
    name = getattr(cls, "name", None)
    if not name or not isinstance(name, str):
        raise TypeError(f"{cls.__name__} 缺少非空类属性 name,无法注册为比价平台")
    if name in _REGISTRY:
        existing = _REGISTRY[name]
        raise ValueError(
            f"平台名 {name!r} 重复注册:{existing.__name__} 与 {cls.__name__}。"
            "两个类抢同一个 name 属于装配错误"
        )
    if name not in PLATFORMS:
        raise ValueError(
            f"{cls.__name__}.name={name!r} 不在契约声明的平台里 {PLATFORMS}。"
            "拼错的平台名会让配置静默失效,所以这里直接拒绝"
        )
    required = ("resolve_anchor", "collect_quotes")
    missing = [m for m in required if not callable(getattr(cls, m, None))]
    if missing:
        raise TypeError(
            f"{cls.__name__} 未实现 PricePlatform 的方法:{missing}"
            "(契约只有 resolve_anchor 与 collect_quotes 两个方法 —— "
            "提候选与取价在**同一次页面访问**里完成,见 contract.py 模块文档)"
        )
    _REGISTRY[name] = cls
    logger.debug("比价平台已注册:{} → {}", name, cls.__name__)
    return cls


def available_platforms() -> tuple[str, ...]:
    """已注册的平台名(排序,便于断言与日志稳定)。"""
    return tuple(sorted(_REGISTRY))


def get_platform_class(name: str) -> type[Any]:
    """按名取平台类。**未知 → 抛 :class:`PlatformNotRegistered`。**"""
    key = (name or "").strip().lower()
    cls = _REGISTRY.get(key)
    if cls is None:
        raise PlatformNotRegistered(
            f"未注册的比价平台 {name!r};已注册:{available_platforms() or '(空)'}"
        )
    return cls


def create_platform(name: str, **kwargs: Any) -> Any:
    """实例化一个平台。未知平台**抛错**。"""
    return get_platform_class(name)(**kwargs)


def resolve_platforms(raw: str | list[str] | tuple[str, ...] | None) -> list[str]:
    """把配置里的平台列表解析成**已注册且去重保序**的平台名。

    ``None`` / 空 → 全部已注册平台。

    ★ 未注册的名字**抛错**(不静默跳过):``HOTEL_PLATFORMS=ctrp,meituan`` 应当
      在启动时就炸,而不是安静地只跑美团 —— 后者会让人以为"携程今天没数据"。
    """
    if raw is None:
        items: list[str] = []
    elif isinstance(raw, str):
        items = [p.strip() for p in raw.replace("，", ",").split(",")]
    else:
        items = [str(p).strip() for p in raw]
    wanted = [p.lower() for p in items if p]

    if not wanted:
        return list(available_platforms())

    unknown = [p for p in wanted if p not in _REGISTRY]
    if unknown:
        raise PlatformNotRegistered(
            f"HOTEL_PLATFORMS 里的平台未注册:{unknown};已注册:{available_platforms()}"
        )
    seen: dict[str, None] = {}
    for p in wanted:
        seen.setdefault(p, None)
    return list(seen)
