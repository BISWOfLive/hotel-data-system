"""对照实验:接口对**可预订**与**不可预订**酒店分别返回什么?

背景(实测截图确证)
==================

「隐欲民宿」的携程详情页**明确写着「本酒店目前不接受预订」**,页面 **0 个 ¥ 数字**,
接口返回 ``money.priceStr = "?"``。

关键问题:**这个 "?" 是"这家店不可订"的特例,还是接口压根不给价?**

* 若换一家**可预订**的店仍是 ``"?"`` → 接口**结构性不带价**,取价必须走 DOM;
* 若可预订的店给出 ``"¥236"`` → 段3 现有实现**本来就是对的**,
  之前显示「—」纯粹是锚点自己不可订。

本脚本对**多个锚点**打同一个接口,把 ``priceStr`` 逐个打出来。

用法::

    .venv\\Scripts\\python.exe scripts\\diag_ctrip_price_by_hotel.py
"""

from __future__ import annotations

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

#: 待测锚点:库里已登记 ebk_hotel_id 的 4 家店(都走"直达"路径,排除城市解析干扰)
CANDIDATES: tuple[tuple[str, str], ...] = (
    ("隐欲民宿", "127826116"),
    ("盛铂仕丹酒店(青城山景区高铁站店)", "60620823"),
    ("嫣杭民宿", "128217893"),
    ("静荷民宿(蒙自市政府店)", "132636475"),
)


async def probe_one(rt: object, name: str, hotel_id: str, checkin: str, checkout: str) -> dict:
    """打开详情页 → 抓 ctGetNearbyHotelList → 返回 (锚点价格情况, 附近酒店价格情况)。"""
    handle = rt.sessions.handle("ctrip", "ota", "ctrip")  # type: ignore[attr-defined]
    captured: list[dict] = []
    result: dict = {"anchor": name, "hotel_id": hotel_id, "page_prices": 0, "api": None}

    async with rt.browser.page_session(handle) as (_b, _c, page):  # type: ignore[attr-defined]

        async def _on_response(resp: object) -> None:
            url = getattr(resp, "url", "") or ""
            if "ctGetNearbyHotelList" not in url:
                return
            try:
                body = await resp.json()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                body = None
            captured.append({"url": url, "json": body})

        page.on("response", _on_response)
        url = (
            f"https://hotels.ctrip.com/hotels/detail/?hotelId={hotel_id}"
            f"&checkin={checkin}&checkout={checkout}"
        )
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:  # noqa: BLE001
            result["error"] = f"导航失败: {exc}"
            return result

        # 等接口 + 等页面渲染
        for _ in range(20):
            await asyncio.sleep(1.0)
            if captured:
                break
        await asyncio.sleep(3.0)

        try:
            body_text = await page.locator("body").inner_text(timeout=8000)
        except Exception:  # noqa: BLE001
            body_text = ""
        import re

        result["page_prices"] = len(re.findall(r"[¥￥]\s*\d{2,6}", body_text or ""))
        # 页面是否明确说不可订
        result["not_bookable"] = ("不接受预订" in (body_text or "")) or ("暂不可订" in (body_text or ""))

    if captured and isinstance(captured[0].get("json"), dict):
        hl = ((captured[0]["json"].get("data") or {}).get("hotelList")) or []
        priced = []
        for item in hl:
            money = item.get("money") or {}
            ps = money.get("priceStr")
            pr = money.get("price")
            if (ps and ps != "?") or pr:
                priced.append(
                    {
                        "name": item["base"]["hotelName"],
                        "priceStr": ps,
                        "price": pr,
                        "soldOut": money.get("isSoldOut"),
                    }
                )
        result["api"] = {
            "hotels": len(hl),
            "with_price": len(priced),
            "samples": priced[:4],
            "first_priceStr": [
                ((it.get("money") or {}).get("priceStr")) for it in hl[:4]
            ],
        }
    return result


async def main() -> int:
    from hoteldata.runtime import Runtime

    today = date.today()
    checkin = today.isoformat()
    checkout = (today + timedelta(days=1)).isoformat()

    print("=" * 78)
    print(f"携程接口对照实验 —— 锚点是否可订 vs 接口是否给价({checkin} → {checkout})")
    print("=" * 78)

    async with Runtime.create(with_browser=True, with_scheduler=False) as rt:
        rows: list[dict] = []
        for name, hid in CANDIDATES:
            print(f"\n▶ {name}  (hotelId={hid})")
            r = await probe_one(rt, name, hid, checkin, checkout)
            rows.append(r)
            if r.get("error"):
                print(f"   ✗ {r['error']}")
                continue
            print(f"   页面 ¥数字 = {r['page_prices']}"
                  + ("   ★ 页面写着「不接受预订」" if r.get("not_bookable") else ""))
            api = r.get("api")
            if api is None:
                print("   接口未命中")
            else:
                print(f"   接口附近酒店 {api['hotels']} 家,其中**有价** {api['with_price']} 家")
                print(f"   前 4 家 priceStr = {api['first_priceStr']}")
                for s in api["samples"]:
                    print(f"     · {s['name'][:36]:36s} priceStr={s['priceStr']!r} price={s['price']!r} soldOut={s['soldOut']}")

    # 汇总
    print("\n" + "=" * 78)
    print("汇总")
    print("=" * 78)
    print(f"{'锚点':34s} {'可订?':8s} {'页面¥':>6s} {'接口有价家数':>12s}")
    any_priced = 0
    for r in rows:
        api = r.get("api") or {}
        n = api.get("with_price", "—")
        if isinstance(n, int):
            any_priced += n
        print(
            f"{r['anchor'][:34]:34s} "
            f"{('否' if r.get('not_bookable') else '是/未知'):8s} "
            f"{r.get('page_prices', 0):>6d} {str(n):>12s}"
        )
    print()
    if any_priced:
        print("★ 结论:接口**能**给出价格(存在有价的附近酒店)→ 段3 现有实现是对的;")
        print("  锚点显示「—」是因为**该锚点自己不可订**,不是取价通道有问题。")
    else:
        print("★ 结论:所有锚点的附近酒店接口价都是 '?' → 接口**结构性不带价**,")
        print("  取价必须改走 DOM 通道。")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
