"""Camera capture into a ring buffer.

PiCameraSource uses picamera2 (install with `sudo apt install python3-picamera2`).
Pick a small crop and high frame rate; for a 3-lane finish line 640x200 is
plenty. Camera Module 3 manages ~120 fps at that size, the Global Shutter
Camera ~200+ fps without rolling-shutter lean on fast cars.

SyntheticCameraSource renders fake cars so the whole pipeline can be tested
and demoed without hardware.
"""
from __future__ import annotations

import collections
import threading
import time
from pathlib import Path
from typing import Sequence

import numpy as np

from .analysis import CameraCalibration, make_photo_finish


class RingBuffer:
    def __init__(self, seconds: float, fps: float):
        self.frames: collections.deque = collections.deque(maxlen=max(10, int(seconds * fps)))
        self.lock = threading.Lock()

    def push(self, ts: float, frame: np.ndarray) -> None:
        with self.lock:
            self.frames.append((ts, frame))

    def snapshot(self, since: float | None = None) -> tuple[list[float], list[np.ndarray]]:
        with self.lock:
            items = list(self.frames)
        if since is not None:
            items = [it for it in items if it[0] >= since]
        return [t for t, _ in items], [f for _, f in items]


class CameraSource:
    width = 640
    height = 200
    fps = 120.0

    def __init__(self, buffer_seconds: float = 3.0):
        self.buffer = RingBuffer(buffer_seconds, self.fps)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.armed_at: float | None = None

    def mark_armed(self) -> None:
        """Called when the timer is armed; earlier frames belong to the previous heat."""
        self.armed_at = time.monotonic()

    def heat_frames(self) -> tuple[list[float], list[np.ndarray]]:
        ts, frames = self.buffer.snapshot(since=self.armed_at)
        if len(frames) < 10:  # armed long ago and buffer rolled, or never armed
            ts, frames = self.buffer.snapshot()
        return ts, frames

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="camera")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        raise NotImplementedError


class PiCameraSource(CameraSource):
    def __init__(self, width: int = 640, height: int = 200, fps: float = 120.0, buffer_seconds: float = 3.0):
        self.width, self.height, self.fps = width, height, fps
        super().__init__(buffer_seconds)

    def _run(self) -> None:
        from picamera2 import Picamera2  # type: ignore

        cam = Picamera2()
        config = cam.create_video_configuration(
            main={"size": (self.width, self.height), "format": "RGB888"},
            controls={"FrameRate": self.fps, "ExposureTime": 1500},  # short exposure = less blur
            buffer_count=6,
        )
        cam.configure(config)
        cam.start()
        try:
            while not self._stop.is_set():
                frame = cam.capture_array("main")
                self.buffer.push(time.monotonic(), frame)
        finally:
            cam.stop()


class SyntheticCameraSource(CameraSource):
    """Draws cars sliding across the line. Call schedule_race() with timer times."""

    def __init__(self, lanes: Sequence[int], fps: float = 120.0, buffer_seconds: float = 3.0):
        self.fps = fps
        super().__init__(buffer_seconds)
        self.lanes = list(lanes)
        self.cal = CameraCalibration.default(self.width, self.height, self.lanes)
        self.race_start: float | None = None
        self.race_times: dict[int, float | None] = {}
        self.car_px_per_s = 2600.0   # ~ 15 mph at this crop

    def schedule_race(self, times: dict[int, float | None], finish_at: float | None = None) -> None:
        """Arrange for the leader to hit the line at `finish_at` (monotonic)."""
        finish_at = finish_at or time.monotonic()
        lead = min(t for t in times.values() if t is not None)
        self.race_start = finish_at - lead
        self.race_times = dict(times)

    def render(self, now: float) -> np.ndarray:
        img = np.full((self.height, self.width, 3), 70, np.uint8)
        img[:, self.cal.finish_x - 1:self.cal.finish_x + 1] = (230, 230, 230)
        if self.race_start is not None:
            for lane, (y0, y1) in self.cal.lane_bands.items():
                t = self.race_times.get(lane)
                if t is None:
                    continue
                cross_at = self.race_start + t
                nose = int(self.cal.finish_x + (now - cross_at) * self.car_px_per_s)
                tail = nose - 140
                x0, x1 = max(tail, 0), min(nose, self.width)
                if x0 < x1:
                    color = ((lane * 70) % 255, 80 + lane * 40 % 175, 200 - lane * 30 % 200)
                    img[y0 + 12:y1 - 12, x0:x1] = color
        return img

    def _run(self) -> None:
        period = 1.0 / self.fps
        nxt = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            self.buffer.push(now, self.render(now))
            nxt += period
            time.sleep(max(0.0, nxt - time.monotonic()))


def capture_photo_finish(source: CameraSource, cal: CameraCalibration, times: dict[int, float | None],
                         out_dir: Path, name: str, margin_ms: float | None = None,
                         title: str = "") -> dict[str, str]:
    """Build still + strip from the current buffer and save JPEGs. Returns {kind: path}."""
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamps, frames = source.heat_frames()
    pf = make_photo_finish(frames, timestamps, cal, times, title, margin_ms)
    paths = {}
    for kind in ("still", "strip"):
        p = out_dir / f"{name}-{kind}.jpg"
        pf[kind].save(p, quality=88)
        paths[kind] = str(p)
    return paths
