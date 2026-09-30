"""数据层 —— PG 异步连接 + 会话管理 + **advisory lock 独占连接**。

段1 T1.3 / T6.2。三条要点:

1. ``create_async_engine``(asyncpg) + ``async_sessionmaker(expire_on_commit=False)``
   + ``pool_pre_ping=True``。
2. 暴露 ``get_session()`` 依赖(FastAPI)与 :meth:`Database.session`(CLI/域层)。
3. ★ **独立提供 :meth:`Database.lock_connection`** 用于 advisory lock —— **不走池**。

为什么 advisory lock 必须独占连接
----------------------------------
PG 的 advisory lock 是**连接级**的:锁跟着连接走,连接关了才释放。

若走 SQLAlchemy 的 session 池,连接用完会被**归还复用**;此时锁仍然挂在那条
已归还的连接上 —— 下一个任务取到同一条连接就"莫名持有"了锁,而真正想拿锁的
人拿不到。**等于没锁。**

旧系统在这里栽过:``storage/db.py:143`` 的 ``_write_lock`` 是**实例级** ``RLock``,
而 ``Storage()`` 全仓有约 60 处各自 ``new`` —— 跨实例根本不互斥,实际只剩
SQLite 文件锁兜底。

所以本实现专门开一个 ``NullPool`` 引擎:每条连接都是**新建且独占**的,
``close()`` 即销毁 → 锁必然随连接释放,不可能被"继承"。
"""

from __future__ import annotations

import hashlib
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from hoteldata.settings import Settings, get_settings

__all__ = ["Database", "advisory_key", "get_session"]


def advisory_key(name: str) -> int:
    """把任务名映射为稳定的 64 位有符号整数(advisory lock 的 key)。

    用 blake2b 而非内置 ``hash()``:后者**跨进程不稳定**(PYTHONHASHSEED 随机),
    两个进程会对同一个任务算出不同的 key,锁形同虚设。
    """
    digest = hashlib.blake2b(name.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


class Database:
    """引擎 + 会话工厂 + 独占锁连接。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        url = self.settings.db.url

        self.engine: AsyncEngine = create_async_engine(
            url,
            echo=self.settings.db.echo,
            pool_size=self.settings.db.pool_size,
            max_overflow=self.settings.db.max_overflow,
            pool_timeout=self.settings.db.pool_timeout_s,
            pool_pre_ping=True,  # 防"服务端已断开的僵尸连接"
            pool_recycle=1800,
            connect_args={
                "server_settings": {
                    "timezone": self.settings.tz,
                    "application_name": "hoteldata",
                    "statement_timeout": str(self.settings.db.statement_timeout_ms),
                },
                # asyncpg 预处理语句缓存与 PgBouncer 不兼容;本机直连保持默认即可
                "timeout": self.settings.db.pool_timeout_s,
            },
        )
        self.sessionmaker: async_sessionmaker[AsyncSession] = async_sessionmaker(
            bind=self.engine,
            class_=AsyncSession,
            expire_on_commit=False,  # 提交后仍可读属性(避免 N+1 式重查)
            autoflush=False,
        )

        # ★ 锁专用引擎:NullPool = 每条连接独占且用完即销毁
        self._lock_engine: AsyncEngine = create_async_engine(
            url,
            echo=False,
            poolclass=NullPool,
            connect_args={
                "server_settings": {
                    "timezone": self.settings.tz,
                    "application_name": "hoteldata-lock",
                }
            },
        )

    # ------------------------------------------------------------------
    # 会话
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """短会话上下文:正常 commit / 异常 rollback / 始终 close。"""
        session = self.sessionmaker()
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
        finally:
            await session.close()

    # ------------------------------------------------------------------
    # 锁连接(不走池)
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def lock_connection(self) -> AsyncIterator[AsyncConnection]:
        """★ 独占一条连接(``NullPool``,用完即销毁)。

        **不要把这里换成 ``self.engine.connect()``** —— 那会从池里取,归还后
        advisory lock 会残留在这条被复用的连接上(见模块 docstring)。
        """
        conn = await self._lock_engine.connect()
        try:
            yield conn
        finally:
            await conn.close()

    @asynccontextmanager
    async def advisory_lock(self, key: str) -> AsyncIterator[bool]:
        """尝试获取任务级 advisory lock。

        ``yield`` 出 ``True`` 表示拿到锁;``False`` 表示别人正在跑(调用方应记
        ``status='skipped'``)。拿到锁期间**连接始终保持打开**。
        """
        lock_id = advisory_key(key)
        async with self.lock_connection() as conn:
            acquired = bool(await conn.scalar(text("select pg_try_advisory_lock(:k)"), {"k": lock_id}))
            try:
                yield acquired
            finally:
                if acquired:
                    try:
                        await conn.scalar(text("select pg_advisory_unlock(:k)"), {"k": lock_id})
                    except Exception:  # noqa: BLE001 - 连接已断时锁随连接释放
                        pass

    # ------------------------------------------------------------------
    # 健康检查 / 生命周期
    # ------------------------------------------------------------------

    async def ping(self) -> str:
        """返回 PG 版本串;连不上则抛异常(**不静默降级**)。"""
        async with self.engine.connect() as conn:
            version = await conn.scalar(text("select version()"))
        return str(version)

    async def server_info(self) -> dict[str, Any]:
        """``/healthz`` 与 ``db ping`` 用的最小信息集。"""
        async with self.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "select current_database() as db, current_user as usr, "
                            "current_setting('TimeZone') as tz, "
                            "(select count(*) from information_schema.tables "
                            " where table_schema='public') as tables"
                        )
                    )
                )
                .mappings()
                .one()
            )
        return {
            "database": row["db"],
            "user": row["usr"],
            "timezone": row["tz"],
            "tables": int(row["tables"]),
        }

    async def has_table(self, name: str) -> bool:
        async with self.engine.connect() as conn:
            found = await conn.scalar(text("select to_regclass(:n) is not null"), {"n": f"public.{name}"})
        return bool(found)

    async def dispose(self) -> None:
        await self.engine.dispose()
        await self._lock_engine.dispose()

    @staticmethod
    def hostname() -> str:
        try:
            return socket.gethostname()
        except OSError:  # pragma: no cover
            return "unknown"


# ---------------------------------------------------------------------------
# FastAPI 依赖
# ---------------------------------------------------------------------------

_session_dep_holder: Database | None = None


def bind_database(db: Database) -> None:
    """由 :class:`~hoteldata.runtime.Runtime` 在启动时绑定(替代模块级全局注入)。"""
    global _session_dep_holder
    _session_dep_holder = db


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖:每请求一个会话。"""
    if _session_dep_holder is None:  # pragma: no cover - 装配顺序错误
        raise RuntimeError("Database 未绑定:请先通过 Runtime 启动应用")
    async with _session_dep_holder.session() as session:
        yield session
