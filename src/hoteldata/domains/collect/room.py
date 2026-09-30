"""房态提取器(T4.2)—— ``getRcProductList`` → ``getRoomInventoryInfo``。

**规格依据**:``docs/参考/旧系统/规格-批次D提取器.md`` §2 + §7.2(重写检查清单)。
旧实现 ``collectors/room_state.py``(293 行,``class RoomStateCollector``)。

链路
----
``POST getRcProductList``(房型列表,body = ``{}``)
→ 展开 ``data[].roomInfos[]`` 按 ``(hotelID, roomTypeID)`` 去重成 dto 列表
→ ``POST getRoomInventoryInfo``(日期区间 + ``hotelRoomInfoDtoList``)
→ 按 ``(roomTypeID, effectDate)`` 聚合 ``roomStatusResult`` + 回填 ``roomPriceResult``
→ **整批替换** ``alert_room_states``。

★ **实测没有分页(不要自己加)**
--------------------------------------------------
任务书里"分页"的措辞与代码不符(规格 §2.3 / 漂移 B-6):

  1. ``getRcProductList`` 请求体是**空字典 ``{}``** —— 无 ``page`` / ``pageIndex`` /
     ``pageSize`` / ``offset`` / ``limit`` 任一参数;
  2. ``getRoomInventoryInfo`` body **7 个键**里同样没有分页字段;
  3. 旧 ``collect()`` **没有任何翻页循环**。

→ 房态是「**一次全量网格拉取**」。此处若引入分页属**新增行为**,须另行确认平台是否支持。

★★ ``available = 1`` 当且仅当 ``roomStatus == 'G'``
--------------------------------------------------
(旧 room_state.py:264-270 判定式逐字:``all_g = all(s == "G" for s in statuses)`` →
``available = 1 if all_g else 0``)

  * ``'G'`` = 开房 = 可订;``'N'`` 等 = 关房 = 不可订;
  * ★ **售完(``canUsedQuantity == 0``)且 ``roomStatus == 'G'`` 仍算可订**
    (``available=1``,不误报未开房)—— **``quantity`` 绝不参与 ``available`` 判定**;
  * 同一 ``(roomTypeID, effectDate)`` 可能命中多条记录(不同 ratePlan/等级):
    **任一 status 非 ``'G'`` → 该组合不可订**(``available=0``);
  * **关房与售满是正交的两件事**:旧库 225 行实测中 ``available=1 且 quantity=0`` 有
    **28 行**,而 ``available=0`` 的 29 行里只有 2 行 ``quantity=0``。

**★ 整批替换 + 降级不写库(本模块最容易写错的地方)**
--------------------------------------------------
落库是 :meth:`~hoteldata.domains.collect.repository.CollectRepository.replace_room_states`:
**同一事务内先 ``DELETE`` 同 ``(hotel_id, collect_date)`` 再批量 INSERT**(不是 UPSERT),
重跑结果一致。因此:

  * ``records`` **只在全量网格成功拿到时才非空**(``status="ok"``);
  * 两接口任一失败 / 房型为空 → ``status="degraded"``、``records=None``;
  * 网格为空 → ``status="no_data"``、``records=None``。

→ 调用方**只在 ``records`` 非空时**才调 ``replace_room_states``。
若在降级路径上传空 rows,那一次 ``DELETE`` 会把当天旧数据**清空**
(旧系统靠"降级提前 return、不碰库"避免,新实现用 ``records=None`` 把这条纪律**结构化**)。
"""

from __future__ import annotations

import html
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
)
from hoteldata.infra.atomic import atomic_write_json

__all__ = ["RoomExtractor"]

# ---------------------------------------------------------------------------
# 端点(旧 room_state.py:42-46 逐字)
# ---------------------------------------------------------------------------

_GET_RC_PRODUCT = "https://ebooking.ctrip.com/ebkovsroom/api/inventory/getRcProductList"
_GET_ROOM_INVENTORY = "https://ebooking.ctrip.com/ebkovsroom/api/inventory/getRoomInventoryInfo"
_REF_CALENDAR = "https://ebooking.ctrip.com/ebkovsroom/inventory/calendar?microJump=true"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125 Safari/537.36"
)

#: 默认拉取天数(旧 ``collect(days=15)``):``startDate = today``,``endDate = today + days - 1``
_DEFAULT_DAYS = 15

#: 展示用标签(``ExtractTarget`` 只服务 ``summary()``;房态不属于 9 个规则窗口)
_PAGE = "calendar"
_MODULE = "room_state"

_LOGIN_URL_MARKERS = ("login", "passport", "signin", "sign_in", "auth", "sso")
_LOGIN_BODY_MARKERS = ("未登录", "请重新登录", "登录失效")


def _decode_html(value: Any) -> str:
    """HTML 实体解码(旧 room_state.py:49-53 逐字);``None`` → ``""``。"""
    if value is None:
        return ""
    return html.unescape(str(value))


def _repr_status(statuses: list[str]) -> str:
    """聚合后的代表状态:**全 ``'G'`` → ``'G'``,否则首个非 ``'G'``**(旧 room_state.py:56-61)。"""
    for s in statuses:
        if s != "G":
            return s
    return "G"


def _build_dto(info: dict[str, Any]) -> dict[str, Any]:
    """单个 ``roomInfo`` → ``getRoomInventoryInfo`` 的 dto(售卖房型粒度)。

    房型名 **4 级回退**:``roomNameDesc`` → ``roomRCNameDesc`` → ``roomName`` →
    ``roomRCName`` → ``""``;``payType`` 缺省 ``"PP"``(★ 不是写死:优先平台值);
    ``roomClass`` 缺省回退 ``roomTypeID``(旧 room_state.py:153-167 逐字)。
    """
    room_name = (
        info.get("roomNameDesc")
        or info.get("roomRCNameDesc")
        or info.get("roomName")
        or info.get("roomRCName")
        or ""
    )
    return {
        "hotelID": info.get("hotelID"),
        "roomTypeID": info.get("roomTypeID"),
        "roomName": _decode_html(room_name),
        "payType": info.get("payType") or "PP",
        "roomClass": info.get("roomClass") or info.get("roomTypeID"),
    }


def _rc_products(resp: Any) -> list[dict[str, Any]]:
    """``getRcProductList`` → 逐 ``roomInfo`` 展开为 dto 列表。

    ★ 去重键 ``(hotelID, roomTypeID)``,**先到先留**(旧 room_state.py:135-151 逐字)。
    """
    dtos: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any]] = set()
    if not isinstance(resp, dict):
        return dtos
    for parent in resp.get("data") or []:
        if not isinstance(parent, dict):
            continue
        for info in parent.get("roomInfos") or []:
            if not isinstance(info, dict):
                continue
            key = (info.get("hotelID"), info.get("roomTypeID"))
            if key in seen:
                continue
            seen.add(key)
            dtos.append(_build_dto(info))
    return dtos


def _price_map(resp: Any) -> dict[tuple[Any, Any], float]:
    """``roomPriceResult`` → ``{(roomTypeID, effectDate): min price}``。

    ★ 多 ratePlan 取最小值的精确实现:严格 ``<`` 才覆盖(**并列时保留先出现的**),
    ``price is None`` 直接跳过,结果 ``float(price)`` 强转(旧 room_state.py:222-235 逐字)。
    """
    prices: dict[tuple[Any, Any], float] = {}
    if not isinstance(resp, dict):
        return prices
    info_list = (resp.get("data") or {}).get("roomPriceResult", {}).get("roomPriceInfo") or []
    for item in info_list:
        if not isinstance(item, dict):
            continue
        key = (item.get("roomTypeID"), item.get("effectDate"))
        price = item.get("price")
        if price is None:
            continue
        if key not in prices or price < prices[key]:
            prices[key] = float(price)
    return prices


def _build_rows(
    ctx: ExtractContext, dtos: list[dict[str, Any]], resp: Any, *, errors: list[str]
) -> list[dict[str, Any]]:
    """聚合 ``roomStatusResult`` + 回填价格 → 落库行(旧 ``_build_rows`` 语义逐条)。"""
    name_map: dict[Any, str | None] = {}
    for dto in dtos:
        name_map.setdefault(dto.get("roomTypeID"), dto.get("roomName"))
    prices = _price_map(resp)

    status_result = (resp.get("data") or {}).get("roomStatusResult") or []
    grouped: dict[tuple[Any, Any], dict[str, Any]] = {}
    for item in status_result:
        if not isinstance(item, dict):
            continue
        rid = item.get("roomTypeID")
        ed = item.get("effectDate")
        if rid is None or ed is None:
            continue  # 聚合键缺任一 → **整条丢弃**
        g = grouped.setdefault((rid, ed), {"statuses": [], "quantity": 0})
        g["statuses"].append(str(item.get("roomStatus") or ""))
        qty = item.get("canUsedQuantity") or 0
        if qty > g["quantity"]:
            g["quantity"] = qty

    rows: list[dict[str, Any]] = []
    for (rid, ed), g in grouped.items():
        try:
            effect = date.fromisoformat(str(ed))
        except ValueError:
            errors.append(f"effectDate 非 ISO 日期({ed!r}),跳过该行")
            continue
        # ★★ 唯一判定式:任一 roomStatus 非 'G' → 不可订;quantity **不参与**
        all_g = all(s == "G" for s in g["statuses"])
        rows.append(
            {
                "hotel_id": ctx.hotel_id,
                "account_id": ctx.account_id,
                "collect_date": ctx.collect_date,
                "room_type_id": str(rid),
                "room_name": name_map.get(rid),
                "effect_date": effect,
                "available": 1 if all_g else 0,
                "status_code": _repr_status(g["statuses"]),
                "quantity": g["quantity"],
                "price": prices.get((rid, ed)),
                "raw_json_path": None,
            }
        )
    return rows


class RoomExtractor:
    """房态提取器(``name = "room"``,T4.2)。

    返回 **一条** :class:`ExtractResult`;``records`` 的行键 = ``alert_room_states``
    的列名(``hotel_id`` / ``account_id`` / ``collect_date`` / ``room_type_id`` /
    ``room_name`` / ``effect_date`` / ``available`` / ``status_code`` / ``quantity`` /
    ``price`` / ``raw_json_path``)。
    """

    name = "room"

    async def extract(self, ctx: ExtractContext, **kwargs: Any) -> list[ExtractResult]:
        """拉取今日起 ``days`` 日的房态网格。

        ``kwargs``
        ----------
        ``days`` : ``int``,可选
            拉取天数,默认 **15**(旧 ``collect(days=15)``);区间为闭区间
            ``[today, today + days - 1]``。
        """
        days = int(kwargs.get("days") or _DEFAULT_DAYS)
        window = f"今日起{days}日"
        errors: list[str] = []

        # ---- ① getRcProductList(房型列表,body = {}) ----
        try:
            products, products_raw = await self._post_json(
                ctx, "getRcProductList", _GET_RC_PRODUCT, {}, window=window
            )
        except ApiCollectError as exc:
            return [
                self._result(
                    "degraded",
                    None,
                    [f"getRcProductList: {exc}"],
                    window=window,
                    error=f"getRcProductList: {exc}",
                )
            ]
        dtos = _rc_products(products)
        if not dtos:
            # ★ 房型为空 → 降级且**不写库**(旧实现同一分支,见规格 §2.6)
            msg = "getRcProductList 未返回任何房型(房型为空),保留旧数据不替换"
            logger.warning(msg)
            return [self._result("degraded", None, [msg], window=window, raw_path=products_raw, error=msg)]

        # ---- ② getRoomInventoryInfo(★ 单次全量,无翻页) ----
        body = self._inventory_body(ctx.collect_date, days, dtos)
        try:
            resp, resp_raw = await self._post_json(
                ctx, "getRoomInventoryInfo", _GET_ROOM_INVENTORY, body, window=window
            )
        except ApiCollectError as exc:
            return [
                self._result(
                    "degraded",
                    None,
                    [f"getRoomInventoryInfo: {exc}"],
                    window=window,
                    error=f"getRoomInventoryInfo: {exc}",
                )
            ]

        rows = _build_rows(ctx, dtos, resp, errors=errors)
        if not rows:
            msg = "getRoomInventoryInfo 未返回任何房态行(网格为空),保留旧数据不替换"
            logger.warning(msg)
            errors.append(msg)
            return [self._result("no_data", None, errors, window=window, error="; ".join(errors))]

        for row in rows:
            row["raw_json_path"] = resp_raw
        logger.info(
            "房态 {} {}: {} 个房型 / {} 行(窗口 今日起 {} 日)",
            ctx.hotel.name,
            ctx.collect_date,
            len(dtos),
            len(rows),
            days,
        )
        return [
            self._result(
                "degraded" if errors else "ok",
                rows,
                errors,
                window=window,
                raw_path=resp_raw,
                error="; ".join(errors) if errors else None,
            )
        ]

    @staticmethod
    def _inventory_body(today: date, days: int, dtos: list[dict[str, Any]]) -> dict[str, Any]:
        """``getRoomInventoryInfo`` 请求体 —— **7 个键,无分页字段**(旧 room_state.py:169-180)。

        区间语义:``startDate = today``、``endDate = today + (days - 1)``(闭区间)。
        """
        end = (today + timedelta(days=days - 1)).strftime("%Y-%m-%d")
        return {
            "startDate": today.strftime("%Y-%m-%d"),
            "endDate": end,
            "showRoomPrice": True,
            "showRoomInventory": True,
            "showLadderPolicy": True,
            "isPreTaxPrice": False,
            "hotelRoomInfoDtoList": dtos,
        }

    async def _post_json(
        self,
        ctx: ExtractContext,
        name: str,
        url: str,
        body: dict[str, Any],
        *,
        window: str,
    ) -> tuple[Any, str | None]:
        """POST JSON(**限频内**)+ 响应校验 + 原始响应落盘;失败抛 :class:`ApiCollectError`。"""
        try:
            cookie = ctx.session.cookie_header()
        except Exception as exc:  # noqa: BLE001 - SessionRef 只承诺"无 cookie 抛异常"
            raise LoginExpiredError(f"取 cookie 失败: {exc}") from exc
        # ★ GET 才不带 content-type;本模块两接口**全是 POST** → application/json
        headers = {
            "cookie": cookie,
            "user-agent": _USER_AGENT,
            "referer": _REF_CALENDAR,
            "x-requested-with": "XMLHttpRequest",
            "content-type": "application/json",
        }
        async with ctx.limiter.request(ctx.platform, ctx.account.alias):
            attempt = await ctx.http.request(
                "POST",
                url,
                headers=headers,
                json_body=body,
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
        data = attempt.json()
        if data is None:
            raise ApiCollectError(f"{name}: 响应非 JSON({attempt.brief()})")
        return data, self._dump_raw(ctx, window=window, api_name=name, data=data)

    @staticmethod
    def _dump_raw(ctx: ExtractContext, *, window: str, api_name: str, data: Any) -> str | None:
        """原始响应落盘(入库只存相对路径;落盘失败不影响采集)。"""
        if not ctx.persist_raw:
            return None
        try:
            path = ctx.layout.raw_api_path(ctx.hotel.name, ctx.collect_date, _PAGE, _MODULE, window, api_name)
            atomic_write_json(path, data)
            return ctx.layout.to_relative(path)
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("原始响应落盘失败({}): {}", api_name, exc)
            return None

    @staticmethod
    def _result(
        status: ExtractStatus,
        rows: list[dict[str, Any]] | None,
        errors: list[str],
        *,
        window: str,
        raw_path: str | None = None,
        error: str | None = None,
    ) -> ExtractResult:
        """★ ``rows`` 只在全量网格成功时非空 —— 降级/无数据一律 ``records=None``(不写库)。"""
        return ExtractResult(
            status=status,
            channel="api",
            records=rows,
            error=error,
            raw_path=raw_path,
            detail={"pages": 1, "rows": len(rows or []), "errors": list(errors)},
            target=ExtractTarget(page=_PAGE, module=_MODULE, window=window),
        )
