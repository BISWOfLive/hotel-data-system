"""T6.6 每日自检 —— **只读聚合,B26 口径;只出指标,不推送**。

段1 的边界(段1 §1.3)
---------------------
"段1 只负责把数据取进来" —— 所以本模块**只做只读聚合并返回结构化指标**;
推送(企业微信运维群)整条链路留给段2 的 ``domains/ops/selfcheck_push.py``(T2G.1),
它直接消费这里的 :class:`SelfCheckReport`,**不重复写聚合口径**
(B26:``selfcheck.py:22-29`` 的字段即"灰度观察列")。

只读纪律
--------
全部指标只走 ``SELECT``(或文件系统只读扫描),**一行都不写库** —— 自检自己把库写坏
是最讽刺的故障。登录态的"需续登"判定复用 :meth:`SessionStore.need_renewal`,
不在自检里另立阈值(否则立刻出现第二个来源)。

指标清单(B26 / 旧 ``selfcheck.py:22-29`` 的字段逐项落地)
------------------------------------------------------
============================  ==========================================================
``disk``                       ``var/`` 所在盘 总量/已用/余量 GB + ``below_min_free``
``dirs``                       ``var/raw`` / ``screenshots`` / ``reports`` / ``backup`` / ``logs`` 的 MB 与文件数
``accounts``                   ``core_accounts`` 总数 + 按 ``status`` 分组
``hotels``                     ``core_hotels`` 总数 + ``status='active'``
``sessions``                   ``var/states/*.json`` 文件数 + ``sessions`` 表按 ``status`` 分组 + **需续登数**
``collect_yesterday``          昨日 ``collect_modules`` 按 ``status`` 分组与 ``success_rate``(★ ``no_data`` 不算失败)
``jobs_today``                 ``job_runs`` 今日按 ``status`` 分组
``interpreter``                ``Py_GIL_DISABLED`` 与 Python 版本(R14/S8)
============================  ==========================================================

> ⚠️ ``alert_portal_columns`` / ``alert_room_states`` / ``review_reviews`` /
> ``review_materials`` 四张表由**另一个并行任务**(批次 D)创建,可能还不存在 ——
> 本自检**故意不查它们**(不做 ``to_regclass`` 探测式硬凑):自检的职责是"段1 自己
> 产的数与自己的守门指标",跨批次表的健康度由批次 D 自己的任务报。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import distinct, func, select

from hoteldata.infra.db import Database
from hoteldata.infra.models import Account, CollectModule, Hotel, JobRun, Session
from hoteldata.infra.paths import Layout, get_layout
from hoteldata.infra.session_store import SessionStore
from hoteldata.logging import check_interpreter
from hoteldata.settings import Settings, get_settings

__all__ = [
    "CollectMetrics",
    "CountMetrics",
    "DirMetrics",
    "DiskMetrics",
    "SelfCheckReport",
    "SessionMetrics",
    "run",
]

#: ``var/`` 下要看体积的目录(相对 ``var/``)
_VOLUME_DIRS: tuple[str, ...] = ("raw", "screenshots", "reports", "backup", "logs")

#: 提取四态里**不算失败**的两态(``models/collect.py``:``no_data`` 不算失败)
_NON_FAILURE_STATUSES: tuple[str, ...] = ("ok", "no_data")


def _mb(value: int) -> float:
    return round(value / 1024**2, 3)


# ---------------------------------------------------------------------------
# 指标结构
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class DiskMetrics:
    """磁盘余量(总纲 §3.1:``DISK_MIN_FREE_GB`` 是清理与告警共同的门槛线)。"""

    path: str
    min_free_gb: float
    total_gb: float = 0.0
    used_gb: float = 0.0
    free_gb: float = 0.0
    below_min_free: bool = False
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "total_gb": self.total_gb,
            "used_gb": self.used_gb,
            "free_gb": self.free_gb,
            "min_free_gb": self.min_free_gb,
            "below_min_free": self.below_min_free,
            "error": self.error,
        }


@dataclass(slots=True)
class DirMetrics:
    """目录体积与文件数(磁盘涨满 S7 的观察列)。"""

    path: str
    exists: bool = False
    mb: float = 0.0
    files: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "exists": self.exists, "mb": self.mb, "files": self.files}


@dataclass(slots=True)
class CountMetrics:
    """通用计数:总数 + 按状态分组(账号/酒店/任务共用)。"""

    total: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"total": self.total, "by_status": dict(self.by_status), "error": self.error}


@dataclass(slots=True)
class SessionMetrics:
    """登录态:文件数 + 表内状态分布 + **需续登数**(B13 阈值 20 天)。"""

    files: int = 0
    handles: int = 0
    need_renewal: int = 0
    total: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "files": self.files,
            "handles": self.handles,
            "need_renewal": self.need_renewal,
            "total": self.total,
            "by_status": dict(self.by_status),
            "error": self.error,
        }


@dataclass(slots=True)
class CollectMetrics:
    """昨日提取成功率 —— ★ ``no_data`` **不算失败**(段1 验收 V8)。"""

    collect_date: str
    total: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    ok: int = 0
    no_data: int = 0
    degraded: int = 0
    failed: int = 0
    success_rate: float = 0.0
    hotels_covered: int = 0
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "collect_date": self.collect_date,
            "total": self.total,
            "by_status": dict(self.by_status),
            "ok": self.ok,
            "no_data": self.no_data,
            "degraded": self.degraded,
            "failed": self.failed,
            "success_rate": self.success_rate,
            "hotels_covered": self.hotels_covered,
            "error": self.error,
        }


@dataclass(slots=True)
class SelfCheckReport:
    """一次自检的全部指标(**段2 推送的唯一输入**)。"""

    checked_at: str
    timezone: str
    collect_date: str
    disk: DiskMetrics
    dirs: dict[str, DirMetrics] = field(default_factory=dict)
    accounts: CountMetrics = field(default_factory=CountMetrics)
    hotels: CountMetrics = field(default_factory=CountMetrics)
    sessions: SessionMetrics = field(default_factory=SessionMetrics)
    collect_yesterday: CollectMetrics | None = None
    jobs_today: CountMetrics = field(default_factory=CountMetrics)
    interpreter: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """没有任何采集错误即视为自检成功(磁盘低位不算"错误",它是一个指标)。"""
        return not self.errors

    @property
    def below_min_free(self) -> bool:
        return self.disk.below_min_free

    def as_dict(self) -> dict[str, Any]:
        return {
            "checked_at": self.checked_at,
            "timezone": self.timezone,
            "ok": self.ok,
            "disk": self.disk.as_dict(),
            "dirs": {name: metrics.as_dict() for name, metrics in self.dirs.items()},
            "accounts": self.accounts.as_dict(),
            "hotels": self.hotels.as_dict(),
            "sessions": self.sessions.as_dict(),
            "collect_yesterday": self.collect_yesterday.as_dict() if self.collect_yesterday else None,
            "jobs_today": self.jobs_today.as_dict(),
            "interpreter": dict(self.interpreter),
            "errors": list(self.errors),
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# 文件系统(只读)
# ---------------------------------------------------------------------------


def _disk_metrics(var_dir: Path, min_free_gb: float) -> DiskMetrics:
    metrics = DiskMetrics(path=str(var_dir), min_free_gb=min_free_gb)
    try:
        usage = shutil.disk_usage(str(var_dir))
    except OSError as exc:
        metrics.error = str(exc)
        metrics.below_min_free = True  # 量不出来 = 保守按低位处理
        return metrics
    metrics.total_gb = round(usage.total / 1024**3, 2)
    metrics.used_gb = round(usage.used / 1024**3, 2)
    metrics.free_gb = round(usage.free / 1024**3, 2)
    metrics.below_min_free = metrics.free_gb < min_free_gb
    return metrics


def _dir_metrics(path: Path, *, label: str) -> DirMetrics:
    metrics = DirMetrics(path=label)
    if not path.is_dir():
        return metrics
    metrics.exists = True
    total = 0
    files = 0
    try:
        entries = list(path.rglob("*"))
    except OSError:
        return metrics
    for item in entries:
        try:
            if not item.is_file():
                continue
            total += item.stat().st_size
            files += 1
        except OSError:
            continue
    metrics.mb = _mb(total)
    metrics.files = files
    return metrics


def _state_files(layout: Layout) -> int:
    try:
        return sum(1 for _ in layout.states_dir.glob("*.json"))
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# 库内只读聚合
# ---------------------------------------------------------------------------


async def _count_by_status(db: Database, model: type[Any], column: Any) -> CountMetrics:
    """``SELECT <status>, count(*) ... GROUP BY <status>``(**只读**)。"""
    metrics = CountMetrics()
    try:
        async with db.session() as session:
            rows = (await session.execute(select(column, func.count()).group_by(column))).all()
    except Exception as exc:  # noqa: BLE001 - 表未迁移/库连不上都要出报告而不是崩
        metrics.error = f"{model.__name__}: {exc}"
        return metrics
    for status, count in rows:
        metrics.by_status[str(status)] = int(count)
    metrics.total = sum(metrics.by_status.values())
    return metrics


async def _session_metrics(db: Database, store: SessionStore, layout: Layout) -> SessionMetrics:
    metrics = SessionMetrics()
    metrics.files = _state_files(layout)
    try:
        handles = store.all_handles()
    except Exception as exc:  # noqa: BLE001 - 单个坏文件不该打断自检
        metrics.error = f"scan_files: {exc}"
        handles = []
    metrics.handles = len(handles)
    metrics.need_renewal = sum(1 for handle in handles if store.need_renewal(handle))
    table = await _count_by_status(db, Session, Session.status)
    metrics.total = table.total
    metrics.by_status = table.by_status
    if table.error and not metrics.error:
        metrics.error = table.error
    return metrics


async def _collect_metrics(db: Database, day: date) -> CollectMetrics:
    """昨日提取成功率:``success_rate = (ok + no_data) / total``(**``no_data`` 不算失败**)。"""
    metrics = CollectMetrics(collect_date=day.isoformat())
    try:
        async with db.session() as session:
            rows = (
                await session.execute(
                    select(CollectModule.status, func.count())
                    .where(CollectModule.collect_date == day)
                    .group_by(CollectModule.status)
                )
            ).all()
            metrics.hotels_covered = int(
                await session.scalar(
                    select(func.count(distinct(CollectModule.hotel_id))).where(
                        CollectModule.collect_date == day
                    )
                )
                or 0
            )
    except Exception as exc:  # noqa: BLE001
        metrics.error = f"collect_modules: {exc}"
        return metrics
    for status, count in rows:
        metrics.by_status[str(status)] = int(count)
    metrics.total = sum(metrics.by_status.values())
    metrics.ok = metrics.by_status.get("ok", 0)
    metrics.no_data = metrics.by_status.get("no_data", 0)
    metrics.degraded = metrics.by_status.get("degraded", 0)
    metrics.failed = metrics.by_status.get("failed", 0)
    if metrics.total:
        good = sum(metrics.by_status.get(s, 0) for s in _NON_FAILURE_STATUSES)
        metrics.success_rate = round(good / metrics.total, 4)
    return metrics


async def _jobs_today(db: Database, start: datetime) -> CountMetrics:
    """今日 ``job_runs``:按 ``started_at`` 归日(未启动的行退回 ``scheduled_at``)。"""
    metrics = CountMetrics()
    day_column = func.coalesce(JobRun.started_at, JobRun.scheduled_at)
    try:
        async with db.session() as session:
            rows = (
                await session.execute(
                    select(JobRun.status, func.count()).where(day_column >= start).group_by(JobRun.status)
                )
            ).all()
    except Exception as exc:  # noqa: BLE001
        metrics.error = f"job_runs: {exc}"
        return metrics
    for status, count in rows:
        metrics.by_status[str(status)] = int(count)
    metrics.total = sum(metrics.by_status.values())
    return metrics


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


async def run(settings: Settings | None = None, *, db: Database | None = None) -> SelfCheckReport:
    """聚合一次自检指标(**只读**;不推送)。

    :param settings: 缺省用进程单例配置。
    :param db: 复用调用方(调度层)的 :class:`Database`;缺省自己建一个并在结束时释放。
    """
    s = settings or get_settings()
    layout = get_layout(s)
    now = datetime.now(s.tzinfo)
    today = now.date()
    yesterday = today - timedelta(days=1)
    day_start = datetime.combine(today, time.min, tzinfo=s.tzinfo)

    database = db or Database(s)
    owns_db = db is None
    store = SessionStore(settings=s, db=database, layout=layout)

    report = SelfCheckReport(
        checked_at=now.isoformat(timespec="seconds"),
        timezone=s.tz,
        collect_date=yesterday.isoformat(),
        disk=_disk_metrics(s.paths.var_dir, s.ops.disk_min_free_gb),
        interpreter=check_interpreter(),
        collect_yesterday=CollectMetrics(collect_date=yesterday.isoformat()),
    )
    # ★ 措辞已按段2 更新:这一段文字会**原样进推送文案**(段2 §T2G.1 的
    #   ``selfcheck_push.build_markdown`` 会带上 notes),所以不能再说"不推送" ——
    #   否则运维群里收到的消息会自相矛盾("本模块不推送"出现在一条推送里)。
    #   边界本身没变:**本模块自己**依然一个字节都不发。
    report.notes.append("本模块只出指标、不发送;推送由段2 ops.selfcheck 的推送壳完成(T2G.1)")
    report.notes.append("四张并行批次表(alert_*/review_*)不在自检范围:本模块只查段1 自己的表")

    try:
        report.dirs = {
            name: _dir_metrics(layout.var_dir / name, label=f"var/{name}") for name in _VOLUME_DIRS
        }
        report.accounts = await _count_by_status(database, Account, Account.status)
        report.hotels = await _count_by_status(database, Hotel, Hotel.status)
        report.sessions = await _session_metrics(database, store, layout)
        report.collect_yesterday = await _collect_metrics(database, yesterday)
        report.jobs_today = await _jobs_today(database, day_start)
    finally:
        if owns_db:
            await database.dispose()

    for label, payload in (
        ("disk", report.disk.error),
        ("accounts", report.accounts.error),
        ("hotels", report.hotels.error),
        ("sessions", report.sessions.error),
        ("collect_yesterday", report.collect_yesterday.error if report.collect_yesterday else None),
        ("jobs_today", report.jobs_today.error),
    ):
        if payload:
            report.errors.append(f"{label}: {payload}")

    if report.disk.below_min_free and not report.disk.error:
        report.notes.append(
            f"⚠️ 磁盘余量 {report.disk.free_gb} GB < 门槛 {report.disk.min_free_gb} GB"
            "(告警由段2 推送,段1 只报数)"
        )
    logger.info(
        "自检完成: 磁盘余量 {} GB(低位={}) 账号 {} 酒店 {}(active {}) 登录态 {}(需续登 {}) "
        "昨日提取 {}/{} 成功率 {} 今日任务 {} 错误 {}",
        report.disk.free_gb,
        report.disk.below_min_free,
        report.accounts.total,
        report.hotels.total,
        report.hotels.by_status.get("active", 0),
        report.sessions.total,
        report.sessions.need_renewal,
        (report.collect_yesterday.ok + report.collect_yesterday.no_data) if report.collect_yesterday else 0,
        report.collect_yesterday.total if report.collect_yesterday else 0,
        report.collect_yesterday.success_rate if report.collect_yesterday else 0.0,
        report.jobs_today.total,
        len(report.errors),
    )
    return report
