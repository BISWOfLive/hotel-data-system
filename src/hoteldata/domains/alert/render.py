"""预警文案渲染(T2E.5 的渲染部分)—— ``config/prompts/<template>.md`` 的填充。

契约(计划书 §5.7 末段,逐字继承旧 ``alert_push.render_trigger``:81-100)
========================================================================

* 模板 = ``config/prompts/<push.template>.md``(启动时已在 :mod:`.rules` 校验存在);
* 占位符三类:``{hotel_name}`` / ``{detail_lines}`` / **payload 里的数据键**
  (``{lead_days}`` / ``{city}`` / ``{price}`` …);
* **残留 ``{x}`` 必须清空** —— 正则 ``\\{[a-zA-Z_][a-zA-Z0-9_]*\\}`` → ``""``。
  ★ 这是甲方能一眼看出来的事故:"推出去一堆花括号"比不推更糟;
* ``context`` 里缺的键 → 空串(**不抛**、不整条不推);
* ``detail_lines`` 用 ``"\\n".join``;
* 模板文件缺失 → **内置兜底文案 + warning**(绝不静默发空消息);
* 文案不以 ``【`` 开头时**补标题行**(旧 ``:97-99``)。

为什么渲染单独一个模块
======================

旧系统渲染埋在 ``alert_push`` 里,和推送目标、日志、汇总混在 391 行里。
段2 §1.3 要求"内容由域组装、投递由 push 层负责",渲染是**纯函数**:
``(Trigger, context) -> str``,不碰 DB、不碰网络 —— 这样 V52 的"残留占位符"能单测。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from loguru import logger

from hoteldata.domains.alert.rules import prompts_dir
from hoteldata.settings import Settings

__all__ = [
    "FALLBACK_TEMPLATE",
    "PENDING_LABELS",
    "RULE_TITLES",
    "clear_placeholders",
    "load_template",
    "render_trigger",
    "template_path",
]

#: 规则中文标题(旧 ``alert_engine._title``:448-456 逐字;``room_closed_today`` 已退役不保留)
RULE_TITLES: dict[str, str] = {
    "room_closed_7d": "关房预警",
    "hot_event_price": "热点日历·重要日期价格预警",
    "channel_below_mean": "渠道数据低于竞争圈均值",
    "home_pending": "首页待办预警",
    "city_heat_remind": "城市热点提前提醒",
    "price_line_optional": "房价固定线提醒",
}

#: E 规则明细行的中文名(旧 ``alert_engine.evaluate_rule``:525-527 逐字)
PENDING_LABELS: dict[str, str] = {
    "comment_pending": "待回复评论",
    "qa_pending": "待回复回答",
    "audit_pending": "待审核",
    "violation_pending": "违约看板",
    "todo_more": "待办事项明细",
}

#: 模板缺失时的兜底(旧 ``alert_push.load_alert_template`` 的兜底串,逐字)
FALLBACK_TEMPLATE = "【预警】{hotel_name}\n\n{detail_lines}\n"

#: ★ 残留占位符清理(计划书 §5.7 指定的正则)
_PLACEHOLDER_RE = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}")

#: 兜底清理:任何花括号内容(含中文键、带冒号的键)。两道网,保证推出去没有花括号。
_ANY_BRACE_RE = re.compile(r"\{[^{}]*\}")


def clear_placeholders(text: str) -> str:
    """清空残留占位符(★ 两道网:标识符正则 + 任意花括号兜底)。"""
    text = _PLACEHOLDER_RE.sub("", text)
    return _ANY_BRACE_RE.sub("", text)


def template_path(
    name: str, *, settings: Settings | None = None, config_dir: Path | str | None = None
) -> Path:
    """模板文件路径(``name`` 可带或不带 ``.md``)。"""
    base = prompts_dir(settings, config_dir=config_dir)
    return base / (name if name.endswith(".md") else f"{name}.md")


def load_template(
    name: str, *, settings: Settings | None = None, config_dir: Path | str | None = None
) -> str | None:
    """读模板;文件缺失/不可读 → ``None``(**由调用方决定兜底并告警**)。"""
    path = template_path(name, settings=settings, config_dir=config_dir)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("预警模板不可读:{} ({})", path, exc)
        return None


def render_trigger(
    trigger: Any,
    *,
    hotel_name: str | None = None,
    extra: dict[str, Any] | None = None,
    settings: Settings | None = None,
    config_dir: Path | str | None = None,
) -> str:
    """``Trigger`` → 推送文本。

    ``hotel_name`` 缺省取 ``trigger.hotel_name``;``extra`` 是**额外占位符**
    (命令预览时塞 ``{title}`` 之类)。渲染顺序:先填``{key}``,再清残留,最后补标题。
    """
    name = str(getattr(trigger, "template", "") or "alert_generic")
    template = load_template(name, settings=settings, config_dir=config_dir)
    if template is None:
        logger.warning("预警模板缺失({}),改用内置兜底文案", name)
        template = FALLBACK_TEMPLATE

    hotel = hotel_name or str(getattr(trigger, "hotel_name", "") or getattr(trigger, "hotel_id", ""))
    lines = getattr(trigger, "detail_lines", None) or []
    if isinstance(lines, str):
        detail = lines
    else:
        detail = "\n".join(str(x) for x in lines)

    context: dict[str, Any] = dict(getattr(trigger, "payload", None) or {})
    context.update(extra or {})
    context["hotel_name"] = hotel
    context["detail_lines"] = detail

    text = template
    for key, value in context.items():
        text = text.replace("{" + str(key) + "}", "" if value is None else str(value))
    text = clear_placeholders(text).strip()

    title = str(getattr(trigger, "title", "") or "")
    if title and not text.startswith("【"):
        text = f"{title}\n\n{text}".strip()
    return text
