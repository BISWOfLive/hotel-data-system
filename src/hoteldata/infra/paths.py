"""产物目录布局与相对路径换算 —— A19 遗产。

**布局**(总纲 5.2 / 段1 §5.1)::

    var/
    ├── states/          登录态 <platform>__<role>__<alias>.json
    ├── raw/             原始响应 <hotel>/<YYYYMMDD>/<page>_<module>_<window>_<ts>.json
    ├── screenshots/     模块截图 <hotel>/<YYYYMMDD>/<name>_<key>.jpg
    ├── reports/         报告产物
    ├── backup/          冷备
    └── logs/

**两条纪律**:
  1. **入库存相对路径**(``to_relative``),绝对路径换机器即失效。
  2. 文件名安全化:替换 ``/ \\ : * ? " < > |``(Windows 上中文模块名带这些字符会直接写失败)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from hoteldata.settings import Settings, get_settings

#: Windows 非法文件名字符
_UNSAFE_CHARS = r'[/\\:*?"<>|]'
_UNSAFE_RE = re.compile(_UNSAFE_CHARS)
_MULTI_DASH_RE = re.compile(r"[-_\s]{2,}")

#: Windows 保留设备名(大小写不敏感)
_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

#: 单段文件名长度上限(为扩展名与时间戳留余量)
_MAX_SEGMENT = 80


def safe_name(raw: str, *, fallback: str = "unnamed", max_len: int = _MAX_SEGMENT) -> str:
    """文件名安全化。

    替换非法字符 → 压掉连续分隔符 → 去首尾点与空白 → 处理 Windows 保留名 → 截断。
    """
    if raw is None:
        return fallback
    text = str(raw).strip()
    if not text:
        return fallback
    text = _UNSAFE_RE.sub("-", text)
    # 控制字符
    text = "".join(ch for ch in text if ch.isprintable())
    text = _MULTI_DASH_RE.sub("-", text)
    text = text.strip(" .-")
    if not text:
        return fallback
    if text.split(".")[0].upper() in _RESERVED_NAMES:
        text = f"_{text}"
    if len(text) > max_len:
        text = text[:max_len].rstrip(" .-")
    return text or fallback


def stamp(dt: datetime | None = None) -> str:
    """``YYYYmmdd_HHMMSS`` 时间戳(本地时区)。"""
    dt = dt or datetime.now()
    return dt.strftime("%Y%m%d_%H%M%S")


@dataclass(frozen=True, slots=True)
class Layout:
    """产物目录布局 + 相对路径换算。

    所有入库存的路径都经 :meth:`to_relative`,所有读取都经 :meth:`from_relative`。
    """

    project_root: Path
    var_dir: Path

    # ---- 目录 ----
    @property
    def states_dir(self) -> Path:
        return self.var_dir / "states"

    @property
    def raw_dir(self) -> Path:
        return self.var_dir / "raw"

    @property
    def screenshots_dir(self) -> Path:
        return self.var_dir / "screenshots"

    @property
    def reports_dir(self) -> Path:
        return self.var_dir / "reports"

    @property
    def backup_dir(self) -> Path:
        return self.var_dir / "backup"

    @property
    def logs_dir(self) -> Path:
        return self.var_dir / "logs"

    # ---- 业务路径 ----
    def hotel_day_dir(self, hotel: str, day: date) -> Path:
        """``var/raw/<hotel>/<YYYYMMDD>/``。"""
        return self.raw_dir / safe_name(hotel) / day.strftime("%Y%m%d")

    def screenshot_day_dir(self, hotel: str, day: date) -> Path:
        """``var/screenshots/<hotel>/<YYYYMMDD>/``。"""
        return self.screenshots_dir / safe_name(hotel) / day.strftime("%Y%m%d")

    def raw_json_path(
        self,
        hotel: str,
        day: date,
        page: str,
        module: str,
        window: str,
        *,
        ts: datetime | None = None,
    ) -> Path:
        """``var/raw/<hotel>/<YYYYMMDD>/<page>_<module>_<window>_<ts>.json``。"""
        name = "_".join(safe_name(x, fallback="x", max_len=40) for x in (page, module, window))
        return self.hotel_day_dir(hotel, day) / f"{name}_{stamp(ts)}.json"

    def raw_api_path(
        self,
        hotel: str,
        day: date,
        page: str,
        module: str,
        window: str,
        api_name: str,
        *,
        ts: datetime | None = None,
    ) -> Path:
        """逐接口原始响应落盘(便于以后校准规则)。

        ``var/raw/<hotel>/<YYYYMMDD>/api/<page>_<module>_<window>__<api>_<ts>.json``
        """
        head = "_".join(safe_name(x, fallback="x", max_len=32) for x in (page, module, window))
        return (
            self.hotel_day_dir(hotel, day)
            / "api"
            / f"{head}__{safe_name(api_name, fallback='api', max_len=48)}_{stamp(ts)}.json"
        )

    def screenshot_path(
        self,
        hotel: str,
        day: date,
        container: str,
        key: str,
        *,
        ext: str = "jpg",
    ) -> Path:
        """``var/screenshots/<hotel>/<YYYYMMDD>/<container>_<key>.jpg``。"""
        return self.screenshot_day_dir(hotel, day) / (
            f"{safe_name(container, max_len=40)}_{safe_name(key, max_len=40)}.{ext}"
        )

    def state_path(self, platform: str, role: str, alias: str) -> Path:
        """★ 登录态路径**由三元组推导**,不存 DB(避免第二来源)。"""
        fname = (
            f"{safe_name(platform, max_len=24)}__{safe_name(role, max_len=24)}__"
            f"{safe_name(alias, max_len=48)}.json"
        )
        return self.states_dir / fname

    def state_key_from_path(self, path: Path) -> tuple[str, str, str] | None:
        """``<platform>__<role>__<alias>.json`` → 三元组(反解)。"""
        stem = path.stem
        parts = stem.split("__")
        if len(parts) != 3:
            return None
        return (parts[0], parts[1], parts[2])

    # ---- 相对路径换算 ----
    def to_relative(self, path: Path | str) -> str:
        """绝对路径 → **入库用相对路径**(相对项目根,正斜杠)。"""
        p = Path(path)
        try:
            rel = p.resolve().relative_to(self.project_root.resolve())
        except ValueError:
            # 不在项目根下:保底退回相对 var/
            try:
                rel = Path("var") / p.resolve().relative_to(self.var_dir.resolve())
            except ValueError:
                return p.as_posix()
        return rel.as_posix()

    def from_relative(self, rel: str) -> Path:
        """入库的相对路径 → 绝对路径。"""
        p = Path(rel)
        if p.is_absolute():
            return p
        return (self.project_root / p).resolve()

    def exists_relative(self, rel: str | None) -> bool:
        if not rel:
            return False
        try:
            return self.from_relative(rel).exists()
        except OSError, ValueError:
            return False


def get_layout(settings: Settings | None = None) -> Layout:
    s = settings or get_settings()
    return Layout(project_root=s.paths.project_root, var_dir=s.paths.var_dir)


__all__ = ["Layout", "get_layout", "safe_name", "stamp"]
