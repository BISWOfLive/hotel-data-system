"""素材上传(T2A.4)—— 把"文件路径 → ``media_id``"这段收在一处。

为什么单独一个模块(而不是留在 client 里)
========================================

``BotClient.upload_media`` 是**协议动作**(三步分片,字节级,不许改);
本模块是**业务动作**:读盘 → 判类型 → 调协议 → **按内容缓存**。

两层分开的好处:协议层可以逐字节对齐官方 SDK,
而"图片文件在磁盘上、相对路径要换算、同一张图一天要发 300 个群"
这些**工程问题**不会污染协议。

★ **按 (路径, mtime, size) 缓存 ``media_id``**
==============================================

日报把同一张模块图发给 300 个群 —— 旧系统每次 ``bot.upload_media`` 都重新
分片上传一遍,300 次 × 每张几百 KB 的 base64 chunk 全打在长连接上,
既慢又容易触发平台限频。

缓存键带上 ``mtime`` 与 ``size``:文件被重新截过就一定换 key,
不会出现"发了旧图"这种最难查的问题。
缓存是**进程内 LRU**(不落库):``media_id`` 是平台侧会话级资源,
跨进程复用没有意义,落库反而制造第二个事实源。
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from typing import Any

from hoteldata.domains.bot.client import BotClient

__all__ = ["MediaUploader", "guess_media_type"]

#: 平台只接受这两种;``image`` 用于 jpg/png,``file`` 用于其它(旧系统只用了 image)
_MEDIA_SUFFIX = {
    ".jpg": "image",
    ".jpeg": "image",
    ".png": "image",
    ".gif": "image",
    ".webp": "image",
}

#: 进程内缓存上限(张)
_CACHE_MAX = 512


def guess_media_type(filename: str) -> str:
    """按扩展名判平台媒体类型(未知 → ``file``,不猜 ``image``)。"""
    return _MEDIA_SUFFIX.get(Path(filename).suffix.lower(), "file")


class MediaUploader:
    """按机器人缓存素材上传结果。"""

    def __init__(self, cache_max: int = _CACHE_MAX) -> None:
        self._cache: dict[tuple[str, str], OrderedDict[tuple[str, int, int], str]] = {}
        self._cache_max = cache_max
        self.uploads = 0
        self.hits = 0

    def _bucket(self, bot_name: str) -> OrderedDict[tuple[str, int, int], str]:
        return self._cache.setdefault(bot_name, OrderedDict())

    async def upload(self, bot: BotClient, path: Path | str) -> str:
        """把本地文件传成 ``media_id``(命中缓存则直接返回)。"""
        p = Path(path)
        if not p.is_absolute():
            raise ValueError(f"素材上传要求绝对路径(相对路径换算应在调用方完成): {path}")
        if not p.exists():
            raise FileNotFoundError(f"素材文件不存在: {p}")
        stat = p.stat()
        key = (str(p), int(stat.st_mtime), int(stat.st_size))
        bucket = self._bucket(bot.name)
        cached = bucket.get(key)
        if cached:
            self.hits += 1
            bucket.move_to_end(key)
            return cached

        media_id = await bot.upload_media(p.read_bytes(), guess_media_type(p.name), p.name)
        self.uploads += 1
        bucket[key] = media_id
        bucket.move_to_end(key)
        while len(bucket) > self._cache_max:
            bucket.popitem(last=False)
        return media_id

    def clear(self, bot_name: str | None = None) -> None:
        if bot_name is None:
            self._cache.clear()
        else:
            self._cache.pop(bot_name, None)

    def stats(self) -> dict[str, Any]:
        return {
            "uploads": self.uploads,
            "hits": self.hits,
            "bots": len(self._cache),
            "entries": sum(len(b) for b in self._cache.values()),
        }
