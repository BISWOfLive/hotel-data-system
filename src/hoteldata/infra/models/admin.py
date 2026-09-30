"""后台操作审计表(迁移 ``0007_admin_audit``)。

★ 为什么需要它
==============

总纲 §7.8 给 Web 后台定的范围是:
**「账号/机器人/比价酒店增删 ＋ 统一口令登录 ＋ 操作审计」**
—— 注意最后那三个字:**操作审计**。

后台是**唯一能改业务实体**(账号、酒店、机器人、群绑定、比价目标)的入口
—— 群命令能做的很有限(绑定/解绑/策略),CSV 导入是一次性的。
所以"谁在什么时候把哪家店的比价目标停用了"必须有地方可查,
否则出了事只能翻 loguru 的日志文件(而那会被 30 天轮转清掉)。

★ 三条设计取舍
==============

1. **与 ``alert_logs`` / ``push_logs`` 同一取向:append-only**
   —— 只 INSERT,不 UPDATE 不 DELETE。"操作历史"如果能被改写就不叫审计。

2. **``detail`` 用 ``JSONB`` 而不是 TEXT**
   —— 旧系统 12 列 JSON 存成 TEXT、过滤靠 ``LIKE``,想查"谁改过 city"
   只能靠字符串猜。JSONB 可以直接 ``detail->>'city'`` 查。

3. **``actor`` 是文本不是外键**
   —— 当前是**统一口令**(没有用户表),actor 记的是 ``"admin"`` 或会话标识;
   将来真加了用户体系,这一列不用迁移就能放用户名。
   ★ 不建用户表是**刻意的**:总纲 §7.8 明确"不做角色区分、权限树"。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, created_at_column

#: 审计动作取值(与路由一一对应,便于按动作筛选)
AUDIT_ACTIONS: tuple[str, ...] = (
    "login",
    "login_failed",
    "logout",
    "hotel.create",
    "hotel.update",
    "hotel.delete",
    "account.create",
    "account.update",
    "account.delete",
    "bot.create",
    "bot.update",
    "bot.delete",
    "binding.create",
    "binding.delete",
    "binding.pause",
    "target.create",
    "target.update",
    "target.delete",
    "target.import",
    "task.run",
    "push.test",
)


class AdminAudit(Base):
    """后台操作审计(**append-only**)。"""

    __tablename__ = "ops_admin_audit"
    __table_args__ = (
        Index("ix_admin_audit_time", text("created_at DESC")),
        Index("ix_admin_audit_action", "action", text("created_at DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    #: 谁(统一口令下恒为 ``admin``;将来有用户体系时放用户名 —— 不是外键,不用迁移)
    actor: Mapped[str] = mapped_column(String(64), nullable=False, server_default=text("'admin'"))
    #: 动作(见 :data:`AUDIT_ACTIONS`)
    action: Mapped[str] = mapped_column(String(48), nullable=False)
    #: 对象类型:hotel / account / bot / binding / target / task
    target_type: Mapped[str | None] = mapped_column(String(32))
    #: 对象标识(酒店名 / 别名 / chatid …)
    target_id: Mapped[str | None] = mapped_column(String(256))
    #: ★ 变更明细(JSONB,可直接按字段查:``detail->>'city'``)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    #: 结果:ok / failed(失败也要留痕 —— 失败的删除尝试同样是需要知道的事)
    result: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'ok'"))
    error: Mapped[str | None] = mapped_column(Text)
    #: 来源 IP(后台只监听 127.0.0.1,但仍记录 —— 万一本机多用户)
    ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = created_at_column()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<AdminAudit {self.action} {self.target_type}={self.target_id} {self.result}>"


__all__ = ["AUDIT_ACTIONS", "AdminAudit"]
