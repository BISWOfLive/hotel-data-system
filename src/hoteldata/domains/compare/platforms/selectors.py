"""比价平台的**实测常量集中处**(A17 级遗产 —— 逐字继承,禁止散落)。

为什么要单独一个模块
====================

段3 风险 T1 是「**前台页面改版(选择器失效)**」。旧系统把选择器散落在
``ctrip.py`` / ``meituan.py`` 各处,改版时要在上千行里找 —— 而且**没有一处能一眼看全**。

段3 把**全部**选择器常量收在这里,好处有三:

1. 改版时**只改一个文件**;
2. ``price probe`` 诊断命令可以直接遍历本模块导出的常量逐条试(``PROBE_TARGETS``);
3. 这些是**实测值**(用户提供真实 DOM),不是猜的 —— 注释里标了出处行号。

★ 常量来源(旧系统,均已实测)
============================

携程(``comparator/platforms/ctrip.py``):

* ``:154-159`` ``NEARBY_TAB_SELECTORS`` —— 「酒店」页签(地图/附近面板)
* ``:161-169`` ``MAP_BUTTON_SELECTORS`` —— 「显示地图」按钮
* ``:171-174`` ``NEARBY_CARD_SELECTORS`` —— 附近酒店卡片容器
* ``:79``      ``_CAPTURE_KEYS`` —— 只读抓包的 URL 关键词表
* ``:26``      ``CITY_SITEMAP_URL`` —— 城市 sitemap
* ``:27-28``   ``LIST_PAGE_URL`` / ``DETAIL_PAGE_URL``

美团(``comparator/platforms/meituan.py``):

* ``:405-412`` 卡片 7 件套(在 ``page.evaluate`` 的 JS 里,原文照抄)
* ``:58``      城市接口
* ``:356-363`` ``list.html`` 直达 URL
* ``:140``     登录 URL
* ``:535-546`` 锚点「名称前 4 字」匹配

★ 为什么美团那 7 个选择器要抄成常量而不是继续内嵌 JS
==================================================

旧系统把它们**内嵌在一段 ``page.evaluate`` 的 JS 字符串里**,并顺手用了
``out.slice(0, %d) % limit`` 这种**字符串插值** —— 一旦有人改动 JS 就会破坏格式。
段3 把选择器提出来,JS 由 :func:`meituan_card_script` 生成,选择器只有一个来源。
"""

from __future__ import annotations

__all__ = [
    "CTRIP_CAPTURE_KEYS",
    "CTRIP_CITY_SITEMAP_URL",
    "CTRIP_DETAIL_PAGE_URL",
    "CTRIP_LIST_PAGE_URL",
    "CTRIP_MAP_BUTTON_SELECTORS",
    "CTRIP_NEARBY_CARD_SELECTORS",
    "CTRIP_NEARBY_TAB_SELECTORS",
    "CTRIP_NEARBY_API_KEY",
    "MEITUAN_CITY_API_PATH",
    "MEITUAN_CITY_LIST_URL",
    "MEITUAN_DEFAULT_CITY_ID",
    "MEITUAN_H5_URL",
    "MEITUAN_LIST_URL",
    "MEITUAN_LOGIN_URL",
    "MEITUAN_POI_SELECTORS",
    "PROBE_TARGETS",
    "meituan_card_script",
]

# ===========================================================================
# 携程(前台)
# ===========================================================================

#: 城市 sitemap(旧 ``ctrip.py:26``)
CTRIP_CITY_SITEMAP_URL = "https://m.ctrip.com/webapp/hotel/sitemap/citylist/"
#: 列表页(旧 ``ctrip.py:27``)
CTRIP_LIST_PAGE_URL = "https://hotels.ctrip.com/hotels/list"
#: 详情页(旧 ``ctrip.py:28``)
CTRIP_DETAIL_PAGE_URL = "https://hotels.ctrip.com/hotels/detail/"
#: 首页(协议 ``home_url`` 用)
CTRIP_HOME_URL = "https://hotels.ctrip.com/"

#: ★ 附近酒店**真接口**(旧系统抓到了却没读懂 —— 段3 最重要的接口遗产)
#:
#: 证据:``旧系统/data/hotel_reports/diag_ctrip_api_no_hotels_20260826_172538.json``
#: 响应形状::
#:
#:     data.hotelList[].base.hotelId / hotelName
#:                        .comment.score          "4.7"(字符串)
#:                        .comment.totalReviews   "267 点评"
#:                        .position.lat / lng     "37.165587"(字符串)
#:                        .position.positionDescOfCtrip  "距酒店直线1公里 · 近统战文化广场"
#:                        .seoInfo.seoUrl         详情页 URL
CTRIP_NEARBY_API_KEY = "ctGetNearbyHotelList"

#: 只读抓包的 URL 关键词表(旧 ``ctrip.py:79``,**逐字**)
#:
#: ★ 旧系统只用它做"猜接口"的启发式;段3 改用它做**兜底发现**
#:   (主路径直接认 :data:`CTRIP_NEARBY_API_KEY`)。
CTRIP_CAPTURE_KEYS: tuple[str, ...] = ("near", "map", "recommend", "around", "poi")

#: 「酒店」页签(旧 ``ctrip.py:154-159``,原文照抄)
CTRIP_NEARBY_TAB_SELECTORS: tuple[str, ...] = (
    "div[class*='nearby-tabs_tabWrap']:has-text('酒店')",
    "div[class*='nearby-tabs_tabTextWrap']:has-text('酒店')",
    "div[class*='nearby-tabs']:has-text('酒店')",
    "span:text-is('酒店')",
)

#: 「显示地图」按钮(旧 ``ctrip.py:161-169``,原文照抄)
CTRIP_MAP_BUTTON_SELECTORS: tuple[str, ...] = (
    "div[class*='style_textLinkButton']:has-text('显示地图')",
    "div[class*='address_showmore']:has-text('显示地图')",
    "div[tabindex='0']:has-text('显示地图')",
    "span:text-is('显示地图')",
    "text=显示地图",
    "button:has-text('显示地图')",
    "a:has-text('显示地图')",
)

#: 附近酒店卡片容器(旧 ``ctrip.py:171-174``,原文照抄)
#:
#: 🔴 **实测已失效**(段3 复核,2026-10-01):携程前台改版后
#: ``recommendCard*`` 类名 **0 命中**。仅作历史留档,**不要再用**。
CTRIP_NEARBY_CARD_SELECTORS_LEGACY: tuple[str, ...] = (
    "div[class*='recommendCard_cardWrap']",
    "div[class*='recommendCard']",
)

#: ★ **新版附近酒店卡片**(实测 20 个,与 ``ctGetNearbyHotelList`` 返回的 20 家一一对应)
#:
#: 每个卡片的 ``innerText`` 形如::
#:
#:     4.7 51点评 寂帆酒店(青城山站店) 距酒店直线930米 · 近青城山站 ¥359 ¥255 起
#:
#: 一次拿到 **评分 / 点评数 / 名称 / 距离文本 / 原价 / 现价** ——
#: 这是段3 在「接口只给坐标、不给价」之后**取价的唯一通道**。
CTRIP_NEARBY_CARD_SELECTORS: tuple[str, ...] = (
    "div[class*='nearbyHotelCard_nearbyHotelCard']",
    "div[class*='nearbyHotelCard']",
)

#: 卡片内子选择器(旧 ``ctrip.py:455/460/470/480/485``,按顺序:名称/价格/评分/距离/链接)
CTRIP_CARD_INNER: dict[str, str] = {
    "name": "div[class*='recommendCard_hotelName'], [class*='hotelName']",
    "price": "[class*='recommendCard_price'], [class*='price']",
    "score": "[class*='recommendCard_score'], [class*='score']",
    "distance": "[class*='recommendCard_distance'], [class*='distance']",
    "link": "a[href*='hotelId'], a[href*='hotel-detail']",
}

#: 列表页首个 ``a[href*='hotelId']`` → 锚点 hotelId(旧 ``_anchor_hotel_id``,``ctrip.py:64-75``)
CTRIP_ANCHOR_LINK_SELECTOR = "a[href*='hotelId']"


# ===========================================================================
# 美团(H5 列表页)
# ===========================================================================

#: 美团 H5 酒店搜索页(旧 ``config.py:96-99``,原文)
MEITUAN_H5_URL = "https://i.meituan.com/awp/h5/hotel/search/search.html"

#: ★ 列表页直达(旧 ``meituan.py:356-363`` 的 ``list.html`` **已 404**)
#:
#: 实测(2026-10-01):旧地址 ``/awp/h5/hotel/list.html`` 返回 CDN 的
#: ``<Code>NoSuchKey</Code> ... Object [h5/hotel/list.html] not exist``。
#: 真实地址**多一层** ``/list/``:``/awp/h5/hotel/list/list.html``
#: —— 该路径在搜索页产生的网络请求里可以旁听到,且能渲染出 30 张卡片。
MEITUAN_LIST_URL = "https://i.meituan.com/awp/h5/hotel/list/list.html"

#: 「查找酒店 / 搜索」按钮(实测 ``text=查找酒店`` 可点,点后结果才渲染)
#:
#: ★ 为什么必须点它:直接打开 ``list/list.html?...`` **也能加载**,但结果区是空的
#:   —— 实测 30 张卡片只在**执行一次搜索**之后才出现。旧系统把 `keyword` 塞进 URL
#:   就当搜索完成了,在当时的版本上恰好可行;现在的版本不行。
MEITUAN_SEARCH_BUTTON_SELECTORS: tuple[str, ...] = (
    "text=查找酒店",
    "button:has-text('搜索')",
    "text=搜索",
    "[class*='search-btn']",
    "[class*='searchBtn']",
)
#: 登录页(旧 ``meituan.py:140``;``backurl`` 由调用方拼)
MEITUAN_LOGIN_URL = "https://passport.meituan.com/useraccount/ilogin"
#: ★ 缺省城市 id(旧 ``meituan.py:358/378`` 硬编码 ``"59"``)
MEITUAN_DEFAULT_CITY_ID = "59"

#: ★ 城市列表接口(旧 ``meituan.py:58``,**实测**;返回 ``{"data":[{id,name,pinyin,isOpen}]}``)
#:
#: ★ **必须在页面内 fetch**(``credentials: 'include'``):该接口要带美团域 cookie。
#: 用 HTTP 直连大概率被拒,所以取城市走 ``page.evaluate`` 里的 fetch。
MEITUAN_CITY_LIST_URL = "https://i.meituan.com/awp/hfe/fep/1a368b1d945fd175cc40c859d172350d.json"

#: 城市接口路径(旧常量,保留给文档引用)
MEITUAN_CITY_API_PATH = "/1a368b1d945fd175cc40c859d172350d.json"

#: ★ 卡片 7 件套(旧 ``meituan.py:405-412`` 的 ``querySelector`` 原文,**逐字**)
#:
#: ==========  ==================================  ==========================================
#: 键           选择器                               说明
#: ==========  ==================================  ==========================================
#: name        ``h1.poi-title, .poi-title``         酒店名
#: score       ``.poi-score``                       评分(「4.5分」)
#: feedback    ``.poi-feedback``                    点评数(「500+消费」)
#: address     ``.poi-address``                     ★ **距离文本**(「距您查询的酒店直线1.2公里」)
#: price_num   ``em.poi-price-num, .poi-price-num`` ★ 现价(**起价**)
#: origin      ``.poi-origin``                      ★ 门市价(「门市价¥138」)
#: raw         整个卡片 ``innerText``                兜底与诊断
#: ==========  ==================================  ==========================================
MEITUAN_POI_SELECTORS: dict[str, str] = {
    "name": "h1.poi-title, .poi-title",
    "score": ".poi-score",
    "feedback": ".poi-feedback",
    # ★ 键名是 address,但**语义是距离** —— 旧 ``meituan.py:426-434`` 把它映射成
    #   ``entry["distance"]`` 是对的,错在后面又把它丢掉了(段3 P1)。
    "address": ".poi-address",
    "price_num": "em.poi-price-num, .poi-price-num",
    "origin": ".poi-origin",
}

#: 卡片容器(旧 ``meituan.py:405``,原文)
MEITUAN_POI_CARD_SELECTOR = 'a.poi, a[class*="poi"]'


def meituan_card_script(limit: int) -> str:
    """生成美团「单次 ``evaluate`` 批量读卡片」的 JS(**一次读取,不逐卡点击**)。

    ★ 与旧 ``meituan.py:398-418`` 的差别只有一处:**选择器不再靠字符串插值**
    (旧写法 ``out.slice(0, %d) % limit`` 一旦被改动就会破坏 JS 格式),
    这里用 :data:`MEITUAN_POI_SELECTORS` 拼装,选择器只有一个来源。

    ``limit`` 用 ``Number.isFinite`` 守卫后内联为整数,不拼接用户输入。
    """
    n = int(limit)
    sels = MEITUAN_POI_SELECTORS
    return f"""() => {{
        const out = [];
        const q = (root, sel) => {{
            const el = root.querySelector(sel);
            return el ? (el.innerText || '').trim() : '';
        }};
        for (const a of document.querySelectorAll({MEITUAN_POI_CARD_SELECTOR!r})) {{
            out.push({{
                name: q(a, {sels["name"]!r}),
                score: q(a, {sels["score"]!r}),
                feedback: q(a, {sels["feedback"]!r}),
                address: q(a, {sels["address"]!r}),
                priceNum: q(a, {sels["price_num"]!r}),
                origin: q(a, {sels["origin"]!r}),
                raw: (a.innerText || '').trim()
            }});
        }}
        return out.slice(0, {n});
    }}"""


# ===========================================================================
# 诊断(``hoteldata price probe``)
# ===========================================================================

#: ``price probe`` 要逐条试的选择器(平台 → [(用途, 选择器)])
#:
#: ★ 这张表是**给改版用的**:平台改版时 ``price probe`` 会逐条报"命中几张"，
#:   一眼看出是哪一条失效了(而不是"取不到价"这种没有信息量的结论)。
PROBE_TARGETS: dict[str, tuple[tuple[str, str], ...]] = {
    "ctrip": (
        ("附近卡片", CTRIP_NEARBY_CARD_SELECTORS[0]),
        ("附近卡片(宽松)", CTRIP_NEARBY_CARD_SELECTORS[1]),
        ("酒店页签", CTRIP_NEARBY_TAB_SELECTORS[0]),
        ("显示地图", CTRIP_MAP_BUTTON_SELECTORS[0]),
        ("锚点链接", CTRIP_ANCHOR_LINK_SELECTOR),
    ),
    "meituan": (
        ("卡片容器", MEITUAN_POI_CARD_SELECTOR),
        ("酒店名", MEITUAN_POI_SELECTORS["name"]),
        ("评分", MEITUAN_POI_SELECTORS["score"]),
        ("距离文本", MEITUAN_POI_SELECTORS["address"]),
        ("价格", MEITUAN_POI_SELECTORS["price_num"]),
        ("门市价", MEITUAN_POI_SELECTORS["origin"]),
    ),
}
