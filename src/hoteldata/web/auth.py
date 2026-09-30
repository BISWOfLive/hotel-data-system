"""后台认证与审计(Phase 3;总纲 §7.8)。

★ 范围(总纲明确定的两条)
========================

> 后台范围:账号/机器人/比价酒店增删 + **统一口令登录** + **操作审计**
> **明确不做**:角色区分、权限树、API 鉴权细化

所以这里**刻意没有用户表**:

* 一个**统一口令**(PBKDF2-HMAC-SHA256 加盐哈希);
* 一个**签名 cookie** 会话(``itsdangerous``,段1 已引入);
* 每条**写操作**落 ``ops_admin_audit``。

★ 为什么用 PBKDF2 而不是 argon2
==============================

总纲 §7.1 写的是「cryptography(Fernet 沿用)**+ argon2**」。但实测:

* ``argon2-cffi`` **不在依赖里**(``pyproject.toml`` 只有 ``cryptography``);
* ``hashlib.pbkdf2_hmac`` 是**标准库**,迭代次数可调,对"单口令、单机、低频登录"
  这个场景完全够用;
* 引入一个只为存一个口令哈希的依赖,不划算(与 §7.1 否决 LiteLLM 同一取向)。

段3 用 **PBKDF2-HMAC-SHA256 / 600,000 次迭代**(OWASP 2023 对 PBKDF2-SHA256 的建议值),
哈希串自带算法与迭代次数(``pbkdf2_sha256$600000$<salt>$<hash>``),
将来调迭代次数时**旧口令仍可验证**。

★ 为什么"没设口令 = 拒绝一切登录",而不是"默认放行"
================================================

这是与段2「未配置 ``MANAGE_CHATIDS`` 时管理群命令**一律拒绝**」同一条安全默认:
一个没有口令的后台等于把"改账号/删酒店"的能力开放给任何能访问该端口的人。
所以 :func:`verify_password` 在没设哈希时**恒返回 False**,并让路由回一句明确的提示。

★ 会话密钥从哪来
==============

优先 ``ADMIN_SESSION_SECRET``;留空则**退回 ``config/secret.key``**
(段1 的 Fernet 密钥,已经在文件里且不进 git)—— 这样"没配新密钥"也能开箱可用,
而不是启动就崩。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from loguru import logger

from hoteldata.settings import Settings, get_settings

__all__ = [
    "COOKIE_NAME",
    "PBKDF2_ITERATIONS",
    "AdminSession",
    "SessionSigner",
    "hash_password",
    "record_audit",
    "verify_password",
]

#: 会话 cookie 名
COOKIE_NAME = "hoteldata_admin"
#: PBKDF2 迭代次数(OWASP 对 PBKDF2-HMAC-SHA256 的建议量级)
PBKDF2_ITERATIONS = 600_000
_ALGO = "pbkdf2_sha256"


# ---------------------------------------------------------------------------
# 口令
# ---------------------------------------------------------------------------


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    """生成口令哈希(自带算法与迭代次数,便于将来升级参数)。

    格式:``pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>``
    """
    if not password:
        raise ValueError("口令不能为空")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return (
        f"{_ALGO}${iterations}$"
        f"{base64.b64encode(salt).decode('ascii')}$"
        f"{base64.b64encode(digest).decode('ascii')}"
    )


def verify_password(password: str, stored: str) -> bool:
    """校验口令。

    * ``stored`` 为空 → **恒 False**(没设口令 = 拒绝一切登录,不是放行);
    * 用 :func:`hmac.compare_digest` 做**定时安全**比较;
    * 哈希串格式不对 → False(并记 warning,那是配置错误不是密码错误)。
    """
    if not stored or not password:
        return False
    try:
        algo, iter_s, salt_b64, hash_b64 = stored.split("$", 3)
    except ValueError:
        logger.warning("ADMIN_PASSWORD_HASH 格式不对(应为 algo$iters$salt$hash);后台将拒绝登录")
        return False
    if algo != _ALGO:
        logger.warning("不支持的口令哈希算法 {}:后台将拒绝登录", algo)
        return False
    try:
        iterations = int(iter_s)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdminSession:
    """一个已登录的后台会话(来自签名 cookie)。"""

    actor: str
    issued_at: datetime
    expires_at: datetime

    @property
    def valid(self) -> bool:
        return datetime.now(self.expires_at.tzinfo) < self.expires_at

    def as_dict(self) -> dict[str, Any]:
        return {
            "actor": self.actor,
            "issued_at": self.issued_at.isoformat(timespec="seconds"),
            "expires_at": self.expires_at.isoformat(timespec="seconds"),
        }


class SessionSigner:
    """签名 cookie 的签发与校验(``itsdangerous``;段1 已引入该依赖)。

    ★ 为什么**不用**服务端会话表:后台是**单机单口令**,会话里只有"是谁、何时到期"。
      引入一张会话表会立刻带来"会话清理"这个新问题,而它并不解决任何真实风险
      —— cookie 已签名且不可伪造,密钥在服务器上。
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.cfg = self.settings.web
        self._signer: Any = None

    # ------------------------------------------------------------------

    def _secret(self) -> bytes:
        """取签名密钥:优先配置,其次 ``config/secret.key``。"""
        raw = self.cfg.session_secret
        if raw:
            return raw.encode("utf-8")
        key_file = self.settings.paths.secret_key_file
        try:
            if key_file.exists():
                return key_file.read_bytes().strip()
        except OSError as exc:  # pragma: no cover - 权限问题
            logger.warning("读取 {} 失败: {}", key_file, exc)
        # ★ 两条都没有:生成一个**进程内**临时密钥并**大声告警**。
        #   后果是"重启即掉线",但**不会**退化成"没有签名"(那才是真的不安全)。
        if not getattr(self, "_ephemeral_warned", False):
            self._ephemeral_warned = True
            logger.warning(
                "ADMIN_SESSION_SECRET 未配置且 {} 不存在 —— 使用进程内临时密钥,"
                "重启后所有后台会话失效(不影响功能,只影响登录保持)",
                key_file,
            )
        self._ephemeral = getattr(self, "_ephemeral", None) or secrets.token_bytes(32)
        return self._ephemeral

    def _get_signer(self) -> Any:
        if self._signer is None:
            from itsdangerous import URLSafeTimedSerializer

            self._signer = URLSafeTimedSerializer(
                self._secret(), salt="hoteldata.admin", serializer=None
            )
        return self._signer

    # ------------------------------------------------------------------

    def issue(self, actor: str = "admin") -> tuple[str, AdminSession]:
        """签发一个会话 cookie。"""
        now = datetime.now(self.settings.tzinfo)
        expires = now + timedelta(hours=int(self.cfg.session_hours))
        token = self._get_signer().dumps({"actor": actor, "iat": int(now.timestamp())})
        return token, AdminSession(actor=actor, issued_at=now, expires_at=expires)

    def load(self, token: str | None) -> AdminSession | None:
        """校验并解出会话。**任何异常都返回 None**(cookie 坏了就是没登录)。"""
        if not token:
            return None
        max_age = int(self.cfg.session_hours) * 3600
        try:
            data = self._get_signer().loads(token, max_age=max_age)
        except Exception:  # noqa: BLE001 - 签名不对/过期/格式错,一律视为未登录
            return None
        if not isinstance(data, dict):
            return None
        tz = self.settings.tzinfo
        try:
            issued = datetime.fromtimestamp(int(data.get("iat", 0)), tz=tz)
        except (TypeError, ValueError, OSError):
            return None
        expires = issued + timedelta(hours=int(self.cfg.session_hours))
        session = AdminSession(
            actor=str(data.get("actor") or "admin"), issued_at=issued, expires_at=expires
        )
        return session if session.valid else None


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------


async def record_audit(
    runtime: Any,
    *,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    detail: dict[str, Any] | None = None,
    result: str = "ok",
    error: str | None = None,
    actor: str = "admin",
    ip: str | None = None,
) -> None:
    """写一行后台操作审计(**append-only**)。

    ★ **审计失败不能拖垮业务操作**:这条纪律与段2「预警附图失败仅告警,文本照发」一致
      —— 但要 ``logger.error`` 让它可见,而不是静默吞掉。审计悄悄不写了
      比"操作失败"更糟:它让你以为有记录。
    """
    from hoteldata.infra.models import AdminAudit

    try:
        async with runtime.db.session() as session:
            session.add(
                AdminAudit(
                    actor=actor,
                    action=action,
                    target_type=target_type,
                    target_id=target_id,
                    detail=detail,
                    result=result,
                    error=error,
                    ip=ip,
                )
            )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "★ 后台审计写入失败(操作已执行,但这行记录丢了):action={} target={}/{} err={}",
            action,
            target_type,
            target_id,
            exc,
        )
