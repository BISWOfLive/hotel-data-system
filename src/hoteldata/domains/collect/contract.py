"""★ 提取器契约(段1 最重要的新设计)。

旧系统的教训(总纲 3.4)
------------------------
比价域的"平台插件"是**假的**:``comparator/platforms/base.py`` 定义了类属性 +
模板方法 ``collect``,但 ``ctrip.py`` **没有实现任何钩子** → 基类模板方法是**死代码**;
真实契约是一个**未声明的方法** ``collect_map_prices()``,靠 ``hasattr`` 探测
(``runner.py:264``),返回**裸 dict 无 schema**,异常被吞成 ``result["error"]``。

段1 要做一个**真的**契约:

  - **类型化返回** → 四态不会被"随手返回个 dict"绕过;
  - **契约显式声明** → 不再靠 ``hasattr`` 探测;
  - **异常有明确语义** → 不再被吞成 ``result["error"]``;
  - **段3 的比价提取器可以直接实现同一个 :class:`Extractor`**,复用会话/限频/落盘/四态。

四态语义(**必须原样继承**)
--------------------------
============  ============================================================  ==========
状态          产生条件                                                      是否算失败
============  ============================================================  ==========
``ok``        API payload 非空且无失败接口                                  否
``degraded``  部分接口失败;或接口响应但未提取到字段;或浏览器兜底成功        否
``no_data``   ``no_data_path`` 命中空值(None / "" / [] / {})               **否**
``failed``    浏览器兜底本身抛异常                                          **是**
============  ============================================================  ==========

顶层汇总规则:任一 ``failed`` → ``failed``;否则任一 ``degraded`` → ``degraded``;
``no_data`` 视为正常(顶层状态永远不会是 ``no_data``)。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

__all__ = [
    "EXTRACT_CHANNELS",
    "EXTRACT_STATUSES",
    "AccountRef",
    "ExtractContext",
    "ExtractResult",
    "ExtractStatus",
    "ExtractTarget",
    "Extractor",
    "HotelRef",
    "HttpRef",
    "LayoutRef",
    "LimiterRef",
    "SessionRef",
    "worst_status",
]

ExtractStatus = Literal["ok", "degraded", "no_data", "failed"]
ExtractChannel = Literal["api", "browser", "screenshot", "fullpage"]

EXTRACT_STATUSES: tuple[ExtractStatus, ...] = ("ok", "degraded", "no_data", "failed")
EXTRACT_CHANNELS: tuple[ExtractChannel, ...] = ("api", "browser", "screenshot", "fullpage")

#: 「明确无数据」判定 —— None / 空串 / 空列表 / 空 dict(**逐字继承**)
#: 不算空的值:``0`` / ``0.0`` / ``False`` / ``"0"`` / ``" "`` / ``[0]`` / ``{"a":1}``


def is_empty_value(v: Any) -> bool:
    """「明确无数据」判定(旧 ``api_collector._is_empty_value`` 逐字继承)。"""
    if v is None:
        return True
    if isinstance(v, (str, list, dict)):
        return len(v) == 0
    return False


def worst_status(statuses: list[str]) -> str:
    """顶层汇总:``failed`` > ``degraded`` > (``ok``/``no_data``)。

    ★ ``no_data`` **不参与降级**;缺 ``status`` 键按 ``ok`` 计;
    返回值**永远不会是** ``no_data``。
    """
    if "failed" in statuses:
        return "failed"
    if "degraded" in statuses:
        return "degraded"
    return "ok"


# ---------------------------------------------------------------------------
# 引用类型(域层只见接口,不见实现 —— 硬约束 2/3)
# ---------------------------------------------------------------------------


@runtime_checkable
class SessionRef(Protocol):
    """登录态引用(**不暴露实现**)。

    实现方是 :class:`hoteldata.infra.session_store.SessionStore`。
    """

    platform: str
    role: str
    alias: str

    def cookie_header(self) -> str:
        """``k1=v1; k2=v2``(分号 + 一个空格)。无 cookie 时抛异常。"""
        ...

    def storage_state_path(self) -> Path:
        """``var/states/<platform>__<role>__<alias>.json``。"""
        ...

    def exists(self) -> bool: ...


@runtime_checkable
class LimiterRef(Protocol):
    """限频引用(两层:账号级串行 + 平台级全局限频)。"""

    def request(self, platform: str, account: str) -> Any:
        """异步上下文管理器;``async with limiter.request(p, a):`` 即取到配额。"""
        ...


@runtime_checkable
class HttpRef(Protocol):
    """HTTP 引用(实现方是 :class:`hoteldata.infra.http.HttpClient`)。

    ``request()`` **不抛网络异常**,把失败装进返回对象,让上游按四态语义决定
    ``degraded`` / ``failed``。
    """

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = ...,
        params: dict[str, Any] | None = ...,
        json_body: Any = ...,
        content: str | bytes | None = ...,
        timeout: float | None = ...,
        follow_redirects: bool = ...,
        retry: Any = ...,
    ) -> Any: ...


@runtime_checkable
class LayoutRef(Protocol):
    """产物落盘布局引用。"""

    def raw_api_path(
        self,
        hotel: str,
        day: date,
        page: str,
        module: str,
        window: str,
        api_name: str,
        *,
        ts: Any = None,
    ) -> Path: ...

    def raw_json_path(
        self, hotel: str, day: date, page: str, module: str, window: str, *, ts: Any = None
    ) -> Path: ...

    def screenshot_path(self, hotel: str, day: date, container: str, key: str, *, ext: str = ...) -> Path: ...

    def to_relative(self, path: Path | str) -> str: ...

    def from_relative(self, rel: str) -> Path: ...


# ---------------------------------------------------------------------------
# 上下文
# ---------------------------------------------------------------------------


class HotelRef(BaseModel):
    """酒店(最小信息集)。"""

    model_config = ConfigDict(frozen=True)

    id: int
    name: str
    city: str | None = None
    ebk_hotel_id: str | None = None


class AccountRef(BaseModel):
    """账号(最小信息集;★ **不含凭据**)。"""

    model_config = ConfigDict(frozen=True)

    id: int | None = None
    alias: str
    platform: str
    is_multi: bool = False


class ExtractTarget(BaseModel):
    """提取目标(数据中心提取器用:页 × 模块 × 窗口)。"""

    model_config = ConfigDict(frozen=True)

    page: str
    module: str
    window: str

    @property
    def key(self) -> str:
        return f"{self.page}/{self.module}/{self.window}"

    def __str__(self) -> str:  # pragma: no cover - 展示用
        return self.key


class ExtractContext(BaseModel):
    """所有提取器的统一入参。

    ⚠️ ``session`` / ``limiter`` / ``layout`` 都是 **Protocol 引用**:
    域层通过它们取能力,但**看不到实现**(硬约束 2:域之间不互相 import;
    硬约束 3:域之间不直接 join 别人的表)。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    hotel: HotelRef
    account: AccountRef
    collect_date: date
    session: SessionRef
    limiter: LimiterRef
    layout: LayoutRef
    http: HttpRef
    #: 逐接口超时(秒),继承 ``API_TIMEOUT_S=20``
    api_timeout_s: float = 20.0
    #: 是否把逐接口原始响应落盘(默认开,便于以后校准规则)
    persist_raw: bool = True

    @property
    def hotel_id(self) -> int:
        return self.hotel.id

    @property
    def account_id(self) -> int | None:
        return self.account.id

    @property
    def platform(self) -> str:
        return self.account.platform

    def multi_store_hotel_id(self) -> str | None:
        """多店账号且登记了 ``ebk_hotel_id`` 时返回门店 id(字符串),否则 None。"""
        if self.account.is_multi and self.hotel.ebk_hotel_id:
            return str(self.hotel.ebk_hotel_id)
        return None


# ---------------------------------------------------------------------------
# 结果
# ---------------------------------------------------------------------------


class ExtractResult(BaseModel):
    """★ 所有提取器的统一返回。**禁止返回裸 dict。**"""

    status: ExtractStatus
    channel: ExtractChannel
    #: 结构化数据(单记录语义:``{label: value}``)
    payload: dict[str, Any] | None = None
    #: 批量提取器(批次 D:房态网格 / 预警三源 / 点评列表)一次返回多行
    records: list[dict[str, Any]] | None = None
    error: str | None = None
    #: ★ **相对路径**(绝对路径换机器即失效)
    raw_path: str | None = None
    #: 逐接口的成败明细
    detail: dict[str, Any] | None = None
    #: 提取目标(数据中心提取器填;批次 D 可空)
    target: ExtractTarget | None = None
    duration_ms: float | None = None

    @property
    def record_count(self) -> int:
        if self.records is not None:
            return len(self.records)
        return len(self.payload or {})

    @property
    def is_failure(self) -> bool:
        """★ ``no_data`` **不算失败**(继承语义)。"""
        return self.status == "failed"

    def summary(self) -> dict[str, Any]:
        """``job_runs.summary`` / CLI 进度行用的紧凑摘要。"""
        return {
            "page": self.target.page if self.target else None,
            "module": self.target.module if self.target else None,
            "window": self.target.window if self.target else None,
            "status": self.status,
            "channel": self.channel,
            "indicators": self.record_count,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# 提取器协议
# ---------------------------------------------------------------------------


@runtime_checkable
class Extractor(Protocol):
    """提取器契约。四个提取器都实现它:``datacenter`` / ``portal`` / ``room`` / ``review``。

    ★ 段3 的比价提取器直接实现同一个 Protocol,即可复用会话/限频/落盘/四态。
    """

    #: ``datacenter`` / ``portal`` / ``room`` / ``review``
    name: str

    async def extract(self, ctx: ExtractContext, **kwargs: Any) -> list[ExtractResult]:
        """执行提取。**不得返回裸 dict**;不得吞异常(失败要落 ``failed``)。"""
        ...


class ExtractorError(RuntimeError):
    """提取器基类异常(**有明确语义**,不被吞成 ``result["error"]``)。"""


class ApiCollectError(ExtractorError):
    """规则缺失、网络失败、解析失败等(非登录类)→ 触发浏览器降级。"""


class PlaceholderError(ApiCollectError):
    """占位符守卫命中 → 触发浏览器降级。

    ★ 防「静默错数据」:绝不携带空日期或占位符原文请求平台。
    """


class LoginExpiredError(ExtractorError):
    """登录态失效(401/403、302→login、响应体含未登录标记)→ 触发重登 + 降级。"""


class NeedRecordError(ApiCollectError):
    """``need_record=true``:接口未校准 → **直接降级浏览器,不猜**(S1 对策)。"""


__all__ += [
    "ApiCollectError",
    "ExtractorError",
    "LoginExpiredError",
    "NeedRecordError",
    "PlaceholderError",
    "is_empty_value",
]
