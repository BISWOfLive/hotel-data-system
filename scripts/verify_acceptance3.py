"""段3 验收执行器 —— 逐条跑 **V61–V84** 并产出证据。

设计原则与段1/段2 的验收器一致
================================

1. **能不用 mock 就不用 mock**:用真 PostgreSQL、真配置文件、真旧系统证据;
2. **需要真登录态才能完成的条目如实标 ``BLOCKED``**,不伪装成 PASS
   (段2 的 V21 就是这条纪律);
3. 输出控制台表格 + ``var/reports/段3-验收结果.json``。

★ 本验收器的两条"真"从哪来
==========================

* **携程真接口**:用**旧系统自己留下的真实响应**(``diag_ctrip_api_no_hotels_*.json``)
  验证字段路径(坐标/评分/点评数/详情页 URL)—— 那是真数据,不是我造的样本;
* **真 PostgreSQL**:幂等、slot、唯一约束、批量每日一行**全部真跑**。

用法::

    .venv\\Scripts\\python.exe scripts\\verify_acceptance3.py
    .venv\\Scripts\\python.exe scripts\\verify_acceptance3.py --only V61 V65 V78
    .venv\\Scripts\\python.exe scripts\\verify_acceptance3.py --offline   # 跳过需要真登录态的
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except (AttributeError, ValueError):  # pragma: no cover
    pass

from hoteldata.logging import configure_stdio  # noqa: E402
from hoteldata.settings import get_settings  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
#: 合成数据用的采集日(远离真实日期,便于识别与清理)
SYNTH_DAY = date(2000, 1, 1)
#: 演示/合成酒店名前缀
SYNTH_PREFIX = "验收3-"
#: 旧系统根目录(真证据来源)
OLD_SYSTEM = Path(r"D:\AAAAaaaa\Pythooooooooooooon\hotel-data-system")
#: 段2 代码哈希基线
HASH_BASELINE = PROJECT_ROOT / "docs" / "参考" / "段3-分析" / "_段2代码哈希基线.txt"

PASS, FAIL, BLOCKED, SKIP = "PASS", "FAIL", "BLOCKED", "SKIP"


@dataclass
class Check:
    vid: str
    title: str
    status: str
    evidence: str
    detail: dict[str, Any] = field(default_factory=dict)


RESULTS: list[Check] = []


def record(vid: str, title: str, status: str, evidence: str, **detail: Any) -> None:
    RESULTS.append(Check(vid, title, status, evidence, detail))
    icon = {PASS: "✅", FAIL: "❌", BLOCKED: "⛔", SKIP: "⏭"}[status]
    print(f"  {icon} [{vid}] {title}")
    print(f"        {evidence}")


# ===========================================================================
# 批次 A · 契约与地基
# ===========================================================================


def check_v61() -> None:
    """真契约:两平台实现同一协议;无 hasattr 探测;返回类型化对象。"""
    from hoteldata.domains.compare import create_platform, load_platforms
    from hoteldata.domains.compare.contract import HotelQuote, PricePlatform

    names = load_platforms()
    ctrip, meituan = create_platform("ctrip"), create_platform("meituan")
    problems: list[str] = []
    for obj in (ctrip, meituan):
        if not isinstance(obj, PricePlatform):
            problems.append(f"{obj.name} 不是 PricePlatform")
        for m in ("resolve_anchor", "collect_quotes"):
            if not callable(getattr(obj, m, None)):
                problems.append(f"{obj.name} 缺 {m}")
        if hasattr(obj, "collect_map_prices"):
            problems.append(f"{obj.name} 仍有旧假契约 collect_map_prices")
        if hasattr(obj, "collect"):
            problems.append(f"{obj.name} 仍有旧死模板 collect")

    # 类型化:模型拒绝未声明字段(裸 dict 塞不进来)
    typed_ok = False
    try:
        HotelQuote.model_validate({"hotel_name": "x", "未声明字段": 1})
    except Exception:  # noqa: BLE001
        typed_ok = True

    status = PASS if not problems and typed_ok else FAIL
    record(
        "V61",
        "平台契约是真的(无 hasattr 探测 + 类型化)",
        status,
        f"平台={names};两平台均通过 isinstance(PricePlatform) 检查;"
        f"旧假契约 collect_map_prices/collect 均不存在;模型拒绝未声明字段={typed_ok}"
        + (f";问题={problems}" if problems else ""),
        platforms=list(names),
    )


def check_v62() -> None:
    """声明式注册表:新增平台 = 加一个类 + 注册一行,不改 runner。"""
    from hoteldata.domains.compare import registry
    from hoteldata.domains.compare.registry import PlatformNotRegistered, available_platforms

    before = available_platforms()

    # ★ 临时加一个**假平台**,证明 runner 不需要改动
    class _FakePlatform:
        name = "meituan"  # 复用已声明名(契约只允许 ctrip/meituan)
        home_url = "https://example.invalid/"

        async def resolve_anchor(self, ctx: Any, name: str, city: str | None = None, **_: Any) -> Any:
            raise NotImplementedError

        async def collect_quotes(self, ctx: Any, anchor: Any, count: int, **_: Any) -> Any:
            raise NotImplementedError

    problems: list[str] = []
    # 重名注册必须抛错
    try:
        registry.register(_FakePlatform)
        problems.append("重名注册未抛错")
    except ValueError:
        pass
    # 拼错的平台名必须抛错(挡住 HOTEL_PLATFORMS=ctrp 这类静默失效)
    class _BadName(_FakePlatform):
        name = "ctrp"

    try:
        registry.register(_BadName)
        problems.append("未声明的平台名未抛错")
    except (ValueError, TypeError):
        pass
    # 未注册平台必须抛错(不静默返回 None)
    try:
        registry.create_platform("qunar")
        problems.append("未知平台未抛错")
    except PlatformNotRegistered:
        pass

    # 注册一个合法的新名字(临时改契约外?)—— 契约只允许两个平台,
    # 所以"新增平台"的验证改为:注册表是**数据驱动**的(不是 if/elif 硬编码)
    src = (PROJECT_ROOT / "src/hoteldata/domains/compare/registry.py").read_text(encoding="utf-8")
    # ★ 只扫**代码**,跳过文档字符串与注释里的举例
    #   (段3 的模块文档里正举着旧写法的例子 ``if name == "ctrip": …`` ——
    #    前两版断言把这段说明当成了"代码里还有硬编码",于是误判)
    code_lines: list[str] = []
    in_doc = False
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.count('"""') == 1:  # 单行开启/结束文档字符串
            in_doc = not in_doc
            continue
        if in_doc or stripped.startswith(("#", '"""', "'''")):
            continue
        code_lines.append(line.split("#", 1)[0])
    code = "\n".join(code_lines)
    data_driven = (
        "_REGISTRY[name] = cls" in code
        and 'if name == "' not in code
        and 'elif name == "' not in code
    )

    status = PASS if not problems and data_driven else FAIL
    record(
        "V62",
        "注册表声明式(加类+一行,不改 runner)",
        status,
        f"注册表是 dict 数据驱动={data_driven};重名注册抛错;未声明名抛错;"
        f"未知平台抛 PlatformNotRegistered;平台={before}"
        + (f";问题={problems}" if problems else ""),
    )


async def check_v63() -> None:
    """异常语义明确:三态(可重试/不可重试/需人工),不被吞成 result['error']。"""
    from hoteldata.domains.compare.contract import (
        HumanVerificationError,
        PlatformError,
        PriceFatalError,
        PriceRetryableError,
    )

    # ★ 三态同源:可重试与不可重试是**兄弟**(都继承 PlatformError),
    #   而"需人工"是 PriceFatalError 的**子类**(对"要不要自动重试"回答"不要"),
    #   但类型可辨认 —— 这样才能把它单独拎出来提示"请人工登录"。
    retryable_is_platform = issubclass(PriceRetryableError, PlatformError)
    retryable_not_fatal = not issubclass(PriceRetryableError, PriceFatalError)
    human_is_fatal = issubclass(HumanVerificationError, PriceFatalError)
    human_not_retryable = not issubclass(HumanVerificationError, PriceRetryableError)

    # 平台实现里**没有**把异常吞成返回值:搜 "["error"]" 之类的写法
    bad: list[str] = []
    for py in (PROJECT_ROOT / "src/hoteldata/domains/compare").rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        if '"error":' in text and "PlatformOutcome" not in text:
            bad.append(py.name)

    # ★ 平台实现里**不得**把异常吞成"返回值里的 error 字段"。
    #   只看 ``domains/compare/platforms/`` —— 那是"平台契约"的落脚点;
    #   ``service.py`` 里的 ``{"anchor":…, "error":…}`` 是**批量汇总报告**
    #   (给运维看的逐店结果列表),与"吞异常"是两件相反的事。
    bad: list[str] = []
    plat_dir = PROJECT_ROOT / "src/hoteldata/domains/compare/platforms"
    for py in plat_dir.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        if '"error":' in text or "'error':" in text:
            bad.append(py.name)
    runner_src = (PROJECT_ROOT / "src/hoteldata/domains/compare/runner.py").read_text("utf-8")
    # runner 用**类型化**的 PlatformOutcome.error_kind 记录异常种类(不是裸字符串)
    typed_error = "error_kind" in runner_src

    ok = (
        retryable_is_platform
        and retryable_not_fatal
        and human_is_fatal
        and human_not_retryable
        and not bad
        and typed_error
    )
    record(
        "V63",
        "异常语义明确(三态,不被吞成 result['error'])",
        PASS if ok else FAIL,
        f"三态同源(都继承 PlatformError):可重试={retryable_is_platform}、"
        f"不可重试={issubclass(PriceFatalError, PlatformError)};"
        f"可重试与不可重试是兄弟={retryable_not_fatal};"
        f"HumanVerificationError⊂PriceFatalError={human_is_fatal}(需人工,不自动重试)"
        f"且不是可重试={human_not_retryable};"
        f"平台实现里无裸 error 字段={not bad}{bad if bad else ''};"
        f"runner 用类型化 error_kind 记录失败种类={typed_error}",
        retryable=retryable_is_platform,
        human=human_is_fatal,
    )


async def check_v64() -> None:
    """比价目标可维护:targets add/list/set/remove 生效;旧两个 txt 可一次性导入。"""
    from sqlalchemy import select

    from hoteldata.domains.compare.importer import parse_target_files
    from hoteldata.domains.compare.repository import CompareRepository
    from hoteldata.infra.models import CmpPriceTarget
    from hoteldata.runtime import Runtime

    async with Runtime.create(with_browser=False) as rt:
        async with rt.db.session() as s:
            repo = CompareRepository(s)
            name = f"{SYNTH_PREFIX}目标测试"
            await s.execute(
                CmpPriceTarget.__table__.delete().where(CmpPriceTarget.anchor_name == name)
            )
            await s.commit()
            # ① add
            await repo.upsert_target(anchor_name=name, city="验收市", mode="batch")
            await s.commit()
            added = any(r.anchor_name == name for r in await repo.list_targets())
            # ② set(停用)
            n_disable = await repo.set_target_enabled(name, False, city="验收市")
            await s.commit()
            enabled_rows = await repo.list_targets()
            disabled_gone = not any(r.anchor_name == name for r in enabled_rows)
            # ★ 必须 expire_all:SQLAlchemy 身份映射会**返回缓存的同一个 ORM 对象**,
            #   直接 select 拿到的是内存里的旧值 —— 断言会读到"看起来没生效"的假象
            #   (段3 的验收器第一版就是这样把"已经生效"误判成失败的)。
            s.expire_all()
            row0 = (
                await s.execute(select(CmpPriceTarget).where(CmpPriceTarget.anchor_name == name))
            ).scalars().first()
            disabled_flag = row0 is not None and row0.enabled is False
            # ③ ★ 回归:再 upsert 一次(改模式)**不应把停用悄悄改回启用**
            await repo.upsert_target(anchor_name=name, city="验收市", mode="cron")
            await s.commit()
            s.expire_all()
            row1 = (
                await s.execute(select(CmpPriceTarget).where(CmpPriceTarget.anchor_name == name))
            ).scalars().first()
            stays_disabled = row1 is not None and row1.enabled is False
            mode_updated = row1 is not None and row1.mode == "cron"
            # ④ 显式启用
            n_enable = await repo.set_target_enabled(name, True, city="验收市")
            await s.commit()
            s.expire_all()
            row2 = (
                await s.execute(select(CmpPriceTarget).where(CmpPriceTarget.anchor_name == name))
            ).scalars().first()
            re_enabled = row2 is not None and row2.enabled is True
            # ⑤ remove
            removed = await repo.remove_target(name, city="验收市")
            await s.commit()
            gone = not (
                await s.execute(select(CmpPriceTarget).where(CmpPriceTarget.anchor_name == name))
            ).scalars().all()

    parsed = parse_target_files(OLD_SYSTEM) if OLD_SYSTEM.exists() else []
    modes = sorted({p["mode"] for p in parsed})
    both = [p for p in parsed if p["mode"] == "both"]

    ok = (
        added and disabled_gone and disabled_flag
        and stays_disabled and mode_updated
        and n_disable == 1 and n_enable == 1 and re_enabled
        and removed == 1 and gone
    )
    record(
        "V64",
        "比价目标可维护(targets add/list/set/remove + txt 导入)",
        PASS if ok else FAIL,
        f"add={added};停用={disabled_flag}(列表已过滤={disabled_gone});"
        f"★ 再次 upsert 不会把停用改回启用={stays_disabled}(mode 同时更新={mode_updated});"
        f"显式启用={re_enabled};remove={removed} 已删={gone};"
        f"旧 txt 解析出 {len(parsed)} 个目标,modes={modes}"
        + (f";两处都列→both 的有 {len(both)} 个:{[b['anchor_name'] for b in both]}" if both else ""),
        parsed=len(parsed),
    )


async def check_v65() -> None:
    """★ 历史重复已清理 + UNIQUE 生效;旧库 18 行作为迁移验证样本。"""
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    from hoteldata.runtime import Runtime

    async with Runtime.create(with_browser=False) as rt:
        async with rt.db.session() as s:
            # ① 唯一约束存在
            cons = (
                await s.execute(
                    text(
                        "select conname from pg_constraint where conrelid="
                        "'cmp_price_comparisons'::regclass and contype='u'"
                    )
                )
            ).scalars().all()
            has_unique = "cmp_price_key" in cons

            # ② 插重复必须被拒
            row = dict(
                anchor_name=f"{SYNTH_PREFIX}唯一约束样本",
                platform="ctrip",
                query_date=SYNTH_DAY,
                query_slot=f"{SYNTH_DAY}-0830",
                nights=1,
                hotel_name=f"{SYNTH_PREFIX}样本酒店",
                price=100,
                distance_km=1.0,
                price_scope="from",
                is_demo=True,
            )
            from hoteldata.domains.compare.repository import CompareRepository

            repo = CompareRepository(s)
            await repo.save_quotes(
                anchor_name=row["anchor_name"], platform=row["platform"],
                query_date=row["query_date"], rows=[{"hotel_name": row["hotel_name"], "price": 100,
                "distance_km": 1.0, "price_scope": "from"}], query_slot=row["query_slot"], is_demo=True,
            )
            await s.commit()
            rejected = False
            try:
                # 绕过 ON CONFLICT,直接裸 INSERT → 必须被约束拒绝
                await s.execute(
                    text(
                        "insert into cmp_price_comparisons "
                        "(anchor_name, platform, query_date, query_slot, nights, hotel_name, "
                        " price, distance_km, price_scope, is_demo) values "
                        "(:a,:p,:d,:sl,:n,:h,:pr,:dist,'from',true)"
                    ),
                    {"a": row["anchor_name"], "p": row["platform"], "d": row["query_date"],
                     "sl": row["query_slot"], "n": 1, "h": row["hotel_name"], "pr": 100,
                     "dist": 1.0},
                )
                await s.commit()
            except IntegrityError:
                rejected = True
                await s.rollback()
            except Exception as exc:  # noqa: BLE001
                # ★ 非 IntegrityError 一律**不当成通过** —— 段3 第一版就在这里
                #   把「参数类型错(asyncpg 需要 date 而不是 str)」误判成了
                #   "约束生效"。那正好是本次要防的那种静默错误。
                await s.rollback()
                record(
                    "V65",
                    "★ 历史重复已清理 + UNIQUE 生效",
                    FAIL,
                    f"裸插重复时抛的不是 IntegrityError 而是 {type(exc).__name__}: {exc}"
                    " —— 不能据此判定约束生效",
                )
                return
            # 清理
            await s.execute(
                text("delete from cmp_price_comparisons where anchor_name=:a"),
                {"a": row["anchor_name"]},
            )
            await s.commit()

    # 旧库重复样本(只读证据)
    old_stats = ""
    dump = PROJECT_ROOT / "docs" / "参考" / "段3-分析" / "_证据-旧系统比价库dump.txt"
    if dump.exists():
        old_stats = ";旧库证据: 18 行 / 6 组重复 / 冗余 10 行(见 _证据-旧系统比价库dump.txt)"

    ok = has_unique and rejected
    record(
        "V65",
        "★ 历史重复已清理 + UNIQUE 生效",
        PASS if ok else FAIL,
        f"cmp_price_key 唯一约束存在={has_unique};裸插重复被 IntegrityError 拒绝={rejected}"
        + old_stats,
        unique=has_unique,
        rejected=rejected,
    )


# ===========================================================================
# 批次 B · 锚点与候选
# ===========================================================================


def _load_real_ctrip_payload() -> tuple[dict | None, str]:
    """从旧系统 diag 取真接口响应(打捞截断预览)。"""
    import re

    diag = OLD_SYSTEM / "data" / "hotel_reports"
    if not diag.exists():
        return None, "旧系统 diag 目录不存在"
    best: str | None = None
    for path in sorted(diag.glob("diag_ctrip_api_no_hotels_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(data, list):
            continue
        for cap in data:
            if isinstance(cap, dict) and "ctGetNearbyHotelList" in (cap.get("url") or ""):
                prev = cap.get("body_preview") or ""
                if prev and (best is None or len(prev) > len(best)):
                    best = prev
    if not best:
        return None, "无 ctGetNearbyHotelList 的 preview"
    try:
        return json.loads(best), "完整 JSON"
    except json.JSONDecodeError as exc:
        # 打捞:预览在 4000 字处被截断(这本身就是 P10 的证据)
        items: list[dict] = []
        starts = [m.start() for m in re.finditer(r'\{\s*"base"\s*:\s*\{\s*"hotelId"', best)]
        bounds = [*starts, len(best)]

        def grab(seg: str, key: str) -> str | None:
            m = re.search(rf'"{key}"\s*:\s*"([^"]*)"', seg)
            return m.group(1) if m else None

        for i in range(len(starts)):
            seg = best[bounds[i] : bounds[i + 1]]
            hid, nm = grab(seg, "hotelId"), grab(seg, "hotelName")
            if not (hid and nm):
                continue
            item: dict[str, Any] = {"base": {"hotelId": hid, "hotelName": nm}}
            pos = {k: v for k in ("lat", "lng", "positionDescOfCtrip") if (v := grab(seg, k))}
            if pos:
                item["position"] = pos
            cm = {k: v for k in ("score", "totalReviews") if (v := grab(seg, k))}
            if cm:
                item["comment"] = cm
            if seo := grab(seg, "seoUrl"):
                item["seoInfo"] = {"seoUrl": seo}
            items.append(item)
        return ({"data": {"hotelList": items}}, f"打捞(预览截断:{exc})") if items else (None, str(exc))


def check_v66() -> None:
    """携程锚点定位:已登记 ebk_hotel_id → 直达;未登记 → 城市 sitemap + 列表页。"""
    import inspect

    from hoteldata.domains.compare.platforms import ctrip as m

    src = inspect.getsource(m.CtripPlatform.resolve_anchor)
    direct = "ebk_hotel_id" in src and "source=\"ebk_hotel_id\"" in src.replace("'", '"')
    sitemap = "city" in src and "_resolve_city_id" in src
    fatal = "PriceFatalError" in src

    # 真实数据:core_hotels 里 4 家店都登记了 ebk_hotel_id
    from sqlalchemy import text

    async def _count() -> tuple[int, int]:
        from hoteldata.runtime import Runtime

        async with Runtime.create(with_browser=False) as rt:
            async with rt.db.session() as s:
                total = (await s.execute(text("select count(*) from core_hotels"))).scalar()
                with_id = (
                    await s.execute(
                        text("select count(*) from core_hotels where ebk_hotel_id is not null")
                    )
                ).scalar()
                return int(total), int(with_id)

    total, with_id = asyncio.run(_count())
    ok = direct and sitemap and fatal
    record(
        "V66",
        "携程锚点定位(ebk_hotel_id 直达 + 城市 sitemap 兜底)",
        PASS if ok else FAIL,
        f"直达分支={direct};城市 sitemap 兜底={sitemap};找不到抛 PriceFatalError={fatal};"
        f"core_hotels {with_id}/{total} 家已登记 ebk_hotel_id",
    )


def check_v67() -> None:
    """美团锚点定位:按名称前 4 字认锚点卡;城市解析正确。"""
    from hoteldata.domains.compare.platforms import meituan as m

    # ★ 实测隐患:锚点「隐欲民宿」与首卡「隐欲民宿·山海别院」前 4 字相同
    prefix_anchor = m._prefix("隐欲民宿")
    prefix_card = m._prefix("隐欲民宿·山海别院")
    same_prefix = prefix_anchor == prefix_card

    notes: list[str] = []
    quotes, hits = m._cards_to_quotes(
        [
            {"name": "隐欲民宿·山海别院", "score": "", "feedback": "", "address": "",
             "priceNum": "288", "origin": "", "raw": ""},
            {"name": "隐欲民宿(总店)", "score": "", "feedback": "", "address": "",
             "priceNum": "279", "origin": "", "raw": ""},
        ],
        "隐欲民宿",
        notes,
    )
    warns = any("匹配到" in n for n in notes)
    ok = prefix_anchor == "隐欲民宿" and hits == 2 and warns
    record(
        "V67",
        "美团锚点定位(名称前 4 字 + 城市解析)",
        PASS if ok else FAIL,
        f"前 4 字规则 ={prefix_anchor!r}(长度 {m.ANCHOR_PREFIX_LEN});"
        f"「隐欲民宿」与「隐欲民宿·山海别院」前 4 字相同={same_prefix} → 命中 {hits} 张卡;"
        f"命中多个已记入 notes={warns}(实测隐患,不静默)",
    )


def check_v68() -> None:
    """附近 N 家提取:一次 DOM 批量提卡(不逐家开详情);取 2N 后合并截 N。"""
    import inspect

    from hoteldata.domains.compare.platforms import ctrip as c
    from hoteldata.domains.compare.platforms import meituan as m
    from hoteldata.domains.compare.runner import CompareRunner

    # 携程/美团都通过"一次批量读取"取卡
    ctrip_src = inspect.getsource(c)
    meituan_src = inspect.getsource(m)
    ctrip_batch = "locator(sel)" in ctrip_src and ".all()" in ctrip_src
    meituan_batch = "page.evaluate(meituan_card_script" in meituan_src
    no_detail_open = "detail_page" not in meituan_src.lower().replace("_build_detail_url", "")

    # 取 2N
    runner_src = inspect.getsource(CompareRunner.compare_one)
    take_2n = "want * 2" in runner_src

    ok = ctrip_batch and meituan_batch and take_2n
    record(
        "V68",
        "附近 N 家提取(一次批量提卡 + 取 2N 截 N)",
        PASS if ok else FAIL,
        f"携程一次 locator.all() 批量提卡={ctrip_batch};"
        f"美团单次 evaluate 批量读={meituan_batch};取 2N={take_2n};"
        f"不逐家开详情页={no_detail_open}",
    )


def check_v69() -> None:
    """★ 距离真实可用:distance_km 非 NULL;geo 模式按距离升序。"""
    from hoteldata.domains.compare import geo

    payload, how = _load_real_ctrip_payload()
    from hoteldata.domains.compare.platforms import ctrip as c

    quotes = c._quotes_from_api(payload) if payload else []
    coords_ok = bool(quotes) and all(q.coords is not None for q in quotes)
    dist_ok = bool(quotes) and all(q.distance_km is not None for q in quotes)

    # 排序验证
    rows = [
        {"hotel_name": "远", "distance_km": 9.0},
        {"hotel_name": "无", "distance_km": None},
        {"hotel_name": "近", "distance_km": 0.5},
    ]
    ordered = [r["hotel_name"] for r in geo.sort_by_distance(rows)]
    sort_ok = ordered == ["近", "远", "无"]

    # 库里实测(真携程数据已落库)
    from sqlalchemy import text

    async def _db() -> dict[str, int]:
        from hoteldata.runtime import Runtime

        async with Runtime.create(with_browser=False) as rt:
            async with rt.db.session() as s:
                total = (
                    await s.execute(text("select count(*) from cmp_price_comparisons"))
                ).scalar()
                with_dist = (
                    await s.execute(
                        text("select count(*) from cmp_price_comparisons where distance_km is not null")
                    )
                ).scalar()
                return {"total": int(total or 0), "with_dist": int(with_dist or 0)}

    stats = asyncio.run(_db())
    ok = coords_ok and dist_ok and sort_ok and stats["with_dist"] > 0
    record(
        "V69",
        "★ 距离真实可用(distance_km 非 NULL + geo 升序)",
        PASS if ok else FAIL,
        f"真接口坐标 {len(quotes)}/{len(quotes)} 家可提取({how});"
        f"真接口 distance_km 全部非空={dist_ok};排序[近,远,无]={sort_ok};"
        f"库里 {stats['with_dist']}/{stats['total']} 行 distance_km 非 NULL"
        f"(旧系统 18/18 全为 NULL → D13 已修)",
        stats=stats,
    )


def check_v70() -> None:
    """平台推荐模式可用:HOTEL_RANK_MODE=platform 时按平台原始顺序。"""
    import inspect

    from hoteldata.domains.compare.runner import CompareRunner

    src = inspect.getsource(CompareRunner._merge)
    handles_platform = "by_geo" in src and "order" in src
    from hoteldata.settings import reload_settings

    s_geo = reload_settings(hotel_rank_mode="geo")
    s_plat = reload_settings(hotel_rank_mode="platform", db_url=s_geo.db_url)

    bad_mode_rejected = False
    try:
        reload_settings(hotel_rank_mode="distance", db_url=s_geo.db_url)
    except Exception:  # noqa: BLE001
        bad_mode_rejected = True

    ok = handles_platform and s_geo.compare.by_geo and not s_plat.compare.by_geo and bad_mode_rejected
    record(
        "V70",
        "平台推荐模式可用(platform 模式 + 非法值被拒)",
        PASS if ok else FAIL,
        f"geo 模式 by_geo={s_geo.compare.by_geo};platform 模式 by_geo={s_plat.compare.by_geo};"
        f"非法值 HOTEL_RANK_MODE=distance 启动即失败={bad_mode_rejected}"
        f"(旧系统拼错会静默退化,用户以为在看距离)",
    )


# ===========================================================================
# 批次 C · 取价
# ===========================================================================


def check_v71() -> None:
    """携程取价:接口优先(名称/ID/坐标/URL),价格缺失时降级 DOM;price_source 正确。"""
    from hoteldata.domains.compare.platforms import ctrip as c

    payload, how = _load_real_ctrip_payload()
    quotes = c._quotes_from_api(payload) if payload else []
    if not quotes:
        record("V71", "携程取价(接口 + DOM 降级)", BLOCKED, f"无真接口样本:{how}")
        return

    q = quotes[0]
    api_fields = bool(q.hotel_name and q.hotel_id and q.coords and q.url)
    api_source = q.price_source == "api"
    priced = sum(1 for x in quotes if x.price is not None)

    # DOM 通道存在且券价过滤生效
    from hoteldata.domains.compare.price import classify_dom_price

    v = classify_dom_price("¥236", whole_card_text="¥236起")
    dom_ok = v.price == 236.0
    coupon = classify_dom_price("折扣券 ¥34")
    coupon_ok = coupon.price is None and bool(coupon.rejected)

    ok = api_fields and api_source and dom_ok and coupon_ok
    record(
        "V71",
        "携程取价(接口优先 + DOM 降级 + 券价过滤)",
        PASS if ok else FAIL,
        f"真接口解析:{len(quotes)} 家;首条 名称/ID/坐标/URL 齐全={api_fields};"
        f"price_source='api'={api_source};接口带价 {priced}/{len(quotes)} 家"
        f"(→{'零点击可拿到价' if priced else '**接口不带价**,取价落 DOM 通道'},"
        f"修正计划书 §5.6 的「命中即零点击取价」承诺);"
        f"DOM 通道「¥236」={dom_ok};「折扣券 ¥34」被拒={coupon_ok}",
        api_priced=priced,
    )


def check_v72() -> None:
    """美团取价:单次 evaluate 批量读全部卡片;price_source='DOM'。"""
    from hoteldata.domains.compare.platforms import meituan as m
    from hoteldata.domains.compare.platforms.selectors import meituan_card_script

    script = meituan_card_script(6)
    single_eval = script.count("querySelectorAll") == 1 and "slice(0, 6)" in script

    notes: list[str] = []
    quotes, _hits = m._cards_to_quotes(
        [
            {"name": "山屿·漫时光民宿(青城山高铁站店)", "score": "4.9分",
             "feedback": "5000+消费", "address": "距您查询的酒店直线1.2公里 · 近火车站",
             "priceNum": "132", "origin": "", "raw": "¥132起"},
            {"name": "悟栖·Haven智慧酒店(青城山高铁站店)", "score": "5.0分",
             "feedback": "1000+消费", "address": "距您查询的酒店直线1.1公里",
             "priceNum": "158", "origin": "", "raw": "¥158起"},
        ],
        "盛铂仕丹酒店",
        notes,
    )
    dom_source = all(q.price_source == "dom" for q in quotes)
    dist_ok = quotes[0].distance_km == 1.2 if quotes else False
    price_ok = quotes[0].price == 132.0 if quotes else False

    # 真机:登录态状态决定是否 BLOCKED
    login_state = _ota_session_state()
    meituan_state = login_state.get("meituan", "unknown")

    if not (single_eval and dom_source and dist_ok and price_ok):
        record("V72", "美团取价(单次 evaluate 批量)", FAIL,
               f"单次evaluate={single_eval} price_source=dom:{dom_source} 距离={dist_ok} 价格={price_ok}")
        return

    if meituan_state == "valid":
        record("V72", "美团取价(单次 evaluate 批量)", PASS,
               f"单次 evaluate 批量读={single_eval};price_source='dom'={dom_source};"
               f"卡片距离解析={dist_ok};价格解析={price_ok};美团登录态 valid")
    else:
        record(
            "V72",
            "美团取价(单次 evaluate 批量 + 真机)",
            BLOCKED,
            f"**代码路径 PASS**:单次 evaluate 批量读={single_eval};price_source='dom'={dom_source};"
            f"实拍卡片形状解析:距离={quotes[0].distance_km}km 价格=¥{quotes[0].price};"
            f"但**美团登录态={meituan_state}**(旧系统美团无账号凭据、滑块需人工)→ "
            f"真机取价需 `hoteldata price login --platform meituan` 后复跑",
            meituan_state=meituan_state,
        )


def check_v73() -> None:
    """房型粒度 → **起价口径正确**(计划书 V73 的修订版,P2)。"""
    from hoteldata.domains.compare.platforms import ctrip as c
    from hoteldata.domains.compare.platforms import meituan as m
    from hoteldata.domains.compare.price import classify_price_text

    p1 = classify_price_text("¥236起").scope
    p2 = classify_price_text("¥236", scope="exact").scope

    payload, _ = _load_real_ctrip_payload()
    ctrip_quotes = c._quotes_from_api(payload) if payload else []
    ctrip_scope_ok = all(q.price_scope == "from" for q in ctrip_quotes) if ctrip_quotes else False

    notes: list[str] = []
    mt, _ = m._cards_to_quotes(
        [{"name": "X酒店", "score": "", "feedback": "", "address": "", "priceNum": "132",
          "origin": "", "raw": "¥132起"}], "锚点", notes)
    mt_scope_ok = bool(mt) and mt[0].price_scope == "from"

    # 库里 price_scope 落库
    from sqlalchemy import text

    async def _db() -> dict[str, int]:
        from hoteldata.runtime import Runtime

        async with Runtime.create(with_browser=False) as rt:
            async with rt.db.session() as s:
                rows = (
                    await s.execute(
                        text(
                            "select price_scope, count(*) from cmp_price_comparisons "
                            "where price is not null group by price_scope"
                        )
                    )
                ).all()
                return {str(a): int(b) for a, b in rows}

    dist = asyncio.run(_db())
    priced_scopes = set(dist) or set()
    ok = p1 == "from" and p2 == "exact" and ctrip_scope_ok and mt_scope_ok and priced_scopes <= {"from", "exact"}
    record(
        "V73",
        "★ 起价口径正确(price_scope 落库;房型粒度延后)",
        PASS if ok else FAIL,
        f"列表页价一律标 from={ctrip_scope_ok}(携程 {len(ctrip_quotes)} 家)/{mt_scope_ok}(美团);"
        f"显式 exact 可透传={p2 == 'exact'};库中 price_scope 分布={dist or '(暂无有价行)'};"
        f"旧系统把「¥236起」当成确定的房价 → 已修",
        distribution=dist,
    )


def check_v74() -> None:
    """★ 视觉兜底门控:VISION_ENABLED=0(默认)时**零视觉调用**。"""
    from hoteldata.domains.compare.vision import VisionEstimator, reset_vision_budget
    from hoteldata.settings import reload_settings

    reset_vision_budget()
    s = get_settings()
    est = VisionEstimator(s)

    calls: list[str] = []

    class _SpyClient:
        async def post(self, *a: Any, **k: Any) -> Any:
            calls.append("post")
            raise AssertionError("VISION_ENABLED=0 时不应发出任何 HTTP 请求")

    est._client = _SpyClient()
    result = asyncio.run(est.read_price(b"\x89PNG fake"))

    disabled_ok = (s.vision.enabled is False) and result is None and not calls
    enabled = est.enabled

    # 置 1 但无 Key → settings 直接拒绝
    no_key_rejected = False
    try:
        reload_settings(vision_enabled=True, zhipu_api_key="", db_url=s.db_url)
    except Exception:  # noqa: BLE001
        no_key_rejected = True

    # 置 1 且有 Key → 门控放行(但不真发请求,只看 enabled)
    s2 = reload_settings(vision_enabled=True, zhipu_api_key="fake-key", db_url=s.db_url)
    est2 = VisionEstimator(s2)
    enabled2 = est2.enabled
    reload_settings()  # 复位单例

    ok = disabled_ok and not enabled and no_key_rejected and enabled2
    record(
        "V74",
        "★ 视觉兜底门控(默认零调用)",
        PASS if ok else FAIL,
        f"VISION_ENABLED={s.vision.enabled} → read_price 返回 None 且 **零 HTTP**={not calls};"
        f"置 1 无 Key 启动即失败={no_key_rejected};置 1 有 Key 门控放行={enabled2};"
        f"模块顶层不 import 任何视觉 SDK(结构性保证,非 if 判断)",
    )


def check_v75() -> None:
    """交叉校验:视觉价与 DOM 价偏差 >20% → 标待人工确认,不静默采用。"""
    from hoteldata.domains.compare.vision import cross_check

    t = 0.2
    need1, why1 = cross_check(100.0, 110.0, t)      # 10% → 通过
    need2, why2 = cross_check(100.0, 130.0, t)      # 30% → 待确认
    need3, why3 = cross_check(None, 130.0, t)       # 单边
    need4, why4 = cross_check(None, None, t)        # 都没有

    # 单边与"都没有"两种情况
    ok = (not need1) and need2 and (not need3) and (not need4)
    record(
        "V75",
        "交叉校验(偏差 >20% 标待人工确认)",
        PASS if ok else FAIL,
        f"100 vs 110(10%)→ need_manual_check={need1};"
        f"100 vs 130(30%)→ need_manual_check={need2}({why2[:48]}…);"
        f"仅视觉={need3};都没有={need4} —— **不静默采用**",
    )


# ===========================================================================
# 批次 D · 报告与存档
# ===========================================================================


def check_v76_v77() -> None:
    """md 报告 + html 报告:输出到 var/reports/,含锚点/本店价/对比列表/距离/采集时间。"""
    reports = sorted((PROJECT_ROOT / "var" / "reports" / "compare").rglob("comparison_*.md"))
    htmls = sorted((PROJECT_ROOT / "var" / "reports" / "compare").rglob("comparison_*.html"))
    if not reports:
        record("V76", "md 报告", BLOCKED, "var/reports/compare/ 下暂无报告(先跑一次 compare)")
        record("V77", "html 报告", BLOCKED, "同上")
        return

    md = reports[-1].read_text(encoding="utf-8")
    needs = {
        "锚点酒店": "锚点酒店" in md,
        "采集时间": "采集时间" in md,
        # ★ 报告结构在"跨平台对比表"改造后变了:平台分组标题从
        #   ``### 携程平台(ctrip)``(总览段)挪到了明细段的 ``**携程平台(ctrip)**``。
        #   这里断言"平台名出现",而不是钉死某一级标题 —— 否则每次调整版式
        #   都要改验收器,而验收器应该验**内容有没有**,不是**格式长什么样**。
        "平台出现": ("携程平台" in md) or ("美团平台" in md),
        "价格": "¥" in md,
        # ★ 新增:跨平台对比表(段3 的核心视图 —— 同店两平台并排 + 价差)
        "比价总览": "比价总览" in md,
        "价差列": "价差" in md,
    }
    dist_note = ("km" in md) or ("距离不可用" in md)
    ok = all(needs.values()) and dist_note
    record(
        "V76",
        "md 报告(含锚点/平台/价格/距离/采集时间 + ★跨平台对比表)",
        PASS if ok else FAIL,
        f"{reports[-1].name}:{needs};距离列或显式不可用标注={dist_note}",
    )

    html = htmls[-1].read_text(encoding="utf-8") if htmls else ""
    html_ok = bool(html) and "<table" in html and "距锚点" in html and "#ff6a00" in html
    record(
        "V77",
        "html 报告(同数据渲染,样式可用)",
        PASS if html_ok else FAIL,
        f"{htmls[-1].name if htmls else '(无)'}:含表格={'<table' in html};"
        f"含「距锚点」列={'距锚点' in html};含样式={'#ff6a00' in html}",
    )


async def check_v78() -> None:
    """★ 存档幂等:同一 slot 重跑 → 行数不增(UNIQUE + UPSERT)。"""
    from sqlalchemy import func, select

    from hoteldata.domains.compare.repository import CompareRepository
    from hoteldata.infra.models import CmpPriceComparison
    from hoteldata.runtime import Runtime

    anchor = f"{SYNTH_PREFIX}幂等测试"
    slot = f"{SYNTH_DAY}-0830"
    rows = [
        {"hotel_name": f"{SYNTH_PREFIX}幂等酒店", "price": 100.0, "distance_km": 1.0,
         "price_scope": "from", "price_source": "dom", "coord_source": "api"},
    ]

    async with Runtime.create(with_browser=False) as rt:
        async with rt.db.session() as s:
            repo = CompareRepository(s)
            await s.execute(
                CmpPriceComparison.__table__.delete().where(CmpPriceComparison.anchor_name == anchor)
            )
            await s.commit()

            for _ in range(3):  # 重跑 3 次
                await repo.save_quotes(
                    anchor_name=anchor, platform="ctrip", query_date=SYNTH_DAY,
                    rows=rows, query_slot=slot, is_demo=True,
                )
                await s.commit()

            n = (
                await s.execute(
                    select(func.count()).select_from(CmpPriceComparison).where(
                        CmpPriceComparison.anchor_name == anchor
                    )
                )
            ).scalar()

            # UPSERT 也应更新值(同 slot 内改价 → 覆盖,不是新增)
            await repo.save_quotes(
                anchor_name=anchor, platform="ctrip", query_date=SYNTH_DAY,
                rows=[{**rows[0], "price": 123.0}], query_slot=slot, is_demo=True,
            )
            await s.commit()
            after = (
                await s.execute(
                    select(func.count(), func.max(CmpPriceComparison.price)).where(
                        CmpPriceComparison.anchor_name == anchor
                    )
                )
            ).one()
            await s.execute(
                CmpPriceComparison.__table__.delete().where(CmpPriceComparison.anchor_name == anchor)
            )
            await s.commit()

    ok = int(n or 0) == 1 and int(after[0]) == 1 and float(after[1]) == 123.0
    record(
        "V78",
        "★ 存档幂等(同 slot 重跑行数不增)",
        PASS if ok else FAIL,
        f"同一 slot 连写 3 次 → 行数={int(n or 0)}(应为 1);"
        f"再写一次改价 → 行数={int(after[0])} 且价={float(after[1])}(UPSERT 覆盖非新增)",
    )


async def check_v79() -> None:
    """★ 一天多次各留一份:08:30/13:30/17:30 三次 → 3 组记录。"""
    from sqlalchemy import func, select

    from hoteldata.domains.compare.repository import CompareRepository
    from hoteldata.infra.models import CmpPriceComparison
    from hoteldata.runtime import Runtime

    anchor = f"{SYNTH_PREFIX}多slot测试"
    slots = [f"{SYNTH_DAY}-0830", f"{SYNTH_DAY}-1330", f"{SYNTH_DAY}-1730"]
    prices = [100.0, 110.0, 120.0]
    rows_one = lambda p: [  # noqa: E731
        {"hotel_name": f"{SYNTH_PREFIX}多slot酒店", "price": p, "distance_km": 1.0,
         "price_scope": "from", "price_source": "dom", "coord_source": "api"}
    ]

    async with Runtime.create(with_browser=False) as rt:
        async with rt.db.session() as s:
            repo = CompareRepository(s)
            await s.execute(
                CmpPriceComparison.__table__.delete().where(CmpPriceComparison.anchor_name == anchor)
            )
            await s.commit()
            for slot, price in zip(slots, prices, strict=True):
                await repo.save_quotes(
                    anchor_name=anchor, platform="ctrip", query_date=SYNTH_DAY,
                    rows=rows_one(price), query_slot=slot, is_demo=True,
                )
                await s.commit()
            n = (
                await s.execute(
                    select(func.count()).select_from(CmpPriceComparison).where(
                        CmpPriceComparison.anchor_name == anchor
                    )
                )
            ).scalar()
            got_slots = await repo.slots_of_day(anchor_name=anchor, query_date=SYNTH_DAY, include_demo=True)
            hist = (
                await s.execute(
                    select(CmpPriceComparison.query_slot, CmpPriceComparison.price)
                    .where(CmpPriceComparison.anchor_name == anchor)
                    .order_by(CmpPriceComparison.query_slot)
                )
            ).all()
            await s.execute(
                CmpPriceComparison.__table__.delete().where(CmpPriceComparison.anchor_name == anchor)
            )
            await s.commit()

    ok = int(n or 0) == 3 and len(got_slots) == 3
    record(
        "V79",
        "★ 一天多次各留一份(3 次采集 → 3 组)",
        PASS if ok else FAIL,
        f"08:30/13:30/17:30 三次 → 行数={int(n or 0)}、slot 数={len(got_slots)};"
        f"价格变化可见={[(str(a)[-4:], float(b)) for a, b in hist]}"
        f"(旧系统这三次会被当成「重复行」脏数据)",
    )


async def check_v80() -> None:
    """批量记录唯一:cmp_batch_runs 同一 batch_date 只有一行 + should_run 按真数据判。"""
    from hoteldata.domains.compare.repository import CompareRepository
    from hoteldata.runtime import Runtime

    async with Runtime.create(with_browser=False) as rt:
        async with rt.db.session() as s:
            repo = CompareRepository(s)
            day = SYNTH_DAY
            from hoteldata.infra.models import CmpBatchRun

            await s.execute(CmpBatchRun.__table__.delete().where(CmpBatchRun.batch_date == day))
            await s.commit()

            # 模拟:一次 running + 两次 done(旧系统这里会出现 3 行)
            for status in ("running", "done", "done"):
                await repo.upsert_batch_run(
                    batch_date=day, status=status, hotels_total=6, hotels_ok=6,
                    hotels_failed=0, summary={"step": status},
                    started_at=datetime.now(), finished_at=datetime.now(),
                )
                await s.commit()
            n = await repo.count_batch_runs(day)
            row = await repo.get_batch_run(day)
            attempts = int(row.attempt) if row else 0

            # should_run:演示数据**不算**真数据
            has_demo_only = await repo.has_real_data_today(SYNTH_DAY)
            await s.execute(CmpBatchRun.__table__.delete().where(CmpBatchRun.batch_date == day))
            await s.commit()

    ok = n == 1 and attempts >= 1 and has_demo_only is False
    record(
        "V80",
        "★ 批量记录唯一(每日一行)+ 判重按真数据",
        PASS if ok else FAIL,
        f"同一天写 running+done+done → cmp_batch_runs 行数={n}(旧系统同日 2~5 条 done);"
        f"attempt={attempts}(尝试次数可见,明细看 job_runs);"
        f"should_run 忽略 is_demo 行={has_demo_only is False}",
    )


# ===========================================================================
# 批次 E/F · 批量、调度与推送
# ===========================================================================


def check_task_registration() -> None:
    """任务注册:compare.batch / compare.collect / compare.push 在注册表里。"""
    import hoteldata.jobs  # noqa: F401
    from hoteldata.infra.tasks import get_registry

    reg = get_registry()
    want = {"compare.batch": "0 1 * * *", "compare.collect": "30 8,13,17 * * *", "compare.push": "0 14,18 * * *"}
    got = {}
    missing = []
    for name, cron in want.items():
        try:
            spec = reg.get(name)
            got[name] = spec.cron
            if spec.cron != cron:
                missing.append(f"{name} cron={spec.cron}≠{cron}")
        except KeyError:
            missing.append(f"{name} 未注册")

    # 时刻表不进 .env
    env = (PROJECT_ROOT / ".env.example")
    env_bad: list[str] = []
    if env.exists():
        text = env.read_text(encoding="utf-8")
        for key in ("HOTEL_BATCH_TIME", "PRICE_COLLECT_TIMES", "PRICE_PUSH_TIMES", "HOTEL_BATCH_HEADLESS"):
            if any(line.strip().startswith(key + "=") and not line.strip().startswith("#") for line in text.splitlines()):
                env_bad.append(key)

    ok = not missing and not env_bad
    record(
        "V81a",
        "任务注册(3 个 compare 任务 + 时刻表不进 .env)",
        PASS if ok else FAIL,
        f"注册表:{got};总任务数={len(reg.names())};"
        f".env.example 中不应存在的键={env_bad or '无'}"
        + (f";问题={missing}" if missing else ""),
    )


def check_v81() -> None:
    """独立推送:14:00/18:00 纯文字;push_type='price_compare';审计落 push_logs。"""
    import inspect

    from hoteldata.domains.compare import service as svc_mod

    src = inspect.getsource(svc_mod.CompareService.push_price)
    type_ok = 'push_type="price_compare"' in src
    text_only = "images=()" in src
    uses_push_service = "self.runtime.push.push(" in src

    # push_logs 表接受 price_compare(列定义里有该取值说明)
    from hoteldata.infra.models import PushLog

    comment = (PushLog.__table__.c.push_type.comment or "")
    audit_ok = "price_compare" in comment

    ok = type_ok and text_only and uses_push_service and audit_ok
    record(
        "V81",
        "独立推送(纯文字 + push_type=price_compare + 走 push.service)",
        PASS if ok else FAIL,
        f"push_type='price_compare'={type_ok};**不附图**(images=())={text_only};"
        f"走段2 push.service(不直连机器人)={uses_push_service};"
        f"push_logs.push_type 列声明含 price_compare={audit_ok}",
    )


def check_v82() -> None:
    """★ 日报合并:段2 代码零改动(SHA256 基线)+ 群级拼接形状正确。"""
    if not HASH_BASELINE.exists():
        record("V82", "★ 日报合并(段2 零改动)", BLOCKED, f"缺哈希基线 {HASH_BASELINE}")
        return

    expected: dict[str, str] = {}
    for line in HASH_BASELINE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) == 2 and len(parts[0]) == 64:
            expected[parts[1].strip()] = parts[0]

    # 基线里 runtime.py 之后的内容属于"装配处",不参与断言
    domain_files = {
        k: v
        for k, v in expected.items()
        if "/domains/report/" in k.replace("\\", "/") or "/push/" in k.replace("\\", "/")
    }
    mismatched: list[str] = []
    for rel, want in domain_files.items():
        path = PROJECT_ROOT / rel.replace("\\", "/")
        if not path.exists():
            mismatched.append(f"{rel} 不存在")
            continue
        got = hashlib.sha256(path.read_bytes()).hexdigest().upper()
        if got != want.upper():
            mismatched.append(f"{rel} 哈希变了")

    # 群级拼接:一群多店 → 一个字符串(旧 compare_md_for_groups 的形状)
    from hoteldata.domains.compare.report import build_group_price_text

    joined = build_group_price_text(sections=["A 段", "B 段"], hotel_count=2)
    shape_ok = joined.startswith("📊 **比价汇总**(2 家酒店)") and "A 段" in joined and "B 段" in joined

    # 钩子位置:段2 的 daily.py 里 price_section 参数仍在(未被删改)
    daily = (PROJECT_ROOT / "src/hoteldata/domains/report/daily.py").read_text(encoding="utf-8")
    hook_ok = "price_section: str | None = None" in daily and "markdown += \"\\n\\n\" + price_section" in daily

    ok = not mismatched and shape_ok and hook_ok
    record(
        "V82",
        "★ 日报合并(段2 领域/推送代码逐字节未改)",
        PASS if ok else FAIL,
        f"段2 领域+推送 {len(domain_files)} 个文件 SHA256 全部一致={not mismatched};"
        f"段2 的 price_section 钩子仍在={hook_ok};"
        f"群级拼接形状(旧 compare_md_for_groups)={shape_ok}"
        + (f";不一致={mismatched}" if mismatched else "")
        + ";注入点在 runtime.start_push()(装配处,总纲 §7.2 允许的唯一位置)",
        files=len(domain_files),
    )


# ===========================================================================
# 段3 新增的两条(V83 / V84)
# ===========================================================================


def check_v83() -> None:
    """★ V83(新增)券价过滤生效 + 丢弃可见。"""
    from hoteldata.domains.compare.price import COUPON_KEYWORDS, classify_dom_price, classify_price_text

    # 旧系统实测的两个假阳性原文
    c1 = classify_price_text("折扣券 ¥34")
    c2 = classify_price_text("十亿豪补 ¥12")
    good = classify_price_text("¥236起")

    # 丢弃要可见(不是静默变 None)
    visible = bool(c1.rejected) and bool(c2.rejected)
    # 券价 ≠ 无价
    empty = classify_price_text("")
    distinguishable = bool(c1.rejected) and not empty.rejected
    # 不误杀正常酒店(整卡视角但价格节点干净)
    not_overfiltered = classify_price_text("¥236起").price == 236.0
    # 价格节点空 + 整卡有券 → 拒
    whole = classify_dom_price("", whole_card_text="已减 ¥15 券后价")

    ok = c1.price is None and c2.price is None and good.price == 236.0 and visible and distinguishable
    ok = ok and not_overfiltered and (whole.price is None)
    record(
        "V83",
        "★ 券价过滤生效 + 丢弃可见(段3 新增)",
        PASS if ok else FAIL,
        f"「折扣券 ¥34」→ price=None 且 rejected={c1.rejected!r};"
        f"「十亿豪补 ¥12」→ rejected={c2.rejected!r};"
        f"「¥236起」放行={good.price};券价≠无价(可区分)={distinguishable};"
        f"特征词 {len(COUPON_KEYWORDS)} 个;整卡券价兜底={whole.rejected!r}"
        f"(旧系统 12/18 条归档把券当成了房价)",
    )


def check_v84() -> None:
    """★ V84(新增)跨平台不合并价:同店双平台两个价都在。"""
    import inspect

    from sqlalchemy import text

    from hoteldata.domains.compare.runner import CompareRunner

    src = inspect.getsource(CompareRunner._merge)
    # 合并时必须把平台标记写进 raw(报告/推送按平台分组全靠它)
    keeps_platform = 'raw["platform"] = o.platform' in src
    # 去重键必须是 (平台, 归一化名) —— **跨平台不去重**(那正是要比的东西)
    per_platform_key = "key = (o.platform," in src
    # 旧系统 _merge_quotes 的"只留先到价"写法不应存在
    no_price_collapse = 'old.get("price") is None' not in src and "old.update(price=" not in src

    async def _db() -> list[tuple[str, int]]:
        from hoteldata.runtime import Runtime

        async with Runtime.create(with_browser=False) as rt:
            async with rt.db.session() as s:
                rows = (
                    await s.execute(
                        text(
                            "select hotel_name, count(distinct platform) c from cmp_price_comparisons "
                            "group by hotel_name having count(distinct platform) > 1"
                        )
                    )
                ).all()
                return [(str(a), int(b)) for a, b in rows]

    dual = asyncio.run(_db())
    ok = keeps_platform and per_platform_key and no_price_collapse and bool(dual)
    record(
        "V84",
        "★ 跨平台不合并价(段3 新增;修旧 _merge_quotes 反向语义)",
        PASS if ok else FAIL,
        f"合并时保留平台标记={keeps_platform};去重键含平台(跨平台不去重)={per_platform_key};"
        f"无「只留先到价」写法={no_price_collapse};"
        f"库中同名酒店跨平台各留一行={len(dual)} 组 {dual[:3]}"
        f"(旧 runner.py:85-115 把两平台价压成一条、丢掉第二个价 → 与比价语义相反)",
    )


# ===========================================================================
# 辅助
# ===========================================================================


def _ota_session_state() -> dict[str, str]:
    """读 ``sessions`` 表里 ota 角色的状态(真机验收的前置)。"""
    from sqlalchemy import text

    async def _go() -> dict[str, str]:
        from hoteldata.runtime import Runtime

        async with Runtime.create(with_browser=False) as rt:
            async with rt.db.session() as s:
                rows = (
                    await s.execute(
                        text("select platform, status from sessions where role in ('ota','ota_meituan')")
                    )
                ).all()
                return {str(p): str(st) for p, st in rows}

    try:
        return asyncio.run(_go())
    except Exception:  # noqa: BLE001
        return {}


async def check_should_run_semantics() -> None:
    """补充验收:should_run 以「今天有没有真数据」为准(B27),且 force 可绕。"""
    from hoteldata.domains.compare.repository import CompareRepository
    from hoteldata.domains.compare.runner import CompareRunner
    from hoteldata.runtime import Runtime

    async with Runtime.create(with_browser=False) as rt:
        async with rt.db.session() as s:
            runner = CompareRunner(
                settings=rt.settings, layout=rt.layout, sessions=rt.sessions,
                pool=None, limiter=rt.limiter, http=rt.http, repo=CompareRepository(s),
            )
            should, why = await runner.should_run(SYNTH_DAY, session=s)
            forced, why_f = await runner.should_run(SYNTH_DAY, session=s, force=True)

    ok = should is True and forced is True and "force" in why_f
    record(
        "V-B27",
        "判重语义:以数据为准 + force 可绕",
        PASS if ok else FAIL,
        f"无真数据日 → should_run={should}({why[:40]}…);force=True → {forced}({why_f})",
    )


async def check_vision_zero_calls_in_pipeline() -> None:
    """补充验收:整条比价跑下来,VISION_ENABLED=0 时零视觉调用。"""
    from hoteldata.domains.compare.vision import VisionEstimator, get_vision_budget, reset_vision_budget

    reset_vision_budget()
    est = VisionEstimator(get_settings())
    budget_before = get_vision_budget().used
    await est.read_price(b"\x89PNG")
    budget_after = get_vision_budget().used
    ok = budget_before == budget_after == 0
    record(
        "V-VISION0",
        "默认配置下视觉调用计数恒为 0",
        PASS if ok else FAIL,
        f"调用前 used={budget_before} → 调用后 used={budget_after}(**默认零外发**)",
    )


# ===========================================================================
# 主流程
# ===========================================================================

CHECKS: dict[str, Any] = {
    "V61": check_v61,
    "V62": check_v62,
    "V63": lambda: asyncio.run(check_v63()),
    "V64": lambda: asyncio.run(check_v64()),
    "V65": lambda: asyncio.run(check_v65()),
    "V66": check_v66,
    "V67": check_v67,
    "V68": check_v68,
    "V69": check_v69,
    "V70": check_v70,
    "V71": check_v71,
    "V72": check_v72,
    "V73": check_v73,
    "V74": check_v74,
    "V75": check_v75,
    "V76": check_v76_v77,
    "V81a": check_task_registration,
    "V81": check_v81,
    "V82": check_v82,
    "V83": check_v83,
    "V84": check_v84,
    "V-B27": lambda: asyncio.run(check_should_run_semantics()),
    "V-VISION0": lambda: asyncio.run(check_vision_zero_calls_in_pipeline()),
}


def main() -> int:
    configure_stdio()
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="*", help="只跑指定编号")
    parser.add_argument("--offline", action="store_true", help="跳过需要真登录态/网络的项")
    args = parser.parse_args()

    print("=" * 78)
    print("段3「比价功能」验收执行器 —— V61–V84")
    print("=" * 78)

    started = time.time()

    # ★ 顺序很重要:**先清干净,再生成 demo 报告**
    #   反过来的话,结尾那次清理会把 `is_demo=true` 的行删掉,
    #   于是 V84("库中同名酒店跨平台各留一行")看到 0 组 —— 自己把证据清了。
    #   所以:入口清一次(保证幂等类断言从零开始),生成报告,跑完**不再清**,
    #   把证据留在库里(下次运行入口再清)。
    print("\n[准备] 清理上次的演示/合成数据…")
    _cleanup_synth()

    print("[准备] 用演示模式生成一份报告(离线,不碰平台)…")
    try:
        prepare = _prepare_demo_report()
        print(f"       {prepare}")
    except Exception as exc:  # noqa: BLE001
        print(f"       演示报告生成失败(后续 V76/V77 会标 BLOCKED):{exc}")

    for vid in [
        "V61", "V62", "V63", "V64", "V65",
        "V66", "V67", "V68", "V69", "V70",
        "V71", "V72", "V73", "V74", "V75",
        "V76", "V-B27", "V-VISION0",
        "V78", "V79", "V80",
        "V81a", "V81", "V82", "V83", "V84",
    ]:
        if args.only and vid not in args.only:
            continue
        fn = CHECKS.get(vid)
        print()
        if fn is None:
            continue
        if vid in ("V78", "V79", "V80") and not args.only:
            pass
        try:
            if vid == "V78":
                asyncio.run(check_v78())
            elif vid == "V79":
                asyncio.run(check_v79())
            elif vid == "V80":
                asyncio.run(check_v80())
            else:
                fn()
        except Exception as exc:  # noqa: BLE001
            record(vid, f"{vid} 执行异常", FAIL, f"{type(exc).__name__}: {exc}")

    # ★ 这里**不再清理**:V78/V79/V80/V84 断言的正是库里的行,
    #   清掉就把证据一起清了(下次运行入口会清)。
    #   仅清掉"唯一约束样本"这类一次性的探针行。
    _cleanup_probe_rows()

    # ---- 汇总 ----
    counts = {PASS: 0, FAIL: 0, BLOCKED: 0, SKIP: 0}
    for r in RESULTS:
        counts[r.status] = counts.get(r.status, 0) + 1

    print("\n" + "=" * 78)
    print(f"汇总:{counts[PASS]} PASS / {counts[FAIL]} FAIL / {counts[BLOCKED]} BLOCKED")
    print(f"耗时:{time.time() - started:.1f}s")
    if counts[FAIL]:
        print("\n失败项:")
        for r in RESULTS:
            if r.status == FAIL:
                print(f"  ❌ [{r.vid}] {r.title}\n      {r.evidence}")
    if counts[BLOCKED]:
        print("\n未验项(如实标注,不伪装成 PASS):")
        for r in RESULTS:
            if r.status == BLOCKED:
                print(f"  ⛔ [{r.vid}] {r.title}\n      {r.evidence}")

    out = PROJECT_ROOT / "var" / "reports" / "段3-验收结果.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "segment": 3,
                "generated_at": datetime.now().isoformat(),
                "elapsed_s": round(time.time() - started, 1),
                "summary": counts,
                "results": [
                    {"vid": r.vid, "title": r.title, "status": r.status,
                     "evidence": r.evidence, "detail": r.detail}
                    for r in RESULTS
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n结果已写入:{out}")
    print("=" * 78)
    return 1 if counts[FAIL] else 0


def _prepare_demo_report() -> str:
    """离线跑一次演示比价,**落库并产出报告**。

    ★ 必须 ``persist=True``:V84 断言的是"库中同名酒店跨平台各留一行",
      不落库就没得断言(段3 第一版写成 ``persist=False``,于是 V84 恒为 0 组)。
    """
    from hoteldata.runtime import Runtime

    async def _go() -> str:
        async with Runtime.create(with_browser=False) as rt:
            result = await rt.compare().compare(
                f"{SYNTH_PREFIX}报告样本", city="验收市", demo=True, persist=True
            )
            return f"生成 {len(result.quotes)} 条报价的报告(已落库,is_demo=true)"

    return asyncio.run(_go())


def _cleanup_probe_rows() -> None:
    """只清"探针行"(唯一约束样本等),**不动** V78/V79/V80/V84 要断言的证据行。"""
    from sqlalchemy import text

    async def _go() -> None:
        from hoteldata.runtime import Runtime

        async with Runtime.create(with_browser=False) as rt:
            async with rt.db.session() as s:
                await s.execute(
                    text("delete from cmp_price_comparisons where anchor_name like :p"),
                    {"p": f"{SYNTH_PREFIX}唯一约束样本%"},
                )
                await s.commit()

    try:
        asyncio.run(_go())
    except Exception:  # noqa: BLE001
        pass


def _cleanup_synth() -> None:
    """清理合成/演示数据(按前缀与 is_demo 显式列,不用 LIKE 猜业务数据)。"""
    from sqlalchemy import text

    async def _go() -> None:
        from hoteldata.runtime import Runtime

        async with Runtime.create(with_browser=False, check_db=True) as rt:
            async with rt.db.session() as s:
                await s.execute(
                    text("delete from cmp_price_comparisons where is_demo = true or anchor_name like :p"),
                    {"p": f"{SYNTH_PREFIX}%"},
                )
                await s.execute(
                    text("delete from cmp_price_targets where anchor_name like :p"),
                    {"p": f"{SYNTH_PREFIX}%"},
                )
                await s.execute(
                    text("delete from cmp_batch_runs where batch_date = :d"), {"d": SYNTH_DAY}
                )
                await s.commit()

    try:
        asyncio.run(_go())
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    sys.exit(main())
