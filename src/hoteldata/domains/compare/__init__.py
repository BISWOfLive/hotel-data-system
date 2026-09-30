"""★ 段3 比价域(``domains/compare``)。

比价域回答一个问题:**「美团/携程前台的房价,怎么按点取回来、比出来、发出去」**。

本包对外的三样东西
==================

===============================  ==========================================================
名字                              用途
===============================  ==========================================================
:func:`load_platforms`           触发 ``@register`` 注册(**必须显式调用**,见下)
:class:`CompareService`          ``runtime.compare()`` 取到的域服务(CLI / jobs 的唯一入口)
:data:`__all__` 里的契约类型       :class:`HotelQuote` / :class:`CompareResult` / 三态异常
===============================  ==========================================================

★ 为什么平台注册要显式 ``load_platforms()``,而不是在 ``__init__`` 里自动 import
==============================================================================

``platforms/ctrip.py`` 需要 import 本包的 ``contract`` / ``geo`` / ``human`` / ``price``,
而它在模块顶层带 ``@register`` —— 于是::

    domains.compare.__init__  →  platforms.ctrip  →  domains.compare.contract
                                     ↑                              │
                                     └──── 回到 domains.compare ←────┘

形成**真实的循环**:``__init__`` 还没跑完,``contract`` 已经在 import 它。
Python 能靠"部分初始化模块"侥幸跑过去,但那是**依赖运气**,
换成任何一次 import 顺序变化就会 ``ImportError``。

所以:``__init__`` **不 import 平台**,只暴露 :func:`load_platforms`;
runner / CLI 在要用平台时显式调一次(幂等)。这样依赖方向永远是
``platforms → contract`` 单向,符合总纲 §7.2 的硬约束 1。

★ 与段1/段2 的边界(硬约束)
==========================

* 浏览器池 / 登录态 / 限频 / 任务注册 **全部复用段1**,本包**不新建**;
* 推送**只经** ``push/service.py``,本包**不直连机器人**;
* 会话角色**:data:`~hoteldata.infra.session_store.ROLE_OTA``(携程前台)/
  ``ota_meituan``(美团前台)—— 沿用段1 建表时已声明的值,**不另起名字**。
"""

from __future__ import annotations

from typing import Any

from hoteldata.domains.compare.contract import (
    AnchorRef,
    CompareResult,
    HotelQuote,
    HumanVerificationError,
    Platform,
    PriceFatalError,
    PricePlatform,
    PriceRetryableError,
    PriceScope,
    PriceSource,
    QuotesPage,
)
from hoteldata.domains.compare.registry import (
    PlatformNotRegistered,
    available_platforms,
    create_platform,
    get_platform_class,
    register,
    resolve_platforms,
)

__all__ = [
    "AnchorRef",
    "CompareResult",
    "FRONT_DESK_ENTRY_URLS",
    "HumanVerificationError",
    "HotelQuote",
    "Platform",
    "PlatformNotRegistered",
    "PriceFatalError",
    "PricePlatform",
    "PriceRetryableError",
    "PriceScope",
    "PriceSource",
    "QuotesPage",
    "available_platforms",
    "create_platform",
    "get_platform_class",
    "load_platforms",
    "register",
    "resolve_platforms",
]

#: ★ **前台**登录入口(与段1 ``LOGIN_ENTRY_URLS`` 的**商家后台**入口不同)
#:
#: 段1 的 ``LOGIN_ENTRY_URLS``(``manager.py:105-108``)是
#: ``ebooking.ctrip.com`` / ``e.meituan.com`` —— 那是**商家后台**。
#: 比价要的是**公开前台**,两者 cookie 域与登录态语义都不同
#: (旧系统正是在这里栽的:它把前台登录态放在根目录的
#: ``storage_state_ctrip.json``,绕开了账号库)。
#:
#: 所以段3 自带这两个入口 —— 而且**人要去的前台页面,就是取价要用的那个页面**。
FRONT_DESK_ENTRY_URLS: dict[str, str] = {
    # 携程前台首页(进入任意酒店详情页即可看到房价)
    "ctrip": "https://hotels.ctrip.com/",
    # 美团 H5 酒店搜索页(登录后仍停在这个域)
    "meituan": "https://i.meituan.com/awp/h5/hotel/search/search.html",
}

#: 内置平台模块(新增平台 = 加一个模块 + 一行;``runner`` 一行不改)
_PLATFORM_MODULES: tuple[str, ...] = (
    "hoteldata.domains.compare.platforms.ctrip",
    "hoteldata.domains.compare.platforms.meituan",
)

_loaded = False


def load_platforms(*, force: bool = False) -> tuple[str, ...]:
    """导入内置平台模块以触发 ``@register``。**幂等。**

    返回注册表里的平台名(便于启动日志与断言)。
    """
    global _loaded
    if _loaded and not force:
        return available_platforms()

    import importlib

    for mod in _PLATFORM_MODULES:
        try:
            importlib.import_module(mod)
        except ImportError as exc:  # pragma: no cover - 装配错误
            # ★ 不静默跳过:少一个平台 = "这个平台今天没数据",必须可见
            from loguru import logger

            logger.error("比价平台模块导入失败 {}: {}", mod, exc)
            raise
    _loaded = True
    return available_platforms()


def compare_service(runtime: Any) -> Any:
    """取域服务(``Runtime.compare()`` 的便捷入口;避免调用方 import 服务类)。"""
    return runtime.compare()
