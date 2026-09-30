"""两层限频 ★ 段1 的风控核心(T2.3)。

旧系统只有**单请求 ``time.sleep(0.6)``**,且**零并发**;两处问题:

  1. 0.6s 的**唯一消费点在 SDK 内层循环末行**,只有"实际发出请求并拿到响应"才 sleep;
  2. 没有平台级全局视角 —— 多账号并发时按账号各睡各的,**出口 IP 的总速率不受控**。

段1 升级为**两层**,语义必须等价甚至更严
----------------------------------------
============  ==========================  ==========================================
层            机制                        依据
============  ==========================  ==========================================
**账号级**    ``asyncio.Lock`` per account  风控按账号算:同账号请求必须**串行**
**平台级**    token bucket(间隔 ≥0.6s)     **风控也按 IP 算** —— 300 账号同出口 IP
                                          光靠账号串行防不住
============  ==========================  ==========================================

★ **目标是稳定,不是更快**。提取的瓶颈不是并发能力,是平台级 0.6s 限频这个风控闸门:
300 家 × 5 模块 = 1,500 模块,每模块约 5 个请求 × 0.6s ≈ **1.25 小时**,窗口有 8 小时
—— 提取**完全不缺时间**(段1 §2.3)。

★ **记录每次实际间隔**,自检时可校验"有没有低于 0.6s"(V16 的证据来源)。
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from hoteldata.settings import Settings, get_settings

__all__ = ["RateLimiter", "RateLimitStats", "RequestSlot"]


@dataclass(slots=True)
class RateLimitStats:
    """限频观测数据 —— 自检/``/status`` 的输入。"""

    total_requests: int = 0
    total_wait_s: float = 0.0
    by_platform: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    by_account: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    #: 最近 N 次平台级实际间隔(秒)
    recent_intervals: deque[float] = field(default_factory=lambda: deque(maxlen=512))
    #: 违例计数:实际间隔 < 配置值(容差内不算)
    violations: int = 0

    def as_dict(self) -> dict[str, object]:
        intervals = list(self.recent_intervals)
        return {
            "total_requests": self.total_requests,
            "total_wait_s": round(self.total_wait_s, 3),
            "by_platform": dict(self.by_platform),
            "by_account": dict(self.by_account),
            "min_interval_s": round(min(intervals), 4) if intervals else None,
            "avg_interval_s": round(sum(intervals) / len(intervals), 4) if intervals else None,
            "violations": self.violations,
        }

    def reset(self) -> None:
        self.total_requests = 0
        self.total_wait_s = 0.0
        self.by_platform.clear()
        self.by_account.clear()
        self.recent_intervals.clear()
        self.violations = 0


class _PlatformBucket:
    """平台级令牌桶:**串行放行 + 相邻请求间隔 ≥ interval**。

    实现要点:用一把 ``asyncio.Lock`` 把「读上次放行时刻 → 睡到该放行 → 记录放行时刻」
    做成**临界区**。若不放进临界区,并发协程会同时读到同一个"上次时刻"、
    同时通过 —— 限频立刻失效。
    """

    __slots__ = ("interval_s", "lock", "_last", "_last_release")

    def __init__(self, interval_s: float) -> None:
        self.interval_s = interval_s
        self.lock = asyncio.Lock()
        self._last: float | None = None
        self._last_release: float | None = None

    async def acquire(self) -> tuple[float, float | None]:
        """返回 ``(等待秒数, **相邻两次放行之间的真实间隔**)``。

        ★ 间隔必须在 **sleep 之后**用 ``release - 上一次 release`` 量。
        不能拿"进临界区时距上次放行的差"当间隔 —— 后者天然小于阈值
        (那正是"需要补睡"的原因),拿它做自检会把**正常限频**全判成违例。
        """
        async with self.lock:
            now = time.monotonic()
            wait = 0.0
            if self._last is not None:
                elapsed = now - self._last
                if elapsed < self.interval_s:
                    wait = self.interval_s - elapsed
                    if wait > 0:
                        await asyncio.sleep(wait)
            release = time.monotonic()
            # ★ 真实放行间隔 = 本次放行 - 上次放行(在 sleep 之后量)
            interval = (release - self._last) if self._last is not None else None
            self._last = release
            self._last_release = release
            return wait, interval


class RateLimiter:
    """账号级串行 + 平台级全局限频。

    用法::

        async with limiter.request("ctrip", "ctrip001"):
            await http.get(...)

    ``interval_s <= 0`` 或 ``enabled=False`` 时**只保留账号级串行**(测试/离线用)。
    """

    def __init__(self, settings: Settings | None = None) -> None:
        s = settings or get_settings()
        self.settings = s
        self.interval_s: float = float(s.rate_limit.interval_s)
        self.enabled: bool = bool(s.rate_limit.enabled)
        self.max_concurrent_accounts: int = int(s.rate_limit.max_concurrent_accounts)

        self._buckets: dict[str, _PlatformBucket] = {}
        self._account_locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()
        self.stats = RateLimitStats()
        #: 跨账号并发闸门(默认 4;平台级限频仍是总闸)
        self.account_semaphore = asyncio.Semaphore(self.max_concurrent_accounts)

    # ------------------------------------------------------------------

    def _account_key(self, platform: str, account: str) -> str:
        return f"{platform}::{account}"

    async def _bucket(self, platform: str) -> _PlatformBucket:
        bucket = self._buckets.get(platform)
        if bucket is None:
            async with self._guard:
                bucket = self._buckets.get(platform)
                if bucket is None:
                    bucket = _PlatformBucket(self.interval_s)
                    self._buckets[platform] = bucket
        return bucket

    async def _account_lock(self, platform: str, account: str) -> asyncio.Lock:
        key = self._account_key(platform, account)
        lock = self._account_locks.get(key)
        if lock is None:
            async with self._guard:
                lock = self._account_locks.get(key)
                if lock is None:
                    lock = asyncio.Lock()
                    self._account_locks[key] = lock
        return lock

    # ------------------------------------------------------------------

    @asynccontextmanager
    async def request(self, platform: str, account: str) -> AsyncIterator[None]:
        """取一次请求配额。

        顺序:**先账号级串行,再平台级限频** —— 保证同一账号的请求严格顺序,
        同时全平台相邻请求间隔 ≥ ``interval_s``。
        """
        lock = await self._account_lock(platform, account)
        async with lock:
            if self.enabled and self.interval_s > 0:
                bucket = await self._bucket(platform)
                wait, interval = await bucket.acquire()
                self.stats.total_wait_s += wait
                if interval is not None:
                    self.stats.recent_intervals.append(interval)
                    # 容差 5%:monotonic 精度与 sleep 唤醒抖动
                    if interval < self.interval_s * 0.95:
                        self.stats.violations += 1
            self.stats.total_requests += 1
            self.stats.by_platform[platform] += 1
            self.stats.by_account[self._account_key(platform, account)] += 1
            yield

    @asynccontextmanager
    async def account_slot(self) -> AsyncIterator[None]:
        """跨账号并发闸门(``MAX_CONCURRENT_ACCOUNTS``,默认 4)。

        ★ 这只是**并发上限**,平台级 0.6s 限频才是**总速率**约束。
        """
        async with self.account_semaphore:
            yield

    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, object]:
        """限频自检快照(V16 证据)。"""
        data = self.stats.as_dict()
        data.update(
            {
                "interval_s": self.interval_s,
                "enabled": self.enabled,
                "max_concurrent_accounts": self.max_concurrent_accounts,
                "rate_limit_ok": bool(self.stats.violations == 0 or not self.enabled or self.interval_s <= 0),
            }
        )
        return data


#: 语义别名(便于阅读调用点)
RequestSlot = RateLimiter
