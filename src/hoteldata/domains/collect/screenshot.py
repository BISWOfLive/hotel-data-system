"""★ 独立截图器(批次 E:T5.1~T5.5)—— 与取数**解耦**。

为什么单独一个模块(段1 §5.8「截图与取数解耦」)
================================================

===================  ==============================================================
设计                  做法(以及为什么)
===================  ==============================================================
**不 import 提取器**  本模块**不 import** ``api`` / ``browser`` / ``channels`` /
                     ``datacenter`` 任何一个提取器实现 —— 域内单向依赖,截图器坏
                     了不影响取数,取数坏了也不影响出图(旧系统 Stage 2 的口径)。
**不解析数据**        只负责「打开 → 等就绪 → 截」;一个字段都不取。
**衔接 = 落库回填**   先幂等建 ``collect_reports`` 行(``channel='screenshot'``)
                     → 截图 → ``link_screenshot(screenshot_path=None,
                     module_screenshots=module_out)`` 用 **COALESCE** 回填
                     (传 ``None``/空 dict → **保留旧值**,重跑不会把已有的图清掉)。
**错峰执行**          独立任务 05:30(``SCREENSHOT_ENABLED`` / ``SCREENSHOT_TIME``
                     由**任务注册表**消费,不在本模块内判断)。
**不缩水**            ``module_screenshots_json`` 的键 = ``screenshot_modules[*].name``
                     原文,值 = **相对路径字符串**(不是数组),键名与旧系统逐字一致,
                     否则段2 取不到图。
===================  ==============================================================

遗产来源(逐字依据见 ``docs/参考/旧系统/规格-截图与轮换.md``,下称「规格」)
--------------------------------------------------------------------------
* 就绪判定 §1.3(``screenshoter.py:122-150`` / ``190-222``);
* 可见容器选取 §1.4(``screenshoter.py:153-171`` / ``_click_first_visible`` 173-188);
* 消弹窗 §1.5(``screenshoter.py:225-254``);
* 交互语义 §1.6(``clicks`` / ``tabs``+``click`` / ``skip_click_on`` /
  ``require_text`` / ``used_keys``);
* 截图方式与体积告警 §1.7;
* 落库回填 §2(``screenshoter.py:503-519`` + ``storage/db.py:760-778``)。

★ 必须在 docstring 里写清楚的四个「规格与直觉不符」之处
-------------------------------------------------------

1. **两套就绪判定,不是一套**(规格 §5 漂移 D-2)。计划书 §3.2 / §5.8 把它们写成
   一句话「spinner=0 + 连续两轮文本一致 +(文本≥60 字且≥2 数字 或 有 canvas/svg)」,
   实际是**两个条件不同**的函数:

   ==================  ============  ==================  ===========  ======
   函数                spinner 归零  连续两轮文本一致    文本≥60 字    上限
   ==================  ============  ==================  ===========  ======
   ``_wait_charts_ready``  ✅ 页面级     ✅ ``body`` 文本    **❌ 没有**   **8s**
   ``_wait_content``       ✅ 容器级     ✅ 容器文本         ✅ 或有关图表 **20s**
   ==================  ============  ==================  ===========  ======

2. **120 秒不在截图器里**(规格 §5 漂移 D-5)。``DATA_WAIT_TIMEOUT_S=120`` 的
   **唯一消费点是取数通道**(``collect/`` 的数据通道),截图器**不引入** 120s。
   本模块的等待上限集合:8s / 20s / 30_000ms / 20_000ms / 15_000ms / 10_000ms /
   5_000ms / 5.0s / 6.0s / 8.0s / 0.5s 轮询。

3. **``used_keys`` 在旧配置下是死代码**(规格 §5 漂移 D-8):22 条
   ``screenshot_modules`` 产出 22 个互不相同的 ``(url, selector)`` 键,重复组为 0。
   本模块**仍实现**它(去护栏不能拆),但**不要**为它写"必须触发"的测试;它的值
   与旧系统一样**只写不读**。

4. **``skip_click_on`` 命中时"既不点击也不重等"**(规格 §1.6.3 末注):跳过点击会
   连带跳过"页签切换后的重等 + 重选容器",顺序敏感,不能只把 ``click`` 置空。

四态映射(段1 契约;``no_data`` **不算失败**)
------------------------------------------
======================  ==================================================
状态                    产生条件
======================  ==================================================
``degraded``            出图(截图**不是主数据通道**,所以有图也不报 ``ok``);
                        或部分条目出图 + 部分条目异常
``no_data``             **明确无该模块入口**(``require_text`` 未命中)→ 快跳;
                        或该子模块没配 ``screenshot_modules``(无图可截)
``failed``              抛异常 / 容器 30s 未可见 / 页签切换后内容未就绪 /
                        ``ensure_report`` 或 ``link_screenshot`` 失败
======================  ==================================================

> ⚠️ 与冻结接口的两处**有意差异**(都已在代码行内注明原因):
> ① ``ensure_report`` 会带上 ``account_id``(旧系统该列恒 ``NULL``);
> ② 浏览器登录态统一走 ``infra.browser.BrowserPool.page_session()``,
> **不再**像旧系统那样在 ``screenshoter.py`` 里复制一份 ``_launch_browser`` /
> ``_new_context``(旧系统四处重复,总纲 5.5 D 级;本模块是"唯一浏览器启动处"的消费者)。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any, Literal

from loguru import logger

from hoteldata.domains.collect.contract import (
    ExtractContext,
    ExtractResult,
    ExtractStatus,
    ExtractTarget,
)
from hoteldata.domains.collect.rotation import RotationError, get_rotation
from hoteldata.domains.collect.rules import (
    ApiRules,
    ApiRulesError,
    PageCfg,
    ScreenshotModuleCfg,
    SubModuleCfg,
    get_api_rules,
)
from hoteldata.domains.collect.windows import DEFAULT_WINDOW
from hoteldata.settings import ScreenshotSettings, Settings, get_settings

if TYPE_CHECKING:  # pragma: no cover - 仅类型
    from playwright.async_api import Locator, Page

    from hoteldata.domains.collect.repository import CollectRepository

__all__ = [
    "BODY_SELECTOR",
    "CHART_READY_WAIT_S",
    "CHART_SELECTOR",
    "CLICK_SCAN_LIMIT",
    "CONTENT_WAIT_S",
    "MIN_NUMBERS",
    "MIN_TEXT_LEN",
    "NOTIFY_WAIT_S",
    "NUMBER_RE",
    "POLL_INTERVAL_S",
    "SCREENSHOT_CHANNEL",
    "SPINNER_SELECTOR",
    "VISIBLE_SCAN_LIMIT",
    "Screenshoter",
    "ShotOutcome",
    "ShotState",
    "aggregate_status",
    "dedupe_targets",
    "screenshot_demand_modules",
    "should_skip_click",
]

SCREENSHOT_CHANNEL = "screenshot"

# ---------------------------------------------------------------------------
# 常量(每一个都能在规格里找到出处;改这里等于改契约)
# ---------------------------------------------------------------------------

#: 页面级图表等待上限(旧 ``Config.CHART_WAIT_TIMEOUT_S``,``config.py:161`` 缺省 8)
#: ★ 只有 ``_wait_charts_ready`` 用它;``_wait_content`` 用的是 20s。
CHART_READY_WAIT_S = 8.0
#: 模块级内容等待上限(旧 ``screenshoter.py:190`` 形参缺省 ``max_seconds: float = 20.0``)
CONTENT_WAIT_S = 20.0
#: 消弹窗轮询上限(旧 ``screenshoter.py:231`` ``deadline = time.time() + 5.0``)
NOTIFY_WAIT_S = 5.0
#: 三处就绪判定的统一轮询间隔(旧 148 / 208 / 220 / 252 行全是 ``sleep(0.5)``)
POLL_INTERVAL_S = 0.5

#: spinner 选择器(规格 §1.3.1 ``screenshoter.py:136``,逐字)
SPINNER_SELECTOR = ".ant-spin-spinning, [class*='spin-spinning']"
#: 图表选择器(规格 §1.3.1 ``screenshoter.py:135``,逐字)
CHART_SELECTOR = "canvas, svg"
#: 文本取样容器(规格 §1.3.1 ``screenshoter.py:139``,逐字)
BODY_SELECTOR = "body"

#: 数字正则(规格 §1.3.2 ``screenshoter.py:211``,逐字:小数整体算 **1** 个数字)
NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
#: 「文本≥60 字且≥2 个数字」的两个阈值(``len()`` 按字符计,中文一字=1;含空白)
MIN_TEXT_LEN = 60
MIN_NUMBERS = 2

#: 可见容器遍历上限(规格 §1.4 ``screenshoter.py:161``:``min(count, 50)``)
VISIBLE_SCAN_LIMIT = 50
#: ``_click_first_visible`` 遍历上限(规格 §1.4 ``screenshoter.py:176``:``min(count, 10)``)
CLICK_SCAN_LIMIT = 10

# ---- 各类超时(毫秒,逐字)----
MODULE_VISIBLE_TIMEOUT_MS = 30_000  # 模块容器可见等待(469)
AFTER_TAB_VISIBLE_TIMEOUT_MS = 20_000  # 页签切换后重等(482)
TAB_ATTACHED_TIMEOUT_MS = 15_000  # .tabs 定位等待(397,state="attached")
TAB_CLICK_TIMEOUT_MS = 10_000  # .tabs 点击超时(416)
ELEMENT_CLICK_TIMEOUT_MS = 5_000  # _click_first_visible 点击超时(182)
CLICKS_SCOPE_WAIT_MS = 20_000  # clicks 容器内等待(436)
CLICKS_PAGE_WAIT_MS = 8_000  # clicks 页面级回退等待(446)

# ---- clicks / tabs 的固定等待(秒,逐字)----
JS_INIT_SLEEP_S = 6.0  # clicks 序列前置(427)/ tabs 点击前(413)
CLICKS_STEP_SLEEP_S = 8.0  # clicks 成功后步间(462)

#: ``clicks`` 第三级回退的交互元素选择器(旧 ``screenshoter.py:453``)
CLICKS_FALLBACK_CSS: tuple[str, ...] = ("button:has-text", "a:has-text", "li:has-text")

#: 消弹窗的存在性门槛(旧 ``screenshoter.py:234``:``text=允许发送通知``)
#: ★ 保留这道门槛是**安全属性**:没有通知弹窗时**绝不**去点页面上的「关闭」按钮
#: (业务页里的「关闭」可能是真实面板的收起按钮)。
NOTIFY_MARKER = "text=允许发送通知"
#: 消弹窗按钮链(旧 ``screenshoter.py:236`` + ``241-242``,顺序即优先级)
NOTIFY_BUTTONS: tuple[str, ...] = (
    "button:has-text('允许')",
    "button:has-text('知道了')",
    "button:has-text('关闭')",
)

#: ``skip_click_on`` 的两个合法取值(``rules.ScreenshotModuleCfg`` 也只认这两个)
SKIP_MONDAY = "monday"
SKIP_MONTH1 = "month1"

#: 截图扩展名(元素截图统一 JPEG)
SHOT_EXT = "jpg"

#: 单张截图的体积告警阈值**以配置为准**:``settings.screenshot.max_bytes``
#: (缺省 ``102_400``,= 旧 ``screenshoter.py:31`` 的 ``100 * 1024``);超限只告警,
#: 不删、不重截、不抛错(规格 §1.7.2)。


ShotState = Literal["saved", "missing_entry", "deduped", "error"]


@dataclass(slots=True)
class ShotOutcome:
    """单个 ``screenshot_modules[*]`` 条目的截图结果(模块内部用,便于离线单测)。"""

    name: str
    state: ShotState
    rel_path: str | None = None
    size: int | None = None
    oversize: bool = False
    reason: str = ""
    error: str | None = None


# ---------------------------------------------------------------------------
# 纯函数(无 IO,离线可测)
# ---------------------------------------------------------------------------


def dedupe_targets(targets: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """``(page, module)`` **去重保序**(重复目标只截一次,避免同页重复导航)。"""
    out: list[tuple[str, str]] = []
    for page_name, module_name in targets:
        key = (str(page_name), str(module_name))
        if key not in out:
            out.append(key)
    return out


def should_skip_click(mod: ScreenshotModuleCfg, today: date) -> bool:
    """``skip_click_on`` 命中判定(规格 §1.6.3 ``screenshoter.py:402-408`` 逐字)。

    ==============  ==========================  ==============================
    取值             命中条件                    原文
    ==============  ==========================  ==============================
    ``monday``       ``today.weekday() == 0``    ``"monday" in _skip and _today.weekday() == 0``
    ``month1``       ``today.day == 1``          ``"month1" in _skip and _today.day == 1``
    ==============  ==========================  ==============================

    甲方口径(2026-08-26):**非**周一/月 1 才点窗口页签(页面默认=上周);周一周 1
    保持页面默认视图。

    ★ 命中后调用方必须**同时跳过**「点击」与「页签切换后的重等/重选」两步
    (规格 §1.6.3 末注:``_click_target = None`` 连带影响 479 行的分支)。
    """
    skip = set(mod.skip_click_on or ())
    if SKIP_MONDAY in skip and today.weekday() == 0:
        return True
    return SKIP_MONTH1 in skip and today.day == 1


def aggregate_status(*, shots: int, errors: int) -> ExtractStatus:
    """把「出图数 / 异常数」汇总成截图通道的四态(``no_data`` 不算失败)。

    * 有异常**且一张图都没有** → ``failed``;
    * 只要有图 → ``degraded``(截图不是主数据通道,即便部分条目异常也是 ``degraded``);
    * 没有图也没有异常 → ``no_data``(明确无入口 / 该模块无截图配置)。
    """
    if errors and not shots:
        return "failed"
    if shots:
        return "degraded"
    return "no_data"


def _targets_from_rotation(day: date, rules: ApiRules) -> list[tuple[str, str]]:
    """当日轮换清单 → ``(page, module)`` 目标(只取 ``module_screenshot`` 项)。

    为什么只取 ``module_screenshot``:``fullpage`` 项(市场分析/点评分析/用户行为/
    预警-热点日历)走的是**整页** ``shot_url`` 通道,不是 ``(page, module)`` 元素截图,
    本批未实现(规格 §5 漂移 D-3 明确该通道**必须保留** —— 否则 4 项丢图,留给后续批次)。
    """
    plan = get_rotation().pick(day)
    out: list[tuple[str, str]] = []
    deferred: list[str] = []
    for item in plan.items:
        if item.name.startswith("预警"):
            # 旧 ``scheduler.py:72`` 的前缀特判:归预警三源采集,不是模块截图
            deferred.append(f"{item.name}(预警三源)")
            continue
        if item.type == "fullpage":
            deferred.append(f"{item.name}(整页 shot_url 通道)")
            continue
        located = rules.locate_module(item.name)
        if located is None:
            # 旧系统在这里静默跳过(T3.6 的坑);新实现直接报错
            raise RotationError(
                f"轮换项 {item.name!r}(page={item.page!r})在 api_rules.sub_modules 中不存在;"
                f"module_screenshot 项的 name 必须是子模块名(已知 "
                f"{len(rules.known_sub_module_names())} 个)"
            )
        page_name, sub = located
        if page_name != item.page:
            logger.warning(
                "轮换项「{}」声明的 page={!r},但规则里属页 {!r}(以规则为准)",
                item.name,
                item.page,
                page_name,
            )
        if not sub.screenshot_modules:
            logger.info("轮换项「{}」无 screenshot_modules 配置,不出图", item.name)
            continue
        out.append((page_name, item.name))
    if deferred:
        logger.debug("截图需求裁剪:{} 项不在本通道:{}", len(deferred), "、".join(deferred))
    return dedupe_targets(out)


def screenshot_demand_modules(
    day: date | None = None,
    *,
    targets: list[tuple[str, str]] | None = None,
    rules: ApiRules | None = None,
) -> list[tuple[str, str]]:
    """当日截图目标 ``[(page, module), ...]``(T5.5「按排期裁剪」)。

    两个输入来源,``targets`` 显式传入时**优先**:

    1. ``targets`` —— 外部传入的目标列表(调用方/调度器注入,原样去重保序);
    2. 缺省 —— ``domains/collect/rotation.py`` 的**当日轮换**
       (``get_rotation().pick(day)``:``offset = day.toordinal() % 21`` 取连续
       ``daily_count=5`` 项),再按 ``rules.locate_module()`` 展开成 ``(page, module)``。

    ★ **同源裁剪的两种语义,不要混**(段1 §5.8 vs 规格 §3.6):

    * **轮换**(本函数走这条):``config/push_rotation.json`` 的 21 项清单,
      每日 5 项,**采集 + 截图 + 推送共用**;``fixed_daily=True`` 的 7 个模块
      **不进**轮换、也**不进**截图清单(规格 §5 漂移 D-9:
      ``服务概览`` 的 ``screenshot_modules`` 为空,其余 5 个只有落进当日窗口才截)。
    * **排期需求**(``report_schedule.json`` 驱动的 ``screenshot_demand_modules``,
      旧 ``app/report_engine.py:151-174``):"今日无数据需求就不截图"。段1 的
      排期在批次 F 的任务注册表里,落地后把结果作为 ``targets`` 传进来即可,
      **本函数不做排期计算**(避免出现第二个事实源)。

    ``day=None`` → 机器本地日期(``rotation.pick`` 内部用 ``date.today()`` 的同一口径;
    跨机器/跨时区必须保证时钟一致,否则当天窗口不同 —— 规格 §6.2)。

    返回值为空列表 = 今日无元素截图目标(不是失败)。
    """
    if targets is not None:
        return dedupe_targets(targets)
    rules = rules or get_api_rules()
    return _targets_from_rotation(day or date.today(), rules)


# ---------------------------------------------------------------------------
# 就绪判定(★ 两个函数,条件不同 —— 漂移 D-2)
# ---------------------------------------------------------------------------


async def _wait_charts_ready(page: Page, *, max_seconds: float | None = None) -> bool:
    """页面级轮询图表就绪(上限 ``chart_ready_wait_s`` = **8s**);超时只告警。

    旧 ``screenshoter.py:122-150``。三个条件**同时**满足才算就绪:

      1. ``page.locator("canvas, svg").count() > 0`` —— **页面级**有图表;
      2. ``page.locator(SPINNER_SELECTOR).count() == 0`` —— spinner 未残留;
      3. ``body`` 文本**连续两轮相等** —— 渲染已稳定(防"占位值 → 真实值"切换瞬间被截)。

    ★ **本函数没有「文本≥60 字」判定** —— 那是 ``_wait_content`` 的独有条件
    (规格 §5 漂移 D-2 逐条对比表)。计划书把它们并成一句话,实施时**不要合并**。

    超时行为:``logger.warning`` 后返回 ``False``,**绝不抛错**(不阻塞后续截图)。
    """
    limit = CHART_READY_WAIT_S if max_seconds is None else max_seconds
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit
    last_body: str | None = None
    while loop.time() < deadline:
        try:
            has_chart = await page.locator(CHART_SELECTOR).count() > 0
            no_spinner = await page.locator(SPINNER_SELECTOR).count() == 0
            body_text = ""
            try:
                body_text = await page.locator(BODY_SELECTOR).inner_text() or ""
            except Exception:  # noqa: BLE001 - 取样失败按空串(旧 139-141 行)
                body_text = ""
            stable = last_body is not None and body_text == last_body
            if has_chart and no_spinner and stable:
                return True
            last_body = body_text
        except Exception:  # noqa: BLE001 - 旧 146-147 行整体吞掉
            pass
        await asyncio.sleep(POLL_INTERVAL_S)
    logger.warning("截图器:等待图表渲染超时({}s),继续", limit)
    return False


async def _wait_content(
    page: Page,
    loc: Locator,
    *,
    max_seconds: float = CONTENT_WAIT_S,
) -> bool:
    """模块级轮询内容就绪(旧 ``screenshoter.py:190-222``,缺省上限 **20s**)。

    ``loc`` 是模块容器或 ``page.locator("body")``;判定式(逐字):

    .. code-block:: text

        spinner 命中 → sleep(0.5) + continue          # ★ 不更新 last_text
        text   = loc.inner_text() or ""
        nums   = re.findall(r"\\d+(?:\\.\\d+)?", text)
        stable = last_text is not None and text == last_text
        has_data  = len(text) >= 60 and len(nums) >= 2   # 0 也算数据
        has_chart = loc.locator("canvas, svg").count() > 0
        就绪 = stable and (has_data or has_chart)        # ★ 先 stable,再或

    ★ 三个易错点:

    1. **spinner 命中时 ``continue``,``last_text`` 不更新** → "连续两轮文本一致"
       指的是**两次非 spinner 轮**的文本相等(规格 §5 漂移 D-2 细节 2);
    2. 数字正则 ``\\d+(?:\\.\\d+)?`` **小数整体算 1 个数字**(计划书写 ``\\d+`` 不等价);
    3. ``len()`` 按字符计(中文一字=1,含空白),这是"≥60 字"的真实含义
       (规格 §5 漂移 D-6,非漂移项)。

    返回值**只用于日志/观测**:旧系统超时后照截(``logger.warning`` 后返回 ``False``,
    由调用方决定),本实现同样**不因超时跳过截图** —— 跳过会产出"假无图"。

    ``page`` 形参保留旧签名(旧 ``_wait_content(self, page, loc, max_seconds=20.0)``),
    判定本身只用 ``loc``。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_seconds
    last_text: str | None = None
    while loop.time() < deadline:
        try:
            spinning = await loc.locator(SPINNER_SELECTOR).count()
            if spinning > 0:
                # ★ 只 sleep + continue:不更新 last_text(旧 207-209 行)
                await asyncio.sleep(POLL_INTERVAL_S)
                continue
            text = await loc.inner_text() or ""
            nums = NUMBER_RE.findall(text)
            stable = last_text is not None and text == last_text
            has_data = len(text) >= MIN_TEXT_LEN and len(nums) >= MIN_NUMBERS
            has_chart = await loc.locator(CHART_SELECTOR).count() > 0
            if stable and (has_data or has_chart):
                return True
            last_text = text
        except Exception as exc:  # noqa: BLE001 - 旧 218-219 行 logger.debug
            logger.debug("截图器:内容就绪判定异常: {}", exc)
        await asyncio.sleep(POLL_INTERVAL_S)
    logger.warning("截图器:内容等待超时({}s),继续", max_seconds)
    return False


async def _dismiss_notifications(page: Page, *, max_seconds: float = NOTIFY_WAIT_S) -> bool:
    """轮询最多 5 秒消除"允许发送通知"弹窗(旧 ``screenshoter.py:225-254``)。

    顺序(旧 234 → 236 → 241-242):

      0. ``text=允许发送通知`` —— **存在性门槛**(不是按钮);
      1. ``button:has-text('允许')``;
      2. ``button:has-text('知道了')``;
      3. ``button:has-text('关闭')``。

    ★ **门槛保留**:没有通知弹窗时**绝不**去点页面上的「关闭」按钮 —— 业务页里的
    「关闭」可能是真实面板的收起按钮,点了会截到被收起的容器(安全属性,不随"顺序链"
    的简化描述而丢)。命中弹窗但按钮都点不动 → ``logger.debug`` 后返回 ``False``。

    ★ 相对旧实现的两处加固:① 用 ``.first`` 取元素(旧代码直接 ``locator.click()``,
    多命中会触发 Playwright strict mode 异常);② 点击带 5s 超时(旧为默认超时)。
    两条都不改变"绝不抛错"的对外语义。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_seconds
    while loop.time() < deadline:
        try:
            marker = page.locator(NOTIFY_MARKER)
            if await marker.count() > 0 and await marker.first.is_visible():
                for selector in NOTIFY_BUTTONS:
                    btn = page.locator(selector)
                    if await btn.count() > 0 and await btn.first.is_visible():
                        await btn.first.click(timeout=ELEMENT_CLICK_TIMEOUT_MS)
                        logger.success("截图器:通知弹窗已关闭")
                        return True
                logger.debug("截图器:检测到通知弹窗,但无可点按钮")
                return False
        except Exception as exc:  # noqa: BLE001 - 旧 250-251 行 logger.debug
            logger.debug("截图器:检测通知弹窗异常: {}", exc)
            return False
        await asyncio.sleep(POLL_INTERVAL_S)
    logger.debug("截图器:未检测到通知弹窗")
    return False


# ---------------------------------------------------------------------------
# 定位辅助(规格 §1.4)
# ---------------------------------------------------------------------------


async def _pick_visible_container(page: Page, selector: str) -> Locator | None:
    """取命中选择器的**第一个可见**元素;无可见元素返回 ``None``。

    旧 ``screenshoter.py:153-171``:遍历 ``min(count, 50)`` 个,``is_visible()`` 为真即返回。
    单元素异常 ``continue``,整体异常 ``return None``。

    为什么必须"首个**可见**":2026-08-25 金字塔双容器实锤 —— KO 模板容器(含静态示例值
    `20,000`)加载完成后被隐藏,真实数据容器才显示;直接取 ``.first`` 会截到隐藏模板/
    骨架的**示例值**(静默错数据)。调用方拿到 ``None`` 时回退 ``page.locator(sel).first``。
    """
    try:
        all_hits = page.locator(selector)
        count = min(await all_hits.count(), VISIBLE_SCAN_LIMIT)
        for i in range(count):
            el = all_hits.nth(i)
            try:
                if await el.is_visible():
                    return el
            except Exception:  # noqa: BLE001 - 单元素可见性异常:跳过继续
                continue
    except Exception:  # noqa: BLE001 - 伪对象/结构异常:当作没找到
        return None
    return None


async def _click_first_visible(cand: Locator) -> bool:
    """遍历候选元素点击**第一个可见**的;``click`` 失败回退 ``dispatch_event('click')``。

    旧 ``screenshoter.py:173-188``,遍历上限 ``min(count, 10)``(**不是 50**)。全部失败
    返回 ``False``,由调用方回退下一级定位(容器内 → 页面级 → 交互元素)。
    """
    try:
        count = min(await cand.count(), CLICK_SCAN_LIMIT)
    except Exception:  # noqa: BLE001
        return False
    for i in range(count):
        el = cand.nth(i)
        try:
            if not await el.is_visible():
                continue
            try:
                await el.click(timeout=ELEMENT_CLICK_TIMEOUT_MS)
            except Exception:  # noqa: BLE001 - 合成点击失败(动画/遮挡)→ 原生事件兜底
                await el.dispatch_event("click")
            return True
        except Exception:  # noqa: BLE001 - 单候选失败试下一个
            continue
    return False


# ---------------------------------------------------------------------------
# 截图器
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _ModuleState:
    """一个 ``(page, module)`` 目标的累积状态(截图 + 跳过 + 异常)。"""

    window: str = DEFAULT_WINDOW
    shots: dict[str, str] = field(default_factory=dict)
    sizes: dict[str, int] = field(default_factory=dict)
    oversize: list[str] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)


class Screenshoter:
    """★ 独立截图器(实现 :class:`~hoteldata.domains.collect.contract.Extractor` 形状)。

    ``name = "screenshot"``;对外只暴露 :meth:`run`。
    浏览器与登录态**全部来自注入**(``infra.browser.BrowserPool`` + ``ctx.session``),
    本类不自建浏览器(旧系统四处复制启动代码是 D 级遗产,不继承)。
    """

    name = "screenshot"

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        rules: ApiRules | None = None,
        pool: Any = None,
    ) -> None:
        self.settings: Settings = settings or get_settings()
        self.rules: ApiRules = rules or get_api_rules()
        self._pool = pool

    # ------------------------------------------------------------------
    # 依赖
    # ------------------------------------------------------------------

    @property
    def shot_settings(self) -> ScreenshotSettings:
        return self.settings.screenshot

    @property
    def pool(self) -> Any:
        """浏览器池(**惰性**构造:不 import playwright、不启动浏览器)。"""
        if self._pool is None:
            from hoteldata.infra.browser import BrowserPool

            self._pool = BrowserPool(self.settings)
        return self._pool

    @property
    def _nav_timeout_ms(self) -> int:
        return int(self.settings.browser.nav_timeout_s * 1000)

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------

    async def run(
        self,
        ctx: ExtractContext,
        targets: list[tuple[str, str]],
        *,
        repo: CollectRepository | Any,
    ) -> list[ExtractResult]:
        """按 ``targets`` 截图:**每个 ``(page, module)`` 一条** :class:`ExtractResult`。

        执行顺序(逐页;页内按 ``sub_module.url`` 分组,**同一 URL 只导航一次**):

        1. ``repo.ensure_report(hotel, date, page, channel="screenshot")`` —— 幂等建行
           (★ 必须在 ``link_screenshot`` 之前;``UPDATE`` 无行可改 → 日报 0 图);
        2. 打开页面 → 每个 ``screenshot_modules[*]``:
           ``require_text`` 入口判定 → ``tabs``+``click`` → ``clicks`` →
           等容器可见(30s)→ 选**首个可见**容器 → ``_wait_content``(20s)→
           (页签已切换时)重等 20s + 重选容器 → ``loc.screenshot(type="jpeg", quality=...)``;
        3. ``repo.link_screenshot(..., screenshot_path=None, module_screenshots=module_out)``
           —— ``screenshot_path`` **恒传 ``None``**(COALESCE 保留旧整页图);
           ``module_out`` 为空时等价 ``None`` → **旧值不清空**。

        ``targets`` 为空 → 返回 ``[]``(今日无目标,不是失败)。
        单页失败不影响其他页;任何异常都会落到 ``failed``,不吞异常。
        """
        ordered = dedupe_targets(targets)
        if not ordered:
            logger.info("截图器:目标为空,本次不截图")
            return []

        by_page: dict[str, list[str]] = {}
        for page_name, module_name in ordered:
            by_page.setdefault(page_name, []).append(module_name)

        collected: dict[tuple[str, str], ExtractResult] = {}
        for page_name, module_names in by_page.items():
            collected.update(await self._run_page(ctx, page_name, module_names, repo=repo))
        # 按输入顺序返回,调用方拿到的每条结果都可与目标一一对应
        return [collected[key] for key in ordered if key in collected]

    # ------------------------------------------------------------------
    # 单页流程
    # ------------------------------------------------------------------

    async def _run_page(
        self,
        ctx: ExtractContext,
        page_name: str,
        module_names: list[str],
        *,
        repo: CollectRepository | Any,
    ) -> dict[tuple[str, str], ExtractResult]:
        """截一页并回填。**保证每个目标恰好一条结果**(异常不外泄)。"""
        try:
            page_cfg = self.rules.page(page_name)
        except ApiRulesError as exc:
            logger.error("截图器:未知页面 {}: {}", page_name, exc)
            unknown = _ModuleState()
            unknown.errors.append(str(exc))
            return {
                (page_name, m): self._result(page_name, m, state=unknown, note="规则里没有这个页,未截图")
                for m in module_names
            }

        state = self._init_module_states(page_cfg, module_names)

        # ---- ① 先幂等建行(旧 screenshoter.py:503-519 的 W2 关键步骤)----
        try:
            row_id = await repo.ensure_report(
                ctx.hotel_id,
                ctx.collect_date,
                page_name,
                channel=SCREENSHOT_CHANNEL,
                # ★ 有意差异:旧系统该列恒 NULL;新实现记录是哪个账号的登录态出的图。
                #   ensure_report 用 ON CONFLICT DO NOTHING → 同日重跑不会改写既有行。
                account_id=ctx.account_id,
            )
        except Exception as exc:  # noqa: BLE001 - 建行失败 → 本页整体 failed(不截孤儿图)
            logger.error("[{}] 截图前建 collect_reports 行失败: {}", page_name, exc)
            for m in module_names:
                state[m].errors.append(f"ensure_report 失败: {exc}")
            return {
                (page_name, m): self._result(page_name, m, state=state[m], row_id=0, note="建行失败,未截图")
                for m in module_names
            }

        link_rows = 0
        link_error: str | None = None
        module_out: dict[str, str] = {}

        try:
            module_out = await self._walk_page(ctx, page_cfg, page_name, module_names, state)
        except Exception as exc:  # noqa: BLE001 - 整页失败(浏览器起不来/导航崩溃)
            logger.error("[{}] 截图失败: {}", page_name, exc)
            for m in module_names:
                if not state[m].shots and not state[m].errors:
                    state[m].errors.append(f"页面截图失败: {exc}")

        # ---- ③ 回填(module_out 空 dict 等价 None → COALESCE 保留旧值)----
        try:
            link_rows = await repo.link_screenshot(
                ctx.hotel_id,
                ctx.collect_date,
                page_name,
                screenshot_path=None,  # ★ 恒 None:整页图列只读不写,保留历史值
                module_screenshots=module_out,
            )
            if link_rows == 0:
                logger.warning(
                    "[{}] 截图回填命中 0 行(应先 ensure_report):{}",
                    page_name,
                    ctx.collect_date,
                )
        except Exception as exc:  # noqa: BLE001
            link_error = str(exc)
            logger.error("[{}] 截图回填失败: {}", page_name, exc)

        if link_error:
            for m in module_names:
                if state[m].shots:  # 有图但没挂上 → 状态仍是 degraded,错误要可见
                    state[m].errors.append(f"回填失败: {link_error}")

        logger.info(
            "[{}] 截图完成:{} 个模块,{} 张图,回填 {} 行",
            page_name,
            len(module_names),
            len(module_out),
            link_rows,
        )
        return {
            (page_name, m): self._result(
                page_name,
                m,
                state=state[m],
                row_id=row_id,
                link_rows=link_rows,
                link_error=link_error,
            )
            for m in module_names
        }

    def _init_module_states(self, page_cfg: PageCfg, module_names: Sequence[str]) -> dict[str, _ModuleState]:
        """初始化每个目标的状态;顺带处理"模块不存在/无截图配置"两类明确情形。"""
        state: dict[str, _ModuleState] = {}
        for module_name in module_names:
            st = _ModuleState()
            sub = page_cfg.sub_module(module_name)
            if sub is None:
                st.errors.append(
                    f"页 {page_cfg.url} 无子模块 {module_name!r};"
                    f"已知:{[m.name for m in page_cfg.sub_modules]}"
                )
            else:
                st.window = (sub.windows or [DEFAULT_WINDOW])[0]
                if not sub.screenshot_modules:
                    # 明确"无图可截"(如 服务概览 / 用户行为),不是失败
                    st.skipped.append({"name": module_name, "reason": "该子模块无 screenshot_modules 配置"})
            state[module_name] = st
        return state

    async def _walk_page(
        self,
        ctx: ExtractContext,
        page_cfg: PageCfg,
        page_name: str,
        module_names: Sequence[str],
        state: dict[str, _ModuleState],
    ) -> dict[str, str]:
        """在**一个** page session 内走完整页,返回 ``module_out``(键=截图模块名)。"""
        groups = self._group_by_url(page_cfg, module_names, state)
        module_out: dict[str, str] = {}
        #: ``used_keys`` 作用域 = **单页**(旧 ``screenshoter.py:351`` 每次 ``_collect_page``
        #: 重新初始化);键 ``(target_url, selector)``,值 = 首张图路径(**只写不读**)。
        used_keys: dict[tuple[str, str], str] = {}

        # ★ 登录态走 ctx.session(SessionRef);page_session 是 async 上下文管理器,
        #   yield (browser, context, page) 三元组。不回写 storage_state:登录态回写
        #   由登录管家负责,避免与取数通道并发写同一份登录态文件。
        async with self.pool.page_session(ctx.session) as (_browser, _context, page):
            for url, entries in groups.items():
                for entry_module, _entry_sub in entries:
                    state[entry_module].urls.append(url)
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=self._nav_timeout_ms)
                except Exception as exc:  # noqa: BLE001 - 导航失败:该 URL 下全部目标失败
                    logger.warning("[{}] 导航失败({}): {}", page_name, url, exc)
                    for module_name, _sub in entries:
                        state[module_name].errors.append(f"导航失败: {exc}")
                    continue
                # 导航后:等图表渲染(8s)→ 消弹窗;两者都不抛
                await _wait_charts_ready(page, max_seconds=self.settings.browser.chart_ready_wait_s)
                await _dismiss_notifications(page)

                for module_name, sub in entries:
                    for mod in sub.screenshot_modules:
                        outcome = await self._shoot_module(
                            page,
                            ctx=ctx,
                            page_name=page_name,
                            target_url=url,
                            mod=mod,
                            used_keys=used_keys,
                        )
                        self._record(state[module_name], outcome)
                        if outcome.state == "saved" and outcome.rel_path:
                            module_out[outcome.name] = outcome.rel_path
        return module_out

    def _group_by_url(
        self,
        page_cfg: PageCfg,
        module_names: Sequence[str],
        state: dict[str, _ModuleState],
    ) -> dict[str, list[tuple[str, SubModuleCfg]]]:
        """按 ``sub_module.url`` 分组(旧 ``screenshoter.py:354-360``:同一 URL 只导航一次)。

        没有 ``screenshot_modules`` 的子模块**不进**分组(省掉一次无意义导航)。
        """
        groups: dict[str, list[tuple[str, SubModuleCfg]]] = {}
        for module_name in module_names:
            sub = page_cfg.sub_module(module_name)
            if sub is None or not sub.screenshot_modules:
                continue
            groups.setdefault(sub.url or page_cfg.url, []).append((module_name, sub))
        return groups

    # ------------------------------------------------------------------
    # 单个截图条目
    # ------------------------------------------------------------------

    async def _shoot_module(
        self,
        page: Page,
        *,
        ctx: ExtractContext,
        page_name: str,
        target_url: str,
        mod: ScreenshotModuleCfg,
        used_keys: dict[tuple[str, str], str],
    ) -> ShotOutcome:
        """一个 ``screenshot_modules[*]`` 条目的完整交互 + 截图。

        顺序**逐字**照旧 ``screenshoter.py:368-497``(顺序敏感):

          1. ``used_keys`` 去重(单页、先到先得);
          2. ``require_text`` 入口前置判定 → 未命中**快跳**(不报错、不等 30s);
          3. ``tabs`` + ``click``(attached 15s → ``skip_click_on`` → sleep 6s → 点击 10s);
          4. ``clicks`` 多步序列(前置 sleep 6s,成功步间 sleep 8s);
          5. 等容器可见 30s(超时 → ``failed``);
          6. 选**首个可见**容器 + ``_wait_content``(20s,超时照截);
          7. 页签切换过 → 重等 20s + **重选**容器(未就绪 → ``failed``);
          8. ``loc.screenshot(type="jpeg", quality=settings.screenshot.jpeg_quality)``
             —— **元素截图,没有 ``clip``**(``_pick_module_container`` / ``_clip_union``
             已退役,规格 §1.7.1)。

        被跳过/失败的条目**不写入** ``module_out``(键不会出现在
        ``module_screenshots_json`` 里,与旧系统一致)。
        """
        key = (target_url, mod.selector)
        if key in used_keys:
            logger.info(
                "截图器:模块「{}」与「{}」同容器({}),跳过重复截图",
                mod.name,
                used_keys[key],
                mod.selector,
            )
            return ShotOutcome(
                name=mod.name,
                state="deduped",
                reason=f"同容器已截图(url={target_url}, selector={mod.selector})",
            )

        # ② require_text:入口前置条件(★ 在 tabs/clicks **之前**,旧 376-383 行)
        if mod.require_text:
            try:
                if await page.locator(f"text={mod.require_text}").count() == 0:
                    logger.info(
                        "截图器:子模块「{}」无入口(页面缺「{}」),跳过截图",
                        mod.name,
                        mod.require_text,
                    )
                    return ShotOutcome(
                        name=mod.name,
                        state="missing_entry",
                        reason=f"页面缺入口文本「{mod.require_text}」",
                    )
            except Exception:  # noqa: BLE001 - 检测异常不阻断(旧 382-383)
                pass

        # ③ tabs + click;返回值 = 是否"需要页签切换后的重等/重选"
        tab_switched = False
        if mod.tabs:
            tab_switched = await self._click_tab(page, mod=mod, today=ctx.collect_date)

        # ④ clicks 多步点击
        if mod.clicks:
            await self._run_clicks(page, mod=mod)

        # ⑤ 等容器可见(30s)
        try:
            await page.wait_for_selector(mod.selector, state="visible", timeout=MODULE_VISIBLE_TIMEOUT_MS)
        except Exception as exc:  # noqa: BLE001 - 旧行为:未就绪即跳过本模块
            logger.warning("截图器:子模块「{}」容器未就绪({}),跳过", mod.name, exc)
            return ShotOutcome(name=mod.name, state="error", error=f"容器 {mod.selector} 未就绪: {exc}")

        # ⑥ 选首个可见容器 + 等内容(20s;超时不跳过)
        loc = await _pick_visible_container(page, mod.selector)
        if loc is None:
            loc = page.locator(mod.selector).first
        await _wait_content(page, loc)

        # ⑦ 页签切换过 → 重等 20s + 重选容器(旧 479-489;skip_click_on 命中时整段跳过)
        if tab_switched:
            try:
                await page.wait_for_selector(
                    mod.selector,
                    state="visible",
                    timeout=AFTER_TAB_VISIBLE_TIMEOUT_MS,
                )
            except Exception as exc:  # noqa: BLE001 - 旧 486 行文案
                logger.warning("截图器:页签切换后「{}」内容未就绪,跳过", mod.name)
                return ShotOutcome(
                    name=mod.name,
                    state="error",
                    error=f"页签切换后内容未就绪: {exc}",
                )
            # ★ 旧实现:485 行的第二次 ``_wait_content`` 用的仍是切换前的 ``loc``,
            #   488-489 行才重选 —— 这里保持同样的顺序(重等 → 重选)。
            await _wait_content(page, loc)
            loc = await _pick_visible_container(page, mod.selector)
            if loc is None:
                loc = page.locator(mod.selector).first

        # ⑧ 元素截图(★ 无 clip)
        path = ctx.layout.screenshot_path(
            ctx.hotel.name,
            ctx.collect_date,
            page_name,
            mod.name,
            ext=SHOT_EXT,
        )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            await loc.screenshot(
                path=str(path),
                type="jpeg",
                quality=self.shot_settings.jpeg_quality,
            )
        except Exception as exc:  # noqa: BLE001 - 旧 497 行文案
            logger.warning("截图器:子模块「{}」截图失败: {}", mod.name, exc)
            return ShotOutcome(name=mod.name, state="error", error=f"截图失败: {exc}")

        try:
            size: int | None = path.stat().st_size
        except OSError:  # pragma: no cover - 落盘成功但取不到大小(不因此判失败)
            size = None
        rel_path = ctx.layout.to_relative(path)
        logger.success("截图器:子模块截图已保存: {}({})", path, mod.name)

        oversize = size is not None and size > self.shot_settings.max_bytes
        if oversize:
            # 仅告警:不删、不重截、不抛错(旧 286-289 行)
            logger.warning(
                "截图器:单张截图超 {}KB({},{}KB),建议降低 JPEG 质量(当前 quality={})",
                round(self.shot_settings.max_bytes / 1024),
                path,
                round((size or 0) / 1024.0, 1),
                self.shot_settings.jpeg_quality,
            )
        used_keys[key] = str(rel_path)  # 值只写不读(旧 495 行同)
        return ShotOutcome(
            name=mod.name,
            state="saved",
            rel_path=rel_path,
            size=size,
            oversize=oversize,
        )

    # ------------------------------------------------------------------
    # ★ 现场 URL 截图(段2 消费:预警附图 / 报告项「类型二」附图)
    # ------------------------------------------------------------------

    async def shot_url(
        self,
        ctx: ExtractContext,
        url: str,
        name: str,
        *,
        selector: str | None = None,
        click: str | None = None,
    ) -> str | None:
        """打开 ``url`` 截一张图(整页或元素),返回**相对路径**;失败返回 ``None``。

        与 :meth:`run` 的关系
        ---------------------

        ``run`` 是**批量、按页、按模块配置驱动**的定时截图(05:30 任务);
        本方法是**临时、按 URL**的单张截图,给段2 的三个场景用:

          * 预警附图(``config/alert_shots.json`` 的 5 个目标);
          * 报告项「类型二」附图(``report_schedule.json`` 的 ``images[].shot``);
          * FAQ 的 ``{"full": true}`` 兜底图。

        ★ **失败只告警不抛**(段2 §5.7 / T2E.6:附图截图失败 → 仅告警,文本照发)。
        这也是它与 ``run`` 的第三处差别:``run`` 把异常落进 ``ExtractResult.status``,
        由调用方汇总;本方法直接返回 ``None``,调用方**不需要 try**。

        ★ **不落 ``collect_reports``**:整页图列(``screenshot_path``)在本项目里
        是"只读不写"的历史列(见 :meth:`link_screenshot` 的 ``screenshot_path=None``),
        段2 的附图属于**推送产物**而非采集产物,落库会产生第二个事实源。
        文件本身落在 ``var/screenshots/<hotel>/<YYYYMMDD>/``,由清理任务统一管理。
        """
        try:
            async with self.pool.page_session(ctx.session) as (_browser, _context, page):
                await page.goto(url, wait_until="domcontentloaded", timeout=self._nav_timeout_ms)
                await _wait_charts_ready(page, max_seconds=self.settings.browser.chart_ready_wait_s)
                await _dismiss_notifications(page)
                if click:
                    await _click_first_visible(page.locator(f"text={click}"))
                    await asyncio.sleep(JS_INIT_SLEEP_S)
                path = ctx.layout.screenshot_path(
                    ctx.hotel.name,
                    ctx.collect_date,
                    name,
                    _url_key(url, selector),
                    ext=SHOT_EXT,
                )
                path.parent.mkdir(parents=True, exist_ok=True)
                if selector:
                    loc = await _pick_visible_container(page, selector)
                    if loc is None:
                        loc = page.locator(selector).first
                    await _wait_content(page, loc)
                    await loc.screenshot(
                        path=str(path), type="jpeg", quality=self.shot_settings.jpeg_quality
                    )
                else:
                    await page.screenshot(
                        path=str(path),
                        type="jpeg",
                        quality=self.shot_settings.jpeg_quality,
                        full_page=True,
                    )
            rel = ctx.layout.to_relative(path)
            logger.success("现场截图已保存: {}({})", path, name)
            return rel
        except Exception as exc:  # noqa: BLE001 - ★ 附图失败只告警不抛(文本照发)
            logger.warning("现场截图失败({} {}): {}", name, url, exc)
            return None

    async def _click_tab(self, page: Page, *, mod: ScreenshotModuleCfg, today: date) -> bool:
        """``tabs`` + ``click``:页签点击(旧 ``screenshoter.py:389-419``)。

        返回 ``True`` 表示"页签切换流程执行过,调用方需要重等 + 重选容器"。

        * ``page.wait_for_selector(tabs, state="attached", timeout=15_000)`` ——
          **``attached`` 不是 ``visible``**:容器可能自身 hidden 但内部 tab 可点
          (实锤:``#J_ReportType ul`` 维度切换 hidden、``span``「房型」可点);
        * ``skip_click_on`` 命中 → ``click`` 置空 → **不点击**;
        * 点击前 ``sleep(6.0)``(SPA 的 DOM 挂载早于事件绑定,attached 后立刻点无效);
        * 点击表达式 ``page.locator(tabs).locator(f"text={click}").first.click(timeout=10_000)``。
        """
        tab_loc = page.locator(mod.tabs).first if mod.tabs else None
        try:
            await page.wait_for_selector(mod.tabs, state="attached", timeout=TAB_ATTACHED_TIMEOUT_MS)
        except Exception:  # noqa: BLE001 - 旧 667-668:定位失败 → tab_loc=None
            tab_loc = None

        target: str | None = mod.click
        if should_skip_click(mod, today):
            # 甲方口径:周一/月 1 保持页面默认视图(不点击、也不重等 20s)
            target = None

        if tab_loc is None or not target:
            return False

        await asyncio.sleep(JS_INIT_SLEEP_S)
        try:
            await page.locator(mod.tabs).locator(f"text={target}").first.click(timeout=TAB_CLICK_TIMEOUT_MS)
        except Exception as exc:  # noqa: BLE001 - 旧 418-419:沿用当前显示
            logger.warning("截图器:页签「{}」点击失败({}),沿用当前显示", target, exc)
        return True

    async def _run_clicks(self, page: Page, *, mod: ScreenshotModuleCfg) -> None:
        """``clicks`` 多步点击序列(旧 ``screenshoter.py:424-467``)。

        为什么这么慢:sleep 6s 是等 **JS 事件绑定**(DOM 可见 ≠ 事件就绪,实测 goto 后
        1s 点击无效、~6s 才绑定 tab 事件);sleep 8s 是等点击后的**数据请求 + 渲染**
        (实测点击后 3-4s 才发请求、再渲染 2-3s),不够就会点下一步时把数据切回去。

        每步三级回退(★ 容器内优先 —— 页面顶栏同名 tab 不触发数据请求):

          ① 容器内 ``scope.locator(f"text={label}")``(先等 20s 可见);
          ② 页面级 ``page.get_by_text(label, exact=True)``(先等 8s 可见,排除"昨"等
             打字机半字节点);
          ③ 交互元素 ``button/a/li:has-text('label')``。

        可见容器选取用 ``_click_first_visible``(上限 10)。单步失败**不阻断**后续步骤。
        ``scope`` 上带 ``.first`` 再等:多命中时 Playwright strict mode 会直接抛错,
        旧实现只等不点所以没暴露,这里顺手加固(不改变等待时长 20s)。
        """
        await asyncio.sleep(JS_INIT_SLEEP_S)
        scope: Locator | None = page.locator(mod.selector) if mod.selector else None
        for label in mod.clicks or ():
            try:
                clicked = False
                if scope is not None:
                    try:
                        await scope.first.wait_for_selector(
                            f"text={label}", state="visible", timeout=CLICKS_SCOPE_WAIT_MS
                        )
                    except Exception:  # noqa: BLE001 - 容器内没有则回退页面级
                        pass
                    clicked = await _click_first_visible(scope.locator(f"text={label}"))
                if not clicked:
                    try:
                        await page.wait_for_selector(
                            f"text={label}", state="visible", timeout=CLICKS_PAGE_WAIT_MS
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    clicked = await _click_first_visible(page.get_by_text(label, exact=True))
                if not clicked:
                    for css in CLICKS_FALLBACK_CSS:
                        clicked = await _click_first_visible(page.locator(f"{css}('{label}')"))
                        if clicked:
                            break
                if clicked:
                    logger.info("截图器:clicks「{}」已点击({})", label, mod.name)
                    await asyncio.sleep(CLICKS_STEP_SLEEP_S)
                else:
                    logger.warning("截图器:clicks「{}」无可点击元素({}),跳过", label, mod.name)
            except Exception as exc:  # noqa: BLE001 - 单步失败不阻断后续
                logger.warning("截图器:clicks「{}」点击失败({}),继续", label, exc)

    # ------------------------------------------------------------------
    # 汇总
    # ------------------------------------------------------------------

    @staticmethod
    def _record(state: _ModuleState, outcome: ShotOutcome) -> None:
        """把一个条目结果并进模块状态。被跳过/失败的条目**不进** ``shots``。"""
        if outcome.state == "saved" and outcome.rel_path:
            state.shots[outcome.name] = outcome.rel_path
            if outcome.size is not None:
                state.sizes[outcome.name] = outcome.size
            if outcome.oversize:
                state.oversize.append(outcome.name)
            return
        if outcome.state == "error":
            state.errors.append(outcome.error or f"{outcome.name}: 截图失败")
            return
        state.skipped.append({"name": outcome.name, "reason": outcome.reason})

    def _result(
        self,
        page_name: str,
        module_name: str,
        *,
        state: _ModuleState,
        row_id: int | None = None,
        link_rows: int | None = None,
        link_error: str | None = None,
        note: str | None = None,
    ) -> ExtractResult:
        """把一个模块的状态装成 :class:`ExtractResult`(四态见模块 docstring)。"""
        shots = state.shots
        status = aggregate_status(shots=len(shots), errors=len(state.errors))
        error = "; ".join(state.errors) if state.errors else None
        if note:
            error = f"{note}; {error}" if error else note
        return ExtractResult(
            status=status,
            channel=SCREENSHOT_CHANNEL,
            # payload = {截图模块名: 相对路径};形状与 module_screenshots_json 一致
            payload=dict(shots) if shots else None,
            error=error,
            # ★ 相对路径;一个模块可能有多张图(如 金字塔-效果 / -计划),
            #   这里取**第一张**方便调用方快速定位,完整映射看 payload/detail。
            raw_path=next(iter(shots.values()), None) if shots else None,
            target=ExtractTarget(
                page=page_name,
                module=module_name,
                # 截图取的是页面**当前视图**,不区分窗口;填该子模块的首个配置窗口
                # 仅为满足 ExtractTarget 的三元组形状(值一定是合法窗口名)。
                window=state.window or DEFAULT_WINDOW,
            ),
            detail={
                "page": page_name,
                "module": module_name,
                "shots": dict(shots),
                "bytes": dict(state.sizes),
                "oversize": list(state.oversize),
                "skipped": list(state.skipped),
                "errors": list(state.errors),
                "urls": list(state.urls),
                "collect_report_id": row_id,
                "link_rows": link_rows,
                "link_error": link_error,
                "note": note,
            },
        )


def _url_key(url: str, selector: str | None = None) -> str:
    """现场截图的文件名 key:``url``(+``selector``)的短哈希。

    用哈希而不是 URL 原文:URL 里有 ``?microJump=true`` 这类查询串,
    直接进文件名会撞 Windows 非法字符表(``?`` / ``:`` / ``/``)
    —— :func:`hoteldata.infra.paths.safe_name` 会兜住,但会把不同 URL 压成同名。
    """
    raw = f"{url}|{selector or ''}".encode()
    return hashlib.sha1(raw).hexdigest()[:10]  # noqa: S324 - 仅作文件名去重,非安全用途
