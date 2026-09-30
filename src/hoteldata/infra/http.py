"""httpx 客户端工厂 + tenacity 重试策略。

段1 T3.3/T3.9 的两条纪律:

1. **超时 20 秒**(``API_TIMEOUT_S``,旧 ``config.py:50`` 逐字继承)。
2. ★ **重试只给幂等 GET**:退避 ``[2, 8, 30]`` 秒;
   **POST(SOA 查询)不重试** —— 重试会放大风控(段1 T3.9 明文要求)。
   旧系统采集链路是**零重试**(单次失败即抛),这里补上但**有上限、有选择**。

另外:登录失效三路检测需要看到 **302 本身**,所以默认 ``follow_redirects=False``
(旧系统实测:``allow_redirects=False`` 才能看到 302)。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from hoteldata.settings import RetrySettings, Settings, get_settings

__all__ = ["HttpAttempt", "HttpClient", "build_async_client"]

#: 触发重试的网络类异常
RETRYABLE_EXC = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
    httpx.NetworkError,
)

#: 触发重试的 HTTP 状态码(服务端临时问题)
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def build_async_client(settings: Settings | None = None) -> httpx.AsyncClient:
    """构造共享的 ``httpx.AsyncClient``。"""
    s = settings or get_settings()
    limits = httpx.Limits(
        max_connections=max(20, s.rate_limit.max_concurrent_accounts * 4),
        max_keepalive_connections=max(10, s.rate_limit.max_concurrent_accounts * 2),
    )
    timeout = httpx.Timeout(
        connect=min(10.0, s.api_timeout_s),
        read=s.api_timeout_s,
        write=s.api_timeout_s,
        pool=s.api_timeout_s,
    )
    return httpx.AsyncClient(
        timeout=timeout,
        limits=limits,
        follow_redirects=False,  # ★ 必须:要看到 302 才能判登录失效
        headers={
            "accept": "*/*",
            "accept-language": "zh-CN,zh;q=0.9",
        },
        http2=False,
    )


@dataclass(slots=True)
class HttpAttempt:
    """一次请求的完整结果(**含失败的请求**,便于逐接口落盘与降级判定)。"""

    url: str
    method: str
    status_code: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    text: str = ""
    response: httpx.Response | None = None
    error: str | None = None
    exception: BaseException | None = None
    attempts: int = 0
    elapsed_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None and self.status_code is not None

    @property
    def is_success(self) -> bool:
        return self.ok and 200 <= (self.status_code or 0) < 300

    def json(self) -> Any:
        """宽容取 JSON;失败返回 ``None``(调用方按"未提取到字段"处理)。"""
        if self.response is None:
            return None
        try:
            return self.response.json()
        except Exception:  # noqa: BLE001 - 非 JSON 响应
            return None

    def location(self) -> str:
        for key, value in self.headers.items():
            if key.lower() == "location":
                return value
        return ""

    def brief(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "method": self.method,
            "status_code": self.status_code,
            "attempts": self.attempts,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "error": self.error,
        }


class HttpClient:
    """带重试策略的薄封装。

    ``request()`` **不抛网络异常**(把失败装进 :class:`HttpAttempt`),
    让上游按四态语义决定 ``degraded`` / ``failed``;但 ``LoginExpiredError``
    这类**语义异常**由 API 通道自己判(它需要先看到响应)。
    """

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._client = client
        self._owns = client is None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = build_async_client(self.settings)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> HttpClient:
        _ = self.client
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------

    def _retry_for(self, method: str) -> RetrySettings:
        """★ 只有 **GET** 拿得到重试配额。"""
        if method.upper() == "GET":
            return self.settings.retry
        return RetrySettings(times=0, backoff_s=())

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        content: str | bytes | None = None,
        timeout: float | None = None,
        follow_redirects: bool = False,
        retry: RetrySettings | None = None,
    ) -> HttpAttempt:
        method = method.upper()
        policy = retry if retry is not None else self._retry_for(method)
        attempt = HttpAttempt(url=url, method=method)
        started = time.monotonic()
        backoff = list(policy.backoff_s)

        max_attempts = max(1, policy.times + 1)
        for i in range(max_attempts):
            attempt.attempts = i + 1
            try:
                resp = await self.client.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json_body if json_body is not None else None,
                    content=content.encode("utf-8") if isinstance(content, str) else content,
                    timeout=timeout or self.settings.api_timeout_s,
                    follow_redirects=follow_redirects,
                )
                attempt.response = resp
                attempt.status_code = resp.status_code
                attempt.headers = dict(resp.headers)
                attempt.text = resp.text
                attempt.error = None
                attempt.exception = None
                if resp.status_code in RETRYABLE_STATUS and i + 1 < max_attempts:
                    await self._sleep(backoff, i)
                    continue
                break
            except RETRYABLE_EXC as exc:
                attempt.error = f"{type(exc).__name__}: {exc}"
                attempt.exception = exc
                if i + 1 < max_attempts:
                    await self._sleep(backoff, i)
                    continue
                break
            except Exception as exc:  # noqa: BLE001 - 非网络类异常不重试
                attempt.error = f"{type(exc).__name__}: {exc}"
                attempt.exception = exc
                break

        attempt.elapsed_ms = (time.monotonic() - started) * 1000.0
        return attempt

    @staticmethod
    async def _sleep(backoff: list[float], index: int) -> None:
        import asyncio

        delay = backoff[index] if index < len(backoff) else (backoff[-1] if backoff else 0.0)
        if delay > 0:
            await asyncio.sleep(delay)
