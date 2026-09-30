"""视觉兜底读价(段3 T3C.4 / T3C.5)—— **门控默认关闭 + 交叉校验**。

★ 先说清楚:旧系统的视觉读价是**死的**,而且是九环死链
=====================================================

编排层考古的确证链:

1. ``vision_extract_prices`` —— **零调用**;
2. ``vision_read_price`` 的唯一调用点在 ``get_price``(旧 ``prices.py:254``);
3. ``get_price`` 的唯一调用点在 ``base.py:152`` —— 属于**死函数** ``_fetch_quote``;
4. 批量主路径**完全不调**视觉;
5. ``vision_locate`` 只在携程「显示地图」兜底里被调(``ctrip.py:563-567``);
6. ``.env`` **没有** ``ZHIPU_API_KEY`` → ``Config.ZHIPU_API_KEY=""``;
7. → ``_zhipu_ask`` 首行即 ``return None``(旧 ``prices.py:97-98``);
8. 归档 18 行里 ``vision_price`` 非空 **0** 条;
9. 所以"视觉读价"从来没有在生效链路上跑过一次。

段3 把它**接回主链**,但用**门控**保证"没授权时一次都不外发"。

★ 门控规则(计划书 §5.6,逐条实现)
=================================

=========================================  ==========================================================
条件                                         行为
=========================================  ==========================================================
``VISION_ENABLED=0``(**默认**)             **零视觉调用**;连 HTTP 都不发
``VISION_ENABLED=1`` 但没配 API Key          构造时**抛错**(settings 已拦),不静默尝试
``VISION_ENABLED=1`` 且配了 Key              **仅当 DOM/接口都失败**时才调;每次调用记日志
当日调用量超 ``VISION_DAILY_CALL_LIMIT``     **停用并告警**(防失控计费)
=========================================  ==========================================================

🔴 **合规前置**:调用 = 把客户酒店数据截图**外发到智谱服务器**,
启用前必须取得**客户书面授权**(总纲 R11 / §3.4)。

★ 为什么不用 ``zhipuai`` SDK(与计划书的一处偏差,实测驱动)
========================================================

计划书 §3 写「视觉兜底 → ``zhipuai``(段1 已引入)」。实测:

* ``zhipuai`` **装了但 import 就失败** —— 缺 ``sniffio`` 依赖;
* 而它并不是必需能力:智谱提供**OpenAI 兼容**的 ``chat/completions`` 接口,
  项目**已经有** ``httpx``(段1 ``infra/http.py``)。

**多引入一个会坏的依赖 + 一条 SDK 升级风险**,换来的是"少写 30 行 JSON 拼装"。
段3 因此**直接走 HTTP**(总纲 7.1 "不引入"的同一取向),并且:

* 模块**顶层不 import 任何视觉 SDK** —— 于是 ``VISION_ENABLED=0`` 时
  **零依赖、零网络**是**结构上保证**的,不是靠 if 判断;
* 视觉挂掉**不影响**比价:所有异常都被吞成本地 ``None`` + 一条 warning。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from loguru import logger

from hoteldata.settings import Settings, get_settings

__all__ = [
    "VISION_ENDPOINT",
    "VisionBudget",
    "VisionEstimator",
    "get_vision_budget",
    "reset_vision_budget",
]

#: 智谱 OpenAI 兼容端点(模型名走 ``ZHIPU_MODEL`` 配置,不硬编码 —— 总纲 §3.4)
VISION_ENDPOINT = "https://open.bigmodel.cn/api/paas/v4/chat/completions"

#: 读价提示词。要求**只回一个数字或 NONE**,便于解析且减少幻觉空间。
_PRICE_PROMPT = (
    "这是一张酒店列表/详情页的截图。请找出其中**酒店的房间价格**"
    "(通常带 ¥ 或「起」字),只回答一个阿拉伯数字(整数或一位小数),不要任何其他文字。"
    "如果截图里没有任何酒店房价(例如页面还在加载、是空白骨架屏、"
    "或只有优惠券金额),请只回答 NONE。"
)


@dataclass(slots=True)
class VisionBudget:
    """当日视觉调用配额(防失控计费 —— 段3 T5)。

    ★ 为什么用**进程内**计数而不是查库:视觉是"每张图一次 HTTP"的短动作,
      为计数去写库会让"限额"本身变成故障点。进程重启后计数归零是可接受的 ——
      真正的成本闸门是 ``VISION_ENABLED`` 默认关闭 + 客户书面授权。
    """

    limit: int
    day: date = field(default_factory=date.today)
    used: int = 0

    def _roll(self) -> None:
        today = date.today()
        if today != self.day:
            self.day = today
            self.used = 0

    @property
    def remaining(self) -> int:
        self._roll()
        return max(0, self.limit - self.used)

    def can_call(self) -> bool:
        return self.remaining > 0

    def consume(self) -> None:
        self._roll()
        self.used += 1

    def snapshot(self) -> dict[str, Any]:
        self._roll()
        return {"day": self.day.isoformat(), "limit": self.limit, "used": self.used,
                "remaining": self.remaining}


_budget: VisionBudget | None = None
_warned_exhausted = False


def get_vision_budget(settings: Settings | None = None) -> VisionBudget:
    """取(或惰性创建)当日视觉配额。"""
    global _budget
    s = settings or get_settings()
    if _budget is None or _budget.limit != s.vision.daily_call_limit:
        _budget = VisionBudget(limit=int(s.vision.daily_call_limit))
    return _budget


def reset_vision_budget() -> None:
    """重置配额计数(验收用)。"""
    global _budget, _warned_exhausted
    _budget = None
    _warned_exhausted = False


class VisionEstimator:
    """视觉读价器。**构造本身不发请求**,只在 :meth:`read_price` 时按门控决定是否调用。"""

    def __init__(self, settings: Settings | None = None, client: Any = None) -> None:
        self.settings = settings or get_settings()
        self.cfg = self.settings.vision
        #: 可注入的 ``httpx.AsyncClient``(段1 的 ``Runtime.http``);
        #: 为 ``None`` 时按需自建并复用
        self._client = client
        self._owns_client = client is None

    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """门控:总开关 + Key 双条件。**少一个都不调**。"""
        return bool(self.cfg.enabled and self.cfg.api_key)

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "model": self.cfg.model,
            "tolerance": self.cfg.price_tolerance,
            "budget": get_vision_budget(self.settings).snapshot(),
            "endpoint": VISION_ENDPOINT,
        }

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            try:
                await self._client.aclose()
            except Exception as exc:  # noqa: BLE001
                logger.debug("关闭视觉 HTTP 客户端失败: {}", exc)
            self._client = None

    # ------------------------------------------------------------------

    async def read_price(self, png_bytes: bytes) -> float | None:
        """读一张截图里的房价。

        **任何失败都返回 ``None``**(不是抛异常):视觉是第三档兜底,
        它失败只意味着"没有兜底价",不该让整次比价失败。
        唯一的例外是**配额耗尽** —— 那会 ``logger.error`` 并停用(要人看见)。
        """
        global _warned_exhausted

        if not self.enabled:
            # ★ 默认路径:零 HTTP、零 SDK、零日志噪音(除非真是"开了但没 Key")
            logger.debug("视觉兜底未启用(门控),跳过")
            return None

        budget = get_vision_budget(self.settings)
        if not budget.can_call():
            if not _warned_exhausted:
                _warned_exhausted = True
                logger.error(
                    "视觉兜底当日调用已达上限 {}(VISION_DAILY_CALL_LIMIT),已停用以免失控计费。"
                    "如需继续请明天再跑或调高限额",
                    budget.limit,
                )
            return None

        import base64

        budget.consume()
        logger.info(
            "★ 视觉兜底调用 {}/{}:model={}(客户经营数据截图将外发到智谱,"
            "须已取得书面授权)",
            budget.used,
            budget.limit,
            self.cfg.model,
        )

        payload = {
            "model": self.cfg.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _PRICE_PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64,"
                                + base64.b64encode(png_bytes).decode("ascii")
                            },
                        },
                    ],
                }
            ],
            "temperature": 0.0,
        }
        headers = {"Authorization": f"Bearer {self.cfg.api_key}"}

        try:
            client = await self._ensure_client()
            # ★ 视觉不重试:它不是幂等读、且每次调用都计费
            resp = await client.post(
                VISION_ENDPOINT,
                json=payload,
                headers=headers,
                timeout=float(self.settings.api_timeout_s),
            )
            if resp.status_code != 200:
                logger.warning("视觉兜底失败:HTTP {} {}", resp.status_code, resp.text[:200])
                return None
            data = resp.json()
            text = _extract_text(data)
            value = _parse_price_answer(text)
            logger.info("视觉兜底读到价格:{}(原文 {!r})", value, (text or "")[:40])
            return value
        except Exception as exc:  # noqa: BLE001 - 视觉是兜底,失败不影响主链路
            logger.warning("视觉兜底异常(忽略,不影响比价): {}", exc)
            return None

    async def _ensure_client(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=float(self.settings.api_timeout_s))
        return self._client


# ---------------------------------------------------------------------------
# 解析与交叉校验
# ---------------------------------------------------------------------------


def _extract_text(data: dict[str, Any]) -> str:
    """从 OpenAI 兼容响应里取文本。"""
    try:
        choices = data.get("choices") or []
        if not choices:
            return ""
        content = (choices[0].get("message") or {}).get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            return " ".join(
                str(part.get("text", "")) for part in content if isinstance(part, dict)
            ).strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def _parse_price_answer(text: str | None) -> float | None:
    """把模型回答解析成价格。``NONE`` / 空 / 非数字 → ``None``。"""
    if not text:
        return None
    up = text.strip().upper()
    if "NONE" in up or "无" in text or "没有" in text:
        return None
    from hoteldata.domains.compare.price import parse_price

    # 模型有时会带 "¥236" 或 "236元",交给统一的 parse_price(带同样的守卫)
    return parse_price(text, require_symbol=False)


def cross_check(
    dom_price: float | None,
    vision_price: float | None,
    tolerance: float,
) -> tuple[bool, str]:
    """★ 交叉校验(V75):视觉价与 DOM 价偏差 ``> tolerance`` → **标"待人工确认"**。

    逐字继承旧 ``prices.py:248-282`` 的立场:偏差过大时**不静默采用任何一个**,
    而是让报告显式标注。

    返回 ``(need_manual_check, 说明)``。

    四种情形:

    ==========================  ==========  ================================================
    DOM / 视觉                    结果        说明
    ==========================  ==========  ================================================
    都有且偏差 ≤ tolerance        not needed  两者互证
    都有且偏差 > tolerance        **needed**  写清两边各是多少、偏差多少
    只有一边                      not needed  说明"单边,无对照"(不是异常)
    都没有                        not needed  说明"都没取到"
    ==========================  ==========  ================================================
    """
    if dom_price is None and vision_price is None:
        return False, "DOM 与视觉均未取到价格"
    if dom_price is None:
        return False, f"仅视觉取到 ¥{vision_price:g}(无 DOM 价对照)"
    if vision_price is None:
        return False, f"仅 DOM 取到 ¥{dom_price:g}(视觉未取到,未启用或失败)"

    base = max(abs(dom_price), abs(vision_price))
    if base <= 0:
        return False, "价格均为 0,跳过校验"
    diff = abs(dom_price - vision_price) / base
    if diff > tolerance:
        return True, (
            f"视觉价 ¥{vision_price:g} 与 DOM 价 ¥{dom_price:g} 偏差 {diff * 100:.1f}% "
            f"> 阈值 {tolerance * 100:.0f}%,**不静默采用**,待人确认"
        )
    return False, f"视觉 ¥{vision_price:g} 与 DOM ¥{dom_price:g} 偏差 {diff * 100:.1f}%,互证通过"
