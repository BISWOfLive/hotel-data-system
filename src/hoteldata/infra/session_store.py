"""★ 登录态唯一 owner(T2.1)—— 取代旧系统**四种存法、三个位置**。

旧系统的病(总纲 3.4)
---------------------
================  ==========================================================
域                旧存法
================  ==========================================================
携程 eBooking     ``storage_states/<alias>.json`` **或** DB 列 ``accounts.storage_state_path``
美团商家后台       同上
携程前台(比价)    根目录 ``storage_state_ctrip.json``(**绕开账号库**)
美团前台(比价)    根目录 ``storage_state_meituan.json``
旧单账号回退       根目录 ``storage_state.json``
================  ==========================================================

→ **四种存法、三个位置、两个目录层级**。

新架构的答案
------------
**寻址键 ``(platform, role, alias)`` 二元寻址;路径由约定推导,不入库。**

    var/states/<platform>__<role>__<alias>.json

  * **不存 ``state_path``** —— 存了立刻产生"第二来源",两个来源必然漂移;
  * ``sessions`` 表只登记**状态**(``valid`` / ``stale`` / ``invalid`` / ``unknown``)
    + ``last_login_at`` / ``last_check_at``;
  * 旧 ``storage_states/*.json`` **可直接复制过来用,不必重新登录**(这是缓存不是数据迁移),
    见 ``scripts/migrate_states.py``。

> ⚠️ D2/D3/D4 红线:旧仓库把 4 个真实酒店账号 Cookie 与 1,936 个浏览器配置文件
> 提交进了 git。新项目 ``.gitignore`` 已堵死 ``var/states/`` 与 ``.edge-profile/``。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from hoteldata.infra.atomic import atomic_write_json, read_json
from hoteldata.infra.db import Database
from hoteldata.infra.models import Account, Session
from hoteldata.infra.paths import Layout, get_layout
from hoteldata.settings import Settings, get_settings

__all__ = [
    "PLATFORM_CTRIP",
    "PLATFORM_MEITUAN",
    "ROLE_EBOOKING",
    "ROLE_MERCHANT",
    "ROLE_OTA",
    "ROLE_OTA_MEITUAN",
    "SESSION_STATUSES",
    "SessionHandle",
    "SessionKey",
    "SessionStore",
    "platform_for_alias",
]

# ---- 平台 / 角色常量 ----
PLATFORM_CTRIP = "ctrip"
PLATFORM_MEITUAN = "meituan"

#: 携程 eBooking 商家后台
ROLE_EBOOKING = "ebooking"
#: 美团商家后台
ROLE_MERCHANT = "merchant"
#: 携程前台(比价用,段3)
ROLE_OTA = "ota"
#: 美团前台(比价用,段3)
ROLE_OTA_MEITUAN = "ota_meituan"

ROLES = (ROLE_EBOOKING, ROLE_MERCHANT, ROLE_OTA, ROLE_OTA_MEITUAN)

#: 长期会话态(★ 与「单次登录动作结果码」是两个概念,见 models/core.py 的说明)
SESSION_STATUSES = ("valid", "stale", "invalid", "unknown")


class SessionError(RuntimeError):
    """登录态相关错误。"""


def platform_for_alias(alias: str) -> str:
    """按别名前缀路由平台(``ctrip*`` → 携程;``meituan*`` → 美团)。"""
    a = (alias or "").strip().lower()
    if a.startswith("meituan") or a.startswith("mt"):
        return PLATFORM_MEITUAN
    if a.startswith("ctrip") or a.startswith("ct"):
        return PLATFORM_CTRIP
    raise SessionError(f"无法从别名 {alias!r} 推断平台:请以 ctrip* 或 meituan* 开头")


@dataclass(frozen=True, slots=True)
class SessionKey:
    """寻址键 —— 全系统唯一。"""

    platform: str
    role: str
    alias: str

    @property
    def pair(self) -> tuple[str, str, str]:
        return (self.platform, self.role, self.alias)

    def __str__(self) -> str:  # pragma: no cover - 展示用
        return f"{self.platform}/{self.role}/{self.alias}"


class SessionHandle:
    """单个登录态的引用(实现 :class:`~hoteldata.domains.collect.contract.SessionRef`)。

    **只暴露能力,不暴露实现**:域层拿到它只能取 cookie 头 / 取文件路径 / 保存,
    看不到存储细节。
    """

    __slots__ = ("_store", "_key", "_layout")

    def __init__(self, store: SessionStore, key: SessionKey, layout: Layout) -> None:
        self._store = store
        self._key = key
        self._layout = layout

    # ---- SessionRef 协议 ----

    @property
    def platform(self) -> str:
        return self._key.platform

    @property
    def role(self) -> str:
        return self._key.role

    @property
    def alias(self) -> str:
        return self._key.alias

    @property
    def key(self) -> SessionKey:
        return self._key

    def storage_state_path(self) -> Path:
        """``var/states/<platform>__<role>__<alias>.json``。"""
        return self._layout.state_path(self._key.platform, self._key.role, self._key.alias)

    def exists(self) -> bool:
        return self.storage_state_path().exists()

    # ---- 读写 ----

    def load(self) -> dict[str, Any]:
        """读 storage_state;不存在/损坏 → 抛 :class:`SessionError`。"""
        path = self.storage_state_path()
        if not path.exists():
            raise SessionError(f"登录态文件不存在: {path}")
        data = read_json(path)
        if not isinstance(data, dict):
            raise SessionError(f"登录态文件解析失败: {path}")
        return data

    def cookies(self) -> list[dict[str, Any]]:
        data = self.load()
        cookies = data.get("cookies") or []
        if not isinstance(cookies, list):
            return []
        return [c for c in cookies if isinstance(c, dict)]

    def cookie_header(self) -> str:
        """``k1=v1; k2=v2``(**分号 + 一个空格**,逐字继承)。"""
        parts = [f"{c['name']}={c.get('value', '')}" for c in self.cookies() if c.get("name") is not None]
        if not parts:
            raise SessionError(f"登录态文件中无 cookies: {self.storage_state_path()}")
        return "; ".join(parts)

    def cookie_dict(self) -> dict[str, str]:
        return {str(c["name"]): str(c.get("value", "")) for c in self.cookies() if c.get("name") is not None}

    def save(self, storage_state: dict[str, Any]) -> Path:
        """★ 原子写(``r+b`` 纪律,见 :mod:`hoteldata.infra.atomic`)。"""
        path = self.storage_state_path()
        atomic_write_json(path, storage_state)
        return path

    def mtime(self) -> float | None:
        path = self.storage_state_path()
        return path.stat().st_mtime if path.exists() else None

    def age_days(self, now: datetime | None = None) -> float | None:
        """登录态文件的"年龄"(天)—— 主动续登阈值用。"""
        mt = self.mtime()
        if mt is None:
            return None
        now = now or datetime.now()
        return (now.timestamp() - mt) / 86400.0

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<SessionHandle {self._key} exists={self.exists()}>"


class SessionStore:
    """登录态的唯一 owner。"""

    def __init__(
        self,
        settings: Settings | None = None,
        db: Database | None = None,
        layout: Layout | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.db = db
        self.layout = layout or get_layout(self.settings)

    # ------------------------------------------------------------------
    # 寻址
    # ------------------------------------------------------------------

    def handle(
        self,
        platform: str,
        role: str = ROLE_EBOOKING,
        alias: str | None = None,
    ) -> SessionHandle:
        """取登录态引用。``alias`` 缺省说明调用方只想按 ``(platform, role)`` 定位。

        ★ 段1 全部走 ``(platform, role, alias)`` 三元组(段3 的比价登录态亦然)。
        """
        if alias is None:
            alias = self.default_alias(platform, role)
        return SessionHandle(self, SessionKey(platform, role, alias), self.layout)

    def handle_for_alias(self, alias: str, role: str = ROLE_EBOOKING) -> SessionHandle:
        """按别名自动路由平台(``ctrip*`` / ``meituan*``)。"""
        return self.handle(platform_for_alias(alias), role, alias)

    def default_alias(self, platform: str, role: str = ROLE_EBOOKING) -> str:
        """无别名时选第一个可用登录态(按文件 mtime 最新)。"""
        found = [k for k in self.scan_files() if k.platform == platform and k.role == role]
        if not found:
            raise SessionError(
                f"没有可用的 {platform}/{role} 登录态;请先 hoteldata login 或运行 "
                "scripts/migrate_states.py 迁移旧登录态"
            )
        found.sort(key=lambda k: self.handle(k.platform, k.role, k.alias).mtime() or 0, reverse=True)
        return found[0].alias

    def scan_files(self) -> list[SessionKey]:
        """扫描 ``var/states/`` 下全部登录态文件(反解三元组)。"""
        out: list[SessionKey] = []
        state_dir: Path = self.layout.states_dir
        if not state_dir.exists():
            return out
        for path in sorted(state_dir.glob("*.json")):
            key = self.layout.state_key_from_path(path)
            if key is None:
                continue
            out.append(SessionKey(*key))
        return out

    def all_handles(self) -> list[SessionHandle]:
        return [self.handle(k.platform, k.role, k.alias) for k in self.scan_files()]

    # ------------------------------------------------------------------
    # sessions 表
    # ------------------------------------------------------------------

    async def ensure_row(self, key: SessionKey) -> None:
        """幂等登记(文件存在时状态给 ``unknown``,由探测更新)。"""
        if self.db is None:
            return
        stmt = (
            pg_insert(Session)
            .values(
                platform=key.platform,
                role=key.role,
                alias=key.alias,
                status="unknown",
            )
            .on_conflict_do_nothing(index_elements=["platform", "role", "alias"])
        )
        async with self.db.session() as s:
            await s.execute(stmt)

    async def sync_files(self) -> int:
        """把文件系统上的登录态**登记进表**(幂等);返回登记/更新条数。"""
        if self.db is None:
            return 0
        n = 0
        for key in self.scan_files():
            handle = self.handle(key.platform, key.role, key.alias)
            await self.set_status(key, None, file_mtime=handle.mtime())
            n += 1
        return n

    async def set_status(
        self,
        key: SessionKey,
        status: str | None,
        *,
        last_login_at: datetime | None = None,
        last_check_at: datetime | None = None,
        file_mtime: float | None = None,
    ) -> None:
        """写状态(``status=None`` 表示只登记/更新时间,不改状态)。"""
        if self.db is None:
            return
        if status is not None and status not in SESSION_STATUSES:
            raise SessionError(f"非法会话状态 {status!r};合法值 {SESSION_STATUSES}")
        login_at = last_login_at
        if login_at is None and file_mtime is not None:
            login_at = datetime.fromtimestamp(file_mtime, tz=UTC)
        values: dict[str, Any] = {
            "platform": key.platform,
            "role": key.role,
            "alias": key.alias,
            "status": status or "unknown",
            "last_login_at": login_at,
            "last_check_at": last_check_at,
            "updated_at": datetime.now(UTC),
        }
        stmt = pg_insert(Session).values(**values)
        update_cols: dict[str, Any] = {"updated_at": stmt.excluded.updated_at}
        if status is not None:
            update_cols["status"] = stmt.excluded.status
        if login_at is not None:
            update_cols["last_login_at"] = stmt.excluded.last_login_at
        if last_check_at is not None:
            update_cols["last_check_at"] = stmt.excluded.last_check_at
        stmt = stmt.on_conflict_do_update(index_elements=["platform", "role", "alias"], set_=update_cols)
        async with self.db.session() as s:
            await s.execute(stmt)

    async def list_sessions(self) -> list[Session]:
        if self.db is None:
            return []
        async with self.db.session() as s:
            rows = (
                (await s.execute(select(Session).order_by(Session.platform, Session.role, Session.alias)))
                .scalars()
                .all()
            )
            return list(rows)

    async def get_status(self, key: SessionKey) -> str | None:
        if self.db is None:
            return None
        async with self.db.session() as s:
            row = (
                await s.execute(
                    select(Session).where(
                        Session.platform == key.platform,
                        Session.role == key.role,
                        Session.alias == key.alias,
                    )
                )
            ).scalar_one_or_none()
        return row.status if row else None

    # ------------------------------------------------------------------
    # 过期判定
    # ------------------------------------------------------------------

    def need_renewal(self, handle: SessionHandle, now: datetime | None = None) -> bool:
        """距上次登录 > ``LOGIN_MAX_AGE_DAYS``(默认 **20 天**)→ 主动续登(B13 遗产)。"""
        if not self.settings.login.renew_enabled:
            return False
        age = handle.age_days(now)
        if age is None:
            return False
        return age > self.settings.login.max_age_days

    # ------------------------------------------------------------------
    # 账号关联
    # ------------------------------------------------------------------

    async def accounts(self) -> list[Account]:
        if self.db is None:
            return []
        async with self.db.session() as s:
            rows = (await s.execute(select(Account).order_by(Account.alias))).scalars().all()
            return list(rows)

    @staticmethod
    def save_state_file(path: Path, storage_state: dict[str, Any]) -> Path:
        """把 playwright ``context.storage_state()`` 落盘(原子写)。"""
        atomic_write_json(path, storage_state)
        return path

    @staticmethod
    def load_state_file(path: Path) -> dict[str, Any]:
        data = read_json(path, default=None)
        if not isinstance(data, dict):
            raise SessionError(f"登录态文件解析失败: {path}")
        return data

    @staticmethod
    def cookie_count(storage_state: dict[str, Any]) -> int:
        return len(storage_state.get("cookies") or [])

    def describe(self, handle: SessionHandle) -> dict[str, Any]:
        """一行摘要(CLI ``sessions`` 用)。"""
        exists = handle.exists()
        return {
            "platform": handle.platform,
            "role": handle.role,
            "alias": handle.alias,
            "path": self.layout.to_relative(handle.storage_state_path()),
            "exists": exists,
            "cookies": len(handle.cookies()) if exists else 0,
            "age_days": round(handle.age_days() or 0.0, 2) if exists else None,
            "need_renewal": self.need_renewal(handle) if exists else False,
        }


def state_age(handle: SessionHandle, days: int) -> bool:
    """辅助:登录态是否比 ``days`` 天更旧。"""
    age = handle.age_days()
    return bool(age is not None and age > days)


__all__ += ["state_age", "timedelta"]
