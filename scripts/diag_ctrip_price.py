"""携程取价链路诊断 —— 页面**上到底有没有价格**?

为什么需要这个脚本
==================

``hoteldata compare`` 现在能取到 20 家附近酒店、距离全对(0.10/0.94/1.00 km),
但价格列恒为「—」。而接口 ``ctGetNearbyHotelList`` 的回应是::

    money.price    = ""
    money.priceStr = "?"
    money.minPriceInfo.minpriceroom.avgprice = null

``priceStr = "?"`` 是携程明确的「**价格未知**」标记。问题是:**为什么未知?**

两种可能,必须分清楚,因为对策完全不同:

* **(A) 页面能显示价,但接口不给** → 对策:取价走 **DOM**(页面上的 ``¥`` 文本);
* **(B) 连页面都不显示价**(该店这些日期无价/需更深的登录) → 对策:换日期/换店/接受现状。

本脚本把两者**一次分开**:带登录态打开详情页,截图 + 把页面上所有 ``¥数字`` 抓出来。

用法::

    .venv\\Scripts\\python.exe scripts\\diag_ctrip_price.py --name 隐欲民宿
    .venv\\Scripts\\python\\Scripts\\... --hotel-id 127826116 --city 莱州
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except (AttributeError, ValueError):  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "var" / "reports" / "diag"

#: 页面上找价格用的正则:¥/￥ 后面 2~6 位数字
PRICE_RE = re.compile(r"[¥￥]\s*(\d{2,6})")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="隐欲民宿", help="锚点酒店名(用于取 ebk_hotel_id)")
    parser.add_argument("--hotel-id", default="", help="直接给携程 hotelId(优先)")
    parser.add_argument("--city", default="莱州")
    parser.add_argument("--checkin", default="", help="入住日期 YYYY-MM-DD(默认今天)")
    parser.add_argument("--nights", type=int, default=1)
    parser.add_argument("--headless", action="store_true", help="无头(默认有头,便于肉眼看)")
    args = parser.parse_args()

    from sqlalchemy import select

    from hoteldata.infra.models import Hotel
    from hoteldata.runtime import Runtime

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today()
    checkin = args.checkin or today.isoformat()
    checkout = (date.fromisoformat(checkin) + timedelta(days=args.nights)).isoformat()

    async with Runtime.create(with_browser=True, with_scheduler=False) as rt:
        if args.headless:
            object.__setattr__(rt.settings, "hotel_headless", True) if False else None

        hotel_id = args.hotel_id.strip()
        if not hotel_id:
            async with rt.db.session() as s:
                row = (
                    await s.execute(select(Hotel).where(Hotel.name == args.name))
                ).scalars().first()
            if row is None or not row.ebk_hotel_id:
                print(f"✗ 库里找不到「{args.name}」的 ebk_hotel_id;用 --hotel-id 直接给")
                return 1
            hotel_id = str(row.ebk_hotel_id)

        handle = rt.sessions.handle("ctrip", "ota", "ctrip")
        state = handle.storage_state_path()
        print("=" * 74)
        print("携程取价链路诊断")
        print("=" * 74)
        print(f"  hotelId   : {hotel_id}")
        print(f"  日期      : {checkin} → {checkout}({args.nights} 晚)")
        print(f"  登录态    : {state}")
        print(f"  文件存在  : {state.exists()}  ({state.stat().st_size if state.exists() else 0} B)")
        print()

        url = (
            f"https://hotels.ctrip.com/hotels/detail/?hotelId={hotel_id}"
            f"&checkin={checkin}&checkout={checkout}"
        )
        print(f"  打开: {url}\n")

        captured: list[str] = []

        async with rt.browser.page_session(handle) as (_b, _c, page):
            async def _on_response(resp: object) -> None:
                u = getattr(resp, "url", "") or ""
                if "ctGetNearbyHotelList" in u or "getHotelDetail" in u or "price" in u.lower():
                    captured.append(u)

            page.on("response", _on_response)
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)

            # 等价格渲染:页面上出现 ¥数字
            print("  等页面渲染价格(最多 25 秒)…")
            seen = False
            for i in range(25):
                await asyncio.sleep(1.0)
                try:
                    body = await page.locator("body").inner_text(timeout=5000)
                except Exception:  # noqa: BLE001
                    body = ""
                prices = PRICE_RE.findall(body or "")
                if prices:
                    seen = True
                    print(f"  ✓ 第 {i + 1} 秒页面上出现价格:{prices[:8]}")
                    break
            if not seen:
                print("  ✗ 25 秒内页面上**始终没有出现 ¥数字**")

            # 截图留证
            shot = OUT_DIR / f"ctrip_detail_{hotel_id}_{checkin}.png"
            try:
                await page.screenshot(path=str(shot), full_page=False)
                print(f"  截图: {shot}")
            except Exception as exc:  # noqa: BLE001
                print(f"  截图失败: {exc}")

            # 抓页面全文里的全部价格 + 关键区块
            try:
                body = await page.locator("body").inner_text(timeout=8000)
            except Exception:  # noqa: BLE001
                body = ""
            all_prices = PRICE_RE.findall(body or "")
            uniq = sorted({int(p) for p in all_prices})
            print()
            print(f"  页面 ¥数字:共 {len(all_prices)} 处,去重 {len(uniq)} 个 → {uniq[:25]}")

            # 房型列表区块(有价时价格通常在这)
            for sel in ("div[class*='room']", "div[class*='Room']", "table", "div[class*='price']"):
                try:
                    n = await page.locator(sel).count()
                    if n:
                        print(f"  选择器 {sel:26s} 命中 {n}")
                except Exception:  # noqa: BLE001
                    pass

            txt_path = OUT_DIR / f"ctrip_detail_{hotel_id}_{checkin}.txt"
            txt_path.write_text(body or "", encoding="utf-8")
            print(f"  页面全文已存: {txt_path}")

            print()
            print(f"  旁听到的相关响应 {len(captured)} 条:")
            for u in captured[:8]:
                print(f"    · {u[:110]}")

        print()
        print("=" * 74)
        if seen:
            print("结论:★ 页面**有价**,接口不给 → 取价应走 DOM 通道(段3 目前接口优先、")
            print("      DOM 仅在接口解析不出条目时才用;需要改成「接口无价也回退 DOM」)")
        else:
            print("结论:页面**也没有价** → 不是取价通道问题,而是该店/该日期在携程前台")
            print("      无可售价格(或需要更深登录)。建议换日期/换店复测。")
        print("=" * 74)
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
