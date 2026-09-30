"""浏览器兜底通道(T3.4)—— API 抛异常时**无条件降级**。

流程(段1 §5.5)::

    导航到 sub_module.url(超时 60s;必要时先 nav)
      → 等数据就绪(上限 120s)/ 等图表渲染(上限 8s)
      → DOM 提取(css: / js: / text: / table:)
      → 汇总:有数据=degraded / 无数据=no_data / 抛异常=failed

**四态(逐字继承)**
-------------------
============  ==============================================  ========
状态          产生条件                                          算失败?
============  ==============================================  ========
``degraded``  浏览器 DOM 提取出**非空** payload                  否
``no_data``   浏览器 DOM 提取出**空** payload                    否
``failed``    兜底过程**抛任何 Exception**                      **是**
============  ==============================================  ========

**DOM 提取四前缀(A18 遗产,逐字继承)**::

    css:<selector>   → 首个可见元素文本
    text:<关键词>    → 正文「关键词+数值」正则(★ 唯一大小写不敏感的)
    js:<表达式>      → page.evaluate(须可 JSON 序列化)
    table:<n>        → 第 n 张**有内容的**表(0 起,二维字符串数组)
    无前缀           → ★ 见下

★ **无前缀 path 的处理:修掉旧系统的"静默假 no_data"(漂移 A)**
--------------------------------------------------------------
旧实现把无前缀 path 当**裸关键词**走 ``text:`` 正则。而 ``api_rules.json`` 里
**全部 485 条 path 都是 JSON 点号路径**(如 ``data.[0].tip1``),拿它当关键词去正文里
找必然找不到 → ``payload`` 为空 → 状态被判 ``no_data``「明确无数据」。

**真实原因是「DOM 路径未校准」,不是「明确无数据」。** 这是一个系统性产出假
``no_data`` 的 bug:它会让"浏览器兜底根本不会提取"这件事**看起来像正常的无数据**,
从而把 S5(DOM 选择器失效)永久藏起来。

新实现:**无 DOM 前缀的 path 直接跳过**(不猜),并在
:attr:`SubModuleCfg.dom_calibrated` 为 ``False`` 时把状态判为 ``degraded``
(附 ``status_note`` 说明"DOM 未校准"),**不再产出假 ``no_data``**。
"""

from __future__ import annotations

import re
from datetime import date
from typing import TYPE_CHECKING, Any

from loguru import logger

from hoteldata.domains.collect.contract import (
    ExtractContext,
    ExtractResult,
    ExtractTarget,
)
from hoteldata.domains.collect.rules import ApiRules, PageCfg, SubModuleCfg
from hoteldata.domains.collect.windows import normalize_window

if TYPE_CHECKING:  # pragma: no cover
    from playwright.async_api import Page

__all__ = [
    "FIELD_PATH_CSS",
    "FIELD_PATH_JS",
    "FIELD_PATH_TABLE",
    "FIELD_PATH_TEXT",
    "DOM_CALIBRATED_PREFIXES",
    "BrowserChannel",
    "extract_module_dom",
    "extract_tables",
]

# ---- 前缀常量(逐字继承旧 ``ebooking.py:70-77``)----
FIELD_PATH_CSS = "css:"
FIELD_PATH_JS = "js:"
FIELD_PATH_TABLE = "table:"
FIELD_PATH_TEXT = "text:"

DOM_CALIBRATED_PREFIXES: tuple[str, ...] = (
    FIELD_PATH_CSS,
    FIELD_PATH_JS,
    FIELD_PATH_TABLE,
    FIELD_PATH_TEXT,
)

#: ``text:`` / 裸关键词的完整正则(逐字继承)
_TEXT_VALUE_RE = r"\s*[:：]?\s*([-\d,]+(?:\.\d+)?\s*%?)"

#: 登录页特征(两处重复常量合一,见旧 ``ebooking.py:31-42``)
LOGIN_URL_MARKERS: tuple[str, ...] = ("login", "passport", "signin", "sign_in", "auth", "sso")

#: 页面出现这些强数据类关键词才视为已进入业务系统(登录成功)
BUSINESS_TEXT_KEYWORDS: tuple[str, ...] = (
    "经营概况",
    "数据中心",
    "数据概览",
    "报表中心",
    "订单管理",
    "房态管理",
    "今日数据",
    "营业数据",
    "经营数据",
    "数据看板",
)


# ---------------------------------------------------------------------------
# DOM 提取(纯函数,便于离线单测)
# ---------------------------------------------------------------------------


def has_dom_prefix(path: str) -> bool:
    """``path`` 是否是 **DOM 已校准**的写法(四前缀之一)。"""
    low = (path or "").lstrip().lower()
    return low.startswith(DOM_CALIBRATED_PREFIXES)


async def extract_tables(page: Page) -> list[list[list[str]]]:
    """抽取页面所有**有内容**的表格为二维字符串数组(一次 evaluate 批量取回)。"""
    try:
        tables = await page.evaluate(
            """() => {
                const out = [];
                for (const t of document.querySelectorAll('table')) {
                    const rows = [];
                    for (const tr of t.querySelectorAll('tr')) {
                        const cells = Array.from(tr.querySelectorAll('th, td'))
                            .map(c => (c.innerText || '').trim());
                        if (cells.some(c => c !== '')) rows.push(cells);
                    }
                    if (rows.length) out.push(rows);
                }
                return out;
            }"""
        )
        return tables or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("表格抽取失败: {}", exc)
        return []


async def extract_module_dom(
    page: Page,
    sub_module: SubModuleCfg,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """按 ``sub_module.apis[*].fields[*].path`` 从 DOM 提取。

    返回 ``(payload, detail)``;``detail`` 记录有多少字段因**未校准**被跳过
    —— 这正是旧系统藏起来的那个信号。
    """
    try:
        body_text = await page.locator("body").inner_text(timeout=10_000)
    except Exception:  # noqa: BLE001
        body_text = ""
    tables = await extract_tables(page)

    payload: dict[str, Any] = {}
    detail: dict[str, Any] = {
        "dom_prefix_fields": 0,
        "uncalibrated_fields": 0,
        "uncalibrated_labels": [],
        "missed_labels": [],
    }

    for entry in sub_module.apis:
        for field_cfg in entry.fields:
            label = field_cfg.label
            path = str(field_cfg.path or "").strip()
            if not label or label in payload:
                continue  # 同 label 首个命中优先(旧语义)
            if not has_dom_prefix(path):
                # ★ 未校准:不猜、不当关键词。旧系统在这里静默产 no_data。
                detail["uncalibrated_fields"] += 1
                if len(detail["uncalibrated_labels"]) < 12:
                    detail["uncalibrated_labels"].append(label)
                continue
            detail["dom_prefix_fields"] += 1
            value = await _extract_one(page, path, body_text, tables)
            if value is not None:
                payload[label] = value
            elif len(detail["missed_labels"]) < 12:
                detail["missed_labels"].append(label)
    return payload, detail


async def _extract_one(page: Page, path: str, body_text: str, tables: list[list[list[str]]]) -> Any:
    """单字段提取;**失败/未命中返回 ``None``**(静默跳过该 label)。"""
    if path.startswith(FIELD_PATH_CSS):
        selector = path[len(FIELD_PATH_CSS) :].strip()
        if not selector:
            return None
        try:
            loc = page.locator(selector).first
            if await loc.count() > 0 and await loc.is_visible():
                return (await loc.inner_text(timeout=3000)).strip()
        except Exception:  # noqa: BLE001
            return None
        return None

    if path.startswith(FIELD_PATH_JS):
        expr = path[len(FIELD_PATH_JS) :].strip()
        if not expr:
            return None
        try:
            return await page.evaluate(expr)
        except Exception:  # noqa: BLE001
            return None

    if path.startswith(FIELD_PATH_TABLE):
        raw_idx = path[len(FIELD_PATH_TABLE) :].strip()
        try:
            idx = int(raw_idx)
        except ValueError:
            return None
        if 0 <= idx < len(tables):
            return tables[idx]
        return None

    # text: (★ 唯一大小写不敏感的前缀)/ 已由 has_dom_prefix 保证有前缀
    kw = path[len(FIELD_PATH_TEXT) :].strip() if path.lower().startswith(FIELD_PATH_TEXT) else path
    if not kw:
        return None
    m = re.search(re.escape(kw) + _TEXT_VALUE_RE, body_text)
    if m:
        return m.group(1).strip()
    return None


# ---------------------------------------------------------------------------
# 通道
# ---------------------------------------------------------------------------


class BrowserChannel:
    """浏览器兜底通道。

    依赖：
      * ``infra.browser.BrowserPool`` —— 取 ``page``(三元组里的第三个);
      * 会话失效时**触发登录管家**(由调用方注入 ``ensure_login`` 回调,避免
        域之间互相 import —— 硬约束 2)。
    """

    def __init__(
        self,
        ctx: ExtractContext,
        rules: ApiRules,
        *,
        pool: Any,
        handle: Any,
        ensure_login: Any = None,
    ) -> None:
        self.ctx = ctx
        self.rules = rules
        self.pool = pool
        self.handle = handle
        self.ensure_login = ensure_login
        self.nav_timeout_ms = int(ctx.api_timeout_s * 3000)  # 默认 60s
        self.page_ready_timeout_s = 120.0
        self.chart_ready_wait_s = 8.0

    # ------------------------------------------------------------------

    async def collect_sub_module(
        self,
        page_name: str,
        page_cfg: PageCfg,
        sub_module: SubModuleCfg,
        window: str,
        *,
        html_dir: Any = None,
    ) -> ExtractResult:
        """★ 浏览器兜底。**唯一产生 ``failed`` 的地方。**"""
        window = normalize_window(window)
        target = ExtractTarget(page=page_name, module=sub_module.name, window=window)
        url = self._nav_url(page_cfg, sub_module)
        try:
            async with self.pool.page_session(self.handle) as (_browser, _ctx, page):
                await self._goto(page, url, sub_module)
                await self._ensure_logged_in(page)
                await self._wait_chart(page)
                await self._wait_ready(page)
                payload, dom_detail = await extract_module_dom(page, sub_module)
                html_path = await self._dump_html(page, page_name, sub_module.name, window)

                if payload:
                    status, note = "degraded", "API 通道失败,浏览器兜底(DOM 提取)"
                elif not sub_module.dom_calibrated:
                    # ★ 修掉旧系统的「静默假 no_data」:这里不是"明确无数据",
                    #    而是"DOM 路径未校准,根本无法提取"。
                    status = "degraded"
                    note = (
                        "DOM 路径未校准(api_rules 的 fields[].path 全是 JSON 点号路径,"
                        f"无 {'/'.join(DOM_CALIBRATED_PREFIXES)} 前缀),无法 DOM 提取;"
                        "若要启用浏览器提取,需为该模块补 DOM 选择器"
                    )
                    logger.warning("[{}|{}|{}] {}", page_name, sub_module.name, window, note)
                else:
                    status, note = "no_data", "API 通道失败且浏览器无数据(明确无数据)"

                return ExtractResult(
                    status=status,  # type: ignore[arg-type]
                    channel="browser",
                    payload=payload,
                    error=note or None,
                    raw_path=html_path,
                    target=target,
                    detail={"dom": dom_detail, "url": url, "note": note},
                )
        except Exception as exc:  # noqa: BLE001 - ★ 单模块失败不阻断其他模块
            logger.error("[{}|{}|{}] 浏览器兜底失败: {}", page_name, sub_module.name, window, exc)
            return ExtractResult(
                status="failed",
                channel="browser",
                payload={},
                error=str(exc),
                raw_path=None,
                target=target,
                detail={"url": url, "exception": type(exc).__name__},
            )

    # ------------------------------------------------------------------

    def _nav_url(self, page_cfg: PageCfg, sub_module: SubModuleCfg) -> str:
        from hoteldata.domains.collect.rules import page_url

        raw = sub_module.url or page_cfg.url
        return (
            page_url(
                page_cfg,
                hotel_id=self.ctx.multi_store_hotel_id(),
                is_multi=self.ctx.account.is_multi,
            )
            if not sub_module.url
            else raw
        )

    async def _goto(self, page: Page, url: str, sub_module: SubModuleCfg) -> None:
        """导航到子模块 URL(超时 60s)。"""
        await page.goto(url, wait_until="domcontentloaded", timeout=self.nav_timeout_ms)
        nav_text = (sub_module.nav or "").strip()
        if nav_text:
            logger.debug("导航说明(人工核对用): {}", nav_text[:120])

    async def _ensure_logged_in(self, page: Page) -> None:
        """被弹回登录页 → 触发登录管家(若注入了回调),然后重试一次。"""
        current = (page.url or "").lower()
        if not any(m in current for m in LOGIN_URL_MARKERS):
            return
        logger.warning("浏览器被重定向到登录页: {}", page.url)
        if self.ensure_login is None:
            raise RuntimeError(f"登录未完成({page.url}),且未注入登录回调")
        ok = await self.ensure_login(self.handle)
        if not ok:
            raise RuntimeError("登录未完成,子模块采集中止")

    async def _wait_chart(self, page: Page) -> None:
        """等图表渲染(上限 ``CHART_READY_WAIT_S`` = 8s)。"""
        try:
            await page.wait_for_timeout(self.chart_ready_wait_s * 1000)
        except Exception:  # noqa: BLE001
            pass

    async def _wait_ready(self, page: Page) -> None:
        """等数据就绪(上限 ``PAGE_READY_TIMEOUT_S`` = 120s)。

        旧系统 ``DATA_WAIT_TIMEOUT_S=120`` 的**唯一消费点就在取数通道**
        (不是截图器 —— 截图器用的是 8s/20s 两套)。
        """
        import asyncio

        deadline = asyncio.get_running_loop().time() + self.page_ready_timeout_s
        last = ""
        while asyncio.get_running_loop().time() < deadline:
            try:
                spinner = await page.locator(".ant-spin-spinning, [class*='spin-spinning']").count()
                text = await page.locator("body").inner_text(timeout=5000)
            except Exception:  # noqa: BLE001
                return
            if spinner == 0 and text and text == last:
                return
            last = text
            await asyncio.sleep(1.0)

    async def _dump_html(self, page: Page, page_name: str, module: str, window: str) -> str | None:
        """落 HTML(**只在兜底时落**,便于事后核对 DOM)。"""
        if not self.ctx.persist_raw:
            return None
        try:
            path = (
                self.ctx.layout.hotel_day_dir(self.ctx.hotel.name, self.ctx.collect_date)
                / "html"
                / (f"{_safe(page_name)}_{_safe(module)}_{_safe(window)}.html")
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            content = await page.content()
            from hoteldata.infra.atomic import atomic_write_text

            atomic_write_text(path, content)
            return self.ctx.layout.to_relative(path)
        except Exception as exc:  # noqa: BLE001
            logger.debug("落 HTML 失败: {}", exc)
            return None


def _safe(text: str) -> str:
    from hoteldata.infra.paths import safe_name

    return safe_name(text, max_len=40)


__all__ += ["BUSINESS_TEXT_KEYWORDS", "LOGIN_URL_MARKERS", "has_dom_prefix", "date"]
