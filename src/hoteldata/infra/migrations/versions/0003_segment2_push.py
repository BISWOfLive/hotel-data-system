"""段2 六张表 + 一列 —— 推送共享层 / 预警状态机 / 点评审计(段2 §5.1 / 附录 E)。

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-01

涵盖:

  ① ``core_bots``              企微智能机器人(多实例,凭据密文)
  ② ``core_group_bindings``    群 ↔ 酒店绑定(UNIQUE 幂等 + paused)
  ③ ``push_logs``              推送审计 ★ ``bot_id`` 建 **Text**(D17 修复)
  ④ ``alert_states``           预警状态机 ★ streak 仅展示
  ⑤ ``alert_logs``             预警日志 ★ ``dedup_key`` **补 UNIQUE**(旧库漏了)
  ⑥ ``review_replies``         点评回复审计(append-only)
  ＋ ``core_hotels.review_policy``  店级点评策略(群命令「点评策略」的落库处)

**迁移纪律(逐字继承 0001/0002)**:全部**显式 DDL**,禁止 ``IF NOT EXISTS``
与 ``try: ALTER ... except: pass``。旧系统 ``alert_logs.dedup_key`` 与比价两表
因缺 UNIQUE 已被写脏(总纲 §4.3:比价库 6 组重复行)。

**三处有意差异(修旧缺陷,不是风格偏好)**

  ==========================  ==========================  ================================
  项                          旧                          新
  ==========================  ==========================  ================================
  ``push_logs.bot_id``        ``INTEGER``(实际写机器人名)  ``TEXT``(D17)
  ``alert_logs.dedup_key``    无 UNIQUE(已被写脏)          ``UNIQUE``
  ``push_logs.slot``          无(靠 substr(pushed_at) 切片) 显式列 + 索引
  ==========================  ==========================  ================================
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # ① core_bots —— 企微智能机器人(多实例)
    #    旧表 bots:name UNIQUE + bot_id_enc / secret_enc(Fernet 密文)
    # ------------------------------------------------------------------
    op.create_table(
        "core_bots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("bot_id_enc", sa.Text(), nullable=False, comment="Fernet 密文(明文永不落库)"),
        sa.Column("secret_enc", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column(
            "capacity_per_bot",
            sa.Integer(),
            nullable=False,
            server_default="10",
            comment="建议承载群数(甲方口径 30 机器人 / 300 群);超出只告警不硬拦",
        ),
        sa.Column("remark", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_core_bots")),
        sa.UniqueConstraint("name", name=op.f("uq_core_bots_name")),
    )
    op.create_index("ix_core_bots_status", "core_bots", ["status"])

    # ------------------------------------------------------------------
    # ② core_group_bindings —— 群 ↔ 酒店(一群多店)
    #    UNIQUE(chatid, hotel_id) = 「绑定」命令幂等的 DB 兜底
    # ------------------------------------------------------------------
    op.create_table(
        "core_group_bindings",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("chatid", sa.String(length=128), nullable=False, comment="企微群 chatid"),
        sa.Column("hotel_id", sa.BigInteger(), nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_core_group_bindings")),
        sa.ForeignKeyConstraint(
            ["hotel_id"],
            ["core_hotels.id"],
            name=op.f("fk_core_group_bindings_hotel_id_core_hotels"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("chatid", "hotel_id", name="uq_core_group_bindings_key"),
    )
    op.create_index("ix_core_group_bindings_chatid", "core_group_bindings", ["chatid"])
    op.create_index("ix_core_group_bindings_hotel", "core_group_bindings", ["hotel_id"])

    # ------------------------------------------------------------------
    # ③ push_logs —— 推送审计
    #    ★ bot_id = TEXT(D17):旧库 INTEGER 而代码写机器人名字符串
    #    ★ slot = YYYY-MM-DD-HH 显式列(旧库靠 substr 两次切片)
    #    hotel_id 用 SET NULL:审计行永不随酒店删除而消失
    # ------------------------------------------------------------------
    op.create_table(
        "push_logs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("hotel_id", sa.BigInteger(), nullable=True),
        sa.Column("group_chatid", sa.String(length=128), nullable=False),
        sa.Column(
            "bot_id",
            sa.Text(),
            nullable=False,
            comment="机器人名(★ 文本:D17 修复,旧库是 INTEGER 却写字符串)",
        ),
        sa.Column(
            "push_type",
            sa.String(length=64),
            nullable=False,
            comment="daily_report / module_{id} / review_analysis / price_compare / ops_alert …",
        ),
        sa.Column("content_preview", sa.Text(), nullable=True, comment="内容前 200 字(人工核对用)"),
        sa.Column("media_count", sa.Integer(), nullable=False, server_default="0", comment="实际发出图片数"),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            comment="ok / failed / skipped(去重命中;skipped 也要留痕,不许静默)",
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("images_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("slot", sa.String(length=16), nullable=False, comment="去重时段键 YYYY-MM-DD-HH"),
        sa.Column(
            "pushed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_push_logs")),
        sa.ForeignKeyConstraint(
            ["hotel_id"],
            ["core_hotels.id"],
            name=op.f("fk_push_logs_hotel_id_core_hotels"),
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_push_logs_slot", "push_logs", ["group_chatid", "hotel_id", "push_type", "slot"]
    )
    op.create_index("ix_push_logs_day", "push_logs", ["slot"])
    op.create_index("ix_push_logs_type", "push_logs", ["push_type", "status"])

    # ------------------------------------------------------------------
    # ④ alert_states —— 预警状态机
    #    ★ streak **只是展示用计数**,不是推送门槛
    #      ("连续 7 天"由引擎从 alert_room_states 逐日推导)
    # ------------------------------------------------------------------
    op.create_table(
        "alert_states",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("rule_id", sa.String(length=64), nullable=False),
        sa.Column("hotel_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "entity_key",
            sa.String(length=256),
            nullable=False,
            comment="房型 id / 事件名+日期 / __hotel__(整店忽略)",
        ),
        sa.Column(
            "streak",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="★ 仅展示用计数,不是推送门槛",
        ),
        sa.Column("last_trigger_date", sa.Date(), nullable=True, comment="当日去重依据"),
        sa.Column("last_ok_date", sa.Date(), nullable=True, comment="最近一次恢复日"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="ok"),
        sa.Column("ignored_until", sa.Date(), nullable=True, comment="忽略至(含当天)"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alert_states")),
        sa.ForeignKeyConstraint(
            ["hotel_id"],
            ["core_hotels.id"],
            name=op.f("fk_alert_states_hotel_id_core_hotels"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("rule_id", "hotel_id", "entity_key", name="uq_alert_states_key"),
    )
    op.create_index("ix_alert_states_hotel", "alert_states", ["hotel_id", "rule_id"])
    op.create_index("ix_alert_states_ignored", "alert_states", ["hotel_id", "ignored_until"])

    # ------------------------------------------------------------------
    # ⑤ alert_logs —— 预警日志(**送达率口径的唯一来源**)
    #    ★ dedup_key 补 UNIQUE:旧库没有,已被写脏(类比比价库 6 组重复行)
    # ------------------------------------------------------------------
    op.create_table(
        "alert_logs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("rule_id", sa.String(length=64), nullable=False),
        sa.Column("hotel_id", sa.BigInteger(), nullable=False),
        sa.Column("entity_key", sa.String(length=256), nullable=False),
        sa.Column(
            "dedup_key",
            sa.String(length=512),
            nullable=False,
            comment="rule_id:hotel_id:entity_key:YYYY-MM-DD(★ UNIQUE;旧库漏了这条约束)",
        ),
        sa.Column("log_date", sa.Date(), nullable=False, comment="日志日(统计与去重的键)"),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column(
            "recipient",
            sa.String(length=256),
            nullable=False,
            comment="管理群 chatid / 运营群 chatid / manage-none(无管理群时写,仍计入送达率分母)",
        ),
        sa.Column("pushed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("images_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "payload_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="触发时的数据快照(复盘误报/漏报用)",
        ),
        sa.Column("pushed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alert_logs")),
        sa.ForeignKeyConstraint(
            ["hotel_id"],
            ["core_hotels.id"],
            name=op.f("fk_alert_logs_hotel_id_core_hotels"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("dedup_key", name="uq_alert_logs_dedup_key"),
    )
    op.create_index("ix_alert_logs_day", "alert_logs", ["log_date"])
    op.create_index("ix_alert_logs_rule", "alert_logs", ["rule_id", "log_date"])
    op.create_index("ix_alert_logs_hotel", "alert_logs", ["hotel_id", "log_date"])

    # ------------------------------------------------------------------
    # ⑥ review_replies —— 点评回复审计(**append-only,不 UPDATE**)
    # ------------------------------------------------------------------
    op.create_table(
        "review_replies",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("hotel_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "review_id",
            sa.String(length=128),
            nullable=False,
            comment="平台 commentId(或内容指纹 h+sha1[:16])",
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            comment="suggested / ok / failed / ignored",
        ),
        sa.Column(
            "strategy",
            sa.String(length=64),
            nullable=True,
            comment="模板 id(g01/b01/b03)/ silent / auto_failed",
        ),
        sa.Column("content", sa.Text(), nullable=True, comment="实际回复内容(草稿或已发出)"),
        sa.Column(
            "exec_by",
            sa.String(length=32),
            nullable=False,
            server_default="draft",
            comment="draft / auto / auto_failed / manage / silent",
        ),
        sa.Column(
            "detail_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="补充证据:失败原因 / 平台回执 / 情感与星级快照",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_review_replies")),
        sa.ForeignKeyConstraint(
            ["hotel_id"],
            ["core_hotels.id"],
            name=op.f("fk_review_replies_hotel_id_core_hotels"),
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_review_replies_review", "review_replies", ["hotel_id", "review_id", "id"])
    op.create_index("ix_review_replies_status", "review_replies", ["hotel_id", "status"])

    # ------------------------------------------------------------------
    # ⑦ core_hotels.review_policy —— 店级点评策略
    #    旧库是 TEXT 存 JSON;新库按项目约定转 JSONB。
    #    策略优先级:本列(群命令写) > review_templates.json overrides > 默认
    # ------------------------------------------------------------------
    op.add_column(
        "core_hotels",
        sa.Column(
            "review_policy",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment='{"good": "g01", "bad": "silent"|"template", "bad_template": "b01"}',
        ),
    )


def downgrade() -> None:
    op.drop_column("core_hotels", "review_policy")

    op.drop_index("ix_review_replies_status", table_name="review_replies")
    op.drop_index("ix_review_replies_review", table_name="review_replies")
    op.drop_table("review_replies")

    op.drop_index("ix_alert_logs_hotel", table_name="alert_logs")
    op.drop_index("ix_alert_logs_rule", table_name="alert_logs")
    op.drop_index("ix_alert_logs_day", table_name="alert_logs")
    op.drop_table("alert_logs")

    op.drop_index("ix_alert_states_ignored", table_name="alert_states")
    op.drop_index("ix_alert_states_hotel", table_name="alert_states")
    op.drop_table("alert_states")

    op.drop_index("ix_push_logs_type", table_name="push_logs")
    op.drop_index("ix_push_logs_day", table_name="push_logs")
    op.drop_index("ix_push_logs_slot", table_name="push_logs")
    op.drop_table("push_logs")

    op.drop_index("ix_core_group_bindings_hotel", table_name="core_group_bindings")
    op.drop_index("ix_core_group_bindings_chatid", table_name="core_group_bindings")
    op.drop_table("core_group_bindings")

    op.drop_index("ix_core_bots_status", table_name="core_bots")
    op.drop_table("core_bots")
