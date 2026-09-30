"""运维表:``ops_login_events``(登录事件) 与 ``job_runs``(★ 任务可观测的地基)。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, created_at_column

#: ``job_runs.status`` 取值
JOB_STATUSES = ("pending", "running", "ok", "failed", "skipped")

#: ``job_runs.trigger`` 取值
JOB_TRIGGERS = ("schedule", "manual", "catchup")

#: ``ops_login_events.action`` 取值
LOGIN_ACTIONS = ("patrol", "relogin", "renew", "expire", "fail")

#: ``ops_login_events.result`` 取值
LOGIN_RESULTS = ("ok", "fail")


class LoginEvent(Base):
    """登录管家事件(append-only)。

    ``detail`` 存放登录管家的**动作结果码**原文(旧 ``login_manager.py:36-44``):
    ``ok`` / ``captcha`` / ``manual_required`` / ``failed`` / ``timeout`` / ``blocked``。
    这是「单次登录动作的结果」,与 ``sessions.status``(长期会话态)是两个概念。
    """

    __tablename__ = "ops_login_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("core_accounts.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_column()


class JobRun(Base):
    """任务运行记录 —— 段1 可观测性的地基。

    ★ **部分唯一索引** ``job_runs_sched_uniq``:只约束 ``scheduled_at IS NOT NULL``
    的"计划触发",手动重跑(``scheduled_at = NULL``)可任意多次。

    为什么 advisory lock 必须独占连接:PG 的 advisory lock 是**连接级**的。
    走 SQLAlchemy session 池的话连接会被归还复用,锁会落在一条已归还的连接上
    —— 等于没锁(旧系统 ``_write_lock`` 是实例级 RLock,而 ``Storage()`` 有约 60 处
    各自 new,跨实例根本不互斥)。
    """

    __tablename__ = "job_runs"
    __table_args__ = (
        Index(
            "uq_job_runs_sched_uniq",
            "task",
            "scheduled_at",
            unique=True,
            postgresql_where=text("scheduled_at IS NOT NULL"),
        ),
        Index("ix_job_runs_task_time", "task", text("started_at DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task: Mapped[str] = mapped_column(String(128), nullable=False)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    args: Mapped[dict | None] = mapped_column(JSONB)
    summary: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    host: Mapped[str | None] = mapped_column(String(128))
    pid: Mapped[int | None] = mapped_column(Integer)

    @property
    def duration_ms(self) -> float | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds() * 1000.0

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<JobRun {self.task} {self.status} trigger={self.trigger}>"


__all__ = [
    "JOB_STATUSES",
    "JOB_TRIGGERS",
    "LOGIN_ACTIONS",
    "LOGIN_RESULTS",
    "JobRun",
    "LoginEvent",
]
