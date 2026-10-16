from gravitygate.photofinish.analysis import make_photo_finish
from gravitygate.photofinish.camera import SyntheticCameraSource


def render_race(cam, times, start, fps=200, seconds=1.0, t0=0.0):
    cam.schedule_race(times, finish_at=t0 + start)
    ts = [t0 + i / fps for i in range(int(seconds * fps))]
    return ts, [cam.render(t) for t in ts]


def test_crossing_order_matches_timer():
    cam = SyntheticCameraSource([1, 2, 3])
    times = {1: 2.530, 2: 2.505, 3: 2.560}
    ts, frames = render_race(cam, times, start=0.3)
    pf = make_photo_finish(frames, ts, cam.cal, times, "test", 25.0)
    c = pf["crossings"]
    assert c[2] < c[1] < c[3]  # lane 2 won, lane 3 last
    off = pf["camera_offsets_ms"]
    assert abs(off[1] - 25) <= 5 and abs(off[3] - 55) <= 5  # within one frame at 200 fps
    assert pf["frame_index"] == c[2]
    assert pf["still"].size == (cam.width, cam.height)


def test_dnf_lane_has_no_crossing():
    cam = SyntheticCameraSource([1, 2, 3])
    times = {1: 2.53, 2: None, 3: 2.56}
    ts, frames = render_race(cam, times, start=0.3)
    pf = make_photo_finish(frames, ts, cam.cal, times)
    assert pf["crossings"][2] is None
    assert pf["frame_index"] == pf["crossings"][1]


def test_previous_heat_frames_are_excluded():
    cam = SyntheticCameraSource([1, 2, 3])
    old_ts, old_frames = render_race(cam, {1: 2.9, 2: 2.6, 3: 2.7}, start=0.3, seconds=0.8)
    for t, f in zip(old_ts, old_frames):
        cam.buffer.push(t, f)
    cam.armed_at = 1.0
    new = {1: 2.50, 2: 2.52, 3: 2.51}
    ts, frames = render_race(cam, new, start=0.3, t0=1.0)
    for t, f in zip(ts, frames):
        cam.buffer.push(t, f)
    hts, hframes = cam.heat_frames()
    assert min(hts) >= 1.0
    pf = make_photo_finish(hframes, hts, cam.cal, new)
    c = pf["crossings"]
    assert c[1] < c[3] < c[2]
