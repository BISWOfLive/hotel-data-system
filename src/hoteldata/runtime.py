"""★ Runtime 组合根(T1.5)—— CLI 与 HTTP 共用一套资源装配。

替代旧系统 ``app/__init__.py`` 里的 ``set_bot()`` / ``set_bot_manager()``
那种**模块级全局注入**(结构性问题 #8)。

::

    class Runtime:
        settings / db / browser / sessions / limiter / http / tasks / layout / rules

  * **HTTP 模式**:由 FastAPI ``lifespan`` 创建;
  * **CLI 模式**:``async with Runtime.create(settings) as rt:``

**析构顺序**(不能乱):先停任务 → 关浏览器 → 关 http → 释放 db。
反过来的话,正在跑的任务会拿到已关闭的连接池。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from hoteldata.infra.db import Database
from hoteldata.infra.http import HttpClient
from hoteldata.infra.paths import Layout, get_layout
from hoteldata.infra.rate_limit import RateLimiter
from hoteldata.infra.session_store import SessionStore
from hoteldata.infra.tasks import TaskRegistry, get_registry
from hoteldata.settings import Settings, get_settings

__all__ = ["Runtime"]


@dataclass
class Runtime:
    """进程级资源容器。**所有入口都必须经由它取资源。**"""

    settings: Settings
    db: Database
    sessions: SessionStore
    limiter: RateLimiter
    http: HttpClient
    tasks: TaskRegistry
    layout: Layout
    #: 浏览器池(**懒启动**:离线命令不必付启动代价)
    browser: Any = None
    #: 登录管家(懒构建)
    login_manager: Any = None
    #: APScheduler(仅 ``serve`` 用)
    scheduler: Any = None
    #: 规则(懒加载,支持 mtime 热加载)
    _rules: Any = None
    #: 段2:多机器人管理器(懒构建;``AIBOT_ENABLED=0`` 时恒为空实例)
    _bots: Any = None
    #: 段2:机器人网关是否**已经真正启动过**(与"实例是否存在"是两件事)
    _bots_started: bool = False
    #: 段2:推送审计 / 绑定(无状态,可随时新建)
    _audit: Any = None
    _bindings: Any = None
    _sender: Any = None
    #: 段2:推送服务(懒构建)—— **唯一对外推送出口**
    _push: Any = None
    #: 段2:消息链编排(懒构建)
    _router: Any = None
    #: 段2:域服务(懒构建)
    _report_svc: Any = None
    _alert_svc: Any = None
    _review_svc: Any = None
    #: 段3:比价域服务(懒构建)
    _compare_svc: Any = None
    _extras: dict[str, Any] = field(default_factory=dict)


    # ==================================================================
    # 构建
    # ==================================================================

    @classmethod
    @asynccontextmanager
    async def create(
        cls,
        settings: Settings | None = None,
        *,
        with_db: bool = True,
        with_browser: bool = False,
        with_scheduler: bool = False,
        check_db: bool = True,
    ) -> AsyncIterator[Runtime]:
        """创建并装配 Runtime。**``check_db=True`` 时连不上 DB 直接失败**。

        「启动即校验,不要静默降级」(T1.2):连不上就抛,不吞。
        """
        s = settings or get_settings()
        s.ensure_dirs()

        db = Database(s)
        if check_db and with_db:
            version = await db.ping()  # 连不上直接抛
            logger.debug("数据库已连接: {}", version.split(",")[0])

        rt = cls(
            settings=s,
            db=db,
            sessions=SessionStore(s, db if with_db else None),
            limiter=RateLimiter(s),
            http=HttpClient(settings=s),
            tasks=get_registry(),
            layout=get_layout(s),
        )
        try:
            if with_browser:
                await rt.start_browser()
            if with_scheduler:
                await rt.start_scheduler()
            yield rt
        finally:
            await rt.aclose()

    # ==================================================================
    # 懒启动
    # ==================================================================

    async def start_browser(self) -> Any:
        if self.browser is not None:
            return self.browser
        from hoteldata.infra.browser import BrowserPool

        pool = BrowserPool(self.settings, self.layout)
        await pool.start()
        self.browser = pool
        logger.info("浏览器池已就绪: mode={} max_contexts={}", pool.mode, pool.settings.max_contexts)
        return pool

    async def start_scheduler(self) -> Any:
        """起 APScheduler。★ **注册表为空时直接报错,不起空调度器。**

        ★★ 这里挡的是一个真实缺陷(本地联调发现):

        ``Runtime.create(with_scheduler=True)`` 在 ``create()`` **内部**就把调度器
        建好了,而 ``hoteldata.jobs`` 的导入写在调用方(main.py)之后 ——
        于是调度器是拿**空注册表**建的:``scheduler.get_jobs() == 0``。
        启动日志照样打印"已注册任务 19 个"(那是导入之后才数的),看起来一切正常,
        实际**没有任何定时任务会触发** —— 09:00 日报、09:04 关房预警、22 项按节奏,
        全部静默不发生。

        一个"0 任务的调度器"在任何场景下都不是合法状态(要么是装配顺序错,
        要么是忘了导入 ``hoteldata.jobs``),所以这里**启动即失败**,
        与"启动即校验,不静默降级"同一条纪律。
        """
        if self.scheduler is not None:
            return self.scheduler
        names = self.tasks.names()
        if not names:
            raise RuntimeError(
                "任务注册表为空,拒绝启动一个 0 任务的调度器(那等于所有定时推送静默失效)。\n"
                "原因通常是:在导入 `hoteldata.jobs` **之前**就创建了 with_scheduler=True 的 Runtime。\n"
                "修法:先 `import hoteldata.jobs`(触发 @task 注册),再 Runtime.create(...)。"
            )
        scheduler = self.tasks.build_scheduler(self)
        scheduler.start()
        self.scheduler = scheduler
        logger.info("调度器已启动:{} 个任务", len(names))
        return scheduler

    # ==================================================================
    # 域服务(懒构建,避免域之间互相 import)
    # ==================================================================

    @property
    def rules(self) -> Any:
        """规则集(按 mtime 热加载)。"""
        from hoteldata.domains.collect.rules import get_api_rules

        return get_api_rules()

    @property
    def rotation(self) -> Any:
        from hoteldata.domains.collect.rotation import get_rotation

        return get_rotation()

    def login(self) -> Any:
        """登录管家(T2.4)。"""
        if self.login_manager is None:
            from hoteldata.domains.session.manager import LoginManager

            self.login_manager = LoginManager(
                self.settings,
                sessions=self.sessions,
                pool=self.browser,
                layout=self.layout,
            )
        return self.login_manager

    def datacenter(self) -> Any:
        """数据中心提取器(T3.9)。"""
        from hoteldata.domains.collect.datacenter import DatacenterExtractor

        return DatacenterExtractor(
            self.rules,
            pool=self.browser,
            ensure_login=self.ensure_login,
            allow_browser=self.browser is not None,
        )

    # ==================================================================
    # ★ 段2:推送与机器人(懒构建 —— 与上面同一套纪律,不用模块级全局)
    # ==================================================================

    @property
    def audit(self) -> Any:
        """推送审计(``push_logs`` 的唯一写入口)。"""
        if self._audit is None:
            from hoteldata.push.audit import PushAudit

            self._audit = PushAudit(self.db)
        return self._audit

    @property
    def bindings(self) -> Any:
        """群 ↔ 酒店绑定。"""
        if self._bindings is None:
            from hoteldata.push.bindings import Bindings

            self._bindings = Bindings(self.db)
        return self._bindings

    @property
    def bots(self) -> Any:
        """★ 多机器人管理器(懒构建)。

        🚫 **没有"单机器人回退"路径**(段2 §4.5 D 级丢弃项):
        「单机器人」就是"1 个实例",一样走 ``core_bots`` 表。
        ``AIBOT_ENABLED=0`` 或表为空 → 返回**空管理器**(``size()==0``),
        推送会失败**并留下审计**,而不是静默。
        """
        if self._bots is None:
            from hoteldata.domains.bot.manager import BotManager

            self._bots = BotManager([], settings=self.settings)
        return self._bots

    @property
    def sender(self) -> Any:
        """发送原语(文本拆分 / 图片逐张 / 素材缓存)。"""
        if self._sender is None:
            from hoteldata.push.sender import Sender

            self._sender = Sender(
                self.bots,
                layout=self.layout,
                max_images=self.settings.push.max_images,
                limit_chars=self.settings.push.merge_limit_chars,
            )
        return self._sender

    @property
    def push(self) -> Any:
        """★ 推送服务(**所有推送的唯一出口**,含 ``send_alert``)。"""
        if self._push is None:
            from hoteldata.push.service import PushService

            self._push = PushService(
                settings=self.settings,
                manager=self.bots,
                sender=self.sender,
                audit=self.audit,
                bindings=self.bindings,
            )
        return self._push

    def router(self) -> Any:
        """★ 消息处理链编排(命令 → 比价问答 → 实时问答 → FAQ → 兜底)。"""
        if self._router is None:
            from hoteldata.domains.bot.router import MessageRouter

            self._router = MessageRouter(self, sender=self.sender)
        return self._router

    def report(self) -> Any:
        """报告节奏引擎服务(22 项 + 日报)。"""
        if self._report_svc is None:
            from hoteldata.domains.report.service import ReportService

            self._report_svc = ReportService(self)
        return self._report_svc

    def alert(self) -> Any:
        """预警服务(6+1 规则)。"""
        if self._alert_svc is None:
            from hoteldata.domains.alert.service import AlertService

            self._alert_svc = AlertService(self)
        return self._alert_svc

    def review(self) -> Any:
        """点评交互服务。"""
        if self._review_svc is None:
            from hoteldata.domains.review.service import ReviewService

            self._review_svc = ReviewService(self)
        return self._review_svc

    def compare(self) -> Any:
        """★ 段3:比价域服务(CLI / 任务 / 段2 日报钩子的唯一入口)。

        ★ **在这里触发平台注册**:``domains.compare`` 的 ``__init__`` 刻意不 import
          平台模块(会形成 ``__init__ → platforms → contract → __init__`` 的循环,
          见其模块文档)。所以"谁要用平台,谁负责 load 一次" —— 这里就是那个点。

        幂等,重复调用不重复注册(``registry.register`` 对重名会抛错 → 必须幂等)。
        """
        if self._compare_svc is None:
            from hoteldata.domains.compare import load_platforms
            from hoteldata.domains.compare.service import CompareService

            load_platforms()
            self._compare_svc = CompareService(self)
        return self._compare_svc

    # ==================================================================
    # ★ 段2:机器人网关启停
    # ==================================================================

    async def start_bots(self) -> Any:
        """★ 拉起机器人网关:读 ``core_bots`` → 解密 → 起长连接 → 注入消息链。

        这是 ``serve`` 里"单进程 asyncio 同时跑机器人 + 定时任务"的接线点。
        返回装配好的 :class:`~hoteldata.domains.bot.manager.BotManager`。

        ★ **实例身份必须稳定**:``self.bots`` 是懒构建的,推送服务可能已经先拿到
        一个空管理器。所以这里用 ``manager.add(...)`` **就地添加**,
        **绝不新建一个 BotManager 再赋值** —— 否则 PushService 会永远对着
        那个 0 实例的旧管理器发消息(每条推送都失败,而且看起来"配置没错")。
        """
        manager = self.bots  # ★ 取(或创建)那一个实例,后续只往里加
        if self._bots_started:
            return manager
        if not self.settings.bot.enabled:
            logger.warning("AIBOT_ENABLED=0,机器人网关未启动(推送会失败并留下审计)")
            self._bots_started = True
            return manager

        from hoteldata.domains.bot.manager import load_bots_from_db

        rows = await load_bots_from_db(self.db, self.settings)
        for item in rows:
            manager.add(item)

        router = self.router()
        manager.set_handlers(on_message=router.on_message, on_event=router.on_event)
        # ★ D1:健康循环的掉线告警走推送服务(遍历在线机器人),不依赖单例
        manager.set_alert_notifier(lambda text: self.push.send_alert(text))
        await manager.start_all()
        manager.start_health_loop()
        self._bots_started = True
        logger.info("机器人网关已启动:{} 个实例 {}", manager.size(), manager.names())
        return manager

    async def start_push(self) -> Any:
        """启动推送派发器(worker 数 = ``max(1, 在线机器人数)``)。"""
        svc = self.push
        await svc.start()
        # 日报组装器由报告域提供(懒 import:避免 push/ 依赖 domain)。
        # ★ 签名 ``(chatid, price_section)`` —— ``price_section`` 是段3 的钩子(§1.4)。
        from hoteldata.domains.report.daily import build_daily_message

        async def _build_daily(chatid: str, price_section: str | None) -> Any:
            # ★★ 段3 的比价段**从这里注入** —— 段2 的 ``build_daily_message``
            #    与 ``build_daily_section`` **一行都没改**(V82 用 SHA256 基线证明)。
            #
            #    为什么注入点在 runtime 而不在 daily.py:
            #    ``daily.py`` 属于段2 的 ``domains/report``,让它 import
            #    ``domains/compare`` 就**违反了"域之间不互相 import"**这条硬约束
            #    (总纲 §7.2)。``runtime.py`` 是**装配处**,它本来就是唯一允许
            #    知道所有域的地方 —— 这就是 §1.4 那个钩子当初留着的意义。
            if price_section is None and self.settings.compare.enabled:
                try:
                    price_section = await self.compare().group_price_section(chatid)
                except Exception as exc:  # noqa: BLE001 - 比价段失败不该让日报发不出去
                    logger.warning("日报比价段组装失败(group={}),日报照发: {}", chatid, exc)
            return await build_daily_message(self, chatid, price_section=price_section)

        svc.daily_builder = _build_daily
        return svc


    async def ensure_login(self, handle: Any) -> bool:
        """供提取域注入的登录回调(**避免域之间互相 import** —— 硬约束 2)。"""
        manager = self.login()
        try:
            return bool(await manager.ensure_valid(handle))
        except Exception as exc:  # noqa: BLE001
            logger.warning("确保登录态有效失败 {}: {}", handle, exc)
            return False

    # ==================================================================
    # 上下文构造
    # ==================================================================

    def extract_context(
        self,
        hotel: Any,
        account: Any,
        collect_date: Any,
        *,
        session: Any = None,
        persist_raw: bool = True,
    ) -> Any:
        """构造 :class:`ExtractContext`(批次 D 与数据中心共用)。"""
        from hoteldata.domains.collect.contract import (
            AccountRef,
            ExtractContext,
            HotelRef,
        )
        from hoteldata.domains.collect.windows import parse_collect_date

        day = parse_collect_date(collect_date)
        h = HotelRef(
            id=int(hotel.id),
            name=str(hotel.name),
            city=getattr(hotel, "city", None),
            ebk_hotel_id=getattr(hotel, "ebk_hotel_id", None),
        )
        a = AccountRef(
            id=getattr(account, "id", None),
            alias=str(getattr(account, "alias", "unknown")),
            platform=str(getattr(account, "platform", "ctrip")),
            is_multi=bool(getattr(account, "is_multi", False)),
        )
        handle = session or self.sessions.handle(a.platform, "ebooking", a.alias)
        return ExtractContext(
            hotel=h,
            account=a,
            collect_date=day,
            session=handle,
            limiter=self.limiter,
            layout=self.layout,
            http=self.http,
            api_timeout_s=self.settings.api_timeout_s,
            persist_raw=persist_raw,
        )

    # ==================================================================
    # 状态 / 析构
    # ==================================================================

    async def status(self) -> dict[str, Any]:
        """``/status`` 用的运行态摘要。"""
        from hoteldata.logging import interpreter_banner

        info: dict[str, Any] = {
            "python": interpreter_banner(),
            "settings": self.settings.safe_repr(),
            "tasks_registered": len(self.tasks.names()),
            "task_names": self.tasks.names(),
            "rate_limit": self.limiter.snapshot(),
        }
        try:
            info["db"] = await self.db.server_info()
            info["db_ok"] = True
        except Exception as exc:  # noqa: BLE001
            info["db_ok"] = False
            info["db_error"] = str(exc)
        if self.scheduler is not None:
            info["scheduler"] = {
                "running": bool(getattr(self.scheduler, "running", False)),
                "jobs": len(getattr(self.scheduler, "get_jobs", lambda: [])()),
            }
        if self.browser is not None:
            info["browser"] = self.browser.snapshot()
        # 段2:机器人健康 + 推送派发器(用统一契约 ``health() -> dict[str, bool]``)
        if self._bots is not None:
            info["bots"] = self._bots.snapshot()
        if self._push is not None:
            info["push"] = self._push.snapshot()
        # 段3:比价域(★ 只在服务已构建时报告,避免只读 status 就拉起平台注册)
        if self._compare_svc is not None:
            info["compare"] = self._compare_svc.snapshot()
        return info

    async def aclose(self) -> None:
        """★ 析构顺序:先停机器人/推送 → 停任务 → 关浏览器 → 关 http → 释放 db。"""
        # ⓪ 先停机器人网关(不再有新的入站消息)与推送派发器
        if self._bots is not None:
            try:
                await self._bots.stop_all()
            except Exception as exc:  # noqa: BLE001
                logger.debug("关闭机器人网关失败: {}", exc)
            self._bots = None
            self._bots_started = False
        if self._push is not None:
            try:
                await self._push.stop()
            except Exception as exc:  # noqa: BLE001
                logger.debug("关闭推送派发器失败: {}", exc)
            self._push = None
        # ① 先停任务(不再有新请求)
        if self.scheduler is not None:
            try:
                self.scheduler.shutdown(wait=False)
            except Exception as exc:  # noqa: BLE001
                logger.debug("关闭调度器失败: {}", exc)
            self.scheduler = None
        # ② 关浏览器
        if self.browser is not None:
            try:
                await self.browser.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("关闭浏览器池失败: {}", exc)
            self.browser = None
        # ③ 关 http
        try:
            await self.http.aclose()
        except Exception as exc:  # noqa: BLE001
            logger.debug("关闭 http 客户端失败: {}", exc)
        # ④ 释放 db
        try:
            await self.db.dispose()
        except Exception as exc:  # noqa: BLE001
            logger.debug("释放数据库失败: {}", exc)

    # 便捷
    def path(self, *parts: str) -> Path:
        return self.settings.paths.project_root.joinpath(*parts)
