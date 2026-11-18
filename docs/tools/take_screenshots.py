"""Regenerate the web screenshots in docs/screenshots from a simulated event.

    gravitygate demo-roster --data /tmp/gg-shots --count 30
    GRAVITYGATE_FLEET=0 gravitygate serve --data /tmp/gg-shots --port 8790 &
    python docs/tools/take_screenshots.py          # needs: pip install playwright (and a Chromium)

Set CHROMIUM=/path/to/chrome if Playwright's own browser isn't installed.
"""
import os
import asyncio, json, time, urllib.request
from playwright.async_api import async_playwright
B = os.environ.get("GG_URL", "http://localhost:8790")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "screenshots") + "/"
def call(p, body=None, method=None):
    r = urllib.request.Request(B + p, data=None if body is None else json.dumps(body).encode(),
                               headers={"Content-Type": "application/json"}, method=method or ("POST" if body is not None else "GET"))
    return json.loads(urllib.request.urlopen(r).read())
async def heat():
    if call("/api/race/next", {})["heat_id"] is None:
        return False
    await asyncio.sleep(0.35); call("/api/race/simulate", {})
    for _ in range(40):
        await asyncio.sleep(0.1)
        if call("/api/state")["state"] == "results":
            break
    return True
async def main():
    call("/api/settings", {"event_name": "Pack 63 Pinewood Derby", "camera_kind": "synthetic", "photo_margin_ms": 25,
                           "wifi_ssid": "Pack63-Derby", "wifi_password": "gofast63", "public_url": "http://10.42.0.1:8000",
                           "roster_csv_url": "https://docs.google.com/spreadsheets/d/EXAMPLE/edit#gid=0"})
    racers = call("/api/racers")
    for r in racers[-3:]:
        call(f"/api/racers/{r['id']}", {"checked_in": 0}, "PATCH")
    q = call("/api/sessions", {"kind": "chart", "name": "Qualifiers", "format": "rotation", "runs_per_lane": 2, "seed": 4})
    errs = []
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path=os.environ.get("CHROMIUM") or None)
        def watch(page, tag):
            page.on("pageerror", lambda e: errs.append(f"{tag}: {e}"))
            return page
        d = watch(await b.new_page(viewport={"width": 1280, "height": 720}), "display")
        await d.goto(B + "/display?kiosk=1"); await asyncio.sleep(1)
        got = False
        for i in range(40):
            await heat(); await asyncio.sleep(0.6)
            st = call("/api/state")
            if not got and i > 3 and st["photo"] and st["photo"]["close"]:
                await asyncio.sleep(1.2); await d.screenshot(path=OUT + "display-photo-finish.png"); got = True
            if got and i >= 9:
                break
        print("close photo captured:", got)
        call("/api/settings", {"photo_margin_ms": 10})
        await heat(); await asyncio.sleep(1.5)
        await d.evaluate("document.getElementById('photo').classList.add('hidden')")
        await d.screenshot(path=OUT + "display-race.png")
        for mode, name in (("standings", "display-standings"), ("idle", "display-welcome-qr")):
            call("/api/display", {"mode": mode}); await asyncio.sleep(1.3); await d.screenshot(path=OUT + name + ".png")
        call("/api/display", {"mode": "race"})
        a = watch(await b.new_page(viewport={"width": 1280, "height": 900}), "admin")
        async def admin(tab, name):
            await a.goto(B + "/admin#" + tab); await a.reload(); await asyncio.sleep(1.4)
            await a.evaluate("window.scrollTo(0, 0)"); await a.screenshot(path=OUT + name + ".png")
        await admin("racers", "admin-racers"); await admin("sessions", "admin-sessions")
        call("/api/race/next", {}); await admin("race", "admin-race-staged")
        call("/api/race/simulate", {}); await asyncio.sleep(2.5); await a.screenshot(path=OUT + "admin-race.png")
        await admin("settings", "admin-settings")
        await a.click("text=Take camera check photo"); await asyncio.sleep(3)
        for title, name in (("Camera check", "admin-camera-check"), ("Audience phones", "admin-settings-phones"), ("Google Sheets", "admin-settings-sheets")):
            card = a.locator(".card", has=a.locator(f"text={title}")).first
            await card.scroll_into_view_if_needed(); await card.screenshot(path=OUT + name + ".png")
        ctx = await b.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2)
        ids = [r["id"] for r in racers[:2]]
        await ctx.add_init_script(f"localStorage.setItem('derbyFollow', JSON.stringify({ids}))")
        m = watch(await ctx.new_page(), "phone")
        await m.goto(B + "/s#now"); await asyncio.sleep(1.5); await m.screenshot(path=OUT + "phone-now.png")
        await m.evaluate("window.scrollTo(0, document.body.scrollHeight)"); await asyncio.sleep(0.5)
        await m.screenshot(path=OUT + "phone-finishes.png")
        await m.click(".thumb.close") if await m.locator(".thumb.close").count() else await m.click(".thumb")
        await asyncio.sleep(0.8); await m.screenshot(path=OUT + "phone-photo.png"); await m.evaluate("closeShot()")
        for t in ("mine", "leaders", "stats"):
            await m.click(f"nav button[data-tab={t}]"); await asyncio.sleep(1.4)
            await m.evaluate("window.scrollTo(0, 0)"); await m.screenshot(path=OUT + f"phone-{t}.png")
        while await heat():
            pass
        await admin("standings", "admin-standings")
        call("/api/display", {"mode": "awards"}); await asyncio.sleep(0.5)
        for _ in range(3):
            call("/api/awards/next", {}); await asyncio.sleep(0.4)
        await asyncio.sleep(1.2); await d.screenshot(path=OUT + "display-awards.png")
        c = call("/api/sessions", {"kind": "elimination", "name": "Champions Bracket", "seed_from_session": q["id"], "per_den": 2, "seed": 3})
        print("bracket heats round 1:", c["heats"])
        while await heat():
            pass
        call("/api/display", {"mode": "bracket"}); await asyncio.sleep(1.6)
        await d.screenshot(path=OUT + "display-bracket-final.png")
        await m.click("nav button[data-tab=leaders]"); await asyncio.sleep(1.6); await m.screenshot(path=OUT + "phone-bracket.png")
        await m.click("#blzoom"); await asyncio.sleep(1); await m.screenshot(path=OUT + "phone-bracket-overview.png")
        call("/api/display", {"mode": "race"})
        po = await b.new_page(viewport={"width": 850, "height": 1100})
        await po.goto(B + "/poster"); await asyncio.sleep(1.2); await po.screenshot(path=OUT + "poster.png")
        await b.close()
    print("errors:", errs)
asyncio.run(main())
