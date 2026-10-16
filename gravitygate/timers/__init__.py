"""Timer drivers. A driver emits TimerEvent objects to a callback.

Events:
    ("result", ParsedResult)   a complete heat result
    ("raw", str)               every line received (for the capture/learn log)
    ("status", dict)           connected / disconnected / error
    ("gate", "open"|"closed")  start gate state, when the timer reports it
"""
from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
from typing import Awaitable, Callable

from .parsers import PLACE_CHARS, PROFILES, LineAssembler, ParsedResult, TimerProfile, parse_line

log = logging.getLogger("gravitygate.timer")

Callback = Callable[[str, object], Awaitable[None] | None]

__all__ = ["PROFILES", "ParsedResult", "TimerProfile", "parse_line", "SerialTimer",
           "SimulatedTimer", "NullTimer", "make_timer"]


class BaseTimer:
    def __init__(self, lane_count: int, callback: Callback):
        self.lane_count = lane_count
        self.callback = callback
        self.loop: asyncio.AbstractEventLoop | None = None
        self.connected = False
        self.expected_lanes: set[int] | None = None
        self.last_raw: list[str] = []

    def _emit(self, kind: str, payload: object) -> None:
        if kind == "raw":
            self.last_raw = (self.last_raw + [str(payload)])[-50:]
        if self.loop is None:
            return
        def run():
            res = self.callback(kind, payload)
            if asyncio.iscoroutine(res):
                asyncio.ensure_future(res)
        self.loop.call_soon_threadsafe(run)

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()

    async def stop(self) -> None:
        pass

    def arm(self, lanes: set[int]) -> None:
        """Prepare for a heat that uses `lanes`."""
        self.expected_lanes = set(lanes)

    def force(self) -> None:
        pass

    def status(self) -> dict:
        return {"type": type(self).__name__, "connected": self.connected, "last_raw": self.last_raw[-10:]}


class NullTimer(BaseTimer):
    """No timer attached: results come from Manual Entry or a remote node."""

    async def start(self) -> None:
        await super().start()
        self.connected = False


class SerialTimer(BaseTimer):
    def __init__(self, lane_count: int, callback: Callback, port: str, profile: TimerProfile):
        super().__init__(lane_count, callback)
        self.port = port
        self.profile = profile
        self._ser = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._assembler = LineAssembler(lane_count, profile)

    async def start(self) -> None:
        await super().start()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="serial-timer", daemon=True)
        self._thread.start()

    async def stop(self) -> None:
        self._stop.set()
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass

    def _open(self):
        import serial  # pyserial
        p = self.profile
        ser = serial.Serial(self.port, p.baud, bytesize=p.bytesize, parity=p.parity,
                            stopbits=p.stopbits, timeout=0.25)
        try:
            ser.dtr = p.dtr
            ser.rts = p.rts
        except Exception:
            pass
        for cmd in p.init_cmds:
            ser.write(cmd)
            time.sleep(0.1)
        return ser

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._ser = self._open()
                self.connected = True
                backoff = 1.0
                self._emit("status", {"connected": True, "port": self.port, "profile": self.profile.key})
                buf = b""
                last_byte = time.monotonic()
                while not self._stop.is_set():
                    chunk = self._ser.read(256)
                    now = time.monotonic()
                    if chunk:
                        buf += chunk
                        last_byte = now
                    # Some timers end with \r only, some \n, some nothing: split on
                    # either, and treat a 300 ms pause as end of line.
                    parts = buf.replace(b"\r", b"\n").split(b"\n")
                    buf = parts.pop()
                    if buf and now - last_byte > 0.3:
                        parts.append(buf)
                        buf = b""
                    for raw in parts:
                        line = raw.decode("ascii", "replace").strip()
                        if not line:
                            continue
                        self._emit("raw", line)
                        res = self._assembler.feed(line, self.expected_lanes)
                        if res is not None:
                            self._emit("result", res)
            except Exception as exc:  # disconnects, missing port, permission
                self.connected = False
                self._emit("status", {"connected": False, "error": str(exc), "port": self.port})
                log.warning("timer %s: %s (retry in %.0fs)", self.port, exc, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 10)

    def _write(self, data: bytes) -> None:
        if data and self._ser is not None and self.connected:
            try:
                self._ser.write(data)
            except Exception as exc:
                log.warning("timer write failed: %s", exc)

    def arm(self, lanes: set[int]) -> None:
        super().arm(lanes)
        self._assembler.flush()
        p = self.profile
        if p.lane_mask_cmd:
            mask = "".join("1" if l in lanes else "0" for l in range(1, self.lane_count + 1))
            self._write(p.lane_mask_cmd.format(mask=mask).encode())
        self._write(p.reset_cmd)

    def force(self) -> None:
        self._write(self.profile.force_cmd)
        res = self._assembler.flush()
        if res is not None:
            self._emit("result", res)

    def status(self) -> dict:
        return {**super().status(), "port": self.port, "profile": self.profile.key,
                "verified_profile": self.profile.verified}


class SimulatedTimer(BaseTimer):
    """Pretend gantry for rehearsals and tests. Each car gets a hidden base speed."""

    def __init__(self, lane_count: int, callback: Callback, seed: int | None = None,
                 lane_bias: dict[int, float] | None = None):
        super().__init__(lane_count, callback)
        self.rng = random.Random(seed)
        self.lane_bias = lane_bias or {l: 0.004 * (l - 1) for l in range(1, lane_count + 1)}
        self.car_speed: dict[int, float] = {}
        self.current_cars: dict[int, int] = {}
        self.observers: list[Callable[[dict[int, float | None], float], None]] = []

    async def start(self) -> None:
        await super().start()
        self.connected = True

    def set_cars(self, lanes: dict[int, int]) -> None:
        self.current_cars = dict(lanes)

    def race(self, delay: float = 0.0) -> None:
        """Drop the gate: produce a result after `delay` seconds."""
        lanes = sorted(self.expected_lanes or self.current_cars or range(1, self.lane_count + 1))
        times = {}
        for lane in lanes:
            car = self.current_cars.get(lane, lane)
            base = self.car_speed.setdefault(car, self.rng.uniform(2.45, 3.30))
            if self.rng.random() < 0.01:
                times[lane] = None  # rare DNF
                continue
            times[lane] = round(base + self.lane_bias.get(lane, 0) + self.rng.gauss(0, 0.006), 4)
        finished = sorted((t, l) for l, t in times.items() if t is not None)
        places = {l: i for i, (_, l) in enumerate(finished, start=1)}
        letters = "ABCDEFGH"
        raw = " ".join(f"{letters[l - 1]}={t:.4f}{PLACE_CHARS[places[l] - 1]}" if t is not None
                       else f"{letters[l - 1]}=0.0000" for l, t in sorted(times.items()))
        result = ParsedResult(times, places, raw)
        for obs in self.observers:  # e.g. the synthetic camera, so photos line up
            obs(times, delay)

        def fire():
            self._emit("raw", raw)
            self._emit("result", result)
        if self.loop is None:
            return
        if delay:
            self.loop.call_later(delay, fire)
        else:
            fire()


def make_timer(kind: str, lane_count: int, callback: Callback, port: str = "",
               profile: str = "generic", seed: int | None = None) -> BaseTimer:
    if kind == "serial":
        return SerialTimer(lane_count, callback, port, PROFILES[profile])
    if kind == "simulator":
        return SimulatedTimer(lane_count, callback, seed)
    return NullTimer(lane_count, callback)
