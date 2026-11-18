"""Regenerate docs/screenshots/admin-fleet.png with four simulated fleet machines.

    GRAVITYGATE_FLEET=0 GRAVITYGATE_MACHINE_ID=0B11 gravitygate serve --data /tmp/gg-fleet --port 8780 &
    python docs/tools/fleet_screenshot.py
"""
import os
import asyncio, json, time
import websockets
from playwright.async_api import async_playwright
from playwright.async_api import async_playwright
B = "http://localhost:8780"; OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "screenshots") + "/"
MACHINES = [
  ({"id": "0B11", "ips": ["10.42.0.1"], "hostname": "gg-0b11", "model": "Raspberry Pi 5 Model B Rev 1.0", "hdmi": ["HDMI-A-1"], "camera": None, "serial": []}, ["brain", "display"]),
  ({"id": "A3F2", "ips": ["10.42.0.17"], "hostname": "gg-a3f2", "model": "Raspberry Pi 4 Model B Rev 1.5", "hdmi": [], "camera": "pi", "serial": ["/dev/ttyUSB0"]}, []),
  ({"id": "7C10", "ips": ["10.42.0.18"], "hostname": "gg-7c10", "model": "Protectli VP2420", "hdmi": ["HDMI-A-1"], "camera": None, "serial": []}, []),
  ({"id": "E402", "ips": ["10.42.0.19"], "hostname": "gg-e402", "model": "Raspberry Pi 4 Model B Rev 1.5", "hdmi": ["HDMI-A-1"], "camera": None, "serial": []}, []),
]
async def agent(m, roles, stop):
    async with websockets.connect("ws://127.0.0.1:8780/ws/agent", proxy=None) as ws:
        await ws.send(json.dumps({"type": "hello", "machine": m, "roles": roles, "local_change": True}))
        while not stop.is_set():
            try: await asyncio.wait_for(ws.recv(), 0.5)
            except asyncio.TimeoutError: pass
async def main():
    stop = asyncio.Event()
    tasks = [asyncio.create_task(agent(m, r, stop)) for m, r in MACHINES]
    await asyncio.sleep(1.5)
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path=os.environ.get("CHROMIUM") or None)
        pg = await b.new_page(viewport={"width": 1280, "height": 860})
        errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
        await pg.goto(B + "/admin#fleet"); await asyncio.sleep(1.2)
        await pg.click("text=Auto-arrange"); await asyncio.sleep(1.5)
        await pg.screenshot(path=OUT + "admin-fleet.png")
        st = await (await pg.request.get(B + "/api/state")).json()
        print("features:", st["features"]); print("timer:", st["timer"]["type"]); print("errors:", errs)
        await b.close()
    stop.set(); await asyncio.gather(*tasks, return_exceptions=True)
asyncio.run(main())
