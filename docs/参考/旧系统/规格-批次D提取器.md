# 规格-批次D提取器（预警三源 / 房态 / 点评）

> **文档性质**：旧系统源码**逐字考古**产出，作为重写实现的唯一依据。凡「值 / 字符串 / URL / 字段名 / SQL / 判定条件」一律**原样抄录**，不做改写、不做归一化。
> **证据约定**：每条结论后标注 `相对路径:行号`（相对旧系统根目录 `D:\AAAAaaaa\Pythooooooooooooon\hotel-data-system`，下文简称 `<ROOT>`）。
> **只读声明**：本次考古**未修改旧系统任何源码文件**；`db/ebooking.db` 以 `file:...?mode=ro` 只读打开（脚本 `D:\AAAAaaaa\pypypypy\_probe_old_db.py`），仅做 `SELECT` / `PRAGMA`，无任何写入。
> **只读副作用披露（诚实记录）**：`ebooking.db` 处于 WAL 模式，SQLite 在开启**只读**连接时也会创建/触碰 `-shm`（32 KB 共享内存索引）与 `-wal`（**实测 0 字节**）两个伴随文件。这是 SQLite 的固有行为，**不是数据写入**。取证：主库 `ebooking.db` 修改时间仍是本次会话之前的 `2026/9/30 9:15:11`（未变），`PRAGMA integrity_check` = `ok`，四表行数与抽样前完全一致（21 / 225 / 2 / 16）。
> **禁止凭记忆**：本文所有接口、字段、SQL 均可按行号在源码中直接核对。

---

## 0. 速查（本批次对象清单）

| 对象 | 文件 | 行数 | 落库表 |
|---|---|---|---|
| 预警三源提取器 | `collectors/portal_columns.py` | 533 | `portal_columns` |
| 房态提取器 | `collectors/room_state.py` | 293 | `room_states` |
| 点评提取器 | `collectors/comment_collector.py` | 433 | `reviews` / `review_materials` |
| 点评端点配置 | `config/review_sources.json` | 118 | —（被上上者读取） |
| 建表与 UPSERT | `storage/db.py` | 1494 | 上述四表 |

**实库行数（`db/ebooking.db` 只读实测）**：

| 表 | 行数 |
|---|---|
| `portal_columns` | **21** |
| `room_states` | **225** |
| `reviews` | **2** |
| `review_materials` | **16** |

**共用的请求头与超时口径**（三个采集器同款）：

- `_USER_AGENT` 三处逐字相同：
  ```python
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
  "(KHTML, like Gecko) Chrome/125 Safari/537.36"
  ```
  → `portal_columns.py:41-44`、`room_state.py:37-40`、`comment_collector.py:43-46`
- 请求头字典（`portal_columns.py:213-223`、`room_state.py:106-116`）：
  ```python
  h = {
      "cookie": self._cookie_header(),
      "user-agent": _USER_AGENT,
      "referer": referer or "",
      "x-requested-with": "XMLHttpRequest",
  }
  if method == "POST":
      h["content-type"] = "application/json" if json_body \
          else "application/x-www-form-urlencoded; charset=UTF-8"
  ```
  `comment_collector.py:134-143` 同款，但参数名为 `form`（`h["content-type"] = _FORM_CONTENT_TYPE if form else "application/json"`），且 `_FORM_CONTENT_TYPE = "application/x-www-form-urlencoded; charset=UTF-8"`（`comment_collector.py:47`）。
- cookie 来源：`ApiCollector(account=..., hotel=...).cookie_header()`（`portal_columns.py:207-211`、`room_state.py:101-104`、`comment_collector.py:129-132`）。
- 超时：`Config.API_TIMEOUT_S`，其值为 `int(os.getenv("API_TIMEOUT_S", "20"))`（`config.py:50`），默认 **20 秒**。
- `allow_redirects=False`：`portal_columns.py:229,238`、`room_state.py:122`、`comment_collector.py:148,157`。
- 响应校验：`status_code >= 400` → 抛错；非 JSON → 抛错（`portal_columns.py:243-250`、`room_state.py:125-130`、`comment_collector.py:162-170`）。

---

## 1. 预警三源提取器 — `collectors/portal_columns.py`

### 1.1 模块定位与硬约定

- 模块 docstring 逐字（`portal_columns.py:2-23`），关键三条：
  - 「本模块只采集与落库,不做判定(判定归 P4-2 预警引擎);」（`portal_columns.py:9`）
  - 「每源失败不阻断整体采集(``status`` 在 ``sources`` 里分别汇总,顶层 ``errors`` 记录);」（`portal_columns.py:11`）
  - 「``hot_calendar``: 列=事件名,值=首日(按 holiName 分组的 min(holiDate));」（`portal_columns.py:21`）
- 主类名与计划书不符（见 §6.1）：实际为 `class PortalCollector:`（`portal_columns.py:167`）。
- 三源入口：`collect_channel`（`portal_columns.py:297`）、`collect_home_pending`（`portal_columns.py:358`）、`collect_hot_calendar`（`portal_columns.py:436`）；汇总 `collect_all`（`portal_columns.py:477-497`），键名为 `{"channel", "home_pending", "hot_calendar"}`（`portal_columns.py:480-482`），返回 `{"ok","sources","rows","errors"}`（`portal_columns.py:497`）。

### 1.2 接口端点（逐字抄录）

`portal_columns.py:49-65`：

```python
_FETCH_VISITOR = (
    "https://ebooking.ctrip.com/datacenter/api/dataCenter/current/fetchVisitorTitleV2"
)
_QUERY_MIN_PRICE = (
    "https://ebooking.ctrip.com/datacenter/api/dataCenter/current/queryHotelMinPriceV1"
)
_GET_COMMENTS_SCORE = (
    "https://ebooking.ctrip.com/datacenter/api/dataCenter/comment/getCommentsScoreV2"
)
_GET_SERVICE_DATA = "https://ebooking.ctrip.com/restapi/soa2/24588/getServiceData"
_GET_COMMENT_FAQ_COUNT = (
    "https://ebooking.ctrip.com/restapi/soa2/26353/getCommentAndFAQNeedFeedBackCount"
)
_QUERY_GROWTH_TASK = (
    "https://ebooking.ctrip.com/restapi/soa2/23958/queryPendingHotelGrowthTaskListV2"
)
_GET_HOT_EVENT = "https://ebooking.ctrip.com/ebkovsroom/api/inventory/getHotelHotEvent"
```

Referer（按页，逐字抄录）`portal_columns.py:70-78`：

```python
_REF_CHANNEL = (
    "https://ebooking.ctrip.com/datacenter/inland/businessreport/outline?microJump=true"
)
_REF_COMPETITION = (
    "https://ebooking.ctrip.com/ebkgrowth/datacenter/competition/competitionprofile"
)
_REF_COMMENT_LIST = "https://ebooking.ctrip.com/comment/commentList?microJump=true"
_REF_HOME = "https://ebooking.ctrip.com/home?microJump=true"
_REF_CALENDAR = "https://ebooking.ctrip.com/ebkovsroom/inventory/calendar?microJump=true"
```

`detail["source_api"]` 的枚举常量（`portal_columns.py:88-97`）：

```python
_SRC_VISITOR = "fetchVisitorTitleV2"
_SRC_MIN_PRICE = "queryHotelMinPriceV1"
_SRC_COMMENT = "getCommentsScoreV2"
_SRC_SERVICE = "getServiceData"
_SRC_COMMENT_FAQ = "getCommentAndFAQNeedFeedBackCount"
_SRC_GROWTH = "queryPendingHotelGrowthTaskListV2"
_SRC_HOT_EVENT = "getHotelHotEvent"
_SRC_AUDIT = "module:审核记录"
_SRC_VIOLATION = "module:违约看板/违规中心"
```

### 1.3 `allow_missing` 语义（★精确）

统一底层调用 `_call`（`portal_columns.py:502-516`）：

```python
def _call(self, name: str, url: str, referer: str, body: Optional[dict],
          errors: list[str], allow_missing: bool = False,
          method: str = "POST") -> Optional[dict]:
    try:
        if method == "GET":
            result = self._get(url, referer)
        else:
            result = self._post_json(url, body or {}, referer)
        return result if isinstance(result, dict) else {}
    except ApiCollectError as exc:
        errors.append(f"{name}: {exc}")
        if allow_missing:
            logger.warning("接口 {} 失败(继续): {}", name, exc)
            return None
        raise
```

语义逐条：

1. `allow_missing=True`：捕获 `ApiCollectError` → 把 `f"{name}: {exc}"` 追加进本源的 `errors` 列表 → 打 warning 日志 → **返回 `None`**，采集继续。`allow_missing=False`：**原样 `raise`**，中断本源。
2. **本源所有接口调用都传 `allow_missing=True`**：`portal_columns.py:307-315`（渠道四接口）、`portal_columns.py:367-370`（计数接口）、`portal_columns.py:374-376`（成长任务）、`portal_columns.py:447-448`（热点日历）。因此 **`allow_missing=False` 在本模块从未被使用**，是保留的严格模式开关。
3. 返回 `None` 后的下游：`json_get(None, "<path>")` 逐段判断 `isinstance(cur, dict)` 失败 → 返回 `None`（`collectors/rules.py:379-400`）→ `_to_str(None)` → `""`（`portal_columns.py:135-137`）。**即缺字段最终落 `value=''`（空串），不是 NULL，也不抛错。**
4. 状态汇总：只要 `errors` 非空 → `status="degraded"`，否则 `"ok"`（`portal_columns.py:352-353`、`397-398`、`471-472`）。`ok = all(s["status"] == "ok" ...) and total_rows > 0`（`portal_columns.py:496`）。
5. `hotel_id` 解析失败是**唯一**的 `status="degraded"` 且提前返回的分支，错误串为 `"未解析到 hotel_id(DB 无酒店)"`（`portal_columns.py:303-305`、`363-365`、`441-443`）。

### 1.4 渠道源（§1）——`collect_channel`（`portal_columns.py:297-353`）

**method**：四个接口全部 **POST，`content-type: application/json`**（走 `_post_json`，`portal_columns.py:225-232`）。

| # | 接口 | URL 常量 | method | 请求体原文 | referer | `allow_missing` |
|---|---|---|---|---|---|---|
| 1 | `fetchVisitorTitleV2` | `_FETCH_VISITOR` | POST | `{}`（`portal_columns.py:307`） | `_REF_CHANNEL` | `True` |
| 2 | `queryHotelMinPriceV1` | `_QUERY_MIN_PRICE` | POST | `{}`（`portal_columns.py:309-310`） | `_REF_CHANNEL` | `True` |
| 3 | `getCommentsScoreV2` | `_GET_COMMENTS_SCORE` | POST | `{}`（`portal_columns.py:311-312`） | `_REF_CHANNEL` | `True` |
| 4 | `getServiceData` | `_GET_SERVICE_DATA` | POST | `_service_data_body(t)`（下方原文） | `_REF_COMPETITION` | `True` |

调用原文（`portal_columns.py:307-316`）：

```python
visitor_body = self._call("fetchVisitorTitleV2", _FETCH_VISITOR, _REF_CHANNEL, {},
                          errors, allow_missing=True)
min_price_body = self._call("queryHotelMinPriceV1", _QUERY_MIN_PRICE, _REF_CHANNEL,
                            {}, errors, allow_missing=True)
comments_body = self._call("getCommentsScoreV2", _GET_COMMENTS_SCORE, _REF_CHANNEL,
                           {}, errors, allow_missing=True)
service_body = self._service_data_body(t)
service_data = self._call("getServiceData", _GET_SERVICE_DATA, _REF_COMPETITION,
                          service_body, errors, allow_missing=True)
rating_avg = _index_type_avg_comp(service_data, 12)
```

**第 4 个接口的 body 不是硬编码**：模板来自 `config/api_rules.json` 里 `name == "getServiceData"` 的首个 `api_def.body`（`portal_columns.py:260-268` 的 `_api_def_body`），再注入 `startDate` / `endDate`：

```python
def _service_data_body(self, today: date) -> dict:
    body = self._api_def_body("getServiceData") or {}
    body = dict(body)
    ctx = window_date_ctx("今日实时", today)
    body["startDate"] = ctx["startDate"]
    body["endDate"] = ctx["endDate"]
    return body
```
（`portal_columns.py:270-276`）

- 模板原文位置：`config/api_rules.json:1437-1498`，`"name": "getServiceData"`（`:1438`）、`"method": "POST"`（`:1439`）、`"url": "https://ebooking.ctrip.com/restapi/soa2/24588/getServiceData"`（`:1440`）、`"params": {"x-traceID": "{trace}"}`（`:1441-1443`）、body 顶层键为 `reqHead`（`:1446`）、`ota: "ctrip"`（`:1491`）、`cipher: {}`（`:1492`）、`header: {"platform": "WEB"}`（`:1493-1495`）。**模板 body 内不含 `startDate`/`endDate`**，二者纯由代码注入。
- `window_date_ctx("今日实时", today)` 的窗口语义：「今日实时/实时 → 今日单日」（`collectors/api_collector.py:145`、`:157-158`），故 `startDate == endDate == today`，二者格式均为 `"%Y-%m-%d"`（`collectors/api_collector.py:190-195`）。

**字段提取路径**（`json_get` 点号路径，实现见 `collectors/rules.py:379-400`）：

携程列（`portal_columns.py:319-330` 逐字）：

```python
ctrip = [
    ("visitor_total", json_get(visitor_body, "visitorTotal"), _SRC_VISITOR),
    ("visitor_avg", json_get(visitor_body, "competitorAvgNumber"), _SRC_VISITOR),
    ("min_price", json_get(min_price_body, "data.minPrice"), _SRC_MIN_PRICE),
    ("min_price_rank", json_get(min_price_body, "data.minPriceRank"), _SRC_MIN_PRICE),
    ("competitor_total", json_get(min_price_body, "data.competitorHotelTotal"),
     _SRC_MIN_PRICE),
    ("ratingall", json_get(comments_body, "data.ctripRatingall"), _SRC_COMMENT),
    ("rating_avg", rating_avg, _SRC_SERVICE),
    ("rating_rank", json_get(comments_body, "data.ctripRatingAllRanking"),
     _SRC_COMMENT),
]
```

去哪儿列（`portal_columns.py:336-345` 逐字）：

```python
qunar = [
    ("visitor_total", json_get(visitor_body, "qunarVisitorTotal"), _SRC_VISITOR),
    ("visitor_avg", json_get(visitor_body, "qunarCompetitorAvgNumber"), _SRC_VISITOR),
    ("ratingall", json_get(comments_body, "data.qunarRatingall"), _SRC_COMMENT),
    ("rating_avg", rating_avg, _SRC_SERVICE),
    ("rating_rank", json_get(comments_body, "data.qunarRatingAllRanking"),
     _SRC_COMMENT),
    ("competitor_total", json_get(comments_body, "data.competitorHotelTotal"),
     _SRC_COMMENT),
]
```

★ **关键差异**：携程的 `competitor_total` 取自 `queryHotelMinPriceV1` 的 `data.competitorHotelTotal`（`portal_columns.py:324-325`），而**去哪儿同名列取自 `getCommentsScoreV2` 的 `data.competitorHotelTotal`**（`portal_columns.py:343-344`）——两列同源键名相同但**请求接口不同**。重写时不可合并。

★ **`rating_avg` 是携程/去哪儿共用同一个值**：`rating_avg = _index_type_avg_comp(service_data, 12)`（`portal_columns.py:316`），两处列都引用该变量（`:327`、`:340`）。去哪儿侧额外在 `detail` 打标：

```python
for col, val, src in qunar:
    detail = {"source_api": src}
    if col == "rating_avg":
        detail["note"] = "去哪儿复用携程评分均值(getServiceData avgComp),无独立均值"
    self._save_column(hotel_id, collect_date, "channel_qunar", col, val, detail)
```
（`portal_columns.py:346-350`）

**`indexType == 12` 的精确取法**（`portal_columns.py:154-164` 逐字）：

```python
def _index_type_avg_comp(body: Optional[dict], index_type: Any) -> Any:
    """从 getServiceData 响应取 ``dataList`` 中 ``indexType == index_type`` 的 ``avgComp``。

    ``indexType`` 可能为 int 或 str,统一按 str 比较;取不到返回 None。
    """
    if not isinstance(body, dict):
        return None
    for item in body.get("dataList") or []:
        if isinstance(item, dict) and str(item.get("indexType")) == str(index_type):
            return item.get("avgComp")
    return None
```

- 比较方式：**`str()` 双向转字符串后比较**，以容忍 int/str 混用（`portal_columns.py:162`）。
- 返回键：`avgComp`（**不是** `val`）。
- 响应实锤样例（`config/responses/competitionprofile.json:8`，逐字片段）：
  `{"indexType": 12, "val": 4.2, "lastVal": 4.2, "avgComp": 4.14375, "rankComp": 6, "lastRank": 12}`
  → 单元测试断言 `rating_avg == "4.14375"`（`tests/test_portal_columns.py:191`），并明确「必须取 indexType==12 的 avgComp(而非 indexType 11)」（`tests/test_portal_columns.py:182`）。

### 1.5 首页待办源（§2）——`collect_home_pending`（`portal_columns.py:358-398`）

**接口 1：`getCommentAndFAQNeedFeedBackCount`**

- URL：`_GET_COMMENT_FAQ_COUNT`（`portal_columns.py:59-61`）
- method：POST，`content-type: application/json`
- referer：`_REF_COMMENT_LIST` = `"https://ebooking.ctrip.com/comment/commentList?microJump=true"`
- body：`_SoaBody.build("/comment/commentList")`（`portal_columns.py:369`）
- 调用原文（`portal_columns.py:367-372`）：

```python
faq_body = self._call("getCommentAndFAQNeedFeedBackCount",
                      _GET_COMMENT_FAQ_COUNT, _REF_COMMENT_LIST,
                      _SoaBody.build("/comment/commentList"), errors,
                      allow_missing=True)
comment_pending = json_get(faq_body, "commentAndFAQNeedFeedBackCount.commentNeedFeedBackCount")
qa_pending = json_get(faq_body, "commentAndFAQNeedFeedBackCount.hotelFAQNeedFeedBackCount")
```

**接口 2：`queryPendingHotelGrowthTaskListV2`**

- URL：`_QUERY_GROWTH_TASK`（`portal_columns.py:62-64`）
- method：POST，`content-type: application/json`
- referer：`_REF_HOME` = `"https://ebooking.ctrip.com/home?microJump=true"`
- body：`_SoaBody.build("/home")`（`portal_columns.py:376`）
- 调用与字段原文（`portal_columns.py:374-379`）：

```python
growth_body = self._call("queryPendingHotelGrowthTaskListV2",
                         _QUERY_GROWTH_TASK, _REF_HOME,
                         _SoaBody.build("/home"), errors, allow_missing=True)
todo_more = json_get(growth_body, "count")
if todo_more is None:
    todo_more = json_get(growth_body, "totalCount")
```

★ `todo_more` 有**主备两个字段路径**：先 `count`，为 `None` 才回退 `totalCount`（`portal_columns.py:377-379`）。

**`_SoaBody` 包裹体全文**（`portal_columns.py:100-132` 逐字）：

```python
class _SoaBody:
    """生成 SOA2 接口的标准 reqHead 包裹体(与 probe 捕获的一致)。

    兜底包裹体(见任务说明):``head`` 与 ``reqHead`` 同层。这里按 ``pathName``
    参数化,供 getCommentAndFAQNeedFeedBackCount(/comment/commentList)与
    queryPendingHotelGrowthTaskListV2(/home)复用。
    """

    _client = {
        "deviceType": "PC", "os": "Windows", "osVersion": "Windows 10",
        "deviceName": "Windows PC", "clientId": "00000000000000000000",
        "screenWidth": 1600, "screenHeight": 1000,
        "isIn": {"ie": False, "chrome": True, "chrome49": False, "wechat": False,
                 "firefox": False, "ios": False, "android": False},
        "isModernBrowser": True, "browser": "Chrome", "browserVersion": "151",
        "platform": "pc", "technology": "web",
    }
    _ubt = {"vid": "0000000000000.xxxxxxxxxxxx", "fp": "00000A-00000B-00000C",
            "rmsToken": ""}
    _gps = {"coord": "", "lat": "", "lng": "", "cid": 0, "cnm": ""}
    _head = {"cid": "00000000000000000000", "ctok": "", "cver": "1.0", "lang": "01",
             "sid": "8888", "syscode": "09", "auth": "", "xsid": "", "extension": []}

    @classmethod
    def build(cls, path_name: str) -> dict:
        return {
            "reqHead": {
                "host": "ebooking.ctrip.com", "pathName": path_name,
                "locale": "zh-CN", "release": "", "client": cls._client,
                "ubt": cls._ubt, "gps": cls._gps, "protocal": "https:",
            },
            "head": cls._head,
        }
```

★ 结构要点：`reqHead` 与 `head` **同层**（`portal_columns.py:126-131`）。注意键名拼写为 **`protocal`**（非 `protocol`，`:129`）。`clientId`/`cid` 为固定常量 `"00000000000000000000"`（`:110`、`:120`）——重写时应改为账号维度可配。

### 1.6 热点日历源（§3）——`collect_hot_calendar`（`portal_columns.py:436-472`）

- URL 拼接+日期参数（逐字，`portal_columns.py:445-448`）：

```python
end = (t + timedelta(days=60)).strftime("%Y-%m-%d")
url = f"{_GET_HOT_EVENT}?startDate={collect_date}&endDate={end}"
body = self._call("getHotelHotEvent", url, _REF_CALENDAR, None, errors,
                  allow_missing=True, method="GET")
```

- `collect_date = t.strftime("%Y-%m-%d")`（`portal_columns.py:438`），即 `startDate=today`、`endDate=today+60`。
- ★ **本接口是 GET**，`method="GET"` 显式传入（`portal_columns.py:448`）；对应的 `_get` 使用 `_headers(referer, method="GET")`，**不带 `content-type`**（`portal_columns.py:234-241`）。
- referer：`_REF_CALENDAR` = `"https://ebooking.ctrip.com/ebkovsroom/inventory/calendar?microJump=true"`。
- body 位置传 `None`（GET 不使用）。

**「按 `holiName` 分组取最早日为首日」的精确实现**（`portal_columns.py:449-472` 逐字）：

```python
events = (body or {}).get("data") or []
# 按 holiName 分组,事件首日 = min(holiDate)
groups: dict[str, dict] = {}
for ev in events:
    name = ev.get("holiName")
    if not name:
        continue
    d = ev.get("holiDate")
    g = groups.setdefault(name, {"dates": [], "real_holiday": ev.get("realHoliday"),
                                 "holiday": ev.get("holiday")})
    if d:
        g["dates"].append(d)
for name, g in groups.items():
    if not g["dates"]:
        continue
    first = min(g["dates"])
    end_date = max(g["dates"])
    lead_days = (date.fromisoformat(first) - t).days
    detail = {"end_date": end_date, "lead_days": lead_days,
              "holiday": bool(g["holiday"]), "real_holiday": bool(g["real_holiday"])}
    self._save_column(hotel_id, collect_date, "hot_calendar", name, first, detail)

return {"status": "ok" if not errors else "degraded", "rows": len(groups),
        "error": None if not errors else "; ".join(errors), "errors": errors}
```

逐条精确语义：

| 环节 | 精确行为 | 行号 |
|---|---|---|
| 事件源 | `body["data"]`，空则 `[]` | `:449` |
| 分组键 | `ev["holiName"]`；**falsy（None/`""`）直接 `continue` 丢弃** | `:453-455` |
| 日期列 | `ev["holiDate"]`；**仅 truthy 才 append** | `:456,459-460` |
| 组级元数据 | `realHoliday` / `holiday` 取**该 `holiName` 首次出现的那条事件**（`setdefault` 语义，后续同组事件的值被忽略） | `:457-458` |
| 首日 | `first = min(g["dates"])` —— **字符串字典序最小值**；因 `holiDate` 为 `YYYY-MM-DD`，字典序 min 等价于时间序最早 | `:464` |
| 末日 | `end_date = max(g["dates"])`，仅进 `detail`，**不落 `value`** | `:465` |
| 倒计时 | `lead_days = (date.fromisoformat(first) - t).days`（相对采集日，不是今天之外的基准） | `:466` |
| `detail` 归一 | `holiday` / `real_holiday` 经 **`bool(...)`** 强转，保证是布尔 | `:467-468` |
| 落库 | `page="hot_calendar"`，`column_name=name`（事件名），`value=first`（首日） | `:469` |
| 无日期组 | `if not g["dates"]: continue` → **跳过，不落行** | `:462-463` |
| 返回值 `rows` | `len(groups)`，**不是实际落库行数**（含被跳过的空日期组） → 见 §6.1 漂移项 A-12 | `:471` |

- 响应字段实锤：`data[]` 的 `{holiDate, holiName, realHoliday, holiday}`（`docs/采集域盘点表-预警.md:46`）。
- 单元测试断言（`tests/test_portal_columns.py:204-209`）：`by_name["中秋节"]["value"] == "2026-09-25"`、`detail["end_date"] == "2026-09-26"`、`detail["lead_days"] == 33`、`holiday is True`、`real_holiday is True`、`by_name["国庆节"]["value"] == "2026-10-01"`。

### 1.7 `column_name` 完整取值清单（每源落哪些列）

**代码来源**（唯一权威）：`channel_ctrip` = `portal_columns.py:319-330`；`channel_qunar` = `:336-345`；`home_pending` = `:385-392`；`hot_calendar` = 动态事件名 `:469`。

| `page` | `column_name`（按代码顺序） | 值来源（接口 + 字段路径） | `detail.source_api` |
|---|---|---|---|
| `channel_ctrip` | `visitor_total` | `fetchVisitorTitleV2` → `visitorTotal` | `fetchVisitorTitleV2` |
| `channel_ctrip` | `visitor_avg` | `fetchVisitorTitleV2` → `competitorAvgNumber` | `fetchVisitorTitleV2` |
| `channel_ctrip` | `min_price` | `queryHotelMinPriceV1` → `data.minPrice` | `queryHotelMinPriceV1` |
| `channel_ctrip` | `min_price_rank` | `queryHotelMinPriceV1` → `data.minPriceRank` | `queryHotelMinPriceV1` |
| `channel_ctrip` | `competitor_total` | `queryHotelMinPriceV1` → `data.competitorHotelTotal` | `queryHotelMinPriceV1` |
| `channel_ctrip` | `ratingall` | `getCommentsScoreV2` → `data.ctripRatingall` | `getCommentsScoreV2` |
| `channel_ctrip` | `rating_avg` | `getServiceData` → `dataList[indexType=="12"].avgComp` | `getServiceData` |
| `channel_ctrip` | `rating_rank` | `getCommentsScoreV2` → `data.ctripRatingAllRanking` | `getCommentsScoreV2` |
| `channel_qunar` | `visitor_total` | `fetchVisitorTitleV2` → `qunarVisitorTotal` | `fetchVisitorTitleV2` |
| `channel_qunar` | `visitor_avg` | `fetchVisitorTitleV2` → `qunarCompetitorAvgNumber` | `fetchVisitorTitleV2` |
| `channel_qunar` | `ratingall` | `getCommentsScoreV2` → `data.qunarRatingall` | `getCommentsScoreV2` |
| `channel_qunar` | `rating_avg` | `getServiceData` → `dataList[indexType=="12"].avgComp`（**与携程同值**） | `getServiceData` + `note` |
| `channel_qunar` | `rating_rank` | `getCommentsScoreV2` → `data.qunarRatingAllRanking` | `getCommentsScoreV2` |
| `channel_qunar` | `competitor_total` | `getCommentsScoreV2` → `data.competitorHotelTotal` | `getCommentsScoreV2` |
| `home_pending` | `comment_pending` | `getCommentAndFAQNeedFeedBackCount` → `commentAndFAQNeedFeedBackCount.commentNeedFeedBackCount` | `getCommentAndFAQNeedFeedBackCount` |
| `home_pending` | `qa_pending` | `getCommentAndFAQNeedFeedBackCount` → `commentAndFAQNeedFeedBackCount.hotelFAQNeedFeedBackCount` | `getCommentAndFAQNeedFeedBackCount` |
| `home_pending` | `audit_pending` | **不调接口**，模块记录派生（见 §1.8） | `module:审核记录` |
| `home_pending` | `violation_pending` | **不调接口**，模块记录派生（见 §1.8） | `module:违约看板/违规中心` |
| `home_pending` | `todo_more` | `queryPendingHotelGrowthTaskListV2` → `count`，回退 `totalCount` | `queryPendingHotelGrowthTaskListV2` |
| `hot_calendar` | **动态** = `holiName` 原文（如 `中秋节` / `国庆节`） | `getHotelHotEvent` → 组内 `min(holiDate)` | （热点日历 `detail` 无 `source_api`，见下） |

★ **hot_calendar 的 `detail` 里没有 `source_api`**：`collect_hot_calendar` 传给 `_save_column` 的 `detail` 只有 `{"end_date","lead_days","holiday","real_holiday"}`（`portal_columns.py:467-469`），未使用 `_SRC_HOT_EVENT`（该常量在 `:95` 定义但**在本函数中未被引用**）。重写时必须显式补上或明确不加。

★ **`home_pending` 共 5 列**，列清单原文（`portal_columns.py:385-392`）：

```python
cols = [
    ("comment_pending", 0 if comment_pending is None else int(comment_pending),
     _SRC_COMMENT_FAQ),
    ("qa_pending", 0 if qa_pending is None else int(qa_pending), _SRC_COMMENT_FAQ),
    ("audit_pending", audit_pending, _SRC_AUDIT),
    ("violation_pending", violation_pending, _SRC_VIOLATION),
    ("todo_more", 0 if todo_more is None else int(todo_more), _SRC_GROWTH),
]
```

★ **缺失归一为 `0`**：前四列（含两个模块派生列）都走 `0 if X is None else int(X)`，即 `None → 0`，**不是空串**（与渠道源的 `None → ""` 语义相反）。

**实库校验**（`db/ebooking.db` 只读实测，`portal_columns` 21 行）：

```
page=channel_ctrip    rows=8     distinct_columns=8    date_range=2026-08-26..2026-08-26
page=channel_qunar    rows=6     distinct_columns=6    date_range=2026-08-26..2026-08-26
page=home_pending     rows=5     distinct_columns=5    date_range=2026-08-26..2026-08-26
page=hot_calendar     rows=2     distinct_columns=2    date_range=2026-08-26..2026-08-26

channel_ctrip: competitor_total,min_price,min_price_rank,rating_avg,rating_rank,ratingall,visitor_avg,visitor_total
channel_qunar: competitor_total,rating_avg,rating_rank,ratingall,visitor_avg,visitor_total
home_pending: audit_pending,comment_pending,qa_pending,todo_more,violation_pending
hot_calendar: 中秋节,国庆节
```

→ 列集合与代码**完全一致**（`8 + 6 + 5 + 2 = 21`）；测试也断言 `result["rows"] == 8 + 6 + 5 + 2`（`tests/test_portal_columns.py:276`）。热点日历实库出现 2 个事件名：`中秋节`（`value=2026-09-25`）、`国庆节`（`value=2026-10-01`）。

### 1.8 `value` 为何统一 `str()` 落 TEXT（★）

**三段证据链**：

1. **表定义只给一个 TEXT 列**（`storage/db.py:319`）：`value TEXT,` —— 表结构上**没有** `value_int` / `value_real` / `value_date`。
2. **采集器先转一次**（`portal_columns.py:288-292`）：

```python
self._storage().save_portal_column(
    hotel_id=hotel_id, collect_date=collect_date, page=page,
    column_name=column_name, value=_to_str(value),
    detail=detail, account_id=(self.account or {}).get("id"),
)
```
   其中 `_to_str` 逐字（`portal_columns.py:135-137`）：

```python
def _to_str(value: Any) -> str:
    """统一 str 化;None → 空串。"""
    return "" if value is None else str(value)
```

3. **存储层再兜一次**（`storage/db.py:997-999`）：

```python
(hotel_id, account_id, collect_date, page, column_name,
 "" if value is None else str(value), detail_json,
 raw_json_path, channel, status, error),
```
   即 `save_portal_column` 在参数绑定处再次 `str()`，docstring 明写「value 统一转 str 存储;detail 序列化为 JSON。」（`storage/db.py:977-978`）。

**根本原因**：同一 `value` 列要同时承载四类**异构标量**——
- 整数计数：`visitor_total` / `visitor_avg` / `min_price` / `min_price_rank` / `competitor_total` / `rating_rank` / `comment_pending` / `qa_pending` / `audit_pending` / `violation_pending` / `todo_more`
- 浮点评分：`ratingall` / `rating_avg`
- ISO 日期串：`hot_calendar` 的 `value`（`first`，形如 `2026-09-25`）
- 空串：字段抓不到时的占位

**落库后果（重写必读）**：
- `value` 列是 **TEXT 且可空**（`notnull=0`，见 §4.1），但代码路径上**永不写入 NULL**：`None` 一律变 `""`。
- 读出后**必须自行 cast**：实库样本 `"available"` 之外，`portal_columns.value` 实测为字符串，如 `"2026-10-01"`、`"4.14375"`；测试断言亦以字符串比较（`tests/test_portal_columns.py:156-158`、`:191`）。
- `hot_calendar` 的日期是**字符串**，`min()` 依赖 ISO 格式的字典序性质（§1.6）。

### 1.9 `audit_pending` / `violation_pending` 由模块记录派生（★）

**常量（逐字，`portal_columns.py:80-86`）**：

```python
# ② 模块记录来源(取最新一条;见盘点表 §2 与 module_check_2026-08-23.md)
_AUDIT_MODULE = "审核记录"                 # 挂牌管理 / 昨日
_AUDIT_WINDOW = "昨日"
_AUDIT_PAYLOAD_KEY = "公示内容"
_VIOLATION_MODULE = "违约看板/违规中心"     # 商机中心 / 实时
_VIOLATION_WINDOW = "实时"
_VIOLATION_PAYLOAD_KEY = "违约记录数"
```

**派生实现（逐字，`portal_columns.py:400-431`）**：

```python
def _audit_pending(self, hotel_id: int, errors: list[str]) -> int:
    """② 模块20 审核记录最新 payload 的「公示内容」非空 → 1 否则 0。"""
    try:
        recs = self._module_latest(hotel_id, _AUDIT_MODULE, _AUDIT_WINDOW)
        payload = (recs.get("payload") or {}) if recs else {}
        return 1 if str(payload.get(_AUDIT_PAYLOAD_KEY) or "").strip() else 0
    except Exception as exc:  # noqa: BLE001 单源失败不阻断
        errors.append(f"audit_pending 读取失败: {exc}")
        return 0

def _violation_pending(self, hotel_id: int, errors: list[str]) -> int:
    """② 模块22 违约看板/违规中心最新 payload 的「违约记录数」。"""
    try:
        recs = self._module_latest(hotel_id, _VIOLATION_MODULE, _VIOLATION_WINDOW)
        payload = (recs.get("payload") or {}) if recs else {}
        val = payload.get(_VIOLATION_PAYLOAD_KEY)
        return 0 if val is None else int(val)
    except Exception as exc:  # noqa: BLE001 单源失败不阻断
        errors.append(f"violation_pending 读取失败: {exc}")
        return 0

def _module_latest(self, hotel_id: int, module: str, window: str) -> Optional[dict]:
    """查询模块记录最新一条:先按 (module, window) 精确,为空则回退按 module 取最新。

    返回 ``None`` 表示无记录(此时 audit=0 / violation=0)。
    """
    st = self._storage()
    recs = st.query_module_records(hotel_id, module=module, window=window)
    if recs:
        return recs[-1]
    recs = st.query_module_records(hotel_id, module=module)
    return recs[-1] if recs else None
```

**精确语义表**：

| 项 | `audit_pending` | `violation_pending` |
|---|---|---|
| 模块（`module_records.module`） | `"审核记录"` | `"违约看板/违规中心"` |
| 窗口（`module_records.window`） | `"昨日"` | `"实时"` |
| payload 键 | `"公示内容"` | `"违约记录数"` |
| 判定 | `1 if str(payload.get("公示内容") or "").strip() else 0` —— **非空字符串 → 1，空/None/纯空白 → 0** | `0 if val is None else int(val)` —— **原样取整数** |
| 无记录 | `recs` 为 `None` → `payload = {}` → `0` | 同左 → `val is None` → `0` |
| 异常兜底 | 任意 `Exception` → `errors.append(f"audit_pending 读取失败: {exc}")`，**返回 0** | 同型，返回 0 |

**「取最新一条」的精确含义（★容易写错）**：

- `_module_latest` **不做 SQL 排序**，而是靠 `Storage.query_module_records` 的固定 `ORDER BY` 后取**列表末元素** `recs[-1]`。
- `query_module_records` 的排序子句逐字：`" ORDER BY collect_date, page, module, window, id"`（`storage/db.py:871`）→ 末元素 = **`collect_date` 最大，同日期时 `id` 最大**的一条。
- **两级回退**：先 `module + window` 精确过滤；结果为空才退化为只按 `module` 过滤（`portal_columns.py:427-431`）。**没有** `collect_date` 过滤，即可能取到历史任意一天的最新记录。

★ 单元测试断言（`tests/test_portal_columns.py:257-258`）：`got["audit_pending"] == "1"`、`got["violation_pending"] == "3"`。

### 1.10 落库 `portal_columns`：唯一键与 UPSERT 语句原文

**唯一键（建表内联）**：`UNIQUE(hotel_id, collect_date, page, column_name)`（`storage/db.py:326`）。实库自动索引 `sqlite_autoindex_portal_columns_1`，`unique=1`，`cols=['hotel_id', 'collect_date', 'page', 'column_name']`。

**UPSERT 语句原文**（`storage/db.py:981-1000` 逐字）：

```sql
INSERT INTO portal_columns
    (hotel_id, account_id, collect_date, page, column_name, value,
     detail, raw_json_path, channel, status, error)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(hotel_id, collect_date, page, column_name) DO UPDATE SET
    account_id=excluded.account_id,
    value=excluded.value,
    detail=excluded.detail,
    raw_json_path=excluded.raw_json_path,
    channel=excluded.channel,
    status=excluded.status,
    error=excluded.error
RETURNING id
```

配套方法与 docstring（`storage/db.py:971-979`）：

```python
def save_portal_column(self, hotel_id: int, collect_date: str, page: str,
                       column_name: str, value: Any,
                       detail: Optional[dict] = None,
                       account_id: Optional[int] = None,
                       raw_json_path: Optional[str] = None, channel: str = "api",
                       status: str = "ok", error: Optional[str] = None) -> int:
    """UPSERT 采集列(计划书④ P4-1):同 (hotel_id, collect_date, page, column_name)
    冲突则覆盖其余字段。value 统一转 str 存储;detail 序列化为 JSON。"""
```

- `detail` 序列化：`detail_json = json.dumps(detail, ensure_ascii=False) if detail is not None else None`（`storage/db.py:979`）——**`ensure_ascii=False`**，中文事件名在 `column_name`，`detail` 保持中文可读。
- **冲突时 `id` / `created_at` 不变**（`DO UPDATE SET` 未列出这两列）。
- 幂等保证：同 `(page, column_name)` 不重复，测试断言 `len(names) == len(set(names))`（`tests/test_portal_columns.py:290-292`）。
- 读回时 `detail` 自动 `json.loads` 回 dict（`storage/db.py:1022-1033`）。
- 配套索引：`CREATE INDEX IF NOT EXISTS idx_portal_columns ON portal_columns(hotel_id, collect_date, page)`（`storage/db.py:464`）。

★ **`collect_date` 归一（`portal_columns.py:281-292`）**：

```python
def _save_column(self, hotel_id: int, collect_date: Any, page: str,
                 column_name: str, value: Any, detail: Optional[dict] = None) -> None:
    # 防御:collect_date 可能是 date/datetime 或 "YYYY-MM-DD" 字符串,统一 str 化
    if hasattr(collect_date, "strftime"):
        collect_date = collect_date.strftime("%Y-%m-%d")
    else:
        collect_date = str(collect_date)
```

---

## 2. 房态提取器 — `collectors/room_state.py`

### 2.1 链路总览

`POST getRcProductList`（房型列表，body=`{}`）→ 展开 `data[].roomInfos[]` 去重成 dto 列表 → `POST getRoomInventoryInfo`（body 含日期区间 + `hotelRoomInfoDtoList`）→ 聚合 `roomStatusResult` + 回填 `roomPriceResult` → `save_room_states`（**先整批 DELETE 再插入**）。

入口：`class RoomStateCollector`（`room_state.py:64`）、`collect(days=15, today=None)`（`room_state.py:182-217`）、模块级 `collect_room_states(...)`（`room_state.py:278-290`）。

### 2.2 两次请求的 URL / method / referer（逐字）

`room_state.py:42-46`：

```python
_GET_RC_PRODUCT = "https://ebooking.ctrip.com/ebkovsroom/api/inventory/getRcProductList"
_GET_ROOM_INVENTORY = (
    "https://ebooking.ctrip.com/ebkovsroom/api/inventory/getRoomInventoryInfo"
)
_REF_CALENDAR = "https://ebooking.ctrip.com/ebkovsroom/inventory/calendar?microJump=true"
```

- 两接口均 **POST + `content-type: application/json`**（走 `_post_json`，`room_state.py:118-130`；`json_body=True` 由 `:119` 写死）。
- referer 均为 `_REF_CALENDAR`（`room_state.py:137`、`:180`）。

**第 1 次请求（逐字，`room_state.py:135-151`）**：

```python
def _rc_products(self) -> list[dict]:
    """getRcProductList → 逐 roomInfo 展开为 dto 列表。"""
    resp = self._post_json(_GET_RC_PRODUCT, {}, _REF_CALENDAR)
    dtos: list[dict] = []
    seen: set = set()
    for parent in (resp.get("data") or []):
        if not isinstance(parent, dict):
            continue
        for info in (parent.get("roomInfos") or []):
            if not isinstance(info, dict):
                continue
            key = (info.get("hotelID"), info.get("roomTypeID"))
            if key in seen:
                continue
            seen.add(key)
            dtos.append(self._build_dto(info))
    return dtos
```

★ **请求体是空字典 `{}`**（`room_state.py:137`），**没有任何分页参数**。

**dto 构造（逐字，`room_state.py:153-167`）**：

```python
@staticmethod
def _build_dto(info: dict) -> dict:
    """单个 roomInfo → getRoomInventoryInfo 的 dto(售卖房型粒度)。

    房型名取已解码的中文名(HTML 实体已解码);roomClass 缺省回退 roomTypeID。
    """
    room_name = (info.get("roomNameDesc") or info.get("roomRCNameDesc")
                 or info.get("roomName") or info.get("roomRCName") or "")
    return {
        "hotelID": info.get("hotelID"),
        "roomTypeID": info.get("roomTypeID"),
        "roomName": _decode_html(room_name),
        "payType": info.get("payType") or "PP",
        "roomClass": info.get("roomClass") or info.get("roomTypeID"),
    }
```

- 房型名 **4 级回退**：`roomNameDesc` → `roomRCNameDesc` → `roomName` → `roomRCName` → `""`（`room_state.py:159-160`）。
- HTML 实体解码 `_decode_html`（逐字，`room_state.py:49-53`）：

```python
def _decode_html(value: Any) -> str:
    """HTML 实体解码;优先取已解码的 ``*Desc`` 字段,否则 unescape 原文。"""
    if value is None:
        return ""
    return html.unescape(str(value))
```
  实库样例房型名含中文全角括号：`露台三床套房（一室一厅+观景阳台）`。
- `payType` 缺省 `"PP"`（`room_state.py:165`）；`roomClass` 缺省回退 `roomTypeID`（`:166`）。
- **去重键 `(hotelID, roomTypeID)`**（`room_state.py:146-149`），先到先留。

**第 2 次请求 body（逐字，`room_state.py:169-180`）**：

```python
def _room_inventory(self, today: date, days: int, dtos: list[dict]) -> dict:
    end = (today + timedelta(days=days - 1)).strftime("%Y-%m-%d")
    body = {
        "startDate": today.strftime("%Y-%m-%d"),
        "endDate": end,
        "showRoomPrice": True,
        "showRoomInventory": True,
        "showLadderPolicy": True,
        "isPreTaxPrice": False,
        "hotelRoomInfoDtoList": dtos,
    }
    return self._post_json(_GET_ROOM_INVENTORY, body, _REF_CALENDAR)
```

- 区间语义：`startDate = today`，`endDate = today + (days-1)`，**默认 `days=15`**（`room_state.py:182`）→ 15 天闭区间。
- ★ **单次请求，无分页、无翻页循环、无 offset/limit**。

### 2.3 ★ 关于「分页处理」——本模块**不存在分页**

任务书要求记录"分页处理"，但代码事实是：

1. `getRcProductList` 请求体为 `{}`（`room_state.py:137`），无 `page`/`pageIndex`/`pageSize`/`offset`/`limit` 任一参数。
2. `getRoomInventoryInfo` 请求体 7 个键（`room_state.py:171-179`），同样无分页字段。
3. `collect()` 无任何 `for page in ...` / `while` 翻页循环（`room_state.py:197-217`）。
4. 盘点表对该两接口的记录同样未提分页（`docs/采集域盘点表-预警.md:55-56`）。

→ **结论：房态是「一次全量网格拉取」，不是分页拉取。** 重写时若引入分页，属**新增行为**，须另行确认平台是否支持，不能按"照抄旧实现"处理。

### 2.4 字段清单与聚合（`_build_rows`，`room_state.py:237-275`）

**落库行字段**（`room_state.py:266-274` 逐字）：

```python
rows.append({
    "room_type_id": str(rid),
    "room_name": name_map.get(rid),
    "effect_date": ed,
    "available": 1 if all_g else 0,
    "status_code": _repr_status(g["statuses"]),
    "quantity": g["quantity"],
    "price": price_map.get((rid, ed)),
})
```

| 落库列 | 来源 | 精确处理 | 行号 |
|---|---|---|---|
| `room_type_id` | `roomStatusResult[].roomTypeID` | `str(rid)` 强转字符串 | `:267` |
| `room_name` | dto 的 `roomName` | 由 `name_map` 反查，键为 `roomTypeID`；`setdefault` 先到先留；查不到 → `None` | `:240-243,268` |
| `effect_date` | `roomStatusResult[].effectDate` | 原样（形如 `2026-09-09`） | `:251,269` |
| `available` | `all(s == "G" for s in statuses)` | `1 if all_g else 0`（见 §2.5） | `:265,270` |
| `status_code` | `statuses` 列表 | `_repr_status(...)` 聚合代表值（见下） | `:271` |
| `quantity` | `roomStatusResult[].canUsedQuantity` | 组内 **最大值** `if qty > g["quantity"]: g["quantity"] = qty` | `:258-260` |
| `price` | `roomPriceResult.roomPriceInfo[]` | 按 `(roomTypeID, effectDate)` 取 **min** | `:273` |

**聚合主循环（逐字，`room_state.py:245-262`）**：

```python
# 按 (roomTypeID, effectDate) 聚合
grouped: dict[tuple, dict] = {}
for item in (resp.get("data") or {}).get("roomStatusResult") or []:
    if not isinstance(item, dict):
        continue
    rid = item.get("roomTypeID")
    ed = item.get("effectDate")
    if rid is None or ed is None:
        continue
    g = grouped.setdefault((rid, ed), {
        "statuses": [], "quantity": 0,
    })
    g["statuses"].append(str(item.get("roomStatus") or ""))
    qty = item.get("canUsedQuantity") or 0
    if qty > g["quantity"]:
        g["quantity"] = qty
```

- 聚合键：**`(roomTypeID, effectDate)`**（`room_state.py:254`），`roomTypeID` 或 `effectDate` 为 `None` 时**整条丢弃**（`:252-253`）。
- 一个键可能命中多条记录（同房型同日期多个 `ratePlan`/等级），**全部 status 收进列表**。
- `canUsedQuantity` 缺失用 `or 0`（`:258`），组内取 max。

**组内状态代表值 `_repr_status`（逐字，`room_state.py:56-61`）**：

```python
def _repr_status(statuses: list[str]) -> str:
    """聚合后的代表状态:全 'G' → 'G' 否则首个非 'G'。"""
    for s in statuses:
        if s != "G":
            return s
    return "G"
```

→ **返回首个非 `'G'` 的状态**（保持平台原始值，如 `'N'`），全 `'G'` 才返回 `'G'`。

**价格回填 `_price_map`（逐字，`room_state.py:222-235`）**：

```python
@staticmethod
def _price_map(resp: dict) -> dict:
    """roomPriceResult → {(roomTypeID, effectDate): min price}。"""
    price_map: dict[tuple, float] = {}
    for item in (resp.get("data") or {}).get("roomPriceResult", {}).get("roomPriceInfo") or []:
        if not isinstance(item, dict):
            continue
        key = (item.get("roomTypeID"), item.get("effectDate"))
        price = item.get("price")
        if price is None:
            continue
        if key not in price_map or price < price_map[key]:
            price_map[key] = float(price)
    return price_map
```

★ **多 ratePlan 取最小值的精确实现**：`if key not in price_map or price < price_map[key]: price_map[key] = float(price)`（`room_state.py:233-234`）——严格小于才覆盖，**并列时保留先出现的**；`price is None` 直接跳过（`:231-232`）；结果 `float(price)` 强转（`:234`）。
★ 取价路径逐字：`(resp.get("data") or {}).get("roomPriceResult", {}).get("roomPriceInfo") or []`（`:226`）——`roomPriceResult` 用 `{}` 兜底，`roomPriceInfo` 用 `or []` 兜底。
★ 单元测试断言最小价格：同 `(roomTypeID, effectDate)` 两个 ratePlan 响应 `114.0` / `200.0` → 断言 `price == 114.0`（`tests/test_room_state.py:89-91,208`）；另一组 `150.0` / `118.0` → 断言 `118.0`（`:92-96,211`）。无价格记录时 `price is None`（`tests/test_room_state.py:197`）。

### 2.5 ★★ `available = 1` 当且仅当 `roomStatus=='G'`（精确代码行 + 注释原文）

**模块 docstring 原文（`room_state.py:9-15`）**：

```
口径(实锤):
- ``available = 1 iff roomStatus == 'G'``(开房=可订);'N' 等 = 关房=不可订;
- 售完(``canUsedQuantity == 0`` 且 roomStatus=='G')仍视为可订(available=1,不误报未开房);
- ``roomStatusResult`` 可能含多个相同 ``roomTypeID`` 的等级(不同 ratePlan),按
  ``(roomTypeID, effectDate)`` 聚合:任一 status 为不可订 → 该组合不可订;
- ``price`` 从 ``roomPriceResult`` 按 ``(roomTypeID, effectDate)`` 回填(多 ratePlan 取最小);
- 每个 ``roomInfos[].roomTypeID`` 构造一个 dto(不同 ratePlan 分开,见任务说明)。
```

**判定代码行原文（`room_state.py:264-270`）**：

```python
for (rid, ed), g in grouped.items():
    all_g = all(s == "G" for s in g["statuses"])
    rows.append({
        "room_type_id": str(rid),
        "room_name": name_map.get(rid),
        "effect_date": ed,
        "available": 1 if all_g else 0,
```

→ **判定式**：`all_g = all(s == "G" for s in g["statuses"])`（`:265`），`available = 1 if all_g else 0`（`:270`）。
→ **等价表述**：单条记录时 `available == 1 ⟺ roomStatus == 'G'`；多条同键记录时 `available == 1 ⟺ 全部 roomStatus == 'G'`（**任一非 `'G'` → 0**，docstring `:12-13` 明写「任一 status 为不可订 → 该组合不可订」）。
→ 测试断言：`'G'` → `available == 1` / `status_code == "G"`；`'N'` → `available == 0` / `status_code == "N"`（`tests/test_room_state.py:179-182`）；两条记录一 G 一 N → `available == 0`、`status_code == "N"`、`quantity == 1`（`tests/test_room_state.py:223-225`）。

**★ 售完仍算可订**：`canUsedQuantity == 0` **不参与** `available` 判定——`available` 只看 `roomStatus`，`quantity` 是独立列。测试原文（`tests/test_room_state.py:185-197`）：

```python
def test_sold_out_still_available(env, monkeypatch):
    """售罄(canUsedQuantity=0 且 roomStatus='G')仍 available=1,不误报关房。"""
    ...
    assert sold["available"] == 1
    assert sold["status_code"] == "G"
    assert sold["quantity"] == 0
    assert sold["price"] is None  # 无 price 记录 → 回填 None
```

**建表口径注释原文（`storage/db.py:330`）**：

```
# 房态(房型×日期;available=1 iff roomStatus=='G'(开房);售完(canUsedQuantity=0)不算关房)
```

**列级注释原文（`storage/db.py:341-343`）**：

```
available INTEGER NOT NULL,          -- 1=可订(开房) 0=不可订(关房)
status_code TEXT,                    -- 原始 roomStatus('G'/'N'/...)
quantity INTEGER,                    -- 可售数量(canUsedQuantity)
```

**实库反证（只读实测，`room_states` 225 行）**：

```
available=0 n=29
available=1 n=196
-- available=0 且 quantity=0 的行数(售完误判检查) --
 2
-- available=1 且 quantity=0 的行数(售完仍可订) --
 28
```

→ **28 行 `available=1` 且 `quantity=0`**，正是「售完仍可订」口径在生产数据上的体现；`available=0` 的 29 行中仅有 2 行 `quantity=0`，说明**关房与售完是正交的两件事**，绝不可把 `quantity=0` 当作 `available=0`。

### 2.6 ★ 整批替换语义：先 `DELETE` 同 `(hotel_id, collect_date)` 再批量插入

**实现原文（`storage/db.py:1036-1061` 逐字）**：

```python
def save_room_states(self, hotel_id: int, collect_date: str, rows: list[dict[str, Any]],
                     account_id: Optional[int] = None,
                     raw_json_path: Optional[str] = None) -> int:
    """整批写入房态(计划书④ P4-1):先删除同 (hotel_id, collect_date) 旧数据再插入,
    幂等(同批重跑结果一致)。rows 元素: {room_type_id, room_name, effect_date,
    available, status_code, quantity, price}。返回写入行数。"""
    with self._write_lock, self._conn() as conn:
        conn.execute(
            "DELETE FROM room_states WHERE hotel_id=? AND collect_date=?",
            (hotel_id, collect_date),
        )
        for r in rows:
            conn.execute(
                """
                INSERT INTO room_states
                    (hotel_id, account_id, collect_date, room_type_id, room_name,
                     effect_date, available, status_code, quantity, price, raw_json_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (hotel_id, account_id, collect_date,
                 str(r.get("room_type_id") or ""), r.get("room_name"),
                 r.get("effect_date"), int(bool(r.get("available"))),
                 r.get("status_code"), r.get("quantity"), r.get("price"),
                 raw_json_path),
            )
        return len(rows)
```

**精确要点**：

1. `DELETE` 条件**只有两个键**：`hotel_id=? AND collect_date=?`（`storage/db.py:1043-1046`）——**不按 `account_id`、不按 `effect_date`**。即同一店同一天的所有房态行被整体清空后重建。
2. **先删后插在同一把 `self._write_lock` + 同一个 `with self._conn() as conn:` 事务块内**（`storage/db.py:1042`）——原子性由该事务保证。
3. `DELETE` 与 `INSERT` 之间**无 `COMMIT`**，是单事务整批替换。
4. **不是 UPSERT**：与 `portal_columns` / `reviews` / `review_materials` 三表的 `ON CONFLICT` 策略**本质不同**。表上虽有 `UNIQUE(hotel_id, collect_date, room_type_id, effect_date)`（`storage/db.py:347`），但代码路径永远先删干净，UNIQUE 只作兜底约束。
5. `available` 落库前再兜一次：`int(bool(r.get("available")))`（`storage/db.py:1057`）——任何真值 → `1`，假值 → `0`。
6. `room_type_id` 落库前：`str(r.get("room_type_id") or "")`（`storage/db.py:1056`）。
7. 返回值：`len(rows)`，即**传入行数**（`storage/db.py:1061`），不是 SQLite 的 `rowcount`。
8. 调用点（`room_state.py:212-217`）：

```python
rows = self._build_rows(dtos, resp)
self._storage().save_room_states(
    hotel_id=hotel_id, collect_date=collect_date, rows=rows,
    account_id=(self.account or {}).get("id"),
)
return {"ok": True, "status": "ok", "rows": len(rows), "errors": errors}
```
   `collect_date = t.strftime("%Y-%m-%d")`（`room_state.py:190`），`t = today or date.today()`（`:189`）。
9. 幂等测试断言：连续两次采集，行数相同 `n1 == 4`、`n2 == n1`（`tests/test_room_state.py:278-279`）。

★ **降级路径不写库**（`room_state.py:197-210`）：`getRcProductList` 失败或返回空 → `{"ok": False, "status": "degraded", "rows": 0, "errors": [...]}` 直接返回，**不调用 `save_room_states`** → **旧数据保留、不会被 DELETE 清空**。同理 `getRoomInventoryInfo` 失败也是提前返回。这是有意的保护：只有拿到完整新数据才整批替换。

### 2.7 落库 `room_states` 全部列名与含义

见 §4.2 的完整 DDL。列语义摘要（注释逐字来自 `storage/db.py:337-346`）：

| 列 | 类型 | 约束 | 注释原文 | 含义 |
|---|---|---|---|---|
| `id` | INTEGER | PK AUTOINCREMENT | — | 自增主键 |
| `hotel_id` | INTEGER | NOT NULL REFERENCES hotels(id) | — | 酒店外键 |
| `account_id` | INTEGER | REFERENCES accounts(id) | — | 采集账号 |
| `collect_date` | TEXT | NOT NULL | `-- 采集日` | 采集日 `YYYY-MM-DD` |
| `room_type_id` | TEXT | NOT NULL | — | 售卖房型 ID（字符串化） |
| `room_name` | TEXT | 可空 | — | 房型中文名（HTML 实体已解码） |
| `effect_date` | TEXT | NOT NULL | `-- 生效日 YYYY-MM-DD` | 房态生效日 |
| `available` | INTEGER | NOT NULL | `-- 1=可订(开房) 0=不可订(关房)` | 可订判定结果 |
| `status_code` | TEXT | 可空 | `-- 原始 roomStatus('G'/'N'/...)` | 平台原始状态 |
| `quantity` | INTEGER | 可空 | `-- 可售数量(canUsedQuantity)` | 组内 max |
| `price` | REAL | 可空 | `-- roomPriceResult 均价(冗余参考)` | 多 ratePlan 取 min |
| `raw_json_path` | TEXT | 可空 | — | 原始响应留档路径（代码未写入，实测为 NULL） |
| `created_at` | TEXT | DEFAULT `(datetime('now','localtime'))` | — | 创建时间 |

唯一键：`UNIQUE(hotel_id, collect_date, room_type_id, effect_date)`（`storage/db.py:347`）。
索引：`CREATE INDEX IF NOT EXISTS idx_room_states ON room_states(hotel_id, collect_date, effect_date)`（`storage/db.py:465`）。

---

## 3. 点评提取器 — `collectors/comment_collector.py`

> 文件名确认：任务书说"文件名可能不同，请在 `collectors/` 下找"——实际文件名**就是** `collectors/comment_collector.py`（433 行），模块 docstring 对应 P5-1（`comment_collector.py:2`）。

### 3.1 ★ 端点/字段**全部来自 `config/review_sources.json`**

**加载链**：`CommentCollector.__init__` → `load_review_sources()` / `load_review_config()`（`comment_collector.py:101-102`）。
`load_review_sources` 定义（`app/review_reply.py:64-72` 逐字）：

```python
def load_review_sources(path=None) -> dict:
    """读 ``config/review_sources.json``;缺失 → {}``(采集器/自动模式按未配置降级)。"""
    p = path or Config.CONFIG_DIR / "review_sources.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("review_sources.json 读取失败({}),按未配置降级", exc)
        return {}
```

**模块 docstring 的硬约定（逐字，`comment_collector.py:12-13`）**：

```
- 端点(URL/method/body/字段路径)完全来自 ``config/review_sources.json``——接口改版/补捕获
  只改配置不改代码(R5-2);``ready=false`` 的源跳过并计 note,不判定失败;
```

**★ 重写要点：URL / method / body / referer / `list_path` / 字段路径 / 分页参数全部是数据，不是代码。** 采集器里**没有任何硬编码的点评端点**。唯一例外是 `_api_def_body` 会去 `config/api_rules.json` 借 `getCommentsScoreV2` 的 body 模板（`comment_collector.py:172-190`，由 `body_from_api_rules` 键触发，`:339-340`）。

### 3.2 `config/review_sources.json` 完整原文（逐字抄录）

```json
{
  "_comment": "计划书⑤ 点评采集/回复通道端点配置(2026-08-23 现场捕获版;URL/method/body/字段路径/分页均已实锤,见 docs/采集域盘点表-点评.md 与 .tmp_tests/probe_review_list.json+probe_review_responses.json;submit 为写操作,按 R5-Q3 模板审核+灰度前不补抓)",
  "pending": {
    "_comment": "待回复列表:restapi/soa2/26353/getCommentList,catalogTab=NotFeedBack=待回复(好评差评同列表,星级在 score.avgScoreSimple,等级在 score.commentLevel 好评/差评);分页 pageIndex/pageSize(响应 commentCount/pageCount/currentPageIndex/commentlist)",
    "list": {
      "ready": true,
      "url": "https://ebooking.ctrip.com/restapi/soa2/26353/getCommentList?_fxpcqlniredt=00000000000000000000",
      "method": "POST",
      "body": {
        "keyWord": "",
        "pageIndex": "{page}",
        "commentStatus": "",
        "isNeedTranslate": false,
        "sortType": 0,
        "catalogTab": "NotFeedBack",
        "catalogName": "待回复",
        "pageSize": 20,
        "needOrder": true,
        "startDate": "",
        "endDate": "",
        "channelSource": "trip",
        "header": {"platform": "WEB"},
        "head": {"cid": "00000000000000000000", "ctok": "", "cver": "1.0", "lang": "01",
                 "sid": "8888", "syscode": "09", "auth": "", "xsid": ""},
        "reqHead": {"host": "ebooking.ctrip.com", "pathName": "/comment/commentList",
                    "locale": "zh-CN", "release": "",
                    "client": {"deviceType": "PC", "os": "Windows", "osVersion": "Windows 10",
                               "deviceName": "Windows PC", "clientId": "00000000000000000000",
                               "screenWidth": 1600, "screenHeight": 1000,
                               "isIn": {"ie": false, "chrome": true, "chrome49": false,
                                        "wechat": false, "firefox": false, "ios": false,
                                        "android": false},
                               "isModernBrowser": true, "browser": "Chrome",
                               "browserVersion": "151", "platform": "pc", "technology": "web"},
                    "ubt": {"pageid": "10650085973", "pvid": 2, "sid": 7, "vid": "",
                            "fp": "", "rmsToken": ""},
                    "gps": {"coord": "", "lat": "", "lng": "", "cid": 0, "cnm": ""},
                    "protocal": "https:"}
      },
      "referer": "https://ebooking.ctrip.com/comment/commentList?microJump=true",
      "list_path": "commentlist",
      "fields": {
        "review_id": "commentId",
        "user_name": "userName",
        "star": "score.avgScoreSimple",
        "content": "content",
        "time": "addtime"
      },
      "sentiment_hint_path": "score.commentLevel",
      "sentiment_hint_map": {"好评": "good", "差评": "bad"},
      "page_size": 20,
      "max_pages": 20
    }
  },
  "scores": {
    "_comment": "评分/对比/趋势/计数素材(全部实锤;score body 复用 api_rules.json getCommentsScoreV2 捕获模板)",
    "score": {
      "ready": true,
      "url": "https://ebooking.ctrip.com/datacenter/api/dataCenter/comment/getCommentsScoreV2",
      "method": "POST",
      "body": null,
      "body_from_api_rules": "getCommentsScoreV2",
      "referer": "https://ebooking.ctrip.com/datacenter/inland/businessreport/outline?microJump=true",
      "payload_path": "data"
    },
    "competitor": {
      "ready": true,
      "url": "https://ebooking.ctrip.com/datacenter/api/dataCenter/comment/getCompetitorCommentStat",
      "method": "POST",
      "body": {},
      "referer": "https://ebooking.ctrip.com/datacenter/inland/userbehavior/user?microJump=true",
      "payload_path": "data"
    },
    "trend": {
      "ready": true,
      "url": "https://ebooking.ctrip.com/datacenter/api/dataCenter/comment/getCommentRateTrend",
      "method": "POST",
      "body": "month=6",
      "referer": "https://ebooking.ctrip.com/datacenter/inland/userbehavior/user?microJump=true",
      "payload_path": "data",
      "form": true
    },
    "num": {
      "ready": true,
      "url": "https://ebooking.ctrip.com/restapi/soa2/26353/getCommentNumV2?_fxpcqlniredt=00000000000000000000",
      "method": "POST",
      "body": {
        "channelSources": ["trip"],
        "header": {"platform": "WEB"},
        "head": {"cid": "00000000000000000000", "ctok": "", "cver": "1.0", "lang": "01",
                 "sid": "8888", "syscode": "09", "auth": "", "xsid": ""},
        "reqHead": {"host": "ebooking.ctrip.com", "pathName": "/comment/commentList",
                    "locale": "zh-CN", "release": "", "client": {"deviceType": "PC"},
                    "ubt": {"pageid": "10650085973", "pvid": 2, "sid": 7, "vid": "",
                            "fp": "", "rmsToken": ""},
                    "gps": {"coord": "", "lat": "", "lng": "", "cid": 0, "cnm": ""},
                    "protocal": "https:"}
      },
      "referer": "https://ebooking.ctrip.com/comment/commentList?microJump=true",
      "payload_path": ""
    }
  },
  "submit": {
    "_comment": "回复提交通道(P5-4 通道研究:提交接口未捕获→ready=false;现场证据:commentId+replyDetail.replyToken+getCommentTemplates 平台模板,详见 docs/回复通道研究结论.md 现场实锤节;按模板审核(R5-Q3)+0 灰度流程后补抓包:真实回复一次→crawl/capture_requests.py 抓提交 POST(候选 soa2/26353/replyComment 等)",
    "ready": false,
    "note": "提交接口未捕获,按 docs/回复通道研究结论.md §4 先补抓包;body_template 占位符:{comment_id}/{reply_token}/{content}",
    "api": {
      "url": "",
      "method": "POST",
      "referer": "https://ebooking.ctrip.com/comment/commentList?microJump=true",
      "body_template": {}
    },
    "rpa": {
      "enabled": false,
      "note": "RPA DOM 模拟(弹层/富文本/断言需现场实测;结论见 docs/回复通道研究结论.md §2)"
    }
  }
}
```

（原文对应 `config/review_sources.json:1-118`，本文档逐字转载，缩进与键序一致。）

### 3.3 `POST soa2/26353/getCommentList` 请求 body 原文

URL 逐字（`config/review_sources.json:7`）：

```
https://ebooking.ctrip.com/restapi/soa2/26353/getCommentList?_fxpcqlniredt=00000000000000000000
```

method：`"POST"`（`config/review_sources.json:8`）。
referer：`"https://ebooking.ctrip.com/comment/commentList?microJump=true"`（`config/review_sources.json:40`）。

**body 原文**（`config/review_sources.json:9-39`，逐字转录为 JSON 片段）：

```json
{
  "keyWord": "",
  "pageIndex": "{page}",
  "commentStatus": "",
  "isNeedTranslate": false,
  "sortType": 0,
  "catalogTab": "NotFeedBack",
  "catalogName": "待回复",
  "pageSize": 20,
  "needOrder": true,
  "startDate": "",
  "endDate": "",
  "channelSource": "trip",
  "header": {"platform": "WEB"},
  "head": {"cid": "00000000000000000000", "ctok": "", "cver": "1.0", "lang": "01",
           "sid": "8888", "syscode": "09", "auth": "", "xsid": ""},
  "reqHead": {"host": "ebooking.ctrip.com", "pathName": "/comment/commentList",
              "locale": "zh-CN", "release": "",
              "client": {"deviceType": "PC", "os": "Windows", "osVersion": "Windows 10",
                         "deviceName": "Windows PC", "clientId": "00000000000000000000",
                         "screenWidth": 1600, "screenHeight": 1000,
                         "isIn": {"ie": false, "chrome": true, "chrome49": false,
                                  "wechat": false, "firefox": false, "ios": false,
                                  "android": false},
                         "isModernBrowser": true, "browser": "Chrome",
                         "browserVersion": "151", "platform": "pc", "technology": "web"},
              "ubt": {"pageid": "10650085973", "pvid": 2, "sid": 7, "vid": "",
                      "fp": "", "rmsToken": ""},
              "gps": {"coord": "", "lat": "", "lng": "", "cid": 0, "cnm": ""},
              "protocal": "https:"}
}
```

**三个关键量逐字确认**：

| 键 | 值 | 出处 |
|---|---|---|
| `catalogTab` | `"NotFeedBack"` | `config/review_sources.json:15` |
| `pageSize` | `20` | `config/review_sources.json:17` |
| `page_size`（驱动翻页停止条件） | `20` | `config/review_sources.json:51` |
| `max_pages`（最大翻页数） | `20` | `config/review_sources.json:52` |

**分页实现（`comment_collector.py:270-311` 逐字）**：

```python
page_size = int(src.get("page_size") or 20)
max_pages = int(src.get("max_pages") or 20)
f = src.get("fields") or {}
seen_items = 0
for page in range(1, max_pages + 1):
    try:
        items, page_count = self._fetch_source_page(src, page, collect_date)
    except ApiCollectError as exc:
        errors.append(f"{key}: {exc}")
        break
    pages += 1
    if not items:
        break
    ...
    seen_items += len(items)
    if len(items) < page_size or page >= page_count:
        break
```

- 翻页变量：`range(1, max_pages + 1)` → **`pageIndex` 从 1 起，最多 20 页**（`comment_collector.py:274`）。
- **两个停止条件（或关系）**：`len(items) < page_size`（本页不足 20 条 → 末页）**或** `page >= page_count`（响应 `pageCount` 已到）（`comment_collector.py:310-311`）。
- `page_count` 来自响应 `pageCount`，解析失败/无值 → `1`，且 `max(page_count, 1)`（`comment_collector.py:217-222`）。
- **`{page}` 占位符替换**：`_render_body(src.get("body") or {}, {"page": page, "today": collect_date})`（`comment_collector.py:199`），递归替换所有层级的字符串 `"{page}"`（`comment_collector.py:71-82`）：
  ```python
  def _render_body(body: Any, ctx: dict) -> Any:
      """递归替换体占位符:``{page}``/``{today}``/``{hotel_id}``;其余原样。"""
      if isinstance(body, str):
          out = body
          for k, v in ctx.items():
              out = out.replace("{" + k + "}", str(v))
          return out
      if isinstance(body, list):
          return [_render_body(i, ctx) for i in body]
      if isinstance(body, dict):
          return {k: _render_body(v, ctx) for k, v in body.items()}
      return body
  ```
- body 形态决定通道：`dict` → `_post_json`（`content-type: application/json`）；`str` → `_post_form`（`content-type: application/x-www-form-urlencoded; charset=UTF-8`，`data=<原串>`）；其他 → `_post_json(url, {}, ...)`（`comment_collector.py:200-205`）。
- **平台拒答识别（★防误判"结构变化"）**（`comment_collector.py:207-216`）：
  ```python
  rs = result.get("resStatus") if isinstance(result, dict) else None
  if isinstance(rs, dict):
      rcode = rs.get("rcode")
      if rcode not in (None, 0, 200):
          raise ApiCollectError(f"平台拒答 rcode={rcode}: {str(rs.get('rmsg') or '')[:120]}")
  items = self._dig(result, src.get("list_path") or "commentlist")
  if not isinstance(items, list):
      raise ApiCollectError(f"list_path={src.get('list_path')} 非列表"
                            f"(结构疑似变化,R5-2 降级人工队列)")
  ```
- `list_path` 默认 `"commentlist"`（`comment_collector.py:213`），配置值为 `"commentlist"`（`config/review_sources.json:41`）。
- `_dig` 路径求值器（`comment_collector.py:391-416`）：点号分段；段形如 `[N]` 按数组下标；dict 用 `.get`；list 用整数下标；其余返回 `None`。

### 3.4 ★ 字段提取：`commentId` / `userName` / `score.avgScoreSimple` / `content` / `addtime`

**配置映射（逐字，`config/review_sources.json:42-48`）**：

```json
"fields": {
  "review_id": "commentId",
  "user_name": "userName",
  "star": "score.avgScoreSimple",
  "content": "content",
  "time": "addtime"
}
```

**代码实际取法（`comment_collector.py:283-307` 逐字）**：

```python
for it in items:
    if not isinstance(it, dict):
        continue
    content = str(self._dig(it, f.get("content") or "content") or "")
    if not content:
        continue
    review_id = str(self._dig(it, f.get("review_id") or "commentId") or "").strip()
    star_raw = self._dig(it, f.get("star") or "score.avgScoreSimple")
    star = None
    if star_raw not in (None, ""):
        try:
            star = int(float(star_raw))
        except (TypeError, ValueError):
            star = None
    if not review_id:
        review_id = review_id_fallback(
            star, self._dig(it, f.get("user_name") or "userName"),
            content, self._dig(it, f.get("time") or "addtime"))
    sentiment = self._sentiment_of(it, f, src, rules)
    self._storage().upsert_review(
        hotel_id=hotel_id, review_id=review_id, content=content,
        user_name=self._tostr(self._dig(it, f.get("user_name") or "userName")),
        star=star, sentiment=sentiment,
        comment_time=parse_addtime(self._dig(it, f.get("time") or "addtime")),
    )
    rows += 1
```

| 目标 | 配置路径 `fields.*` | 代码兜底默认 | 落库列 | 精确转换 |
|---|---|---|---|---|
| `commentId` | `review_id`: `"commentId"` | `"commentId"`（`comment_collector.py:289`） | `reviews.review_id` | `str(... or "").strip()`；**空则走内容指纹**（§3.6） |
| `userName` | `user_name`: `"userName"` | `"userName"`（`:299`、`:304`） | `reviews.user_name` | `_tostr(...)` = `"" if v is None else str(v)`（`:418-420`） |
| ★ 星级 | `star`: `"score.avgScoreSimple"` | `"score.avgScoreSimple"`（`:290`） | `reviews.star` | `int(float(star_raw))`；`None`/`""` → `None`；转换异常 → `None`（`:291-296`） |
| `content` | `content`: `"content"` | `"content"`（`:286`） | `reviews.content` | `str(... or "")`；**`content` 为空串则整条 `continue` 跳过**（`:287-288`） |
| `addtime` | `time`: `"addtime"` | `"addtime"`（`:300`、`:306`） | `reviews.comment_time` | `parse_addtime(...)` → `"YYYY-MM-DD HH:MM:SS"`（§3.5） |

**★ 「星级是对象不是 int」的精确含义**：

- 平台响应里 `score` 是**对象**，字段路径必须是 `score.avgScoreSimple`（**两层**）。盘点表逐字记载其结构（`docs/采集域盘点表-点评.md:157-159`）：
  ```
  {commentId, userName, content, addtime:"/Date(1784685469000+0800)/", score:{maxScore,avgScore,
   avgScoreSimple,commentLevel:好评|差评,subScores[]}, replyDetail:{replyId,replyToken,…}, 
   channelSource, sourceName, status, enableReply, pictureList, videoList,…}
  ```
  及 `docs/采集域盘点表-点评.md:160`：「**星级 = `score.avgScoreSimple`**;好感等级 = `score.commentLevel`(星级缺失兜底);时间 = `addtime`(毫秒时间戳,采集器已解析)」。
- 模块 docstring 逐字（`comment_collector.py:20`）：「星级=``score.avgScoreSimple``;等级=``score.commentLevel``(星级缺失时兜底);」。
- **若误按 `star` / `score` / `rating` 等单层 int 取值**（这正是 `docs/采集域盘点表-点评.md:54` 曾经的推断候选：「候选 `star` / `score` / `rating` / `userStar`,**(推断,不实锤)**」），会全量取到 `None` → `star=NULL`、`sentiment` 全 `unknown` → 好评自动回复全线失效。
- `star_raw` 还做了 `int(float(...))` 两级转换：既容忍 `"4.2"` 字符串，也容忍 `4.2000000001` 之类浮点（`comment_collector.py:294`）。

**好感/差评判定 `_sentiment_of`（逐字，`comment_collector.py:225-242`）**：

```python
@staticmethod
def _sentiment_of(item: dict, f: dict, src: dict, rules: dict) -> str:
    """星级优先;缺失时按 commentLevel(好评/差评)兜底;都无 → unknown。"""
    star_raw = CommentCollector._dig(item, f.get("star") or "score.avgScoreSimple")
    star = None
    if star_raw not in (None, ""):
        try:
            star = int(float(star_raw))
        except (TypeError, ValueError):
            star = None
    senti = classify_sentiment(star, rules)
    if senti == "unknown":
        hint = src.get("sentiment_hint_path") or ""
        hmap = src.get("sentiment_hint_map") or {}
        if hint:
            level = str(CommentCollector._dig(item, hint) or "")
            senti = hmap.get(level, "unknown")
    return senti
```

- 主路径 `classify_sentiment(star, rules)`（`app/review_reply.py:78-92` 逐字）：

```python
def classify_sentiment(star, rules=None) -> str:
    """星级 → 好评/差评/未知:``star >= good_min_star`` → good;``<= bad_max_star`` → bad;
    无星级或中间值 → ``unknown``(宁可漏不可错,防自动错回)。"""
    rules = rules or _DEFAULT_RULES
    if star is None:
        return "unknown"
    try:
        s = int(star)
    except (TypeError, ValueError):
        return "unknown"
    if s >= int(rules.get("good_min_star", 4)):
        return "good"
    if s <= int(rules.get("bad_max_star", 3)):
        return "bad"
    return "unknown"
```
  默认阈值 **`good_min_star=4` / `bad_max_star=3`**（`app/review_reply.py:88,90`），可被 `config/review_templates.json` 的 `rules` 覆盖（`app/review_reply.py:56-57`）。计划书对应配置原文：`"rules": {"good_min_star": 4, "bad_max_star": 3, "reply_interval_s": 120}`（`docs/计划书05_点评自动回复.md:171`）。
- **兜底路径**：`sentiment == "unknown"` 时读 `sentiment_hint_path` = `"score.commentLevel"`，映射表 `{"好评": "good", "差评": "bad"}`（`config/review_sources.json:49-50`）；未命中 → `"unknown"`（`comment_collector.py:241`）。
- 实库样本印证：`star=2` → `sentiment="bad"`；`star=5` → `sentiment="good"`。

### 3.5 `addtime` 格式 `/Date(ms+0800)/` 的精确正则与转换代码

**格式注释与正则（逐字，`comment_collector.py:48-49`）**：

```python
#: addtime 格式 /Date(1784685469000+0800)/
_DATE_RE = re.compile(r"/Date\((\d+)(?:[+-]\d{4})?\)/")
```

**转换函数（逐字，`comment_collector.py:58-68`）**：

```python
def parse_addtime(raw) -> Optional[str]:
    """``/Date(1784685469000+0800)/`` → ``YYYY-MM-DD HH:MM:SS``;其他原样/None。"""
    if raw is None:
        return None
    m = _DATE_RE.search(str(raw))
    if m:
        try:
            return datetime.fromtimestamp(int(m.group(1)) / 1000).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, OSError):
            return None
    return str(raw)[:40] if str(raw).strip() else None
```

**逐条精确语义**：

| 环节 | 精确行为 | 行号 |
|---|---|---|
| 正则 | `r"/Date\((\d+)(?:[+-]\d{4})?\)/"` —— 捕获组 1 = 毫秒数；**时区偏移 `[+-]\d{4}` 是可选的且被丢弃** | `:49` |
| 匹配方式 | `_DATE_RE.search(str(raw))` —— **`search` 不是 `match`**，允许前缀噪声 | `:62` |
| `raw is None` | 返回 `None`（**不落空串**） | `:60-61` |
| 时间戳除 | `int(m.group(1)) / 1000` → 浮点秒 | `:65` |
| 本地化 | `datetime.fromtimestamp(...)` —— **按运行机本地时区**解释，**未使用正则里的 `+0800`** | `:65` |
| 输出格式 | `strftime("%Y-%m-%d %H:%M:%S")` | `:65` |
| 异常 | `(ValueError, OSError)` → 返回 `None` | `:66-67` |
| 非 `/Date/` 串 | 回退 `str(raw)[:40]`，且**纯空白 → `None`** | `:68` |

★ **重写警示**：时区偏移被正则**吞掉但未使用**，`fromtimestamp` 依赖宿主机时区。若重写环境为 UTC，同一毫秒值会得到**不同**的 `comment_time` 字符串。这是旧实现的隐含假设（部署机在 +0800）。
实库样本 `comment_time` 形如 `"2026-08-24 11:13:13"`、`"2026-07-22 09:57:49"`。

### 3.6 `commentId` 缺失时内容指纹 `h + sha1[:16]` 的精确实现

**实现原文（逐字，`comment_collector.py:52-55`）**：

```python
def review_id_fallback(star, user_name, content, comment_time) -> str:
    """平台点评 id 缺失时的兜底 review_id:内容指纹(幂等,同点评同 id)。"""
    raw = "|".join(str(v or "") for v in (star, user_name, content, comment_time))
    return "h" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
```

**逐条精确语义**：

| 环节 | 精确行为 |
|---|---|
| 拼接顺序 | `star` → `user_name` → `content` → `comment_time`，**固定四元组、固定顺序** |
| 分隔符 | `"|"`（U+007C 竖线） |
| 空值归一 | `str(v or "")` —— `None` / `""` / `0` / `False` 都变 `""`（注意 `star=0` 也会被吞成 `""`） |
| 编码 | `raw.encode("utf-8")` |
| 摘要 | `hashlib.sha1(...).hexdigest()`（40 位小写十六进制） |
| 截断 | `[:16]`（前 16 位） |
| 前缀 | 字面量 `"h"` → 最终 17 字符，形如 `h0123456789abcdef` |
| 幂等性 | 同一条点评（四元组相同）→ 同一个 id → 命中 `UNIQUE(hotel_id, review_id)` → 走 UPSERT 更新而非新增 |

**调用点（逐字，`comment_collector.py:297-300`）**：

```python
if not review_id:
    review_id = review_id_fallback(
        star, self._dig(it, f.get("user_name") or "userName"),
        content, self._dig(it, f.get("time") or "addtime"))
```

★ **注意传入的是「原始四元组」而非归一化值**：`star` 是**已转成 int 或 None** 的值（`:291-296`），`comment_time` 是 **`addtime` 原始串**（如 `/Date(1784685469000+0800)/`），**不是** `parse_addtime` 的结果 —— `parse_addtime` 在 `upsert_review` 调用时才执行（`:306`）。重写时**必须保持这个顺序**，否则历史指纹 id 会全部变化，导致重复行。

实库校验：`reviews` 现有 2 行的 `review_id` 首字符均为 `'2'`（`prefix='2' n=2`），即**均为平台真 id**，尚未出现 `h` 前缀指纹行。

### 3.7 ★ 素材 4 类 `score` / `competitor` / `trend` / `num` 各自的接口与落库结构

**入口 `collect_scores`（`comment_collector.py:321-364`）**，遍历 `sources["scores"]` 的**全部键**（`:330-331`），跳过 `_` 前缀说明键与非 dict：

```python
scores = self._sources.get("scores") or {}
for kind, src in scores.items():
    if kind.startswith("_") or not isinstance(src, dict):
        continue
    url = str(src.get("url") or "").strip()
    if not src.get("ready") or not url:
        continue
```

| `kind` | 接口 URL（逐字） | method | body 原文 | referer | `payload_path` | 通道 |
|---|---|---|---|---|---|---|
| `score` | `https://ebooking.ctrip.com/datacenter/api/dataCenter/comment/getCommentsScoreV2` | POST | `null` + `"body_from_api_rules": "getCommentsScoreV2"` | `https://ebooking.ctrip.com/datacenter/inland/businessreport/outline?microJump=true` | `"data"` | JSON |
| `competitor` | `https://ebooking.ctrip.com/datacenter/api/dataCenter/comment/getCompetitorCommentStat` | POST | `{}` | `https://ebooking.ctrip.com/datacenter/inland/userbehavior/user?microJump=true` | `"data"` | JSON |
| `trend` | `https://ebooking.ctrip.com/datacenter/api/dataCenter/comment/getCommentRateTrend` | POST | `"month=6"`（**字符串**） | `https://ebooking.ctrip.com/datacenter/inland/userbehavior/user?microJump=true` | `"data"` | **表单**（`"form": true`） |
| `num` | `https://ebooking.ctrip.com/restapi/soa2/26353/getCommentNumV2?_fxpcqlniredt=00000000000000000000` | POST | SOA 包裹体（见 §3.2 原文） | `https://ebooking.ctrip.com/comment/commentList?microJump=true` | `""`（空 → **整包**） | JSON |

出处：`config/review_sources.json:57-65`（score）、`:66-73`（competitor）、`:74-82`（trend）、`:83-101`（num）。

**请求分派与 body 解析（逐字，`comment_collector.py:337-350`）**：

```python
try:
    body = src.get("body")
    if src.get("body_from_api_rules"):
        body = self._api_def_body(str(src.get("body_from_api_rules"))) or {}
    if isinstance(body, dict) and "_form" in body:
        body = body["_form"]
    if isinstance(body, dict):
        result = self._post_json(url, _render_body(body, {"today": collect_date}),
                                 src.get("referer") or "")
    elif isinstance(body, str):
        result = self._post_form(url, _render_body(body, {"today": collect_date}),
                                 src.get("referer") or "")
    else:
        result = self._post_json(url, {}, src.get("referer") or "")
except ApiCollectError as exc:
    errors.append(f"{kind}: {exc}")
    continue
```

- `body_from_api_rules` 触发跨文件借模板：`_api_def_body` 遍历 `config/api_rules.json` 的 `pages.*.api_defs`，取首个 `name` 匹配的 `body`（`comment_collector.py:172-190`）；`getCommentsScoreV2` 的模板原文在 `config/api_rules.json:197-209`，body 为：
  ```json
  {"fingerPrintKeys": "", "spiderkey": "1006-common-3fcvkQIkgYQUicSWdsR39xNMKbtJ41ysojF5WOqEBcEHGY7PR83wnSEPqRUPykGEDAwTHYcmvUgWpqvN4v57E5HYdow10Rf8RHcYtXWTDjczYmtIc8v7Y75rZPy9AyATRb4yTzj5GyXfEHXEQaYbkyStyolE1Ljg1Y1GWb4EXpIaaKXYt6idhIoLvn1KHNj4TeDcE3Ly6E7siakY8E9LYL4ybE3zY1OylfjaTJZAi5ziX9raSeLqYTgxHPIQzw96jdDwpOeOpES8ITdw6HiDkRcEmlYh9yZdyhBEnsjFUED1w6AjN3Ynajpgjg8w1E5cYt6ytUep8YSNWaAEMpY5byPE8dYbaympJDavqhy05ehfYp3jDZyPly3Bwkaj7FEOpibpwS8jpEc9Y9hyhGv9FWomimkKDTyFpE6XjdtEtEkFiBLyqsRkPegowGhwFLiLfKHzv5dRflEkEhOiltJtLj4bwtSvGnj44xMAePSy5Y4oe10wzGynmRsAy6XjdcyP4YqfWUGiMzvodE6lWFXjGHj03izBItNy6LjFnjndEBleXdrfTYtAwNFiZGxOzwGaEpyqyhwGYZcjp1IMSR3ayGQYS8vznYAovhavO8ygTE9ce7AjTAigmi61ylge5TE3tyg8j6lyFoeO6EH8Efbj9aEHGe4tyFtisXiodEQpRBcE8qyXbvpbJqBvZBw4dy7zy06I5mKgYTPeQlrSDe7pwzSylcY3hwH1j4cwUAYOAwbFJGEHyXYl6iXoRctJtpY7hEAzIQ8Y8Pi7Y71Yt8i4TIoGv38ecTYkNiLlYzArNARl1y0YnLEqseO9WT7eB1EXUj1MWXsE8GjXOyTYlmJc4rFMetsr0sKcLeoHEdkW34EABKtbrSYhQEthJccKL4R4GwN5W4NWfHeMpRAlW7kW0aWp0r4SW6PjfY6wkMeMaWMbRbswPXWZHWHae8mR5dWH9WqSW48xbOiH5vOYn1I4qidOjb8iotJkzJfDe0pILhebY4LYAlrgbI51KSfjmGJ39ykFwT0ehFxZHjd3wt5WHajU1jZYlmI8GjM6eHqJtawXjZfITYMGvzsWASRLSjldwsBvzMjQdxomWNQiQYU7ylse4nK54Yn0ymqRH0xFY44xtQwkQRZcjH4wG1vgzjNUe0mJanJcYUUKGBipaIM3YtcJlkWddxNbKHQyUYhprTnjZMJTOIGSWf0EUYo6J08wM4xb6RM4WSAwfqJc6WMXW6Lyp5j6aYApwFUYgQj6ZYNLiBYTkeN5Iharh4RaSwUNW7AWsleXmRpQW5oWhtWGFwB5wdmimYsTjbDy75ez7RL0wQ7WGhWc3ea5RhmWGtWN0WGsrh6i7k", "spiderVersion": "2.0"}
  ```
  （`spiderkey` 为**平台下发的长令牌**，重写时应视为可失效凭据，需重新捕获；`need_record: false`、`scope: "module"`，`config/api_rules.json:207-208`）
- `"form": true` 键在 `review_sources.json` 里存在（`:81`）但**采集器并未读取它**——通道判定完全由 `body` 的**类型**决定（`dict` → JSON，`str` → 表单）。`trend` 因 `body` 是字符串 `"month=6"` 而自动走表单通道。**这是隐式契约，重写时须保留。**
- `{"today}` 占位符在 4 类素材 body 里均无出现（`{"today": collect_date}` 传入但无匹配），替换为空操作。

**payload 提取与落库（逐字，`comment_collector.py:354-363`）**：

```python
payload_path = str(src.get("payload_path") or "")
payload = result if not payload_path else self._dig(result, payload_path)
if not isinstance(payload, dict):
    payload = {"data": payload}
status = "ok" if payload else "no_data"
self._storage().save_review_material(
    hotel_id=hotel_id, collect_date=collect_date, kind=kind,
    payload=payload, channel="api", status=status,
)
saved += 1
```

- `payload_path == ""`（`num` 的配置）→ `payload = result`（**整包**，见 §5.4 实库样本含 `ResponseStatus`/`hotelMap`/`ctripCount`）。
- 非 dict → 包一层 `{"data": payload}`。
- `status`：`"ok" if payload else "no_data"`（`comment_collector.py:358`）—— **注意 `{}` 空 dict 会判为 `"no_data"`**。该值是**作为参数直接绑定进 `status` 列**的（`storage/db.py:1342-1343`），因此实库可能出现 `status='no_data'`，而建表注释声明的枚举只有 `ok / degraded / failed`（`storage/db.py:438`）→ 见 §6.3 漂移项 C-17。
- `channel="api"` 写死（`comment_collector.py:361`）。
- ★ **`collect_scores` 未做 `resStatus.rcode` 拒答校验**（对比 `_fetch_source_page` 的 `comment_collector.py:207-212`）——平台拒答时会把错误包当素材落库。

**实库校验（`review_materials` 16 行）**：

```
kind=competitor   rows=4     2026-08-26..2026-08-26
kind=num          rows=4     2026-08-26..2026-08-26
kind=score        rows=4     2026-08-26..2026-08-26
kind=trend        rows=4     2026-08-26..2026-08-26
```

→ **实库确实有 4 种 `kind`，含 `num`**；而建表注释只写了 3 种（见 §6.4 漂移项 D-5）。

### 3.8 `review_materials` 的唯一键 `(hotel, date, kind)` 与 UPSERT

**唯一键（建表内联）**：`UNIQUE(hotel_id, collect_date, kind)`（`storage/db.py:441`）。实库自动索引 `sqlite_autoindex_review_materials_1`，`unique=1`，`cols=['hotel_id', 'collect_date', 'kind']`。

**UPSERT 原文（`storage/db.py:1328-1341` 逐字）**：

```sql
INSERT INTO review_materials
    (hotel_id, collect_date, kind, payload_json, raw_json_path,
     channel, status, error)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(hotel_id, collect_date, kind) DO UPDATE SET
    payload_json=excluded.payload_json,
    raw_json_path=excluded.raw_json_path,
    channel=excluded.channel,
    status=excluded.status,
    error=excluded.error
RETURNING id
```

方法签名与 docstring（`storage/db.py:1321-1326`）：

```python
def save_review_material(self, hotel_id: int, collect_date: str, kind: str,
                         payload: Optional[dict] = None, raw_json_path: Optional[str] = None,
                         channel: str = "api", status: str = "ok",
                         error: Optional[str] = None) -> int:
    """UPSERT 点评分析素材(计划书⑤ P5-5):同 (hotel_id, collect_date, kind) 覆盖。"""
    payload_json = json.dumps(payload, ensure_ascii=False) if payload is not None else json.dumps(None)
```

- 序列化：`json.dumps(payload, ensure_ascii=False)`；`payload is None` → `json.dumps(None)` = 字面串 `"null"`（`storage/db.py:1326`）——**`payload_json` 是 `NOT NULL`，永不写 NULL**。
- 冲突时覆盖 5 列，**`id` / `created_at` 不变**。
- 读回时 `payload_json` 自动解析回 `payload` 键（`storage/db.py:1362`、`:1378`，经 `_parse_json_fields(item, ("payload",))`）。
- 索引：`CREATE INDEX IF NOT EXISTS idx_review_materials ON review_materials(hotel_id, collect_date, kind)`（`storage/db.py:468`）。
- 环比取值辅助：`latest_review_material(hotel_id, kind, before=None)` → `ORDER BY collect_date DESC, id DESC LIMIT 1`（`storage/db.py:1356`）。

### 3.9 ★★ UPSERT 不回溯：冲突时绝不重置 `replied` / `strategy`

**`upsert_review` 原文（`storage/db.py:1169-1195` 逐字）**：

```python
def upsert_review(self, hotel_id: int, review_id: str, content: str,
                  user_name: Optional[str] = None, star: Optional[int] = None,
                  sentiment: str = "good", strategy: Optional[str] = None,
                  comment_time: Optional[str] = None) -> int:
    """UPSERT 点评(计划书⑤ P5-1):同 (hotel_id, review_id) 冲突仅刷新字段,
    绝不重置 replied/strategy(已回复不回溯,幂等重跑不产生重复行)。返回 id。"""
    with self._write_lock, self._conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO reviews
                (hotel_id, review_id, user_name, star, content, sentiment,
                 strategy, comment_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(hotel_id, review_id) DO UPDATE SET
                user_name=excluded.user_name,
                star=excluded.star,
                content=excluded.content,
                sentiment=excluded.sentiment,
                comment_time=excluded.comment_time,
                fetched_at=datetime('now','localtime')
            RETURNING id
            """,
            (hotel_id, review_id, user_name, star, content, sentiment,
             strategy, comment_time),
        )
        row = cur.fetchone()
        return int(row["id"]) if row is not None else int(cur.lastrowid)
```

★★ **「不回溯」的两条机制（必须逐字保留）**：

1. **`replied` 根本不在 `INSERT` 列清单里** —— `INSERT INTO reviews (hotel_id, review_id, user_name, star, content, sentiment, strategy, comment_time)`，**无 `replied`**。新行靠建表 `DEFAULT 0`（`storage/db.py:401`）；冲突行走 `DO UPDATE SET`，而 `DO UPDATE SET` 里**同样无 `replied`** → **已回复状态永久保持**。
2. **`strategy` 在 `INSERT` 列清单里、但被 `DO UPDATE SET` 显式排除** —— 冲突时传入的 `strategy` 参数**被丢弃**。采集器 `upsert_review` 根本不传 `strategy`（`comment_collector.py:302-307` 只有 `hotel_id`/`review_id`/`content`/`user_name`/`star`/`sentiment`/`comment_time`），故插入新行时 `strategy` 为 `NULL`。
   → **`strategy` 只能由 `mark_review_replied` 写入**，采集重跑永不覆盖。实库反证：`id=3` 的行 `replied=0` 而 `strategy="auto_failed"` —— 说明 `strategy` 由回复流程写入，采集器重跑未将其清空。

**`DO UPDATE SET` 覆盖列（共 6 项）**：`user_name`、`star`、`content`、`sentiment`、`comment_time`、`fetched_at=datetime('now','localtime')`。
**`DO UPDATE SET` 明确不覆盖**：`id`、`hotel_id`、`review_id`、`replied`、`strategy`。

**写入 `replied` / `strategy` 的唯一路径（`storage/db.py:1243-1259` 逐字）**：

```python
def mark_review_replied(self, hotel_id: int, review_id: str, replied: int = 1,
                        strategy: Optional[str] = None) -> bool:
    """标记点评已回复/忽略(计划书⑤:reply 流程完成后调用;不覆盖既有 strategy 时留空)。"""
    if strategy is None:
        with self._write_lock, self._conn() as conn:
            cur = conn.execute(
                "UPDATE reviews SET replied=?, strategy=COALESCE(?, strategy) "
                "WHERE hotel_id=? AND review_id=?",
                (replied, strategy, hotel_id, review_id),
            )
            return cur.rowcount > 0
    with self._write_lock, self._conn() as conn:
        cur = conn.execute(
            "UPDATE reviews SET replied=?, strategy=? WHERE hotel_id=? AND review_id=?",
            (replied, strategy, hotel_id, review_id),
        )
        return cur.rowcount > 0
```

- `strategy is None` 分支用 `strategy=COALESCE(?, strategy)`（值为 `None` → 保留旧值）；传入非 None 才覆盖。

**建表口径注释原文**（`storage/db.py:400-402`）：

```
sentiment TEXT DEFAULT 'good',       -- good(≥4星)/ bad(≤3星)/ unknown(无星级)
replied INTEGER DEFAULT 0,           -- 1=已回复(ok/ignored/silent) 0=待处理
strategy TEXT,                       -- 处理策略快照:模板id/silent/auto_failed
```

★ 注意 **`sentiment` 的 DEFAULT 是 `'good'`** —— 若调用方不传 `sentiment`，新行默认 `good`（`app/review_reply.py:78-92` 的 `classify_sentiment` 无星级时返回 `"unknown"`，采集器会显式传入，故实际不触发该默认值）。

### 3.10 点评采集的其他精确行为

- **`ready=false` 源跳过**（`comment_collector.py:263-265`）：
  ```python
  if not src.get("ready"):
      notes.append(f"{key}: 源未就绪(跳过) — {str((src or {}).get('note') or '')[:80]}")
      continue
  ```
  `_` 前缀键跳过（`:261-262`）；空 url 跳过并计 note（`:266-269`）。
- **返回结构**（`comment_collector.py:315-316`）：`{"status","rows","pages","notes","errors"}`；`status = "ok" if not errors else "degraded"`。
- **汇总 `collect_all`**（`comment_collector.py:369-386`）：`sources = {"pending": {...}, "scores": {...}}`，`ok = all(s["status"] == "ok" ...)`。
- 测试文件：`tests/test_comment_collector.py`（存在，`<ROOT>/tests/test_comment_collector.py`）。

---

## 4. 四张表的完整 DDL

> 以下 SQL 为 `db/ebooking.db` 的 `sqlite_master.sql` **实读原文**（`PRAGMA`/查询脚本 `_probe_old_db.py` 输出），与 `storage/db.py` 中 `ensure_schema()` 写入的语句**逐字一致**（差异仅：SQLite 存储时将 `CREATE TABLE IF NOT EXISTS` 规范化为 `CREATE TABLE`，并去掉首行缩进）。
> 四个建表语句的 **db.py 源码行号**：`portal_columns` = `storage/db.py:310-329`（口径注释在 `:308-309`）；`room_states` = `:330-350`（注释 `:330`）；`reviews` = `:391-408`（注释 `:389-390`）；`review_materials` = `:428-444`（注释 `:426-427`）。

### 4.1 `portal_columns`

**口径注释（`storage/db.py:308-309` 逐字）**：

```
# 采集列(渠道三指标/首页待办计数/热点日历事件)。页 = channel_ctrip / channel_qunar /
# hot_calendar / home_pending;value 统一 TEXT;detail 存补充 JSON(来源接口/排名/日期等)。
```

**建表语句全文**：

```sql
CREATE TABLE portal_columns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hotel_id INTEGER NOT NULL REFERENCES hotels(id),
    account_id INTEGER REFERENCES accounts(id),
    collect_date TEXT NOT NULL,
    page TEXT NOT NULL,                  -- channel_ctrip / channel_qunar / hot_calendar / home_pending
    column_name TEXT NOT NULL,           -- 字段名(如 visitor_total / rating_avg / comment_pending / 中秋节)
    value TEXT,
    detail TEXT,                         -- JSON: {"source_api": "...", ...}
    raw_json_path TEXT,
    channel TEXT DEFAULT 'api',
    status TEXT DEFAULT 'ok',
    error TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(hotel_id, collect_date, page, column_name)
)
```

**`PRAGMA table_info(portal_columns)` 实测**：

```
cid=0  name=id                 type=INTEGER  notnull=0 default=None pk=1
cid=1  name=hotel_id           type=INTEGER  notnull=1 default=None pk=0
cid=2  name=account_id         type=INTEGER  notnull=0 default=None pk=0
cid=3  name=collect_date       type=TEXT     notnull=1 default=None pk=0
cid=4  name=page               type=TEXT     notnull=1 default=None pk=0
cid=5  name=column_name        type=TEXT     notnull=1 default=None pk=0
cid=6  name=value              type=TEXT     notnull=0 default=None pk=0
cid=7  name=detail             type=TEXT     notnull=0 default=None pk=0
cid=8  name=raw_json_path      type=TEXT     notnull=0 default=None pk=0
cid=9  name=channel            type=TEXT     notnull=0 default="'api'" pk=0
cid=10 name=status             type=TEXT     notnull=0 default="'ok'" pk=0
cid=11 name=error              type=TEXT     notnull=0 default=None pk=0
cid=12 name=created_at         type=TEXT     notnull=0 default="datetime('now','localtime')" pk=0
```

**索引**：

```
name=idx_portal_columns                 unique=0 origin=c cols=['hotel_id', 'collect_date', 'page']
name=sqlite_autoindex_portal_columns_1  unique=1 origin=u cols=['hotel_id', 'collect_date', 'page', 'column_name']
```

**差异对照**：**无差异**。13 列、`notnull`、`default`、UNIQUE 四元组与 `storage/db.py:312-327` 完全一致。

### 4.2 `room_states`

**口径注释（`storage/db.py:330` 逐字）**：

```
# 房态(房型×日期;available=1 iff roomStatus=='G'(开房);售完(canUsedQuantity=0)不算关房)
```

**建表语句全文**：

```sql
CREATE TABLE room_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hotel_id INTEGER NOT NULL REFERENCES hotels(id),
    account_id INTEGER REFERENCES accounts(id),
    collect_date TEXT NOT NULL,          -- 采集日
    room_type_id TEXT NOT NULL,
    room_name TEXT,
    effect_date TEXT NOT NULL,           -- 生效日 YYYY-MM-DD
    available INTEGER NOT NULL,          -- 1=可订(开房) 0=不可订(关房)
    status_code TEXT,                    -- 原始 roomStatus('G'/'N'/...)
    quantity INTEGER,                    -- 可售数量(canUsedQuantity)
    price REAL,                          -- roomPriceResult 均价(冗余参考)
    raw_json_path TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(hotel_id, collect_date, room_type_id, effect_date)
)
```

**`PRAGMA table_info(room_states)` 实测**：

```
cid=0  name=id                 type=INTEGER  notnull=0 default=None pk=1
cid=1  name=hotel_id           type=INTEGER  notnull=1 default=None pk=0
cid=2  name=account_id         type=INTEGER  notnull=0 default=None pk=0
cid=3  name=collect_date       type=TEXT     notnull=1 default=None pk=0
cid=4  name=room_type_id       type=TEXT     notnull=1 default=None pk=0
cid=5  name=room_name          type=TEXT     notnull=0 default=None pk=0
cid=6  name=effect_date        type=TEXT     notnull=1 default=None pk=0
cid=7  name=available          type=INTEGER  notnull=1 default=None pk=0
cid=8  name=status_code        type=TEXT     notnull=0 default=None pk=0
cid=9  name=quantity           type=INTEGER  notnull=0 default=None pk=0
cid=10 name=price              type=REAL     notnull=0 default=None pk=0
cid=11 name=raw_json_path      type=TEXT     notnull=0 default=None pk=0
cid=12 name=created_at         type=TEXT     notnull=0 default="datetime('now','localtime')" pk=0
```

**索引**：

```
name=idx_room_states                    unique=0 origin=c cols=['hotel_id', 'collect_date', 'effect_date']
name=sqlite_autoindex_room_states_1     unique=1 origin=u cols=['hotel_id', 'collect_date', 'room_type_id', 'effect_date']
```

**差异对照**：**无差异**。13 列与 `storage/db.py:333-348` 一致。★ 注意 `available INTEGER NOT NULL` **无 DEFAULT**（`notnull=1 default=None`）—— 写入必须显式给值，`save_room_states` 以 `int(bool(...))` 保证（`storage/db.py:1057`）。

### 4.3 `reviews`

**口径注释（`storage/db.py:389-390` 逐字）**：

```
# reviews:待回复点评(upsert;星级→sentiment good(≥4星)/bad(≤3星)/unknown(无星级,
# 宁可漏不可错);replied=1 后不回溯)。
```

**建表语句全文**：

```sql
CREATE TABLE reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hotel_id INTEGER NOT NULL,           -- hotels.id
    review_id TEXT NOT NULL,             -- 平台点评 id(无则内容哈希,见采集器)
    user_name TEXT,                      -- 评价人
    star INTEGER,                        -- 星级(1~5;空=unknown)
    content TEXT NOT NULL,               -- 点评原文
    sentiment TEXT DEFAULT 'good',       -- good(≥4星)/ bad(≤3星)/ unknown(无星级)
    replied INTEGER DEFAULT 0,           -- 1=已回复(ok/ignored/silent) 0=待处理
    strategy TEXT,                       -- 处理策略快照:模板id/silent/auto_failed
    comment_time TEXT,                   -- 点评时间(原始,展示用)
    fetched_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(hotel_id, review_id)
)
```

**`PRAGMA table_info(reviews)` 实测**：

```
cid=0  name=id                 type=INTEGER  notnull=0 default=None pk=1
cid=1  name=hotel_id           type=INTEGER  notnull=1 default=None pk=0
cid=2  name=review_id          type=TEXT     notnull=1 default=None pk=0
cid=3  name=user_name          type=TEXT     notnull=0 default=None pk=0
cid=4  name=star               type=INTEGER  notnull=0 default=None pk=0
cid=5  name=content            type=TEXT     notnull=1 default=None pk=0
cid=6  name=sentiment          type=TEXT     notnull=0 default="'good'" pk=0
cid=7  name=replied            type=INTEGER  notnull=0 default='0' pk=0
cid=8  name=strategy           type=TEXT     notnull=0 default=None pk=0
cid=9  name=comment_time       type=TEXT     notnull=0 default=None pk=0
cid=10 name=fetched_at         type=TEXT     notnull=0 default="datetime('now','localtime')" pk=0
```

**索引**：

```
name=idx_reviews_pending                unique=0 origin=c cols=['hotel_id', 'replied', 'sentiment']
name=sqlite_autoindex_reviews_1         unique=1 origin=u cols=['hotel_id', 'review_id']
```

**差异对照**：**无差异**（实库 11 列与 `storage/db.py:393-406` 一致）。
★ **但与计划书⑤ §4.1 的 `reviews` 定义有差异**（缺 `comment_time` 等），见 §6.4 漂移项 D-6。
★ `hotel_id` / `review_id` 在此表**没有** `REFERENCES hotels(id)` 外键（对比 `portal_columns` / `room_states` 均有）——这是有意的（建表原文 `storage/db.py:395` 注释为 `-- hotels.id` 而非 `REFERENCES`）。

### 4.4 `review_materials`

**口径注释（`storage/db.py:426-427` 逐字）**：

```
# review_materials:点评分析每日素材缓存(getCommentsScoreV2 评分/getCompetitorCommentStat
# 对比建议/getCommentRateTrend 趋势;kind=score|competitor|trend;不复用 module_records)
```

**建表语句全文**：

```sql
CREATE TABLE review_materials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hotel_id INTEGER NOT NULL,
    collect_date TEXT NOT NULL,          -- YYYY-MM-DD(素材采集日)
    kind TEXT NOT NULL,                  -- score / competitor / trend
    payload_json TEXT NOT NULL,          -- 结构化素材
    raw_json_path TEXT,
    channel TEXT DEFAULT 'api',
    status TEXT DEFAULT 'ok',            -- ok / degraded / failed
    error TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(hotel_id, collect_date, kind)
)
```

**`PRAGMA table_info(review_materials)` 实测**：

```
cid=0  name=id                 type=INTEGER  notnull=0 default=None pk=1
cid=1  name=hotel_id           type=INTEGER  notnull=1 default=None pk=0
cid=2  name=collect_date       type=TEXT     notnull=1 default=None pk=0
cid=3  name=kind               type=TEXT     notnull=1 default=None pk=0
cid=4  name=payload_json       type=TEXT     notnull=1 default=None pk=0
cid=5  name=raw_json_path      type=TEXT     notnull=0 default=None pk=0
cid=6  name=channel            type=TEXT     notnull=0 default="'api'" pk=0
cid=7  name=status             type=TEXT     notnull=0 default="'ok'" pk=0
cid=8  name=error              type=TEXT     notnull=0 default=None pk=0
cid=9  name=created_at         type=TEXT     notnull=0 default="datetime('now','localtime')" pk=0
```

**索引**：

```
name=idx_review_materials               unique=0 origin=c cols=['hotel_id', 'collect_date', 'kind']
name=sqlite_autoindex_review_materials_1 unique=1 origin=u cols=['hotel_id', 'collect_date', 'kind']
```

**差异对照**：**结构无差异**（10 列与 `storage/db.py:430-442` 一致）。
★ **但口径注释与实际写入不一致**：注释写 `kind = score / competitor / trend`（`storage/db.py:434`、`:427`），实际采集器还写 `num`（`config/review_sources.json:83`；实库 `kind=num rows=4`）。见 §6.4 漂移项 D-5。

### 4.5 差异汇总（PRAGMA vs db.py）

| 表 | 列数 | 列名/类型/notnull/default/UNIQUE 差异 | 结论 |
|---|---|---|---|
| `portal_columns` | 13 | 无 | ✅ 一致 |
| `room_states` | 13 | 无 | ✅ 一致 |
| `reviews` | 11 | 无 | ✅ 一致 |
| `review_materials` | 10 | 无 | ✅ 一致（注释口径有别，见 D-5） |

**唯一需要留意的是 `reviews.replied` 的 default 形式**：`PRAGMA` 报 `default='0'`（不带引号包裹），而 `sentiment` 报 `default="'good'"`（带引号）——这是 SQLite 对数值字面量与字符串字面量 `sqlite_master` 存储形式的正常差异，源码均为 `DEFAULT 0` / `DEFAULT 'good'`（`storage/db.py:400-401`），**不是差异**。

**同批创建的相关索引（`storage/db.py:464-468` 逐字）**：

```python
conn.execute("CREATE INDEX IF NOT EXISTS idx_portal_columns ON portal_columns(hotel_id, collect_date, page)")
conn.execute("CREATE INDEX IF NOT EXISTS idx_room_states ON room_states(hotel_id, collect_date, effect_date)")
conn.execute("CREATE INDEX IF NOT EXISTS idx_reviews_pending ON reviews(hotel_id, replied, sentiment)")
conn.execute("CREATE INDEX IF NOT EXISTS idx_review_replies ON review_replies(hotel_id, review_id, status)")
conn.execute("CREATE INDEX IF NOT EXISTS idx_review_materials ON review_materials(hotel_id, collect_date, kind)")
```

---

## 5. 实际数据抽样（只读）

- 连接串：`sqlite3.connect(f"file:{DB}?mode=ro", uri=True)`（脚本 `_probe_old_db.py:32`）
- 库文件：`<ROOT>/db/ebooking.db`（**643 072 字节**，修改时间 `2026/9/30 9:15:11`，早于本次会话）
- 伴随文件（只读连接的 SQLite WAL 副作用）：`ebooking.db-shm` 32 768 字节、`ebooking.db-wal` **0 字节**
- sqlite3 库版本：`3.50.4`；`PRAGMA integrity_check` = `ok`
- 库内全部表：`accounts, alert_logs, alert_states, bots, collect_reports, group_bindings, hotels, login_events, module_records, monthly_snapshots, portal_columns, push_logs, review_materials, review_replies, reviews, room_states, sqlite_sequence`
- **未输出任何凭据/密文**：脚本对列名含 `cookie`/`token`/`password`/`passwd`/`secret`/`auth`/`session`/`ctok`/`xsid` 的列整体替换为 `<REDACTED>`；四表示例行中实际未命中任何敏感列。

### 5.1 行数汇总

| 表 | 行数 |
|---|---|
| `portal_columns` | **21** |
| `room_states` | **225** |
| `reviews` | **2** |
| `review_materials` | **16** |

### 5.2 `portal_columns` 样本（`ORDER BY id DESC LIMIT 2`）

```json
{
  "id": "43",
  "hotel_id": "3",
  "account_id": "2",
  "collect_date": "2026-08-26",
  "page": "hot_calendar",
  "column_name": "国庆节",
  "value": "2026-10-01",
  "detail": "{\"end_date\": \"2026-10-07\", \"lead_days\": 36, \"holiday\": true, \"real_holiday\": true}",
  "raw_json_path": null,
  "channel": "api",
  "status": "ok",
  "error": null,
  "created_at": "2026-08-26 13:17:18"
}
{
  "id": "42",
  "hotel_id": "3",
  "account_id": "2",
  "collect_date": "2026-08-26",
  "page": "hot_calendar",
  "column_name": "中秋节",
  "value": "2026-09-25",
  "detail": "{\"end_date\": \"2026-09-27\", \"lead_days\": 30, \"holiday\": true, \"real_holiday\": true}",
  "raw_json_path": null,
  "channel": "api",
  "status": "ok",
  "error": null,
  "created_at": "2026-08-26 13:17:18"
}
```

**印证**：`value` 为**日期字符串**（TEXT），`column_name` 为**中文事件名**，`detail` 为 JSON 串且键序 `end_date, lead_days, holiday, real_holiday` 与 `portal_columns.py:467-468` 的字典字面量顺序一致。`holiday` / `real_holiday` 落库为 JSON `true`。

### 5.3 `room_states` 样本（`ORDER BY id DESC LIMIT 2`）

```json
{
  "id": "781",
  "hotel_id": "3",
  "account_id": "2",
  "collect_date": "2026-08-26",
  "room_type_id": "2259669216",
  "room_name": "露台三床套房（一室一厅+观景阳台）",
  "effect_date": "2026-09-09",
  "available": "1",
  "status_code": "G",
  "quantity": "1",
  "price": "460.0",
  "raw_json_path": null,
  "created_at": "2026-08-26 13:50:48"
}
{
  "id": "780",
  "hotel_id": "3",
  "account_id": "2",
  "collect_date": "2026-08-26",
  "room_type_id": "2259669216",
  "room_name": "露台三床套房（一室一厅+观景阳台）",
  "effect_date": "2026-09-08",
  "available": "1",
  "status_code": "G",
  "quantity": "1",
  "price": "460.0",
  "raw_json_path": null,
  "created_at": "2026-08-26 13:50:48"
}
```

**分布统计**：

```
available=0 n=29
available=1 n=196
available=0 且 quantity=0 → 2 行
available=1 且 quantity=0 → 28 行   ← 「售完仍可订」口径的生产实证
```

**印证**：`status_code='G'` 与 `available=1` 同行出现；`room_name` 中文全角括号未被 HTML 实体破坏（`_decode_html` 生效）；`raw_json_path` 全为 `null`（采集器未传该参数）。

### 5.4 `reviews` 样本（`ORDER BY id DESC LIMIT 2`）

```json
{
  "id": "4",
  "hotel_id": "3",
  "review_id": "2077171587",
  "user_name": "M253349****",
  "star": "2",
  "content": "整体服务态度很好，但是配套设施真的太老旧了，电视比较小，只是房间面积大",
  "sentiment": "bad",
  "replied": "0",
  "strategy": null,
  "comment_time": "2026-08-24 11:13:13",
  "fetched_at": "2026-08-26 18:00:02"
}
{
  "id": "3",
  "hotel_id": "2",
  "review_id": "2031300215",
  "user_name": "213337****",
  "star": "5",
  "content": "房间里有大浴缸，泡着澡看着电影确实舒服",
  "sentiment": "good",
  "replied": "0",
  "strategy": "auto_failed",
  "comment_time": "2026-07-22 09:57:49",
  "fetched_at": "2026-08-26 18:00:00"
}
```

**分布与印证**：

```
replied=0 sentiment=bad  n=1
replied=0 sentiment=good n=1
review_id 首字符: '2' n=2   ← 均为平台真 id，暂无 h 前缀指纹行
```

- `star=2` → `sentiment="bad"`（阈值 `bad_max_star=3`）；`star=5` → `sentiment="good"`（阈值 `good_min_star=4`）→ 判定链生效。
- `comment_time` 已是 `"YYYY-MM-DD HH:MM:SS"` 格式（`parse_addtime` 生效，非原始 `/Date(...)/`）。
- `user_name` 为平台脱敏形式（`M253349****` / `213337****`）——**平台侧即已脱敏**，采集器原样落库。
- ★ `id=3` 行 `replied=0` 而 `strategy="auto_failed"` → 印证 §3.9「冲突不重置 strategy」：`strategy` 由回复流程写入后，采集重跑未清空。

### 5.5 `review_materials` 样本（`ORDER BY id DESC LIMIT 2`，JSON 截断 800 字符）

```json
{
  "id": "20",
  "hotel_id": "5",
  "collect_date": "2026-08-26",
  "kind": "num",
  "payload_json": "{\"ResponseStatus\": {\"Timestamp\": \"/Date(1787738405619+0800)/\", \"Ack\": \"Success\", \"Errors\": [], \"Extension\": [{\"Id\": \"CLOGGING_TRACE_ID\", \"Value\": \"f6aac5b8-6657-41ed-a5d1-c7e0ca4621e3\"}, {\"Id\": \"RootMessageId\", \"Value\": \"100025527-0a2c1f69-496594-6972\"}]}, \"hasCtripMapping\": true, \"ctripCount\": {\"commentCount\": 12, \"noRecommendCount\": 0, \"unReplyCount\": 0, \"hasPicCount\": 2, \"goodRate\": 1, \"responseRate\": 1, \"jumpUrl\": \"https://hotels.ctrip.com/hotels/detail/?hotelId=132636475#review\", \"commentBeforeDecorationCount\": 0, \"hasCommentBeforeDecorationCount\": false, \"preInterceptionCount\": 0}, \"hotelMap\": {\"ctrip\": \"https://hotels.ctrip.com/hotels/detail/?hotelId=132636475#review\", \"trip\": \"https://www.trip.com/hotels/detail/?hotelid=132636475\", \"tripCommentAnalysis\": \"/ebkgrowth/datacenter/user...<TRUNCATED total=879>",
  "raw_json_path": null,
  "channel": "api",
  "status": "ok",
  "error": null,
  "created_at": "2026-08-26 14:59:03"
}
{
  "id": "19",
  "hotel_id": "5",
  "collect_date": "2026-08-26",
  "kind": "trend",
  "payload_json": "{\"data\": [{\"hotelId\": null, \"errivalDate\": 1772294400000, \"ordercnt\": 19, \"comments\": 2, \"commentsRate\": null}, {\"hotelId\": null, \"errivalDate\": 1774972800000, \"ordercnt\": 18, \"comments\": 2, \"commentsRate\": null}, {\"hotelId\": null, \"errivalDate\": 1777564800000, \"ordercnt\": 10, \"comments\": 0, \"commentsRate\": null}, {\"hotelId\": null, \"errivalDate\": 1780243200000, \"ordercnt\": 8, \"comments\": 1, \"commentsRate\": null}, {\"errivalDate\": 1782835200000, \"ordercnt\": 10, \"comments\": 2, \"commentsRate\": null}, {\"hotelId\": null, \"errivalDate\": 1785513600000, \"ordercnt\": 13, \"comments\": 2, \"commentsRate\": null}]}",
  "raw_json_path": null,
  "channel": "api",
  "status": "ok",
  "error": null,
  "created_at": "2026-08-26 14:59:03"
}
```

**按 kind 分组**：

```
kind=competitor   rows=4     2026-08-26..2026-08-26
kind=num          rows=4     2026-08-26..2026-08-26
kind=score        rows=4     2026-08-26..2026-08-26
kind=trend        rows=4     2026-08-26..2026-08-26
```

**印证**：
- `kind="num"` 的行 `payload_json` 是**整包**（含 `ResponseStatus` / `hasCtripMapping` / `ctripCount` / `hotelMap`）→ 印证 `payload_path=""` 的「整包落库」语义（`comment_collector.py:354-357`）。
- ★ `kind="trend"` 的行 `payload_json` 是 `{"data": [ ... 6 条月度记录 ... ]}`，**恰好印证 `payload_path="data"` 的两段链路**：`_dig(result, "data")` 取到的是**列表**（非 dict）→ 命中 `if not isinstance(payload, dict): payload = {"data": payload}` → 重新包成 `{"data": [...]}`（`comment_collector.py:354-357`）。因此 **`trend` 的落库结构与接口原始响应同形**，但落库路径上确实多包了一层（巧合等值）。重写时须保留该包层逻辑，否则 `competitor`（`payload_path="data"` 且取到 **dict**）会丢掉 `data` 外层键。
- `errivalDate` 为**毫秒时间戳整数**（`1772294400000` 等），字段名拼写保持平台原样（`errivalDate`，非 `arrivalDate`）——**重写时不可"纠正"拼写**。

---

## 6. ⚠️ 文档漂移（计划书 / 盘点表 ↔ 代码事实）

> 本节列出考古中发现的**文档与代码不符**之处。判定原则：**以代码 + 实库为准**，文档为陈旧描述。重写时按"代码事实"实现，并在新文档中修正这些表述。

### 6.1 漂移 A：预警三源（计划书④ ↔ `portal_columns.py`）

| # | 文档原文 | 文档位置 | 代码事实 | 代码位置 |
|---|---|---|---|---|
| A-1 | `class PortalCollectorApi`:封装 cookie 头 + requests(复用 ApiCollector.request_api 语义;referer 按页) | `docs/计划书04_执行清单.md:117` | 类名是 **`class PortalCollector:`**，无 `PortalCollectorApi` | `collectors/portal_columns.py:167` |
| A-2 | ``collect_channel(hotel, account=None) -> dict`` | `docs/计划书04_执行清单.md:118` | 实际签名 **`def collect_channel(self, today: Optional[date] = None) -> dict`** —— 无 `hotel`/`account` 形参（在 `__init__` 注入） | `collectors/portal_columns.py:297` |
| A-3 | ``collect_home_pending(...)`` / ``collect_hot_calendar(...)`` | `docs/计划书04_执行清单.md:121-122` | 均为 `(self, today: Optional[date] = None) -> dict` | `collectors/portal_columns.py:358`、`:436` |
| A-4 | 端口:``collect_all(hotel, db_path=None) -> dict`` 汇总三源 | `docs/计划书04_执行清单.md:123` | 模块级 `collect_all(account=None, hotel=None, db_path=None, today=None) -> dict`；**类方法同名为 `PortalCollector.collect_all(self, today=None)`** | `collectors/portal_columns.py:519-530`、`:477` |
| A-5 | `getServiceData`(取 indexType==12 的 avgComp) | `docs/计划书04_执行清单.md:119` | 一致，但**未记载 `startDate`/`endDate` 是按 `window_date_ctx("今日实时")` 动态注入**（模板 body 内本无这两个键） | `collectors/portal_columns.py:270-276` |
| A-6 | 评分均值…`getServiceData#dataList[indexType==12].avgComp`(4.14375) ←竞争圈profile页实采 | `docs/采集域盘点表-预警.md:20` | 一致 | — |
| A-7 | 「实测 val=4.2 与 `ctripRatingall` 完全一致 → indexType=12=点评分,**avgComp=13 指标** = 竞争圈评分均值」 | `docs/采集域盘点表-预警.md:26` | **「avgComp=13 指标」表述有误**：`avgComp` 是字段名不是"13 指标"；响应实锤为 `{"indexType": 12, "val": 4.2, "avgComp": 4.14375, "rankComp": 6}` | `config/responses/competitionprofile.json:8`；取值代码 `collectors/portal_columns.py:154-164` |
| A-8 | 文档未记载 `channel_qunar.competitor_total` 与 `channel_ctrip.competitor_total` **来源接口不同** | `docs/计划书04_执行清单.md:120`（只并列列出列名） | 携程取 `queryHotelMinPriceV1.data.competitorHotelTotal`；去哪儿取 `getCommentsScoreV2.data.competitorHotelTotal` | `collectors/portal_columns.py:324-325` vs `:343-344` |
| A-9 | 文档未记载 `home_pending` 缺失值归一为 `0`（`0 if X is None else int(X)`） | `docs/计划书04_执行清单.md:121` | 代码显式归一，`None → 0` | `collectors/portal_columns.py:385-392` |
| A-10 | 文档未记载 `todo_more` 有 `count` → `totalCount` **两级回退** | `docs/采集域盘点表-预警.md:37`（只写 `count`） | 代码先 `count`，`None` 才回退 `totalCount` | `collectors/portal_columns.py:377-379` |
| A-11 | 文档未记载 `hot_calendar` 的 `detail` **不含 `source_api`**（常量 `_SRC_HOT_EVENT` 定义了但未使用） | `docs/计划书04_执行清单.md:122`（只写 detail 四键） | `detail = {"end_date","lead_days","holiday","real_holiday"}`，无 `source_api` | `collectors/portal_columns.py:95`（定义）vs `:467-469`（未用） |
| A-12 | 文档未记载 `collect_hot_calendar` 返回的 `rows` 是 `len(groups)` 而非实际落库行数 | `docs/计划书04_执行清单.md:122` | `"rows": len(groups)`，而无日期的组被 `continue` 跳过 | `collectors/portal_columns.py:462-463`、`:471` |
| A-13 | 文档未记载 SOA 包裹体中 `clientId`/`cid` 是**硬编码常量** `"00000000000000000000"` | `docs/采集域盘点表-点评.md:32`（只提 pathName 与 referer） | `_client.clientId` 与 `_head.cid` 均为字面量 `"00000000000000000000"` | `collectors/portal_columns.py:110`、`:120` |
| A-14 | 文档未记载键名拼写为 **`protocal`**（非 `protocol`） | `docs/采集域盘点表-点评.md:32` | `"protocal": "https:"` | `collectors/portal_columns.py:129`；`config/review_sources.json:38,97` |

### 6.2 漂移 B：房态（计划书④ / 盘点表 ↔ `room_state.py`）

| # | 文档原文 | 文档位置 | 代码事实 | 代码位置 |
|---|---|---|---|---|
| B-1 | 1) POST getRcProductList → data[]:basicRoomTypeID/roomName/**roomInfos[]:roomTypeID/hotelID** | `docs/计划书04_执行清单.md:128` | 代码实际读 `roomInfos[]` 的 **`hotelID` / `roomTypeID` / `roomNameDesc` / `roomRCNameDesc` / `roomName` / `roomRCName` / `payType` / `roomClass`**（8 个键，含 4 级房型名回退）；`basicRoomTypeID` **代码未使用** | `collectors/room_state.py:143-167` |
| B-2 | body={...`hotelRoomInfoDtoList":[{hotelID,roomTypeID,roomName,payType:"PP",roomClass:roomTypeID}]}` | `docs/计划书04_执行清单.md:129` | `payType` **不是写死 `"PP"`**，而是 `info.get("payType") or "PP"`（优先平台值）；`roomName` 取的是 **HTML 实体解码后**的中文名 | `collectors/room_state.py:159-166` |
| B-3 | `collect_room_states(hotel, account=None, db_path=None, days=15) -> dict` | `docs/计划书04_执行清单.md:127` | 实际 **`collect_room_states(account=None, hotel=None, db_path=None, days=15, today=None) -> dict`** —— 形参顺序是 `account, hotel`，且多了 `today` | `collectors/room_state.py:278-280` |
| B-4 | 「2) … → data.roomStatusResult[]→room_states 行;data.roomPriceResult.roomPriceInfo 按 (roomTypeID,effectDate) 回填 price」 | `docs/计划书04_执行清单.md:130` | 一致，但**未记载**：(a) `quantity` 取组内 **max**；(b) `status_code` 取**首个非 `G`**；(c) 多条同键记录**任一非 `G` 即 `available=0`**；(d) `roomTypeID`/`effectDate` 为 `None` 的行被**整条丢弃** | `collectors/room_state.py:252-271` |
| B-5 | 「房态(房型×日期;available=1 iff roomStatus=='G'(开房);售完(canUsedQuantity=0)不算关房」 | `docs/计划书04_执行清单.md:32` | **注释缺右括号**；db.py 原文为「…不算关房**)**」（右括号闭合外层） | `storage/db.py:330` |
| B-6 | 计划书/盘点表**均未提及房态接口的分页** | `docs/计划书04_执行清单.md:126-132`、`docs/采集域盘点表-预警.md:51-62` | 代码**确实没有分页**：`getRcProductList` body = `{}`；`getRoomInventoryInfo` body 7 键无分页字段；`collect()` 无翻页循环 → **「分页处理」在本模块不存在**，任务书该措辞与代码不符 | `collectors/room_state.py:137`、`:171-179`、`:197-217` |
| B-7 | 「单测:…断言 available 映射、UNIQUE 幂等、price 回填」 | `docs/计划书04_执行清单.md:132` | 一致；实际测试还断言 `status_code`、`quantity`（max）、房型名（含 HTML 解码） | `tests/test_room_state.py:179-182,223-237` |
| B-8 | 盘点表「`available=1 iff roomStatus=='G'`」 | `docs/采集域盘点表-预警.md:60` | **一致 ✅** —— 本条为文档与代码相符的正面样本 | `collectors/room_state.py:265,270` |

### 6.3 漂移 C：点评（盘点表 / 计划书⑤ ↔ `comment_collector.py` + `review_sources.json`）

| # | 文档原文 | 文档位置 | 代码/配置事实 | 代码位置 |
|---|---|---|---|---|
| C-1 | ★ `pageSize:10(可调)` | `docs/采集域盘点表-点评.md:150` | 配置与代码均为 **`"pageSize": 20`** / `"page_size": 20` / `int(src.get("page_size") or 20)` | `config/review_sources.json:17,51`；`collectors/comment_collector.py:270` |
| C-2 | ★ `channelSource:"",`（盘点表 §8.1 body 实录） | `docs/采集域盘点表-点评.md:151` | 配置为 **`"channelSource": "trip"`**（非空串） | `config/review_sources.json:21` |
| C-3 | 盘点表 §8.1 称请求 query 带 `_fxpcqlniredt=00000000000000000000` **+ `x-traceID`** | `docs/采集域盘点表-点评.md:146-147` | 配置 URL 只含 `?_fxpcqlniredt=00000000000000000000`，**无 `x-traceID`**；采集器也未注入该头 | `config/review_sources.json:7` |
| C-4 | `catalogTab` 枚举(实锤):`NotFeedBack`=待回复 / `all`=全部 / `needpromotion`=差评 | `docs/采集域盘点表-点评.md:152`；`docs/计划书05_点评自动回复.md` 未列 | 代码/配置**只使用 `"NotFeedBack"`**，未实现 `all` / `needpromotion` 分支 | `config/review_sources.json:15` |
| C-5 | 星级字段名只能推断:候选 `star` / `score` / `rating` / `userStar`,**(推断,不实锤)** | `docs/采集域盘点表-点评.md:54` | **已被 §8.1 自我推翻**：实锤为 `score.avgScoreSimple`（对象内字段）；代码 `_dig(it, "score.avgScoreSimple")` | `config/review_sources.json:45`；`collectors/comment_collector.py:290` |
| C-6 | 计划书⑤ `reviews` 建表：**无 `comment_time` 列**，`sentiment TEXT DEFAULT 'good', -- good(≥4星)/ bad(≤3星,阈值可配)` | `docs/计划书05_点评自动回复.md:135-143` | 实际 11 列，**含 `comment_time TEXT -- 点评时间(原始,展示用)`**；注释多了 `unknown(无星级)` | `storage/db.py:393-406` |
| C-7 | 计划书⑤ `review_replies`：`review_id TEXT, hotel_id INTEGER`（**均无 NOT NULL**）；`status TEXT`（无 NOT NULL/DEFAULT）；`mode TEXT, -- suggested / auto` | `docs/计划书05_点评自动回复.md:144-152` | 实际：`hotel_id INTEGER NOT NULL`、`review_id TEXT NOT NULL`、`status TEXT NOT NULL DEFAULT 'suggested'`、`mode TEXT NOT NULL, -- suggested(建议/人工) / auto(自动) / policy(店级静默)` | `storage/db.py:412-423` |
| C-8 | ★ 计划书⑤ **完全未定义 `review_materials` 表** | `docs/计划书05_点评自动回复.md:134-154`（§4.1 数据模型只有 `reviews` + `review_replies`） | `review_materials` 实际存在（10 列，`UNIQUE(hotel_id, collect_date, kind)`） | `storage/db.py:428-444` |
| C-9 | ★ `kind TEXT NOT NULL, -- score / competitor / trend`（建表列注释）；块注释亦只列 3 类 | `storage/db.py:434`、`:426-427` | 采集器遍历 `sources["scores"]` **全部键**，实际写 **4 类**：`score` / `competitor` / `trend` / **`num`**；实库 `kind=num rows=4` | `collectors/comment_collector.py:330-331`；`config/review_sources.json:83`；实库实测 |
| C-10 | 计划书⑤ §2.2 范围表只列 `getCommentsScoreV2` + `getCompetitorCommentStat` | `docs/计划书05_点评自动回复.md:100` | 实际还采 `getCommentRateTrend`（`trend`）与 `getCommentNumV2`（`num`）；`config/review_sources.json` 的 `_comment` 也只提「score/对比/趋势/计数素材」未点名 num | `config/review_sources.json:56,74,83` |
| C-11 | `getCommentRateTrend`…表单字符串 `"month=6"`（eng-cli 表单通道发送） | `docs/采集域盘点表-点评.md:30`、`config/api_rules.json:6067` | 一致；但**「表单通道」是由 `body` 是 `str` 隐式决定的**，配置里的 `"form": true`（`config/review_sources.json:81`）**采集器根本没读** | `collectors/comment_collector.py:341-350` |
| C-12 | 计划书⑤ `_DEFAULT_POLICY = {...}` 相关：`"rules": {"good_min_star": 4, "bad_max_star": 3, "reply_interval_s": 120}` | `docs/计划书05_点评自动回复.md:171` | `classify_sentiment` 默认值一致：`rules.get("good_min_star", 4)` / `rules.get("bad_max_star", 3)` | `app/review_reply.py:88,90` |
| C-13 | 计划书⑤ 未记载 `addtime` 的**时区偏移被正则吞掉但未使用** | `docs/计划书05_点评自动回复.md` 全篇 | 正则 `r"/Date\((\d+)(?:[+-]\d{4})?\)/"` 丢弃 `+0800`；`datetime.fromtimestamp(ms/1000)` 用**宿主机本地时区** | `collectors/comment_collector.py:49,65` |
| C-14 | 计划书⑤ 未记载 `commentId` 缺失指纹的**四元组顺序与原始值语义** | `docs/计划书05_点评自动回复.md` 全篇 | `"|".join(str(v or "") for v in (star, user_name, content, comment_time))` → `"h" + sha1(...)[:16]`；`comment_time` 传的是**原始 `addtime` 串**而非解析结果 | `collectors/comment_collector.py:52-55,297-300` |
| C-15 | 计划书⑤ / 盘点表 未记载「平台拒答 `resStatus.rcode` 校验」 | — | 代码显式校验 `rcode not in (None, 0, 200)` → 抛错；**但只作用于 `pending`，`collect_scores` 无此校验** | `collectors/comment_collector.py:207-212` vs `:337-353` |
| C-16 | 计划书⑤ 未记载点评采集的 `notes` / `pages` 返回字段 | `docs/计划书05_点评自动回复.md:212` | 实际返回 `{"status","rows","pages","notes","errors"}` | `collectors/comment_collector.py:315-316` |
| C-17 | ★ `status TEXT DEFAULT 'ok',            -- ok / degraded / failed`（声明枚举 3 值） | `storage/db.py:438` | 采集器实际写入第 4 个值 **`no_data`**：`status = "ok" if payload else "no_data"` | `collectors/comment_collector.py:358` |
| C-18 | 计划书⑤ `reviews` 未记载 `fetched_at` 在**每次冲突更新时被刷新** | `docs/计划书05_点评自动回复.md:141` | `DO UPDATE SET ... fetched_at=datetime('now','localtime')` —— 采集重跑会刷新该列 | `storage/db.py:1188` |

### 6.4 漂移 D：DDL 对照结论

| # | 项 | 结论 |
|---|---|---|
| D-1 | `portal_columns` PRAGMA vs `storage/db.py:312-327` | ✅ **无差异**（13 列全一致） |
| D-2 | `room_states` PRAGMA vs `storage/db.py:333-348` | ✅ **无差异**（13 列全一致） |
| D-3 | `reviews` PRAGMA vs `storage/db.py:393-406` | ✅ **无差异**（11 列全一致） |
| D-4 | `review_materials` PRAGMA vs `storage/db.py:430-442` | ✅ **结构无差异**（10 列全一致） |
| D-5 | ★ `review_materials.kind` **注释口径 vs 实际写入** | ⚠️ **有差异**：注释写 3 类（`score / competitor / trend`，`storage/db.py:427,434`），实际写 4 类（含 `num`，实库 `kind=num rows=4`）。重写时应把注释改为 `score / competitor / trend / num` |
| D-6 | ★ `reviews` **计划书⑤ §4.1 定义 vs 实际建表** | ⚠️ **有差异**：计划书缺 `comment_time` 列、`sentiment` 注释缺 `unknown`。以 `storage/db.py:393-406` 为准 |
| D-7 | ★ `review_replies` 计划书⑤ vs 实际建表 | ⚠️ **有差异**：NOT NULL 与 DEFAULT 不同（见 C-7）。以 `storage/db.py:412-423` 为准 |
| D-8 | 计划书④ §0 的 4 张预警表 DDL vs `storage/db.py` | ✅ 仅 1 处**标点**差异（B-5 缺右括号），其余逐字一致（`docs/计划书04_执行清单.md:15-48` vs `storage/db.py:312-348`） |

---

## 7. 重写实现检查清单（从上述事实直接导出）

### 7.1 预警三源

1. 四接口全 POST + `application/json`；热点日历 **GET** 且**不带 `content-type`**。
2. 所有调用用 `allow_missing=True` 语义 —— 单接口失败**只记 error 不中断**，缺字段落 `value=''`。
3. `home_pending` 的数值列缺字段落 **`0`**（不是空串）——两源归一化语义**相反**，不可统一。
4. `channel_qunar.competitor_total` 与 `channel_ctrip.competitor_total` **来源接口不同**。
5. `rating_avg` **携程/去哪儿同值**，去哪儿侧要写 `detail.note`。
6. `getServiceData` 的 `startDate`/`endDate` 需**注入**（模板 body 无此键），窗口 = 今日单日。
7. `indexType` 比较必须 **`str()` 双向转换**，取 `avgComp`。
8. 热点日历：分组键 `holiName`，首日 = `min(holiDate)`（ISO 字符串字典序），`detail` 四键 `{end_date, lead_days, holiday, real_holiday}`，**无 `source_api`**；`holiday`/`real_holiday` 要 `bool()` 强转；组元数据取**首个**事件。
9. UPSERT 键 = `(hotel_id, collect_date, page, column_name)`，冲突覆盖 7 列，`id`/`created_at` 不变。
10. `detail` 用 `json.dumps(..., ensure_ascii=False)`。

### 7.2 房态

1. 两次 POST，body 见 §2.2；**无分页**。
2. 房型名 4 级回退 + `html.unescape`；`payType` 缺省 `"PP"`；`roomClass` 缺省回退 `roomTypeID`。
3. ★ 判定式原样保留：`all_g = all(s == "G" for s in statuses)` → `available = 1 if all_g else 0`；**`canUsedQuantity` 绝不参与 `available` 判定**。
4. `quantity` = 组内 **max**；`status_code` = **首个非 `G`**（无则 `"G"`）；`price` = 多 ratePlan **min**（严格 `<` 才覆盖）。
5. 聚合键 = `(roomTypeID, effectDate)`；任一为 `None` → 丢弃该条。
6. ★ 落库 = **单事务内先 `DELETE FROM room_states WHERE hotel_id=? AND collect_date=?` 再批量 INSERT**；不是 UPSERT。
7. ★ 降级路径（两接口任一失败/房型为空）**不写库** → 旧数据保留，不被清空。
8. `available` 写入前 `int(bool(...))`；`room_type_id` 写入前 `str(... or "")`。

### 7.3 点评

1. ★ **端点/字段/分页全部配置化** —— 代码中不得硬编码点评 URL。
2. 星级路径 **`score.avgScoreSimple`**（对象内字段），转 `int(float(...))`；兜底 `score.commentLevel` → `{"好评":"good","差评":"bad"}`。
3. `addtime` 正则原样：`r"/Date\((\d+)(?:[+-]\d{4})?\)/"`，**`search` 而非 `match`**；`fromtimestamp(ms/1000)` 受宿主机时区影响（重写需显式确认时区策略）。
4. `commentId` 缺失指纹：`"h" + sha1("|".join(str(v or "") for v in (star, user_name, content, comment_time)))[:16]`，其中 `comment_time` 用**原始 `addtime` 串**。
5. 只采 `catalogTab="NotFeedBack"`；`pageIndex` 从 **1** 起，最多 **20** 页；停止条件 `len(items) < 20 or page >= pageCount`。
6. 平台拒答校验 `rcode not in (None, 0, 200)` → 报错（理想上 4 类素材都应校验，旧代码只对 pending 做了）。
7. ★ `reviews` UPSERT：`INSERT` 列清单**不含 `replied`**；`DO UPDATE SET` **不含 `replied`、不含 `strategy`** → 已回复不回溯。
8. `review_materials` UPSERT 键 = `(hotel_id, collect_date, kind)`，覆盖 5 列；`payload_json` **NOT NULL**（`None` → 字面串 `"null"`）。
9. ★ `kind` 实际有 **4** 类：`score` / `competitor` / `trend` / `num`；`num` 的 `payload_path=""` → **整包落库**。
10. 通道判定靠 `body` **类型**：`dict` → JSON，`str` → 表单；配置里的 `"form": true` 是**无效键**（保留兼容，但不是驱动逻辑）。

---

## 8. 证据索引（文件 → 行号速查）

| 主题 | 文件 | 行号 |
|---|---|---|
| 渠道四接口端点 | `collectors/portal_columns.py` | 49-65 |
| referer 常量 | `collectors/portal_columns.py` | 70-78 |
| 模块记录常量（审核/违约） | `collectors/portal_columns.py` | 80-86 |
| `source_api` 常量 | `collectors/portal_columns.py` | 88-97 |
| `_SoaBody` | `collectors/portal_columns.py` | 100-132 |
| `_to_str` | `collectors/portal_columns.py` | 135-137 |
| `_index_type_avg_comp` | `collectors/portal_columns.py` | 154-164 |
| `collect_channel` | `collectors/portal_columns.py` | 297-353 |
| `collect_home_pending` | `collectors/portal_columns.py` | 358-398 |
| `_audit_pending` / `_violation_pending` / `_module_latest` | `collectors/portal_columns.py` | 400-431 |
| `collect_hot_calendar` | `collectors/portal_columns.py` | 436-472 |
| `collect_all` / `_call` | `collectors/portal_columns.py` | 477-516 |
| 房态两接口端点 | `collectors/room_state.py` | 42-46 |
| `_decode_html` / `_repr_status` | `collectors/room_state.py` | 49-61 |
| `_rc_products` / `_build_dto` | `collectors/room_state.py` | 135-167 |
| `_room_inventory` | `collectors/room_state.py` | 169-180 |
| `collect` | `collectors/room_state.py` | 182-217 |
| `_price_map` / `_build_rows` | `collectors/room_state.py` | 222-275 |
| `_DATE_RE` / `review_id_fallback` / `parse_addtime` | `collectors/comment_collector.py` | 48-68 |
| `_render_body` | `collectors/comment_collector.py` | 71-82 |
| `_fetch_source_page` | `collectors/comment_collector.py` | 195-223 |
| `_sentiment_of` | `collectors/comment_collector.py` | 225-242 |
| `collect_pending` | `collectors/comment_collector.py` | 244-316 |
| `collect_scores` | `collectors/comment_collector.py` | 321-364 |
| `_dig` / `_tostr` | `collectors/comment_collector.py` | 391-420 |
| `portal_columns` DDL | `storage/db.py` | 308-329 |
| `room_states` DDL | `storage/db.py` | 330-350 |
| `reviews` DDL | `storage/db.py` | 389-408 |
| `review_materials` DDL | `storage/db.py` | 426-444 |
| 索引 | `storage/db.py` | 456-468 |
| `query_module_records` 排序 | `storage/db.py` | 850-877 |
| `save_portal_column` | `storage/db.py` | 971-1002 |
| `save_room_states` | `storage/db.py` | 1036-1061 |
| `upsert_review` | `storage/db.py` | 1169-1195 |
| `mark_review_replied` | `storage/db.py` | 1243-1259 |
| `save_review_material` | `storage/db.py` | 1321-1346 |
| `classify_sentiment` / `load_review_sources` | `app/review_reply.py` | 64-92 |
| `json_get` | `collectors/rules.py` | 379-400 |
| `window_date_ctx` / `compute_window_range` | `collectors/api_collector.py` | 140-201 |
| `getCommentsScoreV2` body 模板 | `config/api_rules.json` | 197-209 |
| `getServiceData` body 模板 | `config/api_rules.json` | 1437-1498 |
| `getServiceData` 响应实锤 | `config/responses/competitionprofile.json` | 8 |
| 点评端点全量配置 | `config/review_sources.json` | 1-118 |
| 盘点表（预警） | `docs/采集域盘点表-预警.md` | 1-92 |
| 盘点表（点评） | `docs/采集域盘点表-点评.md` | 1-184 |
| 计划书④ 执行清单 | `docs/计划书04_执行清单.md` | 1-210 |
| 计划书⑤ | `docs/计划书05_点评自动回复.md` | 1-299 |
| 房态单测 | `tests/test_room_state.py` | 169-296 |
| 预警三源单测 | `tests/test_portal_columns.py` | 150-313 |

---

*规格-批次D提取器完 —— 全部结论带 `文件:行号`；四张表 DDL 已与 `db/ebooking.db` 的 `PRAGMA` 实测对照（§4.5 / §6.4）；实际数据抽样见 §5。旧系统全程只读，`db/ebooking.db` 以 `mode=ro` 打开。*
