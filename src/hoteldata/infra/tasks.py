"""★ 任务注册表 + ``job_runs`` + 补跑(T6.1 / T6.2 / T6.3)。

**时刻表唯一来源 = 代码里的任务注册表**(总纲 7.5)
--------------------------------------------------
旧系统「时刻表散在 4 处」:``.env``(时刻) + ``scheduler.py``(注册) +
``report_schedule.json``(报告桶) + ``push_rotation.json``(轮换)。

新架构:**时刻表只写在 ``@task(...)`` 装饰器里**;``.env`` 只保留 ``*_ENABLED``。
APScheduler 用**内存 jobstore**(仅触发)—— 若把 jobstore 也持久化到 PG,
立刻产生"DB 里的时刻表 vs 代码里的时刻表"两个来源,**那正是旧系统的病根**。
**持久化的只有运行状态**(``job_runs``)。

执行流程(幂等 + 防并发 + 可观测)::

    APScheduler 到点(AsyncIOScheduler,内存 jobstore,timezone=Asia/Shanghai)
      └→ INSERT INTO job_runs(task, scheduled_at, trigger='schedule')
           ON CONFLICT DO NOTHING                -- 幂等:抢不到 = 已跑过
           └→ ★ 独占一条连接 engine.connect()(不走 session 池)
                └→ SELECT pg_try_advisory_lock(hash(task))
                     ├─ 拿不到 → status=skipped
                     └─ 拿到 → running → 执行 → ok|failed + summary|error → 解锁 → 归还

**补跑(必须按这个写,别写错)**
------------------------------
1. 从**任务注册表**反推每个 ``catch_up=True`` 任务**当天的全部应跑时刻**;
2. ``left join job_runs`` 找出**缺失**或 ``failed`` 的行;
3. 若 ``now - scheduled_at > max_delay`` → 记 ``skipped``,**不跑**
   (避免 18:00 去补 00:40 的提取:窗口已错位,白烧风控额度);
4. 否则以 ``trigger='catchup'`` 执行。

> ❌ **错误做法**:扫 ``job_runs`` 里 ``status != 'ok'`` 的补跑。
> 若进程在触发时刻是关着的,**那行记录根本不存在**,永远扫不到 ——
> 而这恰是最该救的场景。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from hoteldata.infra.db import Database
from hoteldata.infra.models import JobRun

__all__ = [
    "TaskRegistry",
    "TaskRunResult",
    "TaskSpec",
    "get_registry",
    "nominal_slot",
    "task",
]

TaskFn = Callable[..., Awaitable[dict[str, Any] | None]]


def _parse_delay(value: str | int | float | timedelta | None) -> timedelta | None:
    """``"6h"`` / ``"30m"`` / ``"90s"`` / ``timedelta`` → ``timedelta``。"""
    if value is None:
        return None
    if isinstance(value, timedelta):
        return value
    if isinstance(value, (int, float)):
        return timedelta(seconds=float(value))
    raw = str(value).strip().lower()
    if not raw:
        return None
    units = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    suffix = raw[-1]
    if suffix in units:
        return timedelta(seconds=float(raw[:-1]) * units[suffix])
    return timedelta(seconds=float(raw))


def nominal_slot(spec: TaskSpec, now: datetime, tz: Any) -> datetime:
    """算出 ``now`` 对应的**名义计划时刻**(≤ now 的最近一个 cron 触发点)。

    ``job_runs.scheduled_at`` 要的是"计划触发时刻",不能拿"实际开始时刻"顶替 ——
    否则同一计划时刻的重跑会因秒级差异各自插一行,幂等约束形同虚设。
    """
    for day in (now.date(), now.date() - timedelta(days=1)):
        times = [t for t in spec.fire_times(day, tz) if t <= now]
        if times:
            return times[-1]
    return now.replace(second=0, microsecond=0)


@dataclass(frozen=True, slots=True)
class TaskSpec:
    """一个任务的注册信息(**时刻表的唯一来源**)。"""

    name: str
    func: TaskFn
    cron: str
    catch_up: bool = False
    max_delay: timedelta | None = None
    lock: bool = True
    domain: str = ""
    description: str = ""
    enabled_flag: str | None = None

    def trigger(self, tz: Any) -> CronTrigger:
        return CronTrigger.from_crontab(self.cron, timezone=tz)

    def fire_times(self, day: date, tz: Any) -> list[datetime]:
        """★ 反推该任务在**某一天**的全部应跑时刻。"""
        trigger = self.trigger(tz)
        start = datetime.combine(day, time.min, tzinfo=tz)
        end = start + timedelta(days=1)
        out: list[datetime] = []
        cursor = start - timedelta(microseconds=1)
        prev: datetime | None = None
        # 上限保护:一天最多 24*60 次(每分钟一次)
        for _ in range(1441):
            # ★ APScheduler **3.x** 的方法名是 get_next_fire_time(previous, now);
            #   get_fire_time 是 4.x 的 API,段1 明确禁用 4.x。
            nxt = trigger.get_next_fire_time(prev, cursor)
            if nxt is None:
                break
            nxt = nxt.astimezone(tz)
            if nxt >= end:
                break
            if nxt >= start:
                out.append(nxt)
            prev, cursor = nxt, nxt
        return out


@dataclass(slots=True)
class TaskRunResult:
    """一次任务执行的结果。"""

    task: str
    status: str  # ok | failed | skipped
    job_run_id: int | None = None
    scheduled_at: datetime | None = None
    trigger: str = "manual"
    summary: dict[str, Any] | None = None
    error: str | None = None
    duration_ms: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "status": self.status,
            "job_run_id": self.job_run_id,
            "scheduled_at": self.scheduled_at.isoformat() if self.scheduled_at else None,
            "trigger": self.trigger,
            "summary": self.summary,
            "error": self.error,
            "duration_ms": round(self.duration_ms, 1) if self.duration_ms else None,
        }


@dataclass
class TaskRegistry:
    """任务注册表。**时刻表唯一来源。**"""

    specs: dict[str, TaskSpec] = field(default_factory=dict)

    # ------------------------------------------------------------------

    def register(self, spec: TaskSpec) -> None:
        if spec.name in self.specs:
            raise ValueError(f"任务名重复注册: {spec.name}")
        # 校验 cron 表达式(启动即失败,不留到运行时)
        try:
            CronTrigger.from_crontab(spec.cron)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"任务 {spec.name} 的 cron 非法: {spec.cron!r} — {exc}") from exc
        self.specs[spec.name] = spec

    def get(self, name: str) -> TaskSpec:
        spec = self.specs.get(name)
        if spec is None:
            raise KeyError(f"未注册的任务 {name!r};已知:{sorted(self.specs)}")
        return spec

    def names(self) -> list[str]:
        return sorted(self.specs)

    def all(self) -> list[TaskSpec]:
        return [self.specs[n] for n in self.names()]

    def catch_up_specs(self) -> list[TaskSpec]:
        return [s for s in self.all() if s.catch_up]

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------

    async def run(
        self,
        name: str,
        runtime: Any,
        *,
        trigger: str = "manual",
        scheduled_at: datetime | None = None,
        args: dict[str, Any] | None = None,
        kwargs: dict[str, Any] | None = None,
        skip_delay_check: bool = False,
        now: datetime | None = None,
    ) -> TaskRunResult:
        """★ 执行一个任务(幂等 + advisory lock + 可观测)。

        ``now`` 是**参考时刻**(默认取真实当前时间)。:meth:`catch_up` 会把它显式传进来,
        保证"补跑判定的参考时刻"与"max_delay 判定的参考时刻"是**同一个** ——
        否则 ``catch_up(now=...)`` 会一边按传入时刻反推测算应跑项、一边按真实
        时钟判 max_delay,两边不一致,既难测也难解释。
        """
        spec = self.get(name)
        db: Database = runtime.db
        tz = runtime.settings.tzinfo
        now = now or datetime.now(tz)

        # ---- 补跑前的 max_delay 判定(超时不跑,记 skipped) ----
        if (
            scheduled_at is not None
            and not skip_delay_check
            and spec.max_delay is not None
            and trigger == "catchup"
        ):
            delay = now - scheduled_at
            if delay > spec.max_delay:
                job_id = await self._record_skipped(
                    db,
                    spec,
                    scheduled_at,
                    trigger,
                    f"超过 max_delay={spec.max_delay}(实际延迟 {delay}),窗口已错位,不补跑",
                    args,
                )
                logger.warning("任务 {} 超时不补跑:延迟 {} > max_delay {}", name, delay, spec.max_delay)
                return TaskRunResult(
                    task=name,
                    status="skipped",
                    job_run_id=job_id,
                    scheduled_at=scheduled_at,
                    trigger=trigger,
                    error="超过 max_delay",
                )

        # ---- ① 幂等占位(仅"计划触发") ----
        job_id = await self._claim(db, spec, scheduled_at, trigger, args)
        if job_id is None:
            logger.info("任务 {} 在 {} 已跑过(ON CONFLICT DO NOTHING)", name, scheduled_at)
            return TaskRunResult(
                task=name,
                status="skipped",
                scheduled_at=scheduled_at,
                trigger=trigger,
                error="该计划时刻已有记录",
            )

        # ---- ② advisory lock(独占连接,不走池) ----
        if spec.lock:
            async with db.advisory_lock(f"task:{name}") as acquired:
                if not acquired:
                    await self._finish(db, job_id, "skipped", error="advisory lock 未获取到(已有实例在跑)")
                    logger.warning("任务 {} 未拿到 advisory lock → skipped", name)
                    return TaskRunResult(
                        task=name,
                        status="skipped",
                        job_run_id=job_id,
                        scheduled_at=scheduled_at,
                        trigger=trigger,
                        error="advisory lock 未获取到",
                    )
                return await self._execute(spec, runtime, job_id, scheduled_at, trigger, kwargs or {})
        return await self._execute(spec, runtime, job_id, scheduled_at, trigger, kwargs or {})

    async def _execute(
        self,
        spec: TaskSpec,
        runtime: Any,
        job_id: int,
        scheduled_at: datetime | None,
        trigger: str,
        kwargs: dict[str, Any],
    ) -> TaskRunResult:
        started = datetime.now(runtime.settings.tzinfo)
        await self._mark_running(runtime.db, job_id, started)
        try:
            summary = await spec.func(runtime, **kwargs)
            finished = datetime.now(runtime.settings.tzinfo)
            await self._finish(runtime.db, job_id, "ok", summary=summary, finished_at=finished)
            duration = (finished - started).total_seconds() * 1000.0
            logger.info("任务 {} 完成({}ms) summary={}", spec.name, round(duration), _brief(summary))
            return TaskRunResult(
                task=spec.name,
                status="ok",
                job_run_id=job_id,
                scheduled_at=scheduled_at,
                trigger=trigger,
                summary=summary,
                duration_ms=duration,
            )
        except Exception as exc:  # noqa: BLE001 - 任务失败要落库,不吞
            finished = datetime.now(runtime.settings.tzinfo)
            logger.exception("任务 {} 失败", spec.name)
            await self._finish(
                runtime.db,
                job_id,
                "failed",
                error=f"{type(exc).__name__}: {exc}",
                finished_at=finished,
            )
            return TaskRunResult(
                task=spec.name,
                status="failed",
                job_run_id=job_id,
                scheduled_at=scheduled_at,
                trigger=trigger,
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=(finished - started).total_seconds() * 1000.0,
            )

    # ------------------------------------------------------------------
    # job_runs 读写
    # ------------------------------------------------------------------

    async def _claim(
        self,
        db: Database,
        spec: TaskSpec,
        scheduled_at: datetime | None,
        trigger: str,
        args: dict[str, Any] | None,
    ) -> int | None:
        """插入 ``job_runs`` 占位;**抢不到返回 None**(已跑过)。"""
        values = {
            "task": spec.name,
            "scheduled_at": scheduled_at,
            "status": "pending",
            "trigger": trigger,
            "attempt": 1,
            "args": args,
            "host": Database.hostname(),
            "pid": os.getpid(),
        }
        stmt = pg_insert(JobRun).values(**values)
        if scheduled_at is not None:
            # ★ 部分唯一索引的任务冲突推断必须带 index_where
            stmt = stmt.on_conflict_do_nothing(
                index_elements=["task", "scheduled_at"],
                index_where=text("scheduled_at IS NOT NULL"),
            )
        stmt = stmt.returning(JobRun.id)
        async with db.session() as s:
            return await s.scalar(stmt)

    async def _mark_running(self, db: Database, job_id: int, started: datetime) -> None:
        async with db.session() as s:
            await s.execute(
                JobRun.__table__.update()
                .where(JobRun.__table__.c.id == job_id)
                .values(status="running", started_at=started)
            )

    async def _finish(
        self,
        db: Database,
        job_id: int,
        status: str,
        *,
        summary: dict[str, Any] | None = None,
        error: str | None = None,
        finished_at: datetime | None = None,
    ) -> None:
        async with db.session() as s:
            await s.execute(
                JobRun.__table__.update()
                .where(JobRun.__table__.c.id == job_id)
                .values(
                    status=status,
                    summary=summary,
                    error=error,
                    finished_at=finished_at or datetime.now(),
                )
            )

    async def _record_skipped(
        self,
        db: Database,
        spec: TaskSpec,
        scheduled_at: datetime | None,
        trigger: str,
        reason: str,
        args: dict[str, Any] | None,
    ) -> int | None:
        job_id = await self._claim(db, spec, scheduled_at, trigger, args)
        if job_id is not None:
            await self._finish(db, job_id, "skipped", error=reason)
        return job_id

    # ------------------------------------------------------------------
    # 补跑
    # ------------------------------------------------------------------

    async def catch_up(
        self,
        runtime: Any,
        *,
        now: datetime | None = None,
        only: list[str] | None = None,
    ) -> list[TaskRunResult]:
        """★ 补跑算法(注册表反推 + left join + ``max_delay`` 判定)。"""
        db: Database = runtime.db
        tz = runtime.settings.tzinfo
        now = now or datetime.now(tz)
        today = now.date()
        specs = [s for s in self.catch_up_specs() if not only or s.name in only]
        results: list[TaskRunResult] = []

        for spec in specs:
            # ① 从注册表反推当天全部应跑时刻(只补"已经到点"的)
            due = [t for t in spec.fire_times(today, tz) if t <= now]
            if not due:
                continue
            # ② left join job_runs 找缺失 / failed
            missing = await self._missing_runs(db, spec.name, due)
            for scheduled_at in missing:
                # ③④ max_delay 判定在 run() 里做;★ 参考时刻必须沿用同一个 now
                res = await self.run(
                    spec.name,
                    runtime,
                    trigger="catchup",
                    scheduled_at=scheduled_at,
                    kwargs={},
                    now=now,
                )
                results.append(res)
        return results

    async def _missing_runs(self, db: Database, task: str, due: list[datetime]) -> list[datetime]:
        """应跑时刻中**没有 ok 记录**的那些。"""
        if not due:
            return []
        async with db.session() as s:
            rows = (
                await s.execute(
                    select(JobRun.scheduled_at, JobRun.status).where(
                        JobRun.task == task,
                        JobRun.scheduled_at.in_(due),
                    )
                )
            ).all()
        have_ok = {r[0] for r in rows if r[1] == "ok"}
        have_any = {r[0] for r in rows}
        out: list[datetime] = []
        for t in due:
            if t in have_ok:
                continue
            if t not in have_any:
                out.append(t)  # ★ 进程当时是关着的 → 那行根本不存在 → 必须补
            elif not await self._has_running(db, task, t):
                out.append(t)  # failed / skipped → 也给一次机会
        return out

    async def _has_running(self, db: Database, task: str, scheduled_at: datetime) -> bool:
        async with db.session() as s:
            n = await s.scalar(
                select(func.count())
                .select_from(JobRun)
                .where(
                    JobRun.task == task,
                    JobRun.scheduled_at == scheduled_at,
                    JobRun.status == "running",
                )
            )
        return bool(n)

    # ------------------------------------------------------------------
    # APScheduler
    # ------------------------------------------------------------------

    def build_scheduler(self, runtime: Any) -> AsyncIOScheduler:
        """构建 APScheduler(**内存 jobstore**,仅触发)。"""
        tz = runtime.settings.tzinfo
        scheduler = AsyncIOScheduler(
            timezone=tz,
            job_defaults={
                "max_instances": 1,  # 同任务不并发
                "coalesce": True,  # 错过的合并成一次
                "misfire_grace_time": 3600,
            },
        )
        for spec in self.all():
            if spec.enabled_flag and not getattr(runtime.settings, spec.enabled_flag, True):
                logger.info("任务 {} 已被 {} 关闭,不注册", spec.name, spec.enabled_flag)
                continue

            async def _job(name: str = spec.name) -> None:
                # ★ 计划时刻必须显式算出:否则 scheduled_at=None,部分唯一索引不生效,
                #    "抢不到 = 已跑过"的幂等语义会整个失效。
                sched_at = nominal_slot(self.get(name), datetime.now(tz), tz)
                await self.run(name, runtime, trigger="schedule", scheduled_at=sched_at)

            scheduler.add_job(
                _job,
                trigger=spec.trigger(tz),
                id=spec.name,
                name=spec.name,
                replace_existing=True,
            )
        return scheduler


def _brief(summary: dict[str, Any] | None) -> str:
    if not summary:
        return "{}"
    keys = ("total", "ok", "degraded", "no_data", "failed", "modules", "hotels", "status")
    picked = {k: summary[k] for k in keys if k in summary}
    return str(picked or {k: summary[k] for k in list(summary)[:4]})


# ---------------------------------------------------------------------------
# 全局注册表 + 装饰器
# ---------------------------------------------------------------------------

_REGISTRY = TaskRegistry()


def get_registry() -> TaskRegistry:
    """取全局任务注册表。"""
    return _REGISTRY


def task(
    name: str,
    *,
    cron: str,
    catch_up: bool = False,
    max_delay: str | int | float | timedelta | None = None,
    lock: bool = True,
    domain: str = "",
    description: str = "",
    enabled_flag: str | None = None,
) -> Callable[[TaskFn], TaskFn]:
    """注册一个任务。

    ``cron`` 为 **5 段 crontab**(分 时 日 月 周),时区固定 ``Asia/Shanghai``。
    """

    def decorator(func: TaskFn) -> TaskFn:
        doc_line = ""
        if func.__doc__:
            lines = [ln.strip() for ln in func.__doc__.strip().splitlines() if ln.strip()]
            doc_line = lines[0] if lines else ""
        _REGISTRY.register(
            TaskSpec(
                name=name,
                func=func,
                cron=cron,
                catch_up=catch_up,
                max_delay=_parse_delay(max_delay),
                lock=lock,
                domain=domain,
                description=description or doc_line,
                enabled_flag=enabled_flag,
            )
        )
        return func

    return decorator


async def wait_for_scheduler_stop(scheduler: AsyncIOScheduler, timeout_s: float = 30.0) -> None:
    """优雅停止(供 Runtime 析构用)。"""
    try:
        scheduler.shutdown(wait=True)
    except Exception as exc:  # noqa: BLE001
        logger.debug("停止调度器失败: {}", exc)
    await asyncio.sleep(0)
    _ = timeout_s
