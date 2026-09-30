"""美团 H5 入口 URL 对照实验 —— 到底哪个地址能出酒店卡片?

背景
====

用户指出:**``https://i.meituan.com/awp/h5/hotel/search/search.html`` 才是酒店 H5 页面**。

段3 之前一直用旧系统的 ``/awp/h5/hotel/list.html``,实测它已 **404**
(CDN 返回 ``<Code>NoSuchKey</Code> ... Object [h5/hotel/list.html] not exist``)。
我改成 ``/awp/h5/hotel/list/list.html``(旁听到的路径),但发现**结果区是空的**。

现在按下面对照,用**真卡片选择器 ``a.poi``**(旧系统那套实测仍有效)
以及**足够长的等待**,一次定论:

=========================================  ==========================================
URL                                          说明
=========================================  ==========================================
``search/search.html?keyword=&checkIn=&…``   用户指出的正确 H5 页(带参数)
``search/search.html``                       同上,不带参数
``list/list.html?…``                         旁听到的路径(备选)
``list.html?…``                              旧系统地址(预期 404)
=========================================  ==========================================

用法::

    .venv\\Scripts\\python.exe scripts\\diag_meituan_url.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except (AttributeError, ValueError):  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parents[1]

#: 旧系统实测仍有效的卡片选择器
CARD = 'a.poi, a[class*="poi"]'
PRICE = "em.poi-price-num, .poi-price-num"
TITLE = "h1.poi-title, .poi-title"

KEYWORD = "隐欲民宿"
CITY_ID = "59"


def build(base: str, *, with_params: bool) -> str:
    if not with_params:
        return base
    today = date.today()
    q = urlencode(
        {
            "cityId": CITY_ID,
            "keyword": KEYWORD,
            "checkIn": today.isoformat(),
            "checkOut": (today + timedelta(days=1)).isoformat(),
        }
    )
    return f"{base}?{q}"


CANDIDATES: tuple[tuple[str, str], ...] = (
    ("search.html(带参数)", build("https://i.meituan.com/awp/h5/hotel/search/search.html", with_params=True)),
    ("search.html(无参数)", build("https://i.meituan.com/awp/h5/hotel/search/search.html", with_params=False)),
    ("list/list.html(带参数)", build("https://i.meituan.com/awp/h5/hotel/list/list.html", with_params=True)),
    ("list.html(旧地址)", build("https://i.meituan.com/awp/h5/hotel/list.html", with_params=True)),
)


async def probe(page: object, label: str, url: str, wait_s: float = 22.0) -> dict:
    """打开 → 轮询等 ``a.poi`` → 报卡片数/价格数/是否 404。"""
    info: dict = {"label": label, "url": url, "cards": 0, "prices": 0, "titles": 0,
                  "err404": False, "waited": 0.0, "head": ""}
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        info["error"] = str(exc)[:100]
        return info

    step = 2.0
    waited = 0.0
    while waited < wait_s:
        await asyncio.sleep(step)
        waited += step
        try:
            n = await page.locator(CARD).count()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            n = 0
        if n:
            break

    info["waited"] = waited
    try:
        info["cards"] = await page.locator(CARD).count()  # type: ignore[attr-defined]
        info["prices"] = await page.locator(PRICE).count()  # type: ignore[attr-defined]
        info["titles"] = await page.locator(TITLE).count()  # type: ignore[attr-defined]
        body = await page.locator("body").inner_text(timeout=8000)  # type: ignore[attr-defined]
        info["err404"] = "NoSuchKey" in (body or "")
        info["head"] = " ".join((body or "").split())[:150]
    except Exception as exc:  # noqa: BLE001
        info["error"] = str(exc)[:100]
    return info


async def main() -> int:
    from hoteldata.runtime import Runtime

    print("=" * 80)
    print("美团 H5 入口 URL 对照实验")
    print("=" * 80)

    results: list[dict] = []
    async with Runtime.create(with_browser=True, with_scheduler=False) as rt:
        handle = rt.sessions.handle("meituan", "ota_meituan", "meituan")
        async with rt.browser.page_session(handle) as (_b, _c, page):
            for label, url in CANDIDATES:
                print(f"\n▶ {label}")
                print(f"   {url[:110]}")
                r = await probe(page, label, url)
                results.append(r)
                if r.get("error") and not r["cards"]:
                    print(f"   ✗ 异常: {r['error']}")
                    continue
                print(f"   等 {r['waited']:.0f}s → a.poi={r['cards']} 价格={r['prices']} 标题={r['titles']}"
                      + ("   ★ 404(NoSuchKey)" if r["err404"] else ""))
                print(f"   正文: {r['head'][:120]}")

            # 带参数那次留一张截图,便于肉眼确认
            out = ROOT / "var" / "reports" / "diag"
            out.mkdir(parents=True, exist_ok=True)
            shot = out / "meituan_url_probe.png"
            try:
                await page.screenshot(path=str(shot))
                print(f"\n   最后一次截图: {shot}")
            except Exception:  # noqa: BLE001
                pass

    print("\n" + "=" * 80)
    print(f"  {'入口':26s} {'a.poi':>6s} {'价格':>6s} {'404':>5s}")
    print("  " + "-" * 52)
    best = None
    for r in results:
        print(f"  {r['label']:26s} {r['cards']:>6d} {r['prices']:>6d} "
              f"{('是' if r['err404'] else '否'):>5s}")
        if r["cards"] and (best is None or r["cards"] > best["cards"]):
            best = r
    print()
    if best:
        print(f"★ 结论:用 **{best['label']}** —— {best['cards']} 张卡片 / {best['prices']} 个价格")
        print(f"   {best['url'][:110]}")
    else:
        print("★ 结论:四个入口都没出卡片 —— 需要人工看一眼截图判断原因")
    print("=" * 80)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
