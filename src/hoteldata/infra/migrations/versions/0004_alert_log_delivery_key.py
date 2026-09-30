"""``alert_logs`` 的键拆分 —— ``dedup_key`` → ``delivery_key`` + ``trigger_key``。

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-01

## 为什么改(设计决定,不是改名游戏)

段2 的 ``alert_logs`` 一行 = **一个触发 × 一个收件人**。这不是可选项:
计划书 §5.7 把送达率定义成「成功行 ÷ 总行;**无管理群也要计入分母**
(写 ``recipient='manage-none'``)」,而 ``manage-none`` 是一个**收件人值**。
若一行代表一个触发,那条"推 3 个目标只留 1 行"的记录既不能写 ``pushed=true``
(B/C 两个目标的失败会在日志里彻底消失 —— D1「出事了没人知道」的同一类病),
也不能写 ``false``(A 的成功被抹掉、分母被低估)。**没有第三种写法。**

但行带上 ``recipient`` 之后,``0003`` 里那个 ``dedup_key`` 就名不副实了:
真正的"当日去重"是**触发级**的,由 ``alert_states.should_push`` 承担。
所以本次把它拆成两个键、各司其职:

==========================  ============================================  ==============
列                           值                                              职责
==========================  ============================================  ==============
``delivery_key``            ``rule:hotel:entity:YYYY-MM-DD:recipient``      **行身份**
                            **UNIQUE**
``trigger_key``             ``rule:hotel:entity:YYYY-MM-DD``                **触发身份**
                            (普通索引)                                       (按触发聚合)
==========================  ============================================  ==============

``trigger_key`` 才是计划书 §5.7 写的那个「日志去重键」,现在它是一等公民,
可以按触发聚合查"这条预警推给了哪些目标、哪些失败"。

## 回填口径

``recipient`` 是 ``delivery_key`` 的**最后一段且不含冒号**(chatid 是
``wrXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX`` 这种 34 字符、无管理群是 ``manage-none``),
而 ``entity_key`` **可能含冒号**(``event:中秋节:2026-10-11``)。
所以回填**只能从右往左剥一段**,不能用 ``split_part`` 从左数:

.. code-block:: sql

    regexp_replace(delivery_key, ':[^:]*$', '')

## 与 `0003` 的关系

``0003`` **不改**(迁移是不可变历史)。本次只做三件事:改列名、加列并回填、
重命名唯一约束(``uq_alert_logs_dedup_key`` → ``uq_alert_logs_delivery_key``,
让约束名与列名一致 —— PG 改列名**不会**连带改约束名)。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # ① 行身份列改名:dedup_key → delivery_key
    # ------------------------------------------------------------------
    op.alter_column("alert_logs", "dedup_key", new_column_name="delivery_key")
    # ★ PG 的 RENAME COLUMN 不会改约束名 —— 显式改名,否则约束名会永远停在
    #   "dedup_key" 上,以后读 schema 的人会以为还有一列叫 dedup_key。
    op.execute("ALTER TABLE alert_logs RENAME CONSTRAINT uq_alert_logs_dedup_key TO uq_alert_logs_delivery_key")

    # ------------------------------------------------------------------
    # ② 新增 trigger_key(触发身份)并回填
    #    先 nullable 建列 → 回填 → 再 SET NOT NULL:表里可能已有真实行,
    #    直接建 NOT NULL 会失败(本机当前 0 行,但迁移不能依赖"恰好为空")。
    # ------------------------------------------------------------------
    op.add_column(
        "alert_logs",
        sa.Column(
            "trigger_key",
            sa.String(length=512),
            nullable=True,
            comment="触发身份 rule:hotel:entity:YYYY-MM-DD(计划书 §5.7 的「日志去重键」)",
        ),
    )
    # ★ 从右往左剥掉最后一段(recipient 不含冒号;entity_key 可能含冒号)
    op.execute("UPDATE alert_logs SET trigger_key = regexp_replace(delivery_key, ':[^:]*$', '')")
    op.alter_column("alert_logs", "trigger_key", nullable=False)

    op.create_index("ix_alert_logs_trigger", "alert_logs", ["trigger_key"])


def downgrade() -> None:
    op.drop_index("ix_alert_logs_trigger", table_name="alert_logs")
    op.drop_column("alert_logs", "trigger_key")
    op.execute("ALTER TABLE alert_logs RENAME CONSTRAINT uq_alert_logs_delivery_key TO uq_alert_logs_dedup_key")
    op.alter_column("alert_logs", "delivery_key", new_column_name="dedup_key")
