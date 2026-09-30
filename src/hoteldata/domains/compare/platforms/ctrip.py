"""携程前台比价平台(T3B.1 / T3C.1)。

★ 最重要的遗产:真接口 ``ctGetNearbyHotelList``
=============================================

旧系统**抓到了这个接口却读不懂它**,于是「零点击直连」从未生效
(证据:``旧系统/data/hotel_reports/diag_ctrip_api_no_hotels_*.json``,
35 份里 33 份为空数组,2 份含完整响应)。

段3 认这个接口,并按**实测字段路径**解析::

    data.hotelList[]:
      base.hotelId                  "100342325"
      base.hotelName                "莱州锦禾轻奢民宿(万通建材批发市场店)"
      comment.score                 "4.7"          ← **字符串**,不是数字
      comment.totalReviews          "267 点评"     ← 带中文后缀
      position.lat                  "37.165587"    ← **字符串**
      position.lng                  "119.946814"
      position.positionDescOfCtrip  "距酒店直线1公里 · 近统战文化广场"
      seoInfo.seoUrl                详情页 URL

★ 为什么不能承诺「接口命中则零点击取价」
=======================================

计划书 §5.6 写了「接口命中则**零点击**取价」。实测**不支持这个承诺**:
旧 diag 只存了 4000 字预览,可见部分**没有数字型 price 字段**
(只有一个 ``isPriceWithDecimal`` 布尔与 ``priceTags`` 标签数组,
且该响应被截断,**完整响应里有没有价格无法从现有证据判断**)。

所以段3 的策略是**分级**:

1. 接口若带价格 → 用接口价,``price_source="api"``;
2. 接口不带价格 → **DOM 卡取价**,``price_source="dom"``;
3. 两者都没有 → 抛 :class:`PriceRetryableError` 或返回"确实没价"的卡片。

**接口保证的是 名称 / ID / 坐标 / 详情页 URL** —— 这几样已经足够,
坐标尤其关键(D13 的修复就靠它)。

★ 兜底条件已修正(旧系统在此翻车)
================================

旧 ``ctrip.py:343`` 的兜底条件是 ``if not hotels or all(price is None)`` ——
而启发式抓来的**券价**让它恒为假,于是「显示地图」兜底**整段跳过**
(12 条归档全中)。

段3 的判定改为 :func:`_need_map_fallback`:只在**候选本身不足**时兜底,
不看"价格是否为空" —— 价格为空是常态(有的卡片确实不显示价),
拿它当兜底开关必然出错。
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlencode

from loguru import logger

from hoteldata.domains.compare import geo
from hoteldata.domains.compare.contract import (
    AnchorRef,
    HotelQuote,
    PriceFatalError,
    PriceRetryableError,
    QuotesPage,
)
from hoteldata.domains.compare.human import (
    ensure_no_captcha,
    human_pause,
    human_scroll,
    wait_for,
)
from hoteldata.domains.compare.platforms.selectors import (
    CTRIP_ANCHOR_LINK_SELECTOR,
    CTRIP_CAPTURE_KEYS,
    CTRIP_CARD_INNER,
    CTRIP_CITY_SITEMAP_URL,
    CTRIP_DETAIL_PAGE_URL,
    CTRIP_HOME_URL,
    CTRIP_LIST_PAGE_URL,
    CTRIP_MAP_BUTTON_SELECTORS,
    CTRIP_NEARBY_API_KEY,
    CTRIP_NEARBY_CARD_SELECTORS,
    CTRIP_NEARBY_CARD_SELECTORS_LEGACY,
    CTRIP_NEARBY_TAB_SELECTORS,
)
from hoteldata.domains.compare.price import classify_dom_price, classify_price_text
from hoteldata.domains.compare.registry import register

__all__ = ["CtripPlatform"]

#: 城市 sitemap 里 ``href=".../hotel/xxx123">中文名酒店<`` 的解析正则
#: (旧 ``ctrip.py:42``,**逐字继承**)
_CITY_LINK_RE = re.compile(r'href="[^"]*/hotel/([a-z]+)(\d+)"[^>]*>([^<]+)酒店<')

#: ``a[href*='hotelId']`` 里取数字 id(旧 ``ctrip.py:70``)
_HOTEL_ID_RE = re.compile(r"hotelId=(\d+)")

#: ``comment.totalReviews`` 形如 "267 点评" → 取数字
_REVIEWS_RE = re.compile(r"(\d+)")

#: 锚点匹配时,卡片名与锚点名都归一化后比前缀
_ANCHOR_PREFIX_LEN = 4


def _to_float(value: Any) -> float | None:
    """容错转 float(接口字段大量是**字符串**)。"""
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    m = _REVIEWS_RE.search(str(value))
    return int(m.group(1)) if m else None


def _deep_get(node: Any, *keys: str) -> Any:
    """按路径安全取值(任一环缺失 → ``None``,不抛)。"""
    cur = node
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
        if cur is None:
            return None
    return cur


def _need_map_fallback(quotes: list[HotelQuote], want: int) -> bool:
    """★ 是否需要「显示地图」兜底 —— **只看候选数量,不看价格**。

    旧 ``ctrip.py:343`` 用 ``all(price is None)`` 当条件,被券价挡住而永不触发。
    段3 的判据是"候选够不够",因为**卡片没有价格是常态**(有的店就是不显示价),
    拿价格当兜底开关在语义上就是错的。
    """
    return len([q for q in quotes if q.hotel_name]) < want


@register
class CtripPlatform:
    """携程前台(列表页 + ``ctGetNearbyHotelList`` 接口)。"""

    name = "ctrip"
    home_url = CTRIP_HOME_URL

    # ==================================================================
    # 锚点定位(T3B.1 / V66)
    # ==================================================================

    async def resolve_anchor(
        self,
        ctx: Any,
        name: str,
        city: str | None = None,
        *,
        hotel_id: str | None = None,
    ) -> AnchorRef:
        """★ 携程锚点:**已登记 ``ebk_hotel_id`` → 直达**(零页面访问);否则城市 sitemap + 列表页首卡。

        逐字继承旧 ``ctrip.py:294-318`` 的优先级,但**去掉了"未登记就报错让用户去登记"**
        那一半 —— 段3 走列表页兜底(旧系统也写了这条路径,只是失败文案比代码更醒目)。
        """
        if hotel_id:
            logger.info("[ctrip] 锚点直达(ebk_hotel_id={}):{}", hotel_id, name)
            return AnchorRef(
                name=name,
                hotel_id=str(hotel_id),
                city=city,
                source="ebk_hotel_id",
            )

        if not (city or "").strip():
            # 没有城市就无法用列表页兜底 —— 这是**明确的不可重试错误**,
            # 不是"今天没有附近酒店"
            raise PriceFatalError(
                f"携程锚点「{name}」既没有登记 ebk_hotel_id,也没有 city,无法定位。"
                "请用 `hoteldata targets set` 补 city,或把该店登记进 core_hotels 带上 ebk_hotel_id"
            )

        city_id = await self._resolve_city_id(ctx, city or "")
        if not city_id:
            raise PriceFatalError(
                f"携程城市 sitemap 里找不到城市 {city!r}(锚点:{name})。"
                f"请确认城市名,或改用 `hoteldata compare --name {name} --city <准确城市>`"
            )

        async with ctx.page_session() as (_browser, _context, page):
            await ensure_no_captcha(page, platform=self.name)
            url = _build_list_url(name, city_id)
            logger.info("[ctrip] 锚点兜底:直达列表页 {}", url)
            await page.goto(url, wait_until="domcontentloaded", timeout=int(ctx.nav_timeout_s * 1000))
            await human_pause(page)
            await ensure_no_captcha(page, platform=self.name)
            found = await self._anchor_hotel_id_from_page(page)
            if not found:
                raise PriceFatalError(
                    f"携程列表页未取到锚点 hotelId(城市 {city},酒店 {name})。"
                    "页面可能改版或该城市无结果 —— 用 `hoteldata price probe --platform ctrip` 看选择器命中"
                )

        return AnchorRef(name=name, hotel_id=found, city=city, city_id=city_id, source="city_sitemap")

    async def _resolve_city_id(self, ctx: Any, city: str) -> str:
        """城市名 → 携程 cityId(旧 ``ctrip.py:186-201``)。

        旧实现有个类级 ``_city_id_cache``(**进程内、不落盘**)—— 段3 改成
        ``ctx.cache``(由 runner 提供的**单次运行内**缓存),避免类级可变状态。
        """
        core = re.split(r"[（(]", city)[0].strip()
        if not core:
            return ""
        cached = ctx.cache_get("ctrip_city", core)
        if cached is not None:
            return str(cached)

        html = await ctx.http_text(CTRIP_CITY_SITEMAP_URL)
        found = ""
        if html:
            for m in _CITY_LINK_RE.finditer(html):
                cid, cname = m.group(2), m.group(3).strip()
                if cname == core or (len(core) >= 2 and core in cname) or (
                    len(core) >= 2 and cname in core
                ):
                    found = cid
                    logger.info("[ctrip] 城市 {} -> cityId={}", cname, cid)
                    break
        ctx.cache_set("ctrip_city", core, found)
        if not found:
            logger.warning("[ctrip] 城市列表里未找到: {}", city)
        return found

    async def _anchor_hotel_id_from_page(self, page: Any) -> str:
        """列表页首个 ``a[href*='hotelId']`` → hotelId(旧 ``ctrip.py:64-75``)。

        ★ 旧实现只 ``wait_for`` 了 12 秒然后取首个链接 —— 若**锚点卡不在首位**
          就会认错店。段3 保留"取首个"的兜底,但**优先按名称匹配**
          (见 :meth:`_pick_anchor_link`),匹配不上才退回首个。
        """
        # ★ 条件必须 async(见 meituan.py 里同一处坑的说明):
        #   ``locator(...).count()`` 是协程,用同步 lambda 包会让 wait_for 立刻返回 True。
        async def _links_ready() -> bool:
            return await page.locator(CTRIP_ANCHOR_LINK_SELECTOR).count() > 0

        ok = await wait_for(page, _links_ready, timeout=12.0, desc="携程列表页渲染")
        if not ok:
            return ""
        return await self._pick_anchor_link(page, None)

    async def _pick_anchor_link(self, page: Any, anchor_name: str | None) -> str:
        try:
            loc = page.locator(CTRIP_ANCHOR_LINK_SELECTOR)
            total = await loc.count()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ctrip] 取 hotelId 失败: {}", exc)
            return ""

        # ① 按名称匹配(前 4 字子串)
        if anchor_name and total:
            want = _norm_prefix(anchor_name)
            for i in range(min(total, 30)):
                try:
                    item = loc.nth(i)
                    text = ((await item.inner_text()) or "").strip()
                except Exception:  # noqa: BLE001
                    continue
                if want and want in _norm_prefix(text):
                    href = (await item.get_attribute("href")) or ""
                    m = _HOTEL_ID_RE.search(href)
                    if m:
                        logger.info("[ctrip] 锚点按名称命中第 {} 个链接:{}", i + 1, text[:40])
                        return m.group(1)

        # ② 退回首个
        if total:
            href = (await loc.first.get_attribute("href")) or ""
            m = _HOTEL_ID_RE.search(href)
            if m:
                logger.info("[ctrip] 锚点按首个链接定位(名称未匹配)")
                return m.group(1)
        return ""

    # ==================================================================
    # 取价(T3C.1 / V71)
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
        """★ 一次页面访问:进详情页 → 等渲染 → 接口捕获优先 → DOM 兜底 → 距离。"""
        if not anchor.hotel_id:
            raise PriceFatalError(f"携程取价缺少锚点 hotelId(锚点:{anchor.name})")

        checkin = (anchor_query_date(ctx) or date.today()).isoformat()
        checkout = (date.fromisoformat(checkin) + timedelta(days=max(1, nights))).isoformat()
        url = _build_detail_url(anchor.hotel_id, checkin, checkout)

        captured: list[dict[str, Any]] = []
        page_out: QuotesPage

        async with ctx.page_session() as (_browser, _context, page):
            # ★ 只读抓包(旧 ``base.py:77-88`` 的语义):**不拦截、不改写、不发请求**,
            #   只旁听页面自己发出的响应。命中真接口优先,否则用 DOM。
            #
            # ★ 回调必须是 **async**:``response.json()`` 是协程,同步回调里拿不到 body ——
            #   而"拿不到 body"正是旧系统 `_scan_hotel_entries` 永远解析不出结果的
            #   同一条死路上的另一块石头(body 为空 → 没有 price 键 → 判无效)。
            async def _on_response(response: Any) -> None:
                await _capture_response(response, captured)

            page.on("response", _on_response)
            await ensure_no_captcha(page, platform=self.name)
            logger.info("[ctrip] 直达详情页: {}", url)
            await page.goto(url, wait_until="domcontentloaded", timeout=int(ctx.nav_timeout_s * 1000))
            await human_pause(page)
            await ensure_no_captcha(page, platform=self.name)

            # ① 等真接口(它就是「附近酒店」的数据源)
            api_ready = await wait_for(
                page,
                lambda: any(CTRIP_NEARBY_API_KEY in (c.get("url") or "") for c in captured),
                timeout=float(ctx.page_ready_timeout_s),
                interval=1.0,
                desc="携程附近酒店接口",
            )

            quotes: list[HotelQuote] = []
            notes: list[str] = []
            price_source = "dom"

            if api_ready:
                payload = _find_nearby_payload(captured)
                quotes = _quotes_from_api(payload)
                if quotes:
                    logger.info("[ctrip] 接口命中,解析出 {} 家附近酒店", len(quotes))
                else:
                    notes.append("接口已捕获但未解析出条目")

            # ★★ 接口**结构性不带价**(实测:4 锚点 × 20 家 = 80 家全是 ``priceStr="?"``),
            #    只给 名称/ID/坐标/距离/评分。所以**只要接口没给出价格,就必须补一次 DOM**
            #    —— 而不是等"quotes 为空"才兜底。
            #
            #    这正是旧系统同类错误的镜像:它用 ``all(price is None)`` 当兜底条件,
            #    却被**券价**弄成假,于是兜底永不触发;段3 第一版又用
            #    "quotes 是否为空"当条件,而接口**有名字没价**同样让兜底永不触发。
            #    两个版本犯的是**同一个错**:兜底判据选错了信号。
            #    正确的判据是「**有没有拿到价格**」。
            if not any(q.price is not None for q in quotes):
                await human_scroll(page, 1, 3)
                dom_quotes = await _quotes_from_dom(page)
                if dom_quotes:
                    before = len([q for q in quotes if q.price is not None])
                    quotes = _merge_dom_prices(quotes, dom_quotes)
                    gained = len([q for q in quotes if q.price is not None]) - before
                    price_source = "dom"
                    notes.append(
                        f"接口不含价,DOM 卡片补价 {gained} 家"
                        if gained
                        else "接口不含价,DOM 卡片也未取到价"
                    )
                    logger.info("[ctrip] DOM 补价后:{} 家有价(共 {} 家)", gained, len(quotes))
                else:
                    notes.append("接口不含价,且 DOM 卡片未命中")

            # ③ 「显示地图」兜底(★ 判据是候选数量,不是价格 —— 见 _need_map_fallback)
            if _need_map_fallback(quotes, want=min(count, 1)):
                extra = await self._try_map_fallback(page)
                if extra:
                    quotes = _merge_by_name(quotes, extra)
                    notes.append("走了「显示地图」兜底")
                    logger.info("[ctrip] 地图兜底补充后共 {} 家", len(quotes))

            if not quotes:
                # ★ 骨架页 = 还没渲染完 → **可重试**,而不是"没有附近酒店"
                raise PriceRetryableError(
                    "携程页面未渲染出任何附近酒店(疑似骨架页/风控)。"
                    "这不是「附近没有酒店」—— 已按可重试处理"
                )

            # ④ 坐标 + 距离(★ D13 的修复点)
            anchor_coords = anchor.coords
            rows = [q.model_dump() for q in quotes]
            geo.attach_distances(rows, anchor_coords)
            quotes = [HotelQuote(**r) for r in rows]

            # ⑤ 锚点自己的价(报告里的「本店价」)
            self_quote = _pick_self(quotes, anchor.name)
            if self_quote is not None and self_quote.price is not None:
                anchor = anchor.model_copy(
                    update={
                        "self_price": self_quote.price,
                        "self_price_platform": self.name,
                        "coords": self_quote.coords or anchor.coords,
                        "coord_source": self_quote.coord_source or anchor.coord_source,
                    }
                )

            raw_path = await _persist_raw(ctx, anchor.name, captured)
            page_out = QuotesPage(
                quotes=_trim(quotes, anchor.name, count),
                degraded=bool(notes),
                notes=notes,
                raw_json_path=raw_path,
                price_source=price_source,  # type: ignore[arg-type]
            )

        return page_out

    async def _try_map_fallback(self, page: Any) -> list[HotelQuote]:
        """点「显示地图」→「酒店」页签 → 再提一次卡(旧 ``ctrip.py`` 的三重兜底)。

        ★ 每一步都可能不存在(改版 / 无地图入口),所以**失败即返回空**,
        由调用方决定是否算降级 —— **不抛异常**(兜底失败不等于整次取价失败)。
        """
        try:
            for sel in CTRIP_MAP_BUTTON_SELECTORS:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click(timeout=8000)
                    await human_pause(page)
                    break
            else:
                return []

            for sel in CTRIP_NEARBY_TAB_SELECTORS:
                tab = page.locator(sel).first
                if await tab.count() > 0 and await tab.is_visible():
                    await tab.click(timeout=8000)
                    await human_pause(page)
                    break

            async def _map_cards_ready() -> bool:
                return await page.locator(CTRIP_NEARBY_CARD_SELECTORS[0]).count() > 0

            await wait_for(page, _map_cards_ready, timeout=15.0, desc="携程地图附近卡片")
            return await _quotes_from_dom(page)
        except Exception as exc:  # noqa: BLE001
            logger.info("[ctrip] 地图兜底未成功(不影响主结果): {}", exc)
            return []


# ---------------------------------------------------------------------------
# 页面 → 报价
# ---------------------------------------------------------------------------


async def _quotes_from_dom(page: Any) -> list[HotelQuote]:
    """一次 ``locator.all()`` 批量提卡(**不逐家开详情页**)。

    ★ 支持两代卡片结构:

    1. **新版** ``div[class*='nearbyHotelCard_nearbyHotelCard']``(实测 20 个,
       与 ``ctGetNearbyHotelList`` 的 20 家一一对应)—— 这里走
       :func:`_parse_nearby_card_text` 解析 ``innerText``;
    2. **旧版** ``div[class*='recommendCard']``(已 0 命中,仅作兜底)。
    """
    cards = None
    used = ""
    for sel in CTRIP_NEARBY_CARD_SELECTORS + CTRIP_NEARBY_CARD_SELECTORS_LEGACY:
        try:
            loc = page.locator(sel)
            n = await loc.count()
            if n > 0:
                cards = await loc.all()
                used = sel
                logger.debug("[ctrip] DOM 卡片选择器命中:{} -> {} 张", sel, n)
                break
        except Exception as exc:  # noqa: BLE001
            logger.debug("[ctrip] 选择器 {} 失败: {}", sel, exc)
    if not cards:
        return []

    # 新版卡片的 innerText 自解释,整卡解析;旧版走子选择器
    is_new = "nearbyHotelCard" in used
    out: list[HotelQuote] = []
    for card in cards[:40]:
        try:
            if is_new:
                text = ""
                try:
                    text = (await card.inner_text()) or ""
                except Exception:  # noqa: BLE001
                    text = ""
                q = _parse_nearby_card_text(text)
                if q is not None:
                    out.append(q)
                continue

            # ---- 旧版兜底 ----
            name = await _inner(card, CTRIP_CARD_INNER["name"])
            if not name:
                continue
            price_text = await _inner(card, CTRIP_CARD_INNER["price"])
            score_text = await _inner(card, CTRIP_CARD_INNER["score"])
            dist_text = await _inner(card, CTRIP_CARD_INNER["distance"])
            whole = ""
            try:
                whole = (await card.inner_text()) or ""
            except Exception:  # noqa: BLE001
                pass
            url = None
            try:
                link = card.locator(CTRIP_CARD_INNER["link"]).first
                if await link.count() > 0:
                    url = await link.get_attribute("href")
            except Exception:  # noqa: BLE001
                pass

            verdict = classify_dom_price(price_text, whole_card_text=whole)
            out.append(
                HotelQuote(
                    hotel_name=name,
                    price=verdict.price,
                    price_scope="from",
                    price_source="dom",
                    price_rejected=[verdict.raw] if verdict.rejected else [],
                    distance_km=geo.parse_distance_km(dist_text) if dist_text else None,
                    score=_to_float(re.sub(r"[^\d.]", "", score_text)) if score_text else None,
                    url=url,
                    raw={"price_text": price_text, "dist_text": dist_text},
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[ctrip] 单张卡片解析失败(跳过): {}", exc)
    if out:
        logger.info("[ctrip] DOM 卡片解析出 {} 家({})", len(out), used)
    return out


#: 新版附近酒店卡片 innerText 的形状(实测)::
#:
#:     4.7 51点评 寂帆酒店(青城山站店) 距酒店直线930米 · 近青城山站 ¥359 ¥255 起
#:
#: * 开头的 ``4.7`` = 评分,``51点评`` = 点评数(**均可缺**);
#: * 名称 = 评分/点评之后、``距酒店直线`` 之前的那一段;
#: * ``¥359`` = 原价(划线价),``¥255`` = **现价** —— 取**最后一个** ¥ 数字;
#: * ``起`` 表示这是起价。
_NEARBY_DIST_SPLIT = "距酒店直线"
#: 卡片开头的「评分 + 点评数」。
#:
#: ★ **必须支持千分位**:实测卡片刻度有 ``4.4 1,070 点评`` 这种写法,
#:   第一版写成 ``(\d+)\s*点评`` 会**整条匹配失败**,于是"4.4 1,070 点评 xxx"
#:   被当成酒店名 —— 归一化后匹配不上接口条目,同一家店在报告里出现两次。
#:   教训:解析**人看的文本**时,数字的分隔符是最容易漏的一类。
_CARD_SCORE_RE = re.compile(r"^\s*(\d(?:\.\d)?)\s*([\d,]{1,9})\s*点评")
_CARD_TRAILING_RE = re.compile(r"(\d{1,4})\s*(公里|千米|米|km|KM|m)")


def _parse_nearby_card_text(text: str) -> HotelQuote | None:
    """解析**新版**附近酒店卡片的 ``innerText`` → :class:`HotelQuote`。

    为什么整卡文本解析而不是逐子选择器:那串类名是**哈希后缀**
    (``nearbyHotelCard_nearbyHotelCard__QSGhD``、``priceBox_ctripRealPrice__ijkNJ``),
    平台每次发版都会变。而 ``innerText`` 的**句式**(评分·点评·名称·距离·价格)
    稳定得多 —— 而且这正是旧系统的教训:它写死了 ``recommendCard`` 类名,
    改版后 **0 命中**,整条取价链静默失效。

    ★ 价格取**最后一个** ¥ 数字:卡片上先出现划线原价(``¥359``)、
      后出现现价(``¥255``),现价才是要报的价。
    """
    raw = " ".join((text or "").split())
    if not raw or "¥" not in raw:
        return None

    # ① 评分 / 点评数(可缺)
    score: float | None = None
    reviews: int | None = None
    m = _CARD_SCORE_RE.match(raw)
    body = raw
    if m:
        score = _to_float(m.group(1))
        # 点评数可能带千分位:``1,070`` → 1070
        reviews = _to_int(m.group(2).replace(",", ""))
        body = raw[m.end() :].strip()

    # ② 按距离标志切成「名称 | 距离近邻 | 价格」
    head, sep, tail = body.partition(_NEARBY_DIST_SPLIT)
    name = head.strip()
    if not name:
        # 没有距离标志 → 退化为「价格之前的那段」当名称
        name = re.split(r"[¥￥]", body)[0].strip()
    if not name or len(name) < 2:
        return None

    # ③ 价格:取最后一个 ¥ 数字(现价);同时把倒数第二个当原价留档
    prices = re.findall(r"[¥￥]\s*(\d{2,6})", raw)
    if not prices:
        return None
    verdict = classify_price_text(prices[-1])
    rejected: list[str] = []
    if verdict.rejected:
        # ★ 券价过滤:最后一段若被判为券价,就**不要**拿它当房价
        rejected.append(verdict.raw)
    origin = prices[-2] if len(prices) >= 2 else None

    # ④ 距离:卡片上写「距酒店直线930米」(**米制**,只认公里会丢)
    dist_km = None
    if sep:
        dm = _CARD_TRAILING_RE.search(tail) or _CARD_TRAILING_RE.search(body)
        if dm:
            dist_km = geo.parse_distance_km(f"{dm.group(1)}{dm.group(2)}")

    return HotelQuote(
        hotel_name=name[:120],
        price=verdict.price,
        price_scope="from",  # 卡片带「起」→ 起价
        price_source="dom",
        price_rejected=rejected,
        distance_km=dist_km,
        coord_source="card" if dist_km is not None else "none",
        score=score,
        reviews=reviews,
        raw={
            "card_text": raw[:200],
            "origin_price": origin,
            "parsed_from": "nearby_card_text",
        },
    )


async def _inner(card: Any, selector: str) -> str:
    try:
        el = card.locator(selector).first
        if await el.count() == 0:
            return ""
        return ((await el.inner_text()) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# 接口 → 报价
# ---------------------------------------------------------------------------


async def _capture_response(response: Any, sink: list[dict[str, Any]]) -> None:
    """只读旁听响应并**当场读出 JSON 体**(异步回调)。

    ★ 与旧实现的关键差别:旧 ``base.py:77-88`` 用 **URL 关键词表猜**接口
      (``_CAPTURE_KEYS``),而真接口名 ``ctGetNearbyHotelList`` 恰好能被 ``near`` 命中,
      但**它抓回来的 body 在解析阶段被要求"含数值型 price"而整条丢弃**
      → 这才是「零点击直连从未生效」的根因。

      段3 的做法:**按接口名认**,并且**不再对 body 形状挑三拣四** ——
      拿到就存,解析失败就降级 DOM(而不是把整个捕获链路判死)。
    """
    try:
        url = response.url or ""
        if CTRIP_NEARBY_API_KEY not in url and not any(k in url.lower() for k in CTRIP_CAPTURE_KEYS):
            return
        body: Any = None
        try:
            body = await response.json()
        except Exception as exc:  # noqa: BLE001 - 非 JSON 响应(图片/HTML)很常见
            logger.debug("[ctrip] 响应非 JSON,忽略:{} ({})", url[:100], exc)
        sink.append({"url": url, "json": body})
    except Exception as exc:  # noqa: BLE001
        logger.debug("[ctrip] 旁听响应失败: {}", exc)


def _find_nearby_payload(captured: list[dict[str, Any]]) -> dict[str, Any] | None:
    """取真接口的 JSON 体。**优先精确接口名;没有才退回关键词表。**"""
    exact = [c for c in captured if CTRIP_NEARBY_API_KEY in (c.get("url") or "")]
    pool = exact or captured
    for cap in reversed(pool):
        body = cap.get("json")
        if isinstance(body, dict) and _deep_get(body, "data", "hotelList"):
            return body
    return None


def _quotes_from_api(payload: dict[str, Any] | None) -> list[HotelQuote]:
    """``data.hotelList[]`` → :class:`HotelQuote`(实测字段路径,见模块文档)。"""
    if not payload:
        return []
    items = _deep_get(payload, "data", "hotelList") or []
    if not isinstance(items, list):
        return []

    out: list[HotelQuote] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(_deep_get(item, "base", "hotelName") or "").strip()
        if not name:
            continue

        price_text = _first_price_text(item)
        verdict = classify_price_text(price_text) if price_text else None

        out.append(
            HotelQuote(
                hotel_name=name,
                hotel_id=_as_str(_deep_get(item, "base", "hotelId")),
                price=verdict.price if verdict else None,
                price_scope="from",
                price_source="api",
                price_rejected=[verdict.raw] if (verdict and verdict.rejected) else [],
                coords=geo.coords_from_obj(item, depth=2),
                coord_source="api",
                distance_km=geo.parse_distance_km(
                    _deep_get(item, "position", "positionDescOfCtrip")
                ),
                score=_to_float(_deep_get(item, "comment", "score")),
                reviews=_to_int(_deep_get(item, "comment", "totalReviews")),
                url=_as_str(_deep_get(item, "seoInfo", "seoUrl")),
                raw={"positionDesc": _deep_get(item, "position", "positionDescOfCtrip")},
            )
        )
    return out


def _first_price_text(item: dict[str, Any]) -> str:
    """在接口条目里找**第一个像价格的文本**。

    ★ 实测该接口的可见部分**没有数字型 price 字段**(只有 ``isPriceWithDecimal``
      布尔与 ``priceTags`` 标签数组) —— 所以这个函数**大概率返回空**,
      取价会落到 DOM。保留它是因为:① 响应被截断过,完整响应里可能有价格;
      ② 一旦平台加回价格字段,这里就能直接用上,不用再改结构。
    """
    for path in (
        ("price", "price"),
        ("price", "amount"),
        ("priceInfo", "price"),
        ("base", "price"),
        ("roomInfo", "price"),
    ):
        val = _deep_get(item, *path)
        if isinstance(val, (int, float, str)) and str(val).strip():
            return str(val)
    tags = _deep_get(item, "tags", "priceTags")
    if isinstance(tags, list):
        for tag in tags:
            title = _deep_get(tag, "title")
            if title and any(ch.isdigit() for ch in str(title)):
                return str(title)
    return ""


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _norm_prefix(name: str) -> str:
    """归一化后取前 N 字,用于锚点名称匹配(旧 ``meituan.py:536`` 的"前 4 字"同源做法)。"""
    cleaned = re.sub(r"[\s·・\-—_()（）\[\]【】]", "", str(name or ""))
    return cleaned[:_ANCHOR_PREFIX_LEN].lower()


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _build_list_url(anchor_name: str, city_id: str, checkin: str = "", checkout: str = "") -> str:
    """列表页直达 URL(旧 ``ctrip.py:203-213``,**参数名逐字**:``city``/``keyword``)。"""
    params: dict[str, str] = {"city": city_id or "1", "keyword": anchor_name}
    if checkin:
        params["checkin"] = checkin
    if checkout:
        params["checkout"] = checkout
    return CTRIP_LIST_PAGE_URL + "?" + urlencode(params)


def _build_detail_url(hotel_id: str, checkin: str = "", checkout: str = "") -> str:
    """详情页直达 URL(旧 ``ctrip.py:215-224``,**参数名逐字**:``hotelId``)。"""
    params: dict[str, str] = {"hotelId": hotel_id}
    if checkin:
        params["checkin"] = checkin
    if checkout:
        params["checkout"] = checkout
    return CTRIP_DETAIL_PAGE_URL + "?" + urlencode(params)


def anchor_query_date(ctx: Any) -> date | None:
    """从上下文取查询日期(``ctx.query_date``);没有就用今天。"""
    day = getattr(ctx, "query_date", None)
    if isinstance(day, date):
        return day
    return None


def _pick_self(quotes: list[HotelQuote], anchor_name: str) -> HotelQuote | None:
    """在候选里找**锚点自己**(报告的「本店价」)。

    旧系统把锚点自己也算进"附近酒店"(实测「隐欲民宿·山海别院」同时出现在两个平台),
    段3 把它单列,不当竞对 —— 但仍要展示,否则用户看不到自家价。
    """
    want = _norm_prefix(anchor_name)
    if not want:
        return None
    for q in quotes:
        if want and want in _norm_prefix(q.hotel_name):
            return q
    return None


def _merge_by_name(base: list[HotelQuote], extra: list[HotelQuote]) -> list[HotelQuote]:
    """按名称合并两批候选(后面的只补前面没有的)。"""
    seen = {re.sub(r"\s+", "", q.hotel_name) for q in base}
    for q in extra:
        key = re.sub(r"\s+", "", q.hotel_name)
        if key and key not in seen:
            seen.add(key)
            base.append(q)
    return base


def _norm_key(name: str) -> str:
    """归一化酒店名做匹配。

    ★ 直接用 :func:`~hoteldata.domains.compare.human.norm_hotel_name`
      (**逐字继承旧 ``human.py:221-228``** 的三步:去空白 → 去括号内容 → 去门店后缀)。
      段3 第一版自己写了个"去空白 + 去括号"的简化版,**漏了"去门店后缀"**,
      于是接口名与卡片名归一化后不一致 → 同一家店在报告里**出现两次**。

      这正说明"复用既有工具"不是洁癖:同一个归一化规则散成两份,
      必然出现"一边去后缀、一边不去"的不一致。
    """
    from hoteldata.domains.compare.human import norm_hotel_name

    return norm_hotel_name(str(name or ""))


#: 门店后缀(与 ``human.norm_hotel_name`` 里那条正则同源 —— 用于"相等前缀"比较)
_STORE_SUFFIXES = ("酒店", "民宿", "客栈", "公寓", "旅馆", "宾馆", "山庄", "度假村", "青年旅舍")


def _same_store(a: str, b: str) -> bool:
    """判断两个**已归一化**的名字是否指同一家店(严格版)。

    只在"去掉门店后缀后完全相同"时才认 —— 比"前 N 字包含"严格得多,
    目的是**宁可配不上,也不要串台**。
    """
    if not a or not b:
        return False
    if a == b:
        return True
    for suf in _STORE_SUFFIXES:
        if a == b + suf or b == a + suf:
            return True
    return False


def _merge_dom_prices(
    api_quotes: list[HotelQuote], dom_quotes: list[HotelQuote]
) -> list[HotelQuote]:
    """★ 把 DOM 卡片的**价格/评分/距离**补进接口条目(按酒店名匹配)。

    **为什么必须以接口条目为骨架**:接口给的是 **ID + 精确坐标**
    (``position.lat/lng`` → haversine 算出的距离最可信),DOM 只给名称与文本距离。
    两者各自最强的地方不同,所以合成一条而不是二选一。

    匹配策略(依次退化,**匹配不上就保留接口条目,绝不硬配**):

    1. **归一化全名相等**;
    2. **相等前缀**:两边的归一化名去掉门店后缀后**完全相同**;
    3. 都匹配不上 → 保留该接口条目(有坐标、无价,报告会列出)。

    ★ 为什么不做"前 N 字包含"这种宽松匹配:青城山一带店名高度相似
      (「上青城度假酒店」「青城山前山景区原石滩酒店」「青城山时光里精品客栈」…),
      一个 6 字前缀会**把 A 的价配到 B 头上** —— 那比"少一个价"严重得多
      (报错价会让客户投诉)。所以宁可**配不上就分开列**。

      实测教训:段3 第二版用 `key[:6]` 前缀包含,结果同一家店在报告里
      **出现两次**(一条接口的、一条 DOM 的)—— 因为它的卡片被前缀误配给了别家。
    """
    by_key: dict[str, HotelQuote] = {}
    for q in dom_quotes:
        by_key.setdefault(_norm_key(q.hotel_name), q)

    used: set[str] = set()
    out: list[HotelQuote] = []
    for q in api_quotes:
        key = _norm_key(q.hotel_name)
        dom = by_key.get(key)
        dom_key = key if (dom is not None and key not in used) else ""
        if not dom_key and len(key) >= 3:
            # ② 相等前缀:只接受**唯一**候选,歧义就不配
            cands = [
                (k, v)
                for k, v in by_key.items()
                if k not in used and k and _same_store(key, k)
            ]
            if len(cands) == 1:
                dom_key, dom = cands[0]
            else:
                dom = None
        if dom is None or not dom_key:
            out.append(q)
            continue
        used.add(dom_key)
        out.append(
            q.model_copy(
                update={
                    # 接口无价 → 用卡片价;接口有价 → **优先接口**(结构化字段更稳)
                    "price": q.price if q.price is not None else dom.price,
                    "price_scope": dom.price_scope if q.price is None else q.price_scope,
                    "price_source": q.price_source if q.price is not None else dom.price_source,
                    "price_rejected": list(dom.price_rejected),
                    # 距离:接口坐标算出来的优先(更精确);没有才用卡片的文本距离
                    "distance_km": q.distance_km if q.distance_km is not None else dom.distance_km,
                    "coord_source": q.coord_source if q.coords is not None else dom.coord_source,
                    "score": q.score if q.score is not None else dom.score,
                    "reviews": q.reviews if q.reviews is not None else dom.reviews,
                    "raw": {
                        **(q.raw or {}),
                        "dom_card": (dom.raw or {}).get("card_text"),
                        "dom_name": dom.hotel_name,
                    },
                }
            )
        )

    # 接口没返回、但卡片上有的店 → 追加(避免漏)
    for q in dom_quotes:
        if _norm_key(q.hotel_name) not in used:
            out.append(q)
    return out


def _trim(quotes: list[HotelQuote], anchor_name: str, count: int) -> list[HotelQuote]:
    """★ 去掉锚点自己 → 按距离排序 → 截 ``count``(旧 ``runner.py:115,162`` 的"取 2N 再截 N")。

    **排序在这里做**(而不是在 runner):因为 ``distance_km`` 是平台内算出来的,
    平台自己最清楚哪条距离可信。
    """
    want = _norm_prefix(anchor_name)
    others = [q for q in quotes if not (want and want in _norm_prefix(q.hotel_name))]
    others = sorted(
        others,
        key=lambda q: (q.distance_km is None, q.distance_km if q.distance_km is not None else 1e9),
    )
    return others[: max(1, count)]


async def _persist_raw(ctx: Any, anchor_name: str, captured: list[dict[str, Any]]) -> str | None:
    """把捕获到的接口响应**全量**落盘(段3 P10:诊断要存全量,不是 4000 字预览)。"""
    writer = getattr(ctx, "persist_capture", None)
    if writer is None or not captured:
        return None
    try:
        return await writer("ctrip", anchor_name, [_serializable(c) for c in captured])
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ctrip] 原始响应落盘失败(不影响取价): {}", exc)
        return None


def _serializable(cap: dict[str, Any]) -> dict[str, Any]:
    return {"url": cap.get("url"), "json": cap.get("json")}
