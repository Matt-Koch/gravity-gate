import asyncio
import json
import socket
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gravitygate.fleet import identity, protocol, roles as R
from gravitygate.fleet.agent import Agent, AgentConfig
from gravitygate.fleet.console import ConsoleModel, render
from gravitygate.fleet.identity import Machine
from gravitygate.fleet.registry import FleetRegistry
from gravitygate.db import Database


# -- identity ---------------------------------------------------------------

def _fake_sys(root: Path):
    def w(rel, text):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    w("sys/class/net/lo/address", "00:00:00:00:00:00")
    w("sys/class/net/wlan0/address", "dc:a6:32:00:00:99")
    w("sys/class/net/eth0/address", "dc:a6:32:11:a3:f2")
    w("sys/class/net/docker0/address", "02:42:ac:11:00:02")
    w("sys/class/drm/card1-HDMI-A-1/status", "connected")
    w("sys/class/drm/card1-HDMI-A-2/status", "disconnected")
    w("sys/class/drm/card1-Writeback-1/status", "unknown")
    w("sys/class/video4linux/video0/name", "unicam-image")
    w("sys/class/video4linux/video10/name", "bcm2835-codec-decode")
    w("sys/class/video4linux/video19/name", "bcm2835-isp")
    w("proc/device-tree/model", "Raspberry Pi 4 Model B Rev 1.5\x00")
    (root / "dev").mkdir()
    (root / "dev/ttyUSB0").write_text("")
    (root / "dev/ttyS0").write_text("")


def test_identity_from_fake_sysfs(tmp_path):
    _fake_sys(tmp_path)
    sysr, dev, proc = str(tmp_path / "sys"), str(tmp_path / "dev"), str(tmp_path / "proc")
    assert identity.primary_mac(sysr) == "dc:a6:32:11:a3:f2"      # wired beats Wi-Fi and docker
    assert identity.short_id("dc:a6:32:11:a3:f2") == "A3F2"
    assert identity.detect_hdmi(sysr) == ["HDMI-A-1"]
    assert identity.detect_camera(sysr) == "pi"                    # codec/isp nodes ignored
    assert identity.detect_serial(dev) == [str(tmp_path / "dev/ttyUSB0")]
    assert identity.detect_model(proc, sysr) == "Raspberry Pi 4 Model B Rev 1.5"


def test_usb_webcam_and_no_camera(tmp_path):
    p = tmp_path / "class/video4linux/video0"
    p.mkdir(parents=True)
    (p / "name").write_text("HD Pro Webcam C920")
    assert identity.detect_camera(str(tmp_path)) == "usb"
    assert identity.detect_camera(str(tmp_path / "nothing")) is None


# -- protocol ---------------------------------------------------------------

def test_beacons_signed_and_unsigned():
    msg = {"t": "brain", "id": "A3F2", "port": 8000}
    assert protocol.decode(protocol.encode(msg))["id"] == "A3F2"
    signed = protocol.encode(msg, "secret")
    assert protocol.decode(signed, "secret")["port"] == 8000
    assert protocol.decode(signed, "wrong") is None
    assert protocol.decode(protocol.encode(msg), "secret") is None     # unsigned refused when keyed
    tampered = json.loads(signed)
    tampered["b"] = tampered["b"].replace("8000", "9999")
    assert protocol.decode(json.dumps(tampered).encode(), "secret") is None
    assert protocol.decode(b"not json") is None
    assert protocol.decode(json.dumps({"v": "other"}).encode()) is None


# -- roles ------------------------------------------------------------------

FLEET = [
    {"id": "B000", "hdmi": ["HDMI-A-1"], "camera": None, "serial": [], "roles": ["brain"]},
    {"id": "F111", "hdmi": [], "camera": "pi", "serial": ["/dev/ttyUSB0"], "roles": []},
    {"id": "D222", "hdmi": ["HDMI-A-1"], "camera": None, "serial": [], "roles": []},
    {"id": "D333", "hdmi": ["HDMI-A-1"], "camera": "usb", "serial": [], "roles": []},
    {"id": "N444", "hdmi": [], "camera": None, "serial": [], "roles": []},
]


def test_auto_arrange_by_hardware():
    plan = R.suggest(FLEET)
    assert "B000" not in plan                       # the brain is left alone
    assert plan["F111"] == ["finish"]               # serial + Pi camera
    assert plan["D222"] == ["display"] and plan["D333"] == ["display"]
    assert plan["N444"] == []                       # nothing attached: standby


def test_no_finish_without_timer_or_camera():
    plan = R.suggest([{"id": "D1", "hdmi": ["HDMI-A-1"], "roles": []}, {"id": "X", "roles": []}])
    assert plan == {"D1": ["display"], "X": []}


def test_role_commands():
    m = {"id": "F111", "serial": ["/dev/ttyUSB0"], "camera": "pi"}
    cmds = R.commands(["finish", "display"], "http://10.42.0.1:8000", m,
                      {"finish": {"profile": "microwizard", "lanes": 4}}, fleet_key="k")
    assert cmds["finish"][:4] == ["gravitygate", "node", "--hub", "ws://10.42.0.1:8000"]
    assert cmds["finish"][cmds["finish"].index("--serial") + 1] == "/dev/ttyUSB0"
    assert cmds["finish"][cmds["finish"].index("--camera") + 1] == "pi"
    assert cmds["finish"][cmds["finish"].index("--profile") + 1] == "microwizard"
    assert cmds["finish"][-2:] == ["--token", "k"]
    assert cmds["display"] == ["gravitygate-kiosk", "http://10.42.0.1:8000/display?kiosk=1&screen=F111"]
    brain = R.commands(["brain", "display"], "", {"id": "B000"})
    assert brain["brain"][:2] == ["gravitygate", "serve"]
    assert brain["display"][1].startswith("http://127.0.0.1:8000/display")
    assert R.commands([], "x", {}) == {}
    assert R.label(["display", "brain"]) == "Brain + Display" and R.label([]) == "Standby"


# -- console ----------------------------------------------------------------

def _view(roles=(), brain_url=None, fleet=()):
    m = {"id": "A3F2", "ips": ["10.42.0.17"], "hostname": "gg", "model": "Pi", "hdmi": ["HDMI-A-1"],
         "camera": None, "serial": [], "version": "0.1.0"}
    return {"machine": m, "roles": list(roles), "role_label": R.label(roles), "brain_url": brain_url,
            "brain_link": bool(brain_url), "conflict": [], "processes": {}, "fleet": list(fleet),
            "features": {}, "message": "", "identify": False}


def test_console_home_keys():
    c = ConsoleModel()
    assert c.handle("d", _view()) == ("local", ["display"])
    assert c.handle("D", _view(["display"])) == ("local", [])
    assert c.handle("f", _view(["display"])) == ("local", ["display", "finish"])
    assert c.handle("s", _view(["display", "finish"])) == ("local", [])
    assert c.handle("b", _view()) == ("local", ["brain"])
    # A brain is already on the network: B needs a second press.
    assert c.handle("b", _view(brain_url="http://x:8000")) is None
    assert c.handle("b", _view(brain_url="http://x:8000")) == ("local", ["brain"])
    assert c.handle("enter", _view()) is None and c.mode == "home"   # fleet screen is brain-only
    assert c.handle("enter", _view(["brain"])) is None and c.mode == "fleet"


def test_console_fleet_keys_and_render():
    fleet = [{"id": "B000", "is_self": True, "roles": ["brain"], "online": True},
             {"id": "F111", "roles": [], "online": True, "ips": ["10.0.0.2"], "role_label": "Standby"},
             {"id": "D222", "roles": ["display"], "online": True, "ips": ["10.0.0.3"], "role_label": "Display"}]
    v = _view(["brain"], fleet=fleet)
    c = ConsoleModel()
    c.handle("enter", v)
    assert c.handle("f", v) == ("brain", {"type": "assign", "id": "F111", "roles": ["finish"]})
    c.handle("down", v)
    assert c.handle("d", v) == ("brain", {"type": "assign", "id": "D222", "roles": []})
    assert c.handle("i", v) == ("brain", {"type": "identify", "id": "D222"})
    assert c.handle("a", v) == ("brain", {"type": "auto_arrange"})
    text = "\n".join(t for t, _ in render(v, c, 100))
    assert "F111" in text and "D222" in text and "> D222" in text
    c.handle("escape", v)
    home = "\n".join(t for t, _ in render(_view(), ConsoleModel(), 100))
    assert "A3F2" in home and "10.42.0.17" in home and "[B] Brain" in home


# -- registry ---------------------------------------------------------------

class Inbox:
    def __init__(self):
        self.msgs = []

    async def __call__(self, msg):
        self.msgs.append(msg)


def test_registry_assignment_rules():
    async def go():
        reg = FleetRegistry(Database(), self_id="B000")
        a, b, brain = Inbox(), Inbox(), Inbox()
        await reg.agent_hello({"id": "B000"}, ["brain"], False, brain)
        await reg.agent_hello({"id": "F111", "serial": ["/dev/ttyUSB0"], "camera": "pi"}, [], False, a)
        await reg.agent_hello({"id": "F222", "serial": ["/dev/ttyUSB0"]}, [], False, b)
        assert await reg.assign("F111", ["finish", "brain"]) == ["finish"]   # brain can't be granted remotely
        assert a.msgs[-1] == {"type": "assign", "roles": ["finish"], "params": reg.params()}
        await reg.assign("F222", ["finish"])                                  # only one finish line
        assert reg.machines["F111"]["roles"] == [] and a.msgs[-1]["roles"] == []
        assert await reg.assign("B000", ["display"]) == ["brain", "display"]  # brain keeps its role
        f = reg.features()
        assert f["finish_online"] and f["finish_camera"] is None and f["displays"] == 1
        # Reconnect: the brain's stored assignment wins over what the agent reports...
        await reg.agent_hello({"id": "F222"}, [], False, b)
        assert b.msgs[-1]["roles"] == ["finish"]
        # ...unless the role was picked on that machine's keyboard while offline.
        await reg.agent_hello({"id": "F222"}, ["display"], True, b)
        assert reg.machines["F222"]["roles"] == ["display"]
        with pytest.raises(KeyError):
            await reg.assign("ZZZZ", ["display"])
        reg.agent_gone("F111")
        assert reg.machines["F111"]["connected"] is False
    asyncio.run(go())


# -- server API -------------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    from gravitygate.server import create_app
    with TestClient(create_app(tmp_path)) as c:
        yield c


HELLO = {"type": "hello", "roles": [], "local_change": False,
         "machine": {"id": "F111", "ips": ["10.0.0.2"], "hdmi": [], "camera": "pi", "serial": ["/dev/ttyUSB0"]}}


def test_agent_websocket_and_feature_gating(client):
    feats = client.get("/api/state").json()["features"]
    assert feats["finish_online"] is False and feats["photo_available"] is False
    assert feats["timer_kind"] in ("simulator", "serial")
    with client.websocket_connect("/ws/agent") as ws:
        ws.send_text(json.dumps(HELLO))
        assert ws.receive_json()["type"] == "assign"        # new machine: keeps its own (empty) roles
        fleet = client.get("/api/fleet").json()
        assert [m["id"] for m in fleet["machines"]] == ["F111"] and fleet["machines"][0]["online"]
        r = client.post("/api/fleet/f111", json={"roles": ["finish"]})
        assert r.json()["roles"] == ["finish"]
        msg = ws.receive_json()
        assert msg["type"] == "assign" and msg["roles"] == ["finish"] and msg["params"]["finish"]["lanes"] == 3
        feats = client.get("/api/state").json()["features"]
        assert feats["finish_online"] and feats["photo_available"] and feats["timer_kind"] == "remote"
        assert client.get("/api/state").json()["timer"]["type"] == "RemoteTimer"   # auto timer followed
        # A non-brain agent may not arrange other machines.
        ws.send_text(json.dumps({"type": "assign", "id": "F111", "roles": []}))
        assert ws.receive_json()["type"] == "error"
        client.post("/api/fleet/F111/identify")
        assert ws.receive_json()["type"] == "identify"
    assert client.post("/api/fleet/NOPE", json={"roles": []}).status_code == 404


def test_brain_console_can_arrange(client):
    with client.websocket_connect("/ws/agent") as brain, client.websocket_connect("/ws/agent") as disp:
        brain.send_text(json.dumps({**HELLO, "roles": ["brain"],
                                    "machine": {"id": "B000", "hdmi": ["HDMI-A-1"]}}))
        assert brain.receive_json()["roles"] == ["brain"]
        disp.send_text(json.dumps({**HELLO, "machine": {"id": "D222", "hdmi": ["HDMI-A-1"]}}))
        assert disp.receive_json()["type"] == "assign"
        brain.send_text(json.dumps({"type": "auto_arrange"}))
        assert disp.receive_json()["roles"] == ["display"]
        msgs = []
        while not any(m.get("type") == "message" for m in msgs):
            msgs.append(brain.receive_json())
        assert any(m["type"] == "fleet" for m in msgs + [brain.receive_json()])


# -- end to end: real UDP discovery + websocket + supervisor -----------------

def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_agent_discovers_brain_and_runs_assigned_role(tmp_path, monkeypatch):
    import uvicorn
    from gravitygate.server import create_app

    monkeypatch.setenv("GRAVITYGATE_FLEET", "1")
    monkeypatch.setenv("GRAVITYGATE_FLEET_KEY", "e2e-key")
    monkeypatch.setenv("GRAVITYGATE_MACHINE_ID", "B000")
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(tmp_path / "brain", port), host="0.0.0.0", port=port,
                                           log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    while not server.started:
        time.sleep(0.05)

    machine = Machine(id="F111", mac="dc:a6:32:11:f1:11", hostname="finish", ips=["10.0.0.2"], model="test",
                      hdmi=[], camera="pi", serial=["/dev/ttyUSB0"])
    agent = Agent(AgentConfig(state_dir=str(tmp_path / "agent"), fleet_key="e2e-key", brain_port=port,
                              beacon_s=0.3), machine)
    launched = []

    def fake_commands(roles, brain_url, m, params, **kw):
        cmds = R.commands(roles, brain_url, m, params, **kw)
        launched.append((sorted(cmds), brain_url))
        return {role: ["sleep", "30"] for role in cmds}
    agent.command_builder = fake_commands

    import urllib.request

    def api(path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST" if body is not None else "GET",
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=5).read())

    async def scenario():
        task = asyncio.create_task(agent.run())
        try:
            async def until(pred, timeout=10):
                end = time.time() + timeout
                while time.time() < end:
                    if pred():
                        return True
                    await asyncio.sleep(0.1)
                raise AssertionError("timed out")
            await until(lambda: agent.brain_link_up)                    # found the brain by broadcast
            assert agent.brain_url.endswith(f":{port}")
            fleet = await asyncio.to_thread(api, "/api/fleet")
            assert fleet["machines"][0]["id"] == "F111"
            await asyncio.to_thread(api, "/api/fleet/F111", {"roles": ["finish"]})
            await until(lambda: agent.supervisor.status.get("finish", {}).get("state") == "running")
            assert agent.roles == ["finish"]
            assert json.loads((tmp_path / "agent/agent.json").read_text())["roles"] == ["finish"]
            feats = (await asyncio.to_thread(api, "/api/state"))["features"]
            assert feats["finish_online"] and feats["timer_kind"] == "remote"
            # Local keyboard choice is reported back to the brain.
            await agent.set_local_roles(["display"])
            await until(lambda: "finish" not in agent.supervisor.status)
            await until(lambda: (fleetnow := api("/api/fleet"))["machines"][0]["roles"] == ["display"])
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            await agent.supervisor.stop_all()
    try:
        asyncio.run(scenario())
    finally:
        server.should_exit = True
        th.join(5)
