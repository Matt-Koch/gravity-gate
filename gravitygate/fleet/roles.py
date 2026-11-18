"""Roles a fleet machine can take, and what each one runs.

A machine can hold several roles at once (the brain usually also drives the
projector). Pure functions only, so the arrangement logic is easy to test.
"""
from __future__ import annotations

from dataclasses import dataclass

BRAIN, FINISH, DISPLAY = "brain", "finish", "display"


@dataclass(frozen=True)
class Role:
    key: str
    name: str
    key_hint: str        # keyboard shortcut on the console role picker
    blurb: str


ROLES: dict[str, Role] = {r.key: r for r in [
    Role(BRAIN, "Brain", "B", "Runs race control, the database and the phone site. Exactly one per event."),
    Role(FINISH, "Finish line", "F", "Reads the gantry timer over serial; with a camera, captures every finish."),
    Role(DISPLAY, "Display", "D", "Shows the race screen full-screen on its HDMI output (projector or TV)."),
]}

# Roles the brain may hand out remotely. Choosing the brain is always done on
# that machine's own keyboard, so two boxes can never fight over it.
ASSIGNABLE = (FINISH, DISPLAY)


def normalize(roles) -> list[str]:
    out = []
    for r in roles or []:
        if r in ROLES and r not in out:
            out.append(r)
    return sorted(out, key=list(ROLES).index)


def label(roles) -> str:
    roles = normalize(roles)
    return " + ".join(ROLES[r].name for r in roles) if roles else "Standby"


def suggest(machines: list[dict]) -> dict[str, list[str]]:
    """Auto-arrange: propose roles for every non-brain machine from its hardware.

    * The box with a timer serial port (preferring one that also has a camera)
      becomes the finish line. Only one finish line.
    * Every other box with a connected HDMI display becomes a display.
    * Boxes with nothing useful attached stay on standby.
    Existing brain roles are kept as they are.
    """
    out: dict[str, list[str]] = {}
    candidates = [m for m in machines if BRAIN not in (m.get("roles") or [])]

    def finish_score(m: dict):
        return (bool(m.get("serial")), m.get("camera") == "pi", bool(m.get("camera")), not m.get("hdmi"))

    finish = max(candidates, key=finish_score, default=None)
    if finish is not None and not (finish.get("serial") or finish.get("camera")):
        finish = None
    for m in candidates:
        roles = []
        if finish is not None and m["id"] == finish["id"]:
            roles.append(FINISH)
        elif m.get("hdmi"):
            roles.append(DISPLAY)
        out[m["id"]] = roles
    return out


def commands(roles, brain_url: str, machine: dict, params: dict | None = None,
             brain_port: int = 8000, data_dir: str = "/var/lib/gravitygate/data",
             fleet_key: str = "", kiosk_cmd: str = "gravitygate-kiosk") -> dict[str, list[str]]:
    """argv to run for each role on this machine."""
    params = params or {}
    out: dict[str, list[str]] = {}
    roles = normalize(roles)
    if BRAIN in roles:
        out[BRAIN] = ["gravitygate", "serve", "--port", str(brain_port), "--data", data_dir]
    hub = (brain_url or f"http://127.0.0.1:{brain_port}").rstrip("/")
    if FINISH in roles:
        p = params.get(FINISH, {})
        serial = p.get("serial") or (machine.get("serial") or ["sim"])[0]
        camera = p.get("camera") or ("pi" if machine.get("camera") == "pi" else "none")
        argv = ["gravitygate", "node", "--hub", "ws" + hub[4:] if hub.startswith("http") else hub,
                "--serial", serial, "--profile", p.get("profile", "generic"),
                "--lanes", str(p.get("lanes", 3)), "--camera", camera]
        token = p.get("token") or fleet_key
        if token:
            argv += ["--token", token]
        out[FINISH] = argv
    if DISPLAY in roles:
        page = params.get(DISPLAY, {}).get("page", "/display")
        sep = "&" if "?" in page else "?"
        out[DISPLAY] = [kiosk_cmd, f"{hub}{page}{sep}kiosk=1&screen={machine.get('id', '')}"]
    return out
