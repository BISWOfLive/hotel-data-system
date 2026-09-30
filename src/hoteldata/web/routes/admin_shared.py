"""后台公共依赖(守卫 / 会话 / 上下文 / 审计再导出)。

拆出来是为了让 ``routes/admin_*.py`` 各页模块**不互相 import**
—— 它们只依赖本模块,路由注册由 :mod:`hoteldata.web.routes.admin` 统一做。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import Request
from fastapi.responses import RedirectResponse, Response
from loguru import logger

from hoteldata.web.auth import COOKIE_NAME, SessionSigner, record_audit

__all__ = [
    "COOKIE_NAME",
    "audit",
    "client_ip",
    "flashes",
    "guard",
    "int_or",
    "login_redirect",
    "page_ctx",
    "record_audit",
    "runtime_of",
    "session_of",
    "signer_of",
]

#: 本机地址白名单(``ADMIN_ALLOW_REMOTE=0`` 时只允许这些)
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def runtime_of(request: Request) -> Any:
    rt = getattr(request.app.state, "runtime", None)
    if rt is None:  # pragma: no cover - 装配错误
        raise RuntimeError("Runtime 未就绪")
    return rt


def signer_of(request: Request) -> SessionSigner:
    signer = getattr(request.app.state, "admin_signer", None)
    if signer is None:
        signer = SessionSigner(runtime_of(request).settings)
        request.app.state.admin_signer = signer
    return signer


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def session_of(request: Request) -> Any:
    """当前会话;未登录返回 ``None``。"""
    return signer_of(request).load(request.cookies.get(COOKIE_NAME))


def is_local(request: Request) -> bool:
    """★ 后台默认**只允许本机**。

    总纲 §7.8 与旧系统都写"仅 127.0.0.1"。但"监听 127.0.0.1"只挡住网络层,
    端口转发/Host 头仍可能摸到 —— 所以在**应用层**再挡一次:
    ``ADMIN_ALLOW_REMOTE=0``(默认)时非本机来源直接 404。
    """
    if runtime_of(request).settings.web.allow_remote:
        return True
    return client_ip(request) in LOCAL_HOSTS


async def guard(request: Request) -> Response | None:
    """访问守卫。返回**非 None** 即调用方应立即返回该响应。"""
    rt = runtime_of(request)
    if not rt.settings.web.admin_enabled:
        return Response("后台已关闭(ADMIN_ENABLED=0)", status_code=404)
    if not is_local(request):
        logger.warning("后台拒绝非本机访问: ip={} path={}", client_ip(request), request.url.path)
        return Response("后台只允许本机访问", status_code=404)
    return None


def login_redirect(request: Request) -> RedirectResponse:
    return RedirectResponse("/admin/login", status_code=303)


def flashes(request: Request) -> list[tuple[str, str]]:
    """从 query 读一次性提示(``?ok=...`` / ``?err=...``)。

    ★ 用 query 而不是 cookie flash:后台是**单机低频**且所有写操作都是
      **POST → 303 重定向**,query 足够,且没有"flash 没被消费掉就粘到下一个页面"
      这个经典毛病。
    """
    out: list[tuple[str, str]] = []
    for key in ("ok", "err", "warn", "info"):
        for value in request.query_params.getlist(key):
            if value:
                out.append((key, value))
    return out


def int_or(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def page_ctx(request: Request, page: str, **extra: Any) -> dict[str, Any]:
    """渲染模板的公共上下文。"""
    rt = runtime_of(request)
    return {
        "request": request,
        "page": page,
        "settings": rt.settings,
        "flashes": flashes(request),
        "session": session_of(request),
        **extra,
    }


async def audit(request: Request, **kwargs: Any) -> None:
    """写审计(带上请求 IP)。便捷包装,免得每个路由都传 ip。"""
    kwargs.setdefault("ip", client_ip(request))
    await record_audit(runtime_of(request), **kwargs)


def now_of(request: Request) -> datetime:
    return datetime.now(runtime_of(request).settings.tzinfo)
