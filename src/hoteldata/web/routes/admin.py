"""Web 后台路由(Phase 3;总纲 §7.8)—— 登录 + 总览。

★ 范围与边界(逐条对应总纲 §7.8)
================================

=========================================  ==================================================
总纲要求                                     实现
=========================================  ==================================================
保持服务端渲染(Jinja2),不引前端构建链       ✅ 手写 ``static/admin.css``,零构建步
后台范围:账号/机器人/比价酒店增删            ✅ 本模块 + :mod:`admin_pages`
**统一口令登录**                             ✅ PBKDF2-HMAC-SHA256 + 签名 cookie
**操作审计**                                 ✅ 每个写操作(含失败)落 ``ops_admin_audit``
明确不做:角色区分、权限树、API 鉴权细化       ✅ 没做,也没铺路
=========================================  ==================================================

★ 三条工程纪律
==============

1. **写操作一律审计,包括失败的尝试** —— "谁试图删掉这家店但没成功"同样要知道。
2. **审计失败不拖垮业务操作**(与段2「预警附图失败仅告警,文本照发」同一取向),
   但 ``logger.error`` 让它可见 —— 审计悄悄不写了比操作失败更糟。
3. **凭据只进不出**:账号密码 / 机器人 secret 加密落库(``infra/crypto``),
   页面**永不回显明文**,编辑时留空 = 不改动。
   这是总纲 R4「旧仓库携带真实凭据」的直接对策。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from loguru import logger
from sqlalchemy import func, select

from hoteldata.domains.compare.repository import CompareRepository
from hoteldata.infra.models import (
    Account,
    AdminAudit,
    AlertLog,
    Bot,
    CmpPriceTarget,
    CollectReport,
    GroupBinding,
    Hotel,
    JobRun,
    PushLog,
)
from hoteldata.infra.models import (
    Session as SessionRow,
)
from hoteldata.web.auth import COOKIE_NAME, verify_password
from hoteldata.web.routes.admin_pages import register as register_pages
from hoteldata.web.routes.admin_shared import (
    audit,
    client_ip,
    guard,
    login_redirect,
    page_ctx,
    runtime_of,
    session_of,
    signer_of,
)
from hoteldata.web.templating import render

__all__ = ["router"]

router = APIRouter(tags=["admin"], include_in_schema=False)


# ===========================================================================
# 登录 / 登出
# ===========================================================================


@router.get("/admin/login", response_class=HTMLResponse)
async def login_form(request: Request) -> Response:
    blocked = await guard(request)
    if blocked is not None:
        return blocked
    if session_of(request) is not None:
        return RedirectResponse("/admin", status_code=303)
    return render(
        "login.html",
        request=request,
        error=request.query_params.get("err", ""),
        has_password=runtime_of(request).settings.web.has_password,
    )


@router.post("/admin/login")
async def login_submit(request: Request, password: str = Form("")) -> Response:
    blocked = await guard(request)
    if blocked is not None:
        return blocked
    rt = runtime_of(request)
    cfg = rt.settings.web

    # ★ 没设口令 = **拒绝一切登录**(与段2「未配置 MANAGE_CHATIDS 时管理群命令一律拒绝」
    #   同一条安全默认)。一个没口令的后台等于把"改账号/删酒店"开放给任何能访问端口的人。
    if not cfg.has_password:
        await audit(
            request,
            action="login_failed",
            target_type="admin",
            detail={"reason": "未设置后台口令"},
            result="failed",
            error="ADMIN_PASSWORD_HASH 为空",
        )
        return RedirectResponse(
            "/admin/login?err=尚未设置后台口令,请先执行 hoteldata admin passwd --set <新口令>",
            status_code=303,
        )

    if not verify_password(password, cfg.admin_password_hash):
        await audit(
            request,
            action="login_failed",
            target_type="admin",
            detail={"reason": "口令错误"},
            result="failed",
            error="口令错误",
        )
        logger.warning("后台登录失败: ip={}", client_ip(request))
        return RedirectResponse("/admin/login?err=口令错误", status_code=303)

    token, sess = signer_of(request).issue()
    await audit(request, action="login", target_type="admin", target_id=sess.actor)
    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie(
        COOKIE_NAME,
        token,
        max_age=int(cfg.session_hours) * 3600,
        httponly=True,
        samesite="lax",
        # ★ 不设 secure:后台走 http://127.0.0.1(本机明文),设了反而登录不上。
        #   这是**本机部署形态**下的正确取舍,不是疏漏。
        secure=False,
    )
    logger.info("后台登录成功: ip={}", client_ip(request))
    return resp


@router.get("/admin/logout")
async def logout(request: Request) -> Response:
    sess = session_of(request)
    if sess is not None:
        await audit(request, action="logout", target_type="admin", target_id=sess.actor)
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME)
    return resp


# ===========================================================================
# 总览
# ===========================================================================


def _job_badge(status: str) -> str:
    return {"ok": "ok", "failed": "err", "running": "info", "skipped": "dim"}.get(status, "warn")


async def _collect_dashboard(rt: Any, request: Request) -> dict[str, Any]:
    today = datetime.now(rt.settings.tzinfo).date()
    day_start = datetime.combine(today, datetime.min.time(), tzinfo=rt.settings.tzinfo)
    out: dict[str, Any] = {}

    async with rt.db.session() as s:

        async def _count(stmt: Any) -> int:
            return int((await s.execute(stmt)).scalar() or 0)

        # ---- 今日提取(四态)----
        rows = (
            await s.execute(
                select(CollectReport.status, func.count())
                .where(CollectReport.collect_date == today)
                .group_by(CollectReport.status)
            )
        ).all()
        ccounts = {str(k): int(v) for k, v in rows}
        out["collect_today"] = {
            "total": sum(ccounts.values()),
            "ok": ccounts.get("ok", 0),
            "degraded": ccounts.get("degraded", 0),
            "no_data": ccounts.get("no_data", 0),
            "failed": ccounts.get("failed", 0),
        }

        # ---- 今日推送 ----
        prows = (
            await s.execute(
                select(PushLog.status, func.count())
                .where(PushLog.created_at >= day_start)
                .group_by(PushLog.status)
            )
        ).all()
        pcounts = {str(k): int(v) for k, v in prows}
        ptotal, pok = sum(pcounts.values()), pcounts.get("ok", 0)
        out["push_today"] = {
            "total": ptotal,
            "ok": pok,
            "failed": pcounts.get("failed", 0),
            "skipped": pcounts.get("skipped", 0),
            "rate": round(pok * 100 / ptotal, 1) if ptotal else 0.0,
        }

        # ---- 今日预警(送达率 = 成功行 ÷ 总行;段2 口径,含 manage-none)----
        arows = (
            await s.execute(
                select(AlertLog.pushed, func.count())
                .where(AlertLog.created_at >= day_start)
                .group_by(AlertLog.pushed)
            )
        ).all()
        apushed = sum(int(n) for flag, n in arows if flag)
        atotal = sum(int(n) for _, n in arows)
        out["alert_today"] = {
            "total": atotal,
            "pushed": apushed,
            "rate": round(apushed * 100 / atotal, 1) if atotal else 0.0,
        }

        # ---- 实体计数 ----
        out["counts"] = {
            "hotels": await _count(select(func.count()).select_from(Hotel)),
            "hotels_no_ebk": await _count(
                select(func.count()).select_from(Hotel).where(Hotel.ebk_hotel_id.is_(None))
            ),
            "accounts": await _count(select(func.count()).select_from(Account)),
            "accounts_invalid": await _count(
                select(func.count()).select_from(Account).where(Account.status != "active")
            ),
            "bots": await _count(select(func.count()).select_from(Bot)),
            "bindings": await _count(select(func.count()).select_from(GroupBinding)),
            "bindings_paused": await _count(
                select(func.count()).select_from(GroupBinding).where(GroupBinding.paused.is_(True))
            ),
            "targets": await _count(select(func.count()).select_from(CmpPriceTarget)),
            "targets_disabled": await _count(
                select(func.count())
                .select_from(CmpPriceTarget)
                .where(CmpPriceTarget.enabled.is_(False))
            ),
            "sessions": await _count(select(func.count()).select_from(SessionRow)),
            "sessions_invalid": await _count(
                select(func.count()).select_from(SessionRow).where(SessionRow.status != "valid")
            ),
        }

        # ---- 今日任务 ----
        jrows = (
            (
                await s.execute(
                    select(JobRun)
                    .where(JobRun.started_at >= day_start)
                    .order_by(JobRun.started_at.desc())
                    .limit(12)
                )
            )
            .scalars()
            .all()
        )
        out["tasks_today"] = [
            {
                "task": r.task,
                "status": r.status,
                "kind": _job_badge(r.status),
                "duration_ms": round(r.duration_ms or 0),
                "trigger": r.trigger,
            }
            for r in jrows
        ]

        # ---- 最近后台操作 ----
        out["audit_recent"] = list(
            (await s.execute(select(AdminAudit).order_by(AdminAudit.created_at.desc()).limit(8)))
            .scalars()
            .all()
        )

    # ---- 今日比价(走 CompareRepository,不自己写 SQL)----
    async with rt.db.session() as s:
        out["compare_today"] = await CompareRepository(s).day_summary(today)

    out["vision"] = rt.compare().vision.snapshot()
    return out


@router.get("/admin", response_class=HTMLResponse)
async def dashboard(request: Request) -> Response:
    blocked = await guard(request)
    if blocked is not None:
        return blocked
    if session_of(request) is None:
        return login_redirect(request)
    rt = runtime_of(request)
    status = await rt.status()
    ctx = await _collect_dashboard(rt, request)
    return render(
        "dashboard.html",
        **page_ctx(
            request,
            "dashboard",
            s=status,
            c=ctx,
            now=datetime.now(rt.settings.tzinfo).strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )


# 其余页面(酒店/账号/机器人/群绑定/比价目标/比价历史/任务/推送/预警/登录态/审计)
# 注册在同一个 router 上,实现在 admin_pages.py。
register_pages(router)
