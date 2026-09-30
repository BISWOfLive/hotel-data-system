"""落库(UPSERT 幂等)—— T3.7。

**B19 遗产:UPSERT 幂等 + 不回溯**
  - 同日重跑即**覆盖**,行数不增(V13);
  - ``reviews`` 冲突时**绝不重置** ``replied`` / ``strategy``(批次 D)。

**★ ``payload_json`` 用 JSONB**(旧系统是 TEXT,12 列 JSON 存成 TEXT、过滤靠 ``LIKE``)。

**★ ``link_screenshot`` 的 COALESCE 语义(逐字继承旧 ``storage/db.py:770-771``)**::

    SET screenshot_path        = COALESCE(?, screenshot_path),
        module_screenshots_json = COALESCE(?, module_screenshots_json)

  - 传 ``NULL`` → **保留旧值**(重跑失败不会把已有图清掉)
  - 传非 ``NULL`` → 覆盖
  - **空 dict 等价 ``None``** → 不清空旧值

**★ ``module_screenshots_json`` 的形状(段2 靠它取图,键名不许改)**::

    {"<screenshot_modules[*].name>": "<相对项目根的单条路径字符串>"}

  值是**字符串不是数组**;多条 = 多键。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

from sqlalchemy import bindparam as sa_bindparam
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from hoteldata.infra.models import (
    AlertPortalColumn,
    AlertRoomState,
    CollectModule,
    CollectReport,
    ReviewMaterial,
    ReviewReview,
)

__all__ = ["CollectRepository"]


class CollectRepository:
    """``collect_reports`` / ``collect_modules`` 的读写。

    ★ 只碰**自己的两张表**(硬约束 3:域之间不直接 join 别人的表)。
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ==================================================================
    # collect_reports
    # ==================================================================

    async def find_report(self, hotel_id: int, collect_date: date, page: str) -> CollectReport | None:
        """读一行 ``collect_reports``。

        ★ 带 ``populate_existing=True``:本类的写入走 **Core** ``update()``/``insert()``,
        不会刷新 ORM 身份映射。若同一 session 内"先读 → 再写 → 再读",
        第二次读会拿到**身份映射里的旧对象**,看起来就像"COALESCE 没生效"。
        强制从库里重取,消除这个陷阱。
        """
        stmt = (
            select(CollectReport)
            .where(
                CollectReport.hotel_id == hotel_id,
                CollectReport.collect_date == collect_date,
                CollectReport.page == page,
            )
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def ensure_report(
        self,
        hotel_id: int,
        collect_date: date,
        page: str,
        *,
        channel: str = "screenshot",
        account_id: int | None = None,
    ) -> int:
        """幂等创建 ``collect_reports`` 行(截图前必须先有行,否则 UPDATE 无行可改)。"""
        stmt = (
            pg_insert(CollectReport)
            .values(
                hotel_id=hotel_id,
                account_id=account_id,
                collect_date=collect_date,
                page=page,
                channel=channel,
                status="ok",
            )
            .on_conflict_do_nothing(
                index_elements=["hotel_id", "collect_date", "page"],
            )
            .returning(CollectReport.id)
        )
        row_id = await self.session.scalar(stmt)
        if row_id is None:
            existing = await self.find_report(hotel_id, collect_date, page)
            row_id = existing.id if existing else None
        return int(row_id) if row_id is not None else 0

    async def upsert_report(
        self,
        hotel_id: int,
        collect_date: date,
        page: str,
        *,
        channel: str,
        account_id: int | None = None,
        indicators: dict[str, Any] | None = None,
        modules: dict[str, Any] | None = None,
        status: str | None = None,
        error: str | None = None,
        raw_json_path: str | None = None,
        html_path: str | None = None,
        module_screenshots: dict[str, str] | None = None,
    ) -> int:
        """页面级 UPSERT(同键覆盖)。"""
        values: dict[str, Any] = {
            "hotel_id": hotel_id,
            "account_id": account_id,
            "collect_date": collect_date,
            "page": page,
            "channel": channel,
            "indicators_json": indicators,
            "modules_json": modules,
            "status": status,
            "error": error,
            "raw_json_path": raw_json_path,
            "html_path": html_path,
        }
        if module_screenshots is not None:
            values["module_screenshots_json"] = module_screenshots
        stmt = pg_insert(CollectReport).values(**values)
        update_cols = {
            k: getattr(stmt.excluded, k)
            for k in (
                "account_id",
                "channel",
                "indicators_json",
                "modules_json",
                "status",
                "error",
                "raw_json_path",
                "html_path",
            )
        }
        if module_screenshots is not None:
            update_cols["module_screenshots_json"] = stmt.excluded.module_screenshots_json
        stmt = stmt.on_conflict_do_update(
            index_elements=["hotel_id", "collect_date", "page"],
            set_=update_cols,
        ).returning(CollectReport.id)
        return int(await self.session.scalar(stmt) or 0)

    async def link_screenshot(
        self,
        hotel_id: int,
        collect_date: date,
        page: str,
        *,
        screenshot_path: str | None = None,
        module_screenshots: dict[str, str] | None = None,
    ) -> int:
        """★ 截图回填 —— ``COALESCE(新值, 旧值)``。

        传 ``None``/空 dict → **保留旧值**;传非空 dict → 覆盖。
        返回受影响行数(0 表示没有对应 ``collect_reports`` 行,调用方应先
        :meth:`ensure_report`)。
        """
        # 空 dict 走 None(逐字继承旧 db.py:775 的判空)
        shots = module_screenshots or None
        # ★ 陷阱:``shots is None`` 时**不能**用 ``bindparam(type_=JSONB)`` 传 None。
        #   SQLAlchemy 的 JSON/JSONB 类型默认 ``none_as_null=False``,会把 Python None
        #   编成 **JSON 字面量 null**(而不是 SQL NULL);于是
        #   ``coalesce('null'::jsonb, col)`` 返回的是 ``'null'::jsonb`` ——
        #   值确实"变"了,读回 Python 也是 None,看起来就像 COALESCE 没保住旧值。
        #   正确做法:为 None 时**自赋值列本身**,语义与 COALESCE 的"保留旧值"完全等价,
        #   且不引入 JSON null。
        module_expr = (
            CollectReport.module_screenshots_json
            if shots is None
            else sa_bindparam("shots", shots, type_=JSONB)
        )
        stmt = (
            update(CollectReport)
            .where(
                CollectReport.hotel_id == hotel_id,
                CollectReport.collect_date == collect_date,
                CollectReport.page == page,
            )
            .values(
                screenshot_path=func.coalesce(screenshot_path, CollectReport.screenshot_path),
                module_screenshots_json=module_expr,
            )
        )
        result = await self.session.execute(stmt)
        return int(result.rowcount or 0)

    async def module_screenshots(self, hotel_id: int, collect_date: date, page: str) -> dict[str, str]:
        """读回 ``module_screenshots_json``(段2 取图路径)。"""
        row = await self.find_report(hotel_id, collect_date, page)
        if row is None or not isinstance(row.module_screenshots_json, dict):
            return {}
        return {str(k): str(v) for k, v in row.module_screenshots_json.items()}

    # ==================================================================
    # collect_modules ★ 核心表
    # ==================================================================

    async def upsert_module(
        self,
        *,
        hotel_id: int,
        account_id: int | None,
        collect_date: date,
        page: str,
        module: str,
        window: str,
        payload: dict[str, Any] | None,
        raw_json_path: str | None = None,
        channel: str = "api",
        status: str = "ok",
        error: str | None = None,
    ) -> int:
        """★ 模块级 UPSERT(唯一键五列),``RETURNING id``。

        同日重跑即覆盖 → **行数不增**(V13)。
        """
        values = {
            "hotel_id": hotel_id,
            "account_id": account_id,
            "collect_date": collect_date,
            "page": page,
            "module": module,
            "window": window,
            "payload_json": payload or {},
            "raw_json_path": raw_json_path,
            "channel": channel,
            "status": status,
            "error": error,
        }
        stmt = pg_insert(CollectModule).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["hotel_id", "collect_date", "page", "module", "window"],
            set_={
                "account_id": stmt.excluded.account_id,
                "payload_json": stmt.excluded.payload_json,
                "raw_json_path": stmt.excluded.raw_json_path,
                "channel": stmt.excluded.channel,
                "status": stmt.excluded.status,
                "error": stmt.excluded.error,
            },
        ).returning(CollectModule.id)
        return int(await self.session.scalar(stmt) or 0)

    async def count_modules(self, hotel_id: int, collect_date: date, page: str | None = None) -> int:
        """统计行数(幂等验收 V13 的证据)。"""
        stmt = (
            select(func.count())
            .select_from(CollectModule)
            .where(
                CollectModule.hotel_id == hotel_id,
                CollectModule.collect_date == collect_date,
            )
        )
        if page:
            stmt = stmt.where(CollectModule.page == page)
        return int(await self.session.scalar(stmt) or 0)

    async def fetch_module(
        self,
        hotel_id: int,
        collect_date: date,
        page: str,
        module: str,
        window: str,
    ) -> CollectModule | None:
        stmt = select(CollectModule).where(
            CollectModule.hotel_id == hotel_id,
            CollectModule.collect_date == collect_date,
            CollectModule.page == page,
            CollectModule.module == module,
            CollectModule.window == window,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_modules(
        self,
        hotel_id: int,
        collect_date: date,
        *,
        page: str | None = None,
        module: str | None = None,
    ) -> list[CollectModule]:
        stmt = (
            select(CollectModule)
            .where(
                CollectModule.hotel_id == hotel_id,
                CollectModule.collect_date == collect_date,
            )
            .order_by(CollectModule.page, CollectModule.module, CollectModule.window)
        )
        if page:
            stmt = stmt.where(CollectModule.page == page)
        if module:
            stmt = stmt.where(CollectModule.module == module)
        return list((await self.session.execute(stmt)).scalars().all())

    async def delete_modules(self, hotel_id: int, collect_date: date, *, page: str | None = None) -> int:
        stmt = delete(CollectModule).where(
            CollectModule.hotel_id == hotel_id,
            CollectModule.collect_date == collect_date,
        )
        if page:
            stmt = stmt.where(CollectModule.page == page)
        result = await self.session.execute(stmt)
        return int(result.rowcount or 0)

    async def status_breakdown(self, hotel_id: int, collect_date: date) -> dict[str, int]:
        """按四态统计(自检/CLI 摘要用)。"""
        stmt = (
            select(CollectModule.status, func.count())
            .where(
                CollectModule.hotel_id == hotel_id,
                CollectModule.collect_date == collect_date,
            )
            .group_by(CollectModule.status)
        )
        rows = (await self.session.execute(stmt)).all()
        return {str(s): int(c) for s, c in rows}

    # ==================================================================
    # 批次 D 提取器(T4.1 / T4.2 / T4.3)
    # ==================================================================

    async def upsert_portal_columns(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """UPSERT 预警采集列(T4.1)—— 唯一键 ``(hotel_id, collect_date, page, column_name)``。

        冲突覆盖 7 列(``account_id`` / ``value`` / ``detail_json`` / ``raw_json_path`` /
        ``channel`` / ``status`` / ``error``);**``id`` / ``created_at`` 不变**
        (旧 ``storage/db.py:981-1000`` 的 ``ON CONFLICT ... DO UPDATE SET`` 逐字)。

        ``value`` 落库前再兜一次 ``"" if v is None else str(v)`` —— 旧存储层的第二道
        ``str()``(旧 ``storage/db.py:997-999``):列本身可空,但**代码路径上永不写 NULL**。

        行键 = :class:`~hoteldata.domains.collect.portal.PortalExtractor` 产出的
        ``alert_portal_columns`` 列名;``detail`` 作为 ``detail_json`` 的别名接受
        (旧列名,便于迁移脚本直接喂旧结构)。**逐行** UPSERT,返回写入行数。
        """
        written = 0
        for row in rows:
            values = {
                "hotel_id": _require(row, "hotel_id"),
                "account_id": row.get("account_id"),
                "collect_date": _as_date(_require(row, "collect_date")),
                "page": _require(row, "page"),
                "column_name": _require(row, "column_name"),
                "value": "" if row.get("value") is None else str(row.get("value")),
                "detail_json": row.get("detail_json", row.get("detail")),
                "raw_json_path": row.get("raw_json_path"),
                "channel": row.get("channel") or "api",
                "status": row.get("status") or "ok",
                "error": row.get("error"),
            }
            stmt = pg_insert(AlertPortalColumn).values(**values)
            stmt = stmt.on_conflict_do_update(
                index_elements=["hotel_id", "collect_date", "page", "column_name"],
                set_={
                    key: getattr(stmt.excluded, key)
                    for key in (
                        "account_id",
                        "value",
                        "detail_json",
                        "raw_json_path",
                        "channel",
                        "status",
                        "error",
                    )
                },
            ).returning(AlertPortalColumn.id)
            await self.session.scalar(stmt)
            written += 1
        return written

    async def replace_room_states(
        self,
        hotel_id: int,
        collect_date: date | datetime | str,
        rows: Sequence[Mapping[str, Any]],
        *,
        account_id: int | None = None,
        raw_json_path: str | None = None,
    ) -> int:
        """★ 整批替换房态(T4.2)—— **同一事务内先 DELETE 再 INSERT**。

        ``DELETE`` 条件**只有两个键**:``hotel_id`` + ``collect_date``
        —— **不按 ``account_id``、不按 ``effect_date``**(旧 ``storage/db.py:1036-1061`` 逐字)。
        两条语句之间**没有 COMMIT**,所以调用方必须把它们放在同一个
        ``async with db.session()`` 块里(由 :class:`~hoteldata.infra.db.Database` 统一提交)。

        ⚠️ **这不是 UPSERT**:与 ``alert_portal_columns`` / ``review_reviews`` /
        ``review_materials`` 三表的 ``ON CONFLICT`` 策略**本质不同**,幂等靠"先删干净再重建"。

        ⚠️ **降级路径绝不可调用本方法** —— 传空 ``rows`` 会把当天旧数据**清空**。
        :class:`~hoteldata.domains.collect.room.RoomExtractor` 用 ``records=None``
        表达"别调我",调用方据此跳过(旧系统靠"失败提前 return 不碰库"避免,新实现结构化)。

        ``available`` 落库前 ``int(bool(...))``、``room_type_id`` 落库前
        ``str(... or "")``(旧 ``storage/db.py:1056-1057`` 的第二道兜底)。返回写入行数。
        """
        await self.session.execute(
            delete(AlertRoomState).where(
                AlertRoomState.hotel_id == hotel_id,
                AlertRoomState.collect_date == _as_date(collect_date),
            )
        )
        payload: list[dict[str, Any]] = []
        for row in rows:
            payload.append(
                {
                    "hotel_id": hotel_id,
                    "account_id": row.get("account_id", account_id),
                    "collect_date": _as_date(row.get("collect_date") or collect_date),
                    "room_type_id": str(row.get("room_type_id") or ""),
                    "room_name": row.get("room_name"),
                    "effect_date": _as_date(_require(row, "effect_date")),
                    "available": int(bool(row.get("available"))),
                    "status_code": row.get("status_code"),
                    "quantity": _opt_int(row.get("quantity")),
                    "price": _opt_float(row.get("price")),
                    "raw_json_path": row.get("raw_json_path", raw_json_path),
                }
            )
        if not payload:
            return 0
        await self.session.execute(pg_insert(AlertRoomState), payload)
        return len(payload)

    async def upsert_reviews(self, rows: Sequence[Mapping[str, Any]]) -> list[int]:
        """★★ UPSERT 点评(T4.3)—— **不回溯**,逐行返回 ``id``。

        两条**独立**机制,缺一条即破坏「已回复不回溯」(旧 ``storage/db.py:1169-1195`` 逐字):

          ① ``replied`` **根本不在 ``INSERT`` 列清单里** → 新行走列默认 ``0``;
             冲突行走 ``DO UPDATE SET``,而 ``DO UPDATE SET`` 里**也没有** ``replied``
             → **已回复状态永久保持**;
          ② ``strategy`` **在 ``INSERT`` 列清单里**(首次插入可落快照),但被
             ``DO UPDATE SET`` **显式排除** → 冲突时传入的 ``strategy`` **被丢弃**,
             采集重跑**永不覆盖**它 —— ``strategy`` 只能由回复流程写入。

        ``DO UPDATE SET`` 覆盖 6 项:``user_name`` / ``star`` / ``content`` / ``sentiment`` /
        ``comment_time`` / ``fetched_at=now()``(旧实现每次冲突都刷新 ``fetched_at``,漂移 C-18)。
        ``sentiment`` 缺省 ``'good'`` 与旧建表 ``DEFAULT 'good'`` 同口径(规格 §3.9 ★)。
        """
        ids: list[int] = []
        for row in rows:
            values = {
                "hotel_id": _require(row, "hotel_id"),
                "review_id": str(_require(row, "review_id")),
                "user_name": row.get("user_name"),
                "star": _opt_int(row.get("star")),
                "content": _require(row, "content"),
                "sentiment": row.get("sentiment") or "good",
                "strategy": row.get("strategy"),  # ★ 在 INSERT 列清单里
                "comment_time": row.get("comment_time"),
                # ★★ 这里**没有** replied —— 新行走列默认 0,是「不回溯」机制 ①
            }
            stmt = pg_insert(ReviewReview).values(**values)
            stmt = stmt.on_conflict_do_update(
                index_elements=["hotel_id", "review_id"],
                set_={
                    "user_name": stmt.excluded.user_name,
                    "star": stmt.excluded.star,
                    "content": stmt.excluded.content,
                    "sentiment": stmt.excluded.sentiment,
                    "comment_time": stmt.excluded.comment_time,
                    "fetched_at": func.now(),
                    # ★★ 既不含 replied 也不含 strategy —— 机制 ① 与 ②
                },
            ).returning(ReviewReview.id)
            ids.append(int(await self.session.scalar(stmt) or 0))
        return ids

    async def upsert_review_materials(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """UPSERT 点评素材(T4.3)—— 唯一键 ``(hotel_id, collect_date, kind)``,覆盖 5 列。

        ``payload_json`` 是 **NOT NULL**:传 ``None`` 时按旧口径写 **JSON 字面量 ``null``**
        (旧 ``json.dumps(None)``),**永不写 SQL NULL**
        (旧 ``storage/db.py:1321-1346``)。``kind`` 实际有 **4** 类
        (``score`` / ``competitor`` / ``trend`` / ``num``),本方法不做白名单限制。
        返回写入行数。
        """
        written = 0
        for row in rows:
            payload = row.get("payload_json", row.get("payload"))
            values = {
                "hotel_id": _require(row, "hotel_id"),
                "collect_date": _as_date(_require(row, "collect_date")),
                "kind": str(_require(row, "kind")),
                # ★ None → 'null'::jsonb(旧系统写 JSON 字面串 "null",列是 NOT NULL)
                "payload_json": payload if payload is not None else text("'null'::jsonb"),
                "raw_json_path": row.get("raw_json_path"),
                "channel": row.get("channel") or "api",
                "status": row.get("status") or "ok",
                "error": row.get("error"),
            }
            stmt = pg_insert(ReviewMaterial).values(**values)
            stmt = stmt.on_conflict_do_update(
                index_elements=["hotel_id", "collect_date", "kind"],
                set_={
                    key: getattr(stmt.excluded, key)
                    for key in (
                        "payload_json",
                        "raw_json_path",
                        "channel",
                        "status",
                        "error",
                    )
                },
            )
            await self.session.execute(stmt)
            written += 1
        return written

    # ---- 读方法(段2 / CLI 自检用) ----

    async def list_portal_columns(
        self,
        hotel_id: int,
        collect_date: date,
        *,
        page: str | None = None,
    ) -> list[AlertPortalColumn]:
        """按 ``(hotel_id, collect_date[, page])`` 读预警采集列。"""
        stmt = (
            select(AlertPortalColumn)
            .where(
                AlertPortalColumn.hotel_id == hotel_id,
                AlertPortalColumn.collect_date == collect_date,
            )
            .order_by(AlertPortalColumn.page, AlertPortalColumn.column_name)
        )
        if page:
            stmt = stmt.where(AlertPortalColumn.page == page)
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_room_states(
        self,
        hotel_id: int,
        collect_date: date,
        *,
        effect_date: date | None = None,
    ) -> list[AlertRoomState]:
        """按 ``(hotel_id, collect_date[, effect_date])`` 读房态(整批替换后的当天快照)。"""
        stmt = (
            select(AlertRoomState)
            .where(
                AlertRoomState.hotel_id == hotel_id,
                AlertRoomState.collect_date == collect_date,
            )
            .order_by(AlertRoomState.room_type_id, AlertRoomState.effect_date)
        )
        if effect_date is not None:
            stmt = stmt.where(AlertRoomState.effect_date == effect_date)
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_reviews(
        self,
        hotel_id: int,
        *,
        replied: int | None = None,
        sentiment: str | None = None,
        limit: int | None = None,
    ) -> list[ReviewReview]:
        """读点评(段2 回复流程用)。

        ★ 默认**只按酒店**过滤、按 ``comment_time`` 倒序:采集侧不写只读的
        ``replied`` / ``strategy``,所以这两个字段是**回复流程的状态**,查询方自己决定
        怎么过滤(``replied=0`` = 待处理)。
        """
        stmt = select(ReviewReview).where(ReviewReview.hotel_id == hotel_id)
        if replied is not None:
            stmt = stmt.where(ReviewReview.replied == replied)
        if sentiment:
            stmt = stmt.where(ReviewReview.sentiment == sentiment)
        stmt = stmt.order_by(ReviewReview.comment_time.desc().nullslast(), ReviewReview.id.desc())
        if limit is not None:
            stmt = stmt.limit(int(limit))
        return list((await self.session.execute(stmt)).scalars().all())

    async def latest_module(
        self,
        hotel_id: int,
        module: str,
        window: str | None = None,
    ) -> CollectModule | None:
        """取模块记录的**最新一条**(T4.1 的 ``audit_pending`` / ``violation_pending`` 派生用)。

        逐字复现旧 ``_module_latest``(``portal_columns.py:427-431``)+
        ``query_module_records`` 的排序(``storage/db.py:871``)::

            ORDER BY collect_date, page, module, window, id   → 取列表**末元素**

        ``ORDER BY`` 升序后取末元素 ≡ **逆序取第一条**,故这里是
        ``ORDER BY collect_date DESC, page DESC, module DESC, window DESC, id DESC LIMIT 1``。
        即"``collect_date`` 最大;同日期时 ``id`` 最大"。

        ⚠️ **没有 ``collect_date`` 过滤** —— 可能取到历史任意一天的最新记录(旧口径如此,
        不要"顺手"加上当天过滤)。两级回退(先 ``module + window``,为空才只按 ``module``)
        由调用方 :class:`~hoteldata.domains.collect.portal.PortalExtractor` 完成:
        ``window=None`` 即第二级。
        """
        stmt = select(CollectModule).where(
            CollectModule.hotel_id == hotel_id,
            CollectModule.module == module,
        )
        if window is not None:
            stmt = stmt.where(CollectModule.window == window)
        stmt = stmt.order_by(
            CollectModule.collect_date.desc(),
            CollectModule.page.desc(),
            CollectModule.module.desc(),
            CollectModule.window.desc(),
            CollectModule.id.desc(),
        ).limit(1)
        return (await self.session.execute(stmt)).scalars().first()


# ---------------------------------------------------------------------------
# 批次 D 行参数的小工具(私有)
# ---------------------------------------------------------------------------


def _require(row: Mapping[str, Any], key: str) -> Any:
    """取必填列;缺失即报错(**不静默写 NULL**)。"""
    if key not in row or row[key] is None:
        raise ValueError(f"批次 D 落库行缺少必填列 {key!r}: {dict(row)!r}")
    return row[key]


def _as_date(value: Any) -> date:
    """归一为 ``date``:``date`` / ``datetime`` 原样,``"YYYY-MM-DD"`` 解析(旧库是 TEXT)。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _opt_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)
