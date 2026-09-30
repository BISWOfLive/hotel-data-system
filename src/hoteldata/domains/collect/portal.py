"""预警三源提取器(T4.1)—— 渠道 / 首页待办 / 热点日历。

**规格依据**:``docs/参考/旧系统/规格-批次D提取器.md`` §1(逐字考古)+ §7.1(重写检查清单)。
旧实现 ``collectors/portal_columns.py``(533 行,类名是 ``PortalCollector`` —— 计划书写的
``PortalCollectorApi`` **不存在**,规格 §6.1 漂移 A-1)。

三源与落库页
============  =============================================================  ================
源            page                                                           列数
============  =============================================================  ================
渠道          ``channel_ctrip`` / ``channel_qunar``                           8 + 6
首页待办      ``home_pending``                                                5
热点日历      ``hot_calendar``                                                ``holiName`` 动态
============  =============================================================  ================

**返回契约(本模块从任务书给的两案中选定的那一种,调用方按此落库)**
------------------------------------------------------------------
**每个 ``page`` 一条 :class:`ExtractResult`,`records`` 是该 page 的多行**;
行键 = ``alert_portal_columns`` 的列名(``hotel_id`` / ``account_id`` / ``collect_date`` /
``page`` / ``column_name`` / ``value`` / ``detail_json`` / ``raw_json_path`` /
``channel`` / ``status`` / ``error``)。调用方逐条
:meth:`~hoteldata.domains.collect.repository.CollectRepository.upsert_portal_columns`。

→ 选它的理由:一源的失败不能拖累另一源的落库。若把三源塞进一条 ``ExtractResult``,
调用方拿到一个 ``degraded`` 就无从判断"哪几行可信",而 UPSERT 是**逐行**幂等的,
按 page 拆开正好对齐同一个事务里"能写的照写"。故本模块返回 **4 条**结果
(``channel_ctrip`` / ``channel_qunar`` / ``home_pending`` / ``hot_calendar``)。

★ **三源全部 ``allow_missing=True``**(旧模块**从未**用过 ``allow_missing=False``,
那是保留的严格模式开关):单个接口失败**只记 error 不中断** ——
``errors.append(f"{name}: {exc}")`` + warning 日志 + 返回 ``None``,
`json_get(None, path)` → ``None`` → 该列照常落库(值见下)。本源 ``status="degraded"``。

★ **两源归一化语义相反,绝不可统一**(规格 §7.1 检查项 3):

  * 渠道源缺字段 → ``value=""``(**空串**,旧 ``_to_str``);
  * ``home_pending`` 数值列缺字段 → ``value=0``(旧 ``0 if X is None else int(X)``)。
    (注意:落库时数值列也走 ``str()`` → 存的是 ``"0"``;TEXT 列要自行 cast。)

★ **错误按 page 分桶归因**:渠道四接口里 ``queryHotelMinPriceV1`` **只喂携程页**,
所以它挂了**不该**把去哪儿页也标 ``degraded``(见
:meth:`PortalExtractor._collect_channel` 的对照表)。

★ **``value`` 一律 ``str()`` 落 TEXT**(旧系统实测):同一列承载整数计数、浮点评分、
ISO 日期串与缺失占位。``None`` 一律变 ``""`` —— **代码路径上永不写 NULL**。

★ 两个 ``competitor_total`` **来源接口不同**(漂移 A-8):携程取
``queryHotelMinPriceV1.data.competitorHotelTotal``,去哪儿取
``getCommentsScoreV2.data.competitorHotelTotal`` —— **不可合并**。
``rating_avg`` 则是携程/去哪儿**共用同值**(``getServiceData`` 的 ``indexType==12`` 的
``avgComp``),去哪儿侧额外写 ``detail.note`` 说明。
"""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import date, timedelta
from typing import Any

from loguru import logger

from hoteldata.domains.collect.contract import (
    ApiCollectError,
    ExtractContext,
    ExtractResult,
    ExtractStatus,
    ExtractTarget,
    LoginExpiredError,
    PlaceholderError,
)
from hoteldata.domains.collect.jsonpath import json_get
from hoteldata.domains.collect.rules import get_api_rules
from hoteldata.domains.collect.windows import (
    WINDOW_REALTIME,
    WINDOW_TODAY_REALTIME,
    window_date_ctx,
)
from hoteldata.infra.atomic import atomic_write_json

__all__ = ["PortalExtractor"]

# ---------------------------------------------------------------------------
# 端点与 referer(旧 portal_columns.py:49-78 逐字抄录)
# ---------------------------------------------------------------------------

_FETCH_VISITOR = "https://ebooking.ctrip.com/datacenter/api/dataCenter/current/fetchVisitorTitleV2"
_QUERY_MIN_PRICE = "https://ebooking.ctrip.com/datacenter/api/dataCenter/current/queryHotelMinPriceV1"
_GET_COMMENTS_SCORE = "https://ebooking.ctrip.com/datacenter/api/dataCenter/comment/getCommentsScoreV2"
_GET_SERVICE_DATA = "https://ebooking.ctrip.com/restapi/soa2/24588/getServiceData"
_GET_COMMENT_FAQ_COUNT = "https://ebooking.ctrip.com/restapi/soa2/26353/getCommentAndFAQNeedFeedBackCount"
_QUERY_GROWTH_TASK = "https://ebooking.ctrip.com/restapi/soa2/23958/queryPendingHotelGrowthTaskListV2"
_GET_HOT_EVENT = "https://ebooking.ctrip.com/ebkovsroom/api/inventory/getHotelHotEvent"

_REF_CHANNEL = "https://ebooking.ctrip.com/datacenter/inland/businessreport/outline?microJump=true"
_REF_COMPETITION = "https://ebooking.ctrip.com/ebkgrowth/datacenter/competition/competitionprofile"
_REF_COMMENT_LIST = "https://ebooking.ctrip.com/comment/commentList?microJump=true"
_REF_HOME = "https://ebooking.ctrip.com/home?microJump=true"
_REF_CALENDAR = "https://ebooking.ctrip.com/ebkovsroom/inventory/calendar?microJump=true"

#: ``detail["source_api"]`` 的枚举常量(旧 portal_columns.py:88-97 逐字)
_SRC_VISITOR = "fetchVisitorTitleV2"
_SRC_MIN_PRICE = "queryHotelMinPriceV1"
_SRC_COMMENT = "getCommentsScoreV2"
_SRC_SERVICE = "getServiceData"
_SRC_COMMENT_FAQ = "getCommentAndFAQNeedFeedBackCount"
_SRC_GROWTH = "queryPendingHotelGrowthTaskListV2"
_SRC_HOT_EVENT = "getHotelHotEvent"
_SRC_AUDIT = "module:审核记录"
_SRC_VIOLATION = "module:违约看板/违规中心"

# ---------------------------------------------------------------------------
# 落库 page / 窗口标签
# ---------------------------------------------------------------------------

_PAGE_CTRIP = "channel_ctrip"
_PAGE_QUNAR = "channel_qunar"
_PAGE_HOME = "home_pending"
_PAGE_CALENDAR = "hot_calendar"

#: 热点日历的窗口标签 —— **只是展示标签**,不是 9 个规则窗口之一(接口区间为 today~+60)
_WINDOW_HOT_CALENDAR = "未来60天"

_MODULE = "portal"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125 Safari/537.36"
)
_FORM_CONTENT_TYPE = "application/x-www-form-urlencoded; charset=UTF-8"

#: 官网登录页路径标记(302 判定用,旧 api_collector.py:35 逐字)
_LOGIN_URL_MARKERS = ("login", "passport", "signin", "sign_in", "auth", "sso")
#: 响应体里的未登录标记(旧 api_collector.py:37 逐字)
_LOGIN_BODY_MARKERS = ("未登录", "请重新登录", "登录失效")

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")

# ---------------------------------------------------------------------------
# 首页待办的模块记录来源(旧 portal_columns.py:80-86 逐字)
# ---------------------------------------------------------------------------

_AUDIT_MODULE = "审核记录"  # 挂牌管理 / 昨日
_AUDIT_WINDOW = "昨日"
_AUDIT_PAYLOAD_KEY = "公示内容"
_VIOLATION_MODULE = "违约看板/违规中心"  # 商机中心 / 实时
_VIOLATION_WINDOW = "实时"
_VIOLATION_PAYLOAD_KEY = "违约记录数"

#: 注入式「取模块最新一条」回调:``(module, window) -> 模块记录 | None``。
#: ``window=None`` 表示只按 module 过滤(旧 ``_module_latest`` 的第二级回退);
#: 返回值可以是模块记录 **dict** 或 ``CollectModule`` **ORM 对象**,也可以是 awaitable
#: (实现方要走 DB,见 ``CollectRepository.latest_module``)。
type ModuleRecord = Mapping[str, Any]
type ModuleLookup = Callable[[str, str | None], ModuleRecord | Awaitable[Any] | None]


class _SoaBody:
    """SOA2 接口的标准 ``reqHead`` 包裹体(旧 portal_columns.py:100-132 逐字)。

    ★ ``reqHead`` 与 ``head`` **同层**;键名拼写是 **``protocal``**(不是 ``protocol``,
    漂移 A-14,不可"顺手纠正")。

    ⚠️ 旧实现的 ``clientId`` / ``cid`` 是**硬编码常量**(漂移 A-13)。
    新实现保留"有个默认值"这个行为,但:

    * 把它提成参数,便于以后按账号维度配置;
    * ★ **默认值换成了全 0 占位符** —— 原值是从**真实浏览器会话**里捕获的
      客户端标识(``clientId`` / ``vid`` / ``fp``),属于**不入库的凭据类数据**。
      平台侧对这几个字段的校验是"格式对不对",不是"值必须等于某一次捕获";
      真要按账号区分时由调用方传 ``client_id``。
      段3/段1 的验收都是在**真实登录态**下跑的,所以换成占位符不影响链路。
    """

    _CLIENT_ID = "00000000000000000000"

    @classmethod
    def build(cls, path_name: str, *, client_id: str | None = None) -> dict[str, Any]:
        cid = client_id or cls._CLIENT_ID
        return {
            "reqHead": {
                "host": "ebooking.ctrip.com",
                "pathName": path_name,
                "locale": "zh-CN",
                "release": "",
                "client": {
                    "deviceType": "PC",
                    "os": "Windows",
                    "osVersion": "Windows 10",
                    "deviceName": "Windows PC",
                    "clientId": cid,
                    "screenWidth": 1600,
                    "screenHeight": 1000,
                    "isIn": {
                        "ie": False,
                        "chrome": True,
                        "chrome49": False,
                        "wechat": False,
                        "firefox": False,
                        "ios": False,
                        "android": False,
                    },
                    "isModernBrowser": True,
                    "browser": "Chrome",
                    "browserVersion": "151",
                    "platform": "pc",
                    "technology": "web",
                },
                "ubt": {
                    # ★ 占位符(长度/形态与原捕获值一致)——见类 docstring 的说明
                    "vid": "0000000000000.xxxxxxxxxxxx",
                    "fp": "00000A-00000B-00000C",
                    "rmsToken": "",
                },
                "gps": {"coord": "", "lat": "", "lng": "", "cid": 0, "cnm": ""},
                "protocal": "https:",  # ★ 平台原文拼写,不要改成 protocol
            },
            "head": {
                "cid": cid,
                "ctok": "",
                "cver": "1.0",
                "lang": "01",
                "sid": "8888",
                "syscode": "09",
                "auth": "",
                "xsid": "",
                "extension": [],
            },
        }


# ---------------------------------------------------------------------------
# 归一化小工具(旧 portal_columns.py:135-164 逐字)
# ---------------------------------------------------------------------------


def _to_str(value: Any) -> str:
    """统一 str 化;``None`` → 空串(**渠道源**的缺失语义)。"""
    return "" if value is None else str(value)


def _to_int(value: Any, *, errors: list[str], column: str) -> int:
    """**首页待办**的缺失语义:``None`` → ``0``。

    ★ 与 :func:`_to_str` **相反**,不可统一(规格 §7.1 检查项 3)。
    旧实现直接 ``int(x)``,平台一旦回 ``"12.5"`` 之类的非整数字符串会**炸掉整源**;
    这里降级为「记 error + 落 0」,与本模块「单接口失败不中断」的总口径一致。
    """
    if value is None:
        return 0
    try:
        return int(value)
    except TypeError, ValueError:
        errors.append(f"{column} 不是整数({value!r}),按 0 落库")
        return 0


def _index_type_avg_comp(body: Any, index_type: Any) -> Any:
    """从 ``getServiceData`` 响应取 ``dataList`` 中 ``indexType == index_type`` 的 ``avgComp``。

    ``indexType`` 可能为 int 或 str,**统一按 ``str()`` 双向比较**;取不到返回 ``None``。
    ★ 返回键是 ``avgComp`` **不是** ``val``(旧 portal_columns.py:154-164 逐字)。
    """
    if not isinstance(body, dict):
        return None
    for item in body.get("dataList") or []:
        if isinstance(item, dict) and str(item.get("indexType")) == str(index_type):
            return item.get("avgComp")
    return None


def _error_buckets(errors: list[str] | Sequence[list[str]]) -> list[list[str]]:
    """把「一个错误列表」或「多个错误列表」统一成多个桶(渠道源按 page 归因用)。

    判定靠**首元素类型**:``[]`` 或 ``["a: b"]`` → 单桶;``[[...], [...]]`` → 多桶。
    渠道源的 ``channel_ctrip`` / ``channel_qunar`` 共用四个接口中的三个,
    各自还独享一个(携程独享 ``queryHotelMinPriceV1``),所以错误必须分桶记,
    否则"携程的价格接口挂了"会把去哪儿页也标成 ``degraded``。
    """
    if isinstance(errors, list) and (not errors or isinstance(errors[0], str)):
        return [errors]
    # ★ 不要把桶**复制**一份:必须写回调用方持有的那些 list
    return list(errors)


def _guard_placeholders(name: str, payload: Any) -> None:
    """占位符残留守卫(旧 t12,防「静默错数据」)。

    渲染后的 body/params 里**还有 ``{xxx}``** → 说明模板没被上下文填满,
    带着占位符原文请求平台可能拿到语义错误的 200 响应 → 抛
    :class:`PlaceholderError`(属 :class:`ApiCollectError`),由调用方按
    ``allow_missing`` 语义降级该源。
    """
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    residual = _PLACEHOLDER_RE.findall(text)
    if residual:
        raise PlaceholderError(
            f"{name}: 请求体渲染后仍残留占位符 {{{residual[0]}}}(视为未校准,不带占位符原文请求平台)"
        )


class PortalExtractor:
    """预警三源提取器(``name = "portal"``,T4.1)。

    **不做判定**:本模块只采集与落库,预警判定归段2 的预警引擎
    (旧 portal_columns.py:9「本模块只采集与落库,不做判定」)。
    """

    name = "portal"

    async def extract(self, ctx: ExtractContext, **kwargs: Any) -> list[ExtractResult]:
        """跑三源,返回 **4 条** ``ExtractResult``(每个 page 一条)。

        ``kwargs``
        ----------
        ``latest_module`` : :data:`ModuleLookup`,可选
            ``home_pending`` 的 ``audit_pending`` / ``violation_pending`` 是**模块记录派生**
            的(不调接口,旧 portal_columns.py:400-431),需要一个「取模块最新一条」的回调。
            典型注入::

                lambda module, window: repo.latest_module(ctx.hotel_id, module, window)

            ``ExtractContext`` 里**没有 DB 会话**(提取器只拿 HTTP/会话/限频/布局),
            所以这个读取能力必须由调用方注入。
            **未注入时**两列按旧实现的异常兜底落 ``0``,并把
            ``"audit_pending 读取失败: ..."`` 记进 errors → 本源 ``degraded``
            (旧语义:读失败 → 记 error + 返回 0;无记录 → 0 且不报错)。

        ``client_id`` : ``str``,可选
            SOA 包裹体的 ``clientId`` / ``cid``(默认沿用平台捕获值,见 :class:`_SoaBody`)。
        """
        latest_module: ModuleLookup | None = kwargs.get("latest_module")
        client_id = kwargs.get("client_id") or _SoaBody._CLIENT_ID

        results: list[ExtractResult] = []
        results.extend(await self._collect_channel(ctx, client_id=client_id))
        results.append(await self._collect_home_pending(ctx, latest_module, client_id=client_id))
        results.append(await self._collect_hot_calendar(ctx))
        return results

    # ==================================================================
    # ① 渠道源(旧 collect_channel,portal_columns.py:297-353)
    # ==================================================================

    async def _collect_channel(self, ctx: ExtractContext, *, client_id: str) -> list[ExtractResult]:
        """渠道四接口 → ``channel_ctrip``(8 列) + ``channel_qunar``(6 列)。

        四接口**全部 POST + ``application/json``**;``getServiceData`` 的 body 来自
        ``config/api_rules.json`` 的模板并**注入** ``startDate`` / ``endDate``
        (窗口 = 「今日实时」单日)。

        ★ **错误按 page 分桶归因**(两页共用一条结果就会"一个接口挂了、两页都 degraded"):

        ====================  ==============  ==================================
        接口                  影响 page        原因
        ====================  ==============  ==================================
        ``fetchVisitorTitle`` 携程 + 去哪儿    两页都有 ``visitor_total``/``visitor_avg``
        ``queryHotelMinPrice`` **仅携程**      携程 ``competitor_total`` 的唯一来源(A-8)
        ``getCommentsScoreV2`` 携程 + 去哪儿    两页的 ``ratingall``/``rating_rank``
        ``getServiceData``     携程 + 去哪儿    ``rating_avg`` **两页共用同值**
        ====================  ==============  ==================================
        """
        ctrip_errors: list[str] = []
        qunar_errors: list[str] = []
        both = (ctrip_errors, qunar_errors)
        only_ctrip = (ctrip_errors,)

        visitor_body, visitor_raw = await self._call(
            ctx,
            _SRC_VISITOR,
            _FETCH_VISITOR,
            _REF_CHANNEL,
            {},
            both,
            page=_PAGE_CTRIP,
            module=_SRC_VISITOR,
            window=WINDOW_TODAY_REALTIME,
        )
        min_price_body, min_price_raw = await self._call(
            ctx,
            _SRC_MIN_PRICE,
            _QUERY_MIN_PRICE,
            _REF_CHANNEL,
            {},
            only_ctrip,
            page=_PAGE_CTRIP,
            module=_SRC_MIN_PRICE,
            window=WINDOW_TODAY_REALTIME,
        )
        comments_body, comments_raw = await self._call(
            ctx,
            _SRC_COMMENT,
            _GET_COMMENTS_SCORE,
            _REF_CHANNEL,
            {},
            both,
            page=_PAGE_CTRIP,
            module=_SRC_COMMENT,
            window=WINDOW_TODAY_REALTIME,
        )
        service_data: dict[str, Any] | None = None
        service_raw: str | None = None
        try:
            service_body = self._service_data_body(ctx.collect_date)
        except ApiCollectError as exc:
            # 占位符守卫命中:与"接口失败"同一条降级路径(只记 error,不中断)
            for bucket in both:
                bucket.append(f"{_SRC_SERVICE}: {exc}")
            logger.warning("接口 {} 失败(继续): {}", _SRC_SERVICE, exc)
        else:
            service_data, service_raw = await self._call(
                ctx,
                _SRC_SERVICE,
                _GET_SERVICE_DATA,
                _REF_COMPETITION,
                service_body,
                both,
                page=_PAGE_CTRIP,
                module=_SRC_SERVICE,
                window=WINDOW_TODAY_REALTIME,
            )

        # ★ 携程/去哪儿共用同一个 rating_avg(getServiceData indexType==12 的 avgComp)
        rating_avg = _index_type_avg_comp(service_data, 12)

        ctrip: list[tuple[str, Any, str, str | None]] = [
            ("visitor_total", json_get(visitor_body, "visitorTotal"), _SRC_VISITOR, visitor_raw),
            (
                "visitor_avg",
                json_get(visitor_body, "competitorAvgNumber"),
                _SRC_VISITOR,
                visitor_raw,
            ),
            ("min_price", json_get(min_price_body, "data.minPrice"), _SRC_MIN_PRICE, min_price_raw),
            (
                "min_price_rank",
                json_get(min_price_body, "data.minPriceRank"),
                _SRC_MIN_PRICE,
                min_price_raw,
            ),
            (
                "competitor_total",
                json_get(min_price_body, "data.competitorHotelTotal"),
                _SRC_MIN_PRICE,
                min_price_raw,
            ),
            (
                "ratingall",
                json_get(comments_body, "data.ctripRatingall"),
                _SRC_COMMENT,
                comments_raw,
            ),
            ("rating_avg", rating_avg, _SRC_SERVICE, service_raw),
            (
                "rating_rank",
                json_get(comments_body, "data.ctripRatingAllRanking"),
                _SRC_COMMENT,
                comments_raw,
            ),
        ]
        # ★ 去哪儿同名列 competitor_total 取自 getCommentsScoreV2(与携程**不同接口**,A-8)
        qunar: list[tuple[str, Any, str, str | None]] = [
            (
                "visitor_total",
                json_get(visitor_body, "qunarVisitorTotal"),
                _SRC_VISITOR,
                visitor_raw,
            ),
            (
                "visitor_avg",
                json_get(visitor_body, "qunarCompetitorAvgNumber"),
                _SRC_VISITOR,
                visitor_raw,
            ),
            (
                "ratingall",
                json_get(comments_body, "data.qunarRatingall"),
                _SRC_COMMENT,
                comments_raw,
            ),
            ("rating_avg", rating_avg, _SRC_SERVICE, service_raw),
            (
                "rating_rank",
                json_get(comments_body, "data.qunarRatingAllRanking"),
                _SRC_COMMENT,
                comments_raw,
            ),
            (
                "competitor_total",
                json_get(comments_body, "data.competitorHotelTotal"),
                _SRC_COMMENT,
                comments_raw,
            ),
        ]

        ctrip_rows = [
            self._row(ctx, _PAGE_CTRIP, col, _to_str(val), {"source_api": src}, raw)
            for col, val, src, raw in ctrip
        ]
        qunar_rows: list[dict[str, Any]] = []
        for col, val, src, raw in qunar:
            detail: dict[str, Any] = {"source_api": src}
            if col == "rating_avg":
                detail["note"] = "去哪儿复用携程评分均值(getServiceData avgComp),无独立均值"
            qunar_rows.append(self._row(ctx, _PAGE_QUNAR, col, _to_str(val), detail, raw))

        return [
            self._result(
                _PAGE_CTRIP,
                ctrip_rows,
                ctrip_errors,
                target_window=WINDOW_TODAY_REALTIME,
                raw_path=visitor_raw,
            ),
            self._result(
                _PAGE_QUNAR,
                qunar_rows,
                qunar_errors,
                target_window=WINDOW_TODAY_REALTIME,
                raw_path=visitor_raw,
            ),
        ]

    def _service_data_body(self, today: date) -> dict[str, Any]:
        """``getServiceData`` body:api_rules 模板 + **注入** ``startDate``/``endDate``。

        模板 body **内不含** ``startDate``/``endDate``(旧 portal_columns.py:270-276),
        二者由 ``window_date_ctx("今日实时", today)`` 提供 → 单日区间,格式 ``%Y-%m-%d``。
        """
        body = dict(self._api_def_body(_SRC_SERVICE) or {})
        ctx = window_date_ctx(WINDOW_TODAY_REALTIME, today)
        body["startDate"] = ctx["startDate"]
        body["endDate"] = ctx["endDate"]
        if not body["startDate"] or not body["endDate"]:
            raise PlaceholderError(
                f"{_SRC_SERVICE}: startDate/endDate 渲染为空串(绝不携带空日期请求平台,视为未校准)"
            )
        return body

    @staticmethod
    def _api_def_body(name: str) -> Any:
        """在 ``config/api_rules.json`` 全部页的 ``api_defs`` 中取首个同名 ``body``。

        旧 ``_api_def_body``(portal_columns.py:260-268)**跨页**扫描;规则不存在时返回 ``None``
        (body 缺失 → 空 body 请求,与旧实现一致)。
        """
        rules = get_api_rules()
        for page_cfg in rules.pages.values():
            found = page_cfg.api_def(name)
            if found is not None:
                return found.body
        logger.warning("api_rules 中找不到接口定义 {},将用空 body 请求", name)
        return None

    # ==================================================================
    # ② 首页待办源(旧 collect_home_pending,portal_columns.py:358-398)
    # ==================================================================

    async def _collect_home_pending(
        self, ctx: ExtractContext, latest_module: ModuleLookup | None, *, client_id: str
    ) -> ExtractResult:
        """两接口 + 两个模块派生列 → ``home_pending`` 共 **5** 列。

        ★ 数值列缺失归一为 **``0``**(不是空串);派生列读不到也落 0。
        """
        errors: list[str] = []

        faq_body, faq_raw = await self._call(
            ctx,
            _SRC_COMMENT_FAQ,
            _GET_COMMENT_FAQ_COUNT,
            _REF_COMMENT_LIST,
            _SoaBody.build("/comment/commentList", client_id=client_id),
            errors,
            page=_PAGE_HOME,
            module=_SRC_COMMENT_FAQ,
            window=WINDOW_REALTIME,
        )
        comment_pending = json_get(faq_body, "commentAndFAQNeedFeedBackCount.commentNeedFeedBackCount")
        qa_pending = json_get(faq_body, "commentAndFAQNeedFeedBackCount.hotelFAQNeedFeedBackCount")

        growth_body, growth_raw = await self._call(
            ctx,
            _SRC_GROWTH,
            _QUERY_GROWTH_TASK,
            _REF_HOME,
            _SoaBody.build("/home", client_id=client_id),
            errors,
            page=_PAGE_HOME,
            module=_SRC_GROWTH,
            window=WINDOW_REALTIME,
        )
        todo_more = json_get(growth_body, "count")
        if todo_more is None:
            # ★ 主备两个字段路径:先 count,为 None 才回退 totalCount(漂移 A-10)
            todo_more = json_get(growth_body, "totalCount")

        audit_pending = await self._audit_pending(latest_module, errors)
        violation_pending = await self._violation_pending(latest_module, errors)

        columns: list[tuple[str, int, str, str | None]] = [
            (
                "comment_pending",
                _to_int(comment_pending, errors=errors, column="comment_pending"),
                _SRC_COMMENT_FAQ,
                faq_raw,
            ),
            (
                "qa_pending",
                _to_int(qa_pending, errors=errors, column="qa_pending"),
                _SRC_COMMENT_FAQ,
                faq_raw,
            ),
            ("audit_pending", audit_pending, _SRC_AUDIT, None),
            ("violation_pending", violation_pending, _SRC_VIOLATION, None),
            ("todo_more", _to_int(todo_more, errors=errors, column="todo_more"), _SRC_GROWTH, growth_raw),
        ]
        rows = [
            self._row(ctx, _PAGE_HOME, col, str(val), {"source_api": src}, raw)
            for col, val, src, raw in columns
        ]
        return self._result(_PAGE_HOME, rows, errors, target_window=WINDOW_REALTIME, raw_path=faq_raw)

    async def _module_latest(self, latest_module: ModuleLookup | None, module: str, window: str) -> Any:
        """两级回退取模块最新一条(旧 ``_module_latest``,portal_columns.py:427-431)。

        先按 ``(module, window)`` 精确;为空才退化为只按 ``module``。
        **没有** ``collect_date`` 过滤 —— 可能取到历史任意一天的最新记录(旧口径)。
        返回模块记录原样(dict 或 ORM 对象),由 :meth:`_module_payload` 取 payload。
        """
        if latest_module is None:
            raise LookupError("未注入 latest_module 回调(ExtractContext 不含 DB 会话)")
        rec = latest_module(module, window)
        if inspect.isawaitable(rec):
            rec = await rec
        if rec:
            return rec
        rec = latest_module(module, None)
        if inspect.isawaitable(rec):
            rec = await rec
        return rec

    async def _audit_pending(self, latest_module: ModuleLookup | None, errors: list[str]) -> int:
        """模块「审核记录 / 昨日」最新 payload 的「公示内容」非空 → 1,否则 0。"""
        try:
            payload = await self._module_payload(latest_module, _AUDIT_MODULE, _AUDIT_WINDOW)
            return 1 if str(payload.get(_AUDIT_PAYLOAD_KEY) or "").strip() else 0
        except Exception as exc:  # noqa: BLE001 - 单源失败不阻断(旧实现同款兜底)
            errors.append(f"audit_pending 读取失败: {exc}")
            return 0

    async def _violation_pending(self, latest_module: ModuleLookup | None, errors: list[str]) -> int:
        """模块「违约看板/违规中心 / 实时」最新 payload 的「违约记录数」(原样取整数)。"""
        try:
            payload = await self._module_payload(latest_module, _VIOLATION_MODULE, _VIOLATION_WINDOW)
            val = payload.get(_VIOLATION_PAYLOAD_KEY)
            return 0 if val is None else int(val)
        except Exception as exc:  # noqa: BLE001 - 单源失败不阻断
            errors.append(f"violation_pending 读取失败: {exc}")
            return 0

    async def _module_payload(
        self, latest_module: ModuleLookup | None, module: str, window: str
    ) -> dict[str, Any]:
        """取模块最新一条的 payload(无记录 → ``{}``)。

        兼容两种记录形态:查询结果 dict(``payload`` / ``payload_json`` 键)与
        ``CollectModule`` ORM 对象(``payload_json`` 属性)。
        """
        rec = await self._module_latest(latest_module, module, window)
        if rec is None:
            return {}
        if isinstance(rec, Mapping):
            payload = rec.get("payload") or rec.get("payload_json")
        else:
            payload = getattr(rec, "payload_json", None) or getattr(rec, "payload", None)
        return dict(payload) if isinstance(payload, Mapping) else {}

    # ==================================================================
    # ③ 热点日历源(旧 collect_hot_calendar,portal_columns.py:436-472)
    # ==================================================================

    async def _collect_hot_calendar(self, ctx: ExtractContext) -> ExtractResult:
        """``getHotelHotEvent``(★ **GET**,不带 ``content-type``)→ 按 ``holiName`` 分组的首日。

        列 = 事件名(``holiName`` 原文),值 = 组内 **``min(holiDate)``**
        (ISO 串字典序最小值 ≡ 时间序最早;旧实现即靠字典序,见规格 §1.6)。
        """
        today = ctx.collect_date
        collect_date = today.strftime("%Y-%m-%d")
        end = (today + timedelta(days=60)).strftime("%Y-%m-%d")
        url = f"{_GET_HOT_EVENT}?startDate={collect_date}&endDate={end}"

        errors: list[str] = []
        body, raw_rel = await self._call(
            ctx,
            _SRC_HOT_EVENT,
            url,
            _REF_CALENDAR,
            None,
            errors,
            method="GET",
            page=_PAGE_CALENDAR,
            module=_SRC_HOT_EVENT,
            window=_WINDOW_HOT_CALENDAR,
        )

        events = (body or {}).get("data") or []
        # 分组键 = holiName;组级元数据取该名**首次出现**的那条事件(setdefault 语义)
        groups: dict[str, dict[str, Any]] = {}
        for ev in events:
            if not isinstance(ev, Mapping):
                continue
            name = ev.get("holiName")
            if not name:
                continue  # falsy(None/"")直接丢弃
            g = groups.setdefault(
                name,
                {"dates": [], "real_holiday": ev.get("realHoliday"), "holiday": ev.get("holiday")},
            )
            d = ev.get("holiDate")
            if d:
                g["dates"].append(d)

        rows: list[dict[str, Any]] = []
        for name, g in groups.items():
            dates: list[str] = g["dates"]
            if not dates:
                continue  # 无日期组:**跳过,不落行**
            first = min(dates)
            end_date = max(dates)
            try:
                lead_days = (date.fromisoformat(first) - today).days
            except TypeError, ValueError:
                errors.append(f"{name}: holiDate 非法({first!r}),跳过该事件")
                continue
            detail = {
                # ★ 本源的 detail **没有 source_api**(drift A-11 / 检查清单 §7.1-8)
                "end_date": end_date,
                "lead_days": lead_days,
                "holiday": bool(g["holiday"]),
                "real_holiday": bool(g["real_holiday"]),
            }
            rows.append(self._row(ctx, _PAGE_CALENDAR, str(name), _to_str(first), detail, raw_rel))

        return self._result(
            _PAGE_CALENDAR, rows, errors, target_window=_WINDOW_HOT_CALENDAR, raw_path=raw_rel
        )

    # ==================================================================
    # 底层调用(旧 _call / _get / _post_json,portal_columns.py:207-250,502-516)
    # ==================================================================

    async def _call(
        self,
        ctx: ExtractContext,
        name: str,
        url: str,
        referer: str,
        body: dict[str, Any] | None,
        errors: list[str] | Sequence[list[str]],
        *,
        allow_missing: bool = True,
        method: str = "POST",
        page: str = _MODULE,
        module: str = "",
        window: str = WINDOW_TODAY_REALTIME,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """单接口调用;``allow_missing=True`` 时失败**只记 error**并返回 ``(None, None)``。

        ``errors`` 可以是**一个**错误列表,也可以是**多个**(渠道源按 page 分桶归因:
        一个接口可能只影响携程页,不该把去哪儿页也标成 ``degraded``)。

        ★ 本模块三源**全部**传 ``allow_missing=True``(旧实现亦然,``allow_missing=False``
        是从未被使用的严格模式开关),因此单接口失败不会中断本源的其余列。
        返回 ``(响应 dict, 原始响应相对路径)``;响应不是 dict → 返回 ``{}``(旧语义)。
        """
        try:
            data, raw_rel = await self._request(
                ctx, name, url, referer, body, method=method, page=page, module=module, window=window
            )
            return (data if isinstance(data, dict) else {}), raw_rel
        except ApiCollectError as exc:
            for bucket in _error_buckets(errors):
                bucket.append(f"{name}: {exc}")
            if not allow_missing:
                raise
            logger.warning("接口 {} 失败(继续): {}", name, exc)
            return None, None

    async def _request(
        self,
        ctx: ExtractContext,
        name: str,
        url: str,
        referer: str,
        body: dict[str, Any] | None,
        *,
        method: str,
        page: str,
        module: str,
        window: str,
    ) -> tuple[Any, str | None]:
        """真实网络请求(**必须包在限频里**,平台级 0.6s 是风控红线)。"""
        method = method.upper()
        if body is not None:
            _guard_placeholders(name, body)
        headers = self._headers(ctx, referer, method=method, json_body=body is not None)
        async with ctx.limiter.request(ctx.platform, ctx.account.alias):
            attempt = await ctx.http.request(
                method,
                url,
                headers=headers,
                json_body=body if (method == "POST" and body is not None) else None,
                timeout=ctx.api_timeout_s,
                follow_redirects=False,  # ★ 要看到 302 才能判登录失效
            )
        self._raise_for_attempt(name, attempt)
        data = attempt.json()
        if data is None:
            raise ApiCollectError(f"{name}: 响应非 JSON({attempt.brief()})")
        raw_rel = self._dump_raw(ctx, page=page, module=module, window=window, api_name=name, data=data)
        return data, raw_rel

    @staticmethod
    def _headers(ctx: ExtractContext, referer: str, *, method: str, json_body: bool) -> dict[str, str]:
        """请求头(旧 portal_columns.py:213-223 逐字;GET **不带** ``content-type``)。"""
        try:
            cookie = ctx.session.cookie_header()
        except Exception as exc:  # noqa: BLE001 - SessionRef 只承诺"无 cookie 抛异常"
            raise LoginExpiredError(f"取 cookie 失败: {exc}") from exc
        headers = {
            "cookie": cookie,
            "user-agent": _USER_AGENT,
            "referer": referer or "",
            "x-requested-with": "XMLHttpRequest",
        }
        if method.upper() == "POST":
            headers["content-type"] = "application/json" if json_body else _FORM_CONTENT_TYPE
        return headers

    @staticmethod
    def _raise_for_attempt(name: str, attempt: Any) -> None:
        """响应校验:登录失效 → ``LoginExpiredError``;其余非 2xx → ``ApiCollectError``。"""
        status = attempt.status_code or 0
        location = (attempt.location() or "").lower()
        if status in (401, 403):
            raise LoginExpiredError(f"{name}: HTTP {status}(登录态失效)")
        if status in (301, 302, 303, 307, 308) and any(m in location for m in _LOGIN_URL_MARKERS):
            raise LoginExpiredError(f"{name}: 被重定向到登录页({location[:120]})")
        if not attempt.is_success:
            raise ApiCollectError(f"{name}: HTTP {status} {attempt.error or ''}".strip())
        text = attempt.text or ""
        if any(m in text for m in _LOGIN_BODY_MARKERS):
            raise LoginExpiredError(f"{name}: 响应体提示未登录")

    @staticmethod
    def _dump_raw(
        ctx: ExtractContext,
        *,
        page: str,
        module: str,
        window: str,
        api_name: str,
        data: Any,
    ) -> str | None:
        """逐接口原始响应落盘(**入库只存相对路径**;失败不影响采集)。"""
        if not ctx.persist_raw:
            return None
        try:
            path = ctx.layout.raw_api_path(ctx.hotel.name, ctx.collect_date, page, module, window, api_name)
            atomic_write_json(path, data)
            return ctx.layout.to_relative(path)
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("原始响应落盘失败({}): {}", api_name, exc)
            return None

    # ==================================================================
    # 行 / 结果构造
    # ==================================================================

    @staticmethod
    def _row(
        ctx: ExtractContext,
        page: str,
        column_name: str,
        value: str,
        detail: dict[str, Any] | None,
        raw_json_path: str | None,
    ) -> dict[str, Any]:
        """一行 ``alert_portal_columns``(行键 = 表列名,调用方直接 UPSERT)。"""
        return {
            "hotel_id": ctx.hotel_id,
            "account_id": ctx.account_id,
            "collect_date": ctx.collect_date,
            "page": page,
            "column_name": column_name,
            "value": value,
            "detail_json": detail,
            "raw_json_path": raw_json_path,
            "channel": "api",
            "status": "ok",
            "error": None,
        }

    @staticmethod
    def _result(
        page: str,
        rows: Sequence[dict[str, Any]],
        errors: Sequence[str],
        *,
        target_window: str,
        raw_path: str | None,
    ) -> ExtractResult:
        """本源结果:``errors`` 非空 → ``degraded``(其余照常落库),否则 ``ok``。"""
        status: ExtractStatus = "degraded" if errors else "ok"
        err_text = "; ".join(errors) if errors else None
        return ExtractResult(
            status=status,
            channel="api",
            records=list(rows),
            error=err_text,
            raw_path=raw_path,
            detail={"page": page, "columns": len(rows), "errors": list(errors)},
            target=ExtractTarget(page=page, module=_MODULE, window=target_window),
        )
