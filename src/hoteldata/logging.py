"""loguru 装配 —— 含 ``Asia/Shanghai`` 时区与启动自检横幅。

段1 T1.6 要点:
  - 时间戳为**北京时间**(``Asia/Shanghai``);
  - 文件轮转 **30 天**(总纲 §3.1 清理策略:``logs/`` loguru retention 30 天);
  - ★ **启动时打印解释器版本与 ``Py_GIL_DISABLED``** —— 防误用 free-threaded
    (no-GIL)构建(R14/S8:依赖只有 ``cp314`` 轮子,没有 ``cp314t``)。
"""

from __future__ import annotations

import logging
import sys
import sysconfig
from pathlib import Path

from loguru import logger

from hoteldata.settings import Settings, get_settings

__all__ = ["check_interpreter", "configure_stdio", "interpreter_banner", "setup_logging"]

#: loguru 默认格式,时区由 ``logger.configure(patcher=...)`` 打到记录上
_CONSOLE_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
)
_FILE_FORMAT = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}"

_configured = False


class _InterceptHandler(logging.Handler):
    """把标准库 logging(uvicorn / sqlalchemy / alembic)重定向到 loguru。"""

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - 集成路径
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def configure_stdio() -> None:
    """★ 把 stdout / stderr 切到 UTF-8。

    Windows 控制台默认代码页是 **GBK**(cp936),`✓` / `✗` / `·` 这些字符
    会直接抛 `UnicodeEncodeError: 'gbk' codec can't encode character`,
    让 CLI/脚本**在任何命令上都崩** —— 而且往往是"活已经干完了、打印结果时炸",
    看起来像业务失败,实际只是终端编码。

    这是纯终端问题,统一在这里解决,而不是把界面文字退化成 ASCII。
    CLI 入口与 `scripts/*` 都要调它。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            encoding = (getattr(stream, "encoding", "") or "").lower()
            if encoding and encoding not in ("utf-8", "utf8"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def check_interpreter() -> dict[str, object]:
    """★ 解释器自检:R14 的落地检查。"""
    gil_disabled = sysconfig.get_config_var("Py_GIL_DISABLED")
    version = ".".join(str(x) for x in sys.version_info[:3])
    return {
        "version": version,
        "gil_disabled": gil_disabled,
        "free_threaded": bool(gil_disabled),
        "executable": sys.executable,
        "ok": (3, 14) <= sys.version_info[:2] < (3, 15) and not gil_disabled,
    }


def interpreter_banner() -> str:
    """启动横幅(写进日志与 ``/status``)。"""
    info = check_interpreter()
    flag = "OK" if info["ok"] else "!! 不合规"
    gil = info["gil_disabled"]
    gil_text = "0/None(标准版)" if not gil else f"{gil}(★ free-threaded,禁止)"
    return f"解释器 Python {info['version']} | Py_GIL_DISABLED={gil_text} | exe={info['executable']} | {flag}"


def setup_logging(settings: Settings | None = None, *, force: bool = False) -> None:
    """装配 loguru。幂等(重复调用直接返回,除非 ``force=True``)。"""
    global _configured
    if _configured and not force:
        return
    s = settings or get_settings()
    log_dir: Path = s.paths.var_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    logger.remove()
    logger.configure(extra={"tz": s.tz})

    def _patch(record: dict) -> None:  # type: ignore[type-arg]
        record["time"] = record["time"].astimezone(s.tzinfo)

    logger.add(
        sys.stderr,
        format=_CONSOLE_FORMAT,
        level="DEBUG" if s.debug else "INFO",
        colorize=True,
        backtrace=s.debug,
        diagnose=s.debug,
        enqueue=False,
    )
    logger.add(
        log_dir / "hoteldata_{time:YYYYMMDD}.log",
        format=_FILE_FORMAT,
        level="DEBUG" if s.debug else "INFO",
        rotation="00:00",  # 每天零点轮转
        retention="30 days",  # ★ 与总纲 §3.1 的 logs/ 保留期一致
        encoding="utf-8",
        enqueue=True,  # 多协程写文件安全
        backtrace=s.debug,
        diagnose=False,  # 文件里不落变量值(可能含凭据)
    )
    logger.add(
        log_dir / "error_{time:YYYYMMDD}.log",
        format=_FILE_FORMAT,
        level="ERROR",
        rotation="00:00",
        retention="30 days",
        encoding="utf-8",
        enqueue=True,
        backtrace=True,
        diagnose=False,
    )
    # loguru 的 patcher 需要在 add 时传;这里用全局 configure 后再统一处理时间戳
    for handler_id in logger._core.handlers:  # noqa: SLF001 - loguru 无公开枚举 API
        try:
            logger._core.handlers[handler_id]._patcher = _patch  # noqa: SLF001
        except Exception:  # noqa: BLE001 - 版本差异兜底
            pass

    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)
    for noisy in ("uvicorn.access", "sqlalchemy.engine.Engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True
    logger.info(interpreter_banner())
