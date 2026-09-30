"""后台各业务页面(酒店 / 账号 / 机器人 / 群绑定 / 比价 / 运行审计)。

由 :func:`register` 把路由挂到 ``admin.py`` 的同一个 ``APIRouter`` 上。

★ 三条纪律(与 admin.py 一致,这里再强调一次因为它们都在本模块落地)
================================================================

1. **每个写操作都审计,包括失败的**:删除不存在的目标也要留一行 ``result='failed'``
   —— "谁在什么时候试图删什么"是有价值的信息。
2. **凭据只进不出**:账号密码 / 机器人 ``secret`` 用 ``infra/crypto`` 的 Fernet 加密后落库;
   列表页**只显示"已加密"与长度**,编辑时留空表示"不改动"。
3. **不重写业务逻辑**:比价目标走 ``CompareRepository``、任务执行走 ``runtime.tasks``、
   测试推送走 ``runtime.push``。本模块只做"表单 → 服务调用 → 重定向"。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from hoteldata.domains.compare.repository import CompareRepository
from hoteldata.infra.crypto import get_cipher
from hoteldata.infra.models import (
    BOT_STATUSES,
    Account,
    AdminAudit,
    AlertLog,
    AlertState,
    Bot,
    CmpPriceComparison,
    CmpPriceTarget,
    GroupBinding,
    Hotel,
    JobRun,
    PushLog,
)
from hoteldata.infra.models import (
    Session as SessionRow,
)
from hoteldata.web.routes.admin_shared import (
    audit,
    guard,
    int_or,
    login_redirect,
    page_ctx,
    runtime_of,
    session_of,
)
from hoteldata.web.templating import render

__all__ = ["register"]

#: 列表页默认行数上限(后台是运维界面,不做分页组件 —— 用上限 + 关键筛选)
PAGE_LIMIT = 500


def _toast(ok: str | None = None, err: str | None = None, warn: str | None = None) -> RedirectResponse:
    """POST → 303 重定向(PRG 模式,防刷新重复提交)。"""
    from urllib.parse import quote

    if ok:
        return RedirectResponse(f"?ok={quote(ok)}", status_code=303)
    if err:
        return RedirectResponse(f"?err={quote(err)}", status_code=303)
    if warn:
        return RedirectResponse(f"?warn={quote(warn)}", status_code=303)
    return RedirectResponse("?", status_code=303)


async def _need_login(request: Request) -> Response | None:
    """返回非 None = 未登录,调用方应立即返回。"""
    return None if session_of(request) is not None else login_redirect(request)


# ===========================================================================
# 注册
# ===========================================================================


def register(router: APIRouter) -> None:  # noqa: C901 - 注册即清单一屏可读,不拆
    """把所有业务页面路由挂到 ``router``。"""

    # ==================================================================
    # 酒店
    # ==================================================================

    @router.get("/admin/hotels", response_class=HTMLResponse)
    async def hotels(request: Request) -> Response:
        blocked = await guard(request)
        if blocked is not None:
            return blocked
        if (r := await _need_login(request)) is not None:
            return r
        rt = runtime_of(request)
        async with rt.db.session() as s:
            rows = list((await s.execute(select(Hotel).order_by(Hotel.id))).scalars().all())
            accs = {
                a.id: a.alias
                for a in (await s.execute(select(Account))).scalars().all()
            }
            # 每店绑定群数
            bind_rows = (
                await s.execute(
                    select(GroupBinding.hotel_id, func.count()).group_by(GroupBinding.hotel_id)
                )
            ).all()
            binds = {int(k): int(v) for k, v in bind_rows}
            # 每店比价目标
            tgt_rows = (
                await s.execute(
                    select(CmpPriceTarget.hotel_id, func.count())
                    .where(CmpPriceTarget.hotel_id.is_not(None))
                    .group_by(CmpPriceTarget.hotel_id)
                )
            ).all()
            tgts = {int(k): int(v) for k, v in tgt_rows}
        return render(
            "hotels.html",
            **page_ctx(request, "hotels", hotels=rows, accounts=accs, binds=binds, targets=tgts),
        )

    @router.post("/admin/hotels/create")
    async def hotel_create(
        request: Request,
        name: str = Form(...),
        city: str = Form(""),
        account_id: str = Form(""),
        ebk_hotel_id: str = Form(""),
    ) -> Response:
        blocked = await guard(request)
        if blocked is not None:
            return blocked
        if (r := await _need_login(request)) is not None:
            return r
        rt = runtime_of(request)
        name = name.strip()
        if not name:
            return _toast(err="酒店名不能为空")
        async with rt.db.session() as s:
            s.add(
                Hotel(
                    name=name,
                    city=city.strip() or None,
                    account_id=int_or(account_id) or None,
                    ebk_hotel_id=ebk_hotel_id.strip() or None,
                )
            )
            try:
                await s.commit()
            except IntegrityError:
                await s.rollback()
                await audit(
                    request, action="hotel.create", target_type="hotel", target_id=name,
                    detail={"city": city, "ebk_hotel_id": ebk_hotel_id},
                    result="failed", error="酒店名重复(name UNIQUE)",
                )
                return _toast(err=f"酒店「{name}」已存在(name 唯一)")
        await audit(
            request, action="hotel.create", target_type="hotel", target_id=name,
            detail={"city": city or None, "ebk_hotel_id": ebk_hotel_id or None},
        )
        return _toast(ok=f"已新增酒店:{name}")

    @router.post("/admin/hotels/{hotel_id}/update")
    async def hotel_update(
        request: Request,
        hotel_id: int,
        name: str = Form(...),
        city: str = Form(""),
        account_id: str = Form(""),
        ebk_hotel_id: str = Form(""),
    ) -> Response:
        blocked = await guard(request)
        if blocked is not None:
            return blocked
        if (r := await _need_login(request)) is not None:
            return r
        rt = runtime_of(request)
        async with rt.db.session() as s:
            row = await s.get(Hotel, hotel_id)
            if row is None:
                await audit(
                    request, action="hotel.update", target_type="hotel", target_id=str(hotel_id),
                    result="failed", error="酒店不存在",
                )
                return _toast(err="酒店不存在")
            before = {"name": row.name, "city": row.city, "ebk_hotel_id": row.ebk_hotel_id}
            row.name = name.strip()
            row.city = city.strip() or None
            row.account_id = int_or(account_id) or None
            row.ebk_hotel_id = ebk_hotel_id.strip() or None
            after = {"name": row.name, "city": row.city, "ebk_hotel_id": row.ebk_hotel_id}
            try:
                await s.commit()
            except IntegrityError:
                await s.rollback()
                await audit(
                    request, action="hotel.update", target_type="hotel", target_id=str(hotel_id),
                    detail={"before": before, "after": after},
                    result="failed", error="酒店名重复",
                )
                return _toast(err="酒店名与其他酒店重复")
        await audit(
            request, action="hotel.update", target_type="hotel", target_id=name.strip(),
            detail={"before": before, "after": after},
        )
        return _toast(ok=f"已更新酒店:{name.strip()}")

    @router.post("/admin/hotels/{hotel_id}/delete")
    async def hotel_delete(request: Request, hotel_id: int) -> Response:
        blocked = await guard(request)
        if blocked is not None:
            return blocked
        if (r := await _need_login(request)) is not None:
            return r
        rt = runtime_of(request)
        async with rt.db.session() as s:
            row = await s.get(Hotel, hotel_id)
            if row is None:
                await audit(
                    request, action="hotel.delete", target_type="hotel", target_id=str(hotel_id),
                    result="failed", error="酒店不存在",
                )
                return _toast(err="酒店不存在")
            name, city, ebk = row.name, row.city, row.ebk_hotel_id
            binds = int(
                (
                    await s.execute(
                        select(func.count())
                        .select_from(GroupBinding)
                        .where(GroupBinding.hotel_id == hotel_id)
                    )
                ).scalar()
                or 0
            )
            await s.delete(row)
            await s.commit()
        await audit(
            request, action="hotel.delete", target_type="hotel", target_id=name,
            detail={"city": city, "ebk_hotel_id": ebk, "群绑定级联删除": binds},
        )
        extra = f"(级联删除 {binds} 条群绑定)" if binds else ""
        return _toast(ok=f"已删除酒店:{name}{extra}")

    # ==================================================================
    # 账号
    # ==================================================================

    @router.get("/admin/accounts", response_class=HTMLResponse)
    async def accounts(request: Request) -> Response:
        blocked = await guard(request)
        if blocked is not None:
            return blocked
        if (r := await _need_login(request)) is not None:
            return r
        rt = runtime_of(request)
        async with rt.db.session() as s:
            rows = list((await s.execute(select(Account).order_by(Account.id))).scalars().all())
            sess = list((await s.execute(select(SessionRow))).scalars().all())
        # 登录态按 (platform, alias) 归到账号(ebooking 角色才是该账号的后台会话)
        by_alias: dict[str, list[Any]] = {}
        for r_ in sess:
            by_alias.setdefault(r_.alias, []).append(r_)
        return render(
            "accounts.html",
            **page_ctx(request, "accounts", accounts=rows, sessions=by_alias),
        )

    @router.post("/admin/accounts/create")
    async def account_create(
        request: Request,
        alias: str = Form(...),
        platform: str = Form("ctrip"),
        username: str = Form(...),
        password: str = Form(...),
        is_multi: str = Form(""),
    ) -> Response:
        blocked = await guard(request)
        if blocked is not None:
            return blocked
        if (r := await _need_login(request)) is not None:
            return r
        rt = runtime_of(request)
        alias = alias.strip()
        if not alias or not username.strip() or not password:
            return _toast(err="别名 / 用户名 / 密码都不能为空")
        cipher = get_cipher(rt.settings)
        async with rt.db.session() as s:
            s.add(
                Account(
                    alias=alias,
                    platform=platform,
                    username_enc=cipher.encrypt(username.strip()),
                    password_enc=cipher.encrypt(password),
                    is_multi=bool(is_multi),
                )
            )
            try:
                await s.commit()
            except IntegrityError:
                await s.rollback()
                await audit(
                    request, action="account.create", target_type="account", target_id=alias,
                    result="failed", error="别名重复",
                )
                return _toast(err=f"别名「{alias}」已存在")
        # ★ 审计里**只记别名与平台**,绝不记用户名/密码
        await audit(
            request, action="account.create", target_type="account", target_id=alias,
            detail={"platform": platform, "is_multi": bool(is_multi), "凭据": "已 Fernet 加密落库"},
        )
        return _toast(ok=f"已新增账号:{alias}(凭据已加密)")

    @router.post("/admin/accounts/{account_id}/update")
    async def account_update(
        request: Request,
        account_id: int,
        password: str = Form(""),
        status: str = Form("active"),
        is_multi: str = Form(""),
    ) -> Response:
        blocked = await guard(request)
        if blocked is not None:
            return blocked
        if (r := await _need_login(request)) is not None:
            return r
        rt = runtime_of(request)
        cipher = get_cipher(rt.settings)
        async with rt.db.session() as s:
            row = await s.get(Account, account_id)
            if row is None:
                await audit(
                    request, action="account.update", target_type="account",
                    target_id=str(account_id), result="failed", error="账号不存在",
                )
                return _toast(err="账号不存在")
            changed = {"status": [row.status, status], "is_multi": [row.is_multi, bool(is_multi)]}
            row.status = status
            row.is_multi = bool(is_multi)
            # ★ 留空 = 不改密码(页面永不回显明文,所以"留空"必须是这个语义)
            if password:
                row.password_enc = cipher.encrypt(password)
                changed["password"] = ["(已加密)", "(已重置)"]
            await s.commit()
            alias = row.alias
        await audit(
            request, action="account.update", target_type="account", target_id=alias,
            detail=changed,
        )
        return _toast(ok=f"已更新账号:{alias}" +("(含密码重置)" if password else ""))

    @router.post("/admin/accounts/{account_id}/delete")
    async def account_delete(request: Request, account_id: int) -> Response:
        blocked = await guard(request)
        if blocked is not None:
            return blocked
        if (r := await _need_login(request)) is not None:
            return r
        rt = runtime_of(request)
        async with rt.db.session() as s:
            row = await s.get(Account, account_id)
            if row is None:
                await audit(
                    request, action="account.delete", target_type="account",
                    target_id=str(account_id), result="failed", error="账号不存在",
                )
                return _toast(err="账号不存在")
            alias = row.alias
            # 该账号下的酒店 account_id 会被 SET NULL(外键),要提示
            hotels = int(
                (
                    await s.execute(
                        select(func.count()).select_from(Hotel).where(Hotel.account_id == account_id)
                    )
                ).scalar()
                or 0
            )
            await s.delete(row)
            await s.commit()
        await audit(
            request, action="account.delete", target_type="account", target_id=alias,
            detail={"关联酒店数": hotels, "关联酒店 account_id 置空": True},
        )
        extra = f"({hotels} 家酒店的 account_id 已置空)" if hotels else ""
        return _toast(ok=f"已删除账号:{alias}{extra}")

    # ==================================================================
    # 机器人
    # ==================================================================

    @router.get("/admin/bots", response_class=HTMLResponse)
    async def bots(request: Request) -> Response:
        blocked = await guard(request)
        if blocked is not None:
            return blocked
        if (r := await _need_login(request)) is not None:
            return r
        rt = runtime_of(request)
        async with rt.db.session() as s:
            rows = list((await s.execute(select(Bot).order_by(Bot.id))).scalars().all())
            # 每个机器人名下绑定的群数(md5(chatid)%n 路由是运行时算的,这里只给总量参考)
            total_binds = int(
                (await s.execute(select(func.count()).select_from(GroupBinding))).scalar() or 0
            )
        health: dict[str, bool] = {}
        if getattr(rt, "_bots", None) is not None:
            try:
                health = await rt.bots.health()
            except Exception as exc:  # noqa: BLE001
                logger.warning("取机器人健康失败: {}", exc)
        return render(
            "bots.html",
            **page_ctx(
                request,
                "bots",
                bots=rows,
                health=health,
                total_binds=total_binds,
                bot_statuses=BOT_STATUSES,
            ),
        )

    @router.post("/admin/bots/create")
    async def bot_create(
        request: Request,
        name: str = Form(...),
        bot_id: str = Form(...),
        secret: str = Form(...),
        capacity: str = Form(""),
    ) -> Response:
        blocked = await guard(request)
        if blocked is not None:
            return blocked
        if (r := await _need_login(request)) is not None:
            return r
        rt = runtime_of(request)
        name = name.strip()
        if not name or not bot_id.strip() or not secret:
            return _toast(err="名称 / bot_id / secret 都不能为空")
        cipher = get_cipher(rt.settings)
        async with rt.db.session() as s:
            s.add(
                Bot(
                    name=name,
                    bot_id_enc=cipher.encrypt(bot_id.strip()),
                    secret_enc=cipher.encrypt(secret),
                    capacity_per_bot=int_or(capacity) or rt.settings.bot.capacity_per_bot,
                )
            )
            try:
                await s.commit()
            except IntegrityError:
                await s.rollback()
                await audit(
                    request, action="bot.create", target_type="bot", target_id=name,
                    result="failed", error="机器人名重复",
                )
                return _toast(err=f"机器人「{name}」已存在")
        await audit(
            request, action="bot.create", target_type="bot", target_id=name,
            detail={"bot_id 与 secret": "已 Fernet 加密落库(页面不回显)"},
        )
        return _toast(ok=f"已新增机器人:{name}。**需重启 serve 才会建立长连接**")

    @router.post("/admin/bots/{bot_id}/update")
    async def bot_update(
        request: Request,
        bot_id: int,
        secret: str = Form(""),
        status: str = Form("active"),
        capacity: str = Form(""),
    ) -> Response:
        blocked = await guard(request)
        if blocked is not None:
            return blocked
        if (r := await _need_login(request)) is not None:
            return r
        rt = runtime_of(request)
        cipher = get_cipher(rt.settings)
        async with rt.db.session() as s:
            row = await s.get(Bot, bot_id)
            if row is None:
                await audit(
                    request, action="bot.update", target_type="bot", target_id=str(bot_id),
                    result="failed", error="机器人不存在",
                )
                return _toast(err="机器人不存在")
            name = row.name
            changed = {"status": [row.status, status]}
            row.status = status
            row.capacity_per_bot = int_or(capacity) or row.capacity_per_bot
            if secret:
                row.secret_enc = cipher.encrypt(secret)
                changed["secret"] = ["(已加密)", "(已重置)"]
            await s.commit()
        await audit(request, action="bot.update", target_type="bot", target_id=name, detail=changed)
        return _toast(ok=f"已更新机器人:{name}")

    @router.post("/admin/bots/{bot_id}/delete")
    async def bot_delete(request: Request, bot_id: int) -> Response:
        blocked = await guard(request)
        if blocked is not None:
            return blocked
        if (r := await _need_login(request)) is not None:
            return r
        rt = runtime_of(request)
        async with rt.db.session() as s:
            row = await s.get(Bot, bot_id)
            if row is None:
                await audit(
                    request, action="bot.delete", target_type="bot", target_id=str(bot_id),
                    result="failed", error="机器人不存在",
                )
                return _toast(err="机器人不存在")
            name = row.name
            await s.delete(row)
            await s.commit()
        await audit(request, action="bot.delete", target_type="bot", target_id=name)
        return _toast(ok=f"已删除机器人:{name}。需重启 serve 生效")

    # ==================================================================
    # 群绑定
    # ==================================================================

    @router.get("/admin/bindings", response_class=HTMLResponse)
    async def bindings(request: Request) -> Response:
        blocked = await guard(request)
        if blocked is not None:
            return blocked
        if (r := await _need_login(request)) is not None:
            return r
        rt = runtime_of(request)
        async with rt.db.session() as s:
            rows = list(
                (
                    await s.execute(
                        select(GroupBinding, Hotel)
                        .join(Hotel, Hotel.id == GroupBinding.hotel_id, isouter=True)
                        .order_by(GroupBinding.chatid, Hotel.name)
                    )
                ).all()
            )
            hotels = list((await s.execute(select(Hotel).order_by(Hotel.name))).scalars().all())
        items = [
            {
                "id": b.id,
                "chatid": b.chatid,
                "hotel_id": b.hotel_id,
                "hotel_name": h.name if h else "(酒店已删除)",
                "paused": b.paused,
                "created_at": b.created_at,
            }
            for b, h in rows
        ]
        groups = sorted({i["chatid"] for i in items})
        return render(
            "bindings.html",
            **page_ctx(request, "bindings", items=items, hotels=hotels, groups=groups,
                       manage=rt.settings.push.manage_chatids, ops=rt.settings.push.ops_chatid),
        )

    @router.post("/admin/bindings/create")
    async def binding_create(
        request: Request, chatid: str = Form(...), hotel_id: str = Form(...)
    ) -> Response:
        blocked = await guard(request)
        if blocked is not None:
            return blocked
        if (r := await _need_login(request)) is not None:
            return r
        rt = runtime_of(request)
        cid, hid = chatid.strip(), int_or(hotel_id)
        if not cid or not hid:
            return _toast(err="群 chatid 与酒店都不能为空")
        created = await rt.bindings.bind(cid, hid)
        await audit(
            request, action="binding.create", target_type="binding", target_id=cid,
            detail={"hotel_id": hid, "本次新建": created},
        )
        return _toast(ok=("已绑定" if created else "该绑定本来就存在(幂等,未重复创建)"))

    @router.post("/admin/bindings/{binding_id}/pause")
    async def binding_pause(request: Request, binding_id: int) -> Response:
        blocked = await guard(request)
        if blocked is not None:
            return blocked
        if (r := await _need_login(request)) is not None:
            return r
        rt = runtime_of(request)
        async with rt.db.session() as s:
            row = await s.get(GroupBinding, binding_id)
            if row is None:
                await audit(
                    request, action="binding.pause", target_type="binding",
                    target_id=str(binding_id), result="failed", error="绑定不存在",
                )
                return _toast(err="绑定不存在")
            row.paused = not row.paused
            cid, hid, paused = row.chatid, row.hotel_id, row.paused
            await s.commit()
        await audit(
            request, action="binding.pause", target_type="binding", target_id=cid,
            detail={"hotel_id": hid, "paused": paused},
        )
        return _toast(ok=f"已{'暂停' if paused else '恢复'}该绑定(群 {cid[:12]}… → 酒店 {hid})")

    @router.post("/admin/bindings/{binding_id}/delete")
    async def binding_delete(request: Request, binding_id: int) -> Response:
        blocked = await guard(request)
        if blocked is not None:
            return blocked
        if (r := await _need_login(request)) is not None:
            return r
        rt = runtime_of(request)
        async with rt.db.session() as s:
            row = await s.get(GroupBinding, binding_id)
            if row is None:
                await audit(
                    request, action="binding.delete", target_type="binding",
                    target_id=str(binding_id), result="failed", error="绑定不存在",
                )
                return _toast(err="绑定不存在")
            cid, hid = row.chatid, row.hotel_id
            await s.delete(row)
            await s.commit()
        await audit(
            request, action="binding.delete", target_type="binding", target_id=cid,
            detail={"hotel_id": hid},
        )
        return _toast(ok="已解绑")

    # ==================================================================
    # 比价目标
    # ==================================================================

    @router.get("/admin/targets", response_class=HTMLResponse)
    async def targets(request: Request) -> Response:
        blocked = await guard(request)
        if blocked is not None:
            return blocked
        if (r := await _need_login(request)) is not None:
            return r
        rt = runtime_of(request)
        async with rt.db.session() as s:
            repo = CompareRepository(s)
            rows = await repo.list_targets(enabled_only=False)
            hotels = list((await s.execute(select(Hotel).order_by(Hotel.name))).scalars().all())
            # 每店今日比价行数(给运营一个"这家店到底有没有在采"的直觉)
            today = datetime.now(rt.settings.tzinfo).date()
            cnt = (
                await s.execute(
                    select(CmpPriceComparison.anchor_name, func.count())
                    .where(CmpPriceComparison.query_date == today)
                    .group_by(CmpPriceComparison.anchor_name)
                )
            ).all()
            today_counts = {str(k): int(v) for k, v in cnt}
        return render(
            "targets.html",
            **page_ctx(request, "targets", targets=rows, hotels=hotels, today_counts=today_counts),
        )

    @router.post("/admin/targets/create")
    async def target_create(
        request: Request,
        anchor_name: str = Form(...),
        city: str = Form(""),
        mode: str = Form("batch"),
        platforms: str = Form("ctrip,meituan"),
        nights: str = Form("1"),
        ebk_hotel_id: str = Form(""),
    ) -> Response:
        blocked = await guard(request)
        if blocked is not None:
            return blocked
        if (r := await _need_login(request)) is not None:
            return r
        rt = runtime_of(request)
        name = anchor_name.strip()
        if not name:
            return _toast(err="锚点酒店名不能为空")
        # ★ 平台用逗号分隔的文本(与 CLI `--platforms` 同一形态):
        #   FastAPI 要在参数默认值里调 `Form(...)`,多选列表的写法会被 ruff B008 拦;
        #   而且逗号串与 `.env` 的 HOTEL_PLATFORMS 形态一致,人不用学两套。
        plats = [p.strip() for p in platforms.replace("，", ",").split(",") if p.strip()]
        plats = [p for p in plats if p] or ["ctrip", "meituan"]
        async with rt.db.session() as s:
            repo = CompareRepository(s)
            tid = await repo.upsert_target(
                anchor_name=name,
                city=city.strip() or None,
                mode=mode,
                platforms=plats,
                nights=int_or(nights, 1) or 1,
                ebk_hotel_id=ebk_hotel_id.strip() or None,
            )
            await repo.backfill_target_hotel_ids()
            await s.commit()
        await audit(
            request, action="target.create", target_type="target", target_id=name,
            detail={"city": city or None, "mode": mode, "platforms": plats},
        )
        return _toast(ok=f"已保存比价目标(id={tid}):{name}")

    @router.post("/admin/targets/{target_id}/toggle")
    async def target_toggle(request: Request, target_id: int) -> Response:
        blocked = await guard(request)
        if blocked is not None:
            return blocked
        if (r := await _need_login(request)) is not None:
            return r
        rt = runtime_of(request)
        async with rt.db.session() as s:
            row = await s.get(CmpPriceTarget, target_id)
            if row is None:
                await audit(
                    request, action="target.update", target_type="target",
                    target_id=str(target_id), result="failed", error="目标不存在",
                )
                return _toast(err="目标不存在")
            row.enabled = not row.enabled
            name, enabled = row.anchor_name, row.enabled
            await s.commit()
        await audit(
            request, action="target.update", target_type="target", target_id=name,
            detail={"enabled": enabled},
        )
        return _toast(ok=f"已{'启用' if enabled else '停用'}:{name}")

    @router.post("/admin/targets/{target_id}/delete")
    async def target_delete(request: Request, target_id: int) -> Response:
        blocked = await guard(request)
        if blocked is not None:
            return blocked
        if (r := await _need_login(request)) is not None:
            return r
        rt = runtime_of(request)
        async with rt.db.session() as s:
            row = await s.get(CmpPriceTarget, target_id)
            if row is None:
                await audit(
                    request, action="target.delete", target_type="target",
                    target_id=str(target_id), result="failed", error="目标不存在",
                )
                return _toast(err="目标不存在")
            name = row.anchor_name
            await s.delete(row)
            await s.commit()
        await audit(request, action="target.delete", target_type="target", target_id=name)
        return _toast(ok=f"已删除目标:{name}(历史比价记录**保留**)")

    @router.post("/admin/targets/import")
    async def target_import(request: Request, from_dir: str = Form("")) -> Response:
        blocked = await guard(request)
        if blocked is not None:
            return blocked
        if (r := await _need_login(request)) is not None:
            return r
        rt = runtime_of(request)
        path = from_dir.strip()
        if not path:
            return _toast(err="请填旧系统根目录")
        from pathlib import Path

        from hoteldata.domains.compare.importer import parse_target_files

        try:
            parsed = parse_target_files(Path(path))
        except Exception as exc:  # noqa: BLE001
            await audit(
                request, action="target.import", target_type="target", target_id=path,
                result="failed", error=str(exc),
            )
            return _toast(err=f"解析失败:{exc}")
        if not parsed:
            await audit(
                request, action="target.import", target_type="target", target_id=path,
                result="failed", error="未解析出任何目标",
            )
            return _toast(err="未在 config/ 找到可解析的 compare_hotels.txt / price_targets.txt")

        async with rt.db.session() as s:
            repo = CompareRepository(s)
            for item in parsed:
                await repo.upsert_target(**item)
            filled = await repo.backfill_target_hotel_ids()
            await s.commit()
        modes: dict[str, int] = {}
        for item in parsed:
            modes[item["mode"]] = modes.get(item["mode"], 0) + 1
        await audit(
            request, action="target.import", target_type="target", target_id=path,
            detail={"导入数": len(parsed), "mode 分布": modes, "回填 hotel_id": filled},
        )
        return _toast(
            ok=f"已导入 {len(parsed)} 个目标(mode 分布 {modes});回填 hotel_id/ebk_hotel_id {filled} 行"
        )

    # ==================================================================
    # 比价历史
    # ==================================================================

    @router.get("/admin/compare", response_class=HTMLResponse)
    async def compare_history(
        request: Request,
        anchor: str = "",
        day: str = "",
        demo: str = "",
    ) -> Response:
        blocked = await guard(request)
        if blocked is not None:
            return blocked
        if (r := await _need_login(request)) is not None:
            return r
        rt = runtime_of(request)
        today = datetime.now(rt.settings.tzinfo).date()
        target_day = date.fromisoformat(day) if day else today
        include_demo = demo == "1"
        async with rt.db.session() as s:
            rows = await CompareRepository(s).list_quotes(
                anchor_name=anchor.strip() or None,
                query_date=target_day,
                include_demo=include_demo,
                limit=PAGE_LIMIT,
            )
            anchors = list(
                (
                    await s.execute(
                        select(CmpPriceComparison.anchor_name)
                        .distinct()
                        .order_by(CmpPriceComparison.anchor_name)
                    )
                )
                .scalars()
                .all()
            )
            slots = sorted({r.query_slot for r in rows})
        with_dist = sum(1 for r in rows if r.distance_km is not None)
        priced = sum(1 for r in rows if r.price is not None)
        manual = sum(1 for r in rows if r.need_manual_check)
        return render(
            "compare.html",
            **page_ctx(
                request, "compare", rows=rows, anchors=anchors, slots=slots,
                anchor=anchor, day=target_day.isoformat(), include_demo=include_demo,
                today=today.isoformat(),
                stats={"rows": len(rows), "with_dist": with_dist, "priced": priced, "manual": manual},
            ),
        )

    # ==================================================================
    # 任务
    # ==================================================================

    @router.get("/admin/tasks", response_class=HTMLResponse)
    async def tasks(request: Request, task: str = "") -> Response:
        blocked = await guard(request)
        if blocked is not None:
            return blocked
        if (r := await _need_login(request)) is not None:
            return r
        rt = runtime_of(request)
        today = datetime.now(rt.settings.tzinfo).date()
        day_start = datetime.combine(today, datetime.min.time(), tzinfo=rt.settings.tzinfo)
        specs = [
            {
                "name": s.name,
                "cron": s.cron,
                "catch_up": s.catch_up,
                "max_delay": str(s.max_delay) if s.max_delay else "—",
                "domain": s.domain,
                "description": s.description,
            }
            for s in rt.tasks.all()
        ]
        async with rt.db.session() as s:
            stmt = (
                select(JobRun)
                .where(JobRun.started_at >= day_start - timedelta(days=2))
                .order_by(JobRun.started_at.desc())
                .limit(PAGE_LIMIT)
            )
            if task.strip():
                stmt = stmt.where(JobRun.task == task.strip())
            runs = list((await s.execute(stmt)).scalars().all())
        return render(
            "tasks.html",
            **page_ctx(
                request, "tasks", specs=specs, runs=runs, task=task,
                today=today.isoformat(),
                scheduler=rt.scheduler is not None and getattr(rt.scheduler, "running", False),
            ),
        )

    @router.post("/admin/tasks/run")
    async def task_run(request: Request, task: str = Form(...), force: str = Form("")) -> Response:
        blocked = await guard(request)
        if blocked is not None:
            return blocked
        if (r := await _need_login(request)) is not None:
            return r
        rt = runtime_of(request)
        name = task.strip()
        if not name:
            return _toast(err="任务名不能为空")
        try:
            result = await rt.tasks.run(name, rt, trigger="manual", force=bool(force))
        except Exception as exc:  # noqa: BLE001
            await audit(
                request, action="task.run", target_type="task", target_id=name,
                detail={"force": bool(force)}, result="failed", error=str(exc),
            )
            logger.error("后台手动跑任务失败 {}: {}", name, exc)
            return _toast(err=f"执行失败:{exc}")
        status = getattr(result, "status", "?")
        summary = getattr(result, "summary", None) or {}
        await audit(
            request, action="task.run", target_type="task", target_id=name,
            detail={"force": bool(force), "status": status, "summary": summary},
        )
        return _toast(ok=f"任务 {name} 执行完成:status={status} {summary}")

    # ==================================================================
    # 推送审计
    # ==================================================================

    @router.get("/admin/pushes", response_class=HTMLResponse)
    async def pushes(request: Request, ptype: str = "", status: str = "") -> Response:
        blocked = await guard(request)
        if blocked is not None:
            return blocked
        if (r := await _need_login(request)) is not None:
            return r
        rt = runtime_of(request)
        async with rt.db.session() as s:
            stmt = select(PushLog).order_by(PushLog.created_at.desc()).limit(PAGE_LIMIT)
            if ptype.strip():
                stmt = stmt.where(PushLog.push_type == ptype.strip())
            if status.strip():
                stmt = stmt.where(PushLog.status == status.strip())
            rows = list((await s.execute(stmt)).scalars().all())
            types = list(
                (await s.execute(select(PushLog.push_type).distinct().order_by(PushLog.push_type)))
                .scalars()
                .all()
            )
        stats = {"ok": 0, "failed": 0, "skipped": 0}
        for r_ in rows:
            stats[r_.status] = stats.get(r_.status, 0) + 1
        return render(
            "pushes.html",
            **page_ctx(request, "pushes", rows=rows, types=types, stats=stats,
                       ptype=ptype, status=status),
        )

    # ==================================================================
    # 预警
    # ==================================================================

    @router.get("/admin/alerts", response_class=HTMLResponse)
    async def alerts(request: Request) -> Response:
        blocked = await guard(request)
        if blocked is not None:
            return blocked
        if (r := await _need_login(request)) is not None:
            return r
        rt = runtime_of(request)
        async with rt.db.session() as s:
            logs = list(
                (await s.execute(select(AlertLog).order_by(AlertLog.created_at.desc()).limit(150)))
                .scalars()
                .all()
            )
            states = list(
                (
                    await s.execute(
                        select(AlertState).order_by(AlertState.last_trigger_date.desc().nullslast()).limit(200)
                    )
                )
                .scalars()
                .all()
            )
            hotels = {h.id: h.name for h in (await s.execute(select(Hotel))).scalars().all()}
        delivered = sum(1 for x in logs if x.pushed)
        stats = {
            "total": len(logs),
            "delivered": delivered,
            "rate": round(delivered * 100 / len(logs), 1) if logs else 0.0,
        }
        return render(
            "alerts.html",
            **page_ctx(request, "alerts", logs=logs, states=states, hotels=hotels, stats=stats),
        )

    # ==================================================================
    # 登录态
    # ==================================================================

    @router.get("/admin/sessions", response_class=HTMLResponse)
    async def sessions(request: Request) -> Response:
        blocked = await guard(request)
        if blocked is not None:
            return blocked
        if (r := await _need_login(request)) is not None:
            return r
        rt = runtime_of(request)
        async with rt.db.session() as s:
            rows = list(
                (
                    await s.execute(
                        select(SessionRow).order_by(SessionRow.platform, SessionRow.role, SessionRow.alias)
                    )
                )
                .scalars()
                .all()
            )
        items = []
        for row in rows:
            handle = rt.sessions.handle(row.platform, row.role, row.alias)
            path = handle.storage_state_path()
            items.append(
                {
                    "platform": row.platform,
                    "role": row.role,
                    "alias": row.alias,
                    "status": row.status,
                    "last_login_at": row.last_login_at,
                    "last_check_at": row.last_check_at,
                    "file_exists": path.exists(),
                    "file_mtime": datetime.fromtimestamp(path.stat().st_mtime) if path.exists() else None,
                    "file_size": path.stat().st_size if path.exists() else 0,
                    "need_renew": bool(
                        row.last_login_at
                        and (datetime.now(rt.settings.tzinfo) - row.last_login_at).days
                        >= rt.settings.login.max_age_days
                    ),
                }
            )
        return render(
            "sessions_admin.html",
            **page_ctx(request, "sessions", items=items,
                       max_age=rt.settings.login.max_age_days),
        )

    # ==================================================================
    # 操作审计
    # ==================================================================

    @router.get("/admin/audit", response_class=HTMLResponse)
    async def audit_log(request: Request, action: str = "") -> Response:
        blocked = await guard(request)
        if blocked is not None:
            return blocked
        if (r := await _need_login(request)) is not None:
            return r
        rt = runtime_of(request)
        async with rt.db.session() as s:
            stmt = select(AdminAudit).order_by(AdminAudit.created_at.desc()).limit(PAGE_LIMIT)
            if action.strip():
                stmt = stmt.where(AdminAudit.action == action.strip())
            rows = list((await s.execute(stmt)).scalars().all())
            actions = list(
                (await s.execute(select(AdminAudit.action).distinct().order_by(AdminAudit.action)))
                .scalars()
                .all()
            )
        return render(
            "audit.html", **page_ctx(request, "audit", rows=rows, actions=actions, action=action)
        )
