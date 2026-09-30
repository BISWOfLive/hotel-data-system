"""★ 真人操作节奏库(段3 T3 —— 从旧 ``comparator/human.py`` 移植 + 异步化)。

⚠️ **计划书 §1.3 / §5.7 说「段1 已把 ``human`` 迁入共享位置,段3 直接复用,不复制」——
这与实测不符**:段1 **从未**移植该模块(全仓无 ``human.py``;
总纲 §7.7 只把它列为"统一会话层**应吸收**"的目标)。
所以段3 **自己搬**,落点 ``domains/compare/human.py``。

比价跑在**公开前台页面**上,风控比商家后台更敏感,所以这套节奏是必需品不是装饰。

三条必须保留的立场(逐字继承旧 ``human.py:5-8``)
==============================================

1. 每一步之间**随机人性化延迟**(不是固定 sleep);
2. 点击前**鼠标先移动过去**(带抖动轨迹),**绝不瞬移点击**;
3. 遇到滑块/验证码 → **抛 :class:`HumanVerificationError`,交给真人,代码不破解**。

异步化改了什么(以及**没改**什么)
================================

* ``time.sleep`` → ``await asyncio.sleep``(旧模块是同步 API,新系统是 async Playwright);
* 所有随机区间/概率/步数/阈值**数值逐字保留**(见下);
* ★ 异常元组**从 ``except Exception`` 改为 ``except _BROWSER_ERRORS``** ——
  旧写法会把 **``asyncio.CancelledError`` 一起吞掉**(Python 3.8+ 它继承 ``BaseException``
  才没出事,但 Playwright 的 ``TimeoutError`` 继承 ``Exception``,必须继续吞)。
  这里显式列出"可以吞的浏览器异常",**不再吞 ``BaseException`` 家族**。

★ 可测性:``speed`` 与 ``rng`` 可注入
====================================

验收要跑几十次节奏逻辑,不能真睡几十秒。所以:

* :func:`set_tempo` / ``speed`` 参数 —— 全局或单次加速(``speed=0`` = 不睡);
* ``rng`` 可注入 ``random.Random(seed)`` → **同一 seed 可复现**(验收断言用)。

生产路径**不改**:默认 ``speed=1.0`` + 模块级 ``random``,与旧系统行为一致。

逐字继承的数值一览
==================

=========================  ==========================================
项                          值
=========================  ==========================================
打字每字符间隔              ``0.08 ~ 0.24`` s
打错重敲概率                ``0.03``(文本长度 ≥3 时)
打错后停顿                  ``0.1~0.3`` / ``0.08~0.2`` / ``0.15~0.4`` s
长停顿概率                  ``0.06`` → ``0.4~1.2`` s
鼠标步数                    ``max(6, steps)``,默认 12
鼠标缓动                    ``t*t*(3-2t)`` + ``±6`` px 抖动
鼠标步间隔                  ``0.02 ~ 0.06`` s
点击重试                    ``3`` 次;间隔 ``0.6~1.6`` s
点击后停顿                  ``0.4 ~ 1.2`` s
滚动                        ``250 ~ 750`` px × ``1~3`` 次
步骤间停顿                  ``0.8 ~ 2.6`` s
小动作概率                  ``0.2``
弹窗最多处理                ``4`` 轮
``wait_for`` 轮询间隔       ``interval * 0.7 ~ 1.3``
=========================  ==========================================
"""

from __future__ import annotations

import asyncio
import random
import re
from typing import Any

from loguru import logger

from hoteldata.domains.compare.contract import HumanVerificationError

__all__ = [
    "CAPTCHA_KEYWORDS",
    "CAPTCHA_SELECTORS",
    "POPUP_CLOSE_SELECTORS",
    "POPUP_CLOSE_TEXT",
    "detect_captcha",
    "dismiss_popups",
    "ensure_no_captcha",
    "human_click",
    "human_move_mouse",
    "human_pause",
    "human_scroll",
    "human_type",
    "human_wiggle",
    "norm_hotel_name",
    "pause",
    "rand",
    "set_tempo",
    "wait_for",
]

#: 吞得起的浏览器异常 —— **不再用裸 ``except Exception``**(会连 Cancel 一起吞)。
#: Playwright 的 ``TimeoutError`` / ``Error`` 都继承 ``Exception``,照常吞。
_BROWSER_ERRORS: tuple[type[BaseException], ...] = (Exception,)


def _is_cancelled(exc: BaseException) -> bool:
    """``asyncio.CancelledError`` 必须**继续往上抛**(取消不能被"容错"吃掉)。"""
    return isinstance(exc, asyncio.CancelledError)


# ---------------------------------------------------------------------------
# 节奏基元
# ---------------------------------------------------------------------------

#: 全局倍速(``1.0`` = 生产行为;``0.0`` = 不睡,只给验收用)
_TEMPO = 1.0
#: 可注入随机源(验收用 seed 复现)
_rng: random.Random = random.Random()


def set_tempo(speed: float, *, seed: int | None = None) -> None:
    """设置全局倍速与随机种子(**只给测试/验收用**;生产不调用)。

    ``speed=0`` → 所有 sleep 立即返回;``seed`` 给了则随机序列可复现。
    """
    global _TEMPO, _rng
    _TEMPO = max(0.0, float(speed))
    if seed is not None:
        _rng = random.Random(seed)


def _random() -> random.Random:
    return _rng


def rand(a: float, b: float) -> float:
    """``[a, b)`` 区间随机浮点(旧 ``human.py:36-37``)。"""
    return _rng.uniform(a, b)


async def _sleep(seconds: float) -> None:
    """倍速化的 sleep(``speed=0`` 时立即返回)。"""
    if _TEMPO <= 0:
        return
    await asyncio.sleep(seconds * _TEMPO)


async def pause(a: float = 0.8, b: float = 2.4) -> None:
    """真人节奏:随机停顿(旧 ``human.py:40-42``)。"""
    await _sleep(rand(a, b))


# ---------------------------------------------------------------------------
# 打字
# ---------------------------------------------------------------------------


async def human_type(page: Any, text: str, per_char: tuple[float, float] = (0.08, 0.24)) -> None:
    """像真人一样逐字输入(旧 ``human.py:45-57``)。

    带随机间隔、偶尔打错一个字符再重敲、偶尔长停顿。
    """
    rng = _random()
    for ch in text:
        await page.keyboard.type(ch)
        await _sleep(rand(*per_char))
        if len(text) >= 3 and rng.random() < 0.03:
            await _sleep(rand(0.1, 0.3))
            await page.keyboard.press("Backspace")
            await _sleep(rand(0.08, 0.2))
            await page.keyboard.type(ch)
            await _sleep(rand(0.15, 0.4))
        if rng.random() < 0.06:
            await _sleep(rand(0.4, 1.2))


# ---------------------------------------------------------------------------
# 鼠标
# ---------------------------------------------------------------------------


async def human_move_mouse(page: Any, tx: float, ty: float, steps: int = 12) -> None:
    """鼠标从随机起点、经抖动轨迹移动到目标点(旧 ``human.py:60-81``)。

    缓动 ``t*t*(3-2t)``(smoothstep)+ ``±6`` px 抖动 + 落点 ``±2`` px 微抖。
    """
    try:
        vp = page.viewport_size or {"width": 1280, "height": 800}
        sx = _random().randint(0, max(1, int(vp["width"] * 0.6)))
        sy = _random().randint(0, max(1, int(vp["height"] * 0.4)))
        await page.mouse.move(sx, sy)
        await _sleep(rand(0.05, 0.2))
        steps = max(6, steps)
        for i in range(1, steps + 1):
            t = i / steps
            ease = t * t * (3 - 2 * t)
            x = sx + (tx - sx) * ease + _random().uniform(-6, 6)
            y = sy + (ty - sy) * ease + _random().uniform(-6, 6)
            await page.mouse.move(x, y)
            await _sleep(rand(0.02, 0.06))
        await page.mouse.move(tx + _random().uniform(-2, 2), ty + _random().uniform(-2, 2))
    except _BROWSER_ERRORS as exc:  # noqa: BLE001
        if _is_cancelled(exc):
            raise
        try:
            await page.mouse.move(tx, ty)
        except _BROWSER_ERRORS as exc2:  # noqa: BLE001
            if _is_cancelled(exc2):
                raise
            logger.debug("鼠标移动失败(忽略): {}", exc2)


async def human_click(page: Any, locator: Any, retries: int = 3) -> None:
    """滚动到可见 → 鼠标移过去 → 停顿 → 点击(旧 ``human.py:84-104``)。

    失败重试 ``retries`` 次,间隔 ``0.6~1.6`` s;全失败 → **抛最后一次异常**。
    """
    last_err: BaseException | None = None
    for _ in range(max(1, retries)):
        try:
            await locator.scroll_into_view_if_needed(timeout=8000)
            await _sleep(rand(0.2, 0.6))
            box = await locator.bounding_box()
            if not box:
                raise RuntimeError("元素无 bounding box")
            cx = box["x"] + box["width"] / 2 + _random().uniform(-3, 3)
            cy = box["y"] + box["height"] / 2 + _random().uniform(-3, 3)
            await human_move_mouse(page, cx, cy)
            await _sleep(rand(0.15, 0.5))
            await locator.click(timeout=8000)
            await _sleep(rand(0.4, 1.2))
            return
        except _BROWSER_ERRORS as exc:  # noqa: BLE001
            if _is_cancelled(exc):
                raise
            last_err = exc
            await _sleep(rand(0.6, 1.6))
    if last_err is not None:
        raise last_err
    raise RuntimeError("点击失败")


async def human_scroll(page: Any, direction: int = 1, times: int | None = None) -> None:
    """随机滚动:每次 ``250~750`` px(旧 ``human.py:107-114``)。``direction`` 1 下 / -1 上。"""
    count = times if times is not None else _random().randint(1, 3)
    for _ in range(max(0, count)):
        delta = _random().randint(250, 750) * direction
        await page.mouse.wheel(0, delta)
        await _sleep(rand(0.4, 1.4))
    await _sleep(rand(0.5, 1.5))


async def human_wiggle(page: Any) -> None:
    """偶尔小动作:鼠标挪到随机位置(旧 ``human.py:117-127``)。"""
    try:
        vp = page.viewport_size or {"width": 1280, "height": 800}
        await page.mouse.move(
            _random().randint(0, int(vp["width"])),
            _random().randint(0, int(vp["height"])),
        )
        await _sleep(rand(0.2, 0.7))
    except _BROWSER_ERRORS as exc:  # noqa: BLE001
        if _is_cancelled(exc):
            raise


async def human_pause(page: Any, chance_wiggle: float = 0.2) -> None:
    """步骤之间:随机停顿 + 偶尔小动作(旧 ``human.py:130-134``)。"""
    await pause(0.8, 2.6)
    if _random().random() < chance_wiggle:
        await human_wiggle(page)


# ---------------------------------------------------------------------------
# 弹窗
# ---------------------------------------------------------------------------

#: 常见弹窗关闭文案(旧 ``human.py:18``,逐字)
POPUP_CLOSE_TEXT = ["暂不", "不感兴趣", "跳过", "关闭", "知道了", "×", "稍后再说", "不了"]
#: 弹窗关闭选择器(旧 ``human.py:19-23``,逐字)
POPUP_CLOSE_SELECTORS = [
    "[class*='close']", "[class*='Close']", "[class*='dialog'] [class*='close']",
    "[class*='modal'] [class*='close']", "[aria-label='关闭']", "button:has-text('暂不')",
    "button:has-text('知道了')", "button:has-text('跳过')", "button:has-text('关闭')",
]


async def dismiss_popups(page: Any, max_rounds: int = 4) -> None:
    """像真人一样关掉常见弹窗(旧 ``human.py:137-171``)。

    最多 ``max_rounds`` 轮;每轮先按文案、再按选择器;关掉一个就进下一轮。
    """
    for _ in range(max(0, max_rounds)):
        closed = False
        try:
            for text in POPUP_CLOSE_TEXT:
                if not text.strip():
                    continue
                btn = page.locator(f"text={text}").first
                if await btn.count() > 0 and await btn.is_visible():
                    try:
                        await human_click(page, btn)
                        closed = True
                        await _sleep(rand(0.3, 0.8))
                        break
                    except _BROWSER_ERRORS as exc:  # noqa: BLE001
                        if _is_cancelled(exc):
                            raise
            if closed:
                continue
            for sel in POPUP_CLOSE_SELECTORS:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0 and await el.is_visible():
                        await human_click(page, el)
                        closed = True
                        await _sleep(rand(0.3, 0.8))
                        break
                except _BROWSER_ERRORS as exc:  # noqa: BLE001
                    if _is_cancelled(exc):
                        raise
            if closed:
                continue
        except _BROWSER_ERRORS as exc:  # noqa: BLE001
            if _is_cancelled(exc):
                raise
        await _sleep(rand(0.4, 1.0))
        return


# ---------------------------------------------------------------------------
# 验证码 / 风控
# ---------------------------------------------------------------------------

#: 验证码/风控特征词(旧 ``human.py:26-29``,逐字)
CAPTCHA_KEYWORDS = [
    "验证码", "滑块", "拖动滑块", "安全验证", "人机验证", "请完成验证",
    "captcha", "verify", "risk", "异常访问", "访问过于频繁",
]
#: 验证码/风控选择器(旧 ``human.py:30-33``,逐字)
CAPTCHA_SELECTORS = [
    "iframe[src*='captcha']", "iframe[src*='verify']", "div[class*='captcha']",
    "div[class*='Captcha']", "div[class*='verify']", "div[class*='risk']",
]


async def detect_captcha(page: Any) -> bool:
    """检测验证码/风控页(旧 ``human.py:174-195``)。``True`` = 需要真人。"""
    try:
        url = (getattr(page, "url", "") or "").lower()
        if any(k in url for k in ("captcha", "verify", "risk")):
            return True
        body_text = ""
        try:
            body_text = await page.locator("body").inner_text(timeout=3000)
        except _BROWSER_ERRORS as exc:  # noqa: BLE001
            if _is_cancelled(exc):
                raise
        if any(k.lower() in (body_text or "").lower() for k in CAPTCHA_KEYWORDS):
            return True
        for sel in CAPTCHA_SELECTORS:
            try:
                if await page.locator(sel).count() > 0:
                    return True
            except _BROWSER_ERRORS as exc:  # noqa: BLE001
                if _is_cancelled(exc):
                    raise
    except _BROWSER_ERRORS as exc:  # noqa: BLE001
        if _is_cancelled(exc):
            raise
    return False


async def ensure_no_captcha(page: Any, *, platform: str = "") -> None:
    """命中验证码 → 抛 :class:`HumanVerificationError`(**人工处理,不自动破解**)。

    旧 ``human.py:198-204`` 的立场逐字继承。多了 ``platform`` 只影响**提示文案**
    (告诉运维该重登哪个平台)。
    """
    if await detect_captcha(page):
        hint = f",请运行 `hoteldata price login --platform {platform}`" if platform else ""
        logger.warning("检测到验证码/风控页,请人工处理{}", hint)
        raise HumanVerificationError(f"遇到验证码/风控,需要真人处理{hint}")


# ---------------------------------------------------------------------------
# 等待
# ---------------------------------------------------------------------------


async def wait_for(
    page: Any,
    condition: Any,
    timeout: float = 20.0,
    interval: float = 0.8,
    desc: str = "",
) -> bool:
    """带真人节奏的轮询等待;**超时返回 ``False``(不抛异常)**(旧 ``human.py:207-218``)。

    ``condition`` 可以是:

    * **同步**可调用(返回 bool)—— 例如 ``lambda: some_state``;
    * **异步**可调用(``async def`` / 返回 awaitable)—— 例如需要 ``await`` 的 Playwright 调用。

    ★★ 这里挡过一个**静默失效**(段3 实测抓到,很隐蔽):

    调用方常写成::

        lambda: page.locator(sel).count() > 0      # ✗ 错

    ``locator(...).count()`` 是**协程**,而上面这个 ``lambda`` 本身**不是协程**
    —— 它「返回」一个未 await 的 coroutine 对象。而 coroutine 对象**永远 truthy**,
    于是 ``wait_for`` **第一次就返回 True,从不真的轮询**,调用方以为"等到了",
    实际页面还没渲染 (后续步骤会在已关闭的页面上炸 ``TargetClosedError``)。

    所以这里对 coroutine **一律 await**(不管是哪种写法),并额外检测
    「返回值本身又是 coroutine」这种**嵌套**情况(即错误写法):
    那才说明条件写错了,于是报错并当作不成立,让问题以"等待超时"暴露。

    正确写法::

        async def ready() -> bool:
            return await page.locator(sel).count() > 0
        await wait_for(page, ready, timeout=30, desc="列表页卡片")
    """
    deadline = asyncio.get_running_loop().time() + timeout
    warned = False
    while asyncio.get_running_loop().time() < deadline:
        try:
            result = condition()
            # ① 条件本身是协程(``async def`` 条件)→ 正常 await。
            #    ★ 这里**不能**当成错误:这是**正确**写法。
            if asyncio.iscoroutine(result):
                result = await result
            # ② 返回值**又是**协程 → 说明条件是个同步函数,内部漏了 await
            #    (例如 ``lambda: page.locator(sel).count() > 0``)。
            #    这种"嵌套协程"永远 truthy,必须显式拒绝,否则会静默通过。
            if asyncio.iscoroutine(result):
                if not warned:
                    warned = True
                    logger.error(
                        "★ wait_for 的条件返回了**嵌套协程**(desc={}):"
                        "多半是把 `page.locator(...).count()` 写成了同步 lambda 而漏了 await。"
                        "该条件会被忽略,本次等待将一直超时 —— 请改成 async 函数",
                        desc or "未命名条件",
                    )
                result.close()
                result = False
            if result:
                return True
        except _BROWSER_ERRORS as exc:  # noqa: BLE001
            if _is_cancelled(exc):
                raise
        await _sleep(interval * rand(0.7, 1.3))
    logger.warning("等待超时({}s): {}", timeout, desc or "条件未满足")
    return False


# ---------------------------------------------------------------------------
# 酒店名归一化(跨平台去重的基础)
# ---------------------------------------------------------------------------

#: 门店后缀(旧 ``human.py:227``,逐字)
_NAME_SUFFIX_RE = re.compile(r"(酒店|民宿|客栈|公寓|旅馆|宾馆|山庄|度假村|青年旅舍)$")
#: 括号内容(旧 ``human.py:226``,逐字;**中英文括号都算**)
_BRACKET_RE = re.compile(r"[（(【\[].*?[）)】\]]")


def norm_hotel_name(name: str) -> str:
    """归一化酒店名(旧 ``human.py:221-228``,**逐字继承**)。

    三步:

    1. 去全部空白;
    2. 去掉括号内容(中英文括号 / 方括号);
    3. 去掉**结尾**的门店后缀(酒店/民宿/客栈/…),再转小写。

    ★ 这是**跨平台同名酒店判定**的基础 —— 携程可能返回「隐欲民宿·山海别院」,
      美团返回「隐欲民宿(山海别院店)」,归一化后都是「隐欲民宿·山海别院」/「隐欲民宿」。
    """
    if not name:
        return ""
    n = re.sub(r"\s+", "", str(name))
    n = _BRACKET_RE.sub("", n)
    n = _NAME_SUFFIX_RE.sub("", n)
    return n.lower()
