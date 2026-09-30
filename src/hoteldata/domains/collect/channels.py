"""★ 双通道编排与四态汇总(T3.5)。

::

    extract_with_fallback(sub_module, window, ctx)
      │
      ├─ channel = "api"                       ← 主通道(单店 ≤30s)
      │   ├─ 取 cookie 头(session_store)
      │   ├─ 展开窗口 → 日期区间(compute_window_range)
      │   ├─ ★ 占位符守卫:模板里若有 {xxx} 未解析 → PlaceholderError(降级)
      │   ├─ 逐接口重放(限频 0.6s + 超时 20s + 仅 GET 重试)
      │   ├─ 按 fields[].path 提取(json_get)
      │   └─ 汇总四态:全成功=ok / 部分失败=degraded / no_data_path 命中=no_data
      │
      └─ 异常 → channel = "browser"(**无条件降级**)
          ├─ 导航到 sub_module.url(60s)
          ├─ 等数据就绪(120s)/ 等图表渲染(8s)
          ├─ DOM 提取(css: / js: / text: / table:)
          └─ 汇总:有数据=degraded / 无数据=no_data / 抛异常=failed

**顶层汇总规则(照抄)**:``failed`` > ``degraded`` > (``ok``/``no_data``)。
**``no_data`` 视为正常** —— 顶层状态永远不会是 ``no_data``。

> ⚠️ 旧系统硬编码 ``try API / except 降级``(``ebooking.py:762-781``),**没有开关**;
> 文档里的 ``channel=auto/api/browser`` 这个"开关"**在代码里根本不存在**(D 级丢弃项)。
> 新实现保留"无开关"的语义:分流永远是 **API 优先 → 异常即降级**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from hoteldata.domains.collect.contract import (
    ExtractContext,
    ExtractResult,
    ExtractStatus,
    LoginExpiredError,
    worst_status,
)
from hoteldata.domains.collect.rules import ApiRules, SubModuleCfg

__all__ = ["ChannelOrchestrator", "ExtractOutcome", "aggregate_results"]


def aggregate_results(results: list[ExtractResult]) -> dict[str, Any]:
    """批量汇总(顶层四态 + 计数)。**``no_data`` 不计失败。**"""
    statuses = [r.status for r in results]
    counts: dict[str, int] = {s: 0 for s in ("ok", "degraded", "no_data", "failed")}
    for s in statuses:
        counts[s] = counts.get(s, 0) + 1
    by_channel: dict[str, int] = {}
    for r in results:
        by_channel[r.channel] = by_channel.get(r.channel, 0) + 1
    return {
        "status": worst_status(statuses),
        "total": len(results),
        "counts": counts,
        "by_channel": by_channel,
        "failed": counts.get("failed", 0),
        "indicators": sum(r.record_count for r in results),
    }


@dataclass(slots=True)
class ExtractOutcome:
    """一次「模块 × 窗口」的编排结果 + 过程证据。"""

    result: ExtractResult
    api_error: str | None = None
    api_exception: str | None = None
    degraded_to_browser: bool = False
    relogin_attempted: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.result.summary(),
            "api_error": self.api_error,
            "api_exception": self.api_exception,
            "degraded_to_browser": self.degraded_to_browser,
            "relogin_attempted": self.relogin_attempted,
        }


@dataclass(slots=True)
class ChannelOrchestrator:
    """把 API 通道与浏览器通道串起来,产出**唯一的** :class:`ExtractResult`。

    ``api`` / ``browser`` 任一为 ``None`` 表示该通道不可用(例如离线只跑 API、
    或没有浏览器池时的纯 API 模式)—— 此时若 API 失败,**如实报错**,不假装成功。
    """

    ctx: ExtractContext
    rules: ApiRules
    api: Any = None
    browser: Any = None
    #: 登录失效时的异步回调 ``async (handle) -> bool``(由外部注入登录管家,
    #: 避免域之间互相 import —— 硬约束 2)
    ensure_login: Any = None
    #: 是否允许降级到浏览器
    allow_browser: bool = True
    #: 逐次采集的过程记录(CLI/自检用)
    history: list[dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------------

    async def extract(
        self,
        page_name: str,
        sub_module: SubModuleCfg,
        window: str,
    ) -> ExtractOutcome:
        """★ 单模块双通道提取(不抛异常 —— 失败落 ``failed`` 四态)。"""
        api_error: str | None = None
        api_exception: str | None = None
        degraded = False
        relogin = False

        # ---------------- ① API 主通道 ----------------
        if self.api is not None:
            try:
                result = await self.api.collect_sub_module(
                    page_name, self.rules.page(page_name), sub_module, window
                )
                outcome = ExtractOutcome(result=result)
                self.history.append(outcome.as_dict())
                return outcome
            except LoginExpiredError as exc:
                api_exception = "LoginExpiredError"
                api_error = str(exc)
                logger.warning("[{}|{}|{}] 登录失效: {}", page_name, sub_module.name, window, exc)
                # 触发重登(串行;失败也不阻断降级)
                if self.ensure_login is not None:
                    relogin = True
                    try:
                        ok = await self.ensure_login(self.ctx.session)
                        if ok:
                            # 重登成功后**再给 API 一次机会**(不无限重试)
                            result = await self.api.collect_sub_module(
                                page_name, self.rules.page(page_name), sub_module, window
                            )
                            outcome = ExtractOutcome(result=result, relogin_attempted=True)
                            self.history.append(outcome.as_dict())
                            return outcome
                    except Exception as retry_exc:  # noqa: BLE001
                        api_exception = type(retry_exc).__name__
                        api_error = f"重登后重试仍失败: {retry_exc}"
                        logger.warning(
                            "[{}|{}|{}] 重登后重试失败: {}",
                            page_name,
                            sub_module.name,
                            window,
                            retry_exc,
                        )
            except Exception as exc:  # noqa: BLE001 - ★ 无条件降级(与旧系统语义一致)
                api_exception = type(exc).__name__
                api_error = str(exc)
                logger.info(
                    "[{}|{}|{}] API 通道未成功({}),降级浏览器: {}",
                    page_name,
                    sub_module.name,
                    window,
                    api_exception,
                    api_error,
                )
        else:
            api_error = "API 通道未装配"
            api_exception = "NoApiChannel"

        # ---------------- ② 浏览器兜底(无条件) ----------------
        if self.browser is None or not self.allow_browser:
            result = ExtractResult(
                status="failed",
                channel="api",
                payload={},
                error=f"API 通道失败且浏览器兜底不可用: {api_error}",
                detail={"api_exception": api_exception, "api_error": api_error},
            )
            outcome = ExtractOutcome(
                result=result,
                api_error=api_error,
                api_exception=api_exception,
                relogin_attempted=relogin,
            )
            self.history.append(outcome.as_dict())
            return outcome

        degraded = True
        result = await self.browser.collect_sub_module(
            page_name, self.rules.page(page_name), sub_module, window
        )
        # 兜底结果若与 API 错误都带上,便于排障
        if result.detail is None:
            result.detail = {}
        result.detail.setdefault("api_error", api_error)
        result.detail.setdefault("api_exception", api_exception)
        outcome = ExtractOutcome(
            result=result,
            api_error=api_error,
            api_exception=api_exception,
            degraded_to_browser=degraded,
            relogin_attempted=relogin,
        )
        self.history.append(outcome.as_dict())
        return outcome

    # ------------------------------------------------------------------

    async def extract_many(
        self,
        page_name: str,
        sub_module: SubModuleCfg,
        windows: list[str],
        *,
        pick_first: bool = False,
    ) -> list[ExtractResult]:
        """多窗口采集。``pick_first=True`` 时「首个 ``payload`` 非空即止」(旧语义)。"""
        out: list[ExtractResult] = []
        for window in windows:
            outcome = await self.extract(page_name, sub_module, window)
            out.append(outcome.result)
            if pick_first and outcome.result.payload:
                break
        return out

    def summary(self) -> dict[str, Any]:
        """本次编排的过程汇总(不重建 ExtractResult,只统计 history)。"""
        return {
            "runs": len(self.history),
            "ok": sum(1 for h in self.history if h.get("status") == "ok"),
            "degraded": sum(1 for h in self.history if h.get("status") == "degraded"),
            "no_data": sum(1 for h in self.history if h.get("status") == "no_data"),
            "failed": sum(1 for h in self.history if h.get("status") == "failed"),
            "browser_fallbacks": sum(1 for h in self.history if h.get("degraded_to_browser")),
            "relogins": sum(1 for h in self.history if h.get("relogin_attempted")),
        }


def status_is_failure(status: ExtractStatus) -> bool:
    """★ 只有 ``failed`` 算失败(``no_data`` 不算)。"""
    return status == "failed"
