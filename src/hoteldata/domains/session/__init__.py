"""登录管家域(``session``)。

**只暴露** :class:`~hoteldata.domains.session.manager.LoginManager` 与
:mod:`hoteldata.domains.session.status` 的状态常量。

⚠️ 其他域(尤其 ``collect``)**不许 import 本包的实现细节** ——
要判断/刷新登录态,走 ``Runtime.ensure_login`` 注入的回调(硬约束 2)。
"""

from __future__ import annotations

from .status import (
    ACTION_RESULTS,
    ActionCode,
    SessionState,
    account_status_for,
    state_for_action,
    state_from_age,
)

__all__ = [
    "ACTION_RESULTS",
    "ActionCode",
    "SessionState",
    "account_status_for",
    "state_for_action",
    "state_from_age",
]
