"""T6.5 磁盘清理 —— **双保留期 + 禁止目录白名单 + ``--dry-run``**(B25 / 旧 ``cleanup.py:36,160``)。

为什么要重写(旧系统的两个危险点)
----------------------------------
1. 🔴 **旧系统没有 dry-run**:``app/cleanup.py:117`` 的 ``cleanup_data(dry_run=False)``
   是默认值,一条命令下去就是 ``shutil.rmtree``,删错只能等备份救。
   本实现把 ``dry_run`` 作为 :func:`run` 的**默认参数**。
2. 🔴 **旧白名单是"按目录名匹配"**(``_is_forbidden(name)``,``cleanup.py:71``):
   只要某一层目录名命中 ``db`` / ``config`` / ``logs`` / ``knowledge`` /
   ``storage_states`` 就整棵跳过 —— 它是**字符串相等**,既误伤同名业务目录,
   也无法表达 ``var/states`` 这种"只在特定位置才禁止"的语义。
   本实现改成**结构性判定**:删除前把目标 ``resolve()`` 后与白名单**根**逐项
   ``Path.is_relative_to`` 比对(见 :func:`_is_forbidden`);因为比的是解析后的
   祖先关系,``var/states_backup`` **不会**被误判成 ``var/states`` 的子路径。

双保留期(B25 / 旧 ``cleanup.py:160`` 的 ``_handle_date_dir``)
-----------------------------------------------------------
======================================  =================================================
对象                                     规则
======================================  =================================================
``var/raw/<hotel>/<YYYYMMDD>/``          日期 < ``now-90`` → **整目录删**;
                                         日期 ∈ ``[now-90, now-30)`` → **只删 raw/html 内容,保留截图**;
                                         更近 → 不动
``var/screenshots/<hotel>/<YYYYMMDD>/``  同上(截图自己活到 90 天,故该窗口内它整目录保留)
``var/reports/**``                       文件 mtime > 30 天 → 删
``var/`` 平铺 html/json                  mtime > 30 天 → 删(``(勿删)`` 受保护)
``var/raw`` / ``var/screenshots`` 散落     mtime 超各自保留期 → 删(日期目录内的不重复处理)
``var/logs/``                            **不碰** —— 由 loguru ``retention="30 days"`` 自己轮转
``var/backup/``                          **不碰** —— 由 :mod:`hoteldata.domains.ops.backup` 轮转
======================================  =================================================

> 截图的散落文件按 ``screenshot_retention_days``(**90**)而非 30 天判定:
> 截图保留期短于数据保留期会与"90 天整目录线"自相矛盾,
> ``settings.py`` 的交叉校验已强制 ``screenshot_retention >= data_retention``。

三道闸(顺序固定,任一命中即跳过并记日志)
--------------------------------------
① 结构性白名单 ``_FORBIDDEN_DIRS`` + ``var/`` 归属校验(清理器只动 ``var/``);
② 文件名含 ``(勿删)``(含目录内含该标记的后代 —— 不整删别人标了"勿删"的目录);
③ 保留期判定。

``--all`` 语义:忽略保留期,清理全部**可清理项** —— 但它**照样服从白名单**,
且仍不碰"今天"的产物(截止线取今日 00:00),避免删掉正在写入的文件。
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import Any

from loguru import logger

from hoteldata.infra.paths import Layout, get_layout
from hoteldata.settings import Settings, get_settings

__all__ = ["CleanupItem", "CleanupReport", "run"]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: ★ 禁止目录白名单(B25 / 旧 ``cleanup.py:36`` 的 ``_FORBIDDEN_DIRS``)。
#:
#: 元素是**相对项目根的路径**(不是目录名):判定用 ``Path.is_relative_to``,
#: 因此 ``var/states_backup`` 不会被误判为 ``var/states`` 命中。
_FORBIDDEN_DIRS: tuple[str, ...] = (
    "config",
    "db",
    "logs",
    "knowledge",
    "storage_states",
    "var/states",
)

#: 别人家的目录:不禁止,但**另一个 owner 在管**,清理器一律不碰。
#:   * ``var/logs``  —— loguru ``retention="30 days"``(``logging.py:105``)
#:   * ``var/backup`` —— :mod:`hoteldata.domains.ops.backup` 按 7 天轮转
_OTHER_OWNER_DIRS: tuple[str, ...] = ("var/logs", "var/backup")

#: ``var/`` 平铺可清理扩展名(旧 ``cleanup.py:47`` 的 ``_FLAT_EXTENSIONS``)
_FLAT_SUFFIXES: frozenset[str] = frozenset({".html", ".htm", ".json"})

#: 截图扩展名(日期目录里"必须留下"的东西)
_IMAGE_SUFFIXES: frozenset[str] = frozenset({".jpg", ".jpeg", ".png", ".webp", ".bmp"})

#: 日期目录内的截图子目录名(兼容旧布局 ``<date>/screenshots/``)
_SCREENSHOT_DIR_NAMES: frozenset[str] = frozenset({"screenshots", "screenshot"})

#: 误伤保护标记(旧 ``cleanup.py:49`` 的 ``_DONT_DELETE_MARK``;顺带认全角括号)
_DONT_DELETE_MARKS: tuple[str, ...] = ("(勿删)", "（勿删）")
_DONT_DELETE_MARK = _DONT_DELETE_MARKS[0]

#: ``YYYYMMDD`` 日期目录名
_DATE_DIR_RE = re.compile(r"^\d{8}$")


# ---------------------------------------------------------------------------
# 路径工具
# ---------------------------------------------------------------------------


def _resolve(path: Path) -> Path | None:
    """``resolve()`` 失败(损坏的 junction / 权限)返回 ``None`` —— 由调用方保守跳过。"""
    try:
        return path.resolve()
    except OSError:
        return None


def _dir_size(path: Path) -> int:
    """递归统计目录字节数(单个文件统计失败则忽略,不影响整体)。"""
    total = 0
    try:
        entries = list(path.rglob("*"))
    except OSError:
        return 0
    for item in entries:
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _has_dont_delete(name: str) -> bool:
    """文件名是否带「勿删」标记。"""
    return any(mark in name for mark in _DONT_DELETE_MARKS)


def _is_date_dir_name(name: str) -> bool:
    return bool(_DATE_DIR_RE.match(name))


def _parse_date_dir(name: str) -> date | None:
    try:
        return datetime.strptime(name, "%Y%m%d").date()
    except ValueError:
        return None


def _is_screenshot_entry(path: Path) -> bool:
    """日期目录里"属于截图、必须留下"的条目(截图子目录或图片文件)。"""
    if path.is_dir():
        return path.name.lower() in _SCREENSHOT_DIR_NAMES
    return path.suffix.lower() in _IMAGE_SUFFIXES


def _older_than(path: Path, cutoff: date, tz: tzinfo) -> bool:
    """文件 mtime 早于 ``cutoff`` 当天 00:00(本地时区)。"""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False
    return mtime < datetime.combine(cutoff, time.min, tzinfo=tz).timestamp()


def _forbidden_roots(project_root: Path) -> tuple[Path, ...]:
    """把白名单(相对路径)解析成**绝对根**,判定时逐项 ``is_relative_to``。"""
    roots: list[Path] = []
    for rel in (*_FORBIDDEN_DIRS, *_OTHER_OWNER_DIRS):
        resolved = _resolve(project_root / rel)
        if resolved is not None:
            roots.append(resolved)
    return tuple(roots)


def _is_forbidden(resolved: Path, roots: tuple[Path, ...]) -> bool:
    """结构性白名单判定 —— **不是**字符串前缀匹配。

    逐项 ``resolved == root or resolved.is_relative_to(root)``:
    ``var/states/x.json`` 命中 ``var/states``;``var/states_backup/x.json`` **不命中**
    (它只是名字前缀相同,祖先链里没有 ``var/states``)。
    """
    return any(resolved == root or resolved.is_relative_to(root) for root in roots)


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CleanupItem:
    """一条清理动作:dry-run 下是"计划",实跑下是"结果"。"""

    path: str
    """相对项目根的路径(``Layout.to_relative``;绝对路径换机器即失效)。"""

    kind: str
    """``"dir"`` 或 ``"file"``。"""

    rule: str
    """命中的规则原文(带截止日期,便于人工核对保留期)。"""

    bytes: int = 0
    """该项占用字节数(dry-run 下即为"预计释放")。"""

    deleted: bool = False
    """实跑下是否真的删掉了;dry-run 恒 ``False``(一个字节都不删)。"""

    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "rule": self.rule,
            "bytes": self.bytes,
            "deleted": self.deleted,
            "error": self.error,
        }


@dataclass(slots=True)
class CleanupReport:
    """清理报告(V18 的"列出待删项"就是 ``items``)。

    计数口径(避免"dry-run 到底删了几个"的歧义):
      * ``deleted``     —— **实际**删除项数;dry-run 恒 ``0``;
      * ``would_delete`` —— 本轮的待删项数(dry-run 下即"将要删");
      * ``kept``        —— 保留项数(目录级决策 + 未到期文件);
      * ``skipped_forbidden`` —— 命中白名单/越界而跳过的项数;
      * ``errors``      —— 失败项数;
      * ``freed_mb``    —— **实际**释放(dry-run 恒 0);
      * ``planned_mb``  —— 预计释放(dry-run 下就是要看的那个数)。
    """

    dry_run: bool = True
    all_: bool = False
    data_cutoff: str = ""
    screenshot_cutoff: str = ""
    items: list[CleanupItem] = field(default_factory=list)
    kept_items: list[str] = field(default_factory=list)
    skipped_forbidden: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    files_scanned: int = 0
    files_kept: int = 0
    freed_bytes: int = 0
    planned_bytes: int = 0

    # ---- 计数 ----
    @property
    def deleted(self) -> int:
        return sum(1 for item in self.items if item.deleted)

    @property
    def would_delete(self) -> int:
        return len(self.items)

    @property
    def kept(self) -> int:
        return len(self.kept_items) + self.files_kept

    @property
    def freed_mb(self) -> float:
        return round(self.freed_bytes / 1024**2, 3)

    @property
    def planned_mb(self) -> float:
        return round(self.planned_bytes / 1024**2, 3)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "dry_run": self.dry_run,
            "all": self.all_,
            "data_cutoff": self.data_cutoff,
            "screenshot_cutoff": self.screenshot_cutoff,
            "deleted": self.deleted,
            "would_delete": self.would_delete,
            "kept": self.kept,
            "skipped_forbidden": len(self.skipped_forbidden),
            "errors": len(self.errors),
            "freed_bytes": self.freed_bytes,
            "freed_mb": self.freed_mb,
            "planned_bytes": self.planned_bytes,
            "planned_mb": self.planned_mb,
            "files_scanned": self.files_scanned,
            "items": [item.as_dict() for item in self.items],
            "kept_items": list(self.kept_items),
            "skipped_forbidden_items": list(self.skipped_forbidden),
            "error_items": list(self.errors),
            "notes": list(self.notes),
        }
        if self.dry_run:
            payload["freed_mb_note"] = "dry-run 未删除任何文件;预计释放见 planned_mb"
        return payload


# ---------------------------------------------------------------------------
# 清理器
# ---------------------------------------------------------------------------


class _Cleaner:
    """一次清理的状态机 —— **dry-run 与实跑共用同一套判定**,只有最后一步不同。"""

    def __init__(
        self,
        *,
        layout: Layout,
        project_root: Path,
        var_root: Path,
        tz: tzinfo,
        data_cutoff: date,
        screenshot_cutoff: date,
        dry_run: bool,
        all_: bool,
    ) -> None:
        self.layout = layout
        self.project_root = project_root
        self.var_root = var_root
        self.tz = tz
        self.data_cutoff = data_cutoff
        self.screenshot_cutoff = screenshot_cutoff
        self.dry_run = dry_run
        self.all_ = all_
        self.forbidden = _forbidden_roots(project_root)
        self.report = CleanupReport(
            dry_run=dry_run,
            all_=all_,
            data_cutoff=data_cutoff.isoformat(),
            screenshot_cutoff=screenshot_cutoff.isoformat(),
        )

    # ------------------------------------------------------------------
    # 判定与记账
    # ------------------------------------------------------------------

    def _blocked_by_whitelist(self, resolved: Path) -> str | None:
        """白名单判定**只有这一处**;返回命中原因,未命中返回 ``None``。"""
        for root in self.forbidden:
            if resolved == root or resolved.is_relative_to(root):
                return f"命中禁止目录白名单 {self.layout.to_relative(root)}"
        if not resolved.is_relative_to(self.var_root):
            return "目标不在 var/ 之下(结构性保险:清理器只动 var/)"
        return None

    def _guard(self, path: Path) -> bool:
        """扫描前的前置闸:命中白名单/越界即记账并返回 ``True``(调用方跳过)。"""
        resolved = _resolve(path)
        if resolved is None:
            self.report.errors.append(f"路径无法解析,跳过: {path}")
            logger.warning("清理跳过(无法 resolve): {}", path)
            return True
        reason = self._blocked_by_whitelist(resolved)
        if reason is not None:
            self._forbidden(resolved, reason)
            return True
        return False

    def _forbidden(self, resolved: Path, reason: str) -> None:
        entry = f"{self.layout.to_relative(resolved)}({reason})"
        self.report.skipped_forbidden.append(entry)
        logger.debug("清理跳过(白名单): {} ← {}", entry, reason)

    def _kept(self, path: Path, reason: str) -> None:
        self.report.kept_items.append(f"{self.layout.to_relative(path)}({reason})")

    def _has_protected_descendant(self, directory: Path) -> Path | None:
        """目录内是否有 ``(勿删)`` 标记 —— 有则**不整删**(保护用户显式标记)。"""
        try:
            entries = list(directory.rglob("*"))
        except OSError:
            return None
        for item in entries:
            if _has_dont_delete(item.name):
                return item
        return None

    # ------------------------------------------------------------------
    # 唯一的删除出口
    # ------------------------------------------------------------------

    def remove(self, path: Path, *, is_dir: bool, rule: str) -> None:
        """★ 全部删除动作的唯一出口:三道闸 → 记账 → (非 dry-run 时)真删。"""
        resolved = _resolve(path)
        if resolved is None:
            self.report.errors.append(f"路径无法解析,跳过: {path}")
            return
        # ① 白名单 + var/ 归属
        reason = self._blocked_by_whitelist(resolved)
        if reason is not None:
            self._forbidden(resolved, reason)
            return
        # ② 「勿删」标记(文件自身 + 待整删目录的后代)
        if _has_dont_delete(resolved.name):
            self._kept(resolved, f"{rule} · 文件名含 {_DONT_DELETE_MARK}")
            return
        if is_dir:
            protected = self._has_protected_descendant(resolved)
            if protected is not None:
                self._kept(resolved, f"{rule} · 目录内含「勿删」文件 {protected.name}")
                return
        # ③ 记账 + 删
        size = _dir_size(resolved) if is_dir else _file_size(resolved)
        item = CleanupItem(
            path=self.layout.to_relative(resolved),
            kind="dir" if is_dir else "file",
            rule=rule,
            bytes=size,
        )
        self.report.planned_bytes += size
        if not self.dry_run:
            logger.debug("清理删除 {} (resolved={}) ← {}", item.path, resolved, rule)
            try:
                if is_dir:
                    shutil.rmtree(resolved)
                else:
                    resolved.unlink()
                item.deleted = True
                self.report.freed_bytes += size
            except OSError as exc:
                item.error = str(exc)
                self.report.errors.append(f"{item.path}: {exc}")
                logger.warning("清理失败 {}: {}", item.path, exc)
        else:
            logger.debug("[dry-run] 待删 {} (resolved={}) ← {}", item.path, resolved, rule)
        self.report.items.append(item)

    def _iterdir(self, directory: Path) -> list[Path]:
        try:
            return sorted(directory.iterdir(), key=lambda p: p.name)
        except OSError as exc:
            self.report.errors.append(f"目录读取失败 {directory}: {exc}")
            return []

    def _under_date_dir(self, path: Path, root: Path) -> bool:
        """``path`` 是否位于 ``root`` 下某个 ``YYYYMMDD`` 目录之内(避免重复处理)。"""
        current = path.parent
        while current != root and current != current.parent:
            if _is_date_dir_name(current.name):
                return True
            current = current.parent
        return False

    # ------------------------------------------------------------------
    # 规则 a/b:日期目录(双保留期)
    # ------------------------------------------------------------------

    def _handle_date_dir(self, day_dir: Path, label: str) -> None:
        """双保留期核心(B25 / 旧 ``cleanup.py:160``)。"""
        if self._guard(day_dir):
            return
        parsed = _parse_date_dir(day_dir.name)
        if parsed is None:
            self.report.errors.append(f"日期目录名无法解析,跳过: {day_dir.name}")
            return
        suffix = "(--all:忽略保留期)" if self.all_ else ""
        if parsed < self.screenshot_cutoff:
            self.remove(
                day_dir,
                is_dir=True,
                rule=(
                    f"{label}:日期 {parsed:%Y-%m-%d} < 截图线 {self.screenshot_cutoff}"
                    f" → 整目录删(数据与截图都到期){suffix}"
                ),
            )
            return
        if parsed < self.data_cutoff:
            rule = (
                f"{label}:日期 {parsed:%Y-%m-%d} ∈ [截图线 {self.screenshot_cutoff}, "
                f"数据线 {self.data_cutoff}) → 只删 raw/html 内容,保留截图"
            )
            self._sweep_date_dir_data(day_dir, rule)
            return
        self._kept(day_dir, f"{label}:日期 {parsed:%Y-%m-%d} 仍在保留期内")

    def _sweep_date_dir_data(self, day_dir: Path, rule: str) -> None:
        """``[now-90, now-30)`` 窗口:删数据内容,留截图。"""
        for child in self._iterdir(day_dir):
            if _is_screenshot_entry(child):
                self._kept(child, f"{rule} · 截图按 SCREENSHOT_RETENTION_DAYS 保留")
                continue
            if self._guard(child):
                continue
            self.remove(child, is_dir=child.is_dir(), rule=rule)

    def _sweep_date_dirs(self, parent: Path, label: str) -> None:
        """扫 ``<parent>/<hotel>/<YYYYMMDD>``(也兼容日期目录直接挂在下面)。"""
        if not parent.is_dir():
            return
        for hotel_dir in self._iterdir(parent):
            if not hotel_dir.is_dir():
                continue
            if self._guard(hotel_dir):
                continue
            if _is_date_dir_name(hotel_dir.name):
                self._handle_date_dir(hotel_dir, label)
                continue
            for day_dir in self._iterdir(hotel_dir):
                if not day_dir.is_dir() or not _is_date_dir_name(day_dir.name):
                    continue
                self._handle_date_dir(day_dir, label)

    # ------------------------------------------------------------------
    # 规则 c/d:按 mtime 的平铺与散落文件
    # ------------------------------------------------------------------

    def _sweep_by_mtime(
        self,
        root: Path,
        cutoff: date,
        suffixes: frozenset[str] | None,
        rule: str,
        *,
        skip_date_dir_descendants: bool = False,
    ) -> None:
        """递归删 ``root`` 下 mtime 早于 ``cutoff`` 的文件(``suffixes=None`` 表示不限扩展名)。"""
        if not root.is_dir():
            return
        try:
            candidates = list(root.rglob("*"))
        except OSError as exc:
            self.report.errors.append(f"目录遍历失败 {root}: {exc}")
            return
        for path in sorted(candidates):
            try:
                if not path.is_file():
                    continue
            except OSError:
                continue
            if suffixes is not None and path.suffix.lower() not in suffixes:
                continue
            if skip_date_dir_descendants and self._under_date_dir(path, root):
                continue
            if self._guard(path):
                continue
            self.report.files_scanned += 1
            if not _older_than(path, cutoff, self.tz):
                self.report.files_kept += 1
                continue
            self.remove(path, is_dir=False, rule=f"{rule} → mtime < {cutoff}")

    def _sweep_flat_files(self, root: Path, suffixes: frozenset[str], rule: str) -> None:
        """只扫 ``root`` **第一层**的平铺文件(旧 ``cleanup.py:202`` 的规则 d)。"""
        if not root.is_dir():
            return
        for path in self._iterdir(root):
            if not path.is_file() or path.suffix.lower() not in suffixes:
                continue
            if self._guard(path):
                continue
            self.report.files_scanned += 1
            if not _older_than(path, self.data_cutoff, self.tz):
                self.report.files_kept += 1
                continue
            self.remove(path, is_dir=False, rule=f"{rule} → mtime < {self.data_cutoff}")

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def sweep(self) -> CleanupReport:
        layout = self.layout
        report = self.report

        # ① 日期目录:双保留期的主战场
        self._sweep_date_dirs(layout.raw_dir, "var/raw")
        self._sweep_date_dirs(layout.screenshots_dir, "var/screenshots")

        # ② mtime 口径
        self._sweep_by_mtime(
            layout.reports_dir,
            self.data_cutoff,
            None,
            "var/reports 报告文件超 DATA_RETENTION_DAYS",
        )
        self._sweep_by_mtime(
            layout.raw_dir,
            self.data_cutoff,
            _FLAT_SUFFIXES,
            "var/raw 散落响应文件超 DATA_RETENTION_DAYS",
            skip_date_dir_descendants=True,
        )
        self._sweep_flat_files(
            layout.var_dir,
            _FLAT_SUFFIXES,
            "var/ 平铺 html/json 超 DATA_RETENTION_DAYS",
        )
        self._sweep_by_mtime(
            layout.screenshots_dir,
            self.screenshot_cutoff,
            _IMAGE_SUFFIXES,
            "var/screenshots 散落截图超 SCREENSHOT_RETENTION_DAYS",
            skip_date_dir_descendants=True,
        )

        # ③ 明确"不归我管"的两个目录(结构性白名单已兜底,这里给人工一个交代)
        report.notes.append("var/logs/ 不在清理范围:由 loguru retention='30 days' 自行轮转(总纲 §3.1)")
        report.notes.append(
            "var/backup/ 不在清理范围:由 ops.backup 按 BACKUP_RETENTION_DAYS 轮转(避免两个 owner 互删)"
        )
        report.notes.append(
            f"白名单(结构性,一个字节不动):{' / '.join(_FORBIDDEN_DIRS)}"
            f" + 另一 owner:{' / '.join(_OTHER_OWNER_DIRS)};文件名含 {_DONT_DELETE_MARK} 亦受保护"
        )
        return report


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


async def run(
    settings: Settings | None = None,
    *,
    dry_run: bool = True,
    all_: bool = False,
    now: date | None = None,
) -> CleanupReport:
    """执行一次清理(V18 的入口)。

    :param settings: 缺省用进程单例配置。
    :param dry_run: ★ **默认 True** —— 只列待删项,一个字节都不删(旧系统没有这个开关)。
    :param all_: 忽略保留期,清理全部可清理项(CLI ``--all``);**照样服从白名单**。
    :param now: 注入"今天"(测试用),缺省取配置时区的当天。
    """
    s = settings or get_settings()
    layout = get_layout(s)
    project_root = s.paths.project_root.resolve()
    var_root = s.paths.var_dir.resolve()
    today = now or datetime.now(s.tzinfo).date()

    # ``--all`` 把两条截止线都拉到"今天",于是所有日期目录都落在整删区间
    data_days = 0 if all_ else s.ops.data_retention_days
    shot_days = 0 if all_ else s.ops.screenshot_retention_days
    cleaner = _Cleaner(
        layout=layout,
        project_root=project_root,
        var_root=var_root,
        tz=s.tzinfo,
        data_cutoff=today - timedelta(days=data_days),
        screenshot_cutoff=today - timedelta(days=shot_days),
        dry_run=dry_run,
        all_=all_,
    )
    report = cleaner.report

    if not var_root.is_dir():
        report.notes.append(f"var/ 不存在,未扫描: {var_root}")
        logger.warning("清理跳过:var/ 不存在 {}", var_root)
        return report

    cleaner.sweep()

    logger.info(
        "磁盘清理{}: 待删 {} 项 / 实删 {} 项 / 保留 {} 项 / 白名单跳过 {} 项 / 失败 {} 项 / "
        "预计释放 {:.2f} MB(实释 {:.2f} MB)",
        "(dry-run)" if dry_run else "",
        report.would_delete,
        report.deleted,
        report.kept,
        len(report.skipped_forbidden),
        len(report.errors),
        report.planned_mb,
        report.freed_mb,
    )
    return report
