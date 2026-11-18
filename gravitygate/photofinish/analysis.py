"""Photo-finish image processing (numpy + Pillow, runs on the bottom Pi).

Two outputs per close finish:
  * still  - the camera frame nearest the moment the leader crosses the line,
             with the finish line, lane labels and timer times drawn on it
  * strip  - a slit-scan: the single pixel column at the finish line taken
             from every buffered frame and laid side by side, so the x-axis is
             time. This is how real photo-finish cameras work; each car appears
             in the order it crossed, and the gap between noses is the margin.

Frame orientation assumption: the camera looks down at the finish line,
cars travel left -> right across the image, lanes are horizontal bands.
Configure `finish_x` (pixel column of the line) and `lane_bands` (y ranges)
from the calibration page.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

import numpy as np


@dataclass
class CameraCalibration:
    finish_x: int
    lane_bands: dict[int, tuple[int, int]]     # lane -> (y0, y1)
    threshold: float = 28.0                    # mean abs diff to call "car present"
    baseline_frames: int = 5
    flip_time: bool = False                    # True = leader on the right, like TV photo finishes
    extra: dict = field(default_factory=dict)

    @classmethod
    def default(cls, width: int, height: int, lanes: Sequence[int]) -> "CameraCalibration":
        n = len(lanes)
        band = height // max(n, 1)
        return cls(finish_x=width // 2,
                   lane_bands={lane: (i * band, (i + 1) * band) for i, lane in enumerate(lanes)})


def slit_scan(frames: Sequence[np.ndarray], x: int, width: int = 1) -> np.ndarray:
    """Stack the finish-line column of each frame left-to-right (H x T x C)."""
    cols = [f[:, x:x + width].mean(axis=1, keepdims=True).astype(np.uint8) for f in frames]
    return np.concatenate(cols, axis=1)


def detect_crossings(strip: np.ndarray, cal: CameraCalibration) -> dict[int, int | None]:
    """Per lane, the first strip column (frame index) where a car occupies the line."""
    gray = strip.astype(np.float32)
    if gray.ndim == 3:
        gray = gray.mean(axis=2)
    out: dict[int, int | None] = {}
    nb = max(1, min(cal.baseline_frames, gray.shape[1] // 4))
    for lane, (y0, y1) in cal.lane_bands.items():
        band = gray[y0:y1, :]
        baseline = np.median(band[:, :nb], axis=1, keepdims=True)
        diff = np.abs(band - baseline).mean(axis=0)
        hits = np.nonzero(diff > cal.threshold)[0]
        out[lane] = int(hits[0]) if hits.size else None
    return out


def frame_time_offsets(crossings: dict[int, int | None], timestamps: Sequence[float]) -> dict[int, float]:
    """Crossing times relative to the first crossing, in ms, from camera timestamps."""
    seen = {l: timestamps[i] for l, i in crossings.items() if i is not None}
    if not seen:
        return {}
    t0 = min(seen.values())
    return {l: round((t - t0) * 1000, 1) for l, t in seen.items()}


def choose_still_index(crossings: dict[int, int | None], times: dict[int, float | None],
                       n_frames: int) -> int:
    """Frame where the timer's winner reaches the line (fallback: middle of buffer)."""
    finished = sorted((t, l) for l, t in times.items() if t is not None)
    for _, lane in finished:
        idx = crossings.get(lane)
        if idx is not None:
            return idx
    found = [i for i in crossings.values() if i is not None]
    return min(found) if found else n_frames // 2


def annotate(frame: np.ndarray, cal: CameraCalibration, times: dict[int, float | None],
             title: str = "", margin_ms: float | None = None):
    from PIL import Image, ImageDraw

    img = Image.fromarray(frame).convert("RGB")
    d = ImageDraw.Draw(img)
    h = img.height
    d.line([(cal.finish_x, 0), (cal.finish_x, h)], fill=(255, 40, 40), width=2)
    finished = sorted((t, l) for l, t in times.items() if t is not None)
    place = {l: i for i, (_, l) in enumerate(finished, start=1)}
    for lane, (y0, y1) in cal.lane_bands.items():
        t = times.get(lane)
        label = f"L{lane} " + (f"{t:.4f}  #{place[lane]}" if t is not None else "DNF")
        d.rectangle([4, y0 + 2, 4 + 7 * len(label), y0 + 16], fill=(0, 0, 0))
        d.text((6, y0 + 3), label, fill=(255, 230, 0))
    banner = title + (f"   margin {margin_ms:.1f} ms" if margin_ms is not None else "")
    if banner.strip():
        d.rectangle([0, h - 18, img.width, h], fill=(0, 0, 0))
        d.text((6, h - 16), banner, fill=(255, 255, 255))
    return img


def strip_image(strip: np.ndarray, cal: CameraCalibration, crossings: dict[int, int | None],
                scale: int = 4):
    from PIL import Image, ImageDraw

    arr = strip[:, ::-1] if cal.flip_time else strip
    img = Image.fromarray(arr).convert("RGB")
    img = img.resize((img.width * scale, img.height), Image.NEAREST)
    d = ImageDraw.Draw(img)
    for lane, idx in crossings.items():
        if idx is None:
            continue
        x = (strip.shape[1] - 1 - idx if cal.flip_time else idx) * scale
        y0, y1 = cal.lane_bands[lane]
        d.line([(x, y0), (x, y1)], fill=(255, 40, 40), width=1)
        d.text((x + 2, y0 + 2), f"L{lane}", fill=(255, 230, 0))
    return img


def make_photo_finish(frames: Sequence[np.ndarray], timestamps: Sequence[float],
                      cal: CameraCalibration, times: dict[int, float | None],
                      title: str = "", margin_ms: float | None = None) -> dict:
    if not frames:
        raise ValueError("no frames buffered")
    strip = slit_scan(frames, cal.finish_x)
    crossings = detect_crossings(strip, cal)
    idx = choose_still_index(crossings, times, len(frames))
    return {
        "still": annotate(frames[idx], cal, times, title, margin_ms),
        "strip": strip_image(strip, cal, crossings),
        "crossings": crossings,
        "camera_offsets_ms": frame_time_offsets(crossings, timestamps),
        "frame_index": idx,
    }


def calibration_from_dict(d: dict | None, width: int, height: int, lanes) -> CameraCalibration:
    """Saved setting -> CameraCalibration; falls back to the default layout."""
    if not d:
        return CameraCalibration.default(width, height, list(lanes))
    return CameraCalibration(finish_x=int(d["finish_x"]),
                             lane_bands={int(k): (int(v[0]), int(v[1])) for k, v in d["lane_bands"].items()},
                             threshold=float(d.get("threshold", 28.0)), flip_time=bool(d.get("flip_time", False)))


def calibration_to_dict(cal: CameraCalibration) -> dict:
    return {"finish_x": cal.finish_x, "lane_bands": {str(k): list(v) for k, v in cal.lane_bands.items()},
            "threshold": cal.threshold, "flip_time": cal.flip_time}


def check_image(frame: np.ndarray, cal: CameraCalibration):
    """Camera check: the live view with the finish line, lane bands and a pixel ruler drawn on,
    so the calibration can be lined up with the real track."""
    from PIL import Image, ImageDraw

    img = Image.fromarray(frame).convert("RGB")
    d = ImageDraw.Draw(img)
    w, h = img.size
    for x in range(0, w, 50):                       # ruler along the top, labelled every 100 px
        d.line([(x, 0), (x, 6 if x % 100 else 12)], fill=(255, 255, 255), width=1)
        if x % 100 == 0:
            d.text((x + 2, 12), str(x), fill=(255, 255, 255))
    for lane, (y0, y1) in sorted(cal.lane_bands.items()):
        d.rectangle([0, y0, w - 1, y1 - 1], outline=(60, 200, 255), width=2)
        d.rectangle([4, y1 - 20, 64, y1 - 4], fill=(0, 0, 0))   # bottom-left: clear of the ruler
        d.text((8, y1 - 18), f"Lane {lane}", fill=(60, 200, 255))
    d.line([(cal.finish_x, 0), (cal.finish_x, h)], fill=(255, 40, 40), width=3)
    d.rectangle([cal.finish_x + 4, h - 20, cal.finish_x + 118, h - 4], fill=(0, 0, 0))
    d.text((cal.finish_x + 8, h - 18), f"finish x={cal.finish_x}", fill=(255, 80, 80))
    return img
