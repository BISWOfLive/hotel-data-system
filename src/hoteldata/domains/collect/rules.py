"""规则引擎 —— ``config/api_rules.json`` 加载 / 强校验 / 热加载(T3.1)。

对应旧系统 ``collectors/rules.py``(手写约 250 行 Schema 校验)。

**三条必须保住的性质**

1. **规则驱动,改配置不改代码**:新增一个模块只需在 ``api_rules.json`` 加一个
   ``SubModule`` 节点(+ 条件性的 ``ApiDef`` / 排期 / 轮换项),**Python 代码零改动**。
   这是旧系统最成功的设计。
2. **热加载**:按文件 mtime 缓存,改规则**不需重启**。
3. **强校验 + 路径定位**:不合法直接抛错,并给出形如
   ``pages.经营报告.sub_modules[3].windows[1]`` 的**精确路径**。

**★ 补齐旧系统缺失的校验(D12)**:旧系统只校验 ``screenshot_modules`` 的
``selector`` / ``name``,扩展键 ``clicks`` / ``tabs`` / ``click`` / ``skip_click_on`` /
``require_text`` **拼错即静默失效**。新实现用 ``extra="forbid"`` 让拼错**直接报错**。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from hoteldata.domains.collect.windows import (
    DEFAULT_WINDOW,
    WINDOW_KEYS,
    normalize_window,
)

__all__ = [
    "ApiDefCfg",
    "ApiMatchCfg",
    "ApiRules",
    "ApiRulesError",
    "FieldCfg",
    "PageCfg",
    "RulesLoader",
    "ScreenshotModuleCfg",
    "SubModuleCfg",
    "get_api_rules",
    "match_resolves",
    "reset_cache",
]

# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class ApiRulesError(ValueError):
    """``api_rules.json`` 结构非法(消息带**精确路径定位**)。"""


# ---------------------------------------------------------------------------
# 校验用的受限模型(extra="forbid" —— 拼错即报错)
# ---------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FieldCfg(_Strict):
    """``apis[].fields[]`` —— 一条字段提取规则。

    ``path`` 的**双语义**(靠前缀区分,见旧系统 §6.4):

      * **API 通道**:JSON 点号路径,如 ``data.[0].tip1``;
      * **DOM 通道**:``css:`` / ``js:`` / ``text:`` / ``table:`` 前缀。

    ⚠️ 当前 ``api_rules.json`` 里 **485 条 path 全是 JSON 点号路径**(无 DOM 前缀)。
    旧系统的浏览器兜底把无前缀 path 当**裸关键词**走 ``text:`` 正则 → 必然提取为空
    → 被标成 ``no_data``「明确无数据」,而真实原因是「DOM 路径未校准」。
    新实现把这个行为显式化(见 :meth:`SubModuleCfg.dom_calibrated`),
    **不再产出假 ``no_data``**。
    """

    path: str = Field(min_length=1)
    label: str = Field(min_length=1)


class ApiMatchCfg(_Strict):
    """``apis[]`` —— 一个接口的匹配 + 字段集。"""

    match: str = Field(min_length=1)
    fields: list[FieldCfg] = Field(default_factory=list)
    note: str | None = None


class ScreenshotModuleCfg(_Strict):
    """``screenshot_modules[]`` —— 一个截图目标 + **交互资产**(B6 遗产)。

    ==================  ==========  ==================================================
    键                   类型        语义
    ==================  ==========  ==================================================
    ``selector``         ``str``     纯 CSS 选择器(全库仅 2 处 XPath)
    ``name``             ``str``     ★ 落 ``module_screenshots_json`` 的**键名**
    ``clicks``           ``list``    多步点击,依次点击这些文本;**第一步前 sleep 6s,成功步间 sleep 8s**
    ``tabs``             ``str``     页签容器选择器(`state="attached"`,15s)
    ``click``            ``str``     在 ``tabs`` 容器内点击的文本(点击前 sleep 6s)
    ``skip_click_on``    ``list``    取值 ``["monday","month1"]``;``weekday()==0`` / ``day==1`` 时跳过
    ``require_text``     ``str``     页面无此文本 → **快跳**(不报错)
    ==================  ==========  ==================================================
    """

    selector: str = Field(min_length=1)
    name: str = Field(min_length=1)
    clicks: list[str] | None = None
    tabs: str | None = None
    click: str | None = None
    skip_click_on: list[str] | None = None
    require_text: str | None = None

    @field_validator("skip_click_on")
    @classmethod
    def _check_skip(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        allowed = {"monday", "month1"}
        bad = [x for x in v if x not in allowed]
        if bad:
            raise ValueError(
                f"skip_click_on 只接受 {sorted(allowed)},非法值 {bad}(旧系统拼错会静默失效,新实现直接报错)"
            )
        return v

    @model_validator(mode="after")
    def _check_click_pair(self) -> ScreenshotModuleCfg:
        if self.click and not self.tabs:
            raise ValueError("配置了 click 但缺少 tabs(click 必须在 tabs 容器内点击)")
        return self


class SubModuleCfg(_Strict):
    """``sub_modules[]`` —— 规则驱动的最小采集单位(全库 24 条)。"""

    name: str = Field(min_length=1)
    nav: str = ""
    url: str = Field(min_length=1)
    apis: list[ApiMatchCfg] = Field(default_factory=list)
    windows: list[str] = Field(default_factory=list)
    screenshot_modules: list[ScreenshotModuleCfg] = Field(default_factory=list)
    fixed_daily: bool | None = None
    note: str | None = None
    #: 「明确无数据」的声明式路径(字符串或字符串列表)。
    #: 命中(路径存在且值是 None/""/[]/{})→ 该模块落 ``no_data`` 而**不算失败**。
    #: ⚠️ 当前 24 个模块**均未使用**(旧系统同样 0 使用),保留支持是为了新模块可声明。
    no_data_path: str | list[str] | None = None

    @field_validator("windows")
    @classmethod
    def _check_windows(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for i, w in enumerate(v):
            if w not in WINDOW_KEYS:
                # ★ 消息里带上**下标**:字段级校验器的错误位置只到 ``...windows``,
                #   不带下标。而计划书要求的定位形态是
                #   ``pages.经营报告.sub_modules[3].windows[1] 非法窗口`` ——
                #   下标必须由消息自己补出来,否则 9 个窗口里错哪一个看不出来。
                raise ValueError(
                    f"windows[{i}] 非法窗口 {w!r};合法值为 9 个窗口名 "
                    f"{sorted({k for k in WINDOW_KEYS if not k.isascii()})} 或其内部键"
                )
            cn = normalize_window(w)
            if cn not in out:
                out.append(cn)
        return out

    @model_validator(mode="after")
    def _default_window(self) -> SubModuleCfg:
        # 无 windows → 默认「昨日」(旧口径)
        if not self.windows:
            object.__setattr__(self, "windows", [DEFAULT_WINDOW])
        return self

    # ---- 便捷视图 ----

    @property
    def dom_calibrated(self) -> bool:
        """★ 该模块是否提供了 **DOM 可提取**的字段路径。

        ``path`` 以 ``css:`` / ``js:`` / ``text:`` / ``table:`` 开头才算 DOM 已校准。
        全部是 JSON 点号路径 → ``False`` → 浏览器兜底**不得**报 ``no_data``。
        """
        for entry in self.apis:
            for fld in entry.fields:
                low = fld.path.lstrip().lower()
                if low.startswith(("css:", "js:", "text:", "table:")):
                    return True
        return False

    def api_names(self) -> list[str]:
        return [a.match for a in self.apis]

    def labels(self) -> list[str]:
        return [f.label for a in self.apis for f in a.fields]

    def screenshot_names(self) -> list[str]:
        return [s.name for s in self.screenshot_modules]


class ApiDefCfg(_Strict):
    """``api_defs[]`` —— 可重放的内部 API 描述(全库 103 条)。

    ``body`` 含 ``clientId`` / ``fp`` / ``vid`` / 514 字 ``rmsToken`` 等**会话级指纹**
    —— 实测捕获值,**不可推导**(A3 遗产),原样继承。
    """

    name: str = Field(min_length=1)
    method: str
    url: str = Field(min_length=1)
    params: dict[str, Any] | None = None
    body: dict[str, Any] | str | None = None
    need_record: bool = False
    scope: str | None = None
    body_by_window: dict[str, dict[str, Any]] | None = None

    @field_validator("method")
    @classmethod
    def _check_method(cls, v: str) -> str:
        upper = (v or "").strip().upper()
        if upper not in {"GET", "POST"}:
            raise ValueError(f"method 必须是 GET 或 POST,实际为 {v!r}")
        return upper

    @model_validator(mode="after")
    def _check_body_window_keys(self) -> ApiDefCfg:
        if self.body_by_window:
            for key in self.body_by_window:
                if key not in WINDOW_KEYS:
                    raise ValueError(f"body_by_window 的键必须是窗口中文名,非法键 {key!r}")
        return self

    @property
    def is_form(self) -> bool:
        """POST + ``body`` 是**非空字符串** → ``x-www-form-urlencoded``。"""
        return self.method == "POST" and isinstance(self.body, str) and bool(self.body)

    @property
    def content_type(self) -> str | None:
        if self.method != "POST":
            return None  # ★ GET 不带 content-type
        return "application/x-www-form-urlencoded; charset=UTF-8" if self.is_form else "application/json"


class PageCfg(_Strict):
    """``pages`` —— 一个采集域页(全库 8 个)。"""

    url: str = Field(min_length=1)
    core_apis: list[str] = Field(default_factory=list)
    modules: dict[str, Any] = Field(default_factory=dict)
    min_indicators: int = 0
    api_defs: list[ApiDefCfg] = Field(default_factory=list)
    sub_modules: list[SubModuleCfg] = Field(default_factory=list)
    screenshot_modules: list[ScreenshotModuleCfg] = Field(default_factory=list)
    scope: str | None = None
    multi_store_param: str | None = None

    @field_validator("min_indicators")
    @classmethod
    def _check_min(cls, v: Any) -> int:
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise ValueError(f"min_indicators 必须是非负整数,实际为 {v!r}")
        return v

    def api_def(self, name: str) -> ApiDefCfg | None:
        for d in self.api_defs:
            if d.name == name:
                return d
        return None

    def sub_module(self, name: str) -> SubModuleCfg | None:
        for m in self.sub_modules:
            if m.name == name:
                return m
        return None


class _RulesRoot(_Strict):
    """``api_rules.json`` 顶层。

    ⚠️ **不能用 ``_comment`` 当字段名**:pydantic v2 把下划线开头的名字当
    **私有属性**而不是模型字段,于是 ``extra="forbid"`` 会把 JSON 里真实存在的
    ``_comment`` 键判成"多余输入"直接报错 —— **一个纯注释键能把整个规则加载炸掉**。
    正确做法是普通字段名 + **别名**(``alias="_comment"``)。
    """

    pages: dict[str, PageCfg]
    # 旧文件里的说明串,原样容忍(不是规则,是注释)
    comment: Any = Field(default=None, alias="_comment")
    screenshot_comment: Any = Field(default=None, alias="_screenshot_comment")

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


# ---------------------------------------------------------------------------
# 路径定位
# ---------------------------------------------------------------------------


def _format_loc(loc: tuple[Any, ...]) -> str:
    """``('pages','经营报告','sub_modules',3,'windows',1)`` → ``pages.经营报告.sub_modules[3].windows[1]``。"""
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += ("." if out else "") + str(part)
    return out


def _format_validation_error(exc: ValidationError, source: Path) -> ApiRulesError:
    lines = [f"api_rules 校验失败: {source}"]
    for err in exc.errors():
        loc = _format_loc(tuple(err.get("loc") or ()))
        msg = err.get("msg", "")
        lines.append(f"  - {loc}: {msg}")
    return ApiRulesError("\n".join(lines))


# ---------------------------------------------------------------------------
# 规则集
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ApiRules:
    """已校验的规则集(不可变)。"""

    path: Path
    mtime: float
    pages: dict[str, PageCfg]
    #: **软问题**(不阻断加载,但要在 ``rules check`` 里显示)
    warnings: tuple[str, ...] = ()

    # ---- 页 ----
    @property
    def page_names(self) -> list[str]:
        return list(self.pages)

    def page(self, name: str) -> PageCfg:
        cfg = self.pages.get(name)
        if cfg is None:
            raise ApiRulesError(f"未知采集页 {name!r};已知页:{list(self.pages)}")
        return cfg

    # ---- 子模块 ----
    def sub_module(self, page: str, module: str) -> SubModuleCfg:
        cfg = self.page(page)
        sub = cfg.sub_module(module)
        if sub is None:
            raise ApiRulesError(f"页 {page!r} 无子模块 {module!r};已知:{[m.name for m in cfg.sub_modules]}")
        return sub

    def find_sub_module(self, page: str, module: str) -> SubModuleCfg | None:
        cfg = self.pages.get(page)
        return cfg.sub_module(module) if cfg else None

    def known_sub_module_names(self) -> list[str]:
        return [m.name for cfg in self.pages.values() for m in cfg.sub_modules]

    def all_sub_modules(self) -> list[tuple[str, SubModuleCfg]]:
        return [(pname, m) for pname, cfg in self.pages.items() for m in cfg.sub_modules]

    def locate_module(self, module: str) -> tuple[str, SubModuleCfg] | None:
        """按模块名（跨页）定位 —— 轮换清单/CLI 需要。"""
        for pname, cfg in self.pages.items():
            sub = cfg.sub_module(module)
            if sub is not None:
                return pname, sub
        return None

    # ---- 轮换一致性校验(T3.6 的坑修复) ----

    def classify_rotation_name(self, name: str, page: str, item_type: str) -> str:
        """判定轮换清单一项属于哪一类。

        返回 ``"module"`` / ``"page"`` / ``"alert"``;无法归类则抛 :class:`ApiRulesError`。

        旧系统里「市场分析」「预警-热点日历」两项**不在** ``sub_modules[*].name`` 中,
        被静默判 ``unknown`` 跳过 —— 真实原因是清单混装了三种语义,
        旧实现却只按"子模块名"一套规则去查:

          * ``module_screenshot`` 项的 ``name`` = **子模块名**;
          * ``fullpage`` 项(``市场分析``)的 ``name`` = **页名**;
          * ``预警*`` 项走**预警三源采集**(``scheduler.py`` 前缀特判),不是模块采集。
        """
        if name.startswith("预警"):
            return "alert"
        if self.locate_module(name) is not None:
            return "module"
        if item_type == "fullpage" and (name in self.pages or page in self.pages):
            return "page"
        raise ApiRulesError(
            "\n".join(
                [
                    f"轮换清单项 {name!r}(page={page!r}, type={item_type!r})在 api_rules 中不存在:",
                    "  · type=module_screenshot → name 必须是 sub_modules[*].name"
                    f"(已知 {len(self.known_sub_module_names())} 个)",
                    f"  · type=fullpage → name 必须是页名或子模块名(已知页 {list(self.pages)})",
                    "  · 名称以「预警」开头 → 归预警三源采集,不算模块",
                    "旧系统对这种情况**静默跳过**,新实现直接报错(T3.6)",
                ]
            )
        )

    # ---- 统计 ----

    def stats(self) -> dict[str, int]:
        subs = self.all_sub_modules()
        defs = [d for cfg in self.pages.values() for d in cfg.api_defs]
        shots = [s for _, m in subs for s in m.screenshot_modules] + [
            s for cfg in self.pages.values() for s in cfg.screenshot_modules
        ]
        ext = {
            k: sum(1 for s in shots if getattr(s, k, None))
            for k in ("clicks", "tabs", "click", "skip_click_on", "require_text")
        }
        return {
            "pages": len(self.pages),
            "sub_modules": len(subs),
            "api_defs": len(defs),
            "apis": sum(len(m.apis) for _, m in subs),
            "fields": sum(len(a.fields) for _, m in subs for a in m.apis),
            "screenshot_modules": len(shots),
            **{f"ext_{k}": v for k, v in ext.items()},
        }


# ---------------------------------------------------------------------------
# 加载 + 热加载
# ---------------------------------------------------------------------------

_DEFAULT_FILENAME = "api_rules.json"


class RulesLoader:
    """按 **mtime** 缓存的热加载器(改规则不需重启)。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._cached: ApiRules | None = None

    def load(self, *, force: bool = False) -> ApiRules:
        if not self.path.exists():
            raise ApiRulesError(f"规则文件不存在: {self.path}")
        mtime = self.path.stat().st_mtime
        if not force and self._cached is not None and self._cached.mtime == mtime:
            return self._cached
        rules = self._parse(mtime)
        self._cached = rules
        return rules

    def _parse(self, mtime: float) -> ApiRules:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ApiRulesError(
                f"api_rules 不是合法 JSON: {self.path}:{exc.lineno}:{exc.colno} — {exc.msg}"
            ) from exc
        if not isinstance(raw, dict) or "pages" not in raw:
            keys = list(raw)[:8] if isinstance(raw, dict) else type(raw).__name__
            raise ApiRulesError(f"api_rules 顶层必须含 pages 对象,实际键为 {keys}")
        try:
            root = _RulesRoot.model_validate(raw)
        except ValidationError as exc:
            raise _format_validation_error(exc, self.path) from exc
        if not root.pages:
            raise ApiRulesError(f"api_rules.pages 为空: {self.path}")

        # ---- 跨字段校验(单字段模型无法表达) ----
        problems: list[str] = []
        warnings: list[str] = []
        for pname, page in root.pages.items():
            seen_modules: set[str] = set()
            for mi, sub in enumerate(page.sub_modules):
                base = f"pages.{pname}.sub_modules[{mi}]"
                if sub.name in seen_modules:
                    # 旧系统用中文模块名做 dict key,重名即静默覆盖
                    problems.append(f"{base}.name: 模块名重复 {sub.name!r}(会静默覆盖)")
                seen_modules.add(sub.name)
                if not sub.apis:
                    # 不是硬错误:纯截图模块可以没有 API 字段(浏览器通道仍能出图)
                    warnings.append(f"{base}.apis: 为空,该模块的 API 通道采不到字段")
                for ai, entry in enumerate(sub.apis):
                    abase = f"{base}.apis[{ai}]"
                    if not match_resolves(entry.match, page.api_defs):
                        problems.append(
                            f"{abase}.match: {entry.match!r} 在该页 api_defs 中既不是定义名、"
                            f"也不是任何定义的 URL 片段(该页有 {len(page.api_defs)} 条定义)"
                        )
                    if not entry.fields:
                        # 实测有 3 条 apis 的 fields 为空(合法:仅触发调用/字段在别处)
                        warnings.append(f"{abase}.fields: 为空,接口 {entry.match!r} 不提取任何字段")
                    for fi, fld in enumerate(entry.fields):
                        if not fld.path.strip():
                            problems.append(f"{abase}.fields[{fi}].path: 不能为空白")
                # 截图模块键名唯一(落 module_screenshots_json 的键)
                shot_names = [s.name for s in sub.screenshot_modules]
                dup = {n for n in shot_names if shot_names.count(n) > 1}
                if dup:
                    problems.append(f"{base}.screenshot_modules: 名称重复 {sorted(dup)}")
                # ★ 扩展键拼错会被 extra="forbid" 拦下;这里额外提示"配了但用不上"
                if sub.screenshot_modules and not page.url and not sub.url:
                    warnings.append(f"{base}: 无 url,截图无法导航")
            # api_defs 名称唯一
            names = [d.name for d in page.api_defs]
            dupd = {n for n in names if names.count(n) > 1}
            if dupd:
                problems.append(f"pages.{pname}.api_defs: 定义名重复 {sorted(dupd)}")
            if not page.sub_modules:
                problems.append(f"pages.{pname}.sub_modules: 为空")
        if problems:
            raise ApiRulesError("api_rules 交叉校验失败:\n" + "\n".join(f"  - {p}" for p in problems))
        return ApiRules(path=self.path, mtime=mtime, pages=root.pages, warnings=tuple(warnings))


def match_resolves(match: str, defs: list[ApiDefCfg]) -> bool:
    """``apis[].match`` 的解析规则(**实测得出,不是猜的**)。

    ``match`` 有两种写法,都必须支持:

      1. **定义名**(大小写不敏感,双向包含)—— 97 条里绝大多数;
      2. **URL 片段** —— 例如 ``toolcenter/api/cpc/queryCampaignReportList``
         这种直接写路径的(URL 里含它即命中)。

    > 只按"定义名"一套规则查会误报:实测 97 条 match 里有多条是 URL 片段写法,
    > 用名字查会全部判成"不存在"。
    """
    m = (match or "").lower()
    if not m:
        return False
    for d in defs:
        n = d.name.lower()
        if m == n or m in n or n in m:
            return True
        if m in (d.url or "").lower():
            return True
    return False


# ---------------------------------------------------------------------------
# 进程内单例
# ---------------------------------------------------------------------------

_loader: RulesLoader | None = None


def _default_path() -> Path:
    from hoteldata.settings import get_settings

    return get_settings().paths.config_dir / _DEFAULT_FILENAME


def get_api_rules(*, force: bool = False) -> ApiRules:
    """取规则(按 mtime 热加载)。"""
    global _loader
    if _loader is None:
        _loader = RulesLoader(_default_path())
    return _loader.load(force=force)


def reset_cache() -> None:
    """清空缓存(测试用)。"""
    global _loader
    _loader = None


# ---------------------------------------------------------------------------
# 辅助:页 URL(referer 用)
# ---------------------------------------------------------------------------


def page_url(page: PageCfg, *, hotel_id: str | None = None, is_multi: bool = False) -> str:
    """页面 URL;多店账号 + 门店 id → 追加门店参数(**用作 referer**)。

    已存在同名参数则不重复追加(旧 ``_append_query`` 语义)。
    """
    url = page.url
    if not (is_multi and hotel_id):
        return url
    key = page.multi_store_param or "hotelId"
    import re as _re

    if _re.search(rf"(?:^|[?&]){_re.escape(key)}=", url):
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{key}={hotel_id}"


# ---------------------------------------------------------------------------
# 排期裁剪辅助
# ---------------------------------------------------------------------------


def modules_for_date(rules: ApiRules, day: date) -> list[str]:
    """当日 ``fixed_daily=True`` 的模块名(不进轮换,每日固定采)。"""
    out: list[str] = []
    for _, sub in rules.all_sub_modules():
        if sub.fixed_daily:
            out.append(sub.name)
    return out


__all__ += ["Literal", "modules_for_date", "page_url"]
