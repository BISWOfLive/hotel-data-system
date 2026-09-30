"""段1 首迁移 —— 7 张表 + 索引 + ★ 部分唯一索引 ``job_runs_sched_uniq``。

Revision ID: 0001
Revises:
Create Date: 2026-09-28

涵盖(段1 §5.10):

  ① ``core_accounts``       账号库(``storage_state_path`` **不在此**,登录态走 ``sessions``)
  ② ``core_hotels``         酒店
  ③ ``sessions``            ★ 登录态唯一索引 (platform, role, alias)
  ④ ``collect_reports``     页面级提取 + 截图回填目标
  ⑤ ``collect_modules``     ★ 模块级结构化数据(payload_json 用 **JSONB**)
  ⑥ ``ops_login_events``    登录事件
  ⑦ ``job_runs``            ★ 任务可观测地基

**约束全部显式声明**。旧系统的教训:``alert_logs.dedup_key`` 与比价两表因缺 UNIQUE
已被写脏(实测 ``hotel_price_comparisons`` 6 组重复、``hotel_batch_runs`` 同日两条 done)。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # ① core_accounts
    # ------------------------------------------------------------------
    op.create_table(
        "core_accounts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("alias", sa.String(length=128), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False, comment="ctrip | meituan"),
        sa.Column("username_enc", sa.Text(), nullable=False),
        sa.Column("password_enc", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="active",
            comment="active | pending_login | blocked(长期账号状态)",
        ),
        sa.Column("is_multi", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("remark", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id", name="pk_core_accounts"),
        sa.UniqueConstraint("alias", name="uq_core_accounts_alias"),
    )

    # ------------------------------------------------------------------
    # ② core_hotels
    # ------------------------------------------------------------------
    op.create_table(
        "core_hotels",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("city", sa.String(length=128), nullable=True),
        sa.Column("account_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "ebk_hotel_id",
            sa.String(length=64),
            nullable=True,
            comment="携程 eBooking 酒店 ID(比价锚点直达用)",
        ),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["core_accounts.id"],
            name="fk_core_hotels_account_id_core_accounts",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_core_hotels"),
        sa.UniqueConstraint("name", name="uq_core_hotels_name"),
    )

    # ------------------------------------------------------------------
    # ③ sessions —— ★ 登录态唯一索引
    #    不存 state_path:路径由 (platform, role, alias) 推导
    # ------------------------------------------------------------------
    op.create_table(
        "sessions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column(
            "role", sa.String(length=32), nullable=False, comment="ebooking | merchant | ota | ota_meituan"
        ),
        sa.Column("alias", sa.String(length=128), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="unknown",
            comment="valid | stale | invalid | unknown(长期会话态)",
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id", name="pk_sessions"),
        sa.UniqueConstraint("platform", "role", "alias", name="uq_sessions_key"),
    )

    # ------------------------------------------------------------------
    # ④ collect_reports —— 截图回填目标
    # ------------------------------------------------------------------
    op.create_table(
        "collect_reports",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("hotel_id", sa.BigInteger(), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=True),
        sa.Column("collect_date", sa.Date(), nullable=False),
        sa.Column("page", sa.String(length=128), nullable=False),
        sa.Column(
            "channel", sa.String(length=32), nullable=False, comment="api | browser | screenshot | fullpage"
        ),
        sa.Column("indicators_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("modules_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "module_screenshots_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="★ 键名必须与旧系统一致,否则段2 取不到图",
        ),
        sa.Column("screenshot_path", sa.Text(), nullable=True),
        sa.Column("raw_json_path", sa.Text(), nullable=True, comment="相对路径"),
        sa.Column("html_path", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(
            ["hotel_id"],
            ["core_hotels.id"],
            name="fk_collect_reports_hotel_id_core_hotels",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["core_accounts.id"],
            name="fk_collect_reports_account_id_core_accounts",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_collect_reports"),
        sa.UniqueConstraint("hotel_id", "collect_date", "page", name="uq_collect_reports_key"),
    )

    # ------------------------------------------------------------------
    # ⑤ collect_modules —— ★ 段1 核心产出
    # ------------------------------------------------------------------
    op.create_table(
        "collect_modules",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("hotel_id", sa.BigInteger(), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=True),
        sa.Column("collect_date", sa.Date(), nullable=False),
        sa.Column("page", sa.String(length=128), nullable=False),
        sa.Column("module", sa.String(length=256), nullable=False),
        sa.Column("window", sa.String(length=64), nullable=False),
        sa.Column(
            "payload_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="★ 旧系统是 TEXT,这里改 JSONB",
        ),
        sa.Column("raw_json_path", sa.Text(), nullable=True, comment="相对路径"),
        sa.Column("channel", sa.String(length=32), nullable=False, server_default="api"),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="ok",
            comment="ok | degraded | no_data | failed(no_data 不算失败)",
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(
            ["hotel_id"],
            ["core_hotels.id"],
            name="fk_collect_modules_hotel_id_core_hotels",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["core_accounts.id"],
            name="fk_collect_modules_account_id_core_accounts",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_collect_modules"),
        sa.UniqueConstraint(
            "hotel_id",
            "collect_date",
            "page",
            "module",
            "window",
            name="uq_collect_modules_key",
        ),
    )
    op.create_index(
        "ix_collect_modules_lookup",
        "collect_modules",
        ["hotel_id", "collect_date", "page", "module"],
    )

    # ------------------------------------------------------------------
    # ⑥ ops_login_events
    # ------------------------------------------------------------------
    op.create_table(
        "ops_login_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "action", sa.String(length=32), nullable=False, comment="patrol | relogin | renew | expire | fail"
        ),
        sa.Column("result", sa.String(length=16), nullable=False, comment="ok | fail"),
        sa.Column(
            "detail",
            sa.Text(),
            nullable=True,
            comment="登录管家动作结果码原文:ok/captcha/manual_required/failed/timeout/blocked",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["core_accounts.id"],
            name="fk_ops_login_events_account_id_core_accounts",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ops_login_events"),
    )

    # ------------------------------------------------------------------
    # ⑦ job_runs —— ★ 可观测地基
    # ------------------------------------------------------------------
    op.create_table(
        "job_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("task", sa.String(length=128), nullable=False),
        sa.Column(
            "scheduled_at", sa.DateTime(timezone=True), nullable=True, comment="计划触发;手动运行 = NULL"
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            comment="pending | running | ok | failed | skipped",
        ),
        sa.Column("trigger", sa.String(length=16), nullable=False, comment="schedule | manual | catchup"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("args", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("host", sa.String(length=128), nullable=True),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_job_runs"),
    )
    # ★ 部分唯一索引:只约束"计划触发",手动重跑(scheduled_at = NULL)可任意多次
    op.create_index(
        "uq_job_runs_sched_uniq",
        "job_runs",
        ["task", "scheduled_at"],
        unique=True,
        postgresql_where=sa.text("scheduled_at IS NOT NULL"),
    )
    op.create_index(
        "ix_job_runs_task_time",
        "job_runs",
        ["task", sa.text("started_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_job_runs_task_time", table_name="job_runs")
    op.drop_index("uq_job_runs_sched_uniq", table_name="job_runs")
    op.drop_table("job_runs")
    op.drop_table("ops_login_events")
    op.drop_index("ix_collect_modules_lookup", table_name="collect_modules")
    op.drop_table("collect_modules")
    op.drop_table("collect_reports")
    op.drop_table("sessions")
    op.drop_table("core_hotels")
    op.drop_table("core_accounts")
