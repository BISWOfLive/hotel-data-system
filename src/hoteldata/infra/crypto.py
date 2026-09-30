"""凭据加密 —— Fernet 单例 + 密钥加载(+ ``kid`` 支持密钥轮换)。

沿用旧系统 ``storage/crypto.py`` 的语义并补两点:

  1. **单例**:旧系统每次调用都可能新建 Fernet;密钥文件读取改为进程内缓存。
  2. **``kid``(key id)前缀**:密文格式为 ``<kid>$<fernet-token>`` 时按 ``kid``
     选择密钥,支持轮换;没有前缀的旧密文按**主密钥**解(向后兼容旧库)。

密钥优先级:显式入参 > 环境变量 ``FERNET_KEY`` > 密钥文件 ``config/secret.key`` >
新生成并落盘(权限 0600)。
"""

from __future__ import annotations

import base64
import hashlib
import os
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from hoteldata.settings import Settings, get_settings

__all__ = ["Cipher", "DecryptError", "build_cipher", "generate_key", "get_cipher"]

#: 密文前缀分隔符
KID_SEP = "$"

#: 密钥轮换:旧密钥列表文件(可选)
OLD_KEYS_FILE = "secret.keys.old"


class DecryptError(RuntimeError):
    """解密失败(密钥不匹配或密文损坏)。"""


def generate_key() -> str:
    """生成一个新 Fernet 密钥(base64 urlsafe,44 字符)。"""
    return Fernet.generate_key().decode("ascii")


def _normalize_key(raw: str | bytes) -> bytes:
    """把各种写法的密钥归一成 Fernet 接受的 bytes。"""
    if isinstance(raw, bytes):
        candidate = raw.strip()
    else:
        candidate = raw.strip().encode("ascii", errors="ignore")
    if not candidate:
        raise ValueError("Fernet 密钥为空")
    # 已是合法 Fernet key(base64 urlsafe,32 字节解码后)
    try:
        if len(base64.urlsafe_b64decode(candidate)) == 32:
            return candidate
    except Exception:  # noqa: BLE001 - 非法 base64 走下面兜底
        pass
    # 兜底:当口令用,派生 32 字节 → base64
    digest = hashlib.sha256(candidate).digest()
    return base64.urlsafe_b64encode(digest)


class Cipher:
    """Fernet 加解密器(支持 ``kid`` 轮换)。"""

    def __init__(
        self,
        key: str | bytes,
        *,
        kid: str = "",
        old_keys: dict[str, str | bytes] | None = None,
    ) -> None:
        self._kid = kid
        self._primary = Fernet(_normalize_key(key))
        self._old: dict[str, Fernet] = {k: Fernet(_normalize_key(v)) for k, v in (old_keys or {}).items()}

    @property
    def kid(self) -> str:
        return self._kid

    def encrypt(self, plaintext: str) -> str:
        """加密。配置了 ``kid`` 时输出 ``<kid>$<token>``。"""
        token = self._primary.encrypt(plaintext.encode("utf-8")).decode("ascii")
        return f"{self._kid}{KID_SEP}{token}" if self._kid else token

    def decrypt(self, ciphertext: str) -> str:
        """解密。无前缀的旧密文按主密钥解(向后兼容旧库)。"""
        if not ciphertext:
            raise DecryptError("密文为空")
        raw = ciphertext.strip()
        if KID_SEP in raw:
            kid, _, token = raw.partition(KID_SEP)
            fernet = self._old.get(kid) or (self._primary if kid == self._kid else None)
            if fernet is None:
                raise DecryptError(f"未知密钥标识 kid={kid!r},无法解密")
        else:
            fernet = self._primary
            token = raw
        try:
            return fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise DecryptError("解密失败:密钥不匹配或密文损坏") from exc

    def try_decrypt(self, ciphertext: str, default: str | None = None) -> str | None:
        """解密失败返回 ``default``(用于容错探测)。"""
        try:
            return self.decrypt(ciphertext)
        except DecryptError:
            return default


def _load_or_create_key(path: Path, *, create: bool = True) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    if not create:
        raise FileNotFoundError(f"密钥文件不存在: {path}")
    key = generate_key()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - Windows 上 chmod 语义有限
        pass
    return key


def _load_old_keys(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        kid, _, key = line.partition("=")
        if kid and key:
            out[kid.strip()] = key.strip()
    return out


def build_cipher(settings: Settings | None = None) -> Cipher:
    """构造一个 Fernet 加解密器(不做缓存)。"""
    s = settings or get_settings()
    env_key = os.getenv("FERNET_KEY", "").strip()
    if env_key:
        key = env_key
    else:
        key = _load_or_create_key(s.paths.secret_key_file, create=True)
    kid = os.getenv("FERNET_KID", "").strip()
    old = _load_old_keys(s.paths.secret_key_file.parent / OLD_KEYS_FILE)
    return Cipher(key, kid=kid, old_keys=old)


@lru_cache(maxsize=1)
def _cached_cipher() -> Cipher:
    """无参缓存 —— ★ 参数不能带 ``Settings``。

    pydantic 的 ``BaseSettings`` **不可哈希**(``__hash__`` 为 None),
    给 ``lru_cache`` 传它当参数会直接抛 ``TypeError: unhashable type: 'Settings'``。
    所以缓存函数**只接受零参**,带 ``settings`` 的重载走 :func:`build_cipher`。
    """
    return build_cipher()


def get_cipher(settings: Settings | None = None) -> Cipher:
    """进程内单例(默认配置)。

    显式传入 ``settings`` 时**不缓存**(避免用不同配置拿到同一个实例)。
    测试可用 ``_cached_cipher.cache_clear()`` 重置。
    """
    if settings is None:
        return _cached_cipher()
    return build_cipher(settings)
