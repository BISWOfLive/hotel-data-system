"""T2F.1 模板层 —— 占位符填充 + 草稿文案(旧 ``review_reply.py:176-222`` 逐字继承)。

**为什么单独一层**
==================

渲染是**纯函数**、可单测、可被 ``draft`` / ``autoreply`` / 命令预览三处复用。
旧系统把它和"取数 + 落库 + 推送"混在一个文件里,新架构按职责切开:
本模块**不认识 runtime,也不碰数据库**。

占位符(``review_templates.json`` 的模板文案,附录 D A3-10)
=========================================================

===============  ==========================================================
占位符             取值(旧 ``render_reply`` 逐字)
===============  ==========================================================
``{{酒店名}}``      ``core_hotels.name``
``{{评价人}}``      点评 ``user_name``;为空 → ``您的入住``
``{{星级}}``        ``star`` 的字符串;**无星级 → ``"5"``**(旧口径,不给平台递空值)
``{{问题}}``        点评原文前 **50** 字
===============  ==========================================================

★★ **残留 ``{{...}}`` 必须清空**(计划书 §5.6 / T2F.1)
====================================================

模板是甲方随时会改的资产,少喂一个占位符就会把 ``{{问题}}`` 原样发到点评平台 ——
那是**对外可见**的事故。所以 :func:`fill_template` 的最后一步是
:func:`strip_placeholders`:**双花括号、单花括号、散落的 ``{``/``}`` 一律清掉**,
并收拾掉因删除产生的双空格。宁可少一句话,不可把模板语法发给客人。

★ 审计口径(不在本模块实现,但由本模块的调用方守住)
=================================================

``review_replies`` 是 **append-only**:状态流转**只 INSERT 不 UPDATE**,
且**先落审计行再推送**。草稿文案只是"打算说什么"的证据,落库由
:func:`hoteldata.domains.review.draft.write_audit_row` 负责。
"""

from __future__ import annotations

import re
from typing import Any

from loguru import logger

from .policy import review_field, sentiment_of

__all__ = [
    "build_draft_md",
    "fill_template",
    "render_reply",
    "review_label",
    "star_text",
    "strip_placeholders",
    "template_text",
]

#: 双花括号 / 单花括号占位符(模板语法残留一律清空)
_BRACE_RE = re.compile(r"\{\{[^{}]*\}\}|\{[^{}]*\}")
#: 散落的半个花括号
_STRAY_BRACE_RE = re.compile(r"[{}]")
#: 删除占位符后收拾空白(只收 2+ 空格与制表符,不动换行)
_BLANK_RE = re.compile(r"[ \t]{2,}")

#: ``{{问题}}`` 截断长度(旧 ``review_reply.py:189`` 的 ``content[:50]``)
ISSUE_MAX_CHARS = 50
#: 草稿标题里的原文截断长度(旧 ``_review_label`` 的 ``content[:60]``)
LABEL_MAX_CHARS = 60


def strip_placeholders(text: str) -> str:
    """清空一切花括号残留(模板语法**绝不外发**)。

    ``"感谢{{酒店名}} {{未知名}}"`` → ``"感谢"``(顺带收拾多余空格)。
    """
    out = _BRACE_RE.sub("", str(text or ""))
    out = _STRAY_BRACE_RE.sub("", out)
    out = _BLANK_RE.sub(" ", out)
    return "\n".join(line.rstrip() for line in out.splitlines())


def star_text(star: Any) -> str:
    """星级文案:``"4星"`` / ``"无星级(人工确认)"``(旧 ``build_draft_md`` 逐字)。"""
    return f"{star}星" if star is not None else "无星级(人工确认)"


def fill_template(
    text: str,
    *,
    hotel_name: str = "",
    user_name: Any = None,
    star: Any = None,
    issue: Any = "",
) -> str:
    """填充 ``{{酒店名}}`` / ``{{评价人}}`` / ``{{星级}}`` / ``{{问题}}``。

    取值口径与旧 ``render_reply``(旧 ``review_reply.py:179-190``)**逐字一致**:
    ``评价人`` 空 → ``您的入住``;``星级`` 空 → ``"5"``(不给平台递空值);
    ``问题`` 取原文前 50 字。**最后一步清空花括号残留**(见模块 docstring)。
    """
    content = str(issue or "")
    body = (
        str(text or "")
        .replace("{{酒店名}}", str(hotel_name or ""))
        .replace("{{评价人}}", str(user_name or "您的入住"))
        .replace("{{星级}}", "5" if star is None else str(star))
        .replace("{{问题}}", content[:ISSUE_MAX_CHARS])
    )
    cleaned = strip_placeholders(body)
    if cleaned != body:
        logger.debug("模板占位符有残留,已清空:{}", body[:80])
    return cleaned


def template_text(config: dict[str, Any], template_id: Any) -> str | None:
    """取模板原文(``review_templates.json`` 的 ``templates``);不存在 → ``None``。"""
    if not template_id:
        return None
    raw = (config.get("templates") or {}).get(str(template_id))
    return None if raw is None else str(raw)


def render_reply(
    policy: dict[str, Any],
    review_row: Any,
    config: dict[str, Any],
    *,
    hotel_name: str = "",
) -> tuple[str, str] | None:
    """按策略渲染一条回复 → ``(模板id, 内容)``;不需要回复 → ``None``。

    ``hotel_name`` 是**额外关键字参数**(缺省空串):旧 ``render_reply(template, review,
    hotel_name)`` 需要店名,而点评行本身不含店名(它在 ``core_hotels`` 上)。
    传 ``review_row``(带 ``hotel_name`` 字段的行)时也可省掉本参数。

    分路(计划书 §5.8「回/不回决策」表):

    * ``good``  → 走 ``policy["good"]``(默认 ``g01``);
    * ``bad``   → 店级 ``silent`` → **``None``(统一不回复,甲方口径)**;
                  ``template`` → 走 ``policy["bad_template"]``;
    * ``unknown``(无星级)→ **``None``**:只出人工待办,**永不自动回复**(宁可漏不可错)。

    模板缺失 → 返回 ``(模板id, "")``:调用方据此标"需人工拟稿",而不是静默不发。
    """
    star = review_field(review_row, "star")
    sentiment = sentiment_of(review_row)

    if sentiment == "good":
        template_id = str(policy.get("good") or "g01")
    elif sentiment == "bad":
        mode = str(policy.get("bad") or "silent").strip().lower()
        if mode == "silent":
            return None  # ★ 口径 ④:差评店级 silent → 统一不回复
        template_id = str(policy.get("bad_template") or "b01")
    else:
        return None  # ★ 口径 ③/④:unknown(无星级)永不自动回复,交人工

    template = template_text(config, template_id)
    if template is None:
        logger.warning("点评模板缺失:template_id={!r}(按需人工拟稿处理)", template_id)
        return template_id, ""
    return template_id, fill_template(
        template,
        hotel_name=hotel_name or str(review_field(review_row, "hotel_name", "") or ""),
        user_name=review_field(review_row, "user_name", ""),
        star=star,
        issue=review_field(review_row, "content", ""),
    )


def review_label(review_row: Any) -> str:
    """草稿标题(旧 ``_review_label`` 逐字):``[评测#<pk>] 原文前 60 字``。

    ★ 用 **``review_reviews.id``(主键)** 而不是平台 ``review_id`` ——
    「回复确认 / 已处理 / 已忽略」命令传的是主键(旧 ``transition_reply(pk)`` 同口径)。
    """
    pk = review_field(review_row, "pk", review_field(review_row, "id", "?"))
    content = str(review_field(review_row, "content", "") or "")
    return f"[评测#{pk}] {content[:LABEL_MAX_CHARS]}"


def build_draft_md(
    review_row: Any,
    hotel: Any,
    template_id: Any,
    body: str,
) -> str:
    """逐条草稿文本(旧 ``build_draft_md``(旧 ``review_reply.py:200-222``)**逐字照抄**)。

    ::

        🆕 [评测#12] 房间很干净,早餐也不错……
          酒店:隐欲民宿 | 评价人:M253349**** | 5星
          类型:good
          原文:房间很干净,早餐也不错……
          建议回复【g01】:尊敬的宾客您好,感谢您的好评!隐欲民宿会继续保持……
          处理:回复确认 12 / 已忽略 12

    ★ 文案顺序、缩进(两空格)、``【模板id】``、结尾的「处理」行**都不许改** ——
    管理群里的人按这一行直接复制 ``回复确认 12`` 回命令。
    """
    hotel_name = review_field(hotel, "name", "") or review_field(review_row, "hotel_name", "")
    star = review_field(review_row, "star")
    sentiment = sentiment_of(review_row)
    pk = review_field(review_row, "pk", review_field(review_row, "id", "?"))

    if template_id and body:
        rendered = body
        if "{" in rendered:
            # fill_template 已保证无残留;这里兜住"手工传 body"的路径(防把模板语法发出去)
            rendered = f"(模板残留占位符,需人工核实) {strip_placeholders(rendered)}"
    else:
        # 旧 ``build_draft_md`` 的原话(缺模板 / 非模板回复都是这一句)
        rendered = "(模板缺失或非模板回复,需人工拟稿)"

    lines = [
        f"🆕 {review_label(review_row)}",
        f"  酒店:{hotel_name or '-'} | 评价人:{review_field(review_row, 'user_name', '') or '未知'}"
        f" | {star_text(star)}",
        f"  类型:{sentiment}",
        f"  原文:{review_field(review_row, 'content', '') or ''}",
        f"  建议回复【{template_id or '手工'}】:{strip_placeholders(rendered)}",
        f"  处理:回复确认 {pk} / 已忽略 {pk}",
    ]
    return "\n".join(lines)
