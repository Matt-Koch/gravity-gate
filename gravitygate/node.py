"""Bottom-of-track node: owns the serial timer and finish-line camera,
forwards results and photo finishes to the hub over a WebSocket.

    gravitygate node --hub ws://derby-top.local:8000 --port /dev/ttyUSB0 --profile microwizard --camera pi

Results are queued while the hub is unreachable and delivered on reconnect,
so a Wi-Fi blip never loses a heat.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import socket
from collections import deque
from pathlib import Path

from .timers import PROFILES, ParsedResult, SerialTimer, SimulatedTimer

log = logging.getLogger("gravitygate.node")


class Node:
    def __init__(self, hub_url: str, lane_count: int, port: str, profile: str, camera: str = "none",
                 token: str = "", calibration_file: str | None = None):
        self.hub_url = hub_url.rstrip("/") + "/ws/node" + (f"?token={token}" if token else "")
        self.lane_count = lane_count
        self.outbox: deque[dict] = deque(maxlen=200)
        self.ws = None
        self.wake = asyncio.Event()
        if port == "sim":
            self.timer = SimulatedTimer(lane_count, self.on_timer)
        else:
            self.timer = SerialTimer(lane_count, self.on_timer, port, PROFILES[profile])
        self.camera = None
        self.camera_kind = camera
        self.calibration_file = calibration_file

    async def on_timer(self, kind: str, payload) -> None:
        if kind == "result":
            res: ParsedResult = payload
            self.send({"type": "result", "times": res.times, "places": res.places, "raw": res.raw})
        elif kind == "raw":
            self.send({"type": "raw", "line": payload})
        elif kind == "status":
            self.send({"type": "status", "status": {"timer_connected": payload.get("connected"), **payload}})

    def send(self, msg: dict) -> None:
        # Raw lines are only useful live; results and photos must survive a reconnect.
        if msg["type"] == "raw" and self.ws is None:
            return
        self.outbox.append(msg)
        self.wake.set()

    def _calibration(self):
        from .photofinish.analysis import CameraCalibration
        if self.calibration_file and Path(self.calibration_file).exists():
            d = json.loads(Path(self.calibration_file).read_text())
            return CameraCalibration(d["finish_x"], {int(k): tuple(v) for k, v in d["lane_bands"].items()},
                                     d.get("threshold", 28.0), flip_time=d.get("flip_time", False))
        return CameraCalibration.default(self.camera.width, self.camera.height,
                                         list(range(1, self.lane_count + 1)))

    async def handle(self, msg: dict) -> None:
        cmd = msg.get("cmd")
        if cmd == "arm":
            self.timer.arm(set(msg["lanes"]))
            if self.camera is not None:
                self.camera.mark_armed()
        elif cmd == "force":
            self.timer.force()
        elif cmd == "photo" and self.camera is not None:
            from .photofinish.analysis import make_photo_finish
            times = {int(k): v for k, v in msg["times"].items()}
            await asyncio.sleep(msg.get("post_roll_s", 0.3))  # let the last car clear the line
            ts, frames = self.camera.heat_frames()
            pf = await asyncio.to_thread(make_photo_finish, frames, ts, self._calibration(), times,
                                         f"Heat {msg['heat_id']}", msg.get("margin_ms"))
            images = {}
            for kind in ("still", "strip"):
                buf = io.BytesIO()
                pf[kind].save(buf, format="JPEG", quality=85)
                images[kind] = base64.b64encode(buf.getvalue()).decode()
            self.send({"type": "photo", "heat_id": msg["heat_id"], "attempt": msg["attempt"],
                       "margin_ms": msg.get("margin_ms"), "images": images,
                       "camera_offsets_ms": pf["camera_offsets_ms"]})

    async def run(self) -> None:
        import websockets

        await self.timer.start()
        if self.camera_kind != "none":
            from .photofinish.camera import PiCameraSource, SyntheticCameraSource
            self.camera = PiCameraSource() if self.camera_kind == "pi" else \
                SyntheticCameraSource(list(range(1, self.lane_count + 1)))
            self.camera.start()
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self.hub_url, ping_interval=10) as ws:
                    self.ws = ws
                    backoff = 1.0
                    log.info("connected to hub %s", self.hub_url)
                    await ws.send(json.dumps({"type": "hello", "info": {
                        "host": socket.gethostname(), "camera": self.camera_kind,
                        "timer_connected": self.timer.connected}}))
                    await asyncio.gather(self._pump(ws), self._receive(ws))
            except Exception as exc:
                log.warning("hub connection lost (%s); retrying in %.0fs", exc, backoff)
            self.ws = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 10)

    async def _pump(self, ws) -> None:
        while True:
            while self.outbox:
                msg = self.outbox[0]
                await ws.send(json.dumps(msg, default=str))
                self.outbox.popleft()
            self.wake.clear()
            await self.wake.wait()

    async def _receive(self, ws) -> None:
        async for raw in ws:
            await self.handle(json.loads(raw))
