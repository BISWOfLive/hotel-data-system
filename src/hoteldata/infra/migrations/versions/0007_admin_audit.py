"""后台操作审计表 —— ``ops_admin_audit``。

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-01

## 为什么

总纲 §7.8 给 Web 后台定的范围里**明确含「操作审计」**。后台是唯一能改业务实体
(账号 / 酒店 / 机器人 / 群绑定 / 比价目标)的入口,而群命令能做的很有限,
所以"谁把哪家店的比价目标停用了"必须可查 —— 否则只能翻 loguru 日志,
而日志 30 天就被轮转清掉了。

## 三条设计取舍

1. **append-only**:只 INSERT。能被改写的"操作历史"不叫审计
   (与 ``alert_logs`` 状态流转新增行、``review_replies`` append-only 同一取向)。
2. **``detail`` 用 JSONB**:旧系统 12 列 JSON 存成 TEXT、过滤靠 LIKE;
   JSONB 可以直接 ``detail->>'city'`` 查。
3. **``actor`` 是文本不是外键**:当前是**统一口令**(无用户表,总纲明确"不做角色区分"),
   记 ``"admin"``;将来加用户体系时这一列不用迁移。

## 回滚

直接删表(纯新增,无历史包袱)。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ops_admin_audit",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "actor",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("'admin'"),
            comment="谁(统一口令下恒为 admin;将来有用户体系时放用户名 —— 不是外键,不用迁移)",
        ),
        sa.Column("action", sa.String(length=48), nullable=False, comment="login / hotel.create / target.delete …"),
        sa.Column("target_type", sa.String(length=32), nullable=True, comment="hotel/account/bot/binding/target/task"),
        sa.Column("target_id", sa.String(length=256), nullable=True, comment="对象标识(酒店名/别名/chatid)"),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="★ 变更明细(JSONB,可按字段查)",
        ),
        sa.Column(
            "result",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'ok'"),
            comment="ok / failed(失败的尝试也要留痕)",
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ops_admin_audit"),
    )
    op.create_index("ix_admin_audit_time", "ops_admin_audit", [sa.text("created_at DESC")])
    op.create_index(
        "ix_admin_audit_action", "ops_admin_audit", ["action", sa.text("created_at DESC")]
    )


def downgrade() -> None:
    op.drop_index("ix_admin_audit_action", table_name="ops_admin_audit")
    op.drop_index("ix_admin_audit_time", table_name="ops_admin_audit")
    op.drop_table("ops_admin_audit")
