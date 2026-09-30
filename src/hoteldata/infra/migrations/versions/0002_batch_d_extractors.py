"""批次 D 四张表 —— 预警三源 / 房态 / 点评(T4.1 / T4.2 / T4.3)。

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-30

涵盖(计划书 §5.10 末段「批次 D 追加 4 张」):

  ① ``alert_portal_columns``  预警采集列(旧 ``portal_columns``,13 列)
  ② ``alert_room_states``     房态(旧 ``room_states``,13 列,**整批替换**语义故无唯一约束)
  ③ ``review_reviews``        待回复点评(旧 ``reviews``,11 列,★ 不回溯 UPSERT)
  ④ ``review_materials``      点评素材缓存(旧 ``review_materials``,10 列)

**迁移纪律(逐字继承 0001 的教训)**:全部用**显式 DDL** —— 旧系统是
``CREATE TABLE IF NOT EXISTS`` + ``try: ALTER ... except: pass`` 的伪迁移,
``alert_logs.dedup_key`` 与比价两表因缺 UNIQUE 已被写脏。这里**禁止** ``IF NOT EXISTS``。

**方言与约定转换**(详见 :mod:`hoteldata.infra.models.extractors` 的模块 docstring):
  - 旧 ``TEXT`` 存 JSON → ``JSONB``(``portal_columns.detail`` → ``detail_json``);
  - 旧 ``TEXT`` 日期/时间 → ``Date`` / ``DateTime(timezone=True)``;
  - 主键 ``BigInteger``;``created_at`` 统一 ``timestamptz not null default now()``;
  - 外键指向 ``core_hotels.id``(CASCADE)/ ``core_accounts.id``(SET NULL)。
    ★ 旧 ``reviews`` / ``review_materials`` 的 ``hotel_id`` 无 REFERENCES(规格 §4.3 称"有意"),
    新库按项目外键纪律补上 —— 这是**有意差异**,记录在此。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # ① alert_portal_columns —— 预警采集列
    #    旧口径注释(storage/db.py:308-309):
    #    「采集列(渠道三指标/首页待办计数/热点日历事件)。页 = channel_ctrip /
    #      channel_qunar / hot_calendar / home_pending;value 统一 TEXT;
    #      detail 存补充 JSON(来源接口/排名/日期等)。」
    # ------------------------------------------------------------------
    op.create_table(
        "alert_portal_columns",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("hotel_id", sa.BigInteger(), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=True),
        sa.Column("collect_date", sa.Date(), nullable=False),
        sa.Column(
            "page",
            sa.String(length=128),
            nullable=False,
            comment="channel_ctrip / channel_qunar / hot_calendar / home_pending",
        ),
        sa.Column(
            "column_name",
            sa.String(length=256),
            nullable=False,
            comment="字段名(如 visitor_total / rating_avg / comment_pending / 中秋节)",
        ),
        sa.Column(
            "value",
            sa.Text(),
            nullable=True,
            comment="统一 str() 落库;缺字段 → 空串(代码路径不写 NULL)",
        ),
        sa.Column(
            "detail_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment='旧列 detail(TEXT),JSON:{"source_api": "...", "note": "...", ...}',
        ),
        sa.Column("raw_json_path", sa.Text(), nullable=True, comment="原始响应相对路径"),
        sa.Column("channel", sa.String(length=32), nullable=False, server_default="api"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="ok"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["hotel_id"],
            ["core_hotels.id"],
            name="fk_alert_portal_columns_hotel_id_core_hotels",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["core_accounts.id"],
            name="fk_alert_portal_columns_account_id_core_accounts",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_alert_portal_columns"),
        sa.UniqueConstraint(
            "hotel_id",
            "collect_date",
            "page",
            "column_name",
            name="uq_alert_portal_columns_key",
        ),
    )
    op.create_index(
        "ix_alert_portal_columns_lookup",
        "alert_portal_columns",
        ["hotel_id", "collect_date", "page"],
    )

    # ------------------------------------------------------------------
    # ② alert_room_states —— 房态
    #    旧口径注释(storage/db.py:330 逐字):
    #    「房态(房型×日期;available=1 iff roomStatus=='G'(开房);
    #      售完(canUsedQuantity=0)不算关房)」
    #    ★ 整批替换语义(同事务 DELETE + INSERT)→ **不建唯一约束**,只建天级索引。
    # ------------------------------------------------------------------
    op.create_table(
        "alert_room_states",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("hotel_id", sa.BigInteger(), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=True),
        sa.Column("collect_date", sa.Date(), nullable=False, comment="采集日"),
        sa.Column(
            "room_type_id",
            sa.String(length=64),
            nullable=False,
            comment="售卖房型 ID(字符串化:str(roomTypeID))",
        ),
        sa.Column("room_name", sa.Text(), nullable=True, comment="房型中文名(HTML 实体已解码)"),
        sa.Column("effect_date", sa.Date(), nullable=False, comment="生效日"),
        sa.Column(
            "available",
            sa.Integer(),
            nullable=False,
            comment=(
                "1=可订(开房) 0=不可订(关房);"
                "available=1 当且仅当 roomStatus=='G'(售完但 canUsedQuantity=0 仍算可订)"
            ),
        ),
        sa.Column(
            "status_code",
            sa.String(length=16),
            nullable=True,
            comment="原始 roomStatus('G'/'N'/...);组内首个非 G",
        ),
        sa.Column("quantity", sa.Integer(), nullable=True, comment="可售数量(canUsedQuantity);组内 max"),
        sa.Column(
            "price",
            sa.Float(),
            nullable=True,
            comment="roomPriceResult 均价(冗余参考);多 ratePlan 取 min",
        ),
        sa.Column("raw_json_path", sa.Text(), nullable=True, comment="原始响应相对路径"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["hotel_id"],
            ["core_hotels.id"],
            name="fk_alert_room_states_hotel_id_core_hotels",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["core_accounts.id"],
            name="fk_alert_room_states_account_id_core_accounts",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_alert_room_states"),
    )
    op.create_index(
        "ix_alert_room_states_day",
        "alert_room_states",
        ["hotel_id", "collect_date"],
    )

    # ------------------------------------------------------------------
    # ③ review_reviews —— 待回复点评
    #    旧口径注释(storage/db.py:389-390 逐字):
    #    「reviews:待回复点评(upsert;星级→sentiment good(≥4星)/bad(≤3星)/
    #      unknown(无星级,宁可漏不可错);replied=1 后不回溯)。」
    #    ★ replied 的 DEFAULT 0 是「不回溯」机制的一半(replied 不在 INSERT 列清单里)。
    # ------------------------------------------------------------------
    op.create_table(
        "review_reviews",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("hotel_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "review_id",
            sa.String(length=128),
            nullable=False,
            comment="平台点评 id(commentId);缺失时为内容指纹 'h'+sha1[:16]",
        ),
        sa.Column(
            "user_name",
            sa.String(length=256),
            nullable=True,
            comment="评价人(平台侧已脱敏,如 M253349****)",
        ),
        sa.Column(
            "star",
            sa.Integer(),
            nullable=True,
            comment="星级(1~5);来源 score.avgScoreSimple,int(float(...));空=unknown",
        ),
        sa.Column("content", sa.Text(), nullable=False, comment="点评原文"),
        sa.Column(
            "sentiment",
            sa.String(length=16),
            nullable=True,
            server_default="good",
            comment="good(≥4星)/ bad(≤3星)/ unknown(无星级)",
        ),
        sa.Column(
            "replied",
            sa.Integer(),
            nullable=True,
            server_default="0",
            comment="1=已回复(ok/ignored/silent) 0=待处理;★ 采集 UPSERT 永不写本列",
        ),
        sa.Column(
            "strategy",
            sa.String(length=64),
            nullable=True,
            comment="处理策略快照:模板id/silent/auto_failed;★ 采集 UPSERT 不覆盖",
        ),
        sa.Column(
            "comment_time",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="点评时间(旧 addtime /Date(ms+0800)/ 显式按 Asia/Shanghai 解析)",
        ),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["hotel_id"],
            ["core_hotels.id"],
            name="fk_review_reviews_hotel_id_core_hotels",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_review_reviews"),
        sa.UniqueConstraint("hotel_id", "review_id", name="uq_review_reviews_review_id"),
    )
    op.create_index(
        "ix_review_reviews_pending",
        "review_reviews",
        ["hotel_id", "replied", "sentiment"],
    )

    # ------------------------------------------------------------------
    # ④ review_materials —— 点评素材缓存
    #    旧口径注释(storage/db.py:426-427 逐字):
    #    「review_materials:点评分析每日素材缓存(getCommentsScoreV2 评分/
    #      getCompetitorCommentStat 对比建议/getCommentRateTrend 趋势;
    #      kind=score|competitor|trend;不复用 module_records)」
    #    ★ 该注释漏了第 4 类 num(getCommentNumV2),实库有 kind=num 4 行
    #      (规格 §6.4 漂移 D-5)→ 本迁移的列注释按事实写 4 类。
    # ------------------------------------------------------------------
    op.create_table(
        "review_materials",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("hotel_id", sa.BigInteger(), nullable=False),
        sa.Column("collect_date", sa.Date(), nullable=False, comment="素材采集日"),
        sa.Column(
            "kind",
            sa.String(length=32),
            nullable=False,
            comment="score / competitor / trend / num(★ 实为 4 类,旧注释漏 num)",
        ),
        sa.Column(
            "payload_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="结构化素材;None → JSON 字面量 null(NOT NULL)",
        ),
        sa.Column("raw_json_path", sa.Text(), nullable=True, comment="原始响应相对路径"),
        sa.Column("channel", sa.String(length=32), nullable=False, server_default="api"),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="ok",
            comment="ok / degraded / no_data / failed(旧注释漏 no_data)",
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["hotel_id"],
            ["core_hotels.id"],
            name="fk_review_materials_hotel_id_core_hotels",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_review_materials"),
        sa.UniqueConstraint("hotel_id", "collect_date", "kind", name="uq_review_materials_key"),
    )
    op.create_index(
        "ix_review_materials_lookup",
        "review_materials",
        ["hotel_id", "collect_date", "kind"],
    )


def downgrade() -> None:
    op.drop_index("ix_review_materials_lookup", table_name="review_materials")
    op.drop_table("review_materials")
    op.drop_index("ix_review_reviews_pending", table_name="review_reviews")
    op.drop_table("review_reviews")
    op.drop_index("ix_alert_room_states_day", table_name="alert_room_states")
    op.drop_table("alert_room_states")
    op.drop_index("ix_alert_portal_columns_lookup", table_name="alert_portal_columns")
    op.drop_table("alert_portal_columns")
