"""Capture a polished Scout dashboard screenshot from an isolated demo database.

The fixture is deliberately local and fictional. Browser requests outside the local
dashboard and the inline demo illustrations are aborted, so running this tool never
contacts Facebook, Telegram, or a listing image host.
"""

import asyncio
import os
import socket
import tempfile
import threading
from pathlib import Path

import httpx
import uvicorn
from playwright.async_api import Route, async_playwright

from scout.api import create_app
from scout.dashboard_auth import DashboardAuth
from scout.models import Listing, SearchResult, SearchSpec
from scout.searches import Searches
from scout.settings import Settings
from scout.store import Store

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "docs/images/scout-dashboard.png"
DEMO_IMAGE_PREFIX = "https://scontent-demo.fbcdn.net/scout-demo/"

# Original, deliberately simple illustrations keep the screenshot attractive without
# shipping or downloading real seller photographs.
DEMO_ILLUSTRATIONS = {
    "camera-01.svg": """<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 640 475'>
      <defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'><stop stop-color='#dfe9d8'/><stop offset='1' stop-color='#b4c6a8'/></linearGradient></defs>
      <rect width='640' height='475' fill='url(#g)'/><circle cx='520' cy='90' r='120' fill='#eef3e8' opacity='.55'/>
      <rect x='118' y='160' width='404' height='190' rx='30' fill='#304e42'/><rect x='145' y='185' width='350' height='140' rx='18' fill='#45675a'/>
      <circle cx='320' cy='255' r='92' fill='#d9dfd5'/><circle cx='320' cy='255' r='70' fill='#273b35'/><circle cx='320' cy='255' r='45' fill='#9aaf9d'/>
      <rect x='195' y='128' width='92' height='36' rx='12' fill='#304e42'/><circle cx='445' cy='205' r='12' fill='#d5e6b4'/>
      <text x='320' y='410' fill='#304e42' font-family='Georgia,serif' font-size='22' text-anchor='middle' letter-spacing='4'>NORTHSTAR 35</text>
    </svg>""",
    "camera-02.svg": """<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 640 475'>
      <defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'><stop stop-color='#f0e4d2'/><stop offset='1' stop-color='#d7baa0'/></linearGradient></defs>
      <rect width='640' height='475' fill='url(#g)'/><circle cx='130' cy='100' r='90' fill='#f7f0e5' opacity='.7'/>
      <rect x='100' y='158' width='438' height='194' rx='25' fill='#855c44'/><rect x='129' y='188' width='380' height='132' rx='14' fill='#a97655'/>
      <circle cx='322' cy='254' r='90' fill='#ecd7bb'/><circle cx='322' cy='254' r='68' fill='#4b3c36'/><circle cx='322' cy='254' r='45' fill='#c6a987'/>
      <rect x='165' y='127' width='104' height='34' rx='10' fill='#855c44'/><rect x='443' y='203' width='38' height='16' rx='8' fill='#f4dc9d'/>
      <text x='320' y='410' fill='#704b3a' font-family='Georgia,serif' font-size='22' text-anchor='middle' letter-spacing='4'>MOSS &amp; CO.</text>
    </svg>""",
    "camera-03.svg": """<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 640 475'>
      <defs><linearGradient id='g' x1='0' y1='1' x2='1' y2='0'><stop stop-color='#dce1eb'/><stop offset='1' stop-color='#b1c4d7'/></linearGradient></defs>
      <rect width='640' height='475' fill='url(#g)'/><circle cx='530' cy='350' r='145' fill='#e9edf3' opacity='.6'/>
      <rect x='100' y='158' width='438' height='194' rx='25' fill='#52687b'/><rect x='129' y='188' width='380' height='132' rx='14' fill='#6f8799'/>
      <circle cx='322' cy='254' r='90' fill='#d9e0e7'/><circle cx='322' cy='254' r='68' fill='#293c4a'/><circle cx='322' cy='254' r='45' fill='#8ea8b8'/>
      <rect x='172' y='128' width='114' height='33' rx='10' fill='#52687b'/><circle cx='455' cy='209' r='10' fill='#e8d495'/>
      <text x='320' y='410' fill='#40586d' font-family='Georgia,serif' font-size='22' text-anchor='middle' letter-spacing='4'>BLUE HOUR</text>
    </svg>""",
    "camera-04.svg": """<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 640 475'>
      <defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'><stop stop-color='#e7e4d3'/><stop offset='1' stop-color='#c7cfaa'/></linearGradient></defs>
      <rect width='640' height='475' fill='url(#g)'/><circle cx='100' cy='350' r='130' fill='#f0f0e2' opacity='.5'/>
      <rect x='105' y='162' width='430' height='190' rx='25' fill='#596651'/><rect x='134' y='190' width='372' height='130' rx='14' fill='#77876d'/>
      <circle cx='322' cy='255' r='88' fill='#e3e5d6'/><circle cx='322' cy='255' r='66' fill='#344238'/><circle cx='322' cy='255' r='43' fill='#a9bb8d'/>
      <rect x='185' y='130' width='100' height='34' rx='10' fill='#596651'/><rect x='445' y='203' width='40' height='16' rx='8' fill='#d7e4a9'/>
      <text x='320' y='410' fill='#4f5e48' font-family='Georgia,serif' font-size='22' text-anchor='middle' letter-spacing='4'>FIELD NOTE</text>
    </svg>""",
}

COUNTRY_DATA = {
    "US": ("USD", "Portland, OR", (165, 240, 295, 125, 380, 210, 185, 320)),
    "CA": ("CAD", "Vancouver, BC", (225, 330, 410, 170, 495, 285, 250, 365)),
    "FR": ("EUR", "Lyon, France", (145, 210, 260, 110, 315, 180, 155, 275)),
}
DESIGNS = (
    "Northstar 35mm vintage camera",
    "Moss & Co. rangefinder vintage camera",
    "Blue Hour compact vintage camera",
    "Field Note film vintage camera",
    "Silverline manual vintage camera",
    "Juniper street vintage camera",
    "Atlas travel vintage camera",
    "Cedar frame vintage camera",
)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def seed_fixture(store, searches):
    spec = SearchSpec(query="vintage camera", countries=["US", "CA", "FR"], sources=["facebook"])
    ident = searches.submit(spec)
    while job := searches.claim():
        currency, location, prices = COUNTRY_DATA[job["country"]]
        listings = []
        for index, title in enumerate(DESIGNS):
            listing_id = f"demo-{job['id']}-{index}"
            listings.append(
                Listing(
                    "facebook",
                    listing_id,
                    title,
                    f"https://www.facebook.com/marketplace/item/{listing_id}/",
                    job["country"],
                    str(prices[index]),
                    currency,
                    location,
                    image_url=f"{DEMO_IMAGE_PREFIX}camera-{index % 4 + 1:02d}.svg",
                )
            )
        searches.record(
            job,
            SearchResult(
                listings,
                radius_km=80,
                collected_count=len(listings) + 2,
                saturated=job["id"] % 5 == 0,
            ),
        )
    searches.create_watch(ident, name="Vintage cameras, anywhere", interval=60)
    store.heartbeat()
    return ident


async def serve_screenshot(settings, store, output, search_id):
    port = free_port()
    app = create_app(store, settings, start_worker=False)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                if httpx.get(base, timeout=1).status_code == 200:
                    break
            except httpx.TransportError:
                await asyncio.sleep(0.05)
        else:
            raise RuntimeError("The local Scout dashboard did not start")

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            context = await browser.new_context(viewport={"width": 1440, "height": 1260})

            async def route(request: Route):
                url = request.request.url
                if url.startswith(DEMO_IMAGE_PREFIX):
                    name = url.removeprefix(DEMO_IMAGE_PREFIX)
                    body = DEMO_ILLUSTRATIONS.get(name)
                    if body is None:
                        await request.abort()
                    else:
                        await request.fulfill(body=body, content_type="image/svg+xml")
                elif url.startswith(base):
                    await request.continue_()
                else:
                    await request.abort()

            await context.route("**/*", route)
            page = await context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            auth = DashboardAuth(store, settings)
            ticket = auth.issue_ticket()
            await page.goto(f"{base}/#signin={ticket}")
            await page.locator("#app").wait_for(state="visible", timeout=10000)
            await page.evaluate(f"openSearch({search_id!r})")
            await page.locator("#results").wait_for(state="visible")
            await page.locator(".listing").first.wait_for(state="visible")
            await page.wait_for_function("document.querySelectorAll('.listing img').length >= 8")
            await page.screenshot(path=str(output), full_page=False)
            if errors:
                raise RuntimeError("Dashboard JavaScript error: " + "; ".join(errors))
            await browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def main():
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / "data/browsers"))
    output = Path(os.environ.get("SCOUT_SCREENSHOT_OUTPUT", DEFAULT_OUTPUT)).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="scout-readme-demo-") as directory:
        data_dir = Path(directory)
        settings = Settings(
            data_dir,
            api_token="local-demo-token-" + "x" * 24,
            telegram_token="demo-token",
            telegram_chat="-1000000000000",
            telegram_thread=42,
        )
        settings.prepare()
        store = Store(data_dir / "scout.sqlite3")
        searches = Searches(store)
        search_id = seed_fixture(store, searches)
        asyncio.run(serve_screenshot(settings, store, output, search_id))
    from PIL import Image

    with Image.open(output) as image:
        print(f"Wrote {output} ({image.width}x{image.height})")


if __name__ == "__main__":
    main()
