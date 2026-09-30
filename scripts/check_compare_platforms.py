"""段3 平台解析器自检 —— **用旧系统留下的真实接口响应**驱动(不是手搓样本)。

★ 为什么必须用真响应
====================

段3 对携程的整个设计都建立在「``ctGetNearbyHotelList`` 的响应里有什么」之上。
如果拿手搓的样本测,测的是**我对响应的想象**,不是响应本身 ——
旧系统正是栽在"以为接口里有数值型 price"这个想象上
(``ctrip.py:82-108`` 的 ``_scan_hotel_entries`` 要求 ≥2 个数值型 price 键,
而真响应里没有 → 整条捕获链路判死)。

所以本脚本直接读旧系统留下的两份真实 diag:

* ``diag_ctrip_api_no_hotels_20260826_172538.json``(含完整 ``hotelList[]``)

用法::

    .venv\\Scripts\\python.exe scripts\\check_compare_platforms.py
    .venv\\Scripts\\python.exe scripts\\check_compare_platforms.py --old-dir <旧系统根目录>
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# ★ 不要 `sys.stdout = io.TextIOWrapper(sys.stdout.buffer, ...)`:那个 wrapper 被 GC 时
#   会**关掉底层的 buffer**,于是本模块一旦被二次导入(验收器/测试里很常见)
#   后续任何 print 都抛 `ValueError: I/O operation on closed file`。
#   `reconfigure` 就地改编码,不新建对象、不接管 buffer。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except (AttributeError, ValueError):  # pragma: no cover - 非 TTY/已重定向
    pass

from hoteldata.domains.compare import load_platforms  # noqa: E402
from hoteldata.domains.compare.platforms import ctrip as ctrip_mod  # noqa: E402
from hoteldata.domains.compare.platforms import meituan as meituan_mod  # noqa: E402
from hoteldata.domains.compare.registry import create_platform  # noqa: E402

DEFAULT_OLD = Path(r"D:\AAAAaaaa\Pythooooooooooooon\hotel-data-system")

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


def load_real_ctrip_payload(old_dir: Path, *, debug: bool = False) -> dict | None:
    """从旧系统 diag 里取出真接口的 JSON。

    ★ 实测结论(脚本会打印):预览**恰好被截在 4000 字符**,不是合法 JSON。
      这本身就是段3 P10 的证据 —— 旧系统的诊断只存预览,
      **考古时无法判断完整响应里到底有没有价格字段**。

    所以这里两步走:

    1. 先试完整解析;
    2. 失败则 :func:`salvage_hotel_list` —— 用括号配平扫描打捞**完整**的酒店对象。
       足以验证字段路径(``position.lat/lng``、``comment.score`` …)是否正确。
    """
    diag = old_dir / "data" / "hotel_reports"
    if not diag.exists():
        if debug:
            print(f"  [debug] diag 目录不存在:{diag}")
        return None
    best: str | None = None
    files = sorted(diag.glob("diag_ctrip_api_no_hotels_*.json"))
    if debug:
        print(f"  [debug] diag 目录 {diag}")
        print(f"  [debug] 匹配到 {len(files)} 个 diag 文件")
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            if debug:
                print(f"  [debug] {path.name} 读取失败:{exc}")
            continue
        if not isinstance(data, list):
            continue
        for cap in data:
            if not isinstance(cap, dict):
                continue
            if "ctGetNearbyHotelList" not in (cap.get("url") or ""):
                continue
            preview = cap.get("body_preview")
            if not preview:
                continue
            if best is None or len(preview) > len(best):
                best = preview
    if best is None:
        if debug:
            print("  [debug] 没有任何 diag 含 ctGetNearbyHotelList 的 body_preview")
        return None
    if debug:
        print(f"  [debug] 取到最长预览 {len(best)} 字符")
    try:
        payload = json.loads(best)
        if debug:
            print(f"  [debug] 完整解析成功,hotelList={len(hotel_list_of(payload))} 家")
        return payload
    except json.JSONDecodeError as exc:
        if debug:
            print(f"  [debug] 预览被截断、不是完整 JSON:{exc}")
            print("  [debug] → 这正是段3 P10 的证据:旧诊断只存 4000 字预览,")
            print("             无法据此判断完整响应里有没有价格字段。改用括号配平打捞。")
    items = salvage_hotel_list(best)
    if debug:
        print(f"  [debug] 打捞出 {len(items)} 条完整酒店对象")
    if not items:
        return None
    return {"data": {"hotelList": items}, "_salvaged": True}


def hotel_list_of(payload: dict) -> list:
    """安全取 ``data.hotelList``。"""
    return (payload.get("data") or {}).get("hotelList") or []


def salvage_hotel_list(text: str) -> list[dict]:
    """从**被截断**的 JSON 文本里打捞酒店条目的字段。

    实测:4000 字预览**在第一条酒店对象内部就被切断**,所以没有任何 ``{...}``
    完整闭合(打捞完整对象得 0 条)。但**坐标字段出现在切断点之前**
    (``base`` → ``comment`` → ``position.lat/lng``,实测位置靠前),
    所以字段级打捞能拿到真数据 —— 足以验证段3 的字段路径是否正确。

    做法:以 ``{"base":{"hotelId"`` 为条目起点切分,对每段用正则抓字段。
    """
    out: list[dict] = []
    # 条目起点 = 含 hotelId 的 base 对象
    starts = [m.start() for m in re.finditer(r'\{\s*"base"\s*:\s*\{\s*"hotelId"', text)]
    if not starts:
        return out
    bounds = [*starts, len(text)]

    def grab(segment: str, key: str) -> str | None:
        m = re.search(rf'"{key}"\s*:\s*"([^"]*)"', segment)
        return m.group(1) if m else None

    for i in range(len(starts)):
        seg = text[bounds[i] : bounds[i + 1]]
        hotel_id = grab(seg, "hotelId")
        name = grab(seg, "hotelName")
        if not hotel_id or not name:
            continue
        item: dict = {"base": {"hotelId": hotel_id, "hotelName": name}}
        score = grab(seg, "score")
        reviews = grab(seg, "totalReviews")
        if score or reviews:
            item["comment"] = {}
            if score:
                item["comment"]["score"] = score
            if reviews:
                item["comment"]["totalReviews"] = reviews
        # ★ 坐标:position 块里的 lat/lng(实测在切断点之前)
        pos: dict = {}
        for key in ("lat", "lng", "positionDescOfCtrip", "positionDesc"):
            val = grab(seg, key)
            if val is not None:
                pos[key] = val
        if "lat" in pos or "lng" in pos:
            item["position"] = pos
        seo = grab(seg, "seoUrl")
        if seo:
            item["seoInfo"] = {"seoUrl": seo}
        out.append(item)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--old-dir", default=str(DEFAULT_OLD))
    args = parser.parse_args()
    old_dir = Path(args.old_dir)

    # ------------------------------------------------------------------
    section("① 声明式注册表(V62)")
    # ------------------------------------------------------------------
    names = load_platforms()
    check("两个平台都已注册", set(names) >= {"ctrip", "meituan"}, True)
    check("available_platforms 排序稳定", names, tuple(sorted(names)))

    ctrip = create_platform("ctrip")
    meituan = create_platform("meituan")
    check("携程实例 name", ctrip.name, "ctrip")
    check("美团实例 name", meituan.name, "meituan")

    # ★ V61:真契约 —— 三个方法都在(不是 hasattr 探测那个未声明的方法)
    check("携程有 resolve_anchor", callable(getattr(ctrip, "resolve_anchor", None)), True)
    check("携程有 collect_quotes", callable(getattr(ctrip, "collect_quotes", None)), True)
    check("美团有 resolve_anchor", callable(getattr(meituan, "resolve_anchor", None)), True)
    check("美团有 collect_quotes", callable(getattr(meituan, "collect_quotes", None)), True)
    # ★ 旧系统的假契约 collect_map_prices **不应存在**
    check("旧假契约 collect_map_prices 已不存在", hasattr(ctrip, "collect_map_prices"), False)
    check("旧死模板 collect 已不存在", hasattr(ctrip, "collect"), False)

    # ★ 未注册平台抛错(不静默返回 None)
    try:
        create_platform("ctrp")
        check("未知平台抛错", "no-raise", "PlatformNotRegistered")
    except Exception as exc:  # noqa: BLE001
        check("未知平台抛错", type(exc).__name__, "PlatformNotRegistered")

    # ------------------------------------------------------------------
    section("② ★ 携程真接口解析(用旧系统留下的真实响应)")
    # ------------------------------------------------------------------
    payload = load_real_ctrip_payload(old_dir, debug=True)
    if payload is None:
        print(f"  [SKIP] 未找到可解析的真实 diag(旧系统路径:{old_dir})")
    else:
        hotel_list = (payload.get("data") or {}).get("hotelList") or []
        print(f"  真实响应:hotelList 共 {len(hotel_list)} 家")
        check("真实响应里能取到 data.hotelList", len(hotel_list) > 0, True)

        quotes = ctrip_mod._quotes_from_api(payload)
        check("解析出酒店条目", len(quotes) > 0, True)

        first = quotes[0] if quotes else None
        if first is not None:
            print(f"  首条:{first.hotel_name} | id={first.hotel_id} | "
                  f"score={first.score} | reviews={first.reviews}")
            print(f"        coords={first.coords} ({first.coord_source}) | "
                  f"distance={first.distance_km} | price={first.price} ({first.price_source})")
            print(f"        url={(first.url or '')[:80]}")

            check("★ 酒店名非空", bool(first.hotel_name), True)
            check("★ 坐标提取成功(D13 的修复点)", first.coords is not None, True)
            check("★ 坐标来源标 api", first.coord_source, "api")
            check("★ 坐标是字符串转的数字", isinstance((first.coords or (0,))[0], float), True)
            check("★ 评分从字符串 '4.7' 解析", first.score, 4.7)
            check("★ 点评数从 '267 点评' 解析", first.reviews, 267)
            check("★ 详情页 URL 取自 seoInfo.seoUrl", "hotels.ctrip.com" in (first.url or ""), True)
            check("★ 列表页价一律标 from", first.price_scope, "from")
            # 位置描述里有「直线1公里」→ 应解析出 1.0
            check("★ 从 positionDescOfCtrip 解析距离", first.distance_km, 1.0)

        # ★ 全部条目的坐标都应可拿到(实测响应里 position.lat/lng 每家都有)
        with_coords = sum(1 for q in quotes if q.coords is not None)
        check("★ 所有条目都有坐标", with_coords, len(quotes))

        # ★ 逐条不串台:两条的坐标必须不同
        if len(quotes) >= 2:
            check("★ 相邻两条坐标不串台", quotes[0].coords != quotes[1].coords, True)

    # ------------------------------------------------------------------
    section("③ 接口条目里的价格情况(P1 的核心事实)")
    # ------------------------------------------------------------------
    if payload is not None:
        hotel_list = (payload.get("data") or {}).get("hotelList") or []
        priced = sum(1 for q in ctrip_mod._quotes_from_api(payload) if q.price is not None)
        print(f"  接口条目数={len(hotel_list)},段3 解析出价格数={priced}")
        print("  → 若为 0,说明【接口不带价格】,取价必须落 DOM 通道;")
        print("    这正是计划书 §5.6「接口命中则零点击取价」不成立的实测依据。")
        # 这里**不断言**具体数字(响应被截断过,事实可能随平台变化),
        # 只记录事实并断言"解析器不会因此崩"
        check("接口解析不因缺价格而抛异常", isinstance(priced, int), True)
        check("接口至少给出坐标(取价的替代价值)", all(q.coords for q in ctrip_mod._quotes_from_api(payload)), True)

    # ------------------------------------------------------------------
    section("④ 美团卡片解析(用实拍截图确证过的文本形状)")
    # ------------------------------------------------------------------
    # 文本取自实拍截图 meituan_list_20260826_173000.png 的真实可见内容
    cards = [
        {
            "name": "山屿·漫时光·繁花一宿·Floral Atelier無璞·轻奢度假民宿(青城山高铁站店)",
            "score": "4.9分",
            "feedback": "落地大窗随时可以欣赏远处的大山 5000+消费",
            "address": "距您查询的酒店直线1.2公里 · 近青城山火车站 · 青城山镇及附近地区",
            "priceNum": "132",
            "origin": "",
            "raw": "¥132起",
        },
        {
            "name": "悟栖·Haven智慧酒店(青城山高铁站店)",
            "score": "5.0分",
            "feedback": "预定客人可联系前台免费接送 1000+消费",
            "address": "距您查询的酒店直线1.1公里 · 近青城山火车站",
            "priceNum": "158",
            "origin": "",
            "raw": "¥158起",
        },
        {
            "name": "青城草堂度假酒店(青城山店)",
            "score": "5.0分",
            "feedback": "超大机麻棋牌室娱乐 500+消费",
            "address": "距您查询的酒店直线1.2公里",
            "priceNum": "118",
            "origin": "门市价¥168",
            "raw": "¥118起 门市价¥168",
        },
    ]
    notes: list[str] = []
    quotes, anchor_hits = meituan_mod._cards_to_quotes(cards, "盛铂仕丹酒店(青城山景区高铁站店)", notes)
    check("解析出 3 张卡片", len(quotes), 3)
    check("★ 距离从 .poi-address 解析出来(D13 修复点)", quotes[0].distance_km, 1.2)
    check("★ 第二条距离", quotes[1].distance_km, 1.1)
    check("★ 价格解析", quotes[0].price, 132.0)
    check("★ 起价标 from", quotes[0].price_scope, "from")
    check("★ 评分从 '4.9分' 解析", quotes[0].score, 4.9)
    check("★ 来源标 dom", quotes[0].price_source, "dom")
    check("锚点名与卡片名不同前缀 → 0 命中", anchor_hits, 0)

    # ★ 券价必须被拒(旧系统 12/18 条翻车点)
    coupon_cards = [
        {
            "name": "某某酒店",
            "score": "4.0分",
            "feedback": "",
            "address": "直线0.5公里",
            "priceNum": "",
            "origin": "",
            "raw": "十亿豪补 ¥12",
        }
    ]
    cnotes: list[str] = []
    cq, _ = meituan_mod._cards_to_quotes(coupon_cards, "锚点", cnotes)
    check("券价卡片:价格为空", cq[0].price if cq else None, None)
    check("★ 券价被登记进 price_rejected", bool(cq and cq[0].price_rejected), True)

    # ------------------------------------------------------------------
    section("⑤ 锚点前 4 字匹配(实测隐患)")
    # ------------------------------------------------------------------
    a_cards = [
        {"name": "隐欲民宿·山海别院", "score": "", "feedback": "", "address": "", "priceNum": "288", "origin": "", "raw": ""},
        {"name": "隐欲民宿(总店)", "score": "", "feedback": "", "address": "", "priceNum": "279", "origin": "", "raw": ""},
    ]
    anotes: list[str] = []
    aq, ahits = meituan_mod._cards_to_quotes(a_cards, "隐欲民宿", anotes)
    print(f"  锚点「隐欲民宿」前 4 字 = {meituan_mod._prefix('隐欲民宿')!r}")
    print(f"  候选前缀:{[meituan_mod._prefix(c['name']) for c in a_cards]}")
    check("★ 前 4 字相同 → 命中 2 张(真实隐患,不是假想)", ahits, 2)
    check("★ 命中多个时被记入 notes", any("匹配到" in n for n in anotes), True)

    # ------------------------------------------------------------------
    section("⑥ URL 构造(参数名逐字继承)")
    # ------------------------------------------------------------------
    u = ctrip_mod._build_detail_url("127826116", "2026-10-01", "2026-10-02")
    check("携程详情页参数 hotelId", "hotelId=127826116" in u, True)
    check("携程详情页参数 checkin", "checkin=2026-10-01" in u, True)
    lu = ctrip_mod._build_list_url("隐欲民宿", "529", "2026-10-01", "2026-10-02")
    check("携程列表页参数 city", "city=529" in lu, True)
    check("携程列表页参数 keyword", "keyword=" in lu, True)

    mu = meituan_mod._build_list_url("隐欲民宿", "59", "2026-10-01", "2026-10-02")
    check("美团 list.html 参数 cityId", "cityId=59" in mu, True)
    check("美团 list.html 参数 checkIn", "checkIn=2026-10-01" in mu, True)
    check("美团 list.html 参数 checkOut", "checkOut=2026-10-02" in mu, True)
    check("美团登录 URL 带 backurl", "backurl=" in meituan_mod.login_url(), True)

    # ------------------------------------------------------------------
    section("⑦ 美团卡片脚本生成(选择器单一来源)")
    # ------------------------------------------------------------------
    script = meituan_mod.meituan_card_script(6)
    check("脚本包含卡片选择器", "a.poi" in script, True)
    check("脚本包含价格选择器", "poi-price-num" in script, True)
    check("脚本包含 slice 上限", "slice(0, 6)" in script, True)

    print("\n" + "=" * 60)
    print(f"段3 平台自检:{PASS} PASS / {FAIL} FAIL")
    if FAILURES:
        print("\n失败明细:")
        for line in FAILURES:
            print("  -", line)
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
