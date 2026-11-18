"""The fleet agent: the one service every imaged machine runs at boot.

    gravitygate agent --tty /dev/tty1

It identifies the box, announces it on the network, finds the brain, shows a
role picker on the HDMI console, and starts/stops whatever the box's roles
need (race server, timer + camera node, full-screen display).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from . import protocol, roles as R
from .identity import Machine, identify

log = logging.getLogger("gravitygate.agent")


@dataclass
class AgentConfig:
    state_dir: str = "/var/lib/gravitygate"
    fleet_key: str = ""
    brain_port: int = 8000
    kiosk_cmd: str = "gravitygate-kiosk"
    beacon_s: float = 2.0
    rescan_s: float = 10.0
    run_as: str = ""            # run brain/finish processes as this user (agent itself runs as root)
    set_hostname: bool = False  # rename the box gg-<id> so cloned images get unique .local names

    @classmethod
    def load(cls, path: str = "/etc/gravitygate/agent.json", **overrides) -> "AgentConfig":
        data = {}
        try:
            data = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            pass
        data.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class Supervisor:
    """Keeps one process per role running; restarts it with backoff if it dies."""

    def __init__(self, log_dir: Path, env: dict | None = None):
        self.log_dir = log_dir
        self.env = env or {}
        self.want: dict[str, list[str]] = {}
        self.procs: dict[str, asyncio.subprocess.Process] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.status: dict[str, dict] = {}

    async def apply(self, cmds: dict[str, list[str]]) -> None:
        for role in list(self.want):
            if role not in cmds or cmds[role] != self.want[role]:
                await self._stop(role)
        for role, argv in cmds.items():
            if role not in self.want:
                self.want[role] = argv
                self.tasks[role] = asyncio.create_task(self._keep_running(role, argv))

    async def _keep_running(self, role: str, argv: list[str]) -> None:
        backoff = 1.0
        self.log_dir.mkdir(parents=True, exist_ok=True)
        while self.want.get(role) == argv:
            started = time.monotonic()
            self.status[role] = {"state": "starting", "argv": argv}
            try:
                logf = open(self.log_dir / f"{role}.log", "ab")
                proc = await asyncio.create_subprocess_exec(
                    *argv, stdout=logf, stderr=asyncio.subprocess.STDOUT, stdin=asyncio.subprocess.DEVNULL,
                    env={**os.environ, **self.env}, start_new_session=True)
            except OSError as exc:
                self.status[role] = {"state": "failed", "error": str(exc), "argv": argv}
                log.warning("role %s failed to start: %s", role, exc)
            else:
                self.procs[role] = proc
                self.status[role] = {"state": "running", "pid": proc.pid, "argv": argv}
                code = await proc.wait()
                logf.close()
                self.procs.pop(role, None)
                if self.want.get(role) != argv:
                    return
                self.status[role] = {"state": "restarting", "exit": code, "argv": argv}
                log.warning("role %s exited with %s; restarting", role, code)
                if time.monotonic() - started > 60:
                    backoff = 1.0
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def _stop(self, role: str) -> None:
        self.want.pop(role, None)
        proc = self.procs.pop(role, None)
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                proc.kill()
        task = self.tasks.pop(role, None)
        if task:
            task.cancel()
        self.status.pop(role, None)

    async def stop_all(self) -> None:
        for role in list(self.want):
            await self._stop(role)


class Agent:
    def __init__(self, config: AgentConfig, machine: Machine | None = None):
        self.config = config
        self.machine = machine or identify()
        self.state_path = Path(config.state_dir) / "agent.json"
        saved = self._load()
        self.roles: list[str] = R.normalize(saved.get("roles"))
        self.params: dict = saved.get("params", {})
        self.local_change: bool = saved.get("local_change", False)
        self.brain_url: str | None = None
        self.brain_seen: float = 0.0
        self.other_brains: dict[str, float] = {}
        self.brain_link_up = False
        self.fleet: list[dict] = []
        self.features: dict = {}
        self.message = ""
        self.identify_until = 0.0
        self._ws = None
        self._loop: asyncio.AbstractEventLoop | None = None
        env = {"GRAVITYGATE_FLEET_KEY": config.fleet_key, "GRAVITYGATE_MACHINE_ID": self.machine.id}
        self.supervisor = Supervisor(Path(config.state_dir) / "logs", env)
        self.command_builder = R.commands   # swappable in tests

    # -- persistence -----------------------------------------------------------
    def _load(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"roles": self.roles, "params": self.params,
                                       "local_change": self.local_change}))
            tmp.replace(self.state_path)
        except OSError as exc:
            log.warning("could not save agent state: %s", exc)

    # -- state ---------------------------------------------------------------------
    @property
    def is_brain(self) -> bool:
        return R.BRAIN in self.roles

    def effective_brain_url(self) -> str | None:
        if self.is_brain:
            return f"http://127.0.0.1:{self.config.brain_port}"
        if self.brain_url and time.time() - self.brain_seen < 15:
            return self.brain_url
        return None

    def brain_conflict(self) -> list[str]:
        """Other machines also claiming to be the brain (should be none)."""
        now = time.time()
        return [ip for ip, t in self.other_brains.items() if now - t < 10]

    async def apply_roles(self) -> None:
        cmds = self.command_builder(self.roles, self.effective_brain_url() or "", self.machine.to_dict(), self.params,
                          brain_port=self.config.brain_port,
                          data_dir=str(Path(self.config.state_dir) / "data"),
                          fleet_key=self.config.fleet_key, kiosk_cmd=self.config.kiosk_cmd)
        # Remote roles wait until there is a brain to point at.
        if not self.effective_brain_url():
            cmds = {k: v for k, v in cmds.items() if k == R.BRAIN}
        if self.config.run_as and os.geteuid() == 0:
            # The kiosk launcher drops privileges itself; everything else via runuser.
            cmds = {k: (v if k == R.DISPLAY else ["runuser", "-u", self.config.run_as, "--", *v])
                    for k, v in cmds.items()}
        await self.supervisor.apply(cmds)

    def ensure_hostname(self) -> None:
        want = f"gg-{self.machine.id.lower()}"
        if not self.config.set_hostname or self.machine.hostname == want or os.geteuid() != 0:
            return
        import subprocess
        try:
            subprocess.run(["hostnamectl", "set-hostname", want], check=True, timeout=10)
            self.machine.hostname = want
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("could not set hostname: %s", exc)

    async def set_local_roles(self, roles: list[str]) -> None:
        """A choice made on this machine's own keyboard."""
        self.roles = R.normalize(roles)
        self.local_change = True
        self._save()
        self.message = f"This machine is now: {R.label(self.roles)}"
        await self.apply_roles()
        await self._send({"type": "set_roles", "roles": self.roles})
        if self.brain_link_up:
            self.local_change = False
            self._save()

    async def brain_command(self, msg: dict) -> None:
        """Fleet commands typed on the brain's own keyboard (assign, auto, identify)."""
        if not self.is_brain:
            self.message = "Only the brain can arrange other machines."
            return
        if not await self._send(msg):
            self.message = "Brain server is still starting; try again in a moment."

    # -- network -------------------------------------------------------------------
    def on_beacon(self, msg: dict, src_ip: str) -> None:
        if msg.get("t") != "brain":
            return
        if msg.get("id") == self.machine.id:
            return  # our own brain server
        if self.is_brain:
            self.other_brains[src_ip] = time.time()
            return
        url = f"http://{src_ip}:{int(msg.get('port', 8000))}"
        changed = url != self.brain_url
        self.brain_url = url
        self.brain_seen = time.time()
        if changed and self._loop:
            self._loop.create_task(self.apply_roles())

    async def _send(self, msg: dict) -> bool:
        ws = self._ws
        if ws is None:
            return False
        try:
            await ws.send(json.dumps(msg))
            return True
        except Exception:
            return False

    async def _beacon_loop(self) -> None:
        while True:
            protocol.send({"t": "agent", "machine": self.machine.to_dict(), "roles": self.roles},
                          self.config.fleet_key)
            await asyncio.sleep(self.config.beacon_s)

    async def _rescan_loop(self) -> None:
        """Hotplug: a camera, monitor or timer cable can be connected after boot."""
        while True:
            await asyncio.sleep(self.config.rescan_s)
            fresh = await asyncio.to_thread(identify)
            if fresh.to_dict() != self.machine.to_dict():
                self.machine = fresh
                await self._send({"type": "hello", "machine": fresh.to_dict(), "roles": self.roles,
                                  "local_change": False})
                await self.apply_roles()

    async def _brain_link_loop(self) -> None:
        import websockets

        while True:
            url = self.effective_brain_url()
            if not url:
                await asyncio.sleep(1)
                continue
            ws_url = "ws" + url[4:] + "/ws/agent" + (f"?key={self.config.fleet_key}" if self.config.fleet_key else "")
            try:
                # proxy=None: machines talk directly on the event LAN, never via an HTTP proxy.
                async with websockets.connect(ws_url, ping_interval=10, open_timeout=5, proxy=None) as ws:
                    self._ws = ws
                    await ws.send(json.dumps({"type": "hello", "machine": self.machine.to_dict(),
                                              "roles": self.roles, "local_change": self.local_change}))
                    self.brain_link_up = True
                    async for raw in ws:
                        await self._handle(json.loads(raw))
            except Exception as exc:
                log.debug("brain link: %s", exc)
            self._ws = None
            self.brain_link_up = False
            await asyncio.sleep(2)

    async def _handle(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "assign":
            new = R.normalize(msg.get("roles"))
            self.params = msg.get("params") or self.params
            changed = new != self.roles
            self.roles = new
            self.local_change = False
            self._save()
            if changed:
                self.message = f"The brain assigned: {R.label(new)}"
            await self.apply_roles()
        elif kind == "fleet":
            self.fleet = msg.get("machines", [])
            self.features = msg.get("features", {})
        elif kind == "identify":
            self.identify_until = time.time() + 15
        elif kind in ("error", "message"):
            self.message = msg.get("message", "")

    async def run(self, tui=None) -> None:
        self._loop = asyncio.get_running_loop()
        await asyncio.to_thread(self.ensure_hostname)
        transport = await protocol.listen(self.on_beacon, self.config.fleet_key)
        await self.apply_roles()
        tasks = [asyncio.create_task(c) for c in
                 (self._beacon_loop(), self._brain_link_loop(), self._rescan_loop())]
        if tui is not None:
            tasks.append(asyncio.create_task(tui.run(self)))
        main = asyncio.current_task()
        import signal
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                self._loop.add_signal_handler(sig, main.cancel)   # stop role processes on shutdown
            except (NotImplementedError, RuntimeError):
                pass
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()
        finally:
            transport.close()
            await self.supervisor.stop_all()
            if tui is not None and hasattr(tui, "stop"):
                tui.stop()

    # -- console view --------------------------------------------------------------
    def view(self) -> dict:
        return {
            "machine": self.machine.to_dict(),
            "roles": self.roles,
            "role_label": R.label(self.roles),
            "brain_url": self.effective_brain_url(),
            "brain_link": self.brain_link_up,
            "conflict": self.brain_conflict(),
            "processes": {k: dict(v) for k, v in self.supervisor.status.items()},
            "fleet": self.fleet,
            "features": self.features,
            "message": self.message,
            "identify": time.time() < self.identify_until,
        }
