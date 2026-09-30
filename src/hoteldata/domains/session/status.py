"""登录状态与动作结果码(**两个必须拆开的概念**)。

⚠️ 计划书里的「6 态状态机」说法有误,重写时必须拆成两层
--------------------------------------------------------
段1 计划书 §T2.4 写「**6 态状态机**:``unknown / valid / stale / invalid /
logging_in / blocked``」。**逐字核对旧系统后,六个状态名只有 ``blocked`` 重合** ——
旧 ``crawl/login_manager.py:36-44`` 实际是:

    ok / captcha / manual_required / failed / timeout / blocked

而且这六个是**单次登录动作的结果码**(一次 ``auto_login()`` 的返回值),
**不是长期会话状态**;长期状态落在 ``accounts.status``,只有**三态**
``active / pending_login / blocked``。

新系统把两个概念显式拆开
------------------------
==============  =============================================  ==========================
概念             取值                                            落在哪
==============  =============================================  ==========================
**长期会话态**    ``unknown / valid / stale / invalid``           ``sessions.status``
**动作结果码**    ``ok / captcha / manual_required /             ``ops_login_events.detail``
                 failed / timeout / blocked``
**账号长期态**    ``active / pending_login / blocked``           ``core_accounts.status``
==============  =============================================  ==========================

混成一个字段的后果:一次 ``ok`` 会覆盖"这个登录态还能用多久"的判断,
而一次 ``timeout`` 会被误读成"会话失效"从而触发不必要的重登。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

__all__ = [
    "ACTION_RESULTS",
    "AUTH_ERROR_MARKERS",
    "BUSINESS_TEXT_KEYWORDS",
    "LOGIN_FORM_WAIT_S",
    "LOGIN_URL_MARKERS",
    "REQUIRED_API_HEADERS",
    "ActionCode",
    "SessionState",
    "account_status_for",
    "looks_like_auth_error",
    "state_from_age",
    "state_for_action",
]


class SessionState(StrEnum):
    """★ 长期会话态 —— 落 ``sessions.status``。"""

    UNKNOWN = "unknown"
    VALID = "valid"
    STALE = "stale"
    INVALID = "invalid"


class ActionCode(StrEnum):
    """★ 单次登录动作的结果码 —— 落 ``ops_login_events.detail``。

    逐字继承旧 ``crawl/login_manager.py:36-44``。
    """

    OK = "ok"
    CAPTCHA = "captcha"
    MANUAL_REQUIRED = "manual_required"
    FAILED = "failed"
    TIMEOUT = "timeout"
    BLOCKED = "blocked"


ACTION_RESULTS: tuple[str, ...] = tuple(c.value for c in ActionCode)

#: 动作结果码 → 长期会话态的建议映射(用于更新 ``sessions.status``)
_ACTION_TO_STATE: dict[str, SessionState] = {
    ActionCode.OK: SessionState.VALID,
    ActionCode.CAPTCHA: SessionState.INVALID,
    ActionCode.MANUAL_REQUIRED: SessionState.INVALID,
    ActionCode.FAILED: SessionState.INVALID,
    ActionCode.TIMEOUT: SessionState.UNKNOWN,  # 超时**不等于失效**
    ActionCode.BLOCKED: SessionState.INVALID,
}

#: URL 中出现这些标记视为登录页(逐字继承旧 ``ebooking.py:31-32``)
LOGIN_URL_MARKERS: tuple[str, ...] = ("login", "passport", "signin", "sign_in", "auth", "sso")

#: ★ 响应体里出现这些**鉴权失败标记** = 会话已死(2026-09-30 对真平台实测补入)。
#:
#: 为什么必须单独一组:平台在**会话失效时返回 HTTP 200**,正文是
#: ``{"error": "invalid_grant", "error_description": "authorized fail!"}``。
#: 只看状态码会把"已登出"判成"有效";而旧的中文标记(未登录/请重新登录/登录失效)
#: 一个都不匹配。两头的口径都不一样,所以既有 header 要求,也有正文判定。
AUTH_ERROR_MARKERS: tuple[str, ...] = (
    "invalid_grant",
    "authorized fail",
    "unauthorized",
    "not login",
    "not_login",
    "login required",
    "please login",
    "session expired",
)

#: 探活/采集请求必须带的头 —— 平台网关**凭这个头**区分 API 调用与页面导航。
#:
#: ★ 2026-09-30 实测:同一份有效 cookie,不带它 → ``302 /login``;带上 → ``200``。
#: 换句话说:**缺这个头时,任何有效会话都会被判成失效**。
REQUIRED_API_HEADERS: dict[str, str] = {
    "x-requested-with": "XMLHttpRequest",
}

#: 登录表单渲染等待(秒)。eBooking 登录页是 React 异步渲染,``goto(domcontentloaded)``
#: 后约 0.4s 密码框才出现(旧系统 2026-08-24 实测);立即扫描会误判「未找到密码输入框」。
LOGIN_FORM_WAIT_S = 10.0

#: 页面出现这些**强数据类关键词**才视为已进入业务系统(登录成功)
BUSINESS_TEXT_KEYWORDS: tuple[str, ...] = (
    "经营概况",
    "数据中心",
    "数据概览",
    "报表中心",
    "订单管理",
    "房态管理",
    "今日数据",
    "营业数据",
    "经营数据",
    "数据看板",
)


def state_for_action(action: str) -> SessionState:
    """动作结果码 → 长期会话态建议值。"""
    return _ACTION_TO_STATE.get(str(action), SessionState.UNKNOWN)


def looks_like_auth_error(body: str | None) -> str | None:
    """响应体是否含**鉴权失败标记**;命中则返回命中的标记,否则 ``None``。

    用于两处:

      * 轻量探活(平台在会话失效时**返回 200**);
      * API 直连通道的登录失效检测。

    只做**小写子串**匹配 —— 平台正文是英文,大小写不保证,不能假设。
    """
    if not body:
        return None
    low = body.lower()
    for marker in AUTH_ERROR_MARKERS:
        if marker in low:
            return marker
    return None


def state_from_age(
    age_days: float | None,
    *,
    max_age_days: int = 20,
    probe_valid: bool | None = None,
) -> SessionState:
    """按"登录态年龄 + 探活结果"推长期状态。

    * ``probe_valid is False`` → ``invalid``(探活说不行就是不行)
    * ``probe_valid is True`` 且 ``age > max_age_days`` → ``stale``(能用但该续登了)
    * ``probe_valid is True`` → ``valid``
    * ``probe_valid is None`` 且 ``age > max_age_days`` → ``stale``
    * 其余 → ``unknown``
    """
    if probe_valid is False:
        return SessionState.INVALID
    if age_days is None:
        return SessionState.UNKNOWN if probe_valid is None else SessionState.VALID
    if age_days > max_age_days:
        return SessionState.STALE
    return SessionState.VALID if probe_valid is not None else SessionState.UNKNOWN


def account_status_for(state: SessionState, current: str = "active") -> str:
    """长期会话态 → ``core_accounts.status`` 三态建议值。"""
    if current == "blocked":
        return "blocked"
    if state is SessionState.INVALID:
        return "pending_login"
    if state in (SessionState.VALID, SessionState.STALE):
        return "active"
    return current


def describe(state: SessionState, when: datetime | None = None) -> str:
    """人类可读描述(CLI 输出用)。"""
    labels = {
        SessionState.VALID: "有效",
        SessionState.STALE: "有效但需续登",
        SessionState.INVALID: "已失效",
        SessionState.UNKNOWN: "未探测",
    }
    suffix = f"({when:%Y-%m-%d %H:%M})" if when else ""
    return f"{labels.get(state, state)}{suffix}"
