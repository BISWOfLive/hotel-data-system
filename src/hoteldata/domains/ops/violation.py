"""T2G.1(后半)违约实时监听 —— **count 增加才推,首次只记基线**(V-AG 口径)。

**逐字继承的三条语义(旧 ``app/violation_realtime.py:89-145``)**
==============================================================

1. **只推"新增违约"**:``count > 上一次`` 才推;``持平 / 减少`` **保持安静**
   (减少说明商家已处理,再去打扰就是"狼来了");
2. **首次只记基线不推**:第一次看到某店时把 ``count`` 记进状态文件但**不推送** ——
   否则每次部署/清状态都会把历史违约当成"新增"轰一遍群(旧 ``violation_realtime.py:127-131``);
3. **无数据不推断**:``违约记录数`` 缺失/不可解析 → ``None`` → 记 ``logger.info`` 跳过
   (旧 ``_count_of``,旧 ``violation_realtime.py:37-43``)。

★★ 与旧系统的**有意差异**:状态文件落在 ``var/states/``,不是 ``config/``
=======================================================================

旧系统把状态写在 ``config/violation_realtime_state.json``(旧
``violation_realtime.py:21``);新架构 **``settings.py`` 的三条禁令之二**明确写着
「🚫 **可变状态不进 ``config/``** —— 落库」。这条差异是**刻意的**:

* ``config/`` 只放"运行规则"(``api_rules.json`` / ``review_templates.json`` 这类**只增不改义**
  的资产),它要能被 git 管理与整体替换;
* 运行期可变状态(违约 ``count`` 基线)属于 ``var/``,与 ``var/states/*.json``
  的登录态一致,清理/备份策略也统一(``ops.cleanup`` 的白名单按 ``var/`` 组织)。
* 落盘用 :func:`hoteldata.infra.atomic.atomic_write_json`(B20 原子写纪律:
  Windows/Python 3.14 上 ``fsync`` 必须用**可写句柄**,见 ``infra/atomic.py``)。

**模块名不硬编码**:「违约看板/违规中心」这个名字从 ``config/api_rules.json`` 的
``sub_modules[*].name`` 里**找含「违约」/「违规」的那个**(计划书批次 G 明确要求
"不要硬编码猜测");找不到 → ``logger.warning`` 并**跳过**,不猜、不静默。
窗口名同样从该模块的 ``windows`` 取(实测为 ``实时``,但不写死)。

**文案**:``config/prompts/alert_violation.md``,占位符 ``{hotel_name}`` /
``{detail_lines}``;**残留 ``{x}`` 一律清空**(计划书 §5.7 的渲染纪律:
"不能推出去一堆花括号")。

**推送目标**:该店**绑定群** + **管理群**(``MANAGE_CHATIDS``),走
``runtime.push.push(BuiltMessage(push_type='violation_realtime'))`` ——
每群一行 ``push_logs`` 审计,且天然享受派发器的 slot 去重与限频
(旧系统用 ``send_with_retry`` 逐群裸发,没有任何审计行)。
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import select

from hoteldata.infra.models import Hotel
from hoteldata.push.service import BuiltMessage

__all__ = ["STATE_FILENAME", "PUSH_TYPE", "find_violation_module", "run", "state_path"]

#: 状态文件名(``var/states/`` 下;**不是** ``config/``,见模块 docstring)
STATE_FILENAME = "violation_state.json"
#: 推送类型(A2-7 命名)
PUSH_TYPE = "violation_realtime"
#: 文案模板
TEMPLATE_FILENAME = "alert_violation.md"
#: 违约明细行数上限(旧 ``_detail_lines(payload, limit=5)``)
DETAIL_LIMIT = 5
#: 模块名匹配关键词(``sub_modules[*].name`` 命中即用)
_MODULE_KEYWORDS = ("违约", "违规")
#: ``{...}`` 残留清空
_BRACE_RE = re.compile(r"\{\{[^{}]*\}\}|\{[^{}]*\}")
_STRAY_BRACE_RE = re.compile(r"[{}]")


def state_path(runtime: Any) -> Path:
    """状态文件路径:``<var>/states/violation_state.json``。"""
    layout = getattr(runtime, "layout", None)
    base = getattr(layout, "states_dir", None)
    if base is None:
        base = Path(runtime.settings.paths.var_dir) / "states"
    return Path(base) / STATE_FILENAME


def find_violation_module(runtime: Any) -> tuple[str, str | None] | None:
    """从 ``api_rules.json`` 的 ``sub_modules`` 里找「违约 / 违规」模块 → ``(模块名, 窗口)``。

    返回 ``None`` = 规则里没有这个模块(调用方必须 **warning + 跳过**,不许硬编码猜名字)。
    """
    rules = getattr(runtime, "rules", None)
    if rules is None:
        logger.warning("[违约实时] 规则未加载(runtime.rules 为空),跳过")
        return None
    try:
        modules = list(rules.all_sub_modules())
    except Exception as exc:  # noqa: BLE001
        logger.warning("[违约实时] 读取 sub_modules 失败: {}", exc)
        return None
    for _page, sub in modules:
        name = str(getattr(sub, "name", "") or "")
        if any(keyword in name for keyword in _MODULE_KEYWORDS):
            windows = list(getattr(sub, "windows", []) or [])
            window = str(windows[0]) if windows else None
            logger.info("[违约实时] 命中模块「{}」窗口={}", name, window)
            return name, window
    logger.warning(
        "[违约实时] api_rules.json 的 sub_modules 里找不到含「违约」/「违规」的模块,跳过(不硬编码猜测)"
    )
    return None


# ---------------------------------------------------------------------------
# 状态读写(var/states,原子写)
# ---------------------------------------------------------------------------


def _load_state(path: Path) -> dict[str, Any]:
    from hoteldata.infra.atomic import read_json

    data = read_json(path, default={})
    return data if isinstance(data, dict) else {}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    """原子写状态(B20:同目录临时文件 → fsync → ``os.replace``)。"""
    from hoteldata.infra.atomic import atomic_write_json

    try:
        atomic_write_json(path, state, indent=1)
    except OSError as exc:  # pragma: no cover - 磁盘满等
        logger.error("[违约实时] 状态写入失败({});本次基线未持久化,下次可能重复推送", exc)


# ---------------------------------------------------------------------------
# payload 解析(旧 ``_count_of`` / ``_detail_lines`` 逐字)
# ---------------------------------------------------------------------------


def _count_of(payload: dict[str, Any]) -> int | None:
    """违约记录数 → ``int``;无/异常 → ``None``(无数据,**不推断**)。"""
    try:
        value = payload.get("违约记录数")
        return int(value) if value not in (None, "") else 0
    except (TypeError, ValueError):
        return None


def _detail_lines(payload: dict[str, Any], limit: int = DETAIL_LIMIT) -> list[str]:
    """违约明细 → 摘要行(``createTime`` / ``categories`` / ``status``,旧实现逐字)。"""
    raw = payload.get("违约明细列表")
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw[:limit]:
        if not isinstance(item, dict):
            continue
        created = str(item.get("createTime") or "")[:10] or "—"
        category = str(
            item.get("categories")
            or item.get("categoryName")
            or item.get("type")
            or item.get("violationType")
            or ""
        )
        status = str(item.get("status") or "")
        line = " ".join(part for part in (created, category, status) if part).strip()
        if line:
            out.append(line)
    return out


def load_template(runtime: Any) -> str:
    """读 ``config/prompts/alert_violation.md``;缺失 → 内置兜底文案(不静默)。"""
    path = Path(runtime.settings.paths.config_dir) / "prompts" / TEMPLATE_FILENAME
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("[违约实时] 文案模板读取失败({}),使用内置兜底文案", exc)
        return "【商家违约提醒】{hotel_name}\n\n{detail_lines}\n\n⚠️ 检测到违约记录,请尽快核实处理。"


def render_text(template: str, hotel_name: str, details: list[str]) -> str:
    """渲染文案并**清空一切 ``{x}`` 残留**(计划书 §5.7 的渲染纪律)。"""
    lines = "\n".join(f"- {item}" for item in details) or "- (无明细)"
    text = str(template or "").replace("{hotel_name}", str(hotel_name or "")).replace(
        "{detail_lines}", lines
    )
    text = _BRACE_RE.sub("", text)
    text = _STRAY_BRACE_RE.sub("", text)
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


async def run(runtime: Any) -> dict[str, Any]:
    """``ops.violation`` 任务:轮询违约看板 → ``count`` 增加才推。

    返回 ``{"ok", "enabled", "checked", "hotels", "pushed", "new", "skipped",
    "details", "errors"}``(**JSON 可序列化**,进 ``job_runs.summary``)。
    """
    out: dict[str, Any] = {
        "ok": True,
        "enabled": bool(runtime.settings.ops_push.violation_enabled),
        "module": None,
        "checked": 0,
        "hotels": 0,
        "pushed": 0,
        "new": 0,
        "skipped": 0,
        "states": {},
        "errors": [],
    }
    if not runtime.settings.ops_push.violation_enabled:
        out["note"] = "VIOLATION_REALTIME_ENABLED=0(违约实时监听关闭)"
        logger.info("[违约实时] 未启用(VIOLATION_REALTIME_ENABLED=0)")
        return out

    located = find_violation_module(runtime)
    if located is None:
        out["skipped"] = 1
        out["ok"] = False
        out["errors"].append("api_rules.json 未找到含「违约」/「违规」的子模块")
        return out
    module, window = located
    out["module"] = f"{module}/{window}" if window else module

    from hoteldata.domains.collect.repository import CollectRepository

    path = state_path(runtime)
    state = _load_state(path)
    now = datetime.now(runtime.settings.tzinfo).strftime("%Y-%m-%d %H:%M:%S")
    template = load_template(runtime)

    async with runtime.db.session() as session:
        hotels = list(
            (await session.execute(select(Hotel).where(Hotel.status == "active").order_by(Hotel.id)))
            .scalars()
            .all()
        )
    manage = list(runtime.settings.push.manage_chatids)

    for hotel in hotels:
        out["checked"] += 1
        try:
            async with runtime.db.session() as session:
                record = await CollectRepository(session).latest_module(int(hotel.id), module, window)
        except Exception as exc:  # noqa: BLE001 - 单店失败不阻断其余酒店
            out["errors"].append(f"{hotel.name}: {exc}")
            logger.warning("[违约实时] 酒店「{}」读取失败: {}", hotel.name, exc)
            continue
        if record is None:
            # ★ 从来没采过这个模块 → **不记基线**(记 0 会让"首次采集到真实数据"被误判成新增)
            logger.info("[违约实时] 酒店「{}」无「{}」模块记录,跳过", hotel.name, module)
            continue
        payload = dict(record.payload_json or {})
        count = _count_of(payload)
        if count is None:
            logger.info("[违约实时] 酒店「{}」违约数据无记录,跳过", hotel.name)
            continue

        key = str(int(hotel.id))
        previous = (state.get(key) or {}).get("count") if isinstance(state.get(key), dict) else None
        if previous is None:
            # ★ 首次:只记基线,**不推**(否则每次部署都把历史违约轰一遍)
            state[key] = {"count": count, "ts": now, "hotel": str(hotel.name)}
            out["states"][key] = {"count": count, "baseline": True}
            logger.info("[违约实时] 酒店「{}」首次基线 count={}(不推)", hotel.name, count)
            continue

        try:
            previous_count = int(previous)
        except (TypeError, ValueError):
            previous_count = count
        if count <= previous_count:
            state[key] = {"count": count, "ts": now, "hotel": str(hotel.name)}
            out["states"][key] = {"count": count, "baseline": False, "delta": count - previous_count}
            logger.info(
                "[违约实时] 酒店「{}」count {} 无新增(上次 {}),保持安静",
                hotel.name,
                count,
                previous_count,
            )
            continue

        added = count - previous_count
        state[key] = {"count": count, "ts": now, "hotel": str(hotel.name)}
        out["states"][key] = {"count": count, "baseline": False, "delta": added}
        out["new"] += added
        details = _detail_lines(payload)
        text = render_text(template, str(hotel.name), details)
        try:
            chatids = await runtime.bindings.groups_of_hotel(int(hotel.id))
        except Exception as exc:  # noqa: BLE001
            chatids = []
            out["errors"].append(f"{hotel.name} 绑定群读取失败: {exc}")
            logger.warning("[违约实时] 酒店「{}」绑定群读取失败: {}", hotel.name, exc)
        targets: list[str] = []
        for chatid in [*chatids, *manage]:
            if chatid and chatid not in targets:
                targets.append(chatid)
        if not targets:
            out["skipped"] += 1
            logger.warning("[违约实时] 酒店「{}」新增违约 {} 条,但无绑定群且未配置管理群,未推送", hotel.name, added)
            continue
        for chatid in targets:
            await runtime.push.push(
                BuiltMessage(
                    chatid=chatid,
                    push_type=PUSH_TYPE,
                    content=text,
                    hotel_ids=(int(hotel.id),),
                    note=f"违约新增 {added} 条(共 {count} 条)",
                    meta={"previous": previous_count, "count": count, "added": added},
                )
            )
        out["pushed"] += len(targets)
        out["hotels"] += 1
        logger.info(
            "[违约实时] 酒店「{}」新增违约 {} 条({}→{}),已入队 {} 个群",
            hotel.name,
            added,
            previous_count,
            count,
            len(targets),
        )

    _save_state(path, state)
    return out
