"""Alembic 环境 —— 异步引擎 + 连接串唯一来源 ``.env``。

``alembic.ini`` 里 ``sqlalchemy.url`` 留空,这里从 :func:`hoteldata.settings.get_settings`
取 ``DB_URL`` 注入,避免"两个来源"。
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from hoteldata.infra.models import Base
from hoteldata.settings import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# 目标元数据(供 autogenerate 比对)
target_metadata = Base.metadata


def _db_url() -> str:
    url = get_settings().db.url
    # Alembic 用 ConfigParser 存值,`%` 是插值字符,需转义
    return url.replace("%", "%%")


def run_migrations_offline() -> None:
    """离线模式:只生成 SQL,不连库。"""
    context.configure(
        url=_db_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_schemas=False,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    configuration = config.get_section(config.config_ini_section, {}) or {}
    configuration["sqlalchemy.url"] = _db_url()
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        future=True,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
