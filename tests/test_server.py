import time

import pytest
from fastapi.testclient import TestClient

from gravitygate.server import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as c:
        yield c


def wait_for(fn, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("timed out")


ROSTER = """Car #,First Name,Last Name,Den
1,Avery,Alvarez,Wolves
38,Blake,Becker,Bears
11,Casey,Chen,Wolves
25,Drew,Dawson,Tigers
5,Emery,Ellis,Bears
13,Finley,Foster,Tigers
"""


def test_full_chart_flow(client):
    r = client.post("/api/roster/import", json={"csv": ROSTER}).json()
    assert r["added"] == 6
    for racer in client.get("/api/racers").json():
        client.patch(f"/api/racers/{racer['id']}", json={"checked_in": 1})
    s = client.post("/api/sessions", json={"kind": "chart", "name": "Pack Race", "format": "rotation", "runs_per_lane": 1}).json()
    assert s["heats"] == 6  # 6 cars x 3 lanes / 3 per heat

    for _ in range(6):
        heat = client.post("/api/race/next").json()["heat_id"]
        assert heat
        assert client.get("/api/state").json()["state"] == "staged"
        client.post("/api/race/simulate")
        wait_for(lambda: client.get("/api/state").json()["state"] == "results")
    assert client.post("/api/race/next").json()["heat_id"] is None

    st = client.get("/api/standings").json()
    assert len(st["overall"]) == 6
    assert set(st["dens"]) == {"Wolves", "Bears", "Tigers"}
    assert all(r["runs"] == 3 for r in st["overall"])
    assert client.get("/api/stats").json()["fastest"]["scale_mph"] > 0
    awards = client.get("/api/awards").json()
    assert any(a["category"] == "Pack Overall" for a in awards)
    csv = client.get("/api/results.csv").text
    assert csv.count("\n") == 1 + 18


def test_rerun_and_manual_entry(client):
    client.post("/api/roster/import", json={"csv": ROSTER})
    ids = [r["id"] for r in client.get("/api/racers").json()]
    client.post("/api/sessions", json={"kind": "chart", "racer_ids": ids, "format": "rotation", "runs_per_lane": 1})
    hid = client.post("/api/race/next").json()["heat_id"]
    client.post("/api/race/simulate")
    wait_for(lambda: client.get("/api/state").json()["state"] == "results")
    assert client.post("/api/race/rerun").json()["heat_id"] == hid
    state = client.get("/api/state").json()
    assert state["state"] == "staged" and all(l["time"] is None for l in state["current"]["lanes"])
    out = client.post("/api/race/manual", json={"heat_id": hid, "entries": {
        "1": {"time": 2.6}, "2": {"time": 2.5}, "3": {"dnf": True}}}).json()
    places = {r["lane"]: r["place"] for r in out["rows"]}
    assert places == {1: 2, 2: 1, 3: None}
    assert out["attempt"] == 2  # attempt 1 was invalidated by the rerun


def test_double_elimination_session(client):
    client.post("/api/roster/import", json={"csv": ROSTER})
    ids = [r["id"] for r in client.get("/api/racers").json()]
    s = client.post("/api/sessions", json={"kind": "elimination", "racer_ids": ids, "seed": 1}).json()
    assert s["heats"] == 2  # 6 cars, 3 lanes -> two heats in round 1 Bracket A
    for _ in range(60):
        if client.post("/api/race/next").json()["heat_id"] is None:
            break
        client.post("/api/race/simulate")
        wait_for(lambda: client.get("/api/state").json()["state"] == "results")
    b = client.get("/api/bracket").json()
    assert b["champion"] is not None
    st = client.get("/api/standings").json()
    assert st["complete"] and st["overall"][0]["losses"] < 2


def test_admin_pin(client):
    client.post("/api/settings", json={"admin_pin": "6363"})
    assert client.post("/api/race/next").status_code == 401
    assert client.post("/api/race/next", headers={"X-Admin-Pin": "6363"}).status_code == 200
    assert client.get("/api/state").status_code == 200  # spectators never need the PIN


def test_photo_finish_with_synthetic_camera(client):
    client.post("/api/settings", json={"camera_kind": "synthetic", "photo_enabled": True,
                                       "photo_margin_ms": 10000})
    client.post("/api/roster/import", json={"csv": ROSTER})
    ids = [r["id"] for r in client.get("/api/racers").json()]
    client.post("/api/sessions", json={"kind": "chart", "racer_ids": ids})
    client.post("/api/race/next")
    time.sleep(0.3)  # let the ring buffer fill
    client.post("/api/race/simulate")
    photo = wait_for(lambda: client.get("/api/state").json()["photo"], timeout=10)
    assert set(photo["images"]) == {"still", "strip"}
    assert client.get(photo["images"]["still"]).status_code == 200


def test_websocket_pushes_state(client):
    with client.websocket_connect("/ws") as ws:
        first = ws.receive_json()
        assert first["type"] == "state"
        client.post("/api/display", json={"mode": "standings"})
        msg = ws.receive_json()
        assert msg["state"]["display_mode"] == "standings"


def test_every_heat_photographed_but_only_close_ones_flagged(client):
    client.post("/api/settings", json={"camera_kind": "synthetic", "photo_enabled": True, "photo_margin_ms": 0.0})
    client.post("/api/roster/import", json={"csv": ROSTER})
    ids = [r["id"] for r in client.get("/api/racers").json()]
    client.post("/api/sessions", json={"kind": "chart", "racer_ids": ids})
    for _ in range(2):
        hid = client.post("/api/race/next").json()["heat_id"]
        time.sleep(0.3)
        client.post("/api/race/simulate")
        wait_for(lambda: (client.get("/api/state").json()["photo"] or {}).get("heat_id") == hid, timeout=10)
    finishes = client.get("/api/finishes").json()
    assert len(finishes) == 2
    assert all(f["photo"]["close"] is False for f in finishes)  # threshold 0 ms: nothing is "close"
    assert client.get(f"/api/heats/{finishes[0]['heat_id']}/photo").json()["images"]["strip"]
    racer = finishes[0]["lanes"][0]["racer_id"]
    runs = client.get(f"/api/racers/{racer}").json()["runs"]
    assert any(r["photo"] for r in runs)


def test_qr_codes(client):
    r = client.get("/api/qr.svg")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg")
    assert client.get("/api/qr.svg?target=wifi").status_code == 404
    client.post("/api/settings", json={"wifi_ssid": "Pack63-Derby", "wifi_password": "go fast",
                                       "public_url": "http://derby.local:8000/"})
    assert client.get("/api/qr.svg?target=wifi").status_code == 200
    info = client.get("/api/join-info").json()
    assert info["spectator_url"] == "http://derby.local:8000/s"
    assert client.get("/poster").status_code == 200


def test_round_robin_qualifiers_then_champions_bracket(client):
    client.post("/api/roster/import", json={"csv": ROSTER})
    ids = [r["id"] for r in client.get("/api/racers").json()]
    q = client.post("/api/sessions", json={"kind": "chart", "racer_ids": ids, "format": "round_robin",
                                           "scope": "pack", "seed": 3}).json()
    heats = client.get(f"/api/sessions/{q['id']}/heats").json()
    met = {frozenset((a["racer_id"], b["racer_id"])) for h in heats for a in h["lanes"] for b in h["lanes"] if a != b}
    assert len(met) == 15  # 6 cars -> every one of the 15 pairings raced
    while client.post("/api/race/next").json()["heat_id"]:
        client.post("/api/race/simulate")
        wait_for(lambda: client.get("/api/state").json()["state"] == "results")
    champ = client.post("/api/sessions", json={"kind": "elimination", "seed_from_session": q["id"], "per_den": 1}).json()
    assert champ["heats"] == 1  # top car from each of 3 dens -> one heat of 3
    b = client.get("/api/bracket").json()
    assert b["rounds"][0]["matchups"][0]["bracket"] == "F"  # 3 cars fit on 3 lanes: straight to the Final


def test_qualifier_spec_three_lanes_two_runs_drop_first_and_last(client):
    """3 lanes x 2 runs per lane = 6 races per car; drop fastest & slowest -> average of middle 4."""
    client.post("/api/settings", json={"lane_count": 3, "runs_per_lane": 2, "drop_runs": "fastest_slowest"})
    client.post("/api/roster/import", json={"csv": ROSTER})
    ids = [r["id"] for r in client.get("/api/racers").json()]
    q = client.post("/api/sessions", json={"kind": "chart", "racer_ids": ids}).json()
    assert q["heats"] == 12  # 6 cars x 6 runs / 3 lanes
    while client.post("/api/race/next").json()["heat_id"]:
        client.post("/api/race/simulate")
        wait_for(lambda: client.get("/api/state").json()["state"] == "results")
    st = client.get("/api/standings").json()
    for row in st["overall"]:
        assert row["runs"] == 6 and sorted(row["lanes"]) == [1, 1, 2, 2, 3, 3]
        middle = sorted(row["times"])[1:-1]
        assert row["counted_runs"] == 4
        assert abs(row["average"] - sum(middle) / 4) < 1e-9
    # Flip the standings toggle to heat points: live re-rank, no new session needed.
    client.post("/api/settings", json={"standings_method": "points"})
    pts = client.get("/api/standings").json()
    assert pts["method"] == "points"
    top = pts["overall"][0]
    assert top["points"] == max(r["points"] for r in pts["overall"])
    assert all(r["points"] <= 6 * 3 for r in pts["overall"])
    assert client.post("/api/settings", json={"standings_method": "bogus"}).status_code == 400
