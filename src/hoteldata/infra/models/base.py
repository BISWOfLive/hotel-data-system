"""SQLAlchemy 2 声明式基类与命名约定。

命名约定集中在此:显式约束名让 Alembic 迁移稳定(旧系统的教训是无迁移管理 +
``CREATE TABLE IF NOT EXISTS`` + ``try: ALTER ... except: pass`` 的伪迁移)。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

#: 约束命名模板 —— 让 Alembic 生成可预测的约束名
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """全局声明式基类。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def created_at_column() -> Mapped[datetime]:
    """``timestamptz not null default now()``(统一带时区,不用 4 种时间存法)。"""
    return mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


__all__ = ["NAMING_CONVENTION", "Base", "created_at_column"]
