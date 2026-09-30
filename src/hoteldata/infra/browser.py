"""浏览器池(T2.2)—— 替代旧系统**四处重复的浏览器启动代码**。

旧系统重复处(总纲 5.5 D 级):``browsers.py`` ↔ ``login_manager.py`` ↔
``ebooking.py`` ↔ ``screenshoter.py`` 各有一份 ``_launch_browser`` / ``_new_context``;
登录探测有 **三处重复实现**、登录页特征常量有 **两处副本**。

本模块是**唯一**的浏览器启动处。

必须保留的 CDP 语义(B14 遗产,旧 ``comparator/browsers.py:73-116``)
------------------------------------------------------------------
  * ``page_session()`` 形状:**``yield (browser, context, page)`` 三元组**;
  * ``save_state_to`` 在 ``finally`` 中回写 ``ctx.storage_state`` ——
    **回写发生在关页之前**(否则拿不到 cookie);
  * **CDP 模式**:``connect_over_cdp`` → 复用 ``browser.contexts[0]``(共享用户 cookie)、
    **只开新 tab**、**退出不关浏览器**;``storage_state`` 入参被忽略;
  * **独立模式**:``finally`` 依次关 page → context → browser → ``p.stop()``。

★ ``max_contexts=4`` 是新系统的**设计指标**(段1 S6:浏览器池内存爆掉的对策),
**不是旧系统实测基线** —— 旧系统 ``config.py`` 全文无该参数、``browsers.py`` 无信号量/池/LRU,
唯一并发常量是 ``comparator/batch.py:62`` 的 ``ThreadPoolExecutor(max_workers=2)``。
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from hoteldata.infra.paths import Layout, get_layout
from hoteldata.infra.session_store import SessionHandle, SessionKey
from hoteldata.settings import BrowserSettings, Settings, get_settings

__all__ = ["STEALTH_INIT_SCRIPT", "BrowserPool", "ContextSlot", "LaunchMode"]

#: ★ B15 遗产 —— 4 行 JS **逐字继承**(``comparator/browsers.py:13-18``)
STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || { runtime: {} };
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
"""

#: 反自动化启动参数(旧系统 4 处独立定义,这里集中一处)
_LAUNCH_ARGS = (
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-features=Translate,OptimizationHints",
)


class BrowserError(RuntimeError):
    """浏览器启动/使用错误。"""


LaunchMode = str  # "cdp" | "chrome" | "chromium"


@dataclass(slots=True)
class ContextSlot:
    """一个 ``(platform, role, alias)`` 的常驻 context。"""

    key: SessionKey
    context: Any
    headless: bool
    mode: LaunchMode
    refs: int = 0
    pages: set[int] = field(default_factory=set)

    @property
    def busy(self) -> bool:
        return self.refs > 0


class BrowserPool:
    """一个 Chromium 进程;每个 ``(platform, role, alias)`` 一个 BrowserContext。

    * 超过 ``max_contexts`` → **LRU 淘汰**(只淘汰**空闲**的;全忙时等待);
    * CDP 模式下**不关闭**用户的浏览器;
    * 每次新 context **注入** :data:`STEALTH_INIT_SCRIPT`(CDP 模式不注入)。
    """

    def __init__(
        self,
        settings: Settings | None = None,
        layout: Layout | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.cfg: BrowserSettings = self.settings.browser
        self.layout = layout or get_layout(self.settings)

        self._pw: Any = None
        self._browser: Any = None
        self._mode: LaunchMode | None = None
        self._cdp_owned_context: Any = None
        self._slots: OrderedDict[tuple[str, str, str], ContextSlot] = OrderedDict()
        self._lock = asyncio.Lock()
        self._launch_lock = asyncio.Lock()

    # ==================================================================
    # 生命周期
    # ==================================================================

    @property
    def mode(self) -> LaunchMode | None:
        return self._mode

    @property
    def running(self) -> bool:
        return self._browser is not None and self._browser.is_connected()

    async def start(self) -> LaunchMode:
        """启动(或连上)浏览器。**幂等**。"""
        async with self._launch_lock:
            if self.running and self._mode is not None:
                return self._mode
            from playwright.async_api import async_playwright

            if self._pw is None:
                self._pw = await async_playwright().start()

            # ① CDP 优先
            if self.cfg.chrome_cdp_url:
                try:
                    self._browser = await self._pw.chromium.connect_over_cdp(
                        self.cfg.chrome_cdp_url,
                        timeout=self.cfg.cdp_connect_timeout_s * 1000,
                    )
                    self._mode = "cdp"
                    logger.info("已通过 CDP 连接浏览器: {}", self.cfg.chrome_cdp_url)
                    return self._mode
                except Exception as exc:  # noqa: BLE001
                    logger.warning("CDP 连接失败({}):回退本地启动 {}", exc, self.cfg.chrome_cdp_url)

            # ② 本机 Chrome
            try:
                self._browser = await self._pw.chromium.launch(
                    channel=self.cfg.channel or None,
                    headless=self.cfg.headless,
                    args=list(_LAUNCH_ARGS),
                    slow_mo=self.cfg.slow_mo_ms or 0,
                )
                self._mode = "chrome"
                logger.info("已启动本机 {} (headless={})", self.cfg.channel, self.cfg.headless)
                return self._mode
            except Exception as exc:  # noqa: BLE001
                logger.warning("本机 Chrome 启动失败({}):回退 Playwright Chromium", exc)

            # ③ Playwright 自带 Chromium
            self._browser = await self._pw.chromium.launch(
                headless=self.cfg.headless,
                args=list(_LAUNCH_ARGS),
                slow_mo=self.cfg.slow_mo_ms or 0,
            )
            self._mode = "chromium"
            logger.info("已启动 Playwright Chromium (headless={})", self.cfg.headless)
            return self._mode

    async def close(self) -> None:
        """关闭池内全部 context;**CDP 模式下不关用户的浏览器**。"""
        async with self._lock:
            slots = list(self._slots.items())
        for key, slot in slots:
            await self._close_slot(key, slot)
        self._slots.clear()
        if self._mode == "cdp":
            # ★ 退出不关浏览器(CDP 复用语义)
            self._browser = None
        elif self._browser is not None:
            try:
                await self._browser.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("关闭浏览器失败: {}", exc)
            self._browser = None
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception as exc:  # noqa: BLE001
                logger.debug("停止 playwright 失败: {}", exc)
            self._pw = None
        self._mode = None

    # ==================================================================
    # context 管理(LRU)
    # ==================================================================

    async def _close_slot(self, key: tuple[str, str, str], slot: ContextSlot) -> None:
        for page in list(slot.pages):  # pragma: no cover - 防御
            slot.pages.discard(page)
        try:
            await slot.context.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("关闭 context {} 失败: {}", key, exc)

    async def _evict_for(self, need: int = 1) -> int:
        """LRU 淘汰**空闲** context,腾出 ``need`` 个位置。返回淘汰数。"""
        evicted = 0
        async with self._lock:
            for key in list(self._slots):
                if len(self._slots) + need <= self.cfg.max_contexts:
                    break
                slot = self._slots[key]
                if slot.busy:
                    continue
                self._slots.pop(key, None)
                evicted += 1
                await self._close_slot(key, slot)
        if evicted:
            logger.debug("LRU 淘汰 {} 个 context(上限 {})", evicted, self.cfg.max_contexts)
        return evicted

    async def _wait_capacity(self, timeout_s: float = 120.0) -> None:
        """等出空位(全忙时);超时抛错。"""
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            if len(self._slots) < self.cfg.max_contexts:
                return
            if await self._evict_for() == 0:
                if asyncio.get_running_loop().time() > deadline:
                    raise BrowserError(
                        f"浏览器 context 全部占用且等待超时({timeout_s}s):"
                        f"上限 MAX_CONTEXTS={self.cfg.max_contexts}"
                    )
                await asyncio.sleep(0.25)
            else:
                return

    async def _new_context(self, key: SessionKey, state_path: Path | None) -> tuple[Any, LaunchMode]:
        """独立模式:新建 context + 注入 stealth + 载入登录态。"""
        assert self._browser is not None
        ctx_kwargs: dict[str, Any] = {
            "viewport": {
                "width": self.cfg.viewport_width,
                "height": self.cfg.viewport_height,
            },
            "locale": self.cfg.locale,
            "timezone_id": self.cfg.timezone_id,
            "user_agent": self.cfg.user_agent,
        }
        if state_path is not None and state_path.exists():
            ctx_kwargs["storage_state"] = str(state_path)
        context = await self._browser.new_context(**ctx_kwargs)
        # ★ 只在独立模式注入;CDP 模式复用用户既有 context,不注入
        await context.add_init_script(STEALTH_INIT_SCRIPT)
        return context, self._mode or "chrome"

    async def _get_context(self, handle: SessionHandle) -> ContextSlot:
        key = handle.key.pair
        async with self._lock:
            slot = self._slots.get(key)
            if slot is not None and self.running:
                self._slots.move_to_end(key)
                return slot
            if slot is not None:  # 浏览器已断开 → 丢弃旧 slot
                self._slots.pop(key, None)

        await self._wait_capacity()
        await self.start()

        if self._mode == "cdp":
            # ★ 复用 browser.contexts[0](共享用户 cookie)
            contexts = self._browser.contexts
            context = contexts[0] if contexts else await self._browser.new_context()
            self._cdp_owned_context = context if not contexts else None
            mode: LaunchMode = "cdp"
        else:
            context, mode = await self._new_context(handle, handle.storage_state_path())

        slot = ContextSlot(key=key, context=context, headless=self.cfg.headless, mode=mode)
        async with self._lock:
            self._slots[key] = slot
            self._slots.move_to_end(key)
        return slot

    # ==================================================================
    # page_session —— 三元组形状(B14 遗产)
    # ==================================================================

    @asynccontextmanager
    async def page_session(
        self,
        handle: SessionHandle,
        *,
        save_state_to: Path | None = None,
        new_page: bool = True,
    ) -> AsyncIterator[tuple[Any, Any, Any]]:
        """★ ``yield (browser, context, page)`` **三元组**(形状不许改)。

        ``finally`` 顺序(逐字继承):

          1. ``ctx.storage_state(save_state_to)`` —— **回写在关页之前**,否则拿不到 cookie;
          2. ``page.close()``;
          3. **非 CDP** 模式才关 context / browser;
          4. **恒** ``p.stop()``(由 :meth:`close` 统一负责,这里不重复)。
        """
        slot = await self._get_context(handle)
        slot.refs += 1
        page = None
        try:
            if new_page:
                page = await slot.context.new_page()
            else:
                pages = slot.context.pages
                page = pages[0] if pages else await slot.context.new_page()
            slot.pages.add(id(page))
            page.set_default_timeout(self.cfg.nav_timeout_s * 1000)
            page.set_default_navigation_timeout(self.cfg.nav_timeout_s * 1000)
            yield (self._browser, slot.context, page)
        finally:
            # ① 先回写登录态(关页之前!)
            if save_state_to is not None:
                try:
                    state = await slot.context.storage_state()
                    self.layout.states_dir.mkdir(parents=True, exist_ok=True)
                    SessionStore_save(save_state_to, state)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("回写登录态失败 {}: {}", save_state_to, exc)
            # ② 关页
            if page is not None:
                slot.pages.discard(id(page))
                try:
                    await page.close()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("关闭 page 失败: {}", exc)
            slot.refs = max(0, slot.refs - 1)
            # ③ CDP 模式:退出不关浏览器(也不关用户的 context)
            #    独立模式:context 保留在池里做常驻(由 LRU 淘汰),这里不关。
            await self._evict_for()

    # ==================================================================
    # 观测
    # ==================================================================

    def snapshot(self) -> dict[str, Any]:
        return {
            "mode": self._mode,
            "running": self.running,
            "max_contexts": self.cfg.max_contexts,
            "contexts": [
                {
                    "key": "/".join(k),
                    "refs": s.refs,
                    "headless": s.headless,
                    "mode": s.mode,
                    "pages": len(s.pages),
                }
                for k, s in self._slots.items()
            ],
        }


def SessionStore_save(path: Path, storage_state: dict[str, Any]) -> None:
    """避免 import 环:直接用原子写落盘(与 ``SessionStore.save_state_file`` 同实现)。"""
    from hoteldata.infra.atomic import atomic_write_json

    atomic_write_json(path, storage_state)


#: 兼容旧调用名
__all__ += ["SessionStore_save"]
