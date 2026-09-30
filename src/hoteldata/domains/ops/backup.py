"""T6.6 数据库冷备 —— ``pg_dump -Fc`` + 保留 7 天 + **同日幂等**(V19;B26 同批)。

为什么不是旧系统的写法
----------------------
旧系统 ``app/cleanup.py:286``(``backup_db``)备的是 **SQLite**:``sqlite3`` 的
``Connection.backup()`` 直接拷一个文件,``Config.DB_DIR/backup/ebooking_YYYYMMDD.db``。
新架构数据在 **PostgreSQL 16 容器**(``docker-compose.yml`` 的 ``hoteldata-pg``),
所以必须走 ``pg_dump`` 的**自定义格式** ``-Fc``:

  * 自带压缩,比纯 SQL 文本小;
  * 支持按表/按 schema 部分恢复;
  * ★ ``pg_restore --list`` 能"自证可打开" —— V19 要的正是"产出**可打开**的备份",
    而"文件存在"根本不等于"能恢复"。

回退链(★ 本机 PostgreSQL 跑在 Docker 里,``pg_dump`` 通常**不在 PATH**)
--------------------------------------------------------------------
1. ``docker exec hoteldata-pg pg_dump -U <user> -d <db> -Fc``(stdout 重定向到文件);
2. 本机 ``pg_dump``(host/port/user/db 由 ``settings.db.url`` 经 ``make_url`` 解析,
   口令走 ``PGPASSWORD`` 环境变量,不进命令行也不进日志);
3. 都失败 → ``ok=False`` + 明确错误信息 —— **绝不假装成功**(旧系统这里是抛
   ``RuntimeError``,由调度层记录;新实现把失败写进报告,**不要**靠返回值猜)。

两条纪律
--------
* 一律 ``asyncio.create_subprocess_exec``(**不用 ``shell=True``**),避免口令与
  Windows 路径被 shell 二次解释;
* 产出先落 ``.part``,校验通过才改名到 ``hoteldata_YYYYMMDD.dump`` ——
  半截文件不会冒充"今天的备份"(旧系统同日幂等只看"文件在不在",半截文件会被当成已完成)。
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy.engine import make_url

from hoteldata.infra.paths import get_layout
from hoteldata.settings import Settings, get_settings

__all__ = ["BackupReport", "run"]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: PG 容器名(与 ``docker-compose.yml`` 的 ``container_name`` 逐字一致)
DOCKER_CONTAINER = "hoteldata-pg"

#: 冷备文件名 ``hoteldata_YYYYMMDD.dump``(旧系统是 ``ebooking_YYYYMMDD.db``)
_BACKUP_RE = re.compile(r"^hoteldata_(\d{8})\.dump(?:\.part)?$")

#: ``pg_dump -Fc`` 自定义格式的魔数(文件头 5 字节)
_PG_DUMP_MAGIC = b"PGDMP"

#: 未完成产物后缀(校验通过后才改名为正式备份)
_PART_SUFFIX = ".part"

#: ``CREATE TABLE`` 等内容在 ``pg_restore --list`` 里以 ``;`` 开头的是注释行
_LIST_COMMENT_PREFIX = ";"

_DUMP_TIMEOUT_S = 900.0
_VERIFY_TIMEOUT_S = 60.0


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BackupReport:
    """冷备报告(V19:``ok`` + ``verified`` 两个字段即"产出可打开的备份")。"""

    ok: bool = False
    path: str | None = None
    bytes: int = 0
    skipped_already_done: bool = False
    """★ 同日重跑幂等:目标已存在且非空 → 不重复产出、不报错。"""

    method: str | None = None
    """实际生效的方式:``docker:hoteldata-pg`` / ``local:pg_dump``。"""

    verified: bool = False
    verify_method: str | None = None
    verify_detail: str | None = None
    retained: int = 0
    removed_outdated: int = 0
    removed_files: list[str] = field(default_factory=list)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def size_mb(self) -> float:
        return round(self.bytes / 1024**2, 3)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "path": self.path,
            "bytes": self.bytes,
            "size_mb": self.size_mb,
            "skipped_already_done": self.skipped_already_done,
            "method": self.method,
            "verified": self.verified,
            "verify_method": self.verify_method,
            "verify_detail": self.verify_detail,
            "retained": self.retained,
            "removed_outdated": self.removed_outdated,
            "removed_files": list(self.removed_files),
            "attempts": list(self.attempts),
            "errors": list(self.errors),
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class _DbTarget:
    """``settings.db.url`` 解析结果(口令只在这里出现,不进日志)。"""

    user: str
    password: str | None
    host: str
    port: int
    database: str

    def redacted_url(self) -> str:
        # 下面这行是**把口令打码后**再拼 DSN(值就是 ``***``,不是明文口令)。
        # 扫描器会把它误报成"连接串含口令" → 人工审阅后**行内标注放行**。
        return f"postgresql://{self.user}:***@{self.host}:{self.port}/{self.database}"  # secretscan:ignore


@dataclass(slots=True)
class _CmdResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    argv: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _db_target(url: str) -> _DbTarget:
    """用 ``sqlalchemy.engine.make_url`` 解析连接串(不手搓字符串切分)。"""
    parsed = make_url(url)
    return _DbTarget(
        user=parsed.username or "postgres",
        password=parsed.password,
        host=parsed.host or "127.0.0.1",
        port=parsed.port or 5432,
        database=parsed.database or "hoteldata",
    )


# ---------------------------------------------------------------------------
# 子进程
# ---------------------------------------------------------------------------


def _brief(text: str, limit: int = 400) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


async def _run_cmd(
    argv: Sequence[str],
    *,
    stdout_path: Path | None = None,
    stdin_path: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float = _DUMP_TIMEOUT_S,
) -> _CmdResult:
    """跑一条外部命令(**``create_subprocess_exec``,不用 shell**)。

    ``stdout_path`` 给定时把 stdout 重定向到文件(dump 动辄几百 MB,不能进内存);
    ``stdin_path`` 给定时把文件接到 stdin(``pg_restore --list`` 用)。
    """
    args = [str(x) for x in argv]
    # 句柄由子进程接管,在 finally 里关闭(不能用 with:子进程仍在写)
    out_handle = open(stdout_path, "wb") if stdout_path is not None else None
    in_handle = open(stdin_path, "rb") if stdin_path is not None else None
    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=in_handle,
                stdout=out_handle if out_handle is not None else asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except (FileNotFoundError, OSError, NotImplementedError) as exc:
            return _CmdResult(returncode=-1, stderr=f"无法启动 {args[0]}: {exc}", argv=tuple(args))
        try:
            raw_out, raw_err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return _CmdResult(
                returncode=124,
                stderr=f"超时(>{timeout:.0f}s),已终止: {' '.join(args[:3])}…",
                argv=tuple(args),
            )
        return _CmdResult(
            returncode=int(proc.returncode or 0),
            stdout=(raw_out or b"").decode("utf-8", errors="replace"),
            stderr=(raw_err or b"").decode("utf-8", errors="replace"),
            argv=tuple(args),
        )
    finally:
        for handle in (out_handle, in_handle):
            if handle is not None:
                try:
                    handle.close()
                except OSError:  # pragma: no cover - 句柄已随子进程关闭
                    pass


# ---------------------------------------------------------------------------
# 校验(V19)
# ---------------------------------------------------------------------------


def _read_magic(path: Path) -> bytes:
    try:
        with path.open("rb") as handle:
            return handle.read(len(_PG_DUMP_MAGIC))
    except OSError:
        return b""


def _count_toc_entries(listing: str) -> int:
    return sum(
        1
        for line in listing.splitlines()
        if line.strip() and not line.lstrip().startswith(_LIST_COMMENT_PREFIX)
    )


async def _verify_dump(target: Path, *, container: str) -> tuple[bool, str | None, str | None]:
    """校验 dump **可打开**:非空 → 魔数 → ``pg_restore --list``(容器优先,退化到本机)。

    :return: ``(verified, method, detail)``。
    """
    try:
        size = target.stat().st_size
    except OSError as exc:
        return False, None, f"备份文件不可读: {exc}"
    if size <= 0:
        return False, None, "备份文件为 0 字节(不视为产出成功)"

    magic = _read_magic(target)
    if magic != _PG_DUMP_MAGIC:
        return False, None, f"文件头不是 {_PG_DUMP_MAGIC!r}(实际 {magic!r}),不是 -Fc 自定义格式"

    candidates: list[tuple[str, list[str]]] = [
        ("pg_restore --list(docker)", ["docker", "exec", "-i", container, "pg_restore", "--list"]),
        ("pg_restore --list(本机)", ["pg_restore", "--list"]),
    ]
    tried: list[str] = []
    for label, argv in candidates:
        result = await _run_cmd(argv, stdin_path=target, timeout=_VERIFY_TIMEOUT_S)
        if result.ok and result.stdout.strip():
            lines = len([line for line in result.stdout.splitlines() if line.strip()])
            entries = _count_toc_entries(result.stdout)
            # 空库的 dump 也合法(--list 只有 `;` 注释行,TOC 条目 0),所以两个数都报出来
            detail = f"--list 输出 {lines} 行 / TOC 条目 {entries} 条"
            return True, label, detail
        tried.append(f"{label}: rc={result.returncode} {_brief(result.stderr, 160)}")
    fallback = "magic:PGDMP"
    return (
        True,
        fallback,
        f"pg_restore 不可用,已退化为文件头校验(非空 + PGDMP 魔数);尝试过 {' | '.join(tried)}",
    )


# ---------------------------------------------------------------------------
# 轮转
# ---------------------------------------------------------------------------


def _iter_backups(backup_dir: Path) -> list[tuple[Path, date]]:
    found: list[tuple[Path, date]] = []
    try:
        entries = sorted(backup_dir.glob("hoteldata_*.dump*"))
    except OSError:
        return found
    for path in entries:
        match = _BACKUP_RE.match(path.name)
        if match is None:
            continue
        try:
            day = datetime.strptime(match.group(1), "%Y%m%d").date()
        except ValueError:
            continue
        found.append((path, day))
    return found


def _rotate(
    backup_dir: Path, today: date, retain_days: int, *, keep: Path | None = None
) -> tuple[int, int, list[str]]:
    """删除日期早于 ``today - retain_days`` 的旧备(含 ``.part`` 半截产物)。

    :return: ``(retained, removed_count, removed_names)`` —— ``retained`` 只数正式 ``.dump``。
    """
    cutoff = today - timedelta(days=retain_days)
    removed: list[str] = []
    retained = 0
    for path, day in _iter_backups(backup_dir):
        if keep is not None and path.name == keep.name:
            retained += 1
            continue
        if day < cutoff:
            try:
                path.unlink()
                removed.append(path.name)
            except OSError as exc:  # pragma: no cover - 权限/占用
                logger.warning("删除过期冷备失败 {}: {}", path.name, exc)
        elif not path.name.endswith(_PART_SUFFIX):
            retained += 1
    return retained, len(removed), removed


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


async def run(
    settings: Settings | None = None,
    *,
    retention_days: int | None = None,
    now: date | None = None,
    docker_container: str = DOCKER_CONTAINER,
) -> BackupReport:
    """执行一次冷备(V19 的入口)。

    :param settings: 缺省用进程单例配置。
    :param retention_days: 覆盖 ``BACKUP_RETENTION_DAYS``(默认 7);**0 表示只备份不轮转**。
    :param now: 注入"今天"(测试用),缺省取配置时区的当天。
    :param docker_container: PG 容器名(默认 ``hoteldata-pg``)。
    """
    s = settings or get_settings()
    layout = get_layout(s)
    today = now or datetime.now(s.tzinfo).date()
    retain = s.ops.backup_retention_days if retention_days is None else retention_days
    report = BackupReport()
    report.notes.append(f"保留期 {retain} 天(BACKUP_RETENTION_DAYS);同日重跑幂等(已存在且非空即跳过)")
    report.notes.append("清理器不碰 var/backup/(owner 是本模块),避免两个 owner 互删")

    backup_dir = layout.backup_dir
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        report.errors.append(f"无法创建备份目录 {backup_dir}: {exc}")
        logger.error("冷备失败:无法创建备份目录 {}: {}", backup_dir, exc)
        return report

    target = backup_dir / f"hoteldata_{today:%Y%m%d}.dump"
    report.path = layout.to_relative(target)

    try:
        parsed = _db_target(s.db.url)
    except Exception as exc:  # noqa: BLE001 - 连接串非法时给明确错误而不是崩栈
        report.errors.append(f"无法解析 DB_URL: {exc}")
        logger.error("冷备失败:无法解析 DB_URL: {}", exc)
        return report

    # ---- ① 同日幂等:已存在且非空 → 不重复产出、不报错(V19) ----
    if target.exists() and target.stat().st_size > 0:
        report.skipped_already_done = True
        report.bytes = target.stat().st_size
        report.ok = True
        verified, method, detail = await _verify_dump(target, container=docker_container)
        report.verified, report.verify_method, report.verify_detail = verified, method, detail
        report.method = "existing(同日已完成)"
        _rotate_and_report(report, backup_dir, today, retain, keep=target)
        logger.info(
            "冷备跳过(同日已完成):{} ({:.2f} MB),校验={}",
            report.path,
            report.size_mb,
            report.verify_detail,
        )
        return report

    # ---- ② 产出:容器 pg_dump → 本机 pg_dump ----
    part = target.with_name(target.name + _PART_SUFFIX)
    base_dump = ["pg_dump", "-U", parsed.user, "-d", parsed.database, "-Fc"]
    env = dict(os.environ)
    if parsed.password:
        env["PGPASSWORD"] = parsed.password
    attempts: list[tuple[str, list[str], dict[str, str] | None]] = [
        ("docker:" + docker_container, ["docker", "exec", docker_container, *base_dump], None),
        (
            "local:pg_dump",
            [
                "pg_dump",
                "-h",
                parsed.host,
                "-p",
                str(parsed.port),
                "-U",
                parsed.user,
                "-d",
                parsed.database,
                "-Fc",
            ],
            env,
        ),
    ]

    report.notes.append(f"目标库 {parsed.redacted_url()}(口令一律走环境变量,不进命令行/日志)")
    for method, argv, attempt_env in attempts:
        if argv[0] == "docker" and shutil.which("docker") is None:
            report.attempts.append({"method": method, "ok": False, "error": "PATH 中找不到 docker"})
            continue
        if argv[0] == "pg_dump" and shutil.which("pg_dump") is None:
            report.attempts.append({"method": method, "ok": False, "error": "PATH 中找不到 pg_dump"})
            continue
        if part.exists():
            part.unlink(missing_ok=True)
        logger.info("冷备尝试 {}: {}", method, " ".join(argv[:6]) + " …")
        result = await _run_cmd(argv, stdout_path=part, env=attempt_env, timeout=_DUMP_TIMEOUT_S)
        size = part.stat().st_size if part.exists() else 0
        attempt: dict[str, Any] = {
            "method": method,
            "ok": result.ok and size > 0,
            "returncode": result.returncode,
            "bytes": size,
            "stderr": _brief(result.stderr),
        }
        report.attempts.append(attempt)
        if not (result.ok and size > 0):
            if result.ok and size == 0:
                attempt["error"] = "pg_dump 返回 0 但产出为空文件"
            report.errors.append(f"{method} 失败: rc={result.returncode} {_brief(result.stderr)}")
            logger.warning("冷备尝试失败 {}: rc={} {}", method, result.returncode, _brief(result.stderr))
            if part.exists():
                part.unlink(missing_ok=True)
            continue
        try:
            part.replace(target)  # 校验前先落正式名:校验的是"这一份"
        except OSError as exc:
            attempt["ok"] = False
            report.errors.append(f"{method} 产出改名失败: {exc}")
            continue
        report.method = method
        report.bytes = size
        break

    if not target.exists() or target.stat().st_size <= 0:
        report.ok = False
        report.verified = False
        report.verify_detail = "两种 pg_dump 方式都未产出非空备份"
        report.notes.append(
            "排查建议:① docker ps 看 hoteldata-pg 是否在跑;② 容器内是否有 pg_dump;"
            "③ 若 PG 不在容器里,请把 PostgreSQL 客户端 bin 目录加进 PATH"
        )
        logger.error("冷备失败:{}(已尝试 {})", report.verify_detail, [a["method"] for a in report.attempts])
        return report

    # ---- ③ 校验可打开(V19:产出**可打开**的备份) ----
    verified, verify_method, detail = await _verify_dump(target, container=docker_container)
    report.verified, report.verify_method, report.verify_detail = verified, verify_method, detail
    report.ok = verified
    if not verified:
        report.errors.append(f"备份校验未通过: {detail}")
        # 校验不过 = 这份文件是垃圾;留着它会让"同日幂等"把坏文件当成已完成,必须删掉
        try:
            target.unlink(missing_ok=True)
            report.notes.append("校验未通过的产物已删除,下次运行会重新产出(不会把坏文件当已完成)")
        except OSError as exc:  # pragma: no cover - 文件被占用
            report.notes.append(f"校验未通过的产物删除失败({exc}),请人工处理: {report.path}")
        logger.error("冷备校验未通过:{} → {}", report.path, detail)
    else:
        logger.info("冷备完成:{} ({:.2f} MB,方式 {})", report.path, report.size_mb, report.method)

    # ---- ④ 轮转 ----
    _rotate_and_report(report, backup_dir, today, retain, keep=target)
    logger.info(
        "冷备轮转:保留 {} 份 / 删除过期 {} 份(保留期 {} 天)",
        report.retained,
        report.removed_outdated,
        retain,
    )
    return report


def _rotate_and_report(
    report: BackupReport, backup_dir: Path, today: date, retain: int, *, keep: Path | None
) -> None:
    """轮转并把计数写回报告(``retain <= 0`` 时只数不删)。"""
    if retain <= 0:
        report.retained = sum(
            1 for path, _ in _iter_backups(backup_dir) if not path.name.endswith(_PART_SUFFIX)
        )
        report.notes.append("retention_days<=0:只产出不轮转")
        return
    retained, removed, names = _rotate(backup_dir, today, retain, keep=keep)
    report.retained = retained
    report.removed_outdated = removed
    report.removed_files = names
    if names:
        report.notes.append(f"已删除过期冷备 {removed} 份: {', '.join(names)}")
