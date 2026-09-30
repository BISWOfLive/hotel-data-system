"""★ 平台契约(段3 最重要的新设计 —— 把旧系统的"假插件"做成真的)。

旧系统是什么样(总纲 3.4 / 段3 §4.3)
====================================

``comparator/platforms/base.py:16`` 定义了类属性 + 模板方法 ``collect()``
串起 ``_search`` / ``_extract_anchor`` / ``_collect_candidates`` / ``_detail_url``
—— 而**四个钩子全是 ``NotImplementedError``**,``ctrip.py`` **一个都没实现**
→ **基类模板方法已是死代码**。

真实契约是一个**未声明的方法** ``collect_map_prices()``,靠 ``hasattr`` 探测
(``runner.py:264``);平台注册表**硬编码**(新增平台要改 runner);
返回**裸 dict 无 schema**(消费方靠约定字段名,改一处炸一片);
异常被吞成 ``result["error"]``(**失败不可见**)。

段3 要做成真的
==============

======================  ============================================================
旧系统                  段3
======================  ============================================================
``hasattr`` 探测        显式 :class:`PricePlatform` 协议
返回裸 dict             :class:`HotelQuote` / :class:`CompareResult`(Pydantic)
异常吞成 ``dict``       **三态异常** + 明确传播(见下)
注册表硬编码            ``@register`` 声明式(``registry.py``)
4 个死模板钩子          3 个真方法
======================  ============================================================

★ 三态异常(不是两态 —— 计划书 §5.3 写的两态,实测需要三态)
=========================================================

计划书 §5.3 只分了「可重试 / 不可重试」两态。但旧系统 ``errors.py:9-10``
里 :class:`HumanVerificationError` **继承自** ``PlatformError`` ——
它既不该被当"网络抖动"自动重试(**会加重风控**),也不该被当"页面结构变了"直接告警了事
(**它需要人来拖滑块**)。所以段3 分三态:

============================  ==========  ==========================================
异常                           自动重试     语义
============================  ==========  ==========================================
``PriceRetryableError``       ✅ 是        超时 / 网络抖动 / 骨架页未就绪
``PriceFatalError``           ❌ 否        页面结构变了 / 锚点找不到 → 告警
``HumanVerificationError``    ❌ **绝不**   验证码 / 风控 → **转人工,不硬闯**
============================  ==========  ==========================================

:class:`HumanVerificationError` 仍是 :class:`PriceFatalError` 的子类
(对"要不要重试"这个问题的答案是"不要"),但**类型可辨认** ——
调用方能把它单独拎出来提示"请人工登录",而不是混在"页面改版"的告警里。

★ ``price_scope``:起价 vs 确定价(计划书没有,实测必须有)
=======================================================

旧系统 18 条归档里 **12 条把优惠券当成了房价**(「折扣券 ¥34」「十亿豪补 ¥12」),
另有多次把列表页的「**起价**」当成确定的房价报出去。

:data:`PriceScope` 把这两件事**显式标注**,不许含糊:

* ``"from"``  —— 列表页「¥236起」,这是**区间下界**,不是这家店今晚的价;
* ``"exact"`` —— 详情页/房型页的确定报价。

报告、日报段、推送文案里都要体现这个区别。

★ ``price_rejected``:丢弃要可见
==============================

:attr:`HotelQuote.price_rejected` 记录**被券价过滤器丢掉的候选原文**。
旧系统把「折扣券 ¥34」当房价是**错误**,但把丢弃做得**无声无息**是**另一个错误** ——
出了问题时无法判断"是真没价"还是"过滤太狠"。

★ 为什么坐标提取是 ``coords`` 而不是旧系统的扁平列表
==================================================

旧 ``geo.deep_find_coords`` 返回 ``list[(lat, lng)]`` ——
**无法与酒店逐条对齐**(它把整棵树里所有坐标对拍平成一个列表)。
这是旧系统"抓到了坐标却接不上线"的技术原因。段3 把坐标**挂在每家酒店上**。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ANCHOR_SOURCES",
    "COORD_SOURCES",
    "PLATFORMS",
    "PRICE_SCOPES",
    "PRICE_SOURCES",
    "AnchorRef",
    "AnchorSource",
    "CompareResult",
    "CoordSource",
    "HumanVerificationError",
    "HotelQuote",
    "PlatformError",
    "PriceFatalError",
    "PricePlatform",
    "PriceRetryableError",
    "PriceScope",
    "PriceSource",
    "Platform",
    "QuotesPage",
]

# ---------------------------------------------------------------------------
# 字面量
# ---------------------------------------------------------------------------

Platform = Literal["ctrip", "meituan"]
PLATFORMS: tuple[Platform, ...] = ("ctrip", "meituan")

#: 价格来源。★ 旧系统的 ``price_source`` 只有 ``"DOM"/""`` 两个值,
#: 且**接口价与券价都被标成 DOM**(语义失真)。段3 让它说真话。
PriceSource = Literal["api", "dom", "vision", "manual"]

#: ``"from"`` = 列表页「¥起」;``"exact"`` = 确定报价。见模块文档。
PriceScope = Literal["from", "exact"]

#: 坐标从哪来 —— ★ 决定 :attr:`HotelQuote.degraded` 是否置位。
#:
#: * ``"api"``      —— 接口字段(携程 ``position.lat/lng``),**最可信**;
#: * ``"card"``     —— 卡片文本(美团「距您查询的酒店直线1.2公里」解析),**次可信**;
#: * ``"city"``     —— 城市中心兜底,**精度降级 → 必须标 degraded**;
#: * ``"none"``     —— 拿不到,**排序退化为平台顺序,报告要显式标注「距离不可用」**。
CoordSource = Literal["api", "card", "city", "none"]

#: 锚点是怎么定位到的(诊断用)。
AnchorSource = Literal["ebk_hotel_id", "city_sitemap", "name_match", "demo"]

ANCHOR_SOURCES: tuple[AnchorSource, ...] = (
    "ebk_hotel_id",
    "city_sitemap",
    "name_match",
    "demo",
)
COORD_SOURCES: tuple[CoordSource, ...] = ("api", "card", "city", "none")
PRICE_SOURCES: tuple[PriceSource, ...] = ("api", "dom", "vision", "manual")
PRICE_SCOPES: tuple[PriceScope, ...] = ("from", "exact")


# ---------------------------------------------------------------------------
# ★ 三态异常
# ---------------------------------------------------------------------------


class PlatformError(Exception):
    """比价域异常基类。**保留旧系统的名字与继承关系**(``errors.py:5``)。"""


class PriceRetryableError(PlatformError):
    """**可重试**:超时 / 网络抖动 / 页面还没就绪(骨架页)。

    对应旧 ``PlatformError``(``errors.py:5-6``,注释写"可恢复,不拖垮其他平台")。
    """


class PriceFatalError(PlatformError):
    """**不可重试**:页面结构变了 / 锚点找不到 / 登录态失效。

    重试没有意义,只会白烧风控额度 —— 直接告警。
    """


class HumanVerificationError(PriceFatalError):
    """**遇到验证码 / 风控 —— 必须真人处理,代码绝不硬闯**。

    ★ 继承 :class:`PriceFatalError`(对"要不要自动重试"的回答是"不要"),
    但**类型可辨认** —— 调用方据此提示"请人工登录"而不是报"页面改版"。

    逐字继承旧 ``errors.py:9-10`` 与 ``human.py:198-204`` 的立场:
    「遇到滑块/验证码 → 抛 HumanVerificationError,交给真人处理,代码不破解」。
    """


# ---------------------------------------------------------------------------
# 类型化模型(★ 禁止裸 dict)
# ---------------------------------------------------------------------------


class _Model(BaseModel):
    """域内模型基类:禁止未声明字段(裸 dict 塞进来的东西会被拒)。"""

    model_config = ConfigDict(extra="forbid", frozen=False)


class AnchorRef(_Model):
    """锚点酒店(比价的中心点)。"""

    name: str
    #: 携程 ``ebk_hotel_id`` / 美团平台内部 id
    hotel_id: str | None = None
    city: str | None = None
    #: 平台城市 id(携程 cityId / 美团 cityId),解析出来才填
    city_id: str | None = None
    #: ``(lat, lng)``;拿不到为 ``None``
    coords: tuple[float, float] | None = None
    coord_source: CoordSource = "none"
    #: 怎么定位到的(诊断用)
    source: AnchorSource = "name_match"
    #: 本店的报价(锚点自己也常常在"附近酒店"列表里)—— 报告里的「本店价」
    self_price: float | None = None
    self_price_platform: str | None = None
    raw: dict[str, Any] | None = None

    @property
    def has_coords(self) -> bool:
        return self.coords is not None


class HotelQuote(_Model):
    """一家酒店的报价。**★ 类型化,禁止裸 dict。**"""

    hotel_name: str
    price: float | None = None
    #: ★ 起价 / 确定价(旧系统无此概念,导致「¥236起」被当成确定的房价)
    price_scope: PriceScope = "from"
    room_type: str | None = None
    #: ★ 旧系统**恒为 None** 的字段(D13);段3 必须真填
    distance_km: float | None = None
    coords: tuple[float, float] | None = None
    coord_source: CoordSource = "none"
    #: ★ 旧系统只有 "DOM"/"";接口价与券价都被标成 DOM
    price_source: PriceSource = "dom"
    #: 视觉价与 DOM 价偏差 >20% 时置 True(不静默采用)
    need_manual_check: bool = False
    #: 走过兜底通道(城市中心坐标 / 视觉 / 平台顺序)时置 True,报告要标注
    degraded: bool = False
    #: ★ 被**券价过滤器**丢掉的候选原文 —— 丢弃必须可见
    price_rejected: list[str] = Field(default_factory=list)
    #: 详情页 URL(携程 ``seoInfo.seoUrl``)
    url: str | None = None
    #: 平台内部酒店 id
    hotel_id: str | None = None
    #: 评分 / 点评数(接口附带,报告可用)
    score: float | None = None
    reviews: int | None = None
    raw: dict[str, Any] | None = None

    @property
    def has_price(self) -> bool:
        return self.price is not None

    @property
    def has_distance(self) -> bool:
        return self.distance_km is not None


class QuotesPage(_Model):
    """一次「进页面 → 提候选 → 取价 → 算距离」的产物(平台返回,runner 消费)。"""

    quotes: list[HotelQuote] = Field(default_factory=list)
    #: 平台是否降级(走了 DOM 兜底 / 缺登录态 / 用城市中心坐标)
    degraded: bool = False
    #: 给报告与诊断看的说明(不是错误;真失败走异常)
    notes: list[str] = Field(default_factory=list)
    #: ★ 本次落盘的**原始响应体**路径(相对项目根)。
    #: 旧系统的诊断只存了 4000 字预览,考古时无法判断完整响应里有没有价格字段
    #: (见 `docs/参考/段3-分析/旧系统-携程平台基线.md`)—— 段3 存全量。
    raw_json_path: str | None = None
    #: 实际用的取价通道(``api`` / ``dom`` / ``vision``),报告与审计要标
    price_source: PriceSource = "dom"


class CompareResult(_Model):
    """一个平台一次的完整比价结果。"""

    platform: Platform
    anchor: AnchorRef
    quotes: list[HotelQuote] = Field(default_factory=list)
    #: ``YYYY-MM-DD-HHMM`` —— 一天多次采集各留一份的依据
    query_slot: str = ""
    query_date: date | None = None
    nights: int = 1
    #: 走过兜底通道(视觉 / 城市中心坐标 / 平台顺序)
    degraded: bool = False
    #: ★ 失败**不吞进这里就不算失败** —— 这里只放"部分降级的原因",
    #: 真失败一律走异常传播(见模块文档的三态异常)
    notes: list[str] = Field(default_factory=list)

    @property
    def priced(self) -> list[HotelQuote]:
        """有有效报价的条目。"""
        return [q for q in self.quotes if q.has_price]

    @property
    def distances_available(self) -> bool:
        return any(q.has_distance for q in self.quotes)


# ---------------------------------------------------------------------------
# ★ 平台协议(声明式 —— 不用 hasattr 探测)
# ---------------------------------------------------------------------------


@runtime_checkable
class PricePlatform(Protocol):
    """平台插件契约。**声明式,不用 ``hasattr`` 探测。**

    两个方法(*不是*计划书 §5.3 写的三个 —— 见下方"为什么去掉一个"):

    ==========================  ==========================================
    方法                         旧系统对应物
    ==========================  ==========================================
    :meth:`resolve_anchor`      ``_hotel_id_from_db`` / 城市 sitemap / 首卡
    :meth:`collect_quotes`      列表页一次提卡 **+ 取价**(同一次页面访问)
    ==========================  ==========================================

    ★ 为什么没有单独的 ``collect_nearby``
    =====================================

    计划书 §5.3 把「提候选」与「取价」拆成两个方法。实测这会**让每家店多开一次页面**:
    ``collect_nearby`` 提完卡片返回名字,``collect_quotes`` 再按名字取价 ——
    但两个平台的价格**本来就和卡片在同一个 DOM 节点里**
    (美团 ``em.poi-price-num`` 就在 ``a.poi`` 卡片内;携程接口的 ``hotelList[]``
    同时有名字和价格)。

    拆开等于:开一次页面拿名字 → 返回 → **再开一次同样的页面**拿价格。
    在**风控敏感**的前台(总纲 R2:T2 风险)这是白烧配额,而段3 §2.3 的性能目标
    是「单店双平台 ≤ 90 秒」。

    所以段3 合成一个方法:**一次页面访问里既提候选又取价**。
    这**不违反** §5 附录 B 的业务事实「一次 DOM 批量提卡,不逐家开详情页」——
    它正是那条事实的严格实现。

    :meth:`resolve_anchor` 之所以保留独立,是因为**已验证酒店可以零页面访问**
    (携程 ``ebk_hotel_id`` 直达,旧 ``ctrip.py:296`` 优先走这条路),
    把它并进取价会强迫所有已登记酒店多开一次页面。
    """

    name: Platform
    home_url: str

    async def resolve_anchor(
        self,
        ctx: Any,
        name: str,
        city: str | None = None,
        *,
        hotel_id: str | None = None,
    ) -> AnchorRef:
        """定位锚点。

        * ``hotel_id``(携程 ``ebk_hotel_id``)给了 → **直达,免名称匹配、免页面访问**;
        * 没给 → 走城市 sitemap + 列表页取首个 hotelId / 按名称前 4 字认卡。

        找不到 → 抛 :class:`PriceFatalError`(**不返回空结果**)。
        """
        ...

    async def collect_quotes(
        self,
        ctx: Any,
        anchor: AnchorRef,
        count: int,
        *,
        nights: int = 1,
        rooms: int = 1,
    ) -> QuotesPage:
        """**一次页面访问**完成「进页面 → 等就绪 → 批量提卡片 → 取价 → 算距离」。

        * ``count`` —— 取几家(**调用方传 ``nearby_count * 2``**,
          旧 ``runner.py:162`` 的"取 2N 再合并截 N":多取一倍以应对去重损耗);
        * ``rooms`` —— 每个平台保留几条报价(``HOTEL_QUOTE_COUNT``);
        * ``nights`` —— 住几晚(进 URL 的 ``checkin``/``checkout``)。

        ★ 列表页只有「起价」→ 返回的报价 ``price_scope="from"``。
        ★ 骨架页(未渲染)不是"没有附近酒店",**必须抛
          :class:`PriceRetryableError`**(旧系统在骨架页上继续跑启发式,
          于是把优惠券当成了房价 —— P1)。
        """
        ...
