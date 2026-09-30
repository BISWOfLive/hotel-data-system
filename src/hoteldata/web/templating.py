"""Jinja2 模板装配(服务端渲染 —— 总纲 §7.8:不引入前端构建链)。

模板目录固定为 ``src/hoteldata/web/templates``,**随包发布**
(``poetry`` 的 ``packages`` 含 ``src/hoteldata``),所以不依赖工作目录。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates

__all__ = ["TEMPLATES_DIR", "get_templates", "render"]

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

_templates: Jinja2Templates | None = None


def get_templates() -> Jinja2Templates:
    """惰性建一次(避免 import 顺序问题)。"""
    global _templates
    if _templates is None:
        _templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
        # 模板里要用的过滤器/全局量
        _templates.env.filters["short"] = _short
        _templates.env.filters["dt"] = _dt
        _templates.env.globals["app_version"] = _version()
    return _templates


def _version() -> str:
    try:
        from hoteldata import __version__

        return str(__version__)
    except Exception:  # noqa: BLE001  # pragma: no cover
        return "?"


def _short(value: Any, length: int = 40) -> str:
    """截断(表格里放长 chatid / 密文用)。"""
    text = "" if value is None else str(value)
    return text if len(text) <= length else text[: length - 1] + "…"


def _dt(value: Any, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """时间格式化;``None`` → ``—``。"""
    if value is None:
        return "—"
    try:
        return value.strftime(fmt)
    except AttributeError:  # pragma: no cover
        return str(value)


def render(name: str, **context: Any) -> Any:
    """渲染模板(``request`` 必须由调用方传入)。

    ★ 为什么自己包一层而不是直接用 ``TemplateResponse``:
      集中一处设置 ``request`` 与全局量,并保证**所有**页面走同一份模板环境
      (将来加 `csrf` / 主题只改这一处)。
    """
    request: Request = context["request"]
    return get_templates().TemplateResponse(request, name, context)
