"""``cmp_price_comparisons`` 唯一约束改 ``NULLS NOT DISTINCT`` —— **D8 的真正修复**。

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-01

## 为什么必须改(实测发现的真 bug)

``0005`` 建的唯一约束是::

    unique (anchor_name, platform, hotel_name, room_type, nights, query_slot)

看起来没问题,**但在 PostgreSQL 里 ``NULL`` 在 UNIQUE 约束中互不相等**
(SQL 标准:``NULL IS NOT NULL`` → 每一行含 NULL 的组合都被视为唯一)。
而段3 当前**只取列表页起价**,``room_type`` 恒为 ``NULL`` —— 于是:

* ``INSERT ... ON CONFLICT (cmp_price_key) DO UPDATE`` **照常工作**
  (ON CONFLICT 用约束做仲裁,能正确匹配到"同名同 slot"那行);
* 但**裸 INSERT**(任何绕过 repo 的写入、以及"我以为约束会兜底"的所有场景)
  **不会被拒绝** —— 实测:同一行连插两次,两次都成功。

这正是 D8 的**同类病**:旧系统 ``storage/prices.py:88-106`` 是 SELECT-then-INSERT、
两表无 UNIQUE,于是写脏;段3 如果留着一个"看起来有约束、实际不挡重复"的设计,
等于**把同一个坑重新挖了一遍**,而且更难发现(因为 schema 里明明写着 UNIQUE)。

## 修法:``NULLS NOT DISTINCT``(PG 15+;本机实测 16.15)

.. code-block:: sql

    ALTER TABLE cmp_price_comparisons
      DROP CONSTRAINT cmp_price_key,
      ADD CONSTRAINT cmp_price_key
        UNIQUE NULLS NOT DISTINCT
        (anchor_name, platform, hotel_name, room_type, nights, query_slot);

这样 ``room_type IS NULL`` 的行之间**会互相冲突**,约束才真的生效。

## 为什么不是"把 NULL 换成空串"

那也能绕过 NULL 语义,但代价是**语义污染**:``''`` 与 ``NULL`` 都表示"无房型",
库里从此有两种"空",每个查询都要记得 ``coalesce``。``NULLS NOT DISTINCT``
是 PG 为这个场景提供的**原生语义**,优先用它。

> 备选方案 ``CREATE UNIQUE INDEX ... (..., room_type) NULLS NOT DISTINCT`` 等价,
> 但换约束比换索引的迁移更直白(约束名保持不变,下游 ``on_conflict_do_update``
> 的 ``constraint="cmp_price_key"`` 一行都不用改)。

## 回滚

``downgrade`` 换回不带 ``NULLS NOT DISTINCT`` 的普通唯一约束
(**注意:回滚会重新引入"NULL 不冲突"的行为**)。
"""

from __future__ import annotations

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ★ 换约束前先按"新语义"去重,否则 ADD CONSTRAINT 会因为已存在的
    #   "room_type 为 NULL 的重复行"而失败(实测正是这些行让旧约束形同虚设)。
    op.execute(
        """
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
        """
    )

    op.drop_constraint("cmp_price_key", "cmp_price_comparisons", type_="unique")
    op.execute(
        """
        ALTER TABLE cmp_price_comparisons
          ADD CONSTRAINT cmp_price_key
          UNIQUE NULLS NOT DISTINCT
          (anchor_name, platform, hotel_name, room_type, nights, query_slot);
        """
    )


def downgrade() -> None:
    op.drop_constraint("cmp_price_key", "cmp_price_comparisons", type_="unique")
    op.execute(
        """
        ALTER TABLE cmp_price_comparisons
          ADD CONSTRAINT cmp_price_key
          UNIQUE (anchor_name, platform, hotel_name, room_type, nights, query_slot);
        """
    )
