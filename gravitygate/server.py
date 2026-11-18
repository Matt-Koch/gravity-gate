"""FastAPI hub: REST API, WebSocket push, static pages, node link."""
from __future__ import annotations

import asyncio
import base64
import csv
import io
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import roster, sync
from .db import Database
from .fleet import protocol as fleet_protocol
from .fleet import roles as fleet_roles
from .fleet.registry import FleetRegistry
from .race import RaceController
from .timers import PROFILES, BaseTimer, ParsedResult, SimulatedTimer, make_timer

log = logging.getLogger("gravitygate.server")
STATIC = Path(__file__).parent / "static"


def lan_ip() -> str | None:
    """Best guess at this Pi's address on the event network (works offline too)."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.254.254.254", 1))  # no packet is sent for UDP connect
        ip = s.getsockname()[0]
        return None if ip.startswith("127.") else ip
    except OSError:
        return None
    finally:
        s.close()


def spectator_url(db: Database, request: Request) -> str:
    public = (db.get_setting("public_url") or "").strip().rstrip("/")
    if public:
        return public + "/s"
    host = request.url.hostname or ""
    port = request.url.port
    if host in ("localhost", "127.0.0.1", "::1", "") or host.startswith("127."):
        host = lan_ip() or host  # the projector browser on the Pi sees localhost; phones can't use that
    netloc = host if port in (None, 80) else f"{host}:{port}"
    return f"{request.url.scheme}://{netloc}/s"


def wifi_qr_payload(ssid: str, password: str) -> str:
    esc = lambda v: "".join("\\" + c if c in '\\;,:"' else c for c in v)
    auth = "WPA" if password else "nopass"
    return f"WIFI:T:{auth};S:{esc(ssid)};" + (f"P:{esc(password)};" if password else "") + ";"


class RemoteTimer(BaseTimer):
    """The timer lives on a node Pi; arm/force are forwarded over its WebSocket."""

    def __init__(self, lane_count: int, callback, hub: "Hub"):
        super().__init__(lane_count, callback)
        self.hub = hub

    def arm(self, lanes: set[int]) -> None:
        super().arm(lanes)
        self.hub.send_nodes({"cmd": "arm", "lanes": sorted(lanes)})

    def force(self) -> None:
        self.hub.send_nodes({"cmd": "force"})

    def status(self) -> dict:
        return {**super().status(), "type": "RemoteTimer", "nodes": list(self.hub.node_info.values()),
                "connected": any(n.get("timer_connected") for n in self.hub.node_info.values())}


class Hub:
    def __init__(self, data_dir: Path, port: int = 8000):
        self.port = port
        self.data_dir = data_dir
        self.photo_dir = data_dir / "photos"
        self.photo_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(data_dir / "derby.sqlite3")
        self.controller = RaceController(self.db)
        self.sync = sync.SyncService(self.db, self.controller)
        self.controller.change_listeners.append(self.sync.mark_dirty)
        self.clients: set[WebSocket] = set()
        self.nodes: dict[WebSocket, asyncio.Queue] = {}
        self.node_info: dict[int, dict] = {}
        self.camera = None
        self.controller.subscribers.add(self.broadcast)
        self.controller.photo_requesters.add(self.request_photo)
        self.controller.arm_listeners.append(lambda heat: self.camera and self.camera.mark_armed())
        # Fleet: the same image on every box; this hub is the brain they report to.
        self.fleet_key = os.environ.get("GRAVITYGATE_FLEET_KEY", "")
        self.fleet = FleetRegistry(self.db, os.environ.get("GRAVITYGATE_MACHINE_ID", ""))
        self.fleet.listeners.append(self._fleet_changed)
        self.controller.feature_probe = self.features
        self._fleet_tasks: list[asyncio.Task] = []
        self.last_snapshot: dict | None = None

    # -- fleet & features ----------------------------------------------------
    def features(self) -> dict:
        """What the hardware that's online right now can do. The UI hides what isn't there."""
        f = self.fleet.features()
        node_cams = [n.get("camera") for n in self.node_info.values() if n.get("camera") not in (None, "none")]
        f["nodes_connected"] = len(self.nodes)
        f["photo_available"] = bool(self.camera is not None or node_cams or f.get("finish_camera") == "pi")
        setting = self.db.get_setting("photo_enabled")
        f["photo_active"] = f["photo_available"] and setting is not False and setting != "off"
        f["timer_kind"] = self.resolve_timer_kind()
        return f

    def resolve_timer_kind(self) -> str:
        """'auto' picks the timer from what's plugged in: finish-line machine > local serial > simulator."""
        kind = self.db.get_setting("timer_kind") or "auto"
        if kind != "auto":
            return kind
        if self.nodes or self.fleet.features().get("finish_online"):
            return "remote"
        from .fleet.identity import detect_serial
        if detect_serial():
            return "serial"
        return "simulator"

    def _fleet_changed(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._after_fleet_change())

    async def _after_fleet_change(self) -> None:
        await self.fleet.broadcast_fleet()
        await self.controller.publish("fleet", {"fleet": self.fleet.snapshot(), "features": self.features()})
        await self.retime_if_needed()

    async def retime_if_needed(self) -> None:
        want = self.resolve_timer_kind()
        have = {"RemoteTimer": "remote", "SerialTimer": "serial", "SimulatedTimer": "simulator",
                "NullTimer": "none"}.get(type(self.controller.timer).__name__)
        if want != have:
            await self.configure_timer()
            await self.controller.publish()

    async def start_fleet(self) -> None:
        if os.environ.get("GRAVITYGATE_FLEET", "1") == "0":
            return
        if not self.fleet.self_id:
            from .fleet.identity import identify
            self.fleet.self_id = (await asyncio.to_thread(identify)).id
        try:
            self._fleet_transport = await fleet_protocol.listen(self.fleet.on_beacon, self.fleet_key)
        except OSError as exc:
            log.warning("fleet discovery disabled: %s", exc)
            self._fleet_transport = None

        async def beacon():
            while True:
                fleet_protocol.send({"t": "brain", "id": self.fleet.self_id, "port": self.port,
                                     "event": self.db.get_setting("event_name")}, self.fleet_key)
                await asyncio.sleep(2)

        async def sweep():  # notice machines going quiet
            seen = None
            while True:
                await asyncio.sleep(5)
                now = tuple((m["id"], m["online"]) for m in self.fleet.snapshot())
                if now != seen:
                    seen = now
                    await self._after_fleet_change()

        self._fleet_tasks = [asyncio.create_task(beacon()), asyncio.create_task(sweep())]

    async def stop_fleet(self) -> None:
        for t in self._fleet_tasks:
            t.cancel()
        if getattr(self, "_fleet_transport", None):
            self._fleet_transport.close()

    # -- timers ------------------------------------------------------------
    async def configure_timer(self) -> None:
        await self.controller.timer.stop()
        s = self.db.all_settings()
        kind = self.resolve_timer_kind()
        lanes = int(s["lane_count"])
        if kind == "remote":
            timer: BaseTimer = RemoteTimer(lanes, self.controller.on_timer_event, self)
        else:
            port = s.get("timer_port") or "/dev/ttyUSB0"
            if s.get("timer_kind") == "auto":
                from .fleet.identity import detect_serial
                port = (detect_serial() or [port])[0]
            timer = make_timer(kind, lanes, self.controller.on_timer_event,
                               port=port,
                               profile=s.get("timer_profile", "generic"))
        self.controller.set_timer(timer)
        await timer.start()
        await self.configure_camera()

    async def configure_camera(self) -> None:
        if self.camera is not None:
            self.camera.stop()
            self.camera = None
        kind = self.db.get_setting("camera_kind") or "none"
        if kind == "none":
            return
        from .photofinish.camera import PiCameraSource, SyntheticCameraSource
        if kind == "pi":
            self.camera = PiCameraSource()
        else:
            cam = SyntheticCameraSource(self.db.active_lanes())
            self.camera = cam
            timer = self.controller.timer
            if isinstance(timer, SimulatedTimer):
                def stage(times, delay, cam=cam):
                    done = [t for t in times.values() if t is not None]
                    if done:
                        cam.schedule_race(times, time.monotonic() + delay - (max(done) - min(done)))
                timer.observers.append(stage)
        self.camera.start()

    def calibration(self):
        from .photofinish.analysis import calibration_from_dict
        cam = self.camera
        return calibration_from_dict(self.db.get_setting("camera_calibration"), cam.width, cam.height,
                                     self.db.active_lanes())

    async def request_photo(self, req: dict) -> None:
        if self.nodes:
            self.send_nodes({"cmd": "photo", **req, "calibration": self.db.get_setting("camera_calibration")})
        if self.camera is not None:
            from .photofinish.camera import capture_photo_finish
            # Post-roll: the timer reports the instant the last car crosses, so keep
            # filming a moment longer or the last car can be cut off the strip.
            await asyncio.sleep(float(self.db.get_setting("photo_post_roll_s") or 0))
            name = f"heat{req['heat_id']}-a{req['attempt']}"
            times = {int(k): v for k, v in req["times"].items()}
            title = f"Heat {req['heat_id']}"
            paths = await asyncio.to_thread(capture_photo_finish, self.camera, self.calibration(), times,
                                            self.photo_dir, name, req["margin_ms"], title)
            await self.controller.photo_ready(req["heat_id"], req["attempt"],
                                              {k: "/photos/" + Path(p).name for k, p in paths.items()},
                                              req["margin_ms"])

    # -- camera check ----------------------------------------------------------
    async def request_snapshot(self) -> int:
        """Ask every camera (finish-line node and/or this machine) for a calibration view."""
        saved = self.db.get_setting("camera_calibration")
        sources = 0
        if self.nodes:
            self.send_nodes({"cmd": "snapshot", "calibration": saved})
            sources += sum(1 for n in self.node_info.values() if n.get("camera") not in (None, "none"))
        if self.camera is not None:
            from .photofinish.analysis import check_image
            _, frames = self.camera.buffer.snapshot()
            if frames:
                img = await asyncio.to_thread(check_image, frames[-1], self.calibration())
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=88)
                await self.snapshot_ready(buf.getvalue(), img.width, img.height)
                sources += 1
        return sources

    async def snapshot_ready(self, jpeg: bytes, width: int, height: int) -> None:
        for old in self.photo_dir.glob("camera-check-*.jpg"):
            old.unlink(missing_ok=True)
        name = f"camera-check-{int(time.time() * 1000)}.jpg"
        (self.photo_dir / name).write_bytes(jpeg)
        self.last_snapshot = {"url": "/photos/" + name, "width": width, "height": height, "at": time.time()}
        await self.controller.publish("snapshot", {"snapshot": self.last_snapshot})

    # -- websockets ----------------------------------------------------------
    async def broadcast(self, msg: dict) -> None:
        data = json.dumps(msg, default=str)
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    def send_nodes(self, msg: dict) -> None:
        for q in self.nodes.values():
            q.put_nowait(msg)


def create_app(data_dir: str | Path = "data", port: int = 8000) -> FastAPI:
    hub = Hub(Path(data_dir), port)
    db, ctl = hub.db, hub.controller

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await hub.configure_timer()
        hub.sync.start()
        await hub.start_fleet()
        yield
        await hub.stop_fleet()
        await hub.sync.stop()
        await ctl.timer.stop()
        if hub.camera:
            hub.camera.stop()

    app = FastAPI(title="Gravity Gate", lifespan=lifespan)
    app.state.hub = hub

    def admin(x_admin_pin: str | None = Header(default=None)) -> None:
        pin = db.get_setting("admin_pin")
        if pin and x_admin_pin != pin:
            raise HTTPException(401, "admin PIN required")

    # -- pages ---------------------------------------------------------------
    @app.get("/")
    async def root():
        return RedirectResponse("/s")

    for page, file in {"/admin": "admin.html", "/display": "display.html", "/s": "spectator.html",
                       "/poster": "poster.html"}.items():
        app.add_api_route(page, (lambda f=file: FileResponse(STATIC / f)), methods=["GET"],
                          include_in_schema=False)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    app.mount("/photos", StaticFiles(directory=hub.photo_dir), name="photos")

    # -- read API (spectators) -----------------------------------------------
    @app.get("/api/state")
    async def state():
        return ctl.snapshot()

    @app.get("/api/standings")
    async def standings(session_id: int | None = None):
        return ctl.standings(session_id)

    @app.get("/api/awards")
    async def awards(session_id: int | None = None, per_den: int = 3, overall: int = 3):
        return ctl.awards(session_id, per_den, overall)

    @app.get("/api/bracket")
    async def bracket(session_id: int | None = None):
        return ctl.bracket(session_id)

    @app.get("/api/stats")
    async def stats():
        return ctl.stats()

    @app.get("/api/racers")
    async def racers():
        return db.racers()

    @app.get("/api/racers/{racer_id}")
    async def racer(racer_id: int):
        try:
            return ctl.racer_detail(racer_id)
        except KeyError:
            raise HTTPException(404, "no such racer")

    @app.get("/api/sessions")
    async def sessions():
        return db.sessions()

    @app.get("/api/sessions/{session_id}/heats")
    async def heats(session_id: int):
        racers = db.racer_map()
        return [ctl.heat_view(h, racers) for h in db.heats(session_id)]

    @app.get("/api/join-info")
    async def join_info(request: Request):
        return {"spectator_url": spectator_url(db, request), "wifi_ssid": db.get_setting("wifi_ssid") or "",
                "event_name": db.get_setting("event_name")}

    @app.get("/api/qr.svg")
    async def qr(request: Request, target: str = "spectator", dark: str = "#0b1020", light: str = "#ffffff"):
        """QR code generated on the Pi (no internet needed). target = spectator | wifi"""
        import segno
        if target == "wifi":
            ssid = db.get_setting("wifi_ssid") or ""
            if not ssid:
                raise HTTPException(404, "no Wi-Fi network configured")
            data = wifi_qr_payload(ssid, db.get_setting("wifi_password") or "")
        else:
            data = spectator_url(db, request)
        buf = io.BytesIO()
        segno.make(data, error="m").save(buf, kind="svg", scale=10, border=2, dark=dark, light=light,
                                         xmldecl=False, svgclass=None)
        return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-cache"})

    @app.get("/api/photos")
    async def photos(limit: int = 30):
        return db.photos(limit=limit)

    @app.get("/api/finishes")
    async def finishes(limit: int = 12):
        """Recent finish-line photos with the heat results, for the phone gallery."""
        return ctl.recent_finishes(limit)

    @app.get("/api/heats/{heat_id}/photo")
    async def heat_photo(heat_id: int):
        photo = ctl.heat_photo(heat_id)
        if photo is None:
            raise HTTPException(404, "no photo for this heat")
        return photo

    # -- admin: racers & roster ---------------------------------------------
    @app.post("/api/racers", dependencies=[Depends(admin)])
    async def add_racer(body: dict = Body(...)):
        try:
            rid = db.add_racer(body.get("first_name", ""), body.get("last_name", ""), body.get("den", ""),
                               body.get("rank", ""), body.get("car_number"), body.get("checked_in", False))
        except Exception as exc:
            raise HTTPException(400, str(exc))
        await ctl.publish("racers")
        return {"id": rid}

    @app.patch("/api/racers/{racer_id}", dependencies=[Depends(admin)])
    async def update_racer(racer_id: int, body: dict = Body(...)):
        try:
            db.update_racer(racer_id, **body)
        except Exception as exc:
            raise HTTPException(400, str(exc))
        await ctl.publish("racers")
        return {"ok": True}

    @app.delete("/api/racers/{racer_id}", dependencies=[Depends(admin)])
    async def delete_racer(racer_id: int):
        db.delete_racer(racer_id)
        await ctl.publish("racers")
        return {"ok": True}

    @app.post("/api/roster/import", dependencies=[Depends(admin)])
    async def import_roster(body: dict = Body(...)):
        try:
            if body.get("csv"):
                res = roster.import_rows(db, roster.parse_csv(body["csv"]))
            else:
                if body.get("url"):
                    db.set_setting("roster_csv_url", body["url"])
                res = await asyncio.to_thread(sync.pull_roster, db)
        except Exception as exc:
            raise HTTPException(400, f"import failed: {exc}")
        await ctl.publish("racers")
        return res

    @app.get("/api/roster.csv")
    async def roster_csv():
        return PlainTextResponse(roster.export_csv(db), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=roster.csv"})

    @app.get("/api/results.csv")
    async def results_csv():
        buf = io.StringIO()
        csv.writer(buf).writerows(sync.results_rows(db))
        return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=results.csv"})

    @app.post("/api/sync/push", dependencies=[Depends(admin)])
    async def sync_push():
        try:
            return await hub.sync.push_now()
        except Exception as exc:
            raise HTTPException(502, f"sync failed: {exc}")

    @app.get("/api/sync/status")
    async def sync_status():
        return hub.sync.status()

    # -- admin: sessions -------------------------------------------------------
    @app.post("/api/sessions", dependencies=[Depends(admin)])
    async def create_session(body: dict = Body(...)):
        kind = body.get("kind", "chart")
        racer_ids = body.get("racer_ids")
        if not racer_ids:
            dens = set(body.get("dens") or [])
            racer_ids = [r["id"] for r in db.racers(checked_in_only=body.get("checked_in_only", True))
                         if not dens or r["den"] in dens]
        if body.get("seed_from_session"):
            from .scoring import seed_from_dens
            table = ctl.standings(int(body["seed_from_session"]))
            racer_ids = seed_from_dens(table, int(body.get("per_den", 3)))
        if len(racer_ids) < 2:
            raise HTTPException(400, "need at least 2 racers")
        try:
            if kind == "chart":
                sid = ctl.create_chart_session(body.get("name", "Qualifiers"), racer_ids,
                                               body.get("runs_per_lane"), body.get("seed"),
                                               body.get("mix_dens", True), body.get("format", "rotation"),
                                               body.get("scope", "den"))
            else:
                sid = ctl.create_elimination_session(
                    body.get("name", "Champions Bracket"), racer_ids, int(body.get("max_losses", 2)),
                    body.get("losers_per_heat", "half"), body.get("runs_per_matchup", "single"), body.get("seed"))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        if body.get("activate", True):
            ctl.set_session(sid)
        await ctl.publish()
        return {"id": sid, "heats": len(db.heats(sid))}

    @app.post("/api/sessions/{session_id}/activate", dependencies=[Depends(admin)])
    async def activate(session_id: int):
        if not db.session(session_id):
            raise HTTPException(404)
        ctl.set_session(session_id)
        await ctl.publish()
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/late-racer", dependencies=[Depends(admin)])
    async def late_racer(session_id: int, body: dict = Body(...)):
        try:
            n = ctl.add_late_racer(session_id, int(body["racer_id"]))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        await ctl.publish()
        return {"heats_added": n}

    @app.delete("/api/sessions/{session_id}", dependencies=[Depends(admin)])
    async def delete_session(session_id: int):
        db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        if ctl.session_id == session_id:
            ctl.set_session(None)
        await ctl.publish()
        return {"ok": True}

    # -- admin: race control ---------------------------------------------------
    @app.post("/api/race/next", dependencies=[Depends(admin)])
    async def next_race():
        heat = await ctl.next_race()
        return {"heat_id": heat["id"] if heat else None}

    @app.post("/api/race/rerun", dependencies=[Depends(admin)])
    async def rerun():
        heat = await ctl.rerun()
        return {"heat_id": heat["id"] if heat else None}

    @app.post("/api/race/manual", dependencies=[Depends(admin)])
    async def manual(body: dict = Body(...)):
        try:
            entries = {int(k): v for k, v in body["entries"].items()}
            return await ctl.manual_entry(int(body["heat_id"]), entries)
        except (KeyError, ValueError) as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/race/skip", dependencies=[Depends(admin)])
    async def skip(body: dict = Body(...)):
        await ctl.skip_heat(int(body["heat_id"]))
        return {"ok": True}

    @app.post("/api/race/force", dependencies=[Depends(admin)])
    async def force():
        ctl.timer.force()
        return {"ok": True}

    @app.post("/api/race/simulate", dependencies=[Depends(admin)])
    async def simulate():
        """Drop the gate on the simulated timer (rehearsals / demo)."""
        t = ctl.timer
        if not isinstance(t, SimulatedTimer):
            raise HTTPException(400, "timer is not the simulator")
        t.race(delay=0.8)
        return {"ok": True}

    @app.post("/api/display", dependencies=[Depends(admin)])
    async def display(body: dict = Body(...)):
        try:
            await ctl.set_display(body["mode"])
        except ValueError:
            raise HTTPException(400, "bad mode")
        return {"ok": True}

    @app.post("/api/awards/next", dependencies=[Depends(admin)])
    async def awards_next():
        await ctl.reveal_next_award()
        return {"revealed": ctl.awards_revealed}

    # -- admin: settings -------------------------------------------------------
    @app.get("/api/settings", dependencies=[Depends(admin)])
    async def get_settings():
        s = db.all_settings()
        s["timer_profiles"] = {k: {"name": p.name, "verified": p.verified, "notes": p.notes}
                               for k, p in PROFILES.items()}
        return s

    @app.get("/api/public-settings")
    async def public_settings():
        s = db.all_settings()
        return {k: s.get(k) for k in ("event_name", "lane_count", "sound", "track_length_ft",
                                      "scale_factor", "photo_margin_ms")} | {"pin_required": bool(s["admin_pin"])}

    @app.post("/api/settings", dependencies=[Depends(admin)])
    async def set_settings(body: dict = Body(...)):
        restart = False
        for k, v in body.items():
            if k == "timer_profiles":
                continue
            db.set_setting(k, v)
            if k == "standings_method" and v not in ("average", "points"):
                raise HTTPException(400, "standings_method must be average or points")
            if k == "drop_runs" and v not in ("none", "slowest", "fastest_slowest"):
                raise HTTPException(400, "bad drop_runs")
            restart |= k in {"timer_kind", "timer_port", "timer_profile", "lane_count", "camera_kind"}
        if restart:
            await hub.configure_timer()
        await ctl.publish()
        return {"ok": True, "timer_restarted": restart}

    @app.get("/api/serial-ports", dependencies=[Depends(admin)])
    async def serial_ports():
        try:
            from serial.tools import list_ports
            return [{"device": p.device, "description": p.description} for p in list_ports.comports()]
        except Exception:
            return []

    # -- websockets ------------------------------------------------------------
    @app.websocket("/ws")
    async def ws_clients(ws: WebSocket):
        await ws.accept()
        hub.clients.add(ws)
        try:
            await ws.send_text(json.dumps({"type": "state", "state": ctl.snapshot()}, default=str))
            while True:
                await ws.receive_text()  # keepalive pings from the page
        except WebSocketDisconnect:
            pass
        finally:
            hub.clients.discard(ws)

    # -- camera check ---------------------------------------------------------
    @app.post("/api/camera/snapshot", dependencies=[Depends(admin)])
    async def camera_snapshot():
        n = await hub.request_snapshot()
        if not n:
            raise HTTPException(409, "no finish-line camera is online")
        return {"sources": n, "snapshot": hub.last_snapshot}

    @app.get("/api/camera/snapshot")
    async def camera_snapshot_get():
        return hub.last_snapshot or {}

    @app.post("/api/camera/calibration", dependencies=[Depends(admin)])
    async def camera_calibration(body: dict = Body(...)):
        """{finish_x, edges: [y0, y1, ...] top to bottom, lane1_at_top, flip_time} or {"reset": true}."""
        if body.get("reset"):
            db.set_setting("camera_calibration", None)
            return {"calibration": None}
        try:
            edges = sorted(int(e) for e in body["edges"])
            lanes = db.active_lanes()
            if len(edges) != len(lanes) + 1:
                raise ValueError(f"need {len(lanes) + 1} lane edges for {len(lanes)} lanes")
            order = lanes if body.get("lane1_at_top", True) else list(reversed(lanes))
            cal = {"finish_x": int(body["finish_x"]),
                   "lane_bands": {str(l): [edges[i], edges[i + 1]] for i, l in enumerate(order)},
                   "threshold": float(body.get("threshold", 28.0)), "flip_time": bool(body.get("flip_time"))}
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(400, f"bad calibration: {exc}")
        db.set_setting("camera_calibration", cal)
        return {"calibration": cal}

    # -- fleet ----------------------------------------------------------------
    @app.get("/api/fleet")
    async def fleet():
        return {"machines": hub.fleet.snapshot(), "features": hub.features(),
                "roles": {k: {"name": r.name, "blurb": r.blurb, "assignable": k in fleet_roles.ASSIGNABLE}
                          for k, r in fleet_roles.ROLES.items()}}

    @app.post("/api/fleet/auto", dependencies=[Depends(admin)])
    async def fleet_auto():
        return {"plan": await hub.fleet.auto_arrange()}

    @app.post("/api/fleet/{machine_id}", dependencies=[Depends(admin)])
    async def fleet_assign(machine_id: str, body: dict = Body(...)):
        try:
            return {"roles": await hub.fleet.assign(machine_id.upper(), body.get("roles") or [])}
        except KeyError:
            raise HTTPException(404, "no such machine")

    @app.post("/api/fleet/{machine_id}/identify", dependencies=[Depends(admin)])
    async def fleet_identify(machine_id: str):
        await hub.fleet.identify(machine_id.upper())
        return {"ok": True}

    @app.websocket("/ws/agent")
    async def ws_agent(ws: WebSocket):
        """Every fleet machine's agent keeps one of these open to the brain."""
        await ws.accept()
        if hub.fleet_key and ws.query_params.get("key") != hub.fleet_key:
            await ws.close(code=4401)
            return
        mid = None
        local = (ws.client.host if ws.client else "") in ("127.0.0.1", "::1", "localhost", "testclient")

        async def send(msg: dict) -> None:
            await ws.send_text(json.dumps(msg, default=str))

        try:
            while True:
                msg = json.loads(await ws.receive_text())
                kind = msg.get("type")
                if kind == "hello":
                    mid = (msg.get("machine") or {}).get("id")
                    await hub.fleet.agent_hello(msg["machine"], msg.get("roles") or [], bool(msg.get("local_change")),
                                                send, ws.client.host if ws.client else None)
                elif mid is None:
                    continue
                elif kind == "set_roles":
                    await hub.fleet.agent_local_change(mid, msg.get("roles") or [])
                elif kind in ("assign", "auto_arrange", "identify"):
                    # Fleet commands typed on the brain's own console. Only the brain's
                    # agent, connecting over loopback, may arrange other machines.
                    if not local or fleet_roles.BRAIN not in hub.fleet.machines.get(mid, {}).get("roles", []):
                        await send({"type": "error", "message": "Only the brain's own console can arrange machines."})
                        continue
                    try:
                        if kind == "assign":
                            roles = await hub.fleet.assign(str(msg.get("id", "")).upper(), msg.get("roles") or [])
                            await send({"type": "message", "message": f"{msg.get('id')}: {fleet_roles.label(roles)}"})
                        elif kind == "auto_arrange":
                            plan = await hub.fleet.auto_arrange()
                            await send({"type": "message", "message": f"Auto-arranged {len(plan)} machine(s)."})
                        else:
                            await hub.fleet.identify(str(msg.get("id", "")).upper())
                    except KeyError:
                        await send({"type": "error", "message": f"No machine {msg.get('id')} on the network."})
        except WebSocketDisconnect:
            pass
        finally:
            if mid:
                hub.fleet.agent_gone(mid)

    @app.websocket("/ws/node")
    async def ws_node(ws: WebSocket):
        tokens = {t for t in (db.get_setting("node_token") or "", hub.fleet_key) if t}
        await ws.accept()
        if tokens and ws.query_params.get("token") not in tokens:
            await ws.close(code=4401)
            return
        q: asyncio.Queue = asyncio.Queue()
        hub.nodes[ws] = q
        node_id = id(ws)
        hub.node_info[node_id] = {"connected_at": time.time()}

        async def sender():
            # Re-arm a node that (re)connects mid-heat.
            if ctl.state == "staged" and ctl.current_heat_id:
                heat = db.heat(ctl.current_heat_id)
                await ws.send_text(json.dumps({"cmd": "arm", "lanes": sorted(heat["lanes"])}))
            while True:
                await ws.send_text(json.dumps(await q.get(), default=str))

        send_task = asyncio.create_task(sender())
        try:
            while True:
                msg = json.loads(await ws.receive_text())
                kind = msg.get("type")
                if kind == "hello":
                    hub.node_info[node_id].update(msg.get("info", {}))
                    await hub.retime_if_needed()   # timer 'auto' -> results now come from this node
                    await hub._after_fleet_change()
                elif kind == "status":
                    hub.node_info[node_id].update(msg.get("status", {}))
                    await ctl.publish("timer", {"timer": ctl.timer.status()})
                elif kind == "raw":
                    await ctl.publish("raw", {"line": msg.get("line", "")})
                elif kind == "result" and isinstance(ctl.timer, RemoteTimer):
                    parsed = ParsedResult({int(k): v for k, v in msg["times"].items()},
                                          {int(k): v for k, v in (msg.get("places") or {}).items()},
                                          msg.get("raw", ""))
                    await ctl.on_timer_result(parsed)
                elif kind == "photo":
                    paths = {}
                    for img_kind, b64 in msg["images"].items():
                        name = f"heat{msg['heat_id']}-a{msg['attempt']}-{img_kind}.jpg"
                        (hub.photo_dir / name).write_bytes(base64.b64decode(b64))
                        paths[img_kind] = "/photos/" + name
                    await ctl.photo_ready(int(msg["heat_id"]), int(msg["attempt"]), paths, msg.get("margin_ms"))
                elif kind == "snapshot":
                    await hub.snapshot_ready(base64.b64decode(msg["image"]), int(msg.get("width", 0)),
                                             int(msg.get("height", 0)))
        except WebSocketDisconnect:
            pass
        finally:
            send_task.cancel()
            hub.nodes.pop(ws, None)
            hub.node_info.pop(node_id, None)
            hub._fleet_changed()
            await ctl.publish("timer", {"timer": ctl.timer.status()})

    return app
