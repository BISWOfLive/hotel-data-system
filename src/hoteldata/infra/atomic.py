"""JSON 原子写(B20 遗产)。

**★ 必须继承的代码纪律(总纲 7.1.1 ② / 段1 §3.3):**

    旧系统 ``storage/atomic.py:20-23`` 实测注释:
      「Windows/Python3.14:只读句柄('rb')fsync 抛 EBADF(Errno 9),须以可写句柄 fsync。」

这个坑**只在写状态文件时才炸**,很难定位。**不要"顺手改成更自然的写法"**(``rb``)。

实现:同目录临时文件 → 写入 → ``flush`` + ``fsync`` → ``os.replace``(同盘原子替换)。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

__all__ = ["atomic_write_bytes", "atomic_write_json", "atomic_write_text", "read_json"]


def _fsync_path(path: Path) -> None:
    """以**可写句柄** fsync(``r+b``,不是 ``rb`` —— 见模块 docstring)。"""
    try:
        # ★★ 不要改成 "rb":Windows/Python 3.14 上只读句柄 fsync 抛 EBADF(Errno 9)
        with open(path, "r+b") as handle:
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:  # pragma: no cover - 平台差异兜底
        pass


def _fsync_dir(path: Path) -> None:
    """fsync 目录项(Windows 不支持打开目录,静默跳过)。"""
    if os.name == "nt":  # pragma: no cover - Windows 分支
        return
    try:  # pragma: no cover - POSIX only
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:  # pragma: no cover
        pass


def atomic_write_bytes(path: Path | str, data: bytes) -> Path:
    """原子写字节。返回最终路径。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_path(tmp)
        os.replace(tmp, target)
        _fsync_dir(target.parent)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return target


def atomic_write_text(path: Path | str, text: str, *, encoding: str = "utf-8") -> Path:
    """原子写文本。"""
    return atomic_write_bytes(path, text.encode(encoding))


def atomic_write_json(
    path: Path | str,
    data: Any,
    *,
    indent: int = 2,
    ensure_ascii: bool = False,
    encoding: str = "utf-8",
) -> Path:
    """原子写 JSON。"""
    payload = json.dumps(data, indent=indent, ensure_ascii=ensure_ascii, default=str)
    return atomic_write_text(path, payload, encoding=encoding)


def read_json(path: Path | str, default: Any = None) -> Any:
    """读 JSON;不存在或损坏时返回 ``default``(不抛)。"""
    target = Path(path)
    if not target.exists():
        return default
    try:
        with open(target, encoding="utf-8") as handle:
            return json.load(handle)
    except OSError, json.JSONDecodeError:
        return default
