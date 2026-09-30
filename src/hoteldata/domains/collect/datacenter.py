"""数据中心提取器(24 子模块)+ 并发与重试编排(T3.9)。

**并发策略(段1 §2.3 —— 必须理解这条"性能真相")**
--------------------------------------------------
> 提取的瓶颈**不是并发能力,是「平台级 0.6s 全局限频」这个风控闸门**。
> 300 家 × 5 模块 = 1,500 模块,每模块约 5 个请求 × 0.6s ≈ **1.25 小时**,
> 窗口有 8 小时 —— **提取完全不缺时间**。
>
> 所以并发策略是:**跨账号适度并发 + 严格服从平台级限频**,
> 目标**不是"更快",而是"稳定且不触发风控"**。

具体:
  * **跨账号并发** —— ``asyncio.Semaphore(MAX_CONCURRENT_ACCOUNTS)``(默认 4);
  * **账号内串行** —— 同一账号的模块按顺序提取(风控按账号算);
  * 所有请求经 ``limiter.request(...)``(**平台级全局限频才是总闸**);
  * **单模块失败不阻断整批**(记 ``failed`` 继续下一个)。

**重试**:仅**幂等 GET** 重试;退避 ``[2, 8, 30]`` 秒;**POST(SOA 查询)不重试**
(避免放大风控)—— 这条在 :class:`~hoteldata.infra.http.HttpClient` 里实现。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from loguru import logger

from hoteldata.domains.collect.api import ApiChannel
from hoteldata.domains.collect.browser import BrowserChannel
from hoteldata.domains.collect.channels import (
    ChannelOrchestrator,
    aggregate_results,
)
from hoteldata.domains.collect.contract import (
    ExtractContext,
    ExtractResult,
)
from hoteldata.domains.collect.repository import CollectRepository
from hoteldata.domains.collect.rotation import get_rotation
from hoteldata.domains.collect.rules import ApiRules, SubModuleCfg, get_api_rules
from hoteldata.domains.collect.windows import (
    ALL_WINDOWS,
    DEFAULT_WINDOW,
    normalize_window,
)

__all__ = ["CollectorStats", "DatacenterExtractor", "ModuleTask", "resolve_targets"]

ProgressFn = Callable[[ExtractResult], None]


@dataclass(frozen=True, slots=True)
class ModuleTask:
    """一个「页 × 模块」及其要采的窗口集合。"""

    page: str
    module: str
    windows: tuple[str, ...]
    #: ``pick_first=True`` → 首个非空 payload 即止(旧 ``pick_first`` 语义)
    pick_first: bool = False

    @property
    def key(self) -> str:
        return f"{self.page}/{self.module}"


@dataclass(slots=True)
class CollectorStats:
    """一次批量提取的统计。"""

    total: int = 0
    ok: int = 0
    degraded: int = 0
    no_data: int = 0
    failed: int = 0
    api: int = 0
    browser: int = 0
    indicators: int = 0
    modules_failed: list[str] = field(default_factory=list)

    def add(self, result: ExtractResult) -> None:
        self.total += 1
        setattr(self, result.status, getattr(self, result.status) + 1)
        if result.channel == "browser":
            self.browser += 1
        else:
            self.api += 1
        self.indicators += result.record_count
        if result.status == "failed":
            label = (
                f"{result.target.page}/{result.target.module}/{result.target.window}"
                if result.target
                else "?"
            )
            self.modules_failed.append(label)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "ok": self.ok,
            "degraded": self.degraded,
            "no_data": self.no_data,
            "failed": self.failed,
            "channel_api": self.api,
            "channel_browser": self.browser,
            "indicators": self.indicators,
            "modules_failed": self.modules_failed[:20],
            # ★ no_data 不计失败
            "success_rate": round((self.total - self.failed) / self.total, 4) if self.total else None,
        }


# ---------------------------------------------------------------------------
# 目标解析
# ---------------------------------------------------------------------------


def resolve_targets(
    rules: ApiRules,
    *,
    page: str | None = None,
    module: str | None = None,
    window: str | None = None,
    all_windows: bool = False,
    rotation: bool = False,
    day: date | None = None,
    include_fixed_daily: bool = True,
) -> list[ModuleTask]:
    """把 CLI 参数解析成「页 × 模块 × 窗口集合」清单。

    ================  ==================================================
    模式              结果
    ================  ==================================================
    ``rotation``      当日轮换项 + ``fixed_daily``(去重)
    ``page+module``   该模块(窗口 = 参数 / 规则声明)
    ``page``          该页全部子模块
    ``module``        跨页定位该模块
    默认              全部 8 页 × 24 子模块
    ================  ==================================================
    """
    tasks: list[ModuleTask] = []

    def windows_for(sub: SubModuleCfg) -> tuple[str, ...]:
        if window:
            return (normalize_window(window),)
        if all_windows:
            return tuple(ALL_WINDOWS)
        declared = [normalize_window(w) for w in (sub.windows or [DEFAULT_WINDOW])]
        return tuple(declared or [DEFAULT_WINDOW])

    if rotation:
        plan = get_rotation().pick(day or date.today())
        names = plan.effective_modules() if include_fixed_daily else plan.module_names
        for name in names:
            located = rules.locate_module(name)
            if located is None:
                continue
            pname, sub = located
            tasks.append(ModuleTask(pname, sub.name, windows_for(sub)))
        return tasks

    if module:
        if page:
            sub = rules.sub_module(page, module)
            return [ModuleTask(page, sub.name, windows_for(sub))]
        located = rules.locate_module(module)
        if located is None:
            from hoteldata.domains.collect.rules import ApiRulesError

            raise ApiRulesError(f"找不到模块 {module!r};已知 24 个:{rules.known_sub_module_names()}")
        pname, sub = located
        return [ModuleTask(pname, sub.name, windows_for(sub))]

    pages = [page] if page else list(rules.pages)
    for pname in pages:
        cfg = rules.page(pname)
        for sub in cfg.sub_modules:
            tasks.append(ModuleTask(pname, sub.name, windows_for(sub)))
    return tasks


# ---------------------------------------------------------------------------
# 提取器
# ---------------------------------------------------------------------------


class DatacenterExtractor:
    """数据中心提取器(实现 :class:`~hoteldata.domains.collect.contract.Extractor`)。

    ``name = "datacenter"``(契约要求的四个名字之一)。
    """

    name = "datacenter"

    def __init__(
        self,
        rules: ApiRules | None = None,
        *,
        pool: Any = None,
        ensure_login: Any = None,
        allow_browser: bool = True,
    ) -> None:
        self.rules = rules or get_api_rules()
        self.pool = pool
        self.ensure_login = ensure_login
        self.allow_browser = allow_browser and pool is not None

    # ------------------------------------------------------------------

    def build_orchestrator(self, ctx: ExtractContext, handle: Any) -> ChannelOrchestrator:
        api = ApiChannel(ctx=ctx, rules=self.rules)
        browser = None
        if self.allow_browser and self.pool is not None:
            browser = BrowserChannel(
                ctx,
                self.rules,
                pool=self.pool,
                handle=handle,
                ensure_login=self.ensure_login,
            )
        return ChannelOrchestrator(
            ctx=ctx,
            rules=self.rules,
            api=api,
            browser=browser,
            ensure_login=self.ensure_login,
            allow_browser=self.allow_browser,
        )

    # ------------------------------------------------------------------

    async def extract(
        self,
        ctx: ExtractContext,
        *,
        handle: Any = None,
        targets: Sequence[ModuleTask] | None = None,
        page: str | None = None,
        module: str | None = None,
        window: str | None = None,
        all_windows: bool = False,
        rotation: bool = False,
        day: date | None = None,
        repo: CollectRepository | None = None,
        on_progress: ProgressFn | None = None,
        stats: CollectorStats | None = None,
    ) -> list[ExtractResult]:
        """★ 执行提取。**单模块失败不阻断整批。**"""
        handle = handle or ctx.session
        tasks = list(
            targets
            if targets is not None
            else resolve_targets(
                self.rules,
                page=page,
                module=module,
                window=window,
                all_windows=all_windows,
                rotation=rotation,
                day=day,
            )
        )
        stats = stats or CollectorStats()
        orch = self.build_orchestrator(ctx, handle)
        results: list[ExtractResult] = []

        for task in tasks:
            sub = self.rules.sub_module(task.page, task.module)
            for win in task.windows:
                try:
                    outcome = await orch.extract(task.page, sub, win)
                    result = outcome.result
                except Exception as exc:  # noqa: BLE001 - 兜底:连编排都炸了
                    logger.exception("[{}|{}|{}] 提取编排异常", task.page, task.module, win)
                    result = ExtractResult(
                        status="failed",
                        channel="api",
                        payload={},
                        error=f"编排异常: {exc}",
                    )
                results.append(result)
                stats.add(result)
                if repo is not None:
                    await self.persist(ctx, result, repo)
                if on_progress is not None:
                    on_progress(result)
        return results

    # ------------------------------------------------------------------

    @staticmethod
    async def persist(ctx: ExtractContext, result: ExtractResult, repo: CollectRepository) -> int:
        """落库(**UPSERT 幂等**;同日重跑即覆盖,行数不增)。"""
        if result.target is None:
            return 0
        return await repo.upsert_module(
            hotel_id=ctx.hotel_id,
            account_id=ctx.account_id,
            collect_date=ctx.collect_date,
            page=result.target.page,
            module=result.target.module,
            window=result.target.window,
            payload=result.payload or {},
            raw_json_path=result.raw_path,
            channel=result.channel,
            status=result.status,
            error=result.error,
        )

    # ------------------------------------------------------------------

    async def extract_batch(
        self,
        contexts: Sequence[ExtractContext],
        *,
        handle_for: Callable[[ExtractContext], Any],
        repo_for: Callable[[ExtractContext], CollectRepository],
        on_progress: ProgressFn | None = None,
        max_concurrent: int = 4,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """★ **跨账号并发**(``asyncio.Semaphore``)+ **账号内串行**。

        平台级 0.6s 限频由 ``ctx.limiter`` 统一保证 —— 并发再高也超不过总速率。
        """
        import asyncio

        sem = asyncio.Semaphore(max(1, max_concurrent))
        stats = CollectorStats()
        per_hotel: dict[str, dict[str, Any]] = {}
        lock = asyncio.Lock()

        async def _one(c: ExtractContext) -> None:
            async with sem:
                try:
                    results = await self.extract(
                        c,
                        handle=handle_for(c),
                        repo=repo_for(c),
                        on_progress=on_progress,
                        stats=stats,
                        **kwargs,
                    )
                    summary = aggregate_results(results)
                except Exception as exc:  # noqa: BLE001 - 单店失败不阻断
                    logger.exception("酒店 {} 批量提取失败", c.hotel.name)
                    summary = {"status": "failed", "error": str(exc), "total": 0}
                async with lock:
                    per_hotel[c.hotel.name] = summary

        await asyncio.gather(*(_one(c) for c in contexts))
        return {
            "hotels": len(contexts),
            "stats": stats.as_dict(),
            "per_hotel": per_hotel,
        }
