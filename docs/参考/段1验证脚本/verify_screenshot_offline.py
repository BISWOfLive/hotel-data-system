"""批次 E 截图器 —— **离线**单元验证(伪造 page/locator,不启动浏览器、不连数据库)。

用法(项目根)::

    .venv\\Scripts\\python.exe _scratch\\verify_screenshot_offline.py

验证清单(每条都打印**实际输入 → 实际输出**):
  1. ``_wait_content`` 判定式:60 字 + 2 数字 / canvas 情形 / 边界(59 字、1 个数字);
  2. ★ ``_wait_content`` 的 spinner 命中分支**不更新 last_text**;
  3. ``_wait_charts_ready`` 与 ``_wait_content`` 的**差别**(前者无 60 字判定、
     上限 8s,后者 20s);
  4. ``should_skip_click`` 的 ``weekday()==0`` / ``day==1`` 判定;
  5. ``require_text`` 未命中 → **快跳**(不等 30s、不报错、不写 module_out);
  6. ``_pick_visible_container`` 的 ``min(count,50)`` + 首个可见;
  7. ``_click_first_visible`` 的 ``min(count,10)``;
  8. 元素截图 + 超限告警 + ``module_out`` 装配;
  9. ★ ``run()`` 的落库回填顺序:``ensure_report`` → 截图 → ``link_screenshot(
     screenshot_path=None, module_screenshots=module_out)`` + 四态;
 10. ``screenshot_demand_modules`` 接真实 ``push_rotation.json`` + ``api_rules.json``。

输出同时写入 ``_scratch/screenshot_offline_out.txt``(UTF-8,避免 Windows 控制台 GBK)。
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hoteldata.domains.collect.contract import AccountRef, ExtractContext, HotelRef  # noqa: E402
from hoteldata.domains.collect.rules import ApiRules, ScreenshotModuleCfg  # noqa: E402
from hoteldata.domains.collect.screenshot import (  # noqa: E402
    CHART_READY_WAIT_S,
    CONTENT_WAIT_S,
    Screenshoter,
    aggregate_status,
    dedupe_targets,
    screenshot_demand_modules,
    should_skip_click,
)
from hoteldata.domains.collect.screenshot import (  # noqa: E402
    _click_first_visible,
    _pick_visible_container,
    _wait_charts_ready,
    _wait_content,
)
from hoteldata.infra.paths import Layout  # noqa: E402

OUT_PATH = Path(__file__).resolve().parent / "screenshot_offline_out.txt"
FAILURES: list[str] = []


def load_rules() -> ApiRules:
    """取真实规则(**只在离线自检里**绕过 rules.py 的两个已知阻断缺陷)。

    ★ 缺陷 1(阻断 ``get_api_rules()``):``rules.py:283-287`` 的 ``_RulesRoot`` 把
      ``_comment`` / ``_screenshot_comment`` 写成下划线开头的名字,pydantic v2 视其为
      **私有属性**(不是字段),而该模型 ``extra="forbid"`` → 真实
      ``config/api_rules.json`` 的这两个顶层注释键被直接拒绝。

    ★ 缺陷 2(阻断 ``RulesLoader._parse`` 的交叉校验):真实 ``api_rules.json`` 里有
      6 处 ``apis[].match`` 不在同页 ``api_defs[*].name`` 中 / ``fields`` 为空,
      校验直接抛错 —— 即当前配置 + 当前校验器下 ``get_api_rules()`` **必然失败**。

    两条都**不属于批次 E 的修复范围**(已上报),本脚本用"剥掉下划线键 + 只跑单字段
    校验"的方式拿到可用的规则对象;``screenshot.py`` 自身**没有任何绕过**。
    """
    import json

    from hoteldata.domains.collect import rules as rules_mod

    try:
        return rules_mod.get_api_rules(force=True)
    except rules_mod.ApiRulesError as exc:
        first = str(exc).splitlines()[0]
        print("[WARN] get_api_rules() 失败(rules.py 已知缺陷),改用只跑单字段校验的副本:")
        print(f"       {first}")
    raw = json.loads((ROOT / "config" / "api_rules.json").read_text(encoding="utf-8"))
    for key in [k for k in raw if k.startswith("_")]:
        raw.pop(key)
    root = rules_mod._RulesRoot.model_validate(raw)
    return rules_mod.ApiRules(path=ROOT / "config" / "api_rules.json", mtime=0.0, pages=root.pages)


class _Tee:
    """把 print 写进 UTF-8 文件(控制台只留 ASCII 进度行)。"""

    def __init__(self, path: Path) -> None:
        self.file = path.open("w", encoding="utf-8")

    def write(self, text: str) -> int:
        self.file.write(text)
        return len(text)

    def flush(self) -> None:
        self.file.flush()


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {label}" + (f"  |  {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


# ---------------------------------------------------------------------------
# 伪造对象
# ---------------------------------------------------------------------------


@dataclass
class FakeSettings:
    """只带截图器真正读到的字段(duck typing,避免连 .env/DB)。"""

    screenshot: Any = field(
        default_factory=lambda: SimpleNamespace(
            jpeg_quality=70, max_bytes=100, enabled=True, time="05:30"
        )
    )
    browser: Any = field(
        default_factory=lambda: SimpleNamespace(
            nav_timeout_s=60.0, chart_ready_wait_s=8.0, page_ready_timeout_s=120.0, headless=True
        )
    )


class _CountView:
    """``locator(sel)`` 返回的计数视图(spinner / canvas)。"""

    def __init__(self, owner: ScriptedLocator, kind: str) -> None:
        self.owner = owner
        self.kind = kind

    async def count(self) -> int:
        if self.kind == "spinner":
            # 每轮循环**先**问 spinner → 用它推进轮次
            self.owner.rounds_seen += 1
            self.owner.index = min(self.owner.rounds_seen - 1, len(self.owner.rounds) - 1)
            return int(self.owner.rounds[self.owner.index]["spinner"])
        return int(self.owner.rounds[self.owner.index]["canvas"])


class ScriptedLocator:
    """按**轮次脚本**返回 ``(spinner, text, canvas)`` 的模块容器假体。"""

    def __init__(self, rounds: list[dict[str, Any]]) -> None:
        self.rounds = rounds
        self.index = 0
        self.rounds_seen = 0
        self.text_calls = 0

    def locator(self, selector: str) -> Any:
        if "spin-spinning" in selector:
            return _CountView(self, "spinner")
        if selector == "canvas, svg":
            return _CountView(self, "chart")
        raise AssertionError(f"未预期的子选择器: {selector!r}")

    async def inner_text(self, timeout: int | None = None) -> str:
        self.text_calls += 1
        return str(self.rounds[self.index]["text"])


class FakeLocator:
    """通用 Locator 假体(元素截图 / 点击 / 可见性)。"""

    def __init__(
        self,
        *,
        count: int = 0,
        visible: bool = True,
        text: str = "",
        children: list[FakeLocator] | None = None,
        on_shot: Any = None,
        log: list[str] | None = None,
        sub_counts: dict[str, int] | None = None,
    ) -> None:
        self._count = count
        self._visible = visible
        self._text = text
        self._children = children or []
        self._on_shot = on_shot
        self.log = log if log is not None else []
        self.sub_counts = sub_counts or {}
        self.nth_calls: list[int] = []
        self.clicked = 0
        self.shot_args: dict[str, Any] | None = None

    # --- 结构 ---
    def locator(self, selector: str) -> FakeLocator:
        self.log.append(f"locator({selector})")
        # ★ 容器内部的 spinner / canvas 计数**默认 0**(否则会被当成"永远加载中")
        if "spin-spinning" in selector:
            return FakeLocator(count=self.sub_counts.get("spinner", 0), log=self.log)
        if selector == "canvas, svg":
            return FakeLocator(count=self.sub_counts.get("canvas", 0), log=self.log)
        return self

    def nth(self, i: int) -> FakeLocator:
        self.nth_calls.append(i)
        if i < len(self._children):
            return self._children[i]
        return self

    @property
    def first(self) -> FakeLocator:
        return self.nth(0)

    # --- 判定 ---
    async def count(self) -> int:
        return self._count

    async def is_visible(self) -> bool:
        return self._visible

    async def inner_text(self, timeout: int | None = None) -> str:
        return self._text

    # --- 动作 ---
    async def click(self, timeout: int | None = None) -> None:
        self.clicked += 1
        self.log.append(f"click(timeout={timeout})")

    async def dispatch_event(self, event: str) -> None:
        self.log.append(f"dispatch_event({event})")

    async def wait_for_selector(self, selector: str, state: str = "", timeout: int = 0) -> None:
        self.log.append(f"wait_for_selector({selector}, state={state}, timeout={timeout})")

    async def screenshot(self, path: str = "", type: str = "", quality: int = 0) -> None:
        self.shot_args = {"path": path, "type": type, "quality": quality}
        if self._on_shot is not None:
            self._on_shot(Path(path))


class FakePage:
    """页面假体:按选择器分发到 FakeLocator。"""

    def __init__(self, mapping: dict[str, Any], *, default: Any = None) -> None:
        self.mapping = mapping
        self.default = default
        self.log: list[str] = []
        self.gotos: list[str] = []

    def locator(self, selector: str) -> Any:
        if selector in self.mapping:
            return self.mapping[selector]
        self.log.append(f"locator({selector})")
        if self.default is not None:
            return self.default
        return FakeLocator(count=0)

    def get_by_text(self, text: str, exact: bool = False) -> FakeLocator:
        self.log.append(f"get_by_text({text}, exact={exact})")
        return FakeLocator(count=0)

    async def goto(self, url: str, wait_until: str = "", timeout: int = 0) -> None:
        self.gotos.append(url)

    async def wait_for_selector(self, selector: str, state: str = "", timeout: int = 0) -> None:
        self.log.append(f"page.wait_for_selector({selector}, state={state}, timeout={timeout})")


def make_ctx(layout: Layout, day: date = date(2026, 9, 30)) -> ExtractContext:
    """真 :class:`ExtractContext`(用 ``model_construct`` 跳过 Protocol 校验)。"""
    return ExtractContext.model_construct(
        hotel=HotelRef(id=7, name="测试酒店"),
        account=AccountRef(id=3, alias="ctrip003", platform="ctrip"),
        collect_date=day,
        session=object(),
        limiter=object(),
        layout=layout,
        http=object(),
    )


# ---------------------------------------------------------------------------
# 1~3. 就绪判定
# ---------------------------------------------------------------------------


async def t_wait_content_judgement() -> None:
    print("\n=== 1. _wait_content 判定式(60 字 + 2 数字 / canvas / 边界)===")
    long_text = "经" * 56 + " 1 2"  # len=60,数字 2 个
    print(f"输入 long_text: len={len(long_text)} text={long_text!r}")

    loc = ScriptedLocator(
        [
            {"spinner": 0, "text": long_text, "canvas": 0},
            {"spinner": 0, "text": long_text, "canvas": 0},
        ]
    )
    t0 = time.monotonic()
    ok = await _wait_content(FakePage({}), loc)  # type: ignore[arg-type]
    print(
        f"  轮次={loc.rounds_seen} text_calls={loc.text_calls} 用时={time.monotonic() - t0:.2f}s"
        f" → {ok}"
    )
    check("60 字 + 2 数字 + 连续两轮一致 → True", ok is True and loc.text_calls == 2)

    short59 = "经" * 55 + " 1 2"  # len=59 → has_data False(但仍有 2 个数字)
    assert len(short59) == 59, len(short59)
    loc = ScriptedLocator([{"spinner": 0, "text": short59, "canvas": 0}] * 3)
    ok = await _wait_content(FakePage({}), loc, max_seconds=0.6)  # type: ignore[arg-type]
    print(f"输入 59 字(len={len(short59)})+2 数字 → {ok}(超时返回 False)")
    check("59 字不满足 ≥60 → 超时 False", ok is False)

    one_num = "经" * 58 + " 1"  # len=60,数字 1 个
    assert len(one_num) == 60 and len([c for c in one_num if c.isdigit()]) == 1
    loc = ScriptedLocator([{"spinner": 0, "text": one_num, "canvas": 0}] * 3)
    ok = await _wait_content(FakePage({}), loc, max_seconds=0.6)  # type: ignore[arg-type]
    print(f"输入 len={len(one_num)} 数字=1 个 → {ok}")
    check("60 字但只有 1 个数字 → 超时 False", ok is False)

    loc = ScriptedLocator([{"spinner": 0, "text": "短", "canvas": 1}] * 2)
    ok = await _wait_content(FakePage({}), loc)  # type: ignore[arg-type]
    print(f"输入 text='短'(len=1) canvas=1 → {ok}")
    check("短文本但容器内有 canvas → True", ok is True)

    # ★ spinner 命中:不更新 last_text
    print("\n--- ★ spinner 命中分支:last_text 不得被更新 ---")
    loc = ScriptedLocator(
        [
            {"spinner": 1, "text": long_text, "canvas": 0},  # 加载中(文本与下一轮相同)
            {"spinner": 0, "text": long_text, "canvas": 0},
            {"spinner": 0, "text": long_text, "canvas": 0},
        ]
    )
    ok = await _wait_content(FakePage({}), loc)  # type: ignore[arg-type]
    print(
        f"输入 轮次=[(spinner=1,text=同下轮),(0,同文本),(0,同文本)]"
        f" → ok={ok} 轮次={loc.rounds_seen} text_calls={loc.text_calls}"
    )
    check(
        "spinner 轮不参与「连续两轮」→ 需 2 次非 spinner 轮文本(text_calls==2)",
        ok is True and loc.text_calls == 2,
    )


async def t_wait_charts_ready() -> None:
    print("\n=== 2. _wait_charts_ready vs _wait_content(漂移 D-2)===")
    print(f"常量: CHART_READY_WAIT_S={CHART_READY_WAIT_S} CONTENT_WAIT_S={CONTENT_WAIT_S}")
    check("_wait_charts_ready 上限 8s", CHART_READY_WAIT_S == 8.0)
    check("_wait_content 上限 20s", CONTENT_WAIT_S == 20.0)

    # 短 body 文本 + 有 canvas + 无 spinner → 图表就绪(证明**没有** 60 字判定)
    body = FakeLocator(text="短文本")  # len=3,数字 0 个
    canvas = FakeLocator(count=1)
    spinner = FakeLocator(count=0)
    page = FakePage({"body": body, "canvas, svg": canvas, ".ant-spin-spinning, [class*='spin-spinning']": spinner})
    t0 = time.monotonic()
    ok = await _wait_charts_ready(page)  # type: ignore[arg-type]
    print(
        f"输入 body='短文本'(len=3,数字=0) canvas=1 spinner=0 → {ok}"
        f" 用时={time.monotonic() - t0:.2f}s(连续两轮=1 个轮询间隔)"
    )
    check("短文本 + canvas → 图表就绪 True(证明本函数无 60 字判定)", ok is True)

    # 同一容器拿去问 _wait_content:短文本且**无** canvas → 超时(两套判定的差别)
    loc_nocanvas = ScriptedLocator([{"spinner": 0, "text": "短文本", "canvas": 0}] * 3)
    ok2 = await _wait_content(FakePage({}), loc_nocanvas, max_seconds=0.6)  # type: ignore[arg-type]
    print(f"同一短文本交给 _wait_content 且无 canvas → {ok2}(超时 False)")
    check("_wait_content 对短文本且无 canvas → False(条件更严)", ok2 is False)

    # 8s 缺省上限(永不稳定的 body)
    class NeverStableBody(FakeLocator):
        def __init__(self) -> None:
            super().__init__(count=1)
            self.n = 0

        async def inner_text(self, timeout: int | None = None) -> str:
            self.n += 1
            return f"变化中-{self.n}"

    page = FakePage(
        {
            "body": NeverStableBody(),
            "canvas, svg": FakeLocator(count=1),
            ".ant-spin-spinning, [class*='spin-spinning']": FakeLocator(count=0),
        }
    )
    t0 = time.monotonic()
    ok = await _wait_charts_ready(page)  # type: ignore[arg-type]
    elapsed = time.monotonic() - t0
    print(f"输入 body 文本每轮变化 → {ok} 用时={elapsed:.2f}s(缺省上限应为 8s)")
    check("缺省上限 = 8s(7.9s <= 用时 < 9.5s)", ok is False and 7.9 <= elapsed < 9.5)


# ---------------------------------------------------------------------------
# 4. skip_click_on
# ---------------------------------------------------------------------------


def t_skip_click_on() -> None:
    print("\n=== 3. skip_click_on 判定(weekday()==0 / day==1)===")
    monday = date(2026, 9, 28)
    while monday.weekday() != 0:
        monday = date.fromordinal(monday.toordinal() + 1)
    not_monday = date.fromordinal(monday.toordinal() + 1)
    mod = ScreenshotModuleCfg(selector="div.x", name="经营对比", tabs="div.x", click="实时")
    cases = [
        (mod.model_copy(update={"skip_click_on": ["monday", "month1"]}), monday, True),
        (mod.model_copy(update={"skip_click_on": ["month1"]}), monday, False),
        (mod.model_copy(update={"skip_click_on": ["monday", "month1"]}), not_monday, False),
        (mod.model_copy(update={"skip_click_on": None}), monday, False),
    ]
    month1 = date(2026, 10, 1)
    mod_m1 = ScreenshotModuleCfg(
        selector="div.x", name="金字塔-效果", tabs="div.x", click="效果类数据",
        skip_click_on=["monday", "month1"],
    )
    cases.append((mod_m1, month1, True))
    cases.append((mod_m1, date(2026, 10, 2), False))
    print(f"monday={monday}(weekday={monday.weekday()}) not_monday={not_monday}"
          f"(weekday={not_monday.weekday()}) month1={month1}(day={month1.day})")
    for cfg, day, expected in cases:
        got = should_skip_click(cfg, day)
        print(
            f"  skip_click_on={cfg.skip_click_on} day={day}(weekday={day.weekday()},"
            f"day={day.day}) → {got}(期望 {expected})"
        )
        check(f"skip_click_on={cfg.skip_click_on} on {day} → {expected}", got is expected)


# ---------------------------------------------------------------------------
# 5~9. 截图器(容器选取 / require_text / 截图 / 落库回填)
# ---------------------------------------------------------------------------


async def t_require_text_fast_skip(tmp: Path) -> None:
    print("\n=== 4. require_text 未命中 → 快跳 ===")
    layout = Layout(project_root=tmp, var_dir=tmp / "var")
    ctx = make_ctx(layout)
    mod = ScreenshotModuleCfg(
        selector="table:has-text('权益状态')",
        name="审核记录",
        clicks=["审核记录", "权益情况"],
        require_text="审核记录",
    )
    page = FakePage({})  # 全部选择器 count=0
    shot = Screenshoter(settings=FakeSettings(), rules=load_rules(), pool=object())
    t0 = time.monotonic()
    outcome = await shot._shoot_module(
        page,  # type: ignore[arg-type]
        ctx=ctx,
        page_name="挂牌管理",
        target_url="https://example.invalid/x",
        mod=mod,
        used_keys={},
    )
    elapsed = time.monotonic() - t0
    print(
        f"输入 page 无「审核记录」文本 → state={outcome.state} reason={outcome.reason!r}"
        f" 用时={elapsed:.3f}s page.log={page.log}"
    )
    check("require_text 未命中 → missing_entry(不报错)", outcome.state == "missing_entry")
    check("快跳:不等 30s(用时 < 0.5s)", elapsed < 0.5)
    check(
        "快跳:只做入口探测,不进 clicks/tabs(无 wait_for_selector)",
        not any("wait_for_selector" in e for e in page.log)
        and any("text=审核记录" in e for e in page.log),
    )


async def t_visible_and_click(tmp: Path) -> None:
    print("\n=== 5. 可见容器选取(50)/ 点击辅助(10)===")
    children = [
        FakeLocator(count=3, visible=False),
        FakeLocator(count=3, visible=True),
        FakeLocator(count=3, visible=True),
    ]
    root = FakeLocator(count=3, children=children)
    page = FakePage({"div.dup": root})
    loc = await _pick_visible_container(page, "div.dup")  # type: ignore[arg-type]
    print(f"输入 count=3 visible=[False,True,True] → 命中 nth={root.nth_calls}")
    check("取首个可见 = nth(1)", root.nth_calls == [0, 1] and loc is children[1])

    many = FakeLocator(count=60, children=[FakeLocator(count=1, visible=False) for _ in range(60)])
    page = FakePage({"div.many": many})
    loc = await _pick_visible_container(page, "div.many")  # type: ignore[arg-type]
    print(f"输入 count=60 全不可见 → loc={loc} 遍历下标={many.nth_calls}")
    check("遍历上限 min(count,50) = 0..49", many.nth_calls == list(range(50)) and loc is None)

    cand = FakeLocator(count=20, children=[FakeLocator(count=1, visible=False) for _ in range(20)])
    ok = await _click_first_visible(cand)
    print(f"输入 20 个全不可见 → {ok} 遍历下标={cand.nth_calls}")
    check("_click_first_visible 上限 min(count,10) = 0..9", cand.nth_calls == list(range(10)))

    second = FakeLocator(count=2, visible=True, log=[])
    cand = FakeLocator(count=2, children=[FakeLocator(count=1, visible=False), second])
    ok = await _click_first_visible(cand)
    print(f"输入 [不可见,可见] → {ok} clicked={second.clicked} log={second.log}")
    check("点第一个可见元素", ok is True and second.clicked == 1)


async def t_shoot_and_backfill(tmp: Path) -> None:
    print("\n=== 6. 元素截图 + 超限告警 + run() 落库回填 ===")
    var = tmp / "var"
    layout = Layout(project_root=tmp, var_dir=var)
    ctx = make_ctx(layout)
    rules = load_rules()
    page_cfg = rules.page("经营报告")
    sub = page_cfg.sub_module("离店")
    assert sub is not None
    mod = sub.screenshot_modules[0]
    print(f"目标: page=经营报告 module=离店 selector={mod.selector!r} url={sub.url}")

    body_text = "离店间夜 12 间 收入 3456 元 " + "说" * 45  # len>=60 且 >=2 个数字
    container = FakeLocator(count=1, visible=True, text=body_text)
    container._on_shot = lambda p: p.write_bytes(b"x" * 200)  # 200B > max_bytes=100
    page = FakePage(
        {
            mod.selector: container,
            "body": FakeLocator(count=1, text="短"),
            "canvas, svg": FakeLocator(count=1),
            ".ant-spin-spinning, [class*='spin-spinning']": FakeLocator(count=0),
        }
    )

    calls: list[str] = []

    class FakeRepo:
        async def ensure_report(self, hotel_id, collect_date, page, *, channel="", account_id=None):
            calls.append(f"ensure_report({hotel_id},{collect_date},{page},channel={channel},account_id={account_id})")
            return 42

        async def link_screenshot(self, hotel_id, collect_date, page, *, screenshot_path=None, module_screenshots=None):
            calls.append(
                f"link_screenshot({hotel_id},{collect_date},{page},screenshot_path={screenshot_path},"
                f"module_screenshots={module_screenshots})"
            )
            return 1

    class FakePool:
        @contextlib.asynccontextmanager
        async def page_session(self, handle, **kwargs):
            calls.append(f"page_session(handle={type(handle).__name__})")
            yield (None, None, page)

    shot = Screenshoter(settings=FakeSettings(), rules=rules, pool=FakePool())
    results = await shot.run(ctx, [("经营报告", "离店"), ("不存在的页", "离店")], repo=FakeRepo())
    print("repo 调用顺序:")
    for c in calls:
        print(f"  · {c}")
    print(f"page_session kwargs 里的导航: {page.gotos}")
    for r in results:
        print(
            f"  → target={r.target.page}/{r.target.module}/{r.target.window} status={r.status}"
            f" channel={r.channel} raw_path={r.raw_path} payload={r.payload}"
            f" error={r.error} detail_keys={sorted((r.detail or {}).keys())}"
        )

    check("ensure_report 在截图**之前**被调用", calls and calls[0].startswith("ensure_report"))
    check("ensure_report 带 channel=screenshot(且带 account_id)", "channel=screenshot" in calls[0])
    check(
        "link_screenshot 恒传 screenshot_path=None(COALESCE 保留旧整页图)",
        any("screenshot_path=None" in c for c in calls),
    )
    check(
        "module_screenshots 是 {name: 相对路径} 字符串映射",
        any("'离店': 'var" in c.replace('"', "'") or "'离店': 'var" in c for c in calls),
    )
    ok_shot = results[0]
    check("有图 → degraded(截图不是主数据通道)", ok_shot.status == "degraded")
    check("payload 键 = screenshot_modules[*].name", list((ok_shot.payload or {}).keys()) == [mod.name])
    check("raw_path 是相对路径", bool(ok_shot.raw_path) and not Path(ok_shot.raw_path or "/").is_absolute())
    check("未知页 → failed", results[1].status == "failed")
    check("超限只告警:oversize 记录进 detail", ok_shot.detail is not None and ok_shot.detail["oversize"] == [mod.name])
    print(f"  截图落盘: {sorted(p.name for p in (var / 'screenshots').rglob('*.jpg'))}")
    print(f"  locator.screenshot 实参: {container.shot_args}")
    check(
        "元素截图 type=jpeg quality=70(settings.screenshot.jpeg_quality),落 .jpg",
        (container.shot_args or {}).get("type") == "jpeg"
        and (container.shot_args or {}).get("quality") == 70
        and str((container.shot_args or {}).get("path", "")).endswith(".jpg"),
    )


async def t_interaction(tmp: Path) -> None:
    """tabs+click / clicks / 消弹窗 —— 用 patch 掉的 asyncio.sleep 记录"该等几秒"。"""
    print("\n=== 7. 交互语义:tabs+click / clicks / 消弹窗安全门槛 ===")
    from unittest import mock

    from hoteldata.domains.collect import screenshot as shot_mod
    from hoteldata.domains.collect.screenshot import _dismiss_notifications

    shot = Screenshoter(settings=FakeSettings(), rules=load_rules(), pool=object())
    pyramid = ScreenshotModuleCfg(
        selector="div.pyramid-comparison .pyramid-diagnostic-chart-row",
        name="金字塔-数据报告-同行对比-效果",
        tabs="div.pyramid-comparison",
        click="效果类数据",
        skip_click_on=["monday", "month1"],
    )
    monday = date(2026, 9, 28)
    while monday.weekday() != 0:
        monday = date.fromordinal(monday.toordinal() + 1)
    tuesday = date.fromordinal(monday.toordinal() + 1)

    tabs = FakeLocator(count=1)
    page = FakePage({"div.pyramid-comparison": tabs})
    with mock.patch.object(shot_mod.asyncio, "sleep", new=mock.AsyncMock()) as sleeper:
        switched = await shot._click_tab(page, mod=pyramid, today=monday)  # type: ignore[arg-type]
    print(
        f"输入 tabs=div.pyramid-comparison click='效果类数据' skip_click_on=… day={monday}"
        f"(周一) → switched={switched} sleeps={sleeper.await_args_list} clicked={tabs.clicked}"
    )
    check("周一 skip_click_on 命中 → 不点击、不 sleep、不重等", switched is False)
    check("周一命中时点击前 6s 也不等", sleeper.await_count == 0)
    check("tabs 仍按 attached/15s 定位", any("state=attached" in e for e in page.log))

    tabs = FakeLocator(count=1)
    page = FakePage({"div.pyramid-comparison": tabs})
    with mock.patch.object(shot_mod.asyncio, "sleep", new=mock.AsyncMock()) as sleeper:
        switched = await shot._click_tab(page, mod=pyramid, today=tuesday)  # type: ignore[arg-type]
    print(
        f"输入 day={tuesday}(非周一) → switched={switched}"
        f" sleeps={[c.args[0] for c in sleeper.await_args_list]} clicked={tabs.clicked}"
    )
    check("非周一 → 点击前 sleep(6.0)", [c.args[0] for c in sleeper.await_args_list] == [6.0])
    check("非周一 → 点击执行且需要页签后重等", switched is True and tabs.clicked == 1)

    flow = ScreenshotModuleCfg(
        selector="div.manage-mode.date-sources-box",
        name="流量来源分析",
        clicks=["过去7天", "昨天", "查看更多"],
    )
    scope = FakeLocator(count=1, visible=True)
    page = FakePage({flow.selector: scope})
    with mock.patch.object(shot_mod.asyncio, "sleep", new=mock.AsyncMock()) as sleeper:
        await shot._run_clicks(page, mod=flow)  # type: ignore[arg-type]
    sleeps = [c.args[0] for c in sleeper.await_args_list]
    print(
        f"输入 clicks={flow.clicks}(容器内可点)→ sleeps={sleeps} clicked={scope.clicked}"
    )
    check("clicks:前置 6.0 + 每成功步 8.0", sleeps == [6.0, 8.0, 8.0, 8.0])
    check("clicks:三级回退第一级即命中容器内元素", scope.clicked == 3)

    empty = FakeLocator(count=0, visible=False)
    page = FakePage({flow.selector: empty})
    with mock.patch.object(shot_mod.asyncio, "sleep", new=mock.AsyncMock()) as sleeper:
        await shot._run_clicks(page, mod=flow)  # type: ignore[arg-type]
    print(
        f"输入 容器内/页面级都无元素 → sleeps={[c.args[0] for c in sleeper.await_args_list]}"
        f" clicked={empty.clicked}"
    )
    check("clicks:无可点元素不 sleep 8s(仅前置 6.0)", [c.args[0] for c in sleeper.await_args_list] == [6.0])

    # ★ 消弹窗的安全门槛:没有通知弹窗时**绝不**点页面上的「关闭」
    close_btn = FakeLocator(count=1, visible=True)
    page = FakePage({"button:has-text('关闭')": close_btn})
    ok = await _dismiss_notifications(page, max_seconds=0.6)  # type: ignore[arg-type]
    print(f"输入 无「允许发送通知」但页面上有可点「关闭」→ ok={ok} clicked={close_btn.clicked}")
    check("无通知弹窗 → 不点「关闭」(安全门槛)", ok is False and close_btn.clicked == 0)

    allow_btn = FakeLocator(count=1, visible=True)
    page = FakePage(
        {
            "text=允许发送通知": FakeLocator(count=1, visible=True),
            "button:has-text('允许')": allow_btn,
        }
    )
    ok = await _dismiss_notifications(page)  # type: ignore[arg-type]
    print(f"输入 有通知弹窗 → ok={ok} allow_clicked={allow_btn.clicked}")
    check("有通知弹窗 → 点「允许」并返回 True", ok is True and allow_btn.clicked == 1)


def t_pure_helpers() -> None:
    print("\n=== 8. 纯函数:dedupe_targets / aggregate_status ===")
    raw = [("经营报告", "离店"), ("经营报告", "离店"), ("广告营销", "金字塔-首页")]
    print(f"输入 {raw} → {dedupe_targets(raw)}")
    check("去重保序", dedupe_targets(raw) == [("经营报告", "离店"), ("广告营销", "金字塔-首页")])
    for shots, errors, expect in [(2, 0, "degraded"), (2, 1, "degraded"), (0, 1, "failed"), (0, 0, "no_data")]:
        got = aggregate_status(shots=shots, errors=errors)
        print(f"  shots={shots} errors={errors} → {got}(期望 {expect})")
        check(f"aggregate_status({shots},{errors})={expect}", got == expect)


def t_demand() -> None:
    print("\n=== 9. screenshot_demand_modules(真实 push_rotation.json + api_rules.json)===")
    rules = load_rules()
    for day in (date(2026, 9, 30), date(2026, 10, 1)):
        targets = screenshot_demand_modules(day, rules=rules)
        print(f"day={day}(weekday={day.weekday()}) → {len(targets)} 个目标: {targets}")
        check(f"{day} 目标数 <= daily_count=5", len(targets) <= 5)
        check(
            f"{day} 每个目标都能在规则里反查到",
            all(rules.find_sub_module(p, m) is not None for p, m in targets),
        )
    explicit = screenshot_demand_modules(targets=[("A", "B"), ("A", "B")])
    print(f"targets 显式传入(外部清单逃生口) → {explicit}")
    check("显式 targets 原样去重保序", explicit == [("A", "B")])


# ---------------------------------------------------------------------------


async def main() -> int:
    tmp = ROOT / "_scratch" / "_tmp_offline"
    tmp.mkdir(parents=True, exist_ok=True)
    print(f"项目根: {ROOT}")
    print(f"临时 var/: {tmp}")
    await t_wait_content_judgement()
    await t_wait_charts_ready()
    t_skip_click_on()
    await t_require_text_fast_skip(tmp)
    await t_visible_and_click(tmp)
    await t_shoot_and_backfill(tmp)
    await t_interaction(tmp)
    t_pure_helpers()
    t_demand()
    print(f"\n===== 结果: {'全部通过' if not FAILURES else f'{len(FAILURES)} 项失败'} =====")
    for f in FAILURES:
        print(f"  FAIL: {f}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    real_stdout = sys.stdout
    sys.stdout = _Tee(OUT_PATH)
    try:
        code = asyncio.run(main())
    finally:
        sys.stdout.flush()
        sys.stdout = real_stdout
    print(f"written: {OUT_PATH}")
    print(f"exit_code={code}")
    raise SystemExit(code)
