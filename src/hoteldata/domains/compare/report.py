"""比价报告渲染(段3 T3D.1 / V76 / V77)—— md(推送用)+ html(留档用)。

★ 逐字继承旧 ``comparator/report.py`` 的形状
===========================================

旧 ``report.py:22-77`` 的 markdown 结构(段3 原样保留):

* 标题 ``## 🏨 酒店比价报告``;
* 元信息块:锚点酒店 / 入住日期(含几晚)/ 城市 / 房型偏好;
* ``---`` 分隔;
* **按平台分组**输出,每组一个小标题 ``### 携程平台(ctrip)``;
* 每条 ``N. **酒店名**(距离)  **¥价** 备注``;
* 没有有效报价 → ``(本次未取到有效报价)``;
* 末尾 ``### ⚠️ 采集异常`` 列出平台级错误。

旧 ``report.py:80-138`` 的 HTML 结构也保留(同一配色 ``#ff6a00``、同一表格列)。

★ 段3 必须新增的三处标注(计划书 §2.2 的要求)
===========================================

1. **「距离不可用」** —— §5.5 明文:「若坐标确实取不到,``distance_km`` 为 ``None``,
   排序退化为平台顺序,并在报告里**显式标注"距离不可用"** —— 不许静默假装排过序」;
2. **「待人工确认」** —— V75:视觉价与 DOM 价偏差 >20% 的条目要标出来,不静默采用;
3. **「起价」字样** —— V73 修订:``price_scope="from"`` 的价格必须写清是**起价**,
   不能当成确定的房价展示。

★ 为什么报告要带**采集时间与 slot**
=================================

旧报告只有"入住日期"(``comparison_20260826_172618.md`` 全文 8 行),
**没有采集时刻** —— 于是同一份报告"是什么时候的价"无法回答。
段3 把 ``query_slot`` 与生成时间都写进报告头。
"""

from __future__ import annotations

import html as html_mod
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from hoteldata.domains.compare.contract import AnchorRef, HotelQuote

__all__ = [
    "PLATFORM_LABELS",
    "ReportBundle",
    "build_html",
    "build_markdown",
    "build_price_text",
    "fmt_price",
]

#: 平台显示名(旧 ``report.py:19``,逐字)
PLATFORM_LABELS: dict[str, str] = {"ctrip": "携程", "meituan": "美团"}

#: 「距离不可用」标注(§5.5 明文要求)
DISTANCE_UNAVAILABLE_NOTE = "> ⚠️ 距离不可用(未取到坐标或距离文本),以上按**平台推荐顺序**排列,不是按距离"
#: 「待人工确认」标注(V75)
MANUAL_CHECK_NOTE = "> ⚠️ 上表有**待人工确认**的条目(视觉价与 DOM 价偏差超过阈值),未静默采用"
#: 「起价」说明(V73 修订)
FROM_PRICE_NOTE = "> 💡 标「起」的价格是**列表页起价**(区间下界),不是该房型当晚的确定报价"


def fmt_price(value: float | None) -> str:
    """价格格式化(旧 ``report.py:13-16``:``None`` → ``—``,否则 ``¥{value:g}``)。"""
    if value is None:
        return "—"
    return f"¥{value:g}"


@dataclass(slots=True)
class ReportBundle:
    """一次比价的完整渲染产物。"""

    markdown: str
    html: str
    #: 写盘后的相对路径(由调用方 ``service``/``runner`` 填)
    md_path: str = ""
    html_path: str = ""
    #: 报告用到的元信息(便于 CLI 打印摘要)
    meta: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# markdown
# ---------------------------------------------------------------------------


def quote_platform(q: HotelQuote) -> str:
    """取这条报价属于哪个平台。

    ★ 平台信息挂在 ``raw["platform"]``(由 runner 的 ``_merge`` 写入),
      而不是 ``HotelQuote`` 的字段 —— 因为一条 :class:`HotelQuote` 描述的是
      「一家店在某平台的一次报价」,平台是**归属**而不是"报价的属性";
      段3 保持这个形状不变(改契约影响面大),统一从这里取。
    """
    return str((q.raw or {}).get("platform") or "").strip()


@dataclass(slots=True)
class CompareGroup:
    """**跨平台分组** —— 一家店一行,每个平台一个价。

    这是比价报告最该让人一眼看到的东西:同一家店在携程/美团**各是多少**。

    ★ 为什么要单独一个结构而不是在渲染函数里就地 groupby:
      三个渲染器(md / html / 推送文本)要用**同一份**分组结果 ——
      如果各写一遍,迟早出现"md 分了组、推送没分"的不一致
      (段3 已经因为"归一化规则写了两份"翻过一次车)。
    """

    hotel_name: str
    key: str
    distance_km: float | None = None
    #: 平台 → 该平台的报价
    by_platform: dict[str, HotelQuote] = field(default_factory=dict)

    @property
    def platforms(self) -> list[str]:
        return sorted(self.by_platform)

    @property
    def prices(self) -> dict[str, float | None]:
        return {p: q.price for p, q in self.by_platform.items()}

    @property
    def min_price(self) -> float | None:
        vals = [q.price for q in self.by_platform.values() if q.price is not None]
        return min(vals) if vals else None

    @property
    def max_price(self) -> float | None:
        vals = [q.price for q in self.by_platform.values() if q.price is not None]
        return max(vals) if vals else None

    @property
    def spread(self) -> float | None:
        """两平台价差(= 换平台能省/多花多少)。只有 >=2 个价格时才有意义。"""
        lo, hi = self.min_price, self.max_price
        if lo is None or hi is None or lo == hi:
            return None
        return round(hi - lo, 2)

    @property
    def cheaper(self) -> str | None:
        """最便宜的平台(用于标"哪边更划算")。价差为 0 或只有一家 → None。"""
        if self.spread is None:
            return None
        lo = self.min_price
        for p, q in self.by_platform.items():
            if q.price is not None and q.price == lo:
                return p
        return None

    @property
    def need_manual_check(self) -> bool:
        return any(q.need_manual_check for q in self.by_platform.values())

    @property
    def rejected(self) -> list[str]:
        return [r for q in self.by_platform.values() for r in (q.price_rejected or [])]


def group_by_hotel(quotes: list[HotelQuote]) -> list[CompareGroup]:
    """按**归一化酒店名**跨平台分组,保持输入顺序(输入已按距离排好)。

    归一化用 :func:`~hoteldata.domains.compare.human.norm_hotel_name`
    —— 与 runner 去重、平台内合并用的**同一个**函数
    (同一个规则散成两份,段3 已经因此翻过一次车)。
    """
    from hoteldata.domains.compare.human import norm_hotel_name

    groups: dict[str, CompareGroup] = {}
    for q in quotes:
        key = norm_hotel_name(q.hotel_name) or q.hotel_name
        g = groups.get(key)
        if g is None:
            g = CompareGroup(hotel_name=q.hotel_name, key=key, distance_km=q.distance_km)
            groups[key] = g
        plat = quote_platform(q) or "未知平台"
        # 同平台同名只留第一个(多报价格时取先到的 —— 输入已排序)
        g.by_platform.setdefault(plat, q)
        # 距离取**有值**的那个
        if g.distance_km is None and q.distance_km is not None:
            g.distance_km = q.distance_km
    return list(groups.values())


def _cell_text(q: HotelQuote | None, *, mark_cheapest: bool = False) -> str:
    """一个平台格子的文本(``¥304 起`` / ``—``)。"""
    if q is None or q.price is None:
        return "—"
    text = fmt_price(q.price)
    if q.price_scope == "from":
        text += " 起"
    if mark_cheapest:
        text += " ⭐"
    if q.need_manual_check:
        text += "⚠️"
    return text


def _cross_compare_table(groups: list[CompareGroup]) -> list[str]:
    """★ **跨平台对比表**:一家店一行,每个平台一列。

    这是"比价"这个功能最该让人一眼看到的东西 —— 而不是两段各自列出同名酒店。

    表格形状::

        | # | 酒店 | 距锚点 | 携程 | 美团 | 价差 |
        |---|------|-------|------|------|------|
        | 1 | 上青城度假酒店 | 0.10 km | ¥304 起 ⭐ | ¥368 起 | ¥64(携程省) |

    三条设计取舍:

    1. **列只放本次实际出现的平台**(不写死携程/美团两列)——
       只跑一个平台时留空列会让人以为"另一个平台没数据";
    2. **⭐ 标最便宜的那个平台**,价差列写"哪边省" ——
       但**价差为 0 或只有一家时不写**,避免"省 ¥0"这种噪音;
    3. 某平台没有这家店 → 格子写 ``—``(表示"该平台没有这家店",
       而不是"没取到价")。这两件事不同,所以 ``—`` 与 ``未取到`` 要区分。
    """
    plats: list[str] = []
    for g in groups:
        for p in g.platforms:
            if p not in plats:
                plats.append(p)
    if not plats:
        return []

    header = "| # | 酒店 | 距锚点 | " + " | ".join(
        PLATFORM_LABELS.get(p, p) for p in plats
    ) + " | 价差 |"
    sep = "|---|------|-------|" + "|".join("------" for _ in plats) + "|------|"

    lines = ["### 比价总览(一家一行)","", header, sep]
    for i, g in enumerate(groups, 1):
        dist = f"{g.distance_km:.2f} km" if g.distance_km is not None else "—"
        # 最便宜的平台(仅当 >=2 个价格时才有意义)
        cheapest = g.cheaper
        cells = [
            _cell_text(g.by_platform.get(p), mark_cheapest=(cheapest == p))
            for p in plats
        ]
        # 价差列
        spread = g.spread
        if spread is None:
            spread_txt = "—"
        else:
            who = PLATFORM_LABELS.get(cheapest or "", cheapest or "")
            spread_txt = f"¥{spread:g}({who}省)" if who else f"¥{spread:g}"
        lines.append(
            f"| {i} | {g.hotel_name} | {dist} | " + " | ".join(cells) + f" | {spread_txt} |"
        )

    # 只有一家平台有数据时说明一下(避免"为什么另一列全是 —")
    covered = {p for g in groups for p in g.platforms}
    if len(covered) == 1:
        only = PLATFORM_LABELS.get(next(iter(covered)), next(iter(covered)))
        lines.append("")
        lines.append(f"> ℹ️ 本次只有**{only}**一个平台的数据 —— 另一个平台的列留空(不是没取到价)。")
    return lines


def build_markdown(
    *,
    anchor_name: str,
    query_date: date | None,
    nights: int,
    city: str | None,
    quotes: list[HotelQuote],
    anchor: AnchorRef | None = None,
    slot: str = "",
    generated_at: datetime | None = None,
    notes: list[str] | None = None,
    errors: dict[str, str] | None = None,
) -> str:
    """渲染 markdown 报告。

    ★ **一家店一行,每个平台一列**(`_cross_compare_table`),这是比价报告的核心视图:
    同一家店在携程/美团各是多少、差多少,一眼可见。
    按平台分组的逐条列表放在它下面作为明细。
    """
    lines: list[str] = ["## 🏨 酒店比价报告"]
    lines.append(f"> 锚点酒店:{anchor_name}")
    if query_date:
        lines.append(f"> 入住日期:{query_date.isoformat()}({nights} 晚)")
    if city:
        lines.append(f"> 城市:{city}")
    if anchor is not None and anchor.self_price is not None:
        lines.append(
            f"> 本店价:{fmt_price(anchor.self_price)}"
            f"({PLATFORM_LABELS.get(anchor.self_price_platform or '', anchor.self_price_platform or '')})"
        )
    # ★ 段3 新增:采集时刻(旧报告没有"这是什么时候的价")
    when = generated_at or datetime.now()
    lines.append(f"> 采集时间:{when:%Y-%m-%d %H:%M:%S}" + (f"(slot {slot})" if slot else ""))
    lines.append("---")

    priced = [q for q in quotes if q.price is not None]
    if priced:
        groups = group_by_hotel(quotes)
        lines.extend(_cross_compare_table(groups))

        # ---- 明细:按平台分组(保留旧形状,便于对照原始平台顺序)----
        by_plat: dict[str, list[HotelQuote]] = {}
        order: list[str] = []
        for q in priced:
            plat = quote_platform(q) or "未知平台"
            if plat not in by_plat:
                by_plat[plat] = []
                order.append(plat)
            by_plat[plat].append(q)
        lines.append("")
        lines.append("### 各平台明细")
        for plat in order:
            label = PLATFORM_LABELS.get(plat, plat)
            lines.append("")
            lines.append(f"**{label}平台({plat})**")
            for i, q in enumerate(by_plat[plat], 1):
                lines.append(_md_line(i, q))
    else:
        lines.append("(本次未取到有效报价)")

    # ★ 三条必须有的标注
    if priced and any(q.distance_km is None for q in quotes):
        lines.append("")
        lines.append(DISTANCE_UNAVAILABLE_NOTE)
    if any(q.need_manual_check for q in quotes):
        lines.append("")
        lines.append(MANUAL_CHECK_NOTE)
    if any(q.price_scope == "from" and q.price is not None for q in quotes):
        lines.append("")
        lines.append(FROM_PRICE_NOTE)

    # 券价被拒的记录(§5.6:丢弃要可见)
    rejected = [r for q in quotes for r in (q.price_rejected or [])]
    if rejected:
        lines.append("")
        lines.append(f"> 🚫 已过滤 {len(rejected)} 条**券价候选**(不计入报价):" + "; ".join(rejected[:3]))

    if notes:
        lines.append("")
        lines.append("### 说明")
        for note in notes:
            lines.append(f"- {note}")

    failed = {k: v for k, v in (errors or {}).items() if v}
    if failed:
        lines.append("---")
        lines.append("### ⚠️ 采集异常")
        for k, err in failed.items():
            lines.append(f"- {PLATFORM_LABELS.get(k, k)}:{err}")
    return "\n".join(lines)


def _md_line(index: int, q: HotelQuote) -> str:
    """单条报价行(旧 ``report.py:51-64`` 的形状)。"""
    dist = f"{q.distance_km:.2f} km" if q.distance_km is not None else ""
    line = f"{index}. **{q.hotel_name}**"
    if dist:
        line += f"({dist})"
    price_text = fmt_price(q.price)
    if q.price_scope == "from" and q.price is not None:
        price_text += " 起"
    line += f"  **{price_text}**"
    tags: list[str] = []
    if q.need_manual_check:
        tags.append("待人工确认")
    if q.degraded:
        tags.append("降级")
    if q.price_source == "vision":
        tags.append("视觉")
    if tags:
        line += " ⚠️ " + "/".join(tags)
    return line


# ---------------------------------------------------------------------------
# html
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>酒店比价报告 · {anchor_name}</title>
<style>
body {{ font-family: 'Microsoft YaHei', sans-serif; margin: 32px; color: #222; }}
h1 {{ font-size: 22px; }}
h2 {{ font-size: 18px; margin-top: 24px; border-left: 4px solid #ff6a00; padding-left: 8px; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 8px; }}
th, td {{ border: 1px solid #ddd; padding: 8px 12px; text-align: left; }}
th {{ background: #f5f5f5; }}
.meta {{ color: #666; margin-bottom: 16px; }}
.note {{ color: #666; font-size: 12px; margin-top: 16px; }}
.warn {{ background: #fff8e1; border: 1px solid #ffe082; padding: 8px 12px; margin-top: 12px;
        color: #8d6e00; font-size: 13px; }}
.bad {{ color: #c62828; font-weight: 600; }}
.muted {{ color: #999; }}
.cheap {{ background: #e8f5e9; font-weight: 700; color: #1b5e20; }}
.spread {{ color: #c62828; font-weight: 600; }}
.nodist {{ color: #b26a00; }}
</style>
</head>
<body>
<h1>🏨 酒店比价报告</h1>
<div class="meta">
锚点:<strong>{anchor_esc}</strong> ｜
入住:{checkin}({nights} 晚){city_part}{self_part}<br>
采集时间:{generated_at}{slot_part}
</div>
{warn_blocks}
{cross_table}
<h2>各平台明细</h2>
<table>
<thead><tr>
<th>#</th><th>酒店</th><th>平台</th><th>距锚点</th><th>价格</th><th>口径</th><th>来源</th><th>备注</th>
</tr></thead>
<tbody>
{rows_html}
</tbody>
</table>
{rejected_html}
{notes_html}
{error_html}
<div class="note">生成时间:{generated_at} ｜ 段3 比价(数据来源:携程/美团前台实时采集)</div>
</body>
</html>
"""


def build_html(
    *,
    anchor_name: str,
    query_date: date | None,
    nights: int,
    city: str | None,
    quotes: list[HotelQuote],
    anchor: AnchorRef | None = None,
    slot: str = "",
    generated_at: datetime | None = None,
    notes: list[str] | None = None,
    errors: dict[str, str] | None = None,
) -> str:
    """渲染 HTML 报告(配色与列结构继承旧 ``report.py:108-138``,列有扩展)。"""
    when = generated_at or datetime.now()
    esc = html_mod.escape

    rows: list[str] = []
    for i, q in enumerate(quotes, 1):
        plat = str((q.raw or {}).get("platform") or "")
        label = PLATFORM_LABELS.get(plat, plat)
        if q.price is None:
            # ★ 无价格的条目**照样列出**(旧 report 直接跳过 → 于是"为什么少了一家店"不可见)
            rows.append(
                "<tr class='muted'>"
                f"<td>{i}</td><td>{esc(q.hotel_name)}</td>"
                f"<td>{esc(label)}</td><td>—</td>"
                "<td class='muted'>未取到</td><td>—</td><td>—</td>"
                f"<td>{esc(_remark(q))}</td></tr>"
            )
            continue
        dist = f"{q.distance_km:.2f} km" if q.distance_km is not None else "—"
        rows.append(
            "<tr>"
            f"<td>{i}</td>"
            f"<td>{esc(q.hotel_name)}</td>"
            f"<td>{esc(label)}</td>"
            f"<td>{dist}</td>"
            f"<td><strong>{esc(fmt_price(q.price))}</strong></td>"
            f"<td>{'起价' if q.price_scope == 'from' else '确定价'}</td>"
            f"<td>{esc(q.price_source)}</td>"
            f"<td>{esc(_remark(q))}</td>"
            "</tr>"
        )
    rows_html = "\n".join(rows) if rows else (
        "<tr><td colspan='8' style='text-align:center;color:#999'>本次未取到有效报价</td></tr>"
    )

    warn_blocks: list[str] = []
    if any(q.price is None for q in quotes):
        warn_blocks.append("<div class='warn'>部分酒店未取到价格(已列出但标为「未取到」,不是被丢弃)</div>")
    if any(q.distance_km is None for q in quotes):
        warn_blocks.append("<div class='warn'>距离不可用(未取到坐标或距离文本)—— 排序为平台推荐顺序,不是按距离</div>")
    if any(q.need_manual_check for q in quotes):
        warn_blocks.append("<div class='warn'>存在<b>待人工确认</b>条目:视觉价与 DOM 价偏差超过阈值,未静默采用</div>")
    if any(q.price_scope == "from" and q.price is not None for q in quotes):
        warn_blocks.append("<div class='warn'>标「起价」的是列表页起价(区间下界),不是确定报价</div>")

    rejected = [r for q in quotes for r in (q.price_rejected or [])]
    rejected_html = ""
    if rejected:
        items = "".join(f"<li>{esc(r)}</li>" for r in rejected[:20])
        rejected_html = f"<h2>已过滤的券价候选</h2><ul>{items}</ul>"

    notes_html = ""
    if notes:
        items = "".join(f"<li>{esc(n)}</li>" for n in notes)
        notes_html = f"<h2>说明</h2><ul>{items}</ul>"

    failed = {k: v for k, v in (errors or {}).items() if v}
    error_html = ""
    if failed:
        items = "".join(
            f"<li class='bad'>{esc(PLATFORM_LABELS.get(k, k))}：{esc(v)}</li>" for k, v in failed.items()
        )
        error_html = f"<h2>采集异常</h2><ul>{items}</ul>"

    self_part = ""
    if anchor is not None and anchor.self_price is not None:
        self_part = f" ｜ 本店价:{esc(fmt_price(anchor.self_price))}"

    return _HTML_TEMPLATE.format(
        anchor_name=esc(anchor_name),
        anchor_esc=esc(anchor_name),
        checkin=query_date.isoformat() if query_date else "—",
        nights=nights,
        city_part=f" ｜ 城市:{esc(city)}" if city else "",
        self_part=self_part,
        generated_at=f"{when:%Y-%m-%d %H:%M:%S}",
        slot_part=f"(slot {esc(slot)})" if slot else "",
        warn_blocks="\n".join(warn_blocks),
        cross_table=_html_cross_table(quotes),
        rows_html=rows_html,
        rejected_html=rejected_html,
        notes_html=notes_html,
        error_html=error_html,
    )


def _html_cross_table(quotes: list[HotelQuote]) -> str:
    """HTML 版**跨平台对比表**(与 markdown 版同一份分组结果)。

    ★ 复用 :func:`group_by_hotel` / :class:`CompareGroup` ——
      两个渲染器共用同一份分组逻辑,避免"md 分了组、html 没分"的不一致。
    """
    groups = group_by_hotel([q for q in quotes if q.price is not None])
    if not groups:
        return ""
    plats: list[str] = []
    for g in groups:
        for p in g.platforms:
            if p not in plats:
                plats.append(p)

    head = (
        "<th>#</th><th>酒店</th><th>距锚点</th>"
        + "".join(f"<th>{PLATFORM_LABELS.get(p, p)}</th>" for p in plats)
        + "<th>价差</th>"
    )
    body: list[str] = []
    for i, g in enumerate(groups, 1):
        if g.distance_km is not None:
            dist = f"{g.distance_km:.2f} km"
        else:
            dist = "<span class='nodist'>不可用</span>"
        cheapest = g.cheaper
        cells: list[str] = []
        for p in plats:
            q = g.by_platform.get(p)
            if q is None or q.price is None:
                # ★ ``—`` = 该平台没有这家店;``未取到`` = 有店但没拿到价
                cells.append("<td class='muted'>—</td>" if q is None else
                             "<td class='muted'>未取到</td>")
                continue
            text = f"¥{q.price:g}" + (" 起" if q.price_scope == "from" else "")
            cls = " class='cheap'" if cheapest == p else ""
            warn = " ⚠️" if q.need_manual_check else ""
            cells.append(f"<td{cls}>{text}{warn}</td>")
        spread = g.spread
        if spread is None:
            spread_html = "<td class='muted'>—</td>"
        else:
            who = PLATFORM_LABELS.get(cheapest or "", cheapest or "")
            spread_html = f"<td class='spread'>¥{spread:g}" + (f"({who}省)</td>" if who else "</td>")
        body.append(
            f"<tr><td>{i}</td><td>{html_mod.escape(g.hotel_name)}</td><td>{dist}</td>"
            + "".join(cells)
            + spread_html
            + "</tr>"
        )
    return (
        "<h2>比价总览(一家一行)</h2>\n<table><thead><tr>"
        + head
        + "</tr></thead><tbody>\n"
        + "\n".join(body)
        + "\n</tbody></table>"
    )


def _remark(q: HotelQuote) -> str:
    parts: list[str] = []
    if q.need_manual_check:
        parts.append("待人工确认")
    if q.degraded:
        parts.append("降级")
    if q.price_source == "vision":
        parts.append("视觉读价")
    if q.coord_source == "city":
        parts.append("距离按城市中心估算")
    if q.price_rejected:
        parts.append(f"已过滤 {len(q.price_rejected)} 条券价候选")
    if q.price is None:
        parts.append("该卡片未显示价格")
    return " / ".join(parts)


# ---------------------------------------------------------------------------
# 推送文本段(日报合并 + 独立推送共用)
# ---------------------------------------------------------------------------


def build_price_text(
    *,
    anchor_name: str,
    quotes: list[HotelQuote],
    query_date: date | None = None,
    slot: str = "",
    quote_count: int = 3,
    distance_available: bool | None = None,
) -> str:
    """单店比价**纯文字段**(日报合并 + 14:00/18:00 独立推送共用)。

    形状逐字继承旧 ``app/price_push.py:97-123``::

        📊 **隐欲民宿 比价**(2026-08-27)
        **携程**:莱州锦禾轻奢民宿(0.9 km)¥103 起、...
        **美团**:...
        > 数据来源:携程/美团实时采集

    三种兜底(旧 ``price_push.py:105-114``,**逐字保留**):

    * 无任何记录 → 「暂无比价记录」;
    * 有记录但无有效报价 → 「暂无有效报价(采集失败或演示数据)」;
    * 有报价 → 正常段。

    ★ 段3 新增:``query_slot`` 写进标题行(否则"这是几点采的"无法回答)、
      距离不可用时在脚注里说明。
    """
    header = f"📊 **{anchor_name} 比价**"
    if query_date:
        header += f"({query_date.isoformat()}"
        if slot:
            # slot 形如 2026-08-27-1730 → 取后 4 位显示成 17:30
            tail = slot.rsplit("-", 1)[-1]
            if len(tail) == 4 and tail.isdigit():
                header += f" {tail[:2]}:{tail[2:]}"
        header += ")"

    priced = [q for q in quotes if q.price is not None]
    if not quotes:
        return f"「{anchor_name}」暂无比价记录(请先运行 hoteldata compare-batch 或等待定时任务)"
    if not priced:
        return f"「{anchor_name}」比价暂无有效报价(采集失败或仅演示数据)"

    lines = [header]
    groups = group_by_hotel(priced)

    # ★ **一家店一行**(比价的核心视图)。旧系统与段3 第一版都是"按平台列出",
    #   于是同一家店在群里出现两次、还要人自己对着看差多少。
    #
    #   企微 markdown **不支持表格**,所以这里用紧凑的逐行形式:
    #       `1. 上青城度假酒店 携程 ¥304起 / 美团 ¥368起 · 携程省 ¥64`
    used_plats: list[str] = []
    for g in groups:
        for p in g.platforms:
            if p not in used_plats:
                used_plats.append(p)

    shown = 0
    for g in groups:
        if shown >= max(1, quote_count):
            break
        parts: list[str] = []
        for p in used_plats:
            q = g.by_platform.get(p)
            if q is None or q.price is None:
                continue
            label = PLATFORM_LABELS.get(p, p)
            text = f"{label} {fmt_price(q.price)}"
            if q.price_scope == "from":
                text += "起"
            if g.cheaper == p:
                text += "⭐"
            parts.append(text)
        if not parts:
            continue
        shown += 1
        line = f"{shown}. {_short_name(g.hotel_name)}"
        if g.distance_km is not None:
            line += f"({g.distance_km:.1f}km)"
        line += " " + " / ".join(parts)
        spread = g.spread
        if spread is not None:
            who = PLATFORM_LABELS.get(g.cheaper or "", g.cheaper or "")
            line += f" · {who}省 ¥{spread:g}"
        lines.append(line)

    used = "/".join(PLATFORM_LABELS.get(p, p) for p in used_plats)
    lines.append(f"> 数据来源:{used}实时采集" + ("(起价)" if any(q.price_scope == "from" for q in priced) else ""))
    if len(used_plats) >= 2:
        lines.append("> ⭐ = 该店两平台里更便宜的一边")

    if distance_available is False:
        lines.append("> ⚠️ 距离不可用,已按平台推荐顺序")
    if any(q.need_manual_check for q in priced):
        lines.append("> ⚠️ 含**待人工确认**条目(视觉与 DOM 偏差过大)")
    return "\n".join(lines)


#: 推送里酒店名的显示上限(企微消息有长度限制,且一行太长在手机上看不清)
_PUSH_NAME_MAX = 18


def _short_name(name: str) -> str:
    """推送用的酒店短名:去掉括号里的门店后缀,太长则截断。

    ★ 为什么推送要短名而报告用全名:报告是留档/电脑上看,全名便于对账;
      推送是**手机上扫一眼**,「山屿·漫时光·繁花一宿·Floral Atelier無璞·轻奢度假民宿
      (青城山高铁站店)」这种名字会把两个平台价挤到看不见。
    """
    text = re.sub(r"[（(【\[].*?[）)】\]]", "", str(name or "")).strip(" ·-—")
    if len(text) <= _PUSH_NAME_MAX:
        return text
    return text[: _PUSH_NAME_MAX - 1] + "…"


def _push_line(q: HotelQuote) -> str:
    text = q.hotel_name
    if q.distance_km is not None:
        text += f"({q.distance_km:.1f}km)"
    text += f" {fmt_price(q.price)}"
    if q.price_scope == "from":
        text += "起"
    if q.need_manual_check:
        text += "⚠️"
    return text


def build_group_price_text(
    *,
    sections: list[str],
    hotel_count: int,
) -> str:
    """★ 一群多店:逐店段拼接,首行加汇总标题。

    逐字继承旧 ``app/price_push.py:126-132`` 的形状::

        📊 **比价汇总**(2 家酒店)
        <店1 段>

        <店2 段>

    ★ 这就是段3 P7 的答案:段2 的 ``daily_builder(chatid, price_section)``
      只接受**一个群级字符串**,而这个函数把 N 家店的段拼成那一个字符串 ——
      **段2 代码一行不用改**。
    """
    if not sections:
        return ""
    header = f"📊 **比价汇总**({hotel_count} 家酒店)"
    return header + "\n\n" + "\n\n".join(sections)
