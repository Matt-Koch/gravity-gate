"""The brain's view of the fleet: who's out there, what they have, what they're doing.

The brain is the authority for role assignments, stored in the database so they
survive restarts. One exception: a role chosen on a machine's own keyboard while
it couldn't reach the brain is reported as a local change and accepted. Wall
clocks are not compared, because Pis without an RTC can boot offline with the
wrong date.
"""
from __future__ import annotations

import time
from typing import Awaitable, Callable

from ..db import Database
from . import roles as R

ONLINE_S = 10.0  # a machine is "online" if we heard from it this recently

Sender = Callable[[dict], Awaitable[None]]


class FleetRegistry:
    def __init__(self, db: Database, self_id: str = ""):
        self.db = db
        self.self_id = self_id
        self.machines: dict[str, dict] = {}
        self.links: dict[str, Sender] = {}
        self.listeners: list[Callable[[], None]] = []

    # -- persistence -------------------------------------------------------
    def assignments(self) -> dict[str, list[str]]:
        return dict(self.db.get_setting("fleet_assignments") or {})

    def _store(self, mid: str, roles: list[str]) -> None:
        a = self.assignments()
        a[mid] = R.normalize(roles)
        self.db.set_setting("fleet_assignments", a)

    def params(self) -> dict:
        """Per-role settings handed to agents along with their roles."""
        s = self.db.all_settings()
        return {R.FINISH: {"profile": s.get("timer_profile", "generic"), "lanes": int(s.get("lane_count", 3)),
                           "token": s.get("node_token") or ""},
                R.DISPLAY: {"page": "/display"}}

    # -- updates from the network --------------------------------------------
    def _record(self, machine: dict, src_ip: str | None) -> dict:
        mid = machine.get("id") or "????"
        rec = self.machines.setdefault(mid, {"id": mid, "roles": [], "connected": False})
        rec.update({k: v for k, v in machine.items() if k != "roles"})
        if src_ip:
            rec["source_ip"] = src_ip
        rec["last_seen"] = time.time()
        return rec

    def on_beacon(self, msg: dict, src_ip: str) -> None:
        if msg.get("t") != "agent" or not isinstance(msg.get("machine"), dict):
            return
        known = msg["machine"].get("id") in self.machines
        rec = self._record(msg["machine"], src_ip)
        changed = not known or rec.get("_beacon_roles") != msg.get("roles")
        rec["_beacon_roles"] = msg.get("roles")
        if not rec.get("connected"):
            rec["roles"] = R.normalize(msg.get("roles"))   # not linked yet: show what it says it runs
        if changed:
            self._changed()

    async def agent_hello(self, machine: dict, roles: list[str], local_change: bool,
                          send: Sender, src_ip: str | None = None) -> list[str]:
        """An agent connected. Returns the roles it should run."""
        rec = self._record(machine, src_ip)
        mid = rec["id"]
        self.links[mid] = send
        rec["connected"] = True
        stored = self.assignments().get(mid)
        reported = R.normalize(roles)
        if local_change or stored is None:
            final = reported                         # keyboard choice made offline, or a new machine
        else:
            final = R.normalize(stored)
            # The brain role is only ever chosen locally; never strip or grant it remotely.
            if R.BRAIN in reported and R.BRAIN not in final:
                final = R.normalize(final + [R.BRAIN])
            elif R.BRAIN not in reported and R.BRAIN in final:
                final = [r for r in final if r != R.BRAIN]
        rec["roles"] = final
        self._store(mid, final)
        await self._push(mid)
        self._changed()
        return final

    async def agent_local_change(self, mid: str, roles: list[str]) -> None:
        rec = self.machines.get(mid)
        if rec is None:
            return
        rec["roles"] = R.normalize(roles)
        self._store(mid, rec["roles"])
        self._changed()

    def agent_gone(self, mid: str) -> None:
        self.links.pop(mid, None)
        if mid in self.machines:
            self.machines[mid]["connected"] = False
        self._changed()

    # -- commands from race control ------------------------------------------
    async def assign(self, mid: str, roles: list[str]) -> list[str]:
        rec = self.machines.get(mid)
        if rec is None:
            raise KeyError(mid)
        wanted = [r for r in R.normalize(roles) if r in R.ASSIGNABLE]
        if R.BRAIN in rec.get("roles", []):
            wanted = R.normalize(wanted + [R.BRAIN])
        if R.FINISH in wanted:
            # One finish line per track: take it away from anyone else.
            for other in self.machines.values():
                if other["id"] != mid and R.FINISH in other.get("roles", []):
                    other["roles"] = [r for r in other["roles"] if r != R.FINISH]
                    self._store(other["id"], other["roles"])
                    await self._push(other["id"])
        rec["roles"] = wanted
        self._store(mid, wanted)
        await self._push(mid)
        self._changed()
        return wanted

    async def auto_arrange(self) -> dict[str, list[str]]:
        plan = R.suggest(self.online())
        for mid, roles in plan.items():
            await self.assign(mid, roles)
        return plan

    async def identify(self, mid: str) -> None:
        """Flash the ID on that machine's screen so you can find it in the room."""
        send = self.links.get(mid)
        if send:
            await send({"type": "identify"})

    async def _push(self, mid: str) -> None:
        send = self.links.get(mid)
        rec = self.machines.get(mid)
        if send and rec is not None:
            try:
                await send({"type": "assign", "roles": rec["roles"], "params": self.params()})
            except Exception:
                self.agent_gone(mid)

    async def broadcast_fleet(self) -> None:
        """Brain-role agents show the fleet on their console, so keep them current."""
        snap = self.snapshot()
        for mid, send in list(self.links.items()):
            if R.BRAIN in self.machines.get(mid, {}).get("roles", []):
                try:
                    await send({"type": "fleet", "machines": snap, "features": self.features()})
                except Exception:
                    self.agent_gone(mid)

    # -- views ---------------------------------------------------------------
    def is_online(self, rec: dict) -> bool:
        return bool(rec.get("connected")) or time.time() - rec.get("last_seen", 0) < ONLINE_S

    def online(self) -> list[dict]:
        return [m for m in self.snapshot() if m["online"]]

    def snapshot(self) -> list[dict]:
        out = []
        for mid in sorted(self.machines):
            rec = self.machines[mid]
            out.append({k: v for k, v in rec.items() if not k.startswith("_")}
                       | {"online": self.is_online(rec), "role_label": R.label(rec.get("roles")),
                          "is_self": mid == self.self_id})
        return out

    def features(self) -> dict:
        finish = [m for m in self.online() if R.FINISH in m.get("roles", [])]
        return {
            "finish_online": bool(finish),
            "finish_camera": next((m.get("camera") for m in finish if m.get("camera")), None),
            "displays": sum(1 for m in self.online() if R.DISPLAY in m.get("roles", [])),
            "machines": len(self.online()),
        }

    def _changed(self) -> None:
        for fn in self.listeners:
            fn()
