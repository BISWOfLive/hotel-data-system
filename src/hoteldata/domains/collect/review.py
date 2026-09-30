"""点评提取器(T4.3)—— 待回复列表 + 4 类分析素材。

**规格依据**:``docs/参考/旧系统/规格-批次D提取器.md`` §3 + §7.3(重写检查清单);
端点与字段的**唯一来源**是 ``config/review_sources.json``(旧 ``config/review_sources.json:1-118``)。

★ **端点 / method / body / referer / ``list_path`` / 字段路径 / 分页参数全部是配置数据**
------------------------------------------------------------------------------------------
本模块**没有任何硬编码的点评 URL**(唯一例外是 ``score`` 素材的 body 去
``config/api_rules.json`` 借 ``getCommentsScoreV2`` 的捕获模板 —— 旧实现同样如此,
由配置里的 ``body_from_api_rules`` 键触发)。接口改版**只改配置不改代码**(R5-2)。

产出两条链路
------------
====================================  ==============================  ============================
链路                                  接口                             落库
====================================  ==============================  ============================
待回复列表 ``pending.list``            ``soa2/26353/getCommentList``    ``review_reviews``
分析素材 ``scores.<kind>``            4 个端点                         ``review_materials``
====================================  ==============================  ============================

**容易写错的几条(逐条对应规格 §7.3)**
------------------------------------
1. ``POST soa2/26353/getCommentList``,``catalogTab="NotFeedBack"``,
   **``pageSize=20``** —— 盘点表写 ``pageSize:10`` 是**错的**(漂移 C-1);
   ``pageSize`` / ``max_pages`` 都从配置读,``pageIndex`` 从 **1** 起,最多 ``max_pages`` 页。
   ⚠️ 占位符按旧实现做**字符串替换**,故 ``pageIndex`` 发出去是 ``"1"``(**字符串不是整数**)
   —— 旧系统与平台都接受,不要"顺手"转 int。
2. 字段路径 ``commentId`` / ``userName`` / **``score.avgScoreSimple``** / ``content`` /
   ``addtime``。★ **星级是对象不是 int**:``score`` 是 dict,路径必须是两层
   ``score.avgScoreSimple``,取值要 ``int(float(...))``(既容忍 ``"4.2"`` 字符串,
   也容忍 ``4.2000000001``)。按单层 ``star``/``score`` 取会**全量取到 None**
   → ``star=NULL``、``sentiment`` 全 ``unknown`` → 好评自动回复全线失效(漂移 C-5)。
3. ``addtime`` 格式 ``/Date(ms+0800)/``,正则 ``r"/Date\\((\d+)(?:[+-]\d{4})?\\)/"``
   (``search`` 不是 ``match``)。★ **时区陷阱**:旧实现把 ``+0800`` **吞掉却未使用**,
   用 ``datetime.fromtimestamp(ms/1000)`` 依赖**宿主机时区** —— 部署到 UTC 会得到
   不同的 ``comment_time``。新实现**显式按 ``Asia/Shanghai``** 转换后写 ``timestamptz``
   (见 :func:`parse_addtime`)。
4. ``commentId`` 缺失 → 内容指纹兜底,★ **顺序敏感**:``"h" + sha1(...)[:16]``,
   四元组固定 ``(star, user_name, content, comment_time)``,其中 ``comment_time``
   传的是**原始 ``addtime`` 串**(不是解析结果)。改顺序会让**历史指纹 id 全变** →
   ``UNIQUE(hotel_id, review_id)`` 命中失败 → 同一条点评产生**重复行**(见
   :func:`review_id_fallback`)。
5. 情感:星级 ``>=4`` → ``good``,``<=3`` → ``bad``,无星级 → ``unknown``;
   ``unknown`` 时用 ``score.commentLevel`` 兜底(``好评``→good / ``差评``→bad)。
6. 素材 **4 类**:``score`` / ``competitor`` / ``trend`` / **``num``**
   —— 旧建表注释只写 3 类,漏了 ``num``(漂移 D-5 / 实库 ``kind=num`` 4 行)。
7. ★★ **UPSERT 不回溯**:``review_reviews`` 的 ``INSERT`` 列清单**不含 ``replied``**,
   ``DO UPDATE SET`` **既不含 ``replied`` 也不含 ``strategy``** —— 两条**独立**机制,
   缺一条就破坏「已回复不回溯」。这里产出的行**只带**
   ``hotel_id`` / ``review_id`` / ``user_name`` / ``star`` / ``content`` / ``sentiment`` /
   ``comment_time``,**绝不带** ``replied`` / ``strategy``;
   落库见 :meth:`~hoteldata.domains.collect.repository.CollectRepository.upsert_reviews`。

**返回契约**:每个**配置源**一条 :class:`ExtractResult`(``pending.list`` 一条、
每个素材 ``kind`` 一条),``records`` 为待落库的多行。
``review_materials`` 的 ``payload`` 为空时 ``status="no_data"`` 但 **``records`` 仍有一行**
(旧口径就是把它落成 ``status='no_data'`` 的行,见规格 §3.7);
``review_reviews`` 没有"空行"概念,接口失败 → ``status="degraded"`` + ``records=None``。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from loguru import logger

from hoteldata.domains.collect.contract import (
    ApiCollectError,
    ExtractContext,
    ExtractResult,
    ExtractStatus,
    ExtractTarget,
    LoginExpiredError,
)
from hoteldata.domains.collect.jsonpath import json_get
from hoteldata.domains.collect.rules import get_api_rules
from hoteldata.infra.atomic import atomic_write_json, read_json
from hoteldata.settings import get_settings

__all__ = [
    "BAD_MAX_STAR",
    "GOOD_MIN_STAR",
    "ReviewExtractor",
    "classify_sentiment",
    "load_review_sources",
    "parse_addtime",
    "review_id_fallback",
]

# ---------------------------------------------------------------------------
# 常量(旧 comment_collector.py:43-49 逐字)
# ---------------------------------------------------------------------------

_SOURCES_FILENAME = "review_sources.json"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125 Safari/537.36"
)
_FORM_CONTENT_TYPE = "application/x-www-form-urlencoded; charset=UTF-8"

#: ``addtime`` 格式 ``/Date(1784685469000+0800)/`` —— **``search`` 不是 ``match``**
_ADD_TIME_RE = re.compile(r"/Date\((\d+)(?:[+-]\d{4})?\)/")

#: ★ 显式时区:旧实现用 ``fromtimestamp`` 依赖宿主机时区(规格 §3.5 ★ 重写警示)
_SHANGHAI = ZoneInfo("Asia/Shanghai")

#: 情感阈值(旧 ``classify_sentiment`` 默认值;旧系统可由 ``review_templates.json`` 的
#: ``rules`` 覆盖,段1 尚无该配置文件 → 用计划书⑤ 的默认 ``good_min_star=4`` / ``bad_max_star=3``)
GOOD_MIN_STAR = 4
BAD_MAX_STAR = 3

_PAGE = "review"
#: 展示用窗口标签(``ExtractTarget`` 只服务 ``summary()``)
_WINDOW = "实时"

_LOGIN_URL_MARKERS = ("login", "passport", "signin", "sign_in", "auth", "sso")
_LOGIN_BODY_MARKERS = ("未登录", "请重新登录", "登录失效")


# ---------------------------------------------------------------------------
# 纯函数(全部可单测;顺序/时区是硬约束)
# ---------------------------------------------------------------------------


def parse_addtime(raw: Any) -> datetime | None:
    """``/Date(1784685469000+0800)/`` → **Asia/Shanghai** 的 ``datetime``;其余 → ``None``。

    ★ **修掉的坑**(规格 §3.5 ★ 重写警示 / 漂移 C-13):旧正则
    ``r"/Date\\((\\d+)(?:[+-]\\d{4})?\\)/"`` 把时区偏移 ``+0800`` **匹配掉却从未使用**,
    随后 ``datetime.fromtimestamp(ms/1000)`` 按**宿主机本地时区**解释 —— 同一毫秒值在
    跑在 UTC 的机器上会得到**差 8 小时**的 ``comment_time`` 字符串,而且**没有任何报错**。

    新实现把时区**显式**钉死在 ``Asia/Shanghai``(数据源的业务时区),
    结果写进 ``timestamptz`` 列 → 无论部署机时区如何,读出来都是同一时刻。

    逐条语义(其余与旧实现一致):
      * ``raw is None`` → ``None``(**不落空串**);
      * 用 ``search`` 而非 ``match``(允许前缀噪声);
      * 异常(``ValueError`` / ``OSError`` / ``OverflowError``)→ ``None``;
      * ★ 非 ``/Date/`` 串 → ``None``:旧实现回退成 ``str(raw)[:40]`` 塞进 TEXT 列;
        新列是 ``timestamptz``,**塞不进任意字符串**,故显式返回 ``None``
        (调用方 :class:`ReviewExtractor` 记 warning,不静默)。
    """
    if raw is None:
        return None
    m = _ADD_TIME_RE.search(str(raw))
    if not m:
        return None
    try:
        return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=_SHANGHAI)
    except ValueError, OSError, OverflowError:
        return None


def review_id_fallback(star: Any, user_name: Any, content: Any, comment_time: Any) -> str:
    """平台 ``commentId`` 缺失时的兜底 ``review_id``:内容指纹(幂等,同点评同 id)。

    ★★ **顺序敏感,不可改**(规格 §3.6 / 漂移 C-14):
    四元组固定为 ``(star, user_name, content, comment_time)``、固定以 ``"|"`` 连接,
    且 ``comment_time`` 传的是 **原始 ``addtime`` 串**(如 ``/Date(1784685469000+0800)/``),
    **不是** :func:`parse_addtime` 的结果(旧实现里 ``parse_addtime`` 是在
    ``upsert_review`` 调用时才执行的)。

    改顺序 / 改传参 → **历史指纹 id 全部变化** → ``UNIQUE(hotel_id, review_id)``
    命中失败 → 同一条点评产生**重复行**。

    空值归一 ``str(v or "")``:``None`` / ``""`` / ``0`` / ``False`` 都变 ``""``
    (注意 ``star=0`` 也会被吞成空串 —— 这是**历史口径**,不要"修正")。
    编码 UTF-8,取 ``sha1().hexdigest()[:16]``,前缀字面量 ``"h"`` → **17** 字符。
    """
    raw = "|".join(str(v or "") for v in (star, user_name, content, comment_time))
    return "h" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def classify_sentiment(
    star: int | None, *, good_min: int = GOOD_MIN_STAR, bad_max: int = BAD_MAX_STAR
) -> str:
    """星级 → ``good`` / ``bad`` / ``unknown``(旧 ``app/review_reply.classify_sentiment``)。

    ``star >= good_min`` → ``good``;``star <= bad_max`` → ``bad``;
    **无星级或中间值 → ``unknown``**(「宁可漏不可错,防自动错回」)。
    """
    if star is None:
        return "unknown"
    try:
        s = int(star)
    except TypeError, ValueError:
        return "unknown"
    if s >= good_min:
        return "good"
    if s <= bad_max:
        return "bad"
    return "unknown"


def _render_body(body: Any, ctx: Mapping[str, Any]) -> Any:
    """递归替换体占位符 ``{page}`` / ``{today}``;其余原样(旧 comment_collector.py:71-82)。"""
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


def _tostr(value: Any) -> str:
    """``"" if v is None else str(v)``(旧 ``_tostr``)。"""
    return "" if value is None else str(value)


def _to_int_or_none(value: Any) -> int | None:
    """``int(float(v))``;``None``/``""``/转换失败 → ``None``(旧星级取值三段逻辑)。"""
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except TypeError, ValueError:
        return None


def load_review_sources(path: Path | str | None = None) -> dict[str, Any]:
    """读 ``config/review_sources.json``;缺失/损坏 → ``{}`` + warning(按未配置降级)。

    旧 ``app/review_reply.load_review_sources`` 逐字同款语义(只是新实现复用
    :func:`hoteldata.infra.atomic.read_json` 的原子读)。
    """
    target = Path(path) if path is not None else get_settings().paths.config_dir / _SOURCES_FILENAME
    data = read_json(target, default=None)
    if not isinstance(data, dict):
        logger.warning("{} 读取失败或缺失({}),点评源按未配置降级", _SOURCES_FILENAME, target)
        return {}
    return data


def _page_count_of(result: Any) -> int:
    """响应 ``pageCount`` → ``int``;解析失败/无值 → ``1``,且 ``max(page_count, 1)``。"""
    try:
        return max(int(json_get(result, "pageCount")), 1)
    except TypeError, ValueError:
        return 1


def _rcode_error(result: Any) -> str | None:
    """平台拒答识别(★ 防误判"结构变化")。

    旧实现只对 ``pending`` 校验了 ``resStatus.rcode``(漂移 C-15);重写检查清单
    §7.3 第 6 条要求**4 类素材一并校验**,这里统一做:
    先看 ``resStatus.rcode``(SOA 包裹体),再看顶层 ``rcode``(datacenter 接口)。
    合法值 ``None`` / ``0`` / ``200``。
    """
    if not isinstance(result, Mapping):
        return None
    candidates: list[Any] = []
    rs = result.get("resStatus")
    if isinstance(rs, Mapping):
        candidates.append((rs.get("rcode"), rs.get("rmsg")))
    if "rcode" in result:
        candidates.append((result.get("rcode"), result.get("msg")))
    for rcode, rmsg in candidates:
        if rcode not in (None, 0, 200):
            return f"平台拒答 rcode={rcode}: {str(rmsg or '')[:120]}"
    return None


class ReviewExtractor:
    """点评提取器(``name = "review"``,T4.3)。"""

    name = "review"

    async def extract(self, ctx: ExtractContext, **kwargs: Any) -> list[ExtractResult]:
        """跑待回复列表 + 4 类素材。

        ``kwargs``
        ----------
        ``sources_path`` : ``str | Path``,可选
            覆盖 ``config/review_sources.json`` 路径(测试/灰度用)。
        """
        sources = load_review_sources(kwargs.get("sources_path"))
        results: list[ExtractResult] = []
        results.extend(await self._collect_pending(ctx, sources.get("pending") or {}))
        results.extend(await self._collect_materials(ctx, sources.get("scores") or {}))
        return results

    # ==================================================================
    # ① 待回复列表(旧 collect_pending,comment_collector.py:244-316)
    # ==================================================================

    async def _collect_pending(self, ctx: ExtractContext, pending: Mapping[str, Any]) -> list[ExtractResult]:
        """遍历 ``pending`` 下**全部**配置源(``_`` 前缀键=说明跳过,``ready=false`` 跳过并计 note)。"""
        results: list[ExtractResult] = []
        for key, src in pending.items():
            if str(key).startswith("_") or not isinstance(src, Mapping):
                continue
            url = str(src.get("url") or "").strip()
            if not src.get("ready"):
                logger.warning("点评源 {}(未就绪,跳过) — {}", key, str(src.get("note") or "")[:80])
                continue
            if not url:
                logger.warning("点评源 {}(url 为空,跳过)", key)
                continue
            results.append(await self._pending_source(ctx, str(key), src))
        return results

    async def _pending_source(self, ctx: ExtractContext, key: str, src: Mapping[str, Any]) -> ExtractResult:
        """单个待回复源:翻页抓取 → 逐条抽字段 → ``review_reviews`` 行。"""
        errors: list[str] = []
        notes: list[str] = []
        rows: list[dict[str, Any]] = []
        pages = 0
        collect_date = ctx.collect_date.isoformat()

        page_size = int(src.get("page_size") or 20)
        max_pages = int(src.get("max_pages") or 20)
        fields: Mapping[str, Any] = src.get("fields") or {}
        hint_path = str(src.get("sentiment_hint_path") or "")
        hint_map = src.get("sentiment_hint_map") or {}

        # ★ pageIndex 从 1 起,最多 max_pages 页
        for page in range(1, max_pages + 1):
            try:
                items, page_count = await self._fetch_source_page(ctx, key, src, page, collect_date)
            except ApiCollectError as exc:
                errors.append(f"{key}: {exc}")
                break
            pages += 1
            if not items:
                break
            for item in items:
                row = self._review_row(ctx, item, fields, hint_path, hint_map, key, notes)
                if row is not None:
                    rows.append(row)
            # ★ 两个停止条件(或关系):本页不足 page_size **或** 已到 pageCount
            if len(items) < page_size or page >= page_count:
                break

        status: ExtractStatus = "degraded" if errors else "ok"
        return ExtractResult(
            status=status,
            channel="api",
            records=rows,
            error="; ".join(errors) if errors else None,
            detail={"source": key, "rows": len(rows), "pages": pages, "notes": notes, "errors": errors},
            target=ExtractTarget(page=_PAGE, module=key, window=_WINDOW),
        )

    @staticmethod
    def _review_row(
        ctx: ExtractContext,
        item: Any,
        fields: Mapping[str, Any],
        hint_path: str,
        hint_map: Mapping[str, Any],
        key: str,
        notes: list[str],
    ) -> dict[str, Any] | None:
        """单条点评 → ``review_reviews`` 行(★ **不含** ``replied`` / ``strategy``)。"""
        if not isinstance(item, Mapping):
            return None
        # 字段路径一律来自配置,兜底默认值与旧实现逐字一致
        content_path = str(fields.get("content") or "content")
        id_path = str(fields.get("review_id") or "commentId")
        star_path = str(fields.get("star") or "score.avgScoreSimple")
        name_path = str(fields.get("user_name") or "userName")
        time_path = str(fields.get("time") or "addtime")

        content = str(json_get(item, content_path) or "")
        if not content:
            return None  # ★ 内容为空 → 整条跳过(旧语义)

        review_id = str(json_get(item, id_path) or "").strip()
        user_name_raw = json_get(item, name_path)
        star_raw = json_get(item, star_path)
        star = _to_int_or_none(star_raw)  # ★ int(float(...)):容忍 "4.2" 与 4.2000000001
        addtime_raw = json_get(item, time_path)

        if not review_id:
            # ★ 指纹的 comment_time 必须是**原始 addtime 串**(不是解析结果)
            review_id = review_id_fallback(star, user_name_raw, content, addtime_raw)

        sentiment = classify_sentiment(star)
        if sentiment == "unknown" and hint_path:
            level = str(json_get(item, hint_path) or "")
            sentiment = str(hint_map.get(level, "unknown"))

        comment_time = parse_addtime(addtime_raw)
        if addtime_raw is not None and comment_time is None:
            notes.append(f"{key}: addtime 无法解析({str(addtime_raw)[:40]!r}),comment_time 落 NULL")

        return {
            "hotel_id": ctx.hotel_id,
            "review_id": review_id,
            "user_name": _tostr(user_name_raw),
            "star": star,
            "content": content,
            "sentiment": sentiment,
            "comment_time": comment_time,
            # ★ 绝不含 replied / strategy —— 不回溯由「不在 INSERT 列清单」保证
        }

    async def _fetch_source_page(
        self, ctx: ExtractContext, key: str, src: Mapping[str, Any], page: int, collect_date: str
    ) -> tuple[list[Any], int]:
        """取一页待回复点评,返回 ``(items, pageCount)``。

        通道由 **``body`` 的类型**隐式决定(旧隐式契约,必须保留):
        ``dict`` → JSON;``str`` → 表单;其他 → 空 JSON body。
        配置里的 ``"form": true`` 是**无效键**(采集器从不读它),本实现同样不读。
        """
        url = str(src.get("url") or "").strip()
        referer = str(src.get("referer") or "")
        body = src.get("body")
        render_ctx = {"page": page, "today": collect_date}
        json_body: Any = None
        content: str | None = None
        if isinstance(body, dict):
            json_body = _render_body(body, render_ctx)
        elif isinstance(body, str):
            content = str(_render_body(body, render_ctx))
        else:
            json_body = {}

        result, _raw = await self._send(
            ctx,
            name=key,
            url=url,
            referer=referer,
            module=key,
            json_body=json_body,
            content=content,
        )
        rcode_msg = _rcode_error(result)
        if rcode_msg:
            raise ApiCollectError(rcode_msg)
        list_path = str(src.get("list_path") or "commentlist")
        items = json_get(result, list_path)
        if not isinstance(items, list):
            raise ApiCollectError(f"list_path={list_path} 非列表(结构疑似变化,R5-2 降级人工队列)")
        return items, _page_count_of(result)

    # ==================================================================
    # ② 分析素材(旧 collect_scores,comment_collector.py:321-364)
    # ==================================================================

    async def _collect_materials(self, ctx: ExtractContext, scores: Mapping[str, Any]) -> list[ExtractResult]:
        """遍历 ``scores`` 下**全部**配置源 → 每类一行 ``review_materials``。

        ★ 实际 **4 类**:``score`` / ``competitor`` / ``trend`` / ``num``
        (旧建表注释只写 3 类,实库有 ``kind=num``,漂移 D-5)。
        """
        results: list[ExtractResult] = []
        collect_date = ctx.collect_date.isoformat()
        for kind, src in scores.items():
            if str(kind).startswith("_") or not isinstance(src, Mapping):
                continue
            url = str(src.get("url") or "").strip()
            if not src.get("ready") or not url:
                logger.warning("点评素材源 {}(未就绪或 url 为空,跳过)", kind)
                continue
            results.append(await self._material_source(ctx, str(kind), src, collect_date))
        return results

    async def _material_source(
        self, ctx: ExtractContext, kind: str, src: Mapping[str, Any], collect_date: str
    ) -> ExtractResult:
        """单个素材源:请求 → 取 ``payload_path`` → 落一行。"""
        url = str(src.get("url") or "").strip()
        referer = str(src.get("referer") or "")
        render_ctx = {"today": collect_date}
        try:
            body = src.get("body")
            if src.get("body_from_api_rules"):
                # 跨文件借 body 模板(旧 _api_def_body):getCommentsScoreV2 的 spiderkey
                # 是**平台下发的长令牌**,视为可失效凭据,改版时重新捕获
                body = self._api_def_body(str(src.get("body_from_api_rules"))) or {}
            if isinstance(body, dict) and "_form" in body:
                body = body["_form"]
            if isinstance(body, dict):
                result, raw_rel = await self._send(
                    ctx,
                    name=kind,
                    url=url,
                    referer=referer,
                    module=kind,
                    json_body=_render_body(body, render_ctx),
                )
            elif isinstance(body, str):
                result, raw_rel = await self._send(
                    ctx,
                    name=kind,
                    url=url,
                    referer=referer,
                    module=kind,
                    content=str(_render_body(body, render_ctx)),
                )
            else:
                result, raw_rel = await self._send(
                    ctx, name=kind, url=url, referer=referer, module=kind, json_body={}
                )
        except ApiCollectError as exc:
            logger.warning("点评素材 {} 失败: {}", kind, exc)
            return ExtractResult(
                status="degraded",
                channel="api",
                records=None,
                error=f"{kind}: {exc}",
                detail={"kind": kind},
                target=ExtractTarget(page=_PAGE, module=kind, window=_WINDOW),
            )

        payload_path = str(src.get("payload_path") or "")
        # ★ payload_path 为空串 → **整包落库**(num 就是这种,实库含 ResponseStatus/hotelMap)
        payload = result if not payload_path else json_get(result, payload_path)
        if not isinstance(payload, dict):
            # ★ 非 dict → 包一层 {"data": ...}(trend 取到的是**列表**,靠这层保留结构)
            payload = {"data": payload}
        row_status = "ok" if payload else "no_data"
        rows = [
            {
                "hotel_id": ctx.hotel_id,
                "collect_date": ctx.collect_date,
                "kind": kind,
                "payload_json": payload,
                "raw_json_path": raw_rel,
                "channel": "api",
                "status": row_status,
                "error": None,
            }
        ]
        return ExtractResult(
            status="ok" if payload else "no_data",
            channel="api",
            records=rows,
            raw_path=raw_rel,
            detail={"kind": kind, "row_status": row_status},
            target=ExtractTarget(page=_PAGE, module=kind, window=_WINDOW),
        )

    @staticmethod
    def _api_def_body(name: str) -> Any:
        """在 ``config/api_rules.json`` 全部页取首个同名 ``api_def.body``(跨页扫描,旧语义)。"""
        rules = get_api_rules()
        for page_cfg in rules.pages.values():
            found = page_cfg.api_def(name)
            if found is not None:
                return found.body
        logger.warning("api_rules 中找不到接口定义 {}", name)
        return None

    # ==================================================================
    # 底层请求
    # ==================================================================

    async def _send(
        self,
        ctx: ExtractContext,
        *,
        name: str,
        url: str,
        referer: str,
        module: str,
        json_body: Any = None,
        content: str | None = None,
    ) -> tuple[Any, str | None]:
        """POST(**限频内**)+ 响应校验 + 原始响应落盘。

        请求头(旧 comment_collector.py:134-143):``cookie`` / ``user-agent`` /
        ``referer`` / ``x-requested-with``;``content-type`` 由 body 类型决定 ——
        ``dict`` → ``application/json``,``str``(表单) → ``application/x-www-form-urlencoded``。
        """
        try:
            cookie = ctx.session.cookie_header()
        except Exception as exc:  # noqa: BLE001 - SessionRef 只承诺"无 cookie 抛异常"
            raise LoginExpiredError(f"取 cookie 失败: {exc}") from exc
        headers = {
            "cookie": cookie,
            "user-agent": _USER_AGENT,
            "referer": referer or "",
            "x-requested-with": "XMLHttpRequest",
            "content-type": _FORM_CONTENT_TYPE if content is not None else "application/json",
        }
        async with ctx.limiter.request(ctx.platform, ctx.account.alias):
            attempt = await ctx.http.request(
                "POST",
                url,
                headers=headers,
                json_body=json_body if content is None else None,
                content=content,
                timeout=ctx.api_timeout_s,
                follow_redirects=False,
            )
        status = attempt.status_code or 0
        location = (attempt.location() or "").lower()
        if status in (401, 403):
            raise LoginExpiredError(f"{name}: HTTP {status}(登录态失效)")
        if status in (301, 302, 303, 307, 308) and any(m in location for m in _LOGIN_URL_MARKERS):
            raise LoginExpiredError(f"{name}: 被重定向到登录页({location[:120]})")
        if not attempt.is_success:
            raise ApiCollectError(f"{name}: HTTP {status} {attempt.error or ''}".strip())
        if any(m in (attempt.text or "") for m in _LOGIN_BODY_MARKERS):
            raise LoginExpiredError(f"{name}: 响应体提示未登录")
        result = attempt.json()
        if result is None:
            raise ApiCollectError(f"{name}: 响应非 JSON({attempt.brief()})")
        return result, self._dump_raw(ctx, module=module, api_name=name, data=result)

    @staticmethod
    def _dump_raw(ctx: ExtractContext, *, module: str, api_name: str, data: Any) -> str | None:
        """原始响应落盘(入库只存相对路径;落盘失败不影响采集)。"""
        if not ctx.persist_raw:
            return None
        try:
            path = ctx.layout.raw_api_path(ctx.hotel.name, ctx.collect_date, _PAGE, module, _WINDOW, api_name)
            atomic_write_json(path, data)
            return ctx.layout.to_relative(path)
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("原始响应落盘失败({}): {}", api_name, exc)
            return None
