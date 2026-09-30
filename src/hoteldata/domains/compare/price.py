"""★ 取价语义(段3 P1 的核心修复 —— 计划书 §5.6「取价通道」的落地)。

旧系统为什么在这里崩的(实测)
============================

旧 ``prices.py:24-51`` 的 :func:`parse_price` 是一段**启发式**:

* 正则扫出文本里所有"像价格"的数字;
* 限 ``5 ~ 100000``;
* 无货币符号时,还要求"整段文本除了这个数字什么都没有";
* **取最小值**。

问题不在正则,在于**它的输入没有经过任何"这是不是房价"的过滤** ——
于是页面上任何带 ¥ 的数字都能成为"房价"。实测结果(**18 条归档**):

* **12 条把优惠券当成了房价** —— 「折扣券 ¥34」「十亿豪补 ¥12」;
* 而这些脏候选又**挡住了兜底**:旧 ``ctrip.py:343`` 的兜底条件是
  ``if not hotels or all(price is None)``,券价让它恒为假
  → **「显示地图」兜底整段跳过**(12 条归档全中)。

段3 的三条修正
==============

1. **券价过滤器**(:func:`classify_price_text`):命中券/红包/立减/补贴等词 → **拒绝**,
   并把原文记进 :attr:`HotelQuote.price_rejected` —— **丢弃必须可见**;
2. **口径标注**(:data:`PriceScope`):列表页的「¥236起」是 :data:`~.contract.PriceScope`
   的 ``"from"``,**不是**确定的房价;
3. **兜底条件改用"有没有可用报价"**,不再被脏候选挡住。

★ 为什么"取最小值"要保留
========================

`sorted()[0]` 在**同一张卡片内**是对的(卡片上「会员价 ¥236 / 原价 ¥288」取 236 合理)。
它错的是**跨卡片**被当成了"这家店的价格" —— 那是调用方的责任,不是这个函数的。
所以 :func:`parse_price` 的取最小值语义**逐字继承**,过滤逻辑放在它外面。
"""

from __future__ import annotations

import re
from typing import Any, NamedTuple

from loguru import logger

from hoteldata.domains.compare.contract import PriceScope

__all__ = [
    "COUPON_KEYWORDS",
    "PriceVerdict",
    "classify_dom_price",
    "classify_price_text",
    "is_coupon_text",
    "parse_price",
    "parse_price_value",
    "quotes_from_verdicts",
]

#: ★ 券价特征词 —— 命中即**拒绝**该候选。
#:
#: 旧系统实测的假阳性原文:「折扣券 ¥34」「十亿豪补 ¥12」。
#: 这份表是段3 相对旧系统的**核心新增**,宁可漏杀不可错杀的是"房价",
#: 但把券当房价的代价更高(整份报告的第一行就是错的价)。
COUPON_KEYWORDS: tuple[str, ...] = (
    "券",
    "红包",
    "立减",
    "补贴",
    "豪补",
    "返现",
    "返券",
    "优惠码",
    "代金券",
    "满减",
    "立省",
    "神券",
    "礼包",
    "抵扣",
)

#: 价格正则(**逐字继承**旧 ``prices.py:21``)。
#: 支持千分位 ``1,288`` 与小数 ``0.5``;货币符号可选。
_PRICE_RE = re.compile(
    r"(?:(¥|￥|CNY)\s*)?"
    r"((?:[1-9]\d{0,2}(?:,\d{3})+|[1-9]\d{0,5})(?:\.\d{1,2})?|0\.\d{1,2})"
)

#: 无货币符号时,允许出现在"纯价格文本"里的字符(旧 ``prices.py:45``,逐字)
_PLAIN_OK_RE = re.compile(r"[¥￥CNY\s,，。.!！·起元晚/晚间天位人价(（)）]")


def parse_price(text: str, require_symbol: bool = False) -> float | None:
    """从一段文本里解析价格(**取最小的非零数字**)。

    ★ **逐字继承**旧 ``prices.py:24-51`` 的语义,包括三个容易写错的地方:

    * 范围 ``5 <= v <= 100000``(低于 5 元的不是房价,高于 10 万的不是房价);
    * ``require_symbol=False`` 时,**无货币符号的数字必须"整段文本除了它就没别的"**
      才接受 —— 防的是把年份 ``2026`` / 房间号当价格;
    * 无符号且是 ``1000~2999`` 的整数 → 排除(那个区间几乎全是年份/编号)。

    本函数**不做券价过滤**(那是 :func:`classify_price_text` 的职责)。
    """
    if not text:
        return None
    nums: list[float] = []
    for m in _PRICE_RE.finditer(text):
        symbol = m.group(1)
        raw = m.group(2).replace(",", "")
        try:
            v = float(raw)
        except ValueError:
            continue
        if not (5 <= v <= 100000):
            continue
        if require_symbol and not symbol:
            continue
        if not symbol:
            if _PLAIN_OK_RE.sub("", text) != raw:
                continue
            if 1000 <= v <= 2999 and v == int(v):
                continue
        nums.append(v)
    return min(nums) if nums else None


def parse_price_value(text: str | None) -> float | None:
    """便捷版:接受 ``None`` 输入(卡片字段常缺)。"""
    return parse_price(text or "")


# ---------------------------------------------------------------------------
# ★ 券价过滤 + 口径判定
# ---------------------------------------------------------------------------


class PriceVerdict(NamedTuple):
    """一次取价判定的结果。"""

    price: float | None
    scope: PriceScope
    #: 被拒绝的原因(空串 = 没拒绝)
    rejected: str
    #: 原始文本(诊断用)
    raw: str

    @property
    def ok(self) -> bool:
        return self.price is not None


def is_coupon_text(text: str | None) -> str | None:
    """命中券价特征词时返回**命中的词**,否则 ``None``。

    返回命中词(而不是 bool)是为了让 :attr:`HotelQuote.price_rejected`
    能写出「拒绝原因」,而不是只丢一个 ``True``。
    """
    if not text:
        return None
    for kw in COUPON_KEYWORDS:
        if kw in text:
            return kw
    return None


def classify_price_text(
    text: str | None,
    *,
    require_symbol: bool = False,
    scope: PriceScope = "from",
) -> PriceVerdict:
    """把一段卡片文本判定成一个 :class:`PriceVerdict`(**段3 取价的唯一入口**)。

    判定顺序(顺序本身是语义的一部分):

    1. 空文本 → ``price=None``,``rejected=""``(**不是拒绝,是没价**);
    2. **券价特征词** → ``price=None``,``rejected="券价特征:<词>"``
       → 调用方把 ``raw`` 记进 ``price_rejected``;
    3. 解析出价格 → ``price=v``,``scope`` 原样透传;
    4. 解析不出 → ``price=None``,``rejected=""``(**页面确实没价**)。

    ★ 第 2 步与第 4 步的**区别必须保留**:一个是"我们主动不要",一个是"确实没有"。
      旧系统把两者都变成了"没有价格",于是无法判断过滤是否过狠。
    """
    raw = (text or "").strip()
    if not raw:
        return PriceVerdict(None, scope, "", "")

    hit = is_coupon_text(raw)
    if hit is not None:
        logger.debug("券价候选已拒绝(命中 {!r}):{}", hit, raw[:80])
        return PriceVerdict(None, scope, f"券价特征:{hit}", raw)

    value = parse_price(raw, require_symbol=require_symbol)
    return PriceVerdict(value, scope, "", raw)


def classify_dom_price(
    price_text: str | None,
    *,
    whole_card_text: str | None = None,
) -> PriceVerdict:
    """DOM 取价的判定:**价格节点本身干净 → 采信;否则看整卡**。

    ★ 为什么不能一律用整卡文本判定:酒店名里带「券」字的店
      (如「如家·优惠券主题店」)会被**误杀**。
      所以只有**价格节点自己没给出可信价格**时,才退到整卡视角找券价证据 ——
      这样才能既挡住「已减 ¥15」,又不误杀正常酒店。

    返回的 ``rejected`` 在"整卡命中券、价格节点也没解析出价"时才会非空。
    """
    verdict = classify_price_text(price_text)
    if verdict.ok:
        return verdict
    if whole_card_text:
        hit = is_coupon_text(whole_card_text)
        if hit is not None:
            return PriceVerdict(None, verdict.scope, f"券价特征:{hit}(整卡)", whole_card_text.strip())
    return verdict


def quotes_from_verdicts(
    rows: list[tuple[str, PriceVerdict, dict[str, Any]]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """把 ``[(酒店名, 判定, 附加字段)]`` 拆成 ``(可用报价, 被拒原文)``。

    便捷助手,供平台实现共用,避免每个平台各写一遍"拒绝也要记下来"的逻辑。
    """
    ok: list[dict[str, Any]] = []
    rejected: list[str] = []
    for name, verdict, extra in rows:
        if verdict.ok:
            ok.append({"hotel_name": name, "price": verdict.price, "price_scope": verdict.scope, **extra})
        elif verdict.rejected:
            rejected.append(f"{name}: {verdict.raw[:120]}")
    return ok, rejected
