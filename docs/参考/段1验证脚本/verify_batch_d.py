"""批次 D 自检脚本(**临时,不属于交付物** —— 放在 _scratch/ 下,不进 src/)。

覆盖四块:
  1. 四张表的 DDL(CreateTable 打印)+ 列数 + 关键口径注释;
  2. ``json_get`` / ``parse_addtime`` / ``review_id_fallback`` 在给定样例上的取值;
  3. 三个提取器的**端到端**冒烟(假 HTTP + 假 ctx):allow_missing、value=''vs 0、
     hot_calendar 分组、available 判定、降级不写库、4 类素材;
  4. ``CollectRepository`` 批次 D 方法的**真实 SQL**(编译后断言 SET 子句,
     验证「不回溯」两条机制与整批替换的 DELETE+INSERT)。

运行:``.venv\\Scripts\\python.exe _scratch\\verify_batch_d.py``
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402
from sqlalchemy.dialects import postgresql  # noqa: E402
from sqlalchemy.schema import CreateTable  # noqa: E402

from hoteldata.domains.collect import portal as portal_mod  # noqa: E402
from hoteldata.domains.collect import review as review_mod  # noqa: E402
from hoteldata.domains.collect import room as room_mod  # noqa: E402
from hoteldata.domains.collect.repository import CollectRepository  # noqa: E402
from hoteldata.infra.http import HttpAttempt  # noqa: E402
from hoteldata.infra.models import (  # noqa: E402
    AlertPortalColumn,
    AlertRoomState,
    Base,
    ReviewMaterial,
    ReviewReview,
)

FAILURES: list[str] = []
CHECKS = 0


def check(name: str, ok: bool, extra: Any = "") -> None:
    global CHECKS
    CHECKS += 1
    mark = "PASS" if ok else "FAIL"
    if not ok:
        FAILURES.append(name)
    print(f"[{mark}] {name}" + (f"  -> {extra}" if extra != "" else ""))


def section(title: str) -> None:
    print(f"\n===== {title} =====")


# ---------------------------------------------------------------------------
# ① 模型 / DDL
# ---------------------------------------------------------------------------

PG = postgresql.dialect()


def test_models() -> None:
    section("① 模型与 DDL")
    tables = sorted(Base.metadata.tables)
    check("Base.metadata 共 11 张表(7+4)", len(tables) == 11, tables)
    for name in (
        "alert_portal_columns",
        "alert_room_states",
        "review_reviews",
        "review_materials",
    ):
        check(f"新增表 {name} 在 metadata 中", name in tables)

    counts = {
        "alert_portal_columns": 13,
        "alert_room_states": 13,
        "review_reviews": 11,
        "review_materials": 10,
    }
    for table, expected in counts.items():
        actual = len(Base.metadata.tables[table].c)
        check(f"{table} 列数 == {expected}", actual == expected, f"实际 {actual}")

    for table in counts:
        ddl = str(CreateTable(Base.metadata.tables[table]).compile(dialect=PG))
        print(f"\n----- DDL: {table} -----\n{ddl.strip()}\n")

    avail = AlertRoomState.__table__.c.available.comment or ""
    check(
        "available 列注释带「售完仍可订」口径",
        "available=1 当且仅当 roomStatus=='G'" in avail and "canUsedQuantity=0" in avail,
        avail,
    )
    kind = ReviewMaterial.__table__.c.kind.comment or ""
    check("review_materials.kind 注释写 4 类(含 num)", "num" in kind, kind)
    check(
        "review_reviews.replied 注释声明「采集 UPSERT 永不写本列」",
        "永不写" in (ReviewReview.__table__.c.replied.comment or ""),
    )
    check(
        "唯一键:alert_portal_columns 四元组",
        {c.name for c in AlertPortalColumn.__table__.constraints if c.name and "uq" in c.name}
        == {"uq_alert_portal_columns_key"},
    )
    room_uniques = [c.name for c in AlertRoomState.__table__.constraints if c.name and "uq" in c.name]
    check("alert_room_states 无唯一约束(整批替换语义)", not room_uniques, room_uniques)
    check(
        "alert_room_states 有 (hotel_id, collect_date) 索引",
        [tuple(i.columns.keys()) for i in AlertRoomState.__table__.indexes]
        == [("hotel_id", "collect_date")],
    )


# ---------------------------------------------------------------------------
# ② 纯函数样例
# ---------------------------------------------------------------------------

ADDTIME = "/Date(1756224000000+0800)/"


def test_pure() -> None:
    section("② json_get / parse_addtime / 指纹")
    from hoteldata.domains.collect.jsonpath import json_get

    check("json_get data.[0].tip1 == 3", json_get({"data": [{"tip1": 3}]}, "data.[0].tip1") == 3)
    check("json_get data[0].tip1 is None(旧坑)", json_get({"data": [{"tip1": 3}]}, "data[0].tip1") is None)
    check("json_get(None, ...) is None(allow_missing 下游)", json_get(None, "data.minPrice") is None)
    check("json_get 两层路径", json_get({"data": {"minPrice": 460}}, "data.minPrice") == 460)
    check(
        "json_get list 路径",
        json_get({"commentlist": [1, 2]}, "commentlist") == [1, 2],
    )

    dt = review_mod.parse_addtime(ADDTIME)
    expected_iso = "2025-08-27T00:00:00+08:00"
    check(
        f"parse_addtime({ADDTIME}) == 2025-08-27 00:00:00 Asia/Shanghai",
        dt is not None and dt.isoformat() == expected_iso,
        dt.isoformat() if dt else None,
    )
    check(
        "同一毫秒在 UTC 表示下 == 2025-08-26T16:00:00+00:00(时区坑已修)",
        dt is not None and dt.astimezone(timezone.utc).isoformat() == "2025-08-26T16:00:00+00:00",
        dt.astimezone(timezone.utc).isoformat() if dt else None,
    )
    check("parse_addtime(None) is None", review_mod.parse_addtime(None) is None)
    check(
        "parse_addtime 非 /Date/ 串 → None(旧实现存原文,新列 timestamptz)",
        review_mod.parse_addtime("2026-08-24 11:13:13") is None,
    )
    check(
        "parse_addtime search(前缀噪声也命中)",
        review_mod.parse_addtime("x/Date(1756224000000)/y") is not None,
    )

    fp = review_mod.review_id_fallback(4, "张三", "很好", ADDTIME)
    raw = "|".join(str(v or "") for v in (4, "张三", "很好", ADDTIME))
    manual = "h" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    check("指纹 == h+sha1(...)[:16](手算一致)", fp == manual, fp)
    check("指纹 17 字符", len(fp) == 17 and fp.startswith("h"), len(fp))
    check(
        "指纹顺序敏感(换序即变 id)",
        review_mod.review_id_fallback("张三", 4, "很好", ADDTIME) != fp,
    )
    check("指纹幂等", review_mod.review_id_fallback(4, "张三", "很好", ADDTIME) == fp)
    check(
        "指纹 comment_time 传原始 addtime 串(≠解析结果)",
        review_mod.review_id_fallback(4, "张三", "很好", "2025-08-27 00:00:00") != fp,
    )

    check("sentiment 5→good", review_mod.classify_sentiment(5) == "good")
    check("sentiment 4→good", review_mod.classify_sentiment(4) == "good")
    check("sentiment 3→bad", review_mod.classify_sentiment(3) == "bad")
    check("sentiment None→unknown", review_mod.classify_sentiment(None) == "unknown")
    check("sentiment「4.2」→good(int(float))", review_mod.classify_sentiment(int(float("4.2"))) == "good")

    check("_to_str(None) == ''(渠道源)", portal_mod._to_str(None) == "")
    check("_to_str(4.14375) == '4.14375'", portal_mod._to_str(4.14375) == "4.14375")
    errs: list[str] = []
    check("_to_int(None) == 0(首页待办源)", portal_mod._to_int(None, errors=errs, column="x") == 0)
    check("两源缺失归一化相反(不可统一)", portal_mod._to_str(None) != portal_mod._to_int(None, errors=errs, column="x"))
    check(
        "_index_type_avg_comp 取 indexType=='12' 的 avgComp(str 双向比较)",
        portal_mod._index_type_avg_comp(
            {"dataList": [{"indexType": 11, "avgComp": 9.9}, {"indexType": "12", "avgComp": 4.14375}]},
            12,
        )
        == 4.14375,
    )
    check(
        "_index_type_avg_comp 取不到 → None",
        portal_mod._index_type_avg_comp(None, 12) is None,
    )
    soa = portal_mod._SoaBody.build("/home")
    check("SOA 包裹体 reqHead/head 同层", "reqHead" in soa and "head" in soa)
    check("SOA 键名是 protocal(平台原文)", soa["reqHead"]["protocal"] == "https:")
    check("SOA pathName 参数化", soa["reqHead"]["pathName"] == "/home")
    try:
        portal_mod._guard_placeholders("t", {"a": "{foo}"})
        placeholders_ok = False
    except portal_mod.PlaceholderError:
        placeholders_ok = True
    check("占位符守卫命中 → PlaceholderError", placeholders_ok)
    portal_mod._guard_placeholders("t", {"a": "2026-08-26"})
    check("干净 body 不误报", True)


# ---------------------------------------------------------------------------
# ③ 三个提取器的端到端冒烟
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _quota():
    yield


class FakeLimiter:
    def __init__(self) -> None:
        self.calls = 0

    def request(self, platform: str, account: str) -> Any:
        self.calls += 1
        return _quota()


class FakeSession:
    platform = "ctrip"
    role = "ebooking"
    alias = "ctrip1"

    def cookie_header(self) -> str:
        return "_bfa=1; cticket=2"


class FakeHttp:
    """按 URL 子串路由的假 HTTP;记录每次请求(头/体)。"""

    def __init__(self, routes: list[tuple[str, tuple[int, Any]]]) -> None:
        self.routes = routes
        self.calls: list[dict[str, Any]] = []

    def _match(self, url: str) -> tuple[int, Any]:
        for key, payload in self.routes:
            if key in url:
                return payload
        return 404, {"rcode": 404, "msg": "no route"}

    async def request(self, method: str, url: str, **kw: Any) -> HttpAttempt:
        self.calls.append({"method": method, "url": url, **kw})
        status, body = self._match(url)
        resp = httpx.Response(status, json=body)
        return HttpAttempt(
            url=url,
            method=method,
            status_code=status,
            headers={},
            text=resp.text,
            response=resp,
            attempts=1,
        )


def make_ctx(http: FakeHttp, day: date = date(2026, 8, 26)) -> Any:
    return SimpleNamespace(
        hotel=SimpleNamespace(id=3, name="测试酒店", city=None, ebk_hotel_id=None),
        account=SimpleNamespace(id=2, alias="ctrip1", platform="ctrip", is_multi=False),
        collect_date=day,
        session=FakeSession(),
        limiter=FakeLimiter(),
        layout=None,
        http=http,
        api_timeout_s=20.0,
        persist_raw=False,
        hotel_id=3,
        account_id=2,
        platform="ctrip",
    )


def portal_routes(
    fail_visitor: bool = False, fail_min_price: bool = False
) -> list[tuple[str, tuple[int, Any]]]:
    return [
        ("fetchVisitorTitleV2", (500, {"err": 1}) if fail_visitor else
         (200, {"visitorTotal": 120, "competitorAvgNumber": 88,
                "qunarVisitorTotal": 30, "qunarCompetitorAvgNumber": 25})),
        ("queryHotelMinPriceV1", (500, {"err": 1}) if fail_min_price else
         (200, {"data": {"minPrice": 460, "minPriceRank": 3, "competitorHotelTotal": 12}})),
        ("comment/getCommentsScoreV2", (200, {"data": {"ctripRatingall": 4.2,
                                                       "ctripRatingAllRanking": 6,
                                                       "qunarRatingall": 4.0,
                                                       "qunarRatingAllRanking": 9,
                                                       "competitorHotelTotal": 7}})),
        ("getServiceData", (200, {"dataList": [{"indexType": 11, "avgComp": 9.9},
                                               {"indexType": "12", "avgComp": 4.14375}]})),
        ("getCommentAndFAQNeedFeedBackCount", (200, {"commentAndFAQNeedFeedBackCount": {
            "commentNeedFeedBackCount": 4, "hotelFAQNeedFeedBackCount": 2}})),
        ("queryPendingHotelGrowthTaskListV2", (200, {"count": 5})),
        ("getHotelHotEvent", (200, {"data": [
            {"holiDate": "2026-09-26", "holiName": "中秋节", "realHoliday": True, "holiday": True},
            {"holiDate": "2026-09-25", "holiName": "中秋节", "realHoliday": False, "holiday": True},
            {"holiDate": "2026-10-01", "holiName": "国庆节", "realHoliday": True, "holiday": True},
            {"holiName": "无日期节", "holiday": False},
        ]})),
    ]


def _by_page(results: list[Any]) -> dict[str, Any]:
    return {r.target.page: r for r in results if r.target is not None}


async def test_portal() -> None:
    section("③-1 PortalExtractor")
    http = FakeHttp(portal_routes())
    ctx = make_ctx(http)
    lookup_calls: list[tuple[str, str | None]] = []

    def lookup(module: str, window: str | None) -> dict[str, Any] | None:
        lookup_calls.append((module, window))
        if module == "审核记录":
            return {"payload": {"公示内容": "有内容"}}
        if module == "违约看板/违规中心":
            return {"payload": {"违约记录数": 3}}
        return None

    results = await portal_mod.PortalExtractor().extract(ctx, latest_module=lookup)
    by_page = _by_page(results)
    check("返回 4 条(每 page 一条)", len(results) == 4 and set(by_page) ==
          {"channel_ctrip", "channel_qunar", "home_pending", "hot_calendar"}, sorted(by_page))
    check("全部 status == ok", all(r.status == "ok" for r in results),
          [r.status for r in results])
    check("channel='api'", all(r.channel == "api" for r in results))

    ctrip = {r["column_name"]: r["value"] for r in by_page["channel_ctrip"].records}
    qunar = {r["column_name"]: r["value"] for r in by_page["channel_qunar"].records}
    check("channel_ctrip 8 列", len(ctrip) == 8, sorted(ctrip))
    check("channel_qunar 6 列", len(qunar) == 6, sorted(qunar))
    check("value 一律 str(落 TEXT)", all(isinstance(v, str) for v in ctrip.values()))
    check("visitor_total == '120'", ctrip["visitor_total"] == "120", ctrip["visitor_total"])
    check("min_price == '460'(int→str)", ctrip["min_price"] == "460", ctrip["min_price"])
    check("rating_avg == '4.14375'(取 avgComp 非 val)", ctrip["rating_avg"] == "4.14375",
          ctrip["rating_avg"])
    check("携程 competitor_total 来自 queryHotelMinPriceV1", ctrip["competitor_total"] == "12")
    check("去哪儿 competitor_total 来自 getCommentsScoreV2(不同接口)",
          qunar["competitor_total"] == "7", qunar["competitor_total"])
    check("去哪儿 rating_avg 与携程同值", qunar["rating_avg"] == ctrip["rating_avg"])
    qnote = [r["detail_json"].get("note") for r in by_page["channel_qunar"].records
             if r["column_name"] == "rating_avg"][0]
    check("去哪儿 rating_avg 带 detail.note", isinstance(qnote, str) and "复用" in qnote, qnote)
    check("detail.source_api 齐备",
          all(r["detail_json"].get("source_api") for r in by_page["channel_ctrip"].records))

    home = {r["column_name"]: r["value"] for r in by_page["home_pending"].records}
    check("home_pending 5 列", len(home) == 5, sorted(home))
    check("数值列落字符串 '0' 形式", home["comment_pending"] == "4", home["comment_pending"])
    check("qa_pending == '2'", home["qa_pending"] == "2")
    check("todo_more == '5'", home["todo_more"] == "5")
    check("audit_pending 由模块记录派生 == '1'", home["audit_pending"] == "1", home["audit_pending"])
    check("violation_pending == '3'", home["violation_pending"] == "3")
    check("模块派生走两级回退(先带 window)", ("审核记录", "昨日") in lookup_calls, lookup_calls[:4])

    cal = {r["column_name"]: r for r in by_page["hot_calendar"].records}
    check("hot_calendar 2 行(无日期组被跳过)", len(cal) == 2, sorted(cal))
    check("中秋节 value = min(holiDate) = 2026-09-25", cal["中秋节"]["value"] == "2026-09-25",
          cal["中秋节"]["value"])
    d = cal["中秋节"]["detail_json"]
    check("detail 四键无 source_api",
          set(d) == {"end_date", "lead_days", "holiday", "real_holiday"}, sorted(d))
    check("end_date = max(holiDate) = 2026-09-26", d["end_date"] == "2026-09-26")
    check("lead_days = 30(相对采集日)", d["lead_days"] == 30, d["lead_days"])
    check("组元数据取首个事件(realHoliday=True 被保留)", d["real_holiday"] is True)
    check("holiday 经 bool() 强转", d["holiday"] is True)
    check("国庆节 value == '2026-10-01'", cal["国庆节"]["value"] == "2026-10-01")

    get_call = [c for c in http.calls if c["method"] == "GET"][0]
    check("热点日历是 GET", "getHotelHotEvent" in get_call["url"])
    check("GET 带 startDate=today & endDate=+60",
          "startDate=2026-08-26" in get_call["url"] and "endDate=2026-10-25" in get_call["url"],
          get_call["url"])
    check("GET 不带 content-type", "content-type" not in get_call["headers"])
    post_call = [c for c in http.calls if c["method"] == "POST"][0]
    check("POST 带 content-type: application/json",
          post_call["headers"]["content-type"] == "application/json")
    check("请求头含 cookie/user-agent/referer/x-requested-with",
          {"cookie", "user-agent", "referer", "x-requested-with"} <= set(post_call["headers"]))
    check("限频被调用(每次真实请求)", ctx.limiter.calls == len(http.calls),
          f"{ctx.limiter.calls}/{len(http.calls)}")

    # ---- allow_missing:单接口失败只记 error,其余照落 ----
    http2 = FakeHttp(portal_routes(fail_visitor=True))
    res2 = _by_page(await portal_mod.PortalExtractor().extract(
        make_ctx(http2), latest_module=lambda m, w: None))
    ctrip2 = res2["channel_ctrip"]
    vals2 = {r["column_name"]: r["value"] for r in ctrip2.records}
    check("单接口失败 → 本源 degraded", ctrip2.status == "degraded", ctrip2.status)
    check("失败源仍落满 8 行(不中断)", len(ctrip2.records) == 8)
    check("缺字段落 value=''(不是 0/NULL)", vals2["visitor_total"] == "", repr(vals2["visitor_total"]))
    check("error 串带接口名", "fetchVisitorTitleV2" in (ctrip2.error or ""), ctrip2.error)
    check("visitor 两页共用 → 去哪儿同源也 degraded(而非被中断)",
          res2["channel_qunar"].status == "degraded" and len(res2["channel_qunar"].records) == 6)
    check("无关源不受影响(home_pending / hot_calendar 仍 ok)",
          res2["home_pending"].status == "ok" and res2["hot_calendar"].status == "ok")

    # ---- 只影响携程的接口失败 → 去哪儿源照常 ok(源间隔离) ----
    http2b = FakeHttp(portal_routes(fail_min_price=True))
    res2b = _by_page(await portal_mod.PortalExtractor().extract(
        make_ctx(http2b), latest_module=lambda m, w: None))
    check("queryHotelMinPriceV1 失败 → channel_ctrip degraded", res2b["channel_ctrip"].status == "degraded")
    vals2b = {r["column_name"]: r["value"] for r in res2b["channel_ctrip"].records}
    check("min_price 缺字段落 ''", vals2b["min_price"] == "")
    check("携程 competitor_total 缺字段落 ''", vals2b["competitor_total"] == "")
    check("★ channel_qunar 不受影响(competitor_total 来自另一个接口)",
          res2b["channel_qunar"].status == "ok"
          and {r["column_name"]: r["value"] for r in res2b["channel_qunar"].records}["competitor_total"]
          == "7")

    # ---- 未注入 latest_module → 派生列落 0 且记 error ----
    http3 = FakeHttp(portal_routes())
    res3 = _by_page(await portal_mod.PortalExtractor().extract(make_ctx(http3)))
    home3 = {r["column_name"]: r["value"] for r in res3["home_pending"].records}
    check("未注入 lookup → 派生列落 0", home3["audit_pending"] == "0")
    check("未注入 lookup → 本源 degraded + 记 error",
          res3["home_pending"].status == "degraded"
          and "audit_pending 读取失败" in (res3["home_pending"].error or ""))


async def test_room() -> None:
    section("③-2 RoomExtractor")
    routes = [
        ("getRcProductList", (200, {"data": [{"roomInfos": [
            {"hotelID": "h1", "roomTypeID": "2259669216",
             "roomNameDesc": "露台三床套房（一室一厅+观景阳台）&amp;A", "payType": None, "roomClass": None},
            {"hotelID": "h1", "roomTypeID": "2259669216", "roomNameDesc": "重复房型(应去重)"},
            {"hotelID": "h1", "roomTypeID": "2259669217", "roomRCName": "大床房"},
        ]}]})),
        ("getRoomInventoryInfo", (200, {"data": {
            "roomStatusResult": [
                {"roomTypeID": "2259669216", "effectDate": "2026-09-08", "roomStatus": "G",
                 "canUsedQuantity": 0},
                {"roomTypeID": "2259669216", "effectDate": "2026-09-09", "roomStatus": "G",
                 "canUsedQuantity": 1},
                {"roomTypeID": "2259669216", "effectDate": "2026-09-09", "roomStatus": "N",
                 "canUsedQuantity": 5},
                {"roomTypeID": "2259669217", "effectDate": "2026-09-10", "roomStatus": "N",
                 "canUsedQuantity": 2},
                {"roomTypeID": None, "effectDate": "2026-09-11", "roomStatus": "G"},
            ],
            "roomPriceResult": {"roomPriceInfo": [
                {"roomTypeID": "2259669216", "effectDate": "2026-09-08", "price": 200.0},
                {"roomTypeID": "2259669216", "effectDate": "2026-09-08", "price": 114.0},
                {"roomTypeID": "2259669216", "effectDate": "2026-09-09", "price": None},
            ]},
        }})),
    ]
    http = FakeHttp(routes)
    ctx = make_ctx(http)
    results = await room_mod.RoomExtractor().extract(ctx)
    check("返回 1 条", len(results) == 1)
    res = results[0]
    check("status == ok", res.status == "ok", res.error)
    rows = {(r["room_type_id"], r["effect_date"]): r for r in res.records}
    check("聚合键 (roomTypeID, effectDate) → 3 行(缺 roomTypeID 的丢弃)", len(rows) == 3, sorted(rows))

    sold = rows[("2259669216", date(2026, 9, 8))]
    check("★ 售完(canUsedQuantity=0, roomStatus='G') 仍 available=1", sold["available"] == 1)
    check("★ status_code == 'G' 且 quantity == 0", sold["status_code"] == "G" and sold["quantity"] == 0)
    check("price 取多 ratePlan 最小值 114.0", sold["price"] == 114.0, sold["price"])
    check("price 是 float", isinstance(sold["price"], float))
    check("房型名 HTML 实体已解码", sold["room_name"] == "露台三床套房（一室一厅+观景阳台）&A",
          sold["room_name"])

    mixed = rows[("2259669216", date(2026, 9, 9))]
    check("同键一 G 一 N → available=0", mixed["available"] == 0)
    check("status_code = 首个非 G", mixed["status_code"] == "N")
    check("quantity = 组内 max(5)", mixed["quantity"] == 5, mixed["quantity"])
    check("无价 → price is None", mixed["price"] is None)

    closed = rows[("2259669217", date(2026, 9, 10))]
    check("关房 roomStatus='N' → available=0", closed["available"] == 0)
    check("roomClass/payType 缺省回退可用(房型名走 roomRCName)", closed["room_name"] == "大床房")

    inv_call = [c for c in http.calls if "getRoomInventoryInfo" in c["url"]][0]
    body = inv_call["json_body"]
    check("getRoomInventoryInfo body 恰 7 键且无分页字段", len(body) == 7 and
          not ({"page", "pageIndex", "pageSize", "offset", "limit"} & set(body)), sorted(body))
    check("区间闭区间 [today, today+14]", body["startDate"] == "2026-08-26"
          and body["endDate"] == "2026-09-09", (body["startDate"], body["endDate"]))
    prod_call = [c for c in http.calls if "getRcProductList" in c["url"]][0]
    check("getRcProductList body == {}", prod_call["json_body"] == {})
    check("dto 去重(同 roomTypeID 只留一个)",
          len(body["hotelRoomInfoDtoList"]) == 2, body["hotelRoomInfoDtoList"])
    check("dto payType 缺省 PP / roomClass 回退 roomTypeID",
          body["hotelRoomInfoDtoList"][0]["payType"] == "PP"
          and body["hotelRoomInfoDtoList"][0]["roomClass"] == "2259669216")

    # ---- 降级路径:records 必须为 None(调用方据此不 DELETE) ----
    http_bad = FakeHttp([("getRcProductList", (500, {"err": 1}))])
    bad = (await room_mod.RoomExtractor().extract(make_ctx(http_bad)))[0]
    check("getRcProductList 失败 → degraded", bad.status == "degraded", bad.status)
    check("★ 降级路径 records is None(不写库、不 DELETE)", bad.records is None)

    http_empty = FakeHttp([("getRcProductList", (200, {"data": []}))])
    empty = (await room_mod.RoomExtractor().extract(make_ctx(http_empty)))[0]
    check("房型为空 → degraded + records None", empty.status == "degraded" and empty.records is None)

    http_nogrid = FakeHttp([
        ("getRcProductList", (200, {"data": [{"roomInfos": [{"hotelID": "h", "roomTypeID": "1"}]}]})),
        ("getRoomInventoryInfo", (200, {"data": {"roomStatusResult": []}})),
    ])
    nogrid = (await room_mod.RoomExtractor().extract(make_ctx(http_nogrid)))[0]
    check("网格为空 → no_data + records None", nogrid.status == "no_data" and nogrid.records is None)


def review_routes() -> list[tuple[str, tuple[int, Any]]]:
    return [
        ("/26353/getCommentList", (200, {"resStatus": {"rcode": 0}, "pageCount": 2, "commentlist": [
            {"commentId": "2077171587", "userName": "M253349****",
             "content": "整体服务态度很好，但是配套设施真的太老旧了",
             "addtime": ADDTIME, "score": {"avgScoreSimple": 2.9, "commentLevel": "差评"}},
            {"commentId": "", "userName": "张三", "content": "很好", "addtime": ADDTIME,
             "score": {"avgScoreSimple": 4, "commentLevel": "好评"}},
            {"commentId": "skip-me", "userName": "李四", "content": "", "addtime": ADDTIME,
             "score": {}},
        ]})),
        ("getCommentsScoreV2", (200, {"rcode": 0, "data": {"ctripRatingall": 4.2}})),
        ("getCompetitorCommentStat", (200, {"rcode": 0, "data": {"rank": 6}})),
        ("getCommentRateTrend", (200, {"rcode": 0, "data": [{"ordercnt": 10, "comments": 2}]})),
        ("getCommentNumV2", (200, {"ResponseStatus": {"Ack": "Success"},
                                   "ctripCount": {"commentCount": 12}})),
    ]


async def test_review() -> None:
    section("③-3 ReviewExtractor")
    http = FakeHttp(review_routes())
    ctx = make_ctx(http)
    results = await review_mod.ReviewExtractor().extract(ctx)
    check("返回 5 条(1 待回复 + 4 素材)", len(results) == 5, len(results))

    pending = results[0]
    check("待回复源 status == ok", pending.status == "ok", pending.error)
    check("2 条点评(content 空的被跳过)", len(pending.records) == 2, len(pending.records))
    rows = {r["review_id"]: r for r in pending.records}
    real = rows["2077171587"]
    check("★ star = int(float(score.avgScoreSimple)) = 2", real["star"] == 2, real["star"])
    check("star<=3 → sentiment bad", real["sentiment"] == "bad")
    check("user_name 原样(平台已脱敏)", real["user_name"] == "M253349****")
    check(
        "★ comment_time 按 Asia/Shanghai == 2025-08-27 00:00:00+08:00",
        real["comment_time"].isoformat() == "2025-08-27T00:00:00+08:00",
        real["comment_time"].isoformat(),
    )
    fp = review_mod.review_id_fallback(4, "张三", "很好", ADDTIME)
    fp_row = rows.get(fp)
    check("★ commentId 缺失 → 指纹兜底(用的原始 addtime 串)", fp_row is not None, sorted(rows))
    check("指纹行 star == 4 → good", fp_row is not None and fp_row["star"] == 4
          and fp_row["sentiment"] == "good")
    check("★ 行里没有 replied / strategy", all(
        "replied" not in r and "strategy" not in r for r in pending.records))

    comment_call = [c for c in http.calls if "/26353/getCommentList" in c["url"]][0]
    check("getCommentList body 的 pageSize == 20(盘点表的 10 是错的)",
          comment_call["json_body"]["pageSize"] == 20, comment_call["json_body"]["pageSize"])
    check("catalogTab == NotFeedBack", comment_call["json_body"]["catalogTab"] == "NotFeedBack")
    check("pageIndex 从 1 起(占位符按 legacy 渲染为字符串)",
          str(comment_call["json_body"]["pageIndex"]) == "1",
          repr(comment_call["json_body"]["pageIndex"]))
    check("分页只请求 1 页(3 < 20 触发停止)", len(
        [c for c in http.calls if "/26353/getCommentList" in c["url"]]) == 1)
    check("request body 无残留占位符", "{page}" not in json.dumps(comment_call["json_body"]))

    kinds = [r.target.module for r in results[1:]]
    check("★ 素材 4 类 score/competitor/trend/num", kinds == ["score", "competitor", "trend", "num"],
          kinds)
    mats = {r.target.module: r for r in results[1:]}
    check("score payload = data(dict)", mats["score"].records[0]["payload_json"] == {"ctripRatingall": 4.2})
    check("competitor payload = data(dict)", mats["competitor"].records[0]["payload_json"] == {"rank": 6})
    check("trend payload_path='data' 取到 list → 包一层 {'data': [...]}",
          list(mats["trend"].records[0]["payload_json"]) == ["data"])
    check("num payload_path='' → 整包落库",
          "ResponseStatus" in mats["num"].records[0]["payload_json"])
    check("素材行 channel='api'", all(r.records[0]["channel"] == "api" for r in results[1:]))
    score_call = [c for c in http.calls if "getCommentsScoreV2" in c["url"]][0]
    check("score 的 body 来自 api_rules(含 spiderkey)",
          "spiderkey" in score_call["json_body"], sorted(score_call["json_body"]))
    trend_call = [c for c in http.calls if "getCommentRateTrend" in c["url"]][0]
    check("trend 走表单通道(body 是 str → content + form content-type)",
          trend_call["content"] == "month=6"
          and trend_call["headers"]["content-type"].startswith("application/x-www-form-urlencoded"),
          (trend_call["content"], trend_call["headers"]["content-type"]))
    num_call = [c for c in http.calls if "getCommentNumV2" in c["url"]][0]
    check("num 走 JSON 通道", num_call["headers"]["content-type"] == "application/json")

    # ---- 平台拒答 → degraded,不落库 ----
    http_bad = FakeHttp([("/26353/getCommentList", (200, {"resStatus": {"rcode": 5001,
                                                                       "rmsg": "无权限"}}))])
    bad = (await review_mod.ReviewExtractor().extract(make_ctx(http_bad)))[0]
    check("平台拒答 rcode 非 0/200 → degraded", bad.status == "degraded", bad.status)
    check("拒答时 records 为空(不落库)", not bad.records)


# ---------------------------------------------------------------------------
# ④ repository 真实 SQL
# ---------------------------------------------------------------------------


class RecordingSession:
    def __init__(self) -> None:
        self.statements: list[Any] = []
        self.params: list[Any] = []

    async def scalar(self, stmt: Any) -> int:
        self.statements.append(stmt)
        return 7

    async def execute(self, stmt: Any, params: Any = None) -> None:
        self.statements.append(stmt)
        self.params.append(params)


def sql_of(stmt: Any) -> str:
    return str(stmt.compile(dialect=PG))


async def test_repository() -> None:
    section("④ CollectRepository 批次 D SQL")
    sess = RecordingSession()
    repo = CollectRepository(sess)  # type: ignore[arg-type]

    await repo.upsert_portal_columns([
        {"hotel_id": 3, "account_id": 2, "collect_date": "2026-08-26", "page": "channel_ctrip",
         "column_name": "visitor_total", "value": 120, "detail": {"source_api": "x"}},
    ])
    sql = sql_of(sess.statements[-1])
    check("portal UPSERT 唯一键四元组", "ON CONFLICT (hotel_id, collect_date, page, column_name)" in sql)
    check("portal UPSERT 覆盖 7 列", all(
        f"{c} = excluded.{c}" in sql for c in
        ("account_id", "value", "detail_json", "raw_json_path", "channel", "status", "error")), sql)
    check("portal UPSERT 不动 id/created_at",
          "id=excluded" not in sql and "created_at=excluded" not in sql)

    sess2 = RecordingSession()
    repo2 = CollectRepository(sess2)  # type: ignore[arg-type]
    n = await repo2.replace_room_states(3, "2026-08-26", [
        {"room_type_id": "1", "effect_date": "2026-09-08", "available": 1, "status_code": "G",
         "quantity": 0, "price": 114.0, "room_name": "X"},
    ])
    del_sql = sql_of(sess2.statements[0])
    ins_sql = sql_of(sess2.statements[1])
    check("整批替换第 1 步是 DELETE", del_sql.startswith("DELETE FROM alert_room_states"), del_sql[:60])
    check("DELETE 只按 hotel_id + collect_date",
          "hotel_id" in del_sql and "collect_date" in del_sql and "effect_date" not in del_sql)
    check("整批替换第 2 步是 INSERT(不是 UPSERT)", ins_sql.startswith("INSERT INTO alert_room_states")
          and "ON CONFLICT" not in ins_sql)
    check("replace_room_states 返回写入行数", n == 1, n)
    check("available 落库前 int(bool(...))",
          sess2.params[-1][0]["available"] == 1 and sess2.params[-1][0]["room_type_id"] == "1",
          sess2.params[-1][0])
    sess3 = RecordingSession()
    repo3 = CollectRepository(sess3)  # type: ignore[arg-type]
    n3 = await repo3.replace_room_states(3, "2026-08-26", [])
    check("空 rows:仍先 DELETE(故调用方必须自己保证不误调)", len(sess3.statements) == 1 and n3 == 0)

    sess4 = RecordingSession()
    repo4 = CollectRepository(sess4)  # type: ignore[arg-type]
    ids = await repo4.upsert_reviews([
        {"hotel_id": 3, "review_id": "r1", "user_name": "u", "star": 5, "content": "c",
         "sentiment": "good", "comment_time": datetime(2025, 8, 27, tzinfo=ZoneInfo("Asia/Shanghai"))},
    ])
    rsql = sql_of(sess4.statements[-1])
    head, _, set_part = rsql.partition("ON CONFLICT")
    check("upsert_reviews 返回 id", ids == [7], ids)
    check("★ INSERT 列清单不含 replied", "replied" not in head, head.replace("\n", " ")[:200])
    check("★ INSERT 列清单含 strategy(机制②的前半)", "strategy" in head)
    check("★★ DO UPDATE SET 不含 replied", "replied" not in set_part, set_part.replace("\n", " "))
    check("★★ DO UPDATE SET 不含 strategy", "strategy" not in set_part, set_part.replace("\n", " "))
    check("DO UPDATE SET 覆盖 6 项", all(
        k in set_part for k in ("user_name", "star", "content", "sentiment", "comment_time",
                                "fetched_at")), set_part.replace("\n", " "))
    check("冲突时刷新 fetched_at=now()", "fetched_at=now()" in set_part.replace(" ", ""))

    sess5 = RecordingSession()
    repo5 = CollectRepository(sess5)  # type: ignore[arg-type]
    await repo5.upsert_review_materials([
        {"hotel_id": 3, "collect_date": date(2026, 8, 26), "kind": "num",
         "payload_json": {"a": 1}},
    ])
    msql = sql_of(sess5.statements[-1])
    check("materials UPSERT 键 = (hotel_id, collect_date, kind)",
          "ON CONFLICT (hotel_id, collect_date, kind)" in msql)
    check("materials 覆盖 5 列", all(
        f"{c} = excluded.{c}" in msql
        for c in ("payload_json", "raw_json_path", "channel", "status", "error")), msql)

    sess6 = RecordingSession()
    repo6 = CollectRepository(sess6)  # type: ignore[arg-type]
    await repo6.upsert_review_materials([
        {"hotel_id": 3, "collect_date": date(2026, 8, 26), "kind": "trend", "payload_json": None},
    ])
    check("payload None → 'null'::jsonb(NOT NULL 不写 SQL NULL)",
          "'null'::jsonb" in sql_of(sess6.statements[-1]).replace('"', "'"))

    try:
        await repo.upsert_portal_columns([{"hotel_id": 3}])
        missing_ok = False
    except ValueError as exc:
        missing_ok = "collect_date" in str(exc)
    check("缺必填列直接报错(不静默写 NULL)", missing_ok)

    # ---- latest_module 的排序等价性 ----
    sess7 = RecordingSession()
    repo7 = CollectRepository(sess7)  # type: ignore[arg-type]

    class _Scalars:
        def first(self) -> None:
            return None

    class _Res:
        def scalars(self) -> _Scalars:
            return _Scalars()

    async def _execute(stmt: Any, params: Any = None) -> Any:
        sess7.statements.append(stmt)
        return _Res()

    sess7.execute = _execute  # type: ignore[method-assign]
    await repo7.latest_module(3, "审核记录", "昨日")
    lsql = sql_of(sess7.statements[-1])
    check("latest_module 逆序取最新(等价旧 ORDER BY 升序取末元素)",
          "ORDER BY collect_modules.collect_date DESC" in lsql and "LIMIT" in lsql, lsql.replace("\n", " "))
    check("latest_module 无 collect_date 过滤(旧口径)",
          "collect_date =" not in lsql and "collect_date >" not in lsql)


async def main() -> int:
    test_models()
    test_pure()
    await test_portal()
    await test_room()
    await test_review()
    await test_repository()
    print(f"\n===== 汇总: {CHECKS - len(FAILURES)}/{CHECKS} 通过 =====")
    if FAILURES:
        print("失败项:")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
