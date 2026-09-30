"""单店比价编排(段3 T3E.1)—— 锚点 → 候选 → 取价 → 报告 → 存档。

★ 本模块**不新建任何基础设施**
============================

计划书 §1.3 的复用清单是硬约束。这里全部走段1 的东西:

=========================================  ==================================================
需要的能力                                   用谁
=========================================  ==================================================
打开浏览器 / 管 context                      ``infra/browser.py`` 浏览器池
登录态读写                                  ``infra/session_store.py``(``role='ota'`` / ``'ota_meituan'``)
请求限频                                    ``infra/rate_limit.py``
产物落盘与相对路径                          ``infra/paths.py``
真人操作节奏                                ``domains/compare/human.py``(段3 移植,见其文档)
=========================================  ==================================================

★ 跨平台合并:**只归一化去重,绝不合并价格**(段3 P3b)
==================================================

旧 ``runner.py:85-115`` 的 ``_merge_quotes`` 把**跨平台同名酒店合并成一条**,
并且 ``if old.get("price") is None`` —— **只保留先到的那个平台价**,
第二个平台的价被丢弃。**这与"比价"的语义正好相反**:
比价的产出物本身就是"同一家店在两个平台各是多少"。

段3 的合并只做一件事:**把两个平台的结果放在一起**(平台信息挂在 quote 的 ``raw`` 上),
价格**各自保留**。V84 专测这一点。

★ 判重与力度的分工
================

* ``should_run`` —— **以数据为准**:"今天有没有真数据"(B27),不是"今天跑没跑";
* ``force`` —— 显式绕过(**CLI 的 ``--force`` 才用它**)。

旧系统的 D8 根因就是 ``pusher.py:568`` 的 ``run_price_collect``
**硬编码 ``force=True``** —— 于是判重逻辑永远被绕过,库里攒出 6 组重复。
段3 把这个 ``force`` 变成**只有人来按的开关**。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from loguru import logger

from hoteldata.domains.compare import geo
from hoteldata.domains.compare.contract import (
    AnchorRef,
    CompareResult,
    HotelQuote,
    PriceFatalError,
    PriceRetryableError,
    QuotesPage,
)
from hoteldata.domains.compare.human import norm_hotel_name
from hoteldata.domains.compare.registry import create_platform, resolve_platforms
from hoteldata.domains.compare.report import (
    ReportBundle,
    build_html,
    build_markdown,
    build_price_text,
)
from hoteldata.domains.compare.repository import CompareRepository, query_slot_of
from hoteldata.domains.compare.vision import VisionEstimator, cross_check
from hoteldata.infra.paths import Layout, safe_name, stamp
from hoteldata.infra.session_store import SessionHandle
from hoteldata.settings import Settings, get_settings

__all__ = ["CompareRunner", "PlatformOutcome", "QueryContext"]


# ---------------------------------------------------------------------------
# 平台执行上下文(平台方法拿到的 ``ctx``)
# ---------------------------------------------------------------------------


@dataclass
class QueryContext:
    """一次查询的运行上下文(注入给平台;平台**不直接碰 Runtime**)。

    ★ 这样切的理由:平台实现要能被**离线夹具**驱动(验收时用录制响应跑真实代码路径),
      如果它直接 ``runtime.browser`` / ``runtime.settings``,就没法在不启浏览器的情况下测。

    ★ ``page_session()`` 是**同步方法返回异步上下文管理器** ——
      调用点写 ``async with ctx.page_session() as page:``,与段1 浏览器池的用法一致。
    """

    settings: Settings
    layout: Layout
    handle: SessionHandle
    pool: Any
    limiter: Any
    http: Any
    query_date: date
    platform: str
    #: 单次运行内的键值缓存(城市 id 等)—— 用完即弃,不做类级可变状态
    cache: dict[str, Any] = field(default_factory=dict)

    # ---- 供平台用的便捷入口 ----

    @property
    def nav_timeout_s(self) -> float:
        return float(self.settings.nav_timeout_s)

    @property
    def page_ready_timeout_s(self) -> float:
        return float(self.settings.page_ready_timeout_s)

    def page_session(self) -> Any:
        """开一个页面会话(段1 浏览器池的三元组 ``page_session``)。"""
        return self.pool.page_session(self.handle)

    def cache_get(self, kind: str, key: str) -> Any:
        return self.cache.get((kind, key))

    def cache_set(self, kind: str, key: str, value: Any) -> None:
        self.cache[(kind, key)] = value

    async def http_text(self, url: str) -> str:
        """取文本(城市 sitemap 等);**失败返回空串**,由调用方决定语义。

        ★ 段1 的 :class:`~hoteldata.infra.http.HttpClient` **没有** ``get()`` ——
          它的唯一入口是 :meth:`HttpClient.request`,返回的是
          :class:`~hoteldata.infra.http.HttpAttempt`(含状态码/文本/错误,便于逐接口落盘)。
          段3 一开始按 httpx 的习惯写了 ``await self.http.get(url)``,
          结果**只在"未登记 ebk_hotel_id 的酒店"这条兜底路径上**炸
          (而 4 家生产酒店都登记了 id,所以一直没暴露)——
          这正是"兜底路径没人走,所以没人发现它坏了"的典型。
        """
        try:
            # ★ 限频:即使是非浏览器请求也走平台级限频(风控按 IP 算)
            async with self.limiter.request(self.platform, f"{self.platform}_web"):
                attempt = await self.http.request("GET", url, follow_redirects=True)
            if not attempt.is_success:
                logger.warning(
                    "[{}] 取文本失败 {}: status={} error={}",
                    self.platform,
                    url[:80],
                    attempt.status_code,
                    attempt.error,
                )
                return ""
            return attempt.text or ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("[{}] 取文本失败 {}: {}", self.platform, url[:80], exc)
            return ""

    async def persist_capture(
        self, platform: str, anchor_name: str, captured: list[dict[str, Any]]
    ) -> str | None:
        """把原始响应**全量**落盘(段3 P10)。返回**相对项目根**的路径。"""
        if not captured:
            return None
        path = (
            self.layout.hotel_day_dir(f"compare_{platform}", self.query_date)
            / f"capture_{safe_name(anchor_name, max_len=40)}_{stamp()}.json"
        )
        try:
            import json

            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(captured, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
            )
            return self.layout.to_relative(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[{}] 原始响应落盘失败: {}", platform, exc)
            return None


# ---------------------------------------------------------------------------
# 单个平台的结果
# ---------------------------------------------------------------------------


@dataclass
class PlatformOutcome:
    """一个平台一次比价的结果(成功或失败,**都保留**)。"""

    platform: str
    anchor: AnchorRef | None = None
    quotes: list[HotelQuote] = field(default_factory=list)
    slot: str = ""
    degraded: bool = False
    notes: list[str] = field(default_factory=list)
    error: str | None = None
    #: 错误分级(三态异常的类名),用于"失败可见"(段3 §4.3)
    error_kind: str = ""
    raw_json_path: str | None = None
    price_source: str = "dom"

    @property
    def ok(self) -> bool:
        return self.error is None


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class CompareRunner:
    """单店比价编排器。"""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        layout: Layout,
        sessions: Any,
        pool: Any = None,
        limiter: Any = None,
        http: Any = None,
        repo: CompareRepository | None = None,
        vision: VisionEstimator | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.cfg = self.settings.compare
        self.layout = layout
        self.sessions = sessions
        self.pool = pool
        self.limiter = limiter
        self.http = http
        self.repo = repo
        self.vision = vision or VisionEstimator(self.settings, client=http)
        #: 每个平台**最多**试几次(只对 :class:`PriceRetryableError` 生效)
        self.retry_times = int(self.settings.retry_times)
        self.retry_backoff = tuple(self.settings.retry_backoff_s)

    # ==================================================================
    # 会话角色
    # ==================================================================

    def session_handle(self, platform: str) -> SessionHandle:
        """比价用**前台**登录态:``role='ota'``(携程)/ ``'ota_meituan'``(美团)。

        ★ 这两个值**不是段3 新起的名字** —— 段1 建 ``sessions`` 表时就写明了
          (``infra/models/core.py:112-113``),实测库里也已存在这两条登录态。
        """
        role = "ota" if platform == "ctrip" else "ota_meituan"
        alias = "ctrip" if platform == "ctrip" else "meituan"
        try:
            return self.sessions.handle(platform, role, alias)
        except Exception:  # noqa: BLE001 - 没配任何该角色的登录态
            return self.sessions.handle(platform, role, "default")

    # ==================================================================
    # 单店
    # ==================================================================

    async def compare_one(
        self,
        anchor_name: str,
        *,
        city: str | None = None,
        platforms: Iterable[str] | None = None,
        nights: int = 1,
        quote_count: int | None = None,
        nearby_count: int | None = None,
        ebk_hotel_id: str | None = None,
        hotel_id: int | None = None,
        query_date: date | None = None,
        slot: str | None = None,
        demo: bool = False,
        persist: bool = True,
        write_report: bool = True,
    ) -> CompareResult:
        """★ 比价一家店(**双平台**),返回 :class:`CompareResult`。

        单平台的失败**不阻断**另一个平台(返回结果里 ``notes`` 记下),
        只有**两个平台都失败**才抛异常 —— 这与旧批量语义
        「单店失败不阻断整批」(附录 B)是同一条纪律的下沉。
        """
        day = query_date or datetime.now(self.settings.tzinfo).date()
        slot = slot or query_slot_of()
        names = resolve_platforms(list(platforms) if platforms else list(self.cfg.platforms))
        rooms = quote_count or self.cfg.quote_count
        want = nearby_count or self.cfg.nearby_count

        # ★ 取 2N:旧 ``runner.py:162`` 的"取 2N 后合并截 N"——
        #   多取一倍应对跨平台去重与"锚点自己也在列表里"的损耗。
        fetch = max(want * 2, want)

        city_hint = (city or self.cfg.city or "").strip() or None

        logger.info(
            "开始比价:{} | 城市={} | 平台={} | 取 {} 家(截 {}) | slot={}",
            anchor_name,
            city_hint or "(未指定)",
            names,
            fetch,
            want,
            slot,
        )

        outcomes: list[PlatformOutcome] = []
        # ★ 平台**串行**:平台级 0.6s 限频是总闸,并发只会让风控更容易注意到
        #   (而且两个平台共用一个出口 IP)
        for plat_name in names:
            if demo:
                outcomes.append(self._demo_outcome(plat_name, anchor_name, city_hint, want))
                continue
            outcomes.append(
                await self._compare_platform(
                    plat_name,
                    anchor_name,
                    city=city_hint,
                    nights=nights,
                    fetch=fetch,
                    rooms=rooms,
                    day=day,
                    ebk_hotel_id=ebk_hotel_id,
                    demo=demo,
                )
            )

        ok = [o for o in outcomes if o.ok]
        if not ok:
            # ★ 「都失败」是**不可重试**的终止态(重试已在平台层按类型做过)——
            #   抛出时带上每个平台的**原因**,而不是一句"比价失败"。
            detail = "; ".join(f"{o.platform}({o.error_kind or 'Error'})={o.error}" for o in outcomes)
            raise PriceFatalError(f"「{anchor_name}」所有平台都未取到数据:{detail or '无可用平台'}")

        merged, anchor = self._merge(outcomes, anchor_name, want)
        errors = {o.platform: (o.error or "") for o in outcomes if not o.ok}
        notes = [f"[{o.platform}] {n}" for o in ok for n in o.notes if n]

        result = CompareResult(
            platform=ok[0].platform,  # type: ignore[arg-type]
            anchor=anchor,
            quotes=merged,
            query_slot=slot,
            query_date=day,
            nights=nights,
            degraded=any(o.degraded for o in ok),
            notes=notes,
        )
        # 平台级错误挂进 notes(报告要显示"哪个平台没拿到")
        for plat, err in errors.items():
            result.notes.append(f"[{plat}] 采集失败:{err}")

        if write_report:
            bundle = self.write_report(result, errors=errors, city=city_hint)
            result.notes.append(f"报告:{bundle.md_path}")
        if persist:
            await self.save(result, city=city_hint, hotel_id=hotel_id, demo=demo)
        return result

    async def _compare_platform(
        self,
        platform: str,
        anchor_name: str,
        *,
        city: str | None,
        nights: int,
        fetch: int,
        rooms: int,
        day: date,
        ebk_hotel_id: str | None,
        demo: bool,
    ) -> PlatformOutcome:
        """跑一个平台,带**按异常类型的重试策略**(段3 三态异常)。"""
        plat = create_platform(platform)
        handle = self.session_handle(platform)
        ctx = QueryContext(
            settings=self.settings,
            layout=self.layout,
            handle=handle,
            pool=self.pool,
            limiter=self.limiter,
            http=self.http,
            query_date=day,
            platform=platform,
        )
        out = PlatformOutcome(platform=platform)

        attempt = 0
        while True:
            attempt += 1
            try:
                anchor = await plat.resolve_anchor(
                    ctx, anchor_name, city, hotel_id=ebk_hotel_id
                )
                page: QuotesPage = await plat.collect_quotes(
                    ctx, anchor, fetch, nights=nights, rooms=rooms
                )
                out.anchor = anchor
                out.quotes = list(page.quotes)
                out.degraded = page.degraded
                out.notes = list(page.notes)
                out.raw_json_path = page.raw_json_path
                out.price_source = page.price_source
                logger.info("[{}] 取到 {} 条候选", platform, len(out.quotes))
                return out
            except PriceRetryableError as exc:
                # ★ 只有这一类才重试 —— 且**有上限、有退避**
                if attempt > self.retry_times:
                    out.error = str(exc)
                    out.error_kind = type(exc).__name__
                    logger.error("[{}] 重试 {} 次后仍失败:{}", platform, self.retry_times, exc)
                    return out
                wait = self.retry_backoff[min(attempt - 1, len(self.retry_backoff) - 1)]
                logger.warning(
                    "[{}] 可重试失败(第 {}/{} 次):{} —— {}s 后重试",
                    platform,
                    attempt,
                    self.retry_times + 1,
                    exc,
                    wait,
                )
                await asyncio.sleep(wait)
            except Exception as exc:  # noqa: BLE001
                # ★ 不可重试(含 HumanVerificationError)—— **明确记录类型**,不吞成字符串
                out.error = str(exc)
                out.error_kind = type(exc).__name__
                level = logger.error if isinstance(exc, PriceFatalError) else logger.warning
                level("[{}] 取价失败({}):{}", platform, out.error_kind, exc)
                return out

    # ==================================================================
    # 合并(★ 只归一化去重,绝不合并价格 —— P3b)
    # ==================================================================

    def _merge(
        self, outcomes: list[PlatformOutcome], anchor_name: str, want: int
    ) -> tuple[list[HotelQuote], AnchorRef]:
        """把各平台结果合到一起。

        ★ **每个平台的报价独立保留**(V84)。同名酒店在携程 / 美团各有一行,
          这**正是比价的产出物** —— 旧系统把它们压成一条、只留先到的价,
          与"比价"的语义相反(段3 P3b)。

        排序:``HOTEL_RANK_MODE=geo`` → 距离升序(``None`` 垫底);
        否则按平台原始顺序(平台内部已经排过)。
        """
        anchor = outcomes[0].anchor or AnchorRef(name=anchor_name)
        for o in outcomes:
            if o.anchor and o.anchor.self_price is not None:
                anchor = o.anchor
                break

        merged: list[HotelQuote] = []
        seen: set[tuple[str, str]] = set()
        for o in outcomes:
            if not o.ok:
                continue
            for q in o.quotes:
                # 去重键 = (平台, 归一化酒店名) —— **跨平台不去重**(那正是要比的东西)
                key = (o.platform, norm_hotel_name(q.hotel_name) or q.hotel_name)
                if key in seen:
                    continue
                seen.add(key)
                # 平台信息挂进 raw(报告与推送都要按平台分组)
                raw = dict(q.raw or {})
                raw["platform"] = o.platform
                merged.append(q.model_copy(update={"raw": raw}))

        if self.cfg.by_geo:
            merged = sorted(
                merged,
                key=lambda q: (
                    q.distance_km is None,
                    q.distance_km if q.distance_km is not None else 1e9,
                ),
            )
        else:
            # 平台推荐顺序:**平台内保持原序**,平台间按配置顺序拼接
            order = {o.platform: i for i, o in enumerate(outcomes)}
            merged = sorted(merged, key=lambda q: order.get(str((q.raw or {}).get("platform")), 99))

        # ★ 截断按"每家平台 want 条"算,否则 geo 排序会让一个平台把另一个挤没
        per_platform: dict[str, int] = {}
        kept: list[HotelQuote] = []
        for q in merged:
            plat = str((q.raw or {}).get("platform") or "")
            count = per_platform.get(plat, 0)
            if count >= want:
                continue
            per_platform[plat] = count + 1
            kept.append(q)

        if not self.cfg.by_geo and any(q.distance_km is None for q in kept):
            logger.info("部分条目无距离,报告将标注「距离不可用」")
        return kept, anchor

    # ==================================================================
    # 报告
    # ==================================================================

    def report_paths(self, anchor_name: str, day: date, slot: str) -> tuple[Path, Path]:
        """报告落 ``var/reports/compare/<锚点>/``(经 ``infra/paths.py``,不自己拼)。"""
        base = self.layout.reports_dir / "compare" / safe_name(anchor_name, max_len=60)
        base.mkdir(parents=True, exist_ok=True)
        tail = slot.rsplit("-", 1)[-1] if slot else stamp()
        return base / f"comparison_{day:%Y%m%d}_{tail}.md", base / f"comparison_{day:%Y%m%d}_{tail}.html"

    def write_report(
        self, result: CompareResult, *, errors: dict[str, str] | None = None, city: str | None = None
    ) -> ReportBundle:
        """渲染并落盘 md + html(V76 / V77)。"""
        day = result.query_date or datetime.now(self.settings.tzinfo).date()
        md_path, html_path = self.report_paths(result.anchor.name, day, result.query_slot)
        common = {
            "anchor_name": result.anchor.name,
            "query_date": day,
            "nights": result.nights,
            "city": city or result.anchor.city,
            "quotes": result.quotes,
            "anchor": result.anchor,
            "slot": result.query_slot,
            "notes": result.notes,
            "errors": errors,
        }
        markdown = build_markdown(**common)
        html = build_html(**common)
        try:
            md_path.write_text(markdown, encoding="utf-8")
            html_path.write_text(html, encoding="utf-8")
        except OSError as exc:
            logger.error("报告落盘失败 {}: {}", md_path, exc)
        return ReportBundle(
            markdown=markdown,
            html=html,
            md_path=self.layout.to_relative(md_path),
            html_path=self.layout.to_relative(html_path),
            meta={"slot": result.query_slot, "quotes": len(result.quotes)},
        )

    # ==================================================================
    # 存档
    # ==================================================================

    async def save(
        self,
        result: CompareResult,
        *,
        city: str | None = None,
        hotel_id: int | None = None,
        demo: bool = False,
        session: Any = None,
    ) -> int:
        """UPSERT 存档(T3D.2)。

        ★ 按平台分组写入 —— 因为 ``cmp_price_key`` 唯一键里含 ``platform``。
        """
        day = result.query_date or datetime.now(self.settings.tzinfo).date()

        async def _do(sess: Any) -> int:
            repo = CompareRepository(sess)
            total = 0
            by_platform: dict[str, list[dict[str, Any]]] = {}
            for q in result.quotes:
                plat = str((q.raw or {}).get("platform") or result.platform)
                by_platform.setdefault(plat, []).append(
                    {
                        "hotel_name": q.hotel_name,
                        "room_type": q.room_type,
                        "price": q.price,
                        "distance_km": q.distance_km,
                        "coord_source": q.coord_source,
                        "price_source": q.price_source,
                        "price_scope": q.price_scope,
                        "need_manual_check": q.need_manual_check,
                        "degraded": q.degraded,
                        "price_rejected": q.price_rejected,
                        "url": q.url,
                        "score": q.score,
                        "reviews": q.reviews,
                        "raw_json_path": None,
                    }
                )
            for plat, rows in by_platform.items():
                total += await repo.save_quotes(
                    anchor_name=result.anchor.name,
                    platform=plat,
                    query_date=day,
                    rows=rows,
                    hotel_id=hotel_id,
                    city=city or result.anchor.city,
                    nights=result.nights,
                    query_slot=result.query_slot,
                    is_demo=demo,
                )
            return total

        if session is not None:
            return await _do(session)
        if self.repo is not None:
            return await _do(self.repo.session)
        raise RuntimeError("CompareRunner.save() 需要传入 session 或 repo")

    # ==================================================================
    # 演示模式(离线;不碰浏览器、不碰平台)
    # ==================================================================

    def _demo_outcome(
        self, platform: str, anchor_name: str, city: str | None, want: int
    ) -> PlatformOutcome:
        """★ 演示数据 —— **离线可跑**,用于验收与"没有登录态时看一眼报告长什么样"。

        三条纪律(与旧系统 ``_demo_payload`` / ``_demo_one`` 的差别):

        1. **不伪装成真数据**:每行落库时 ``is_demo=True``(显式列),
           ``should_run_batch`` 按 ``is_demo=false`` 判"今天有没有真数据",
           所以演示数据**不会**让批量误以为今天跑过了;
        2. 酒店名带 ``compare_demo_prefix``(默认 ``验收3-``),
           与段1/段2 的 ``verify2-`` 同一纪律,便于一眼识别与批量清理;
        3. 距离**真的算出来**(演示也要体现 D13 修好了),而不是写死 ``None``。
        """
        prefix = self.cfg.demo_prefix or "验收3-"
        anchor_coords = (37.165587, 119.946814)
        # (名称后缀, 价格, 距离公里) —— 距离用于反推坐标,让 haversine 结果可核对
        seeds = [
            ("示例轻奢酒店", 103.0, 0.9),
            ("示例商务宾馆", 80.0, 1.4),
            ("示例影宿公寓", 117.0, 2.1),
        ]
        quotes: list[HotelQuote] = []
        for i, (suffix, price, km) in enumerate(seeds[: max(1, want)], 1):
            # 纬度每 0.01° ≈ 1.112 km —— 用这个比例反推一个"距离正好是 km"的坐标
            delta = km * 0.01 / 1.112
            quotes.append(
                HotelQuote(
                    hotel_name=f"{prefix}{suffix}{i}",
                    price=price,
                    price_scope="from",
                    price_source="dom",
                    distance_km=round(km, 3),
                    coords=(round(anchor_coords[0] + delta, 6), anchor_coords[1]),
                    coord_source="api",
                    score=4.5,
                    reviews=100 + i,
                    raw={"platform": platform, "demo": True},
                )
            )
        return PlatformOutcome(
            platform=platform,
            anchor=AnchorRef(
                name=anchor_name,
                city=city,
                coords=anchor_coords,
                coord_source="api",
                source="demo",
            ),
            quotes=quotes,
            degraded=True,
            notes=[f"演示模式:数据为合成样本(前缀 {prefix}),落库标 is_demo=true"],
            price_source="dom",
        )

    # ==================================================================
    # 视觉兜底(门控)—— 供 CLI/批量在 DOM 取不到价时按需调用
    # ==================================================================

    async def vision_fallback(
        self, quote: HotelQuote, screenshot_png: bytes | None
    ) -> tuple[HotelQuote, str]:
        """对某条没有价格的报价做视觉兜底 + 交叉校验(T3C.4 / T3C.5)。

        ★ 门控在 :class:`VisionEstimator` 里:``VISION_ENABLED=0``(默认)时
          **一次 HTTP 都不会发**,这里直接返回原报价。
        """
        if screenshot_png is None or quote.price is not None:
            return quote, ""
        vision_price = await self.vision.read_price(screenshot_png)
        need_check, why = cross_check(
            quote.price, vision_price, float(self.settings.vision.price_tolerance)
        )
        if vision_price is None:
            return quote, why
        return (
            quote.model_copy(
                update={
                    "price": vision_price,
                    "price_source": "vision",
                    "need_manual_check": need_check,
                    "degraded": True,
                }
            ),
            why,
        )

    # ==================================================================
    # 判重(B27)
    # ==================================================================

    async def should_run(
        self, day: date, *, session: Any = None, force: bool = False
    ) -> tuple[bool, str]:
        """★ **以数据为准**:今天有没有**真数据**,不是"今天跑没跑"。

        ``force=True`` 是**人来按的开关**(CLI ``--force``);
        旧系统的病根是代码里**硬编码** ``force=True``(``pusher.py:568``)
        —— 于是这段判重永远被绕过,库里攒出 6 组重复(D8)。

        返回 ``(要不要跑, 原因)`` —— **原因一定要能打印**,否则"为什么没跑"又成了静默失败。
        """

        async def _do(sess: Any) -> tuple[bool, str]:
            repo = CompareRepository(sess)
            if force:
                return True, "显式 --force,跳过判重"
            has = await repo.has_real_data_today(day)
            if has:
                return False, f"{day} 已有真实比价数据(is_demo=false 且 price 非空),跳过"
            return True, f"{day} 尚无真实比价数据,需要跑"

        if session is not None:
            return await _do(session)
        if self.repo is not None:
            return await _do(self.repo.session)
        raise RuntimeError("should_run() 需要传入 session 或 repo")

    # ==================================================================
    # 报告/推送共用的文本段
    # ==================================================================

    def price_text(
        self,
        result: CompareResult,
        *,
        quote_count: int | None = None,
        distance_available: bool | None = None,
    ) -> str:
        """单店比价**纯文字段**(日报合并与独立推送共用)。"""
        return build_price_text(
            anchor_name=result.anchor.name,
            quotes=result.quotes,
            query_date=result.query_date,
            slot=result.query_slot,
            quote_count=quote_count or self.cfg.quote_count,
            distance_available=(
                result.distances_available if distance_available is None else distance_available
            ),
        )


def distance_summary(result: CompareResult) -> dict[str, Any]:
    """距离可用性摘要(报告与自检用)。"""
    total = len(result.quotes)
    with_dist = sum(1 for q in result.quotes if q.distance_km is not None)
    sources: dict[str, int] = {}
    for q in result.quotes:
        sources[q.coord_source] = sources.get(q.coord_source, 0) + 1
    return {
        "total": total,
        "with_distance": with_dist,
        "ratio": round(with_dist / total, 3) if total else 0.0,
        "sources": sources,
        "available": with_dist > 0,
    }


def sort_quotes_for_report(quotes: list[HotelQuote]) -> list[HotelQuote]:
    """报告用排序(距离升序 + ``None`` 垫底;复用 :mod:`geo` 的同一语义)。"""
    rows = [q.model_dump() for q in quotes]
    ordered = geo.sort_by_distance(rows)
    return [HotelQuote(**r) for r in ordered]
