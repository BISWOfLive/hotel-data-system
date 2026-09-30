"""FastAPI app + ``lifespan``(T1.6)。

``lifespan`` 内创建 :class:`~hoteldata.runtime.Runtime` 并存到 ``app.state``
—— **替代旧系统 ``app/__init__.py`` 里的模块级全局注入**(``set_bot()`` /
``set_bot_manager()``,结构性问题 #8)。

启动顺序:装配日志 → 建 Runtime(连不上 DB **直接失败**)→ 注册任务 →
**起推送派发器 → 起机器人网关** → 起调度器 → **补跑**(跨过触发时刻的缺失任务)
→ 对外服务。

★ **为什么机器人网关在调度器之前起**:推送派发器的 worker 数 = ``max(1, 在线机器人数)``
(段2 §5.4)。反过来的话会先用 1 个 worker 建好队列,机器人上线后 worker 数不会自动变 ——
30 个机器人只跑 1 个 worker,限频是对的但吞吐只有 1/30。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from loguru import logger

from hoteldata import __version__
from hoteldata.infra.db import bind_database
from hoteldata.logging import interpreter_banner, setup_logging
from hoteldata.runtime import Runtime
from hoteldata.settings import get_settings
from hoteldata.web.routes.admin import router as admin_router
from hoteldata.web.routes.health import router as health_router

__all__ = ["app", "create_app"]

#: 静态文件目录(手写 CSS,零前端构建链 —— 总纲 §7.8)
STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging(settings)
    logger.info("启动 hoteldata {} | {}", __version__, interpreter_banner())

    # ★★ **必须在 Runtime.create 之前导入**:``with_scheduler=True`` 会在
    #   ``create()`` 内部就把 APScheduler 建起来,而它是按**当时的注册表**建任务的。
    #   以前这行写在 ``async with`` 里面 → 调度器拿空注册表建成
    #   ``get_jobs() == 0``,启动日志却照样打印"已注册任务 19 个",
    #   结果**所有定时推送静默不触发**(本地联调发现)。
    #   ``Runtime.start_scheduler`` 现在还加了一道"空注册表直接报错"的兜底。
    import hoteldata.jobs  # noqa: F401

    # 连不上 DB 直接抛(T1.2:启动即校验,不静默降级)
    async with Runtime.create(settings, with_db=True, with_browser=False, with_scheduler=True) as rt:
        bind_database(rt.db)
        app.state.runtime = rt

        logger.info("已注册任务 {} 个: {}", len(rt.tasks.names()), rt.tasks.names())
        scheduled = rt.scheduler.get_jobs() if rt.scheduler is not None else []
        logger.info("调度器已装载 {} 个任务", len(scheduled))
        if len(scheduled) != len(rt.tasks.names()):  # pragma: no cover - 装配错误的哨兵
            raise RuntimeError(
                f"调度器装载 {len(scheduled)} 个任务,而注册表有 {len(rt.tasks.names())} 个 —— "
                "装配顺序错了(见 lifespan 顶部注释)"
            )

        # ★ 段2:机器人网关**先**起(推送派发器的 worker 数 = max(1, 在线机器人数)),
        #   再起推送派发器。反过来的话会先用 1 个 worker 建好队列,机器人上线后
        #   worker 数不会自动变 —— 30 个机器人只跑 1 个 worker。
        try:
            await rt.start_bots()
        except Exception as exc:  # noqa: BLE001 - 网关起不来不能拖垮采集与推送
            logger.error("机器人网关启动失败(推送仍会工作,但会写失败审计): {}", exc)
        await rt.start_push()

        # ★ 补跑:跨过触发时刻的缺失任务(按注册表反推,不扫 job_runs)
        catchup = await rt.tasks.catch_up(rt)
        for res in catchup:
            logger.info("补跑 {}: {}", res.task, res.status)
        if catchup:
            logger.info("启动补跑完成:{} 项", len(catchup))
        try:
            yield
        finally:
            logger.info("hoteldata 正在退出…")


def create_app() -> FastAPI:
    settings = get_settings()
    s = settings
    application = FastAPI(
        title="酒店经营数据自动化系统",
        version=__version__,
        description=(
            "携程 eBooking 经营数据自动化 · 三段一体(提取 / 推送 / 比价)\n\n"
            "· `/healthz` `/status` —— 存活与运行态(JSON)\n"
            "· `/admin` —— 管理后台(**统一口令登录 + 操作审计**,只监听本机)\n\n"
            "后台范围(总纲 §7.8):账号 / 机器人 / 酒店 / 群绑定 / 比价酒店增删 + 操作审计。"
            "**明确不做**:角色区分、权限树、API 鉴权细化。"
        ),
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )
    application.include_router(health_router)
    application.include_router(admin_router)
    # 静态文件(admin.css)。命名为 static/ 而不是 cdN —— 后台只监听本机,无需 CDN
    application.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    application.state.settings = s
    return application


app = create_app()


def main() -> None:  # pragma: no cover - 供 ``python -m hoteldata.main`` 用
    import uvicorn

    s = get_settings()
    uvicorn.run(
        "hoteldata.main:app",
        host=s.web.host,
        port=s.web.port,
        log_config=None,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
