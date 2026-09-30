"""18 条群内命令(T2B.1 / T2B.2 / T2B.3)。

为什么是 18 条、为什么文案一个字都不许改
========================================

命令表与帮助文案是**甲方口径**(计划书附录 A + 旧 ``app/commands.py:28-72``):
群成员已经把「绑定」「今日数据」这些字眼背下来了,帮助文案里的每一行
(含 emoji、全角标点、「示例:绑定 隐欲民宿、山海别院」)都是**承诺**。
改一个标点就要重新培训 300 个群 —— 所以 ``_COMMANDS`` / ``_CMD_PREFIXES`` /
``_HELP_TEXT`` 三块**逐字继承旧系统**。

本模块只做两件事
================

① **识别**:``parse_command`` —— ``startswith`` **最长前缀匹配**(V26)。
   为什么必须"最长"而不是"最短"或"精确":旧系统既有「我的酒店」也有「绑定」,
   最短前缀会把「我的酒店」截成不存在的前缀而漏命中;精确匹配则让
   「绑定隐欲民宿」(无空格)识别不出来。两者都会让群成员以为机器人死了。

② **生成回复文本**:``handle_command`` —— ★ **只返回文本,不发送**。
   发送统一由 :mod:`hoteldata.domains.bot.router` 经 ``Sender`` 完成:
   谁发送、怎么拆长文、失败怎么重试是**共享层**的事,命令层不掺和。

三条硬口径
==========

* **私聊无 ``chatid`` → 返回 ``None``**(V28):命令只在群里生效,
  私聊应当回退到问答链,而不是回一句"⛔ 该命令仅管理群可用"让人莫名其妙;
* **管理群白名单未配置时一律拒绝**(V27):判定只走
  ``runtime.settings.push.is_manage(chatid)``。旧系统 ``_manage_chatids()``
  未配置时返回空列表也算"拒绝",但新架构把这个语义**收进 settings**,
  命令层不许自己解析 ``MANAGE_CHATIDS``(那是"配置解析散落各处"的老病)。

★ D18 修复位置(计划书 §4.4)
============================

旧 ``commands.py:298``::

    online_bots = health.get("online", active_bots)

而 ``get_health()`` 返回的是 ``{机器人名: bool}`` —— 键不存在,于是**永远**回退到
"配置里的活跃机器人数",多机器人配置下「状态」显示的在线数与事实不符。

本模块按 ``BotManager.health()`` 的**统一契约**读::

    online = sum(1 for v in health.values() if v)

并把 health 字典原样渲染出来(哪台在线、哪台掉线,一眼可见)。

预警 / 点评命令:惰性 import
============================

``domains/alert`` / ``domains/review`` 由**其它批次并行开发**,此刻可能还不存在。
所以:

* 一律在**函数体内** ``import``(模块级 import 会让整个命令层跟着挂掉);
* ``ImportError`` / ``AttributeError`` / 方法缺失 → 返回
  ``"预警模块未就绪: ..."`` 这类**明确文案**,不抛异常、不静默;
* 域服务的方法名是本层与域层的**约定**:调用点逐条写清了期望的签名,
  域层按此实现即可(见各 ``_*_reply`` 函数 docstring)。
"""

from __future__ import annotations

import inspect
import re
from datetime import date, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import select

from hoteldata.domains.bot.protocol import chatid_of
from hoteldata.infra.models import Hotel

__all__ = [
    "MANAGE_COMMANDS",
    "MANAGE_COMMAND_ROUTES",
    "NOT_MANAGE_TEXT",
    "handle_command",
    "is_command",
    "parse_command",
]

# ---------------------------------------------------------------------------
# 命令表 / 帮助文案(★ 逐字继承旧 ``app/commands.py:28-72``,禁止改写)
# ---------------------------------------------------------------------------

#: 支持的群内命令(附录 A:18 条,一个不多一个不少)
_COMMANDS = (
    "绑定",
    "解绑",
    "我的酒店",
    "今日数据",
    "重推",
    "帮助",
    "汇总",
    "状态",
    "预警测试",
    "预警状态",
    "忽略此店",
    "预警线",
    "点评待办",
    "点评状态",
    "点评策略",
    "回复确认",
    "已处理",
    "已忽略",
)

#: 最长匹配优先:保证「我的酒店」不会被更短前缀截断(V26)
_CMD_PREFIXES = sorted(_COMMANDS, key=len, reverse=True)

#: ★ 仅管理群可用的命令(计划书 §2.2 V27「11 条管理群命令」的口径)。
#:
#: **集合里有 12 个名字、11 条路**:命令表里 ``回复确认`` 与 ``已处理`` 是两条命令,
#: 但走的是**同一条** ``transition_reply(status="ok")`` 分支(旧 ``commands.py:625``
#: 的 ``if cmd in ("回复确认", "已处理")``),所以按"路径"数是 11 条。
#:
#: 为什么两个名字都要列进来:白名单判定发生在**分派之前**,
#: 少列一个名字就等于那条命令在非管理群**不设防**(漏网会直接打到点评域)。
MANAGE_COMMANDS: frozenset[str] = frozenset(
    {
        "汇总",
        "状态",
        "预警测试",
        "预警状态",
        "忽略此店",
        "预警线",
        "点评待办",
        "点评状态",
        "点评策略",
        "回复确认",
        "已处理",
        "已忽略",
    }
)

#: 管理群命令的**路径数**(=V27 的 11 条:``已处理`` 与 ``回复确认`` 同路)
MANAGE_COMMAND_ROUTES = 11

#: ★ 帮助文案(**逐字继承**旧 ``app/commands.py:52-72``,含 emoji / 全角标点 / 示例行)
_HELP_TEXT = (
    "📖 群内命令帮助\n"
    "「绑定 <酒店名[,酒店名...]>」 当前群绑定酒店（支持一群多店）\n"
    "「解绑 [酒店名]」 解绑指定酒店（缺省=解绑全部）\n"
    "「我的酒店」 查询当前群绑定的酒店列表\n"
    "「今日数据」 重推本群今日日报（精简数据+轮换图）\n"
    "「重推」 补推今日日报（绕过当天去重）\n"
    "「帮助」 本命令列表\n"
    "「汇总」 全量酒店批量汇总（仅管理群）\n"
    "「状态」 系统运行状态/机器人在线/推送统计（仅管理群）\n"
    "「预警测试 [酒店名]」 即时预览该店预警（仅管理群，不真发）\n"
    "「预警状态 [酒店名]」 查询预警状态/今日发稿统计（仅管理群）\n"
    "「忽略此店 <酒店名> [天数]」 暂停该店全部预警提醒（仅管理群）\n"
    "「预警线 <酒店名> 高 <数值> 低 <数值>」 设置房价高/低预警线（仅管理群；缺省=取消）\n"
    "「点评待办 [酒店名]」 列出点评草稿并刷新（仅管理群）\n"
    "「点评状态 [酒店名]」 查询点评待回复/审计统计/自动模式状态（仅管理群）\n"
    "「点评策略 <酒店名> [好评 <模板id>] [差评 silent|template [模板id]]」 设置店级点评策略（仅管理群）\n"
    "「回复确认 <评测#id>」/「已处理 <评测#id>」 标记该点评已在平台回复（仅管理群）\n"
    "「已忽略 <评测#id>」 确认不回复该点评（仅管理群）\n"
    "示例：绑定 隐欲民宿、山海别院"
)

#: 管理群拒绝文案(V27:未配置白名单时**也**回这一句,不是放行)
NOT_MANAGE_TEXT = "⛔ 该命令仅管理群可用"


# ---------------------------------------------------------------------------
# 解析(T2B.1)
# ---------------------------------------------------------------------------


def _strip_at_mention(text: str) -> str:
    """兜底去掉消息头部残留的 ``@机器人`` 提及(旧 ``commands._strip_at_mention`` 逐字)。

    设计约定 ``content`` 已"去 @"(见 :func:`hoteldata.domains.bot.faq.clean_question`),
    但消息入口实传未清洗的原始文本,故在命令识别前去掉一个开头的 @ 提及,
    避免影响前缀匹配。**只处理开头一处** —— 句中的 ``@`` 可能是正文。
    """
    return re.sub(r"^@[^\s@，。]{1,40}\s*", "", text)


def _split_args(remainder: str) -> list[str]:
    """把命令剩余部分按逗号拆分并去空白。

    旧 ``commands._split_args`` 的正则是 ``r"[,，]"``(**只有**英文 ``,`` 与全角 ``，``)。
    本实现**加上顿号 ``、``**:帮助文案自己的示例就是
    「示例:绑定 隐欲民宿、山海别院」—— 客户照抄示例时,旧实现会把两家店当成
    一个名字,回一句「⚠️ 未找到酒店:隐欲民宿、山海别院」。

    为什么必须在这里修:绑定落到 ``bindings.bind(chatid, hotel_id)``(**单店**)。
    "一整串名字"在新架构里**没有**任何一个 API 能接受,不修就是功能缺失。
    """
    return [a.strip() for a in re.split(r"[,，、]", remainder) if a.strip()]


def _ws_tokens(args: list[str]) -> list[str]:
    """把参数切分为空白 token(旧 ``_ws_tokens`` 逐字:预警线/忽略此店 用;兼容逗号与空格混排)。"""
    parts: list[str] = []
    for a in args:
        parts.extend(re.split(r"[\s,，]+", a))
    return [p for p in parts if p]


def parse_command(content: Any) -> tuple[str, list[str]] | None:
    """识别一条(已清理/去 @、仅 strip)消息是否为群内命令。

    返回 ``(cmd, args)``;不是命令返回 ``None``。
    ★ 命令匹配为 ``startswith`` + **最长前缀优先**(``_CMD_PREFIXES`` 已按长度倒序),
    所以「我的酒店」不会被更短的前缀吃掉(V26)。
    """
    if not isinstance(content, str) or not content:
        return None
    text = _strip_at_mention(content.strip())
    if not text:
        return None
    for cmd in _CMD_PREFIXES:
        if text.startswith(cmd):
            remainder = text[len(cmd):].strip()
            return (cmd, _split_args(remainder))
    return None


def is_command(content: Any) -> bool:
    """供消息入口快速分流:是否为群内命令(旧 ``is_command``)。"""
    return parse_command(content) is not None


# ---------------------------------------------------------------------------
# 基础设施接入(一律走 runtime,不用模块级全局)
# ---------------------------------------------------------------------------


def _push_settings(runtime: Any) -> Any:
    """取 ``Settings.push``(管理群白名单的唯一判定来源)。"""
    return runtime.settings.push


def _is_manage(runtime: Any, chatid: str) -> bool:
    """管理群判定:``settings.push.is_manage(chatid)``。

    ★ **未配置 MANAGE_CHATIDS 时一律 False(拒绝)** —— 不是放行(V27)。
    """
    try:
        return bool(_push_settings(runtime).is_manage(chatid))
    except Exception as exc:  # noqa: BLE001 - 配置异常按"拒绝"处理,绝不因配置问题放行
        logger.error("管理群判定失败,按拒绝处理: chatid={} err={}", chatid, exc)
        return False


async def _hotel_name_map(runtime: Any) -> dict[str, int]:
    """``{酒店名: hotel_id}``(★ 只取 ``status='active'``)。

    🚫 不直接 join 任何提取表,只读 ``core_hotels``(硬约束 3)。
    用 ``runtime.db.session()`` **短会话**,与全项目一致。

    过滤口径与 ``cli.py:388`` / :mod:`hoteldata.push.bindings` 一致:
    **暂停的店不该再被绑定** —— 旧 ``_available_hotel_hint`` 用
    ``status="active"``,新架构沿用同一口径(旧代码里的 ``"paused"`` 在此是历史写法)。
    """
    async with runtime.db.session() as session:
        rows = list(
            (
                await session.execute(select(Hotel).where(Hotel.status == "active").order_by(Hotel.name))
            )
            .scalars()
            .all()
        )
    return {str(h.name): int(h.id) for h in rows if h.name}


async def _available_hotel_hint(runtime: Any) -> str:
    """「可用酒店:A、B 等」(旧 ``_available_hotel_hint``:最多列 8 家)。"""
    names = sorted(await _hotel_name_map(runtime))
    if not names:
        return ""
    shown = names[:8]
    return "可用酒店：" + "、".join(shown) + (" 等" if len(names) > 8 else "")


def _format_bound(runtime_names: list[str]) -> str:
    """``A、B`` 形式的名字串。"""
    return "、".join(runtime_names)


# ---------------------------------------------------------------------------
# 通用命令(T2B.2)
# ---------------------------------------------------------------------------


async def _bind_reply(runtime: Any, chatid: str, args: list[str]) -> str:
    """``绑定 <酒店名[,酒店名...]>``。

    * 按**中文/英文逗号**拆分(``parse_command`` 已拆好);
    * 找不到的店**列出来** + 「可用酒店:...」提示;
    * ★ **幂等**:``bindings.bind`` 返回 ``False`` 表示"本来就有",
      与 ``True`` 一样算成功(``core_group_bindings`` 有 ``UNIQUE(chatid, hotel_id)``)。
    """
    if not args:
        return "请使用：绑定 <酒店名[,酒店名...]>"
    name_map = await _hotel_name_map(runtime)
    bound: list[str] = []
    missing: list[str] = []
    repeat: list[str] = []
    for name in args:
        hotel_id = name_map.get(name)
        if hotel_id is None:
            missing.append(name)
            continue
        created = await runtime.bindings.bind(chatid, hotel_id)
        (bound if created else repeat).append(name)
    if missing:
        lines: list[str] = []
        if bound:
            lines.append(f"✅ 已绑定：{_format_bound(bound)}（共{len(bound)}家）")
        lines.append(f"⚠️ 未找到酒店：{_format_bound(missing)}")
        hint = await _available_hotel_hint(runtime)
        if hint:
            lines.append(hint)
        return "\n".join(lines)
    if not bound:
        # 全部是重复绑定 —— 幂等成功,但要告诉用户"本来就有"
        return f"✅ 本群已绑定：{_format_bound(repeat)}（共{len(repeat)}家，重复绑定无副作用）"
    return f"✅ 本群已绑定：{_format_bound(bound)}（共{len(bound)}家）"


async def _unbind_reply(runtime: Any, chatid: str, args: list[str]) -> str:
    """``解绑 [酒店名...]``(**无参数 = 解绑全部**)。"""
    if not args:
        count = await runtime.bindings.unbind(chatid)
        if count:
            return f"已解绑本群全部 {count} 家酒店"
        return "本群没有绑定任何酒店"
    name_map = await _hotel_name_map(runtime)
    removed: list[str] = []
    not_bound: list[str] = []
    for name in args:
        hotel_id = name_map.get(name)
        if hotel_id is not None and await runtime.bindings.unbind(chatid, hotel_id):
            removed.append(name)
        else:
            not_bound.append(name)
    lines: list[str] = []
    if removed:
        lines.append(f"已解绑：{_format_bound(removed)}")
    if not_bound:
        lines.append(f"未找到/未绑定：{_format_bound(not_bound)}")
    if not lines:
        return "本群没有绑定任何酒店"
    return "\n".join(lines)


async def _my_hotels_reply(runtime: Any, chatid: str) -> str:
    """``我的酒店``:列当前群绑定(旧 ``_my_hotels_reply`` 文案原样)。"""
    bound = await runtime.bindings.for_group(chatid)
    if not bound:
        return "本群还没有绑定任何酒店，发送「绑定 酒店名」即可。"
    names = [h.name for h in bound]
    return f"本群绑定：{_format_bound(names)}（共{len(names)}家）"


def _interpret_push_result(result: Any) -> str:
    """解释 ``push_daily`` 的返回值 → 用户可读文本(形状沿用旧 ``_interpret_push_result``)。

    新架构返回 :class:`hoteldata.push.sender.Delivery`(同步投递结果),
    但为了让本函数**形状兼容**旧口径,同时接受 ``(ok, msg)`` 元组 / ``bool`` /
    ``dict`` / ``None``: 域层并行开发期间,返回形态可能短暂不一致,
    "解释器"宽容一点,总比让群成员看到 ``<Delivery object at 0x...>`` 强。
    """
    if result is None:
        return "推送失败：无数据"
    ok = getattr(result, "ok", None)
    if ok is not None:
        if not ok:
            err = getattr(result, "error", None) or "未知错误"
            return f"推送失败：{err}"
        bot_id = getattr(result, "bot_id", "") or ""
        parts = getattr(result, "parts", 0) or 0
        media = getattr(result, "media_count", 0) or 0
        extra = f"机器人 {bot_id} / {parts} 条文本 / {media} 张图" if bot_id else f"{parts} 条文本 / {media} 张图"
        return f"✅ 已推送成功（{extra}）"
    if isinstance(result, tuple) and len(result) == 2:
        flag, msg = result[0], result[1]
        if flag:
            return f"✅ 已推送成功（{msg}）" if msg else "✅ 已推送成功"
        return f"推送失败：{msg}" if msg else "推送失败"
    if result is True:
        return "✅ 已推送成功"
    if result is False:
        return "推送失败"
    if isinstance(result, dict):
        if result.get("ok", result.get("success")):
            extra = result.get("count", result.get("total"))
            return f"✅ 已推送成功（{extra}）" if extra is not None else "✅ 已推送成功"
        reason = result.get("reason") or result.get("error")
        return f"推送失败：{reason}" if reason else "推送失败"
    return "✅ 已推送成功" if result else "推送失败：无数据"


async def _push_reply(runtime: Any, chatid: str) -> str:
    """``今日数据`` / ``重推``:★ **同步等结果**地调 ``push.push_daily(chatid, force=True)``。

    为什么 ``force=True`` 两条都用:这两条命令的语义就是"我现在就要看",
    当天已推过的去重必须让位(旧 ``push_group_now(chatid, force=True)`` 同一口径)。
    ``push_daily`` 内部 ``now=True`` → ``deliver_now``,所以是**等结果**而不是入队,
    群成员发完命令能立刻看到"成功/失败",不会石沉大海。
    """
    try:
        result = await runtime.push.push_daily(chatid, force=True)
    except Exception as exc:  # noqa: BLE001 - 推送异常要转成可读文案,不抛给消息链
        logger.warning("推送命令执行失败: {}", exc)
        return f"推送失败：{exc}"
    return _interpret_push_result(result)


# ---------------------------------------------------------------------------
# 管理群命令(T2B.3)
# ---------------------------------------------------------------------------


async def _summary_reply(runtime: Any) -> str:
    """``汇总``:当日 ``audit.group_stats()`` + ``type_stats()`` 汇总成文本。

    旧系统读 ``batch_summary_*.md``(一份离线文件)—— 那是"批量汇总"时代的遗留,
    新架构的事实来源是 ``push_logs``(可查、可按群/类型下钻)。
    """
    try:
        group_stats = await runtime.audit.group_stats()
        type_stats = await runtime.audit.type_stats()
    except Exception as exc:  # noqa: BLE001
        return f"汇总查询失败：{exc}"
    if not group_stats and not type_stats:
        return "📊 今日汇总：暂无推送记录"
    lines = [f"📊 今日汇总（成功推送 {sum(group_stats.values())} 条 / {len(group_stats)} 个群）"]
    if type_stats:
        lines.append("- 按类型：" + "、".join(f"{k}={v}" for k, v in sorted(type_stats.items())))
    if group_stats:
        lines.append("- 按群：")
        for chatid, cnt in list(group_stats.items())[:10]:
            lines.append(f"  · {_short(chatid)} → {cnt} 条")
        if len(group_stats) > 10:
            lines.append(f"  · …共 {len(group_stats)} 个群")
    return "\n".join(lines)


def _short(chatid: str, keep: int = 10) -> str:
    """群 id 太长会撑爆消息,截前 10 位用于展示。"""
    text = str(chatid or "")
    return text if len(text) <= keep else f"{text[:keep]}…"


async def _status_reply(runtime: Any) -> str:
    """``状态``:机器人在线(★ **按 ``BotManager.health()`` 统一契约读**)+ 今日推送统计。

    ★★ **D18 修复点**(旧 ``commands.py:298``)::

        # 旧(错):health 是 {名字: bool},health["online"] 恒不存在
        online_bots = health.get("online", active_bots)
        # 新(对):
        online = sum(1 for v in health.values() if v)

    并把 health 字典**原样渲染**(``机器人 a=在线 / b=离线``),
    这样多机器人配置下"哪台掉线"在群里直接可见,而不是只给一个总数。
    """
    manager = getattr(runtime, "bots", None)
    health: dict[str, bool] = {}
    if manager is not None:
        try:
            health = dict(manager.health())
        except Exception as exc:  # noqa: BLE001 - 健康查询失败不该让「状态」命令整体失败
            logger.warning("读取机器人健康失败: {}", exc)
            health = {}
    online = sum(1 for v in health.values() if v)
    total = len(health)
    lines = ["📊 系统状态", f"- 机器人在线：{online}/{total}"]
    if health:
        detail = "、".join(f"{name}={'在线' if ok else '离线'}" for name, ok in sorted(health.items()))
        lines.append(f"- 机器人健康：{detail}")
    else:
        lines.append("- 机器人健康：无实例（core_bots 为空或 AIBOT_ENABLED=0）")
    try:
        stats = await runtime.audit.day_stats()
        lines.append(
            f"- 今日推送：成功 {stats.get('ok', 0)} / 失败 {stats.get('failed', 0)}"
            f" / 去重跳过 {stats.get('skipped', 0)}（送达率 {float(stats.get('rate', 0.0)):.1f}%）"
        )
        type_stats = await runtime.audit.type_stats()
        if type_stats:
            lines.append("- 按类型：" + "、".join(f"{k}={v}" for k, v in sorted(type_stats.items())))
    except Exception as exc:  # noqa: BLE001
        lines.append(f"- 今日推送：查询失败（{exc}）")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 预警命令(惰性 import)
# ---------------------------------------------------------------------------


def _readiness(exc: BaseException) -> str:
    """把"域未就绪"的异常压成一行人话(D18 同源纪律:错误要可见,不许静默)。"""
    return f"{type(exc).__name__}: {exc}"


def _finalize(result: Any) -> str:
    """域服务返回值 → 群内可读文本(容错多种返回形态)。

    域层可能回 ``str`` / ``dict``(``{"text"|"reply"|"md"|"lines"|"message"}``) /
    ``None``。这里统一收口,**绝不把 ``None`` 拼进消息**、也不 ``str()`` 一个对象。
    """
    if result is None:
        return "（无内容）"
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        for key in ("text", "reply", "md", "markdown", "message", "summary"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value
        lines = result.get("lines")
        if isinstance(lines, (list, tuple)) and lines:
            return "\n".join(str(x) for x in lines)
        return str(result)
    if isinstance(result, (list, tuple)):
        return "\n".join(str(x) for x in result)
    return str(result)


async def _call_method(service: Any, names: tuple[str, ...], *args: Any, **kwargs: Any) -> Any:
    """按候选名依次调用域服务方法(第一个**存在**的即用),返回其结果。

    为什么要"候选名":``domains/alert`` / ``domains/review`` 由并行批次实现,
    方法名存在同义变体(``status`` / ``stats``,``set_lines`` / ``set_alert_lines``)。
    本层把候选名**写死、可读、可审**,比"影子协议 + 运行时猜"更可控;
    一个都没命中 → 抛 ``AttributeError``,由调用方转成"模块未就绪"文案。

    ``args`` / ``kwargs`` 是**给第一个候选**用的;调用点还可以追加
    ``_variants=((args2, kwargs2), ...)`` 提供向后兼容的备用签名
    (同名方法、不同参数形态):第一个变体成功即返回,全部失败才抛最后一个异常。
    """
    variants: tuple[tuple[tuple[Any, ...], dict[str, Any]], ...] = kwargs.pop("_variants", ())
    last_exc: Exception | None = None
    for name in names:
        fn = getattr(service, name, None)
        if not callable(fn):
            continue
        attempts = ((args, kwargs), *variants)
        for call_args, call_kwargs in attempts:
            try:
                result = fn(*call_args, **call_kwargs)
                if inspect.isawaitable(result):
                    result = await result
                return result
            except TypeError as exc:
                # 签名不匹配 → 试下一个变体(★ 只吞 TypeError:业务异常必须冒出去)
                last_exc = exc
                logger.debug("调用 {} 的签名不匹配,尝试下一个变体: {}", name, exc)
        break
    if last_exc is not None:
        raise last_exc
    raise AttributeError(f"服务 {type(service).__name__} 缺少方法:{'/'.join(names)}")


def _alert_service(runtime: Any) -> Any:
    """惰性取预警域服务(此时 ``domains/alert`` 可能还不存在 → 抛 ImportError)。"""
    return runtime.alert()


def _review_service(runtime: Any) -> Any:
    """惰性取点评域服务(此时 ``domains/review`` 可能还不存在 → 抛 ImportError)。"""
    return runtime.review()


async def _alert_test_reply(runtime: Any, args: list[str]) -> str:
    """``预警测试 [酒店名]``:干跑预览(**不发送、不写状态**)。

    约定调用(与 ``cli.py:1124`` 的 ``alert test`` 同一签名)::

        runtime.alert().check(slot, dry_run=True, rule_id=None, hotel_name=<可选>)

    返回 ``{"triggers": [{"title", "detail_lines"}], "rules_checked", "hotels_checked",
    "skipped"}``(与旧 ``check_tier("09:00", dry_run=True)`` 同形)。
    """
    try:
        service = _alert_service(runtime)
    except (ImportError, AttributeError) as exc:
        return f"预警模块未就绪: {_readiness(exc)}"
    hotel_name = args[0] if args else None
    try:
        summary = await _call_method(
            service,
            ("check", "check_tier"),
            "09:00",
            dry_run=True,
            rule_id=None,
            hotel_name=hotel_name,
            _variants=((("09:00",), {"dry_run": True, "hotel_name": hotel_name}),),
        )
    except (ImportError, AttributeError) as exc:
        return f"预警模块未就绪: {_readiness(exc)}"
    except Exception as exc:  # noqa: BLE001 - 域内业务异常 → 可读文案
        logger.exception("预警测试失败")
        return f"预警测试失败：{exc}"
    if not isinstance(summary, dict):
        return _finalize(summary)
    triggers = list(summary.get("triggers") or [])
    if hotel_name:
        triggers = [t for t in triggers if hotel_name in str((t or {}).get("hotel_name") or "")]
    if not triggers:
        return (
            f"当前无预警触发（规则 {summary.get('rules_checked')} / "
            f"酒店 {summary.get('hotels_checked')}；跳过 {summary.get('skipped', {})}）"
        )
    lines: list[str] = []
    for item in triggers[:5]:
        detail = "\n".join(f"   {ln}" for ln in ((item or {}).get("detail_lines") or []))
        lines.append(f"● {(item or {}).get('title')}\n{detail}")
    return "(预警测试·仅预览不发送)\n" + "\n\n".join(lines)[:2800]


async def _alert_state_reply(runtime: Any, args: list[str]) -> str:
    """``预警状态 [酒店名]`` → ``runtime.alert().status(hotel_name)``。

    域未就绪 → ``"预警模块未就绪: ..."``;域内异常 → ``"预警状态查询失败：..."``。
    """
    try:
        service = _alert_service(runtime)
    except (ImportError, AttributeError) as exc:
        return f"预警模块未就绪: {_readiness(exc)}"
    hotel_name = args[0] if args else None
    try:
        result = await _call_method(service, ("status", "state"), hotel_name)
    except (ImportError, AttributeError) as exc:
        return f"预警模块未就绪: {_readiness(exc)}"
    except Exception as exc:  # noqa: BLE001
        logger.exception("预警状态查询失败")
        return f"预警状态查询失败：{exc}"
    return _finalize(result)


async def _ignore_hotel_reply(runtime: Any, args: list[str]) -> str:
    """``忽略此店 <酒店名> [天数=7]`` → ``runtime.alert().ignore_hotel(店, 天数)``。"""
    tokens = _ws_tokens(args)
    if not tokens:
        return "请使用：忽略此店 <酒店名> [天数]"
    hotel_name = tokens[0]
    days = 7
    if len(tokens) > 1:
        try:
            days = max(1, int(tokens[1]))
        except ValueError:
            return "天数应为整数"
    try:
        service = _alert_service(runtime)
    except (ImportError, AttributeError) as exc:
        return f"预警模块未就绪: {_readiness(exc)}"
    try:
        result = await _call_method(
            service,
            ("ignore_hotel", "ignore"),
            hotel_name,
            days,
            reason="管理群命令「忽略此店」",
        )
    except (ImportError, AttributeError) as exc:
        return f"预警模块未就绪: {_readiness(exc)}"
    except Exception as exc:  # noqa: BLE001
        logger.exception("忽略此店失败")
        return f"忽略此店失败：{exc}"
    if result is None or result is False:
        return f"❌ 未忽略「{hotel_name}」（酒店不存在或写入未生效）"
    if result is True:
        until = (date.today() + timedelta(days=days - 1)).isoformat()
        return f"✅ 已忽略「{hotel_name}」全部预警 {days} 天（至 {until}）"
    return _finalize(result)


def _parse_alert_lines(tokens: list[str]) -> tuple[float | None, float | None] | str:
    """解析 ``高 <数值> 低 <数值>``(可单给)。返回 ``(high, low)`` 或**错误文案**。"""
    high: float | None = None
    low: float | None = None
    idx = 0
    while idx < len(tokens):
        token = tokens[idx]
        if token == "高" and idx + 1 < len(tokens):
            try:
                high = float(tokens[idx + 1])
            except ValueError:
                return f"数值非法: {tokens[idx + 1]}"
            idx += 2
            continue
        if token == "低" and idx + 1 < len(tokens):
            try:
                low = float(tokens[idx + 1])
            except ValueError:
                return f"数值非法: {tokens[idx + 1]}"
            idx += 2
            continue
        return f"参数非法: {token}（应为 高 <数值> 低 <数值>）"
    return high, low


async def _price_line_reply(runtime: Any, args: list[str]) -> str:
    """``预警线 <酒店名> 高 <数值> 低 <数值>`` → ``runtime.alert().set_lines(店, 高, 低)``。

    **缺省 = 取消**(两条线都不给 → 传 ``None, None``)。
    """
    tokens = _ws_tokens(args)
    if not tokens:
        return "请使用：预警线 <酒店名> 高 <数值> 低 <数值>（高/低 可单给；不跟数值=取消该线）"
    hotel_name = tokens[0]
    parsed = _parse_alert_lines(tokens[1:])
    if isinstance(parsed, str):
        return parsed
    high, low = parsed
    try:
        service = _alert_service(runtime)
    except (ImportError, AttributeError) as exc:
        return f"预警模块未就绪: {_readiness(exc)}"
    try:
        result = await _call_method(
            service,
            ("set_lines", "set_alert_lines"),
            hotel_name,
            high=high,
            low=low,
            _variants=(((hotel_name, high, low), {}),),
        )
    except (ImportError, AttributeError) as exc:
        return f"预警模块未就绪: {_readiness(exc)}"
    except Exception as exc:  # noqa: BLE001
        logger.exception("设置预警线失败")
        return f"设置预警线失败：{exc}"
    if high is None and low is None:
        return f"已取消「{hotel_name}」预警线（默认不触发）" if result else f"「{hotel_name}」本就未设置预警线"
    parts: list[str] = []
    if high is not None:
        parts.append(f"高 {high:g}")
    if low is not None:
        parts.append(f"低 {low:g}")
    return f"✅ 已设置「{hotel_name}」预警线：{' / '.join(parts)} 元"


# ---------------------------------------------------------------------------
# 点评命令(惰性 import)
# ---------------------------------------------------------------------------


def _review_hotel_name(args: list[str], *, need: bool) -> str:
    """点评命令的 ``[酒店名]`` 参数(**可能带空格**,故用空格连接)。

    ``need=True``(点评策略)时缺参 → 返回空串,由调用方给出用法提示。
    """
    name = " ".join(a for a in args if a).strip()
    if need and not name:
        return ""
    return name


async def _review_todo_reply(runtime: Any, args: list[str]) -> str:
    """``点评待办 [酒店名]``:当场刷新 + 列出草稿(**仅预览不推送**)。

    ``cli.py:1169`` 的 ``review draft``(不带 ``--push``)用的就是
    ``runtime.review().drafts(hotel)`` —— 本命令与它**同一语义**:
    只列草稿,绝不推送(推送给管理群是 ``suggest()``,那是定时任务的事)。
    为了让域层的方法名有回旋余地,候选顺序为
    ``refresh_pending`` → ``pending_drafts`` → ``drafts_for_hotel`` → ``drafts``。
    """
    try:
        service = _review_service(runtime)
    except (ImportError, AttributeError) as exc:
        return f"点评模块未就绪: {_readiness(exc)}"
    hotel_name = _review_hotel_name(args, need=False) or None
    try:
        result = await _call_method(
            service,
            ("refresh_pending", "pending_drafts", "drafts_for_hotel", "drafts"),
            hotel_name,
            _variants=(((), {"hotel": hotel_name}),),
        )
    except (ImportError, AttributeError) as exc:
        return f"点评模块未就绪: {_readiness(exc)}"
    except Exception as exc:  # noqa: BLE001
        logger.exception("点评待办失败")
        return f"点评待办失败：{exc}"
    return _finalize(result)


async def _review_status_reply(runtime: Any, args: list[str]) -> str:
    """``点评状态 [酒店名]``:待回复 / 审计统计 / 自动模式状态。

    与 ``cli.py:1202`` 的 ``review status`` 同一签名:``runtime.review().status(hotel)``;
    酒店名缺省传 ``None``(全店汇总)。
    """
    try:
        service = _review_service(runtime)
    except (ImportError, AttributeError) as exc:
        return f"点评模块未就绪: {_readiness(exc)}"
    hotel_name = _review_hotel_name(args, need=False) or None
    try:
        result = await _call_method(
            service,
            ("status", "stats", "review_stats"),
            hotel_name,
            _variants=(((), {"hotel": hotel_name}),),
        )
    except (ImportError, AttributeError) as exc:
        return f"点评模块未就绪: {_readiness(exc)}"
    except Exception as exc:  # noqa: BLE001
        logger.exception("点评状态查询失败")
        return f"点评状态查询失败：{exc}"
    return _finalize(result)


async def _review_policy_reply(runtime: Any, args: list[str]) -> str:
    """``点评策略 <酒店名> [好评 <模板id>] [差评 silent|template [模板id]]``。

    语法**在本层校验**(参数非法要当场告诉人,不能丢给域层猜),
    通过后整串 tokens 交给域服务:``set_policy`` / ``set_strategy`` / ``update_policy``。
    """
    tokens = _ws_tokens(args)
    if not tokens:
        return "请使用：点评策略 <酒店名> [好评 <模板id>] [差评 silent|template [模板id]]"
    hotel_name = tokens[0]
    rest = tokens[1:]
    idx = 0
    while idx < len(rest):
        token = rest[idx]
        if token == "好评" and idx + 1 < len(rest):
            idx += 2
            continue
        if token == "差评" and idx + 1 < len(rest):
            value = rest[idx + 1].lower()
            if value not in ("silent", "template"):
                return f"差评策略非法: {value}（应为 silent|template）"
            idx += 2
            if value == "template" and idx < len(rest) and rest[idx] not in ("好评", "差评"):
                idx += 1
            continue
        return f"参数非法: {token}"
    try:
        service = _review_service(runtime)
    except (ImportError, AttributeError) as exc:
        return f"点评模块未就绪: {_readiness(exc)}"
    try:
        result = await _call_method(
            service,
            ("set_policy", "set_strategy", "update_policy"),
            hotel_name,
            changes=rest,
            _variants=(((hotel_name, rest), {}), ((hotel_name, *rest), {})),
        )
    except (ImportError, AttributeError) as exc:
        return f"点评模块未就绪: {_readiness(exc)}"
    except Exception as exc:  # noqa: BLE001
        logger.exception("设置点评策略失败")
        return f"设置点评策略失败：{exc}"
    return _finalize(result)


def _parse_review_ref(token: str) -> int | str:
    """解析 ``<评测#id>``:纯数字 → ``int``(草稿里 "评测#12" 的序号);
    否则原样字符串(新架构 ``review_reviews.review_id`` 是平台 ``commentId`` / 指纹)。

    为什么两种都收:旧实现的 id 是 SQLite 自增主键(整数),新架构
    ``infra/models/review.py`` 明确 ``review_id`` 是**平台 commentId 或内容指纹**
    (``h+sha1[:16]``)。命令层不猜域层的取值空间 —— 数字给 int、其余给 str,
    由 ``runtime.review()`` 决定怎么认。
    """
    text = str(token).lstrip("#").strip()
    try:
        return int(text)
    except ValueError:
        return text


async def _reply_transition_reply(runtime: Any, args: list[str], status: str) -> str:
    """``回复确认`` / ``已处理`` → ``ok``;``已忽略`` → ``ignored``。

    ``回复确认`` 与 ``已处理`` **同路**(旧 ``commands.py:625`` 的
    ``if cmd in ("回复确认", "已处理")`` 分支)。
    """
    if not args:
        return f"请使用：{'回复确认' if status == 'ok' else '已忽略'} <评测#id>"
    review_ref = _parse_review_ref(args[0])
    try:
        service = _review_service(runtime)
    except (ImportError, AttributeError) as exc:
        return f"点评模块未就绪: {_readiness(exc)}"
    try:
        result = await _call_method(
            service,
            ("transition_reply", "transition"),
            review_ref,
            status=status,
            exec_by="manage",
            _variants=(((review_ref, status), {"exec_by": "manage"}),),
        )
    except (ImportError, AttributeError) as exc:
        return f"点评模块未就绪: {_readiness(exc)}"
    except Exception as exc:  # noqa: BLE001
        logger.exception("点评状态流转失败")
        return f"点评状态流转失败：{exc}"
    if isinstance(result, dict):
        if result.get("ok") is False:
            return f"❌ {result.get('error') or '状态流转失败'}"
        if result.get("ok"):
            return f"✅ 点评 #{review_ref} 已标记为「{result.get('status', status)}」（策略={result.get('strategy') or '-'}）"
    return _finalize(result)


# ---------------------------------------------------------------------------
# 入口:识别 + 生成回复文本(T2B.1)
# ---------------------------------------------------------------------------


async def handle_command(runtime: Any, bot: Any, frame: dict, content: str) -> str | None:
    """命令命中 → 返回回复文本(**不发送**);未命中 → ``None``(回退问答链)。

    ``bot`` 仅作为"接收者"标识保留(发送由 :class:`~hoteldata.domains.bot.router.MessageRouter`
    经 ``Sender`` 完成),命令层不碰 ``BotClient``。

    流程:
      1. ``parse_command`` 不命中 → ``None``;
      2. **私聊无 ``chatid``** → ``None``(V28:命令只在群聊生效,回退问答);
      3. 11 条管理群命令 → ``settings.push.is_manage`` 不通过就回
         ``"⛔ 该命令仅管理群可用"``(**未配置白名单时一律拒绝**,V27);
      4. 分派到具体回复函数。
    """
    parsed = parse_command(content)
    if parsed is None:
        return None
    cmd, args = parsed
    chatid = chatid_of(frame)
    if not chatid:
        return None  # 私聊:命令仅在群聊生效,回退问答(V28)
    logger.info("群内命令: cmd={} args={} chatid={}", cmd, args, chatid)

    if cmd in MANAGE_COMMANDS and not _is_manage(runtime, chatid):
        logger.info("管理群命令被拒绝: cmd={} chatid={}", cmd, chatid)
        return NOT_MANAGE_TEXT

    if cmd == "绑定":
        return await _bind_reply(runtime, chatid, args)
    if cmd == "解绑":
        return await _unbind_reply(runtime, chatid, args)
    if cmd == "我的酒店":
        return await _my_hotels_reply(runtime, chatid)
    if cmd in ("今日数据", "重推"):
        return await _push_reply(runtime, chatid)
    if cmd == "帮助":
        return _HELP_TEXT
    if cmd == "汇总":
        return await _summary_reply(runtime)
    if cmd == "状态":
        return await _status_reply(runtime)
    if cmd == "预警测试":
        return await _alert_test_reply(runtime, args)
    if cmd == "预警状态":
        return await _alert_state_reply(runtime, args)
    if cmd == "忽略此店":
        return await _ignore_hotel_reply(runtime, args)
    if cmd == "预警线":
        return await _price_line_reply(runtime, args)
    if cmd == "点评待办":
        return await _review_todo_reply(runtime, args)
    if cmd == "点评状态":
        return await _review_status_reply(runtime, args)
    if cmd == "点评策略":
        return await _review_policy_reply(runtime, args)
    if cmd in ("回复确认", "已处理"):
        return await _reply_transition_reply(runtime, args, "ok")
    if cmd == "已忽略":
        return await _reply_transition_reply(runtime, args, "ignored")
    return None
