"""美团前台(H5 列表页)比价平台(T3B.2 / T3C.2)。

★ 先把命名纠正过来:「地图模式」是**误称**
========================================

计划书与旧代码都把它叫 ``mode="map"``,但旧 ``meituan.py:4-7`` 自己写明:

> 美团 H5 网页版**没有**「显示地图→酒店标签」交互

段3 不再用 ``collect_map_prices`` / ``mode="map"`` 这套命名(段3 §4.5 D 级丢弃)。
真实做法是 **列表页的「为您推荐附近的酒店」区块** ——
它就在 ``list.html`` 上,一次 ``evaluate`` 批量读完。

★ 这条线的真实状态(实测,不是推测)
==================================

* **数据面可用**:旧库 id=21…38 中,美团侧**每次 3/3 都有价**(¥79–¥166),
  实拍截图 ``meituan_list_20260826_173000.png`` 可见「¥236起 / ¥132起 / 距您查询的酒店直线1.2公里」;
* **登录面脆弱**:登录态是**纯人工**(根目录文件、无账号凭据、滑块不破解);
* **契约面残缺**:``distance_km`` 结构性恒 ``None``,价是**列表「起价」**无房型粒度。

★ 三个必须修掉的旧缺陷
====================

1. **``distance_km`` 写死 ``None``**(旧 ``meituan.py:601/605``)——
   卡片上的 ``.poi-address`` **本来就是距离文本**(旧 ``:426-434`` 已把它读进
   ``entry["distance"]``),段3 直接解析成公里(P1/D13);
2. **「起价」被当成房价**——``em.poi-price-num`` 是 ``¥132起``,
   段3 标 ``price_scope="from"``(P1/V73);
3. **锚点「前 4 字」匹配的隐患** —— 旧 ``:535-546`` 用名称前 4 字认锚点卡。
   实测锚点是「隐欲民宿」而首卡是「隐欲民宿·山海别院」——**前 4 字相同但不是同一家店**。
   段3 保留这条规则(它是实测有效的),但匹配到多个候选时**记 note 并可降级**,
   而不是安静地取第一个。

★ 「未识别到价格」不是失败
========================

旧 ``meituan.py:557/560`` 把"没识别到价格"写进 ``error`` 字段(于是和"登录失败"
混在一个通道里)。段3 的语义是:

* **登录态失效 / 被踢回 passport** → :class:`PriceFatalError`(要人处理);
* **风控 / 验证码** → :class:`HumanVerificationError`(要人处理);
* **页面没渲染出卡片** → :class:`PriceRetryableError`(可重试);
* **有卡片但没价** → **正常返回**,``price=None``(这不是错误)。
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any
from urllib.parse import quote, urlencode

from loguru import logger

from hoteldata.domains.compare import geo
from hoteldata.domains.compare.contract import (
    AnchorRef,
    HotelQuote,
    HumanVerificationError,
    PriceFatalError,
    PriceRetryableError,
    QuotesPage,
)
from hoteldata.domains.compare.human import (
    ensure_no_captcha,
    human_pause,
    human_scroll,
    human_type,
    wait_for,
)
from hoteldata.domains.compare.platforms.selectors import (
    MEITUAN_CITY_LIST_URL,
    MEITUAN_DEFAULT_CITY_ID,
    MEITUAN_H5_URL,
    MEITUAN_LIST_URL,
    MEITUAN_LOGIN_URL,
    MEITUAN_POI_CARD_SELECTOR,
    meituan_card_script,
)
from hoteldata.domains.compare.price import classify_dom_price
from hoteldata.domains.compare.registry import register

__all__ = ["MeituanPlatform"]

#: 锚点匹配用的名称前缀长度(旧 ``meituan.py:536`` 的「前 4 字」,逐字继承)
ANCHOR_PREFIX_LEN = 4

#: 评分文本「4.5分」→ 4.5(旧 ``meituan.py:436``)
_SCORE_RE = re.compile(r"(\d(?:\.\d)?)\s*分")

#: 门市价「门市价¥138」→ 138(旧 ``meituan.py:449``)
_ORIGIN_RE = re.compile(r"(\d+(?:\.\d+)?)")

#: 被踢回登录页的特征(URL 级,最可靠)
_LOGIN_URL_MARKERS = ("passport.meituan.com", "ilogin", "login")

#: 未收录城市的平台文案(旧 ``meituan.py:335-338/374-375``)
_UNKNOWN_CITY_HINT = "暂未收录"


def _clean(text: Any) -> str:
    """清空白。旧 ``meituan.py`` 的 ``clean_h5_text``(去私有区字符 + 压空白)。"""
    if not text:
        return ""
    t = "".join(ch for ch in str(text) if not _is_private_use(ch))
    return re.sub(r"\s+", " ", t).strip()


#: 行政区后缀(归一化城市名时去掉,便于"莱州" ↔ "莱州市" 匹配)
_CITY_SUFFIXES = (
    "特别行政区", "自治区", "自治州", "自治县", "地区", "盟",
    "省", "市", "县", "区", "旗",
)


def _norm_city_name(name: str) -> str:
    """归一化城市名:去空白/括号 → 去行政区后缀 → 小写。

    ★ 为什么必须做:美团城市表里是「莱州市」这类**带后缀**的名字,
      而清单/配置里人写的是「莱州」。不归一化就匹配不上,
      会静默退回硬编码的 ``cityId=59``(成都)——
      实测后果是"搜莱州的店,返回成都郫县的推荐"。
    """
    text = re.sub(r"[\s·・]", "", str(name or ""))
    text = re.sub(r"[（(【\[].*?[）)】\]]", "", text)
    # 只去一次后缀(「红河哈尼族彝族自治州」→ 去"自治州")
    for suf in _CITY_SUFFIXES:
        if text.endswith(suf) and len(text) > len(suf):
            text = text[: -len(suf)]
            break
    return text.lower()


#: 不作为"父级"的顶层键(``city_hierarchy.json`` 里的直辖市占位)
_NOT_A_PARENT = ("市辖区", "市辖", "县", "省直辖县级行政区划", "自治区直辖县级行政区划")


def _parent_city_of(city: str, config_dir: Any) -> str:
    """从 ``config/city_hierarchy.json`` 反查某城市所属的**父级地级市**。

    文件结构(实测):``{地级市名: [下辖区县/县级市, ...]}``,337 个顶层键::

        "红河哈尼族彝族自治州": ["个旧市", "开远市", "蒙自市", ...]

    所以"反查"= **遍历所有顶层键,看目标城市在谁的列表里**。

    ★ 跳过 ``市辖区`` / ``县`` 这类**占位键**(它们不是真实城市名)。
    ★ 同时支持**双向子串**(``蒙自`` ↔ ``蒙自市``)。
    ★ 结果**进程内缓存**(337 项遍历一次约几毫秒,但没必要每次做)。

    找不到 → 返回 ``""``(调用方退回默认 cityId 并告警,**不静默**)。
    """
    global _PARENT_CACHE
    key = _norm_city_name(city)
    if not key:
        return ""
    if key in _PARENT_CACHE:
        return _PARENT_CACHE[key]

    import json
    from pathlib import Path

    path = Path(config_dir) / "city_hierarchy.json"
    result = ""
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                # ★ 收集**所有**命中,只在**唯一**时才接受。
                #   第一版一命中就 break,于是「义乌」配到「锦州市」
                #   (锦州下辖"义县",`义乌` 与 `义县` 谁也不是谁的子串 ——
                #    真正命中的是别的键)、「不存在城」配到「阳泉市」。
                #   配错城市 = 拿回另一个城市的酒店与价格,而报告一切正常 ——
                #   这是比"取不到"严重得多的错误,所以宁可返回空。
                hits: list[str] = []
                for parent, children in data.items():
                    if parent in _NOT_A_PARENT or not isinstance(children, list):
                        continue
                    for child in children:
                        cnorm = _norm_city_name(str(child))
                        if not cnorm:
                            continue
                        if cnorm == key or (
                            len(key) >= 2 and (key in cnorm or cnorm in key)
                        ):
                            hits.append(str(parent))
                            break
                uniq = sorted(set(hits))
                if len(uniq) == 1:
                    result = uniq[0]
                elif len(uniq) > 1:
                    logger.warning(
                        "[meituan] {} 的父级候选有 {} 个({}),歧义 → 不推导",
                        city,
                        len(uniq),
                        uniq[:5],
                    )
    except Exception as exc:  # noqa: BLE001 - 推导失败不是致命,退回默认城市
        logger.warning("[meituan] 读 city_hierarchy.json 失败: {}", exc)

    _PARENT_CACHE[key] = result
    return result


#: ``_parent_city_of`` 的进程内缓存(城市名 → 父级地级市)
_PARENT_CACHE: dict[str, str] = {}


def _is_private_use(ch: str) -> bool:
    cp = ord(ch)
    return 0xE000 <= cp <= 0xF8FF or 0xF0000 <= cp <= 0xFFFFD or 0x100000 <= cp <= 0x10FFFD


def _prefix(name: str) -> str:
    """取名称前 4 字(去符号后),用于锚点卡识别。"""
    cleaned = re.sub(r"[\s·・\-—_()（）\[\]【】]", "", str(name or ""))
    return cleaned[:ANCHOR_PREFIX_LEN].lower()


@register
class MeituanPlatform:
    """美团前台(H5 列表页「为您推荐附近的酒店」)。"""

    name = "meituan"
    home_url = MEITUAN_H5_URL

    # ==================================================================
    # 锚点定位(T3B.2 / V67)
    # ==================================================================

    async def resolve_anchor(
        self,
        ctx: Any,
        name: str,
        city: str | None = None,
        *,
        hotel_id: str | None = None,
    ) -> AnchorRef:
        """美团锚点:**没有 ``ebk_hotel_id`` 直达通道**,只能靠列表页的「锚点 + 推荐」结构。

        所以这里只做**城市解析**,真正的锚点卡片识别在 :meth:`collect_quotes`
        里做(它本来就要进列表页 —— 那一步的产物就是"哪张卡是锚点")。

        ★ 与携程的差别是**平台事实**,不是实现偷懒:
          携程自家店在 ``core_hotels.ebk_hotel_id`` 有登记,美团没有对应 id 可登记。
        """
        resolved_city, city_id = await self._resolve_city(ctx, city or "")
        return AnchorRef(
            name=name,
            hotel_id=None,
            city=resolved_city or city,
            city_id=city_id or MEITUAN_DEFAULT_CITY_ID,
            source="name_match",
        )

    async def _resolve_city(self, ctx: Any, city: str) -> tuple[str, str]:
        """城市名 → (规范名, cityId)。

        ★★ 这里曾有个**静默错误**:第一版直接返回硬编码的
        :data:`MEITUAN_DEFAULT_CITY_ID`(``"59"`` = 成都),注释还美其名曰
        "美团接受城市名,由页面自己解析"。实测后果:

        * 锚点「隐欲民宿」在**莱州**,却带着 ``cityId=59``(成都)去搜;
        * 美团搜不到 → 页面显示「暂无相关酒店,我们向您推荐以下酒店」;
        * 于是取回的是**成都郫县/犀浦**的酒店与价格 ——
          **数据是"真"的,但位置全错**,而且不报任何错。

        这比取不到价更危险:报告看起来完全正常,价格也像模像样。

        正确做法(旧 ``meituan.py:283-330`` 的实测路子):**在页面内 fetch 城市列表**
        (该接口要带美团域 cookie,HTTP 直连会被拒),把城市名归一化后匹配 ``id``。
        """
        core = re.split(r"[（(]", city or "")[0].strip()
        if not core:
            return "", MEITUAN_DEFAULT_CITY_ID

        cached = ctx.cache_get("meituan_city", core)
        if cached is not None:
            name, cid = cached  # type: ignore[misc]
            return str(name), str(cid)

        # ① 直接走 HTTP 试一次(便宜),含父级推导
        cid = await self._city_id_with_parent(ctx, core, page=None)
        # ② HTTP 拿不到就用**页面内 fetch**(带 cookie,成功率高)
        if not cid:
            cid = await self._city_id_via_page(ctx, core)

        if not cid:
            logger.warning(
                "[meituan] 城市 {!r} 未在美团城市表里解析出 cityId —— "
                "将退回默认 {}(**可能取到别的城市的酒店**,报告会体现距离不可用)",
                core,
                MEITUAN_DEFAULT_CITY_ID,
            )
        ctx.cache_set("meituan_city", core, (core, cid or MEITUAN_DEFAULT_CITY_ID))
        return core, cid or MEITUAN_DEFAULT_CITY_ID

    async def _city_id_from_list(self, ctx: Any, core: str, page: Any) -> str:
        """从城市列表里匹配 cityId(归一化后相等,其次**包含**)。"""
        cities = await self._fetch_city_list(ctx, page)
        return self._match_city(cities, core)

    @staticmethod
    def _match_city(cities: list[dict], core: str) -> str:
        """城市名 → cityId(**唯一匹配才接受**)。

        ★ 实测教训:第一版用"归一化相等,其次**第一个**包含匹配",
          结果 ``不存在城`` 会配到任意一个含字城市(实测配到 阳泉市)——
          而这正是我们**最不想要**的行为:配错城市 = 拿回另一个城市的酒店与价格,
          报告却一切正常。

        所以:

        1. **归一化相等** —— 命中即可(唯一);
        2. **包含匹配** —— 只接受**唯一候选**,有歧义就返回 ``""``
           (让调用方退回默认城市并**告警**,而不是猜一个)。
        """
        want = _norm_city_name(core)
        if not want or not cities:
            return ""
        # ① 归一化相等
        exact = [
            str(c.get("id"))
            for c in cities
            if _norm_city_name(c.get("name") or "") == want and c.get("id")
        ]
        if len(exact) == 1:
            logger.info("[meituan] 城市 {} -> cityId={}", core, exact[0])
            return exact[0]
        if len(exact) > 1:
            logger.warning("[meituan] 城市 {} 在美团表里有 {} 个同名项,歧义 → 不猜", core, len(exact))
            return ""

        # ② 包含匹配(>=2 字),**必须唯一**
        if len(want) >= 2:
            cands = [
                (str(c.get("id")), c.get("name"))
                for c in cities
                if c.get("id")
                and (cn := _norm_city_name(c.get("name") or ""))
                and (want in cn or cn in want)
            ]
            if len(cands) == 1:
                logger.info("[meituan] 城市 {} -> cityId={}(唯一包含匹配 {})", core, cands[0][0], cands[0][1])
                return cands[0][0]
            if len(cands) > 1:
                logger.warning(
                    "[meituan] 城市 {} 有 {} 个包含候选({}),歧义 → 不猜",
                    core,
                    len(cands),
                    [n for _, n in cands[:5]],
                )
        return ""

    async def _city_id_with_parent(self, ctx: Any, core: str, page: Any) -> str:
        """先直接匹配;不成再**推导父级地级市**匹配(旧 ``meituan.py:113-122``)。

        为什么需要它:美团的城市表是**地级市粒度**,而清单里常写**县级市**。
        实测:``蒙自`` 在美团表里没有,但它是 ``红河哈尼族彝族自治州`` 的下辖市
        (``config/city_hierarchy.json``),而红河州有 cityId=338。

        不推导的后果与"硬编码成都"同类:搜不到就退化成"推荐附近的酒店",
        **拿回一批位置完全不对的酒店**。
        """
        cities = await self._fetch_city_list(ctx, page)
        direct = self._match_city(cities, core)
        if direct:
            return direct

        parent = _parent_city_of(core, ctx.settings.paths.config_dir)
        if not parent:
            return ""
        logger.info("[meituan] {} 不在美团城市表,推导父级地级市:{}", core, parent)
        return self._match_city(cities, parent)

    async def _fetch_city_list(self, ctx: Any, page: Any) -> list[dict]:
        """取城市列表:优先页面内 fetch,退回 HTTP。"""
        if page is not None:
            try:
                data = await page.evaluate(
                    """async (u) => {
                        const r = await fetch(u, {credentials: 'include'});
                        const j = await r.json();
                        return (j && j.data) || [];
                    }""",
                    MEITUAN_CITY_LIST_URL,
                )
                if isinstance(data, list) and data:
                    logger.debug("[meituan] 页面内取到城市 {} 个", len(data))
                    return data
            except Exception as exc:  # noqa: BLE001
                logger.debug("[meituan] 页面内取城市列表失败: {}", exc)
        text = await ctx.http_text(MEITUAN_CITY_LIST_URL)
        if not text:
            return []
        try:
            import json as _json

            payload = _json.loads(text)
            data = payload.get("data") if isinstance(payload, dict) else payload
            return data if isinstance(data, list) else []
        except Exception as exc:  # noqa: BLE001
            logger.debug("[meituan] 解析城市列表失败: {}", exc)
            return []

    async def _city_id_via_page(self, ctx: Any, core: str) -> str:
        """**开一个页面**专门取城市列表(cookie 完整,成功率高)。"""
        try:
            async with ctx.page_session() as (_b, _c, page):
                await page.goto(MEITUAN_H5_URL, wait_until="domcontentloaded", timeout=45000)
                return await self._city_id_with_parent(ctx, core, page)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[meituan] 页面内解析城市失败: {}", exc)
            return ""

    # ==================================================================
    # 取价(T3C.2 / V72)
    # ==================================================================

    async def collect_quotes(
        self,
        ctx: Any,
        anchor: AnchorRef,
        count: int,
        *,
        nights: int = 1,
        rooms: int = 1,
    ) -> QuotesPage:
        """★ **单次 ``evaluate`` 批量读全部卡片**(旧 ``meituan.py:398`` 的语义)。"""
        checkin = (_query_date(ctx) or date.today()).isoformat()
        checkout = (date.fromisoformat(checkin) + timedelta(days=max(1, nights))).isoformat()
        url = _build_list_url(anchor.name, anchor.city_id or MEITUAN_DEFAULT_CITY_ID, checkin, checkout)

        async with ctx.page_session() as (_browser, _context, page):
            await ensure_no_captcha(page, platform=self.name)
            logger.info("[meituan] 列表页直达: {}", url)
            await page.goto(url, wait_until="domcontentloaded", timeout=int(ctx.nav_timeout_s * 1000))
            await human_pause(page)
            await ensure_no_captcha(page, platform=self.name)

            # ★ 登录态:美团**没有账号凭据**,登录态过期就只能人工。
            #   被踢回 passport 要**显式报错**,不能等 30 秒超时后报"没找到卡片"。
            await self._guard_login(page)

            # ★★ 必须**执行一次搜索**结果区才有卡片。
            #
            # 实测:直接打开 ``list/list.html?cityId=…&keyword=…`` 页面能加载,
            # 但结果区是**空的**;而搜索页上点一下「查找酒店」后再进列表页,
            # 30 张 ``a.poi`` 卡片立刻出现。
            #
            # 旧系统把 ``keyword`` 塞进 URL 就当搜索完成了 —— 在当时的版本上恰好可行,
            # 现在不行。所以段3 **显式做一次搜索交互**再提卡。
            #
            # 条件必须 async(见 human.wait_for 的文档:同步 lambda 会让它立刻返回 True)。
            async def _cards_ready() -> bool:
                return await page.locator(MEITUAN_POI_CARD_SELECTOR).count() > 0

            ready = await wait_for(page, _cards_ready, timeout=8.0, desc="美团列表页卡片(直达)")
            if not ready:
                await self._run_search(page, anchor.name, checkin, checkout)
                ready = await wait_for(page, _cards_ready, timeout=25.0, desc="美团搜索后卡片")
            if not ready:
                # 再给一次机会:滚一下(旧 ``:521-533`` 的 wheel 兜底)
                await human_scroll(page, 1, 2)
                ready = await wait_for(page, _cards_ready, timeout=15.0, desc="美团卡片(滚动后)")
            if not ready:
                # 滚动后仍无卡片 —— 先重新确认真是被踢回登录页而不是单纯没渲染
                await self._guard_login(page)
                raise PriceRetryableError(
                    "美团列表页未渲染出任何酒店卡片(疑似风控或改版)。"
                    f"用 `hoteldata price probe --platform meituan` 看选择器命中;页面 URL={page.url}"
                )

            raw = await page.evaluate(meituan_card_script(min(max(count * 2, count), 40)))

        notes: list[str] = []
        quotes, anchor_hits = _cards_to_quotes(raw or [], anchor.name, notes)

        if not quotes:
            raise PriceRetryableError(
                "美团列表页有卡片但一张都没解析出酒店名(选择器可能失效)"
            )

        # 坐标/距离(卡片文本路径 —— 美团没有接口坐标)
        rows = [q.model_dump() for q in quotes]
        geo.attach_distances(rows, anchor.coords)
        quotes = [HotelQuote(**r) for r in rows]

        self_quote = _pick_self(quotes, anchor.name)
        if self_quote is not None and self_quote.price is not None:
            anchor = anchor.model_copy(
                update={
                    "self_price": self_quote.price,
                    "self_price_platform": self.name,
                }
            )

        return QuotesPage(
            quotes=_trim(quotes, anchor.name, count),
            degraded=bool(notes),
            notes=notes,
            price_source="dom",
        )

    async def _run_search(
        self, page: Any, keyword: str, checkin: str, checkout: str
    ) -> None:
        """在美团 H5 上**真正执行一次搜索**(实测:不搜索则结果区为空)。

        路线(按可靠性排序):

        1. 打开**搜索页** ``search/search.html`` 并把关键词/日期填进去;
        2. 点「查找酒店」(或回车)触发搜索;
        3. 搜索会跳/渲染到列表结果 —— 之后由调用方提卡。

        ★ 失败**不抛异常**:搜索交互是"多给的一次机会",失败后由调用方按
          "无卡片"处理(那里的错误信息更准确)。这里只记日志。
        """
        from hoteldata.domains.compare.platforms.selectors import (
            MEITUAN_H5_URL,
            MEITUAN_SEARCH_BUTTON_SELECTORS,
        )

        try:
            search_url = (
                f"{MEITUAN_H5_URL}?keyword={quote(keyword)}"
                f"&checkIn={checkin}&checkOut={checkout}"
            )
            logger.info("[meituan] 改走搜索页并执行一次搜索: {}", search_url)
            await page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
            await human_pause(page)
            await ensure_no_captcha(page, platform=self.name)

            # 关键词输入框:搜索页会把 keyword 带进来,但**结果要按一次搜索才出**
            for sel in ("input[placeholder*='酒店']", "input[type='search']", "input"):
                try:
                    inp = page.locator(sel).first
                    if await inp.count() > 0 and await inp.is_visible():
                        await inp.click(timeout=5000)
                        await human_type(page, keyword)
                        break
                except Exception:  # noqa: BLE001
                    continue

            clicked = False
            for sel in MEITUAN_SEARCH_BUTTON_SELECTORS:
                try:
                    btn = page.locator(sel).first
                    if await btn.count() > 0 and await btn.is_visible():
                        await btn.click(timeout=8000)
                        clicked = True
                        logger.info("[meituan] 已点击搜索按钮:{}", sel)
                        break
                except Exception:  # noqa: BLE001
                    continue
            if not clicked:
                logger.info("[meituan] 未找到搜索按钮,改用回车提交")
                try:
                    await page.keyboard.press("Enter")
                except Exception:  # noqa: BLE001
                    pass
            await human_pause(page)
        except Exception as exc:  # noqa: BLE001 - 搜索交互失败由调用方兜底
            logger.warning("[meituan] 搜索交互未成功(交给调用方判定): {}", exc)

    async def _guard_login(self, page: Any) -> None:
        """被踢回登录页 → :class:`PriceFatalError`(**要人处理,不自动重试**)。

        ★ 为什么不自动填表重登:旧系统虽然写了 ``MEITUAN_USERNAME`` /
          ``MEITUAN_PASSWORD`` 自动填表(``:193-213``),但**滑块仍需人工**
          (``README:68`` 记录过"美团账号被风控锁定")。自动填表只会让风控更早触发。

        所以段3 **不实现自动重登**,只把"需要登录"这件事说清楚。
        """
        try:
            current = (page.url or "").lower()
        except Exception:  # noqa: BLE001
            return
        if any(marker in current for marker in _LOGIN_URL_MARKERS) and "hotel" not in current:
            raise HumanVerificationError(
                "美团登录态已失效,已被重定向到登录页。"
                "美团**没有账号凭据**且滑块需人工处理,请运行 "
                "`hoteldata price login --platform meituan` 手动登录后重试"
            )
        # 页面文案兜底(URL 未变但页面上写着登录)
        try:
            body = await page.locator("body").inner_text(timeout=3000)
        except Exception:  # noqa: BLE001
            return
        if _UNKNOWN_CITY_HINT in (body or ""):
            raise PriceFatalError(
                "美团提示该城市暂未收录酒店。请确认城市名(清单行内 city 优先),"
                "或改用 `hoteldata compare --name <酒店> --city <所属城市>`"
            )
        if "登录" in (body or "") and "酒店" not in (body or ""):
            raise HumanVerificationError(
                "美团页面要求登录(未见酒店列表)。请运行 "
                "`hoteldata price login --platform meituan` 手动登录后重试"
            )


# ---------------------------------------------------------------------------
# 卡片 → 报价
# ---------------------------------------------------------------------------


def _cards_to_quotes(
    raw: list[dict[str, Any]], anchor_name: str, notes: list[str]
) -> tuple[list[HotelQuote], int]:
    """把 ``evaluate`` 读回的卡片转成 :class:`HotelQuote`。

    ★ 字段映射(旧 ``meituan.py:426-449``):

    ==================  ==========================================  ==========================
    卡片字段             含义                                          落到哪
    ==================  ==========================================  ==========================
    ``name``            酒店名                                        ``hotel_name``
    ``score``           「4.5分」                                     ``score``
    ``feedback``        「500+消费」                                  ``reviews``(尽力解析)
    ``address``         ★ **距离文本**(「距您查询的酒店直线1.2公里」)   ``distance_text`` → ``distance_km``
    ``priceNum``        ★ **起价**(``em.poi-price-num``)              ``price`` + ``price_scope="from"``
    ``origin``          「门市价¥138」                                ``raw``(参考)
    ``raw``             整卡文本                                      券价整卡判定 + 诊断
    ==================  ==========================================  ==========================

    ★ 去重按**归一化名**(旧 ``:457-459`` 用原名 ``seen_names``,不做归一去重)。
    """
    from hoteldata.domains.compare.human import norm_hotel_name

    out: list[HotelQuote] = []
    seen: set[str] = set()
    anchor_hits = 0
    want = _prefix(anchor_name)

    for item in raw:
        if not isinstance(item, dict):
            continue
        name = _clean(item.get("name") or "")
        price_text = _clean(item.get("priceNum") or "")
        if not name and not price_text:
            continue
        if not name:
            # 只有价格没有名字 —— 无法归属,跳过(旧实现同样跳过)
            continue

        key = norm_hotel_name(name) or name
        if key in seen:
            continue
        seen.add(key)

        whole = _clean(item.get("raw") or "")
        verdict = classify_dom_price(price_text, whole_card_text=whole)

        score_text = _clean(item.get("score") or "")
        m = _SCORE_RE.match(score_text)
        score = float(m.group(1)) if m else None

        if want and want in _prefix(name):
            anchor_hits += 1

        out.append(
            HotelQuote(
                hotel_name=name,
                price=verdict.price,
                price_scope="from",
                price_source="dom",
                price_rejected=[verdict.raw] if verdict.rejected else [],
                # ★ 美团的距离就在卡片上(旧系统读到了又丢掉)
                distance_km=geo.parse_distance_km(_clean(item.get("address") or "")),
                coord_source="none",
                score=score,
                raw={
                    "distance_text": _clean(item.get("address") or ""),
                    "origin": _clean(item.get("origin") or ""),
                    "price_text": price_text,
                },
            )
        )

    if out and not any(q.price is not None for q in out):
        notes.append("卡片已读到但均未识别出价格(可能只有会员价或需登录)")
    # ★ 锚点识别是**同前缀风险**,必须在算出 anchor_hits 的地方记 ——
    #   放到调用方(collect_quotes)会让"直接调解析器"的验收路径看不到这条提示。
    if anchor_hits > 1:
        notes.append(
            f"锚点前 {ANCHOR_PREFIX_LEN} 字匹配到 {anchor_hits} 张卡片,"
            "可能包含同前缀的不同门店(名单里可能混入非锚点店)"
        )
    return out, anchor_hits


def _build_list_url(name: str, city_id: str, checkin: str, checkout: str) -> str:
    """``list.html?cityId&keyword&checkIn&checkOut``(旧 ``meituan.py:356-363``,**参数名逐字**)。"""
    params = {
        "cityId": city_id or MEITUAN_DEFAULT_CITY_ID,
        "keyword": name,
        "checkIn": checkin,
        "checkOut": checkout,
    }
    return MEITUAN_LIST_URL + "?" + urlencode(params)


def login_url(backurl: str = MEITUAN_H5_URL) -> str:
    """登录 URL(旧 ``meituan.py:140``;``backurl`` 用于登完跳回酒店页)。"""
    return f"{MEITUAN_LOGIN_URL}?backurl={backurl}"


def _query_date(ctx: Any) -> date | None:
    day = getattr(ctx, "query_date", None)
    return day if isinstance(day, date) else None


def _pick_self(quotes: list[HotelQuote], anchor_name: str) -> HotelQuote | None:
    want = _prefix(anchor_name)
    if not want:
        return None
    for q in quotes:
        if want in _prefix(q.hotel_name):
            return q
    return None


def _trim(quotes: list[HotelQuote], anchor_name: str, count: int) -> list[HotelQuote]:
    """去掉锚点自己 → 按距离排序 → 截 ``count``。"""
    want = _prefix(anchor_name)
    others = [q for q in quotes if not (want and want in _prefix(q.hotel_name))]
    others = sorted(
        others,
        key=lambda q: (q.distance_km is None, q.distance_km if q.distance_km is not None else 1e9),
    )
    return others[: max(1, count)]
