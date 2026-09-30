"""DOM 取价选择器验证 —— 携程详情页上的价格到底在哪个容器?

背景
====

实测结论(见 `diag_ctrip_price_by_hotel.py`):

* 接口 ``ctGetNearbyHotelList`` **结构性不带价** —— 4 个锚点 × 20 家 = **80 家全是** ``priceStr="?"``;
* 但**详情页上有价**(盛铂仕丹 3 个 ¥数字、嫣杭 1 个)。

所以取价**必须走 DOM**。问题是:**旧系统那批选择器现在还命中吗?**

旧选择器(``comparator/platforms/ctrip.py:171-174``)::

    div[class*='recommendCard_cardWrap']
    div[class*='recommendCard']

含 ``recommendCard`` 的类名是**旧的 React 实现**;段3 的实测里,
详情页已经出现了新的 ``div[class*='price']``(23 个命中)与 ``table``(5 个)。

本脚本**枚举候选选择器**并报命中数 + 样例文本,一次看出**该用哪个**。

用法::

    .venv\\Scripts\\python.exe scripts\\diag_ctrip_dom_selectors.py --hotel-id 60620823
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except (AttributeError, ValueError):  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parents[1]

#: 候选选择器:(标签, 选择器, 是否期望含价格)
CANDIDATES: tuple[tuple[str, str], ...] = (
    # ---- 旧系统的"附近酒店卡片"(可能已失效) ----
    ("旧:卡片 Wrap", "div[class*='recommendCard_cardWrap']"),
    ("旧:卡片", "div[class*='recommendCard']"),
    ("旧:Tab", "div[class*='nearby-tabs']"),
    # ---- 详情页房型/报价区(新版) ----
    ("房型行", "div[class*='room']"),
    ("房型行(大写)", "div[class*='Room']"),
    ("价格容器", "div[class*='price']"),
    ("价格容器(大写)", "div[class*='Price']"),
    ("表格", "table"),
    ("表格行", "table tr"),
    # ---- 通用:直接含 ¥ 的元素 ----
    ("含¥元素", "text=/[¥￥]\\s*\\d{2,6}/"),
)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hotel-id", default="60620823", help="携程 hotelId(默认盛铂仕丹:详情页有价)")
    parser.add_argument("--checkin", default="")
    parser.add_argument("--dump", action="store_true", help="把命中样例文本打印出来")
    args = parser.parse_args()

    from hoteldata.runtime import Runtime

    today = date.today()
    checkin = args.checkin or today.isoformat()
    checkout = (date.fromisoformat(checkin) + timedelta(days=1)).isoformat()

    print("=" * 78)
    print(f"携程 DOM 取价选择器验证(hotelId={args.hotel_id},{checkin})")
    print("=" * 78)

    async with Runtime.create(with_browser=True, with_scheduler=False) as rt:
        handle = rt.sessions.handle("ctrip", "ota", "ctrip")
        url = (
            f"https://hotels.ctrip.com/hotels/detail/?hotelId={args.hotel_id}"
            f"&checkin={checkin}&checkout={checkout}"
        )
        async with rt.browser.page_session(handle) as (_b, _c, page):
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            print("  等渲染 20 秒…")
            await asyncio.sleep(20)

            print()
            print(f"  {'标签':22s} {'命中':>6s}   样例")
            print("  " + "-" * 72)
            for label, sel in CANDIDATES:
                try:
                    loc = page.locator(sel)
                    n = await loc.count()
                except Exception as exc:  # noqa: BLE001
                    print(f"  {label:22s} {'ERR':>6s}   {str(exc)[:60]}")
                    continue
                sample = ""
                if n and args.dump:
                    try:
                        first = loc.first
                        txt = (await first.inner_text(timeout=3000)) or ""
                        sample = " ".join(txt.split())[:64]
                    except Exception:  # noqa: BLE001
                        sample = "(取文本失败)"
                print(f"  {label:22s} {n:>6d}   {sample}")

            # ★ 关键:把页面上每个含 ¥ 的元素及其容器路径打出来
            print()
            print("  含 ¥ 的元素(最多 12 个,含文本与最近的带 class 祖先):")
            script = """() => {
                const out = [];
                const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                const seen = new Set();
                let node;
                while ((node = walk.nextNode()) && out.length < 12) {
                    const t = (node.textContent || '').trim();
                    if (!/[¥￥]\\s*\\d{2,6}/.test(t)) continue;
                    const el = node.parentElement;
                    if (!el || seen.has(el)) continue;
                    seen.add(el);
                    // 向上找 3 层带 class 的祖先
                    const chain = [];
                    let cur = el;
                    for (let i = 0; i < 4 && cur; i++) {
                        const cls = (cur.className && typeof cur.className === 'string')
                            ? cur.className.split(/\\s+/).filter(Boolean).slice(0,3).join('.') : '';
                        if (cls) chain.push(cur.tagName.toLowerCase() + '.' + cls);
                        cur = cur.parentElement;
                    }
                    out.push({text: t.slice(0, 40), chain: chain});
                }
                return out;
            }"""
            try:
                hits = await page.evaluate(script)
            except Exception as exc:  # noqa: BLE001
                hits = []
                print(f"    (evaluate 失败: {exc})")
            if not hits:
                print("    (页面上没有找到含 ¥ 的文本)")
            for h in hits:
                print(f"    · {h['text']!r}")
                for c in h["chain"]:
                    print(f"        ← {c}")

            # 截图
            out = ROOT / "var" / "reports" / "diag"
            out.mkdir(parents=True, exist_ok=True)
            shot = out / f"ctrip_dom_{args.hotel_id}_{checkin}.png"
            try:
                await page.screenshot(path=str(shot), full_page=True)
                print(f"\n  整页截图: {shot}")
            except Exception as exc:  # noqa: BLE001
                print(f"\n  截图失败: {exc}")

    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
