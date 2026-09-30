"""段3 比价域建表 —— ``cmp_price_targets`` / ``cmp_price_comparisons`` / ``cmp_batch_runs``。

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-01

## 本迁移做什么

1. ``cmp_price_targets`` —— 比价目标(**取代旧系统两个 txt**:``compare_hotels.txt`` +
   ``price_targets.txt``;段3 §5.4 对总纲 §7.3 的修正);
2. ``cmp_price_comparisons`` —— 比价历史(**D8 的正面修复**:补 UNIQUE、补
   ``distance_km`` 真填、补 ``is_demo`` 显式列、补 ``price_scope``、补 ``price_rejected``);
3. ``cmp_batch_runs`` —— 批量运行记录(**每日一行 + UPSERT**,消除"同日两条 done")。

## ★ 为什么"先去重再加约束"写死在本迁移里(T3A.4 / 风险 T6)

旧库 ``hotel_price_comparisons`` 实测 **18 行 / 8 个唯一组合** ——
在 ``(anchor_name, query_date, nights)`` 上有 **6 组重复、冗余 10 行**
(详见 ``docs/参考/段3-分析/_证据-旧系统比价库dump.txt``)。

根因是 ``pusher.py:568`` 的 ``run_price_collect`` **硬编码 ``force=True``**:
判重 SQL 本身是对的(模拟 ``exists_today`` 3/3 命中),但每次都被绕过;
而两张表**都没有 UNIQUE 兜底**(``storage/prices.py:88-106`` 是 SELECT-then-INSERT)。

段3 是**新建表**,所以本迁移不会撞上历史脏数据。但把去重前置写在这里,是因为
**"先去重再加约束"这个顺序一旦搞反,迁移会直接失败** —— 而且失败现场
(约束创建报 duplicate key)看起来像是"代码写错了",不像"数据脏了"。
留一段显式的、幂等的清理语句,让将来任何一次带数据的重放都不会踩这个坑。

## 回滚

``downgrade`` 直接删三张表(段3 新建,无历史包袱)。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ==================================================================
    # ⓪ ★ 去重前置(顺序不能反)
    #    本机是新表,这段是空操作;但若目标库存在同名的历史脏表,
    #    它会先把重复组压成一行,**再加 UNIQUE 才不会失败**。
    #    策略(与 scripts/dedupe_price_history.py 一致):
    #    同组保留 price 非空优先、其次 created_at 最新的一条。
    # ==================================================================
    op.execute(
        """
        DO $$
        BEGIN
            IF to_regclass('public.cmp_price_comparisons') IS NOT NULL THEN
                WITH ranked AS (
                    SELECT id,
                           row_number() OVER (
                               PARTITION BY anchor_name, platform, hotel_name,
                                            room_type, nights, query_slot
                               ORDER BY (price IS NULL), created_at DESC NULLS LAST, id DESC
                           ) AS rn
                      FROM cmp_price_comparisons
                )
                DELETE FROM cmp_price_comparisons c
                 USING ranked r
                 WHERE c.id = r.id AND r.rn > 1;
            END IF;
        END $$;
        """
    )

    # ==================================================================
    # ① cmp_price_targets —— 比价目标(取代两个 txt)
    # ==================================================================
    op.create_table(
        "cmp_price_targets",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "hotel_id",
            sa.BigInteger(),
            nullable=True,
            comment="关联 core_hotels(可空:清单里可能有没有登记的酒店)",
        ),
        sa.Column("anchor_name", sa.String(length=256), nullable=False),
        sa.Column("city", sa.String(length=128), nullable=True),
        sa.Column(
            "platforms",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{ctrip,meituan}'"),
            comment="参与哪些平台",
        ),
        sa.Column(
            "mode",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'batch'"),
            comment="batch(批量清单,旧 compare_hotels.txt)| cron(定时采集,旧 price_targets.txt)",
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "ebk_hotel_id",
            sa.String(length=64),
            nullable=True,
            comment="携程锚点直达用(旧 ctrip.py:227/296 的 ebk_hotel_id 优先)",
        ),
        sa.Column("nights", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("remark", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["hotel_id"],
            ["core_hotels.id"],
            name="fk_cmp_price_targets_hotel_id_core_hotels",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_cmp_price_targets"),
        sa.UniqueConstraint("anchor_name", "city", name="cmp_target_key"),
    )
    op.create_index("cmp_target_mode", "cmp_price_targets", ["mode", "enabled"])

    # ==================================================================
    # ② cmp_price_comparisons —— 比价历史(D8 修复点)
    # ==================================================================
    op.create_table(
        "cmp_price_comparisons",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "hotel_id",
            sa.BigInteger(),
            nullable=True,
            comment="★ 旧表只有 anchor_name 文本 → 酒店改名即失联;补 id 关联",
        ),
        sa.Column("anchor_name", sa.String(length=256), nullable=False),
        sa.Column("city", sa.String(length=128), nullable=True),
        sa.Column("platform", sa.String(length=32), nullable=False, comment="ctrip | meituan"),
        sa.Column("query_date", sa.Date(), nullable=False),
        sa.Column(
            "query_slot",
            sa.String(length=32),
            nullable=False,
            comment="YYYY-MM-DD-HHMM —— 一天多次采集各留一份(V79);同 slot 重跑幂等(V78)",
        ),
        sa.Column("nights", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("hotel_name", sa.String(length=256), nullable=False),
        sa.Column("room_type", sa.String(length=128), nullable=True),
        sa.Column("price", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column(
            "distance_km",
            sa.Numeric(precision=8, scale=3),
            nullable=True,
            comment="★ 旧系统恒为 NULL(D13);段3 必须真填。8 位给跨城兜底留余量",
        ),
        sa.Column(
            "coord_source",
            sa.String(length=16),
            nullable=True,
            comment="api(接口坐标)| card(卡片距离文本)| city(城市中心兜底)| none",
        ),
        sa.Column("price_source", sa.String(length=16), nullable=True, comment="api | dom | vision | manual"),
        sa.Column(
            "price_scope",
            sa.String(length=8),
            nullable=False,
            server_default=sa.text("'from'"),
            comment="★ from(列表页起价)| exact(确定价)—— 旧系统无此概念,把「¥236起」当成了房价",
        ),
        sa.Column(
            "need_manual_check",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="视觉价与 DOM 价偏差 >20% 时置 true,不静默采用(V75)",
        ),
        sa.Column(
            "degraded",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="走过兜底通道(城市中心坐标 / 视觉 / 平台顺序)",
        ),
        sa.Column(
            "price_rejected",
            postgresql.ARRAY(sa.Text()),
            nullable=True,
            comment="★ 被券价过滤器丢掉的候选原文 —— 丢弃必须可见(V83)",
        ),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("score", sa.Numeric(precision=4, scale=2), nullable=True),
        sa.Column("reviews", sa.Integer(), nullable=True),
        sa.Column(
            "is_demo",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="★ 显式列,不用 LIKE 猜(旧 exists_today 的 payload NOT LIKE '%演示%' 是脆弱写法)",
        ),
        sa.Column("raw_json_path", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["hotel_id"],
            ["core_hotels.id"],
            name="fk_cmp_price_comparisons_hotel_id_core_hotels",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_cmp_price_comparisons"),
        sa.UniqueConstraint(
            "anchor_name",
            "platform",
            "hotel_name",
            "room_type",
            "nights",
            "query_slot",
            name="cmp_price_key",
        ),
    )
    op.create_index("cmp_price_lookup", "cmp_price_comparisons", ["anchor_name", "query_date", "platform"])
    op.create_index("cmp_price_hotel_day", "cmp_price_comparisons", ["hotel_id", "query_date"])
    op.create_index("cmp_price_slot", "cmp_price_comparisons", ["query_date", "query_slot"])

    # ==================================================================
    # ③ cmp_batch_runs —— 每日一行(旧系统同日多条 done)
    # ==================================================================
    op.create_table(
        "cmp_batch_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("batch_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, comment="running | done | failed"),
        sa.Column("hotels_total", sa.Integer(), nullable=True),
        sa.Column("hotels_ok", sa.Integer(), nullable=True),
        sa.Column("hotels_failed", sa.Integer(), nullable=True),
        sa.Column("hotels_skipped", sa.Integer(), nullable=True),
        sa.Column(
            "attempt",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
            comment="第几次尝试;尝试级历史看 job_runs(段1)",
        ),
        sa.Column("summary_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_cmp_batch_runs"),
        sa.UniqueConstraint("batch_date", name="cmp_batch_key"),
    )


def downgrade() -> None:
    op.drop_table("cmp_batch_runs")
    op.drop_index("cmp_price_slot", table_name="cmp_price_comparisons")
    op.drop_index("cmp_price_hotel_day", table_name="cmp_price_comparisons")
    op.drop_index("cmp_price_lookup", table_name="cmp_price_comparisons")
    op.drop_table("cmp_price_comparisons")
    op.drop_index("cmp_target_mode", table_name="cmp_price_targets")
    op.drop_table("cmp_price_targets")
