"""渲染契约 —— 硬编码 markdown,**不引入模板引擎**(T2D.4)。

权威参考是旧 ``app/renderer.py``(295 行),逐条对应计划书 §5.6 的渲染契约表:

======================  ==========================================================================
元素                     规则
======================  ==========================================================================
数字                     千分位;``float`` 整值去小数(``3.0`` → ``"3"``);``None`` → ``"—"``;
                         ``bool`` → ``true``/``false``;字符串**原样**
环比                     🟢/🔴 + 1 位小数;≈0 → ``"0.0%"``;上期缺失/0/非数值 → ``"—"``
展开                     **一层** ``parent.child``;``list`` 值跳过(交给列表渲染)
表格                     有上期 → 四列 ``| 指标 | 本期 | 上期 | 环比 |``;否则三列
列表                     dict 列表 → 表(**≤8 行**,单元格截 **30** 字);
                         标量列表 → 单行 **≤12 值** + ``…(共N期)``
脚注                     ``> 数据来源:携程 eBooking 数据中心 · 采集时间 {created_at}``
标题                     ``#### 【模块】·窗口(采集日期 YYYY-MM-DD)``
======================  ==========================================================================

两处**有意与旧实现不同**(都写在行内注释里,便于复核):

1. **环比固定 1 位小数**。旧 ``renderer.py:66-67`` 会把 ``+12.0%`` 去尾零成 ``+12%``;
   计划书 §5.6 的契约表写的是"🟢/🔴 + **1 位小数**"。按**计划书**执行
   (``STRIP_TRAILING_ZERO`` 保留开关,需要旧行为时改一行)。
2. **上期列为空时不摆四列**。旧实现只要 ``compare`` 是个 dict(哪怕 ``{}``)就摆四列,
   全是 ``—``;计划书写的是"**有上期** → 四列"。

★ ``branch`` 模板机制**保留**(计划书 §5.6 末注)
=============================================

``config/prompts/<name>.md`` 里替换 ``{name}`` / ``{status}`` / ``{detail}``
(旧 ``report_push.resolve_branch``,``report_push.py:47-86``)。22 项**无一配置**它,
机制闲置但必须留着(预警在用同类机制,甲方"违约有无"类文案可能要用)。
**残留 ``{x}`` 一律清空** —— 绝不能把一堆花括号推到群里(附录 C「残留 {x} 必须清空」)。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from loguru import logger

from hoteldata.domains.report.engine import flatten_scalars, item_field

__all__ = [
    "MISSING",
    "compare_map",
    "first_list_key",
    "fmt_num",
    "fmt_ratio",
    "list_section",
    "payload_of",
    "render_data_block",
    "render_item",
    "resolve_branch",
    "scalar_rows",
    "scalar_table",
    "strip_placeholders",
]

#: 缺值占位(计划书 §5.6:``None`` → ``"—"``)
MISSING = "—"

#: 环比保留位数(计划书 §5.6:**1 位小数**)
RATIO_DECIMALS = 1

#: 是否去掉 ``.0`` 尾零。旧 ``renderer.py:66-67`` 会去掉;计划书 §5.6 要求保留 1 位。
#: 保留这个开关是为了"想切回旧行为时只改一行",默认按计划书。
STRIP_TRAILING_ZERO = False

#: 标量表基线行数(旧 ``renderer.py:97`` ``max_rows: int = 15``)
DEFAULT_MAX_ROWS = 15
#: 列表表基线行数(计划书 §5.6:dict 列表 ≤8 行)
DEFAULT_LIST_ROWS = 8
#: 列表单元格截断长度(计划书 §5.6:30 字)
CELL_LIMIT = 30
#: 标量列表单行显示上限(计划书 §5.6:≤12 值)
SCALAR_LIST_LIMIT = 12


# ---------------------------------------------------------------------------
# 数字与环比
# ---------------------------------------------------------------------------


def fmt_num(value: Any) -> str:
    """数值 → 千分位字符串(旧 ``renderer.py:23-44`` 逐字)。

    ``None`` → ``"—"``;``bool`` → ``"true"``/``"false"``;``int``/``float`` → 千分位
    (``float`` 整值 ``3.0`` → ``"3"``);字符串标量 → **原样返回**(房型名/数据更新时间);
    其余不可显示值 → ``"—"``。
    """
    if value is None:
        return MISSING
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        if value.is_integer():
            return f"{int(value):,}"
        return f"{value:,}"
    if isinstance(value, str):
        return value
    return MISSING


def fmt_ratio(current: Any, previous: Any) -> str:
    """环比文案(计划书 §5.6)。

    * 上期缺失 / ``0`` / 任一非数值(``bool`` 也算非数值)→ ``"—"``;
    * ≈0(四舍五入到 1 位为 ``0.0``)→ ``"0.0%"``(**无箭头**);
    * 正 → ``"🟢 +12.3%"``;负 → ``"🔴 -5.2%"``。
    """
    if current is None or previous is None:
        return MISSING
    if isinstance(current, bool) or isinstance(previous, bool):
        return MISSING
    if not isinstance(current, (int, float)) or not isinstance(previous, (int, float)):
        return MISSING
    if previous == 0:
        return MISSING
    ratio = (current - previous) / previous * 100
    pct = f"{abs(ratio):.{RATIO_DECIMALS}f}"
    if float(pct) == 0:
        return "0.0%"
    if STRIP_TRAILING_ZERO and pct.endswith(".0"):
        pct = pct[:-2]
    if ratio > 0:
        return f"🟢 +{pct}%"
    return f"🔴 -{pct}%"


# ---------------------------------------------------------------------------
# 展开与表格
# ---------------------------------------------------------------------------


def compare_map(compare: Any) -> dict[str, Any] | None:
    """归一化 ``compare`` → 扁平 ``{指标: 上期值}``(旧 ``renderer.py:204-212``)。

    吃三种形状,段2 三种都会遇到:

    * ``{"payload": {...}}`` / ``{"indicators": {...}}`` —— 包壳(取数层返回的 record);
    * ``{指标: {"prev": 10, "delta": 2, "pct": 0.2}}`` —— 段1 ``build_payload`` 的 compare;
    * ``{指标: 上期值}`` —— 段2 周报的 ``aggregate_with_compare``(D10)。

    ⚠️ 不能直接复用 ``flatten_scalars``:它会把 ``{"离店间夜": {"prev": 10}}``
    展开成键 ``离店间夜.prev``,而上期列要的是 ``离店间夜``(旧实现的 ``_flatten_map``
    在这一点上其实是**错的**,只是旧 compare 恰好是扁平结构才没暴露)。
    """
    if not isinstance(compare, Mapping) or not compare:
        return None
    inner: Any = compare
    for key in ("payload", "indicators", "compare"):
        shell = compare.get(key)
        if isinstance(shell, Mapping) and len(compare) <= 2:
            inner = shell
            break
    out: dict[str, Any] = {}
    for key, value in inner.items():
        if isinstance(value, Mapping):
            if "prev" in value:
                out[str(key)] = value.get("prev")
                continue
            for child_key, child_value in value.items():
                if isinstance(child_value, (Mapping, list)):
                    continue
                out[f"{key}.{child_key}"] = child_value
        elif isinstance(value, list):
            continue
        else:
            out[str(key)] = value
    return out or None


def payload_of(record: Any) -> dict[str, Any]:
    """从记录对象里取待渲染 payload(优先 ``payload``,次取 ``indicators``;旧 ``renderer.py:193-201``)。"""
    if not isinstance(record, Mapping):
        return {}
    for key in ("payload", "indicators"):
        value = record.get(key)
        if isinstance(value, Mapping):
            return dict(value)
    return {}


def scalar_rows(
    payload: Any,
    compare: Any = None,
    keys: Any = None,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> list[tuple[str, str, str]]:
    """展开 payload 的标量指标 → ``[(指标名, 本期文本, 环比文本)]``。

    * 指标名 = 页键;嵌套 dict 展开**一层** ``parent.child``;``list`` 值跳过(交列表渲染);
    * ``compare`` 提供时算环比;``keys`` 为字符串/序列时做过滤;``max_rows`` 封顶。
    """
    items = flatten_scalars(payload)
    if keys is not None and not isinstance(keys, str):
        wanted = {str(k) for k in keys}
        items = [it for it in items if it[0] in wanted]
    elif isinstance(keys, str):
        items = [it for it in items if it[0] == keys]
    cmap = compare_map(compare)
    rows: list[tuple[str, str, str]] = []
    for name, value in items[:max_rows]:
        ratio = MISSING if cmap is None else fmt_ratio(value, cmap.get(name))
        rows.append((name, fmt_num(value), ratio))
    return rows


def scalar_table(
    payload: Any,
    compare: Any = None,
    keys: Any = None,
    max_rows: int = DEFAULT_MAX_ROWS,
    title: str = "指标",
) -> str:
    """标量指标 Markdown 表(计划书 §5.6)。

    * **有上期** → 四列 ``| 指标 | 本期 | 上期 | 环比 |``;
    * 否则 → 三列 ``| 指标 | 本期 | 环比 |``。
    """
    rows = scalar_rows(payload, compare=compare, keys=keys, max_rows=max_rows)
    cmap = compare_map(compare)
    cols = [title, "本期"]
    if cmap is not None:
        cols.append("上期")
    cols.append("环比")
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for name, current_txt, ratio_txt in rows:
        if cmap is not None:
            lines.append(f"| {name} | {current_txt} | {fmt_num(cmap.get(name))} | {ratio_txt} |")
        else:
            lines.append(f"| {name} | {current_txt} | {ratio_txt} |")
    return "\n".join(lines)


def first_list_key(payload: Any) -> str | None:
    """payload 中第一个 ``list`` 键(旧 ``renderer.py:146-153``)。"""
    if not isinstance(payload, Mapping):
        return None
    for key, value in payload.items():
        if isinstance(value, list):
            return str(key)
    return None


def list_section(payload: Any, list_key: Any = None, max_rows: int = DEFAULT_LIST_ROWS) -> str:
    """``payload[list_key]`` 的明细渲染(计划书 §5.6)。

    * ``list`` 均为 dict → ``### 明细`` + Markdown 表(列=各 dict 键的并集,
      行 ≤ ``max_rows``,单元格 ``str(v)[:30]``);
    * ``list`` 均为标量 → ``**键名**: 1, 2, 3, …(共N期)``(≤12 值,截断防刷屏);
    * 非 list / 空列表 → ``""``。
    """
    if list_key is None:
        list_key = first_list_key(payload)
        if list_key is None:
            return ""
    if not isinstance(payload, Mapping):
        return ""
    value = payload.get(list_key)
    if not isinstance(value, list) or not value:
        return ""
    if all(isinstance(row, Mapping) for row in value):
        cols: list[str] = []
        for row in value:
            for key in row:
                if str(key) not in cols:
                    cols.append(str(key))
        if not cols:
            return ""
        lines = [
            "| " + " | ".join(cols) + " |",
            "|" + "---|" * len(cols),
        ]
        for row in value[:max_rows]:
            cells = [str(row.get(col, ""))[:CELL_LIMIT] for col in cols]
            lines.append("| " + " | ".join(cells) + " |")
        return "### 明细\n\n" + "\n".join(lines)
    shown = [fmt_num(x) for x in value[:SCALAR_LIST_LIMIT]]
    tail = f", …(共{len(value)}期)" if len(value) > SCALAR_LIST_LIMIT else ""
    return f"**{list_key}**: " + ", ".join(shown) + tail


# ---------------------------------------------------------------------------
# branch 模板机制(保留)
# ---------------------------------------------------------------------------


def strip_placeholders(text: str) -> str:
    """清空**残留** ``{x}`` 占位符(附录 C:不能把花括号推给甲方)。"""
    out: list[str] = []
    depth = 0
    for char in text:
        if char == "{":
            depth += 1
            continue
        if char == "}":
            if depth > 0:
                depth -= 1
                continue
            out.append(char)
            continue
        if depth == 0:
            out.append(char)
    return "".join(out)


def _template_path(name: Any) -> Path | None:
    """模板名 → ``config/prompts/<name>.md``(容错 ``prompts/`` 前缀与 ``.md`` 后缀)。"""
    from hoteldata.settings import get_settings

    fname = str(name or "").strip().replace("\\", "/")
    if not fname:
        return None
    if fname.endswith(".md"):
        fname = fname[:-3]
    if fname.startswith("prompts/"):
        fname = fname[len("prompts/") :]
    return Path(get_settings().paths.config_dir) / "prompts" / f"{fname}.md"


def resolve_branch(item: Any, payload: Any, hotel: Any = None) -> str | None:
    """按 ``item.branch = {field, has, none, detail_field}`` 选模板文本;模板缺失 → ``None``。

    旧 ``report_push.resolve_branch``(``report_push.py:47-86``)逐字:

    * ``payload[field]`` 真值 → ``has`` 模板,否则 ``none`` 模板;
    * 替换 ``{name}``(酒店名,缺省回退 item 名)/ ``{status}``(字段值)/ ``{detail}``
      (``detail_field`` 缺省 ``"detail"``);
    * 模板不存在 → ``None``(调用方回退数据渲染);
    * ★ 替换完**残留占位符一律清空**。
    """
    branch = item_field(item, "branch")
    if not isinstance(branch, Mapping):
        return None
    field = branch.get("field")
    if not field:
        return None
    value = payload.get(field) if isinstance(payload, Mapping) else None
    template_name = branch.get("has") if value else branch.get("none")
    if not template_name:
        return None
    path = _template_path(template_name)
    if path is None or not path.exists():
        logger.info("branch 模板不存在,回退数据渲染: {}", path)
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("branch 模板读取失败({}): {}", path, exc)
        return None
    detail_field = branch.get("detail_field") or "detail"
    detail = payload.get(detail_field, "") if isinstance(payload, Mapping) else ""
    owner = item_field(hotel, "name") or item_field(item, "name") or item_field(item, "module") or ""
    text = text.replace("{name}", str(owner))
    text = text.replace("{status}", "" if value is None else str(value))
    text = text.replace("{detail}", str(detail))
    return strip_placeholders(text)


# ---------------------------------------------------------------------------
# 整块渲染
# ---------------------------------------------------------------------------


def render_data_block(item: Any, record: Any, compare: Any = None, hotel: Any = None) -> str:
    """数据模式整块(标题 + 指标表 + 明细 + 来源脚注)。

    * 标题:``#### 【模块】·窗口(采集日期 YYYY-MM-DD)``,模块 = ``item.module or item.name``;
    * payload 里有 list-of-dict 键 → 先标量表,再逐 list 出明细(:func:`list_section`);
    * 结尾脚注:``> 数据来源:携程 eBooking 数据中心 · 采集时间 <created_at>``;
    * payload 无任何标量/列表 → 标题 + ``（无有效指标）``;
    * ``compare=None`` → 无上期列(三列表)。
    """
    payload = payload_of(record)
    module = item_field(item, "module") or item_field(item, "name") or ""
    window = ""
    collect_date = ""
    created_at = ""
    if isinstance(record, Mapping):
        window = str(record.get("window") or "")
        collect_date = str(record.get("collect_date") or "")
        created_at = str(record.get("created_at") or "")
    if not window:
        window = str(item_field(item, "window") or "") or _first_declared_window(item)
    if not collect_date:
        collect_date = str(item_field(item, "collect_date") or "")
    title = f"#### 【{module}】·{window}(采集日期 {collect_date})"

    scalars = flatten_scalars(payload)
    list_keys = [str(k) for k, v in payload.items() if isinstance(v, list)]
    has_content = bool(scalars) or any(isinstance(v, list) and v for v in payload.values())
    if not has_content:
        return title + "\n（无有效指标）"

    body: list[str] = [title]
    if scalars:
        body.append(scalar_table(payload, compare=compare))
    for list_key in list_keys:
        section = list_section(payload, list_key)
        if section:
            body.append(section)
    if created_at:
        body.append(f"> 数据来源:携程 eBooking 数据中心 · 采集时间 {created_at}")
    return "\n\n".join(body)


def _first_declared_window(item: Any) -> str:
    """item 声明的首个窗口(record 没带 window 时的兜底;旧 ``renderer.py:227-231``)。"""
    windows = item_field(item, "windows", {}) or {}
    if isinstance(windows, Mapping):
        for bucket in ("daily", "monday", "month1", "realtime"):
            declared = windows.get(bucket) or []
            if declared:
                return str(declared[0])
    return ""


def render_item(item: Any, record: Any, compare: Any = None, hotel: Any = None) -> str:
    """单 item 内容:**branch 模板优先 → 回退数据渲染**(旧 ``report_push.py:287-301``)。

    22 项当前都没配 ``branch``,所以这条路径等价于 :func:`render_data_block`;
    保留它是为了"违约有无"类文案将来能直接挂模板,而**不必改渲染代码**。
    """
    payload = payload_of(record)
    if item_field(item, "branch"):
        template = resolve_branch(item, payload, hotel)
        if template is not None:
            return template
    return render_data_block(item, record, compare, hotel)


def describe_compare(compare: Any) -> Sequence[str]:
    """诊断辅助:compare 里解析出来的字段名列表(CLI/日志用,不参与渲染)。"""
    cmap = compare_map(compare)
    return sorted(cmap) if cmap else []
