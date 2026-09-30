"""Web 路由:``/healthz`` 与 ``/status``。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Request
from sqlalchemy import func, select

from hoteldata.infra.models import JobRun

__all__ = ["router"]

router = APIRouter(tags=["ops"])


def _runtime(request: Request) -> Any:
    rt = getattr(request.app.state, "runtime", None)
    if rt is None:  # pragma: no cover - 装配顺序错误
        raise RuntimeError("Runtime 未就绪")
    return rt


@router.get("/healthz", summary="存活 + DB 可达")
async def healthz(request: Request) -> dict[str, Any]:
    rt = _runtime(request)
    try:
        info = await rt.db.server_info()
        db_ok = True
        db_error = None
    except Exception as exc:  # noqa: BLE001
        info = {}
        db_ok = False
        db_error = str(exc)
    payload = {
        "status": "ok" if db_ok else "degraded",
        "time": datetime.now(rt.settings.tzinfo).isoformat(timespec="seconds"),
        "db": {"ok": db_ok, **(info or {}), **({"error": db_error} if db_error else {})},
    }
    if not db_ok:
        # 让编排系统看到非 200(但保持可读体)
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=503, content=payload)  # type: ignore[return-value]
    return payload


@router.get("/status", summary="调度器状态 + 今日任务汇总")
async def status(request: Request) -> dict[str, Any]:
    rt = _runtime(request)
    payload = await rt.status()

    # 今日 job_runs 汇总
    tz = rt.settings.tzinfo
    today_start = datetime.combine(date.today(), datetime.min.time(), tzinfo=tz)
    try:
        async with rt.db.session() as s:
            rows = (
                await s.execute(
                    select(JobRun.status, func.count())
                    .where(JobRun.started_at >= today_start)
                    .group_by(JobRun.status)
                )
            ).all()
        payload["job_runs_today"] = {str(st): int(n) for st, n in rows}
    except Exception as exc:  # noqa: BLE001
        payload["job_runs_today"] = {"error": str(exc)}

    payload["time"] = datetime.now(tz).isoformat(timespec="seconds")
    return payload


@router.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    """根路径:给人看的入口清单(不是 API)。"""
    return {
        "service": "hoteldata · 酒店经营数据自动化(段1 提取 / 段2 推送 / 段3 比价)",
        "admin": "/admin",
        "docs": "/docs",
        "healthz": "/healthz",
        "status": "/status",
    }
