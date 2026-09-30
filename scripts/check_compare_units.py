"""段3 自检 —— 取价语义 / 坐标距离 / 名称归一化(纯函数层,不需要 DB 与网络)。

这个脚本的存在理由,是段3 相对旧系统**新增的那部分语义**最容易写错、
且错了最难发现(旧系统就是把「折扣券 ¥34」当成房价报出去,半年没人发现):

* **券价过滤**(P1 / V83)—— 12/18 条旧归档的翻车点;
* **``price_scope``**(V73 修订)—— 「¥236起」不是房价;
* **券价 vs 无价的区别** —— 一个是我们主动不要,一个是真的没有;
* **坐标逐条归属**(P3)—— 旧 ``deep_find_coords`` 返回扁平列表、接不上线;
* **距离单位**(米/公里)—— 只认"公里"会把「800米」整条静默丢掉;
* **名称归一化** —— 跨平台去重的基础。

用法::

    .venv\\Scripts\\python.exe scripts\\check_compare_units.py
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from hoteldata.domains.compare import geo, price  # noqa: E402
from hoteldata.domains.compare.human import norm_hotel_name  # noqa: E402
from hoteldata.domains.compare.price import PriceVerdict  # noqa: E402

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(label: str, got: object, want: object) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        FAILURES.append(f"{label}: got={got!r} want={want!r}")
        print(f"  [FAIL] {label}\n         got ={got!r}\n         want={want!r}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------------------
# ① ★ 券价过滤(P1 / V83)—— 旧系统 12/18 条归档的翻车点
# ---------------------------------------------------------------------------

section("① 券价过滤(V83)—— 旧系统把优惠券当房价")

# 旧系统实测的假阳性原文(证据:docs/参考/段3-分析/_证据-旧系统比价库dump.txt)
check("「折扣券 ¥34」被拒", price.classify_price_text("折扣券 ¥34").price, None)
check(
    "「折扣券 ¥34」拒绝理由可见",
    price.classify_price_text("折扣券 ¥34").rejected,
    "券价特征:券",
)
check("「十亿豪补 ¥12」被拒", price.classify_price_text("十亿豪补 ¥12").price, None)
check("「立减 ¥15」被拒", price.classify_price_text("立减 ¥15").price, None)
check("「红包抵扣 ¥20」被拒", price.classify_price_text("红包抵扣 ¥20").price, None)
check("「满100减20」被拒", price.classify_price_text("满100减20").price, None)

# 正常房价必须**不被误杀**
check("「¥236起」放行", price.classify_price_text("¥236起").price, 236.0)
check("「¥288」放行", price.classify_price_text("¥288").price, 288.0)
check("「¥1,288」千分位", price.classify_price_text("¥1,288").price, 1288.0)
check("「¥79」放行", price.classify_price_text("¥79").price, 79.0)


# ---------------------------------------------------------------------------
# ② ★ 券价 ≠ 无价(丢弃必须可见 —— 否则无法判断过滤是否过狠)
# ---------------------------------------------------------------------------

section("② 券价 vs 无价 —— 两种 None 必须可区分")

coupon = price.classify_price_text("折扣券 ¥34")
empty = price.classify_price_text("")
unparsable = price.classify_price_text("暂无报价")

check("券价:price=None", coupon.price, None)
check("券价:rejected 非空", bool(coupon.rejected), True)
check("空文本:price=None", empty.price, None)
check("空文本:rejected 为空(不是拒绝,是没价)", empty.rejected, "")
check("不可解析:rejected 为空(页面确实没价)", unparsable.rejected, "")
check("三者的 ok 都是 False", (coupon.ok, empty.ok, unparsable.ok), (False, False, False))


# ---------------------------------------------------------------------------
# ③ ★ price_scope —— 起价不是房价(V73 修订)
# ---------------------------------------------------------------------------

section("③ price_scope:列表页「起价」必须标 from")

check("默认 scope = from", price.classify_price_text("¥236起").scope, "from")
check("显式 exact 透传", price.classify_price_text("¥236", scope="exact").scope, "exact")
check("券价的 scope 仍保留", price.classify_price_text("券 ¥10", scope="exact").scope, "exact")


# ---------------------------------------------------------------------------
# ④ parse_price 的三条守卫(逐字继承旧 prices.py:24-51)
# ---------------------------------------------------------------------------

section("④ parse_price 守卫:范围 / 无符号整段 / 年份排除")

check("低于 5 元不算房价", price.parse_price("¥3"), None)
check("高于 10 万不算房价", price.parse_price("¥200000"), None)
check("取最小值(会员价 vs 原价)", price.parse_price("会员价 ¥236 原价 ¥288"), 236.0)
check("无符号 + 非纯价格文本 → 拒", price.parse_price("2026年开业 房间号 1203"), None)
check("无符号 + 纯价格文本 → 接受", price.parse_price("236"), 236.0)
check("无符号 1000~2999 整数 → 排除(年份区间)", price.parse_price("2026"), None)
check("带符号时不受年份规则影响", price.parse_price("¥2026"), 2026.0)
check("require_symbol=True 时无符号拒", price.parse_price("236", require_symbol=True), None)
check("require_symbol=True 时有符号收", price.parse_price("¥236", require_symbol=True), 236.0)


# ---------------------------------------------------------------------------
# ⑤ ★ 逐条坐标归属(P3)—— 旧 deep_find_coords 的致命缺陷
# ---------------------------------------------------------------------------

section("⑤ 坐标**逐条**归属 —— 旧实现返回扁平列表、无法对齐")

# 携程真接口形状(证据:diag_ctrip_api_no_hotels_20260826_172538.json)
ctrip_row = {
    "base": {"hotelId": "100342325", "hotelName": "莱州锦禾轻奢民宿"},
    "comment": {"score": "4.7", "totalReviews": "267 点评"},
    "position": {"cityId": 3915, "lat": "37.165587", "lng": "119.946814"},
    "seoInfo": {"seoUrl": "https://hotels.ctrip.com/hotels/x"},
}
coords = geo.coords_from_obj(ctrip_row)
check("携程行 → 提取到坐标", coords, (37.165587, 119.946814))

# ★ 关键回归:两条记录各自取到**自己的**坐标(旧实现会拍平成一个列表)
two_rows = [
    {"base": {"hotelName": "A"}, "position": {"lat": "30.0", "lng": "120.0"}},
    {"base": {"hotelName": "B"}, "position": {"lat": "31.0", "lng": "121.0"}},
]
ca = geo.coords_from_obj(two_rows[0])
cb = geo.coords_from_obj(two_rows[1])
check("A 行坐标", ca, (30.0, 120.0))
check("B 行坐标", cb, (31.0, 121.0))
check("两条不串台", ca != cb, True)

check("纬度越界被拒", geo.coords_from_obj({"lat": "999", "lng": "120"}), None)
check("经度越界被拒", geo.coords_from_obj({"lat": "30", "lng": "999"}), None)
check("只有 lat 不算坐标", geo.coords_from_obj({"lat": "30"}), None)
check("无坐标返回 None", geo.coords_from_obj({"name": "x"}), None)


# ---------------------------------------------------------------------------
# ⑥ 距离文本解析 ★ 米制不能丢
# ---------------------------------------------------------------------------

section("⑥ 距离文本:公里 + 米(只认公里会静默丢店)")

check("美团卡片原文(公里)", geo.parse_distance_km("距您查询的酒店直线1.2公里 · 近青城山火车站"), 1.2)
check("km 单位", geo.parse_distance_km("1.5km"), 1.5)
check("KM 大写", geo.parse_distance_km("2.3 KM"), 2.3)
check("★ 米制换算", geo.parse_distance_km("距酒店直线800米"), 0.8)
check("千米", geo.parse_distance_km("直线3千米"), 3.0)
check("无距离文本 → None", geo.parse_distance_km("近青城山火车站"), None)
check("空 → None", geo.parse_distance_km(""), None)
check("零距离 → None(异常值)", geo.parse_distance_km("0公里"), None)

hitted = geo.parse_distance_text("直线1.2公里 · 近公园")
check("parse_distance_text 返回 (公里, 原文)", hitted, (1.2, "1.2公里"))


# ---------------------------------------------------------------------------
# ⑦ haversine(逐字继承旧 geo.py:8-15)
# ---------------------------------------------------------------------------

section("⑦ haversine 距离")

d = geo.haversine_km(37.165587, 119.946814, 37.175587, 119.946814)
# 0.01° 纬度在 WGS84 平均球半径(6371.0088 km)下 = 6371.0088 * 0.01 * pi/180 ≈ 1.112 km
check("纬度差 0.01° ≈ 1.112 km", round(d, 3), 1.112)
check("同一点 = 0", geo.haversine_km(30.0, 120.0, 30.0, 120.0), 0.0)
check("赤道半径常量 6371.0088", geo.EARTH_RADIUS_KM, 6371.0088)


# ---------------------------------------------------------------------------
# ⑧ attach_distances 的优先级与降级标注
# ---------------------------------------------------------------------------

section("⑧ attach_distances:坐标 > 卡片文本 > 城市兜底 > 无")

items = [
    {"hotel_name": "有接口坐标", "coords": (37.175587, 119.946814)},
    {"hotel_name": "只有卡片距离", "distance_text": "直线2.5公里"},
    {"hotel_name": "啥都没有"},
]
geo.attach_distances(items, anchor=(37.165587, 119.946814))
check("① 坐标 → haversine", items[0]["distance_km"], 1.112)
check("① 来源标 api", items[0]["coord_source"], "api")
check("② 卡片文本 → 2.5", items[1]["distance_km"], 2.5)
check("② 来源标 card", items[1]["coord_source"], "card")
check("③ 拿不到 → None", items[2]["distance_km"], None)
check("③ 来源标 none", items[2]["coord_source"], "none")

# 锚点无坐标 + 有城市兜底 → 降级且**必须标出来**
degraded_items = [{"hotel_name": "X", "coords": (31.0, 121.0)}]
geo.attach_distances(degraded_items, anchor=None, city_fallback=(30.0, 120.0))
check("④ 城市兜底算出距离", degraded_items[0]["distance_km"] is not None, True)
check("④ 来源标 city", degraded_items[0]["coord_source"], "city")
check("④ ★ 标 degraded(不许假装精确)", degraded_items[0]["degraded"], True)


# ---------------------------------------------------------------------------
# ⑨ 排序:距离为 None 排最后(旧 runner.py:113 同款语义)
# ---------------------------------------------------------------------------

section("⑨ 按距离排序")

rows = [
    {"hotel_name": "远", "distance_km": 9.0},
    {"hotel_name": "无距离", "distance_km": None},
    {"hotel_name": "近", "distance_km": 0.5},
]
ordered = [r["hotel_name"] for r in geo.sort_by_distance(rows)]
check("升序且 None 垫底", ordered, ["近", "远", "无距离"])


# ---------------------------------------------------------------------------
# ⑩ 酒店名归一化(跨平台去重的基础)
# ---------------------------------------------------------------------------

section("⑩ norm_hotel_name")

check("去后缀:酒店", norm_hotel_name("盛铂仕丹酒店"), "盛铂仕丹")
check("去后缀:民宿", norm_hotel_name("隐欲民宿"), "隐欲")
check("去括号(中文)", norm_hotel_name("静荷民宿(蒙自市政府店)"), "静荷")
check("去括号(英文)", norm_hotel_name("盛铂仕丹酒店(青城山景区高铁站店)"), "盛铂仕丹")
# ★ 第 1 步是 re.sub(r"\s+", "", ...) —— **去掉全部空白**(逐字继承旧 human.py:225),
#   不是 trim。所以 "ABC Hotel" → "abchotel"。
check("去全部空白 + 小写", norm_hotel_name("  ABC Hotel  "), "abchotel")
check("空 → 空串", norm_hotel_name(""), "")


# ---------------------------------------------------------------------------
# ⑪ PriceVerdict 形状
# ---------------------------------------------------------------------------

section("⑪ PriceVerdict")

v = PriceVerdict(236.0, "from", "", "¥236起")
check("PriceVerdict.ok 为 True", v.ok, True)
check("PriceVerdict 是 NamedTuple", hasattr(v, "_fields"), True)


# ---------------------------------------------------------------------------
# ⑫ ★ 跨平台分组(V84 的呈现层)—— 一家店一行
# ---------------------------------------------------------------------------

section("⑫ 跨平台分组:一家店一行,每个平台一个价")


def _q(name: str, price: float | None, plat: str, dist: float | None = 1.0, **kw: object):
    from hoteldata.domains.compare.contract import HotelQuote as _HQ

    return _HQ(
        hotel_name=name, price=price, price_scope="from", distance_km=dist,
        raw={"platform": plat}, **kw,  # type: ignore[arg-type]
    )


from hoteldata.domains.compare.report import (  # noqa: E402
    build_markdown,
    build_price_text,
    group_by_hotel,
)

# 同店两平台:名字带不同门店后缀,归一化后应合并
qs = [
    _q("上青城度假酒店(青城山景区高铁站店)", 304, "ctrip", 0.10),
    _q("上青城度假酒店(青城山高铁站店)", 368, "meituan", 0.10),
    _q("青城山前山景区原石滩酒店", 246, "ctrip", 0.48),
    _q("来住别院酒店", 322, "meituan", 0.10),
]
groups = group_by_hotel(qs)
check("4 条报价 → 3 组(同店跨平台合并)", len(groups), 3)
g0 = groups[0]
check("第一组含两个平台", g0.platforms, ["ctrip", "meituan"])
check("★ 价差 = 368 - 304", g0.spread, 64.0)
check("★ 便宜方 = 携程", g0.cheaper, "ctrip")
check("最低价", g0.min_price, 304.0)
check("最高价", g0.max_price, 368.0)

# 单平台组:没有价差
g1 = groups[1]
check("单平台组 platforms", g1.platforms, ["ctrip"])
check("单平台组无价差(None,不是 0)", g1.spread, None)
check("单平台组无便宜方", g1.cheaper, None)

# 同价:价差 0 → 不标便宜方(spread 返回 None,避免"省 ¥0"噪音)
same = group_by_hotel([
    _q("同价酒店", 200, "ctrip"), _q("同价酒店", 200, "meituan"),
])
check("★ 两平台同价 → spread=None(不写「省 ¥0」)", same[0].spread, None)
check("同价时 cheaper=None", same[0].cheaper, None)

# 距离:取有值的那个
mixed = group_by_hotel([
    _q("混合距离", 100, "ctrip", None), _q("混合距离", 110, "meituan", 0.7),
])
check("距离取有值的一边", mixed[0].distance_km, 0.7)

# ---- markdown 表格 ----
md = build_markdown(
    anchor_name="锚点", query_date=__import__("datetime").date(2026, 10, 1),
    nights=1, city="某市", quotes=qs, slot="2026-10-01-0300",
)
check("md 含「比价总览」表头", "### 比价总览(一家一行)" in md, True)
check("★ md 一行同时含两个平台价", "¥304 起 ⭐ | ¥368 起" in md, True)
check("★ md 含价差列(携程省)", "¥64(携程省)" in md, True)
check("md 保留平台明细段", "### 各平台明细" in md, True)
check("md 一行只有一列的店标 —", "| ¥246 起 | — |" in md, True)

# ---- 推送文本 ----
push = build_price_text(
    anchor_name="锚点", quotes=qs, query_date=__import__("datetime").date(2026, 10, 1),
    slot="2026-10-01-0300",
)
check("★ 推送一行含两个平台价", "携程 ¥304起⭐ / 美团 ¥368起" in push, True)
check("★ 推送含价差与便宜方", "携程省 ¥64" in push, True)
check("推送长名被缩短", "山屿·漫时光·繁花一宿" not in push, True)
check("推送含 ⭐ 图例", "⭐ = 该店两平台里更便宜的一边" in push, True)

# ---- 同价时推送不写「省 ¥0」----
push_same = build_price_text(anchor_name="锚点", quotes=same[0].by_platform.values() and [
    _q("同价酒店", 200, "ctrip"), _q("同价酒店", 200, "meituan"),
])
check("★ 同价时推送不出现「省 ¥0」", "省" not in push_same, True)


# ---------------------------------------------------------------------------

print("\n" + "=" * 60)
print(f"段3 单元自检:{PASS} PASS / {FAIL} FAIL")
if FAILURES:
    print("\n失败明细:")
    for line in FAILURES:
        print("  -", line)
print("=" * 60)
sys.exit(1 if FAIL else 0)
