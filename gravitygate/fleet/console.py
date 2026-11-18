"""Full-screen role picker on the machine's own HDMI console (tty1).

Runs on the Linux text console, so it shows up on any HDMI monitor with no
desktop installed. Home screen: a big machine ID, IP, detected hardware and
the role keys. On the brain, Enter opens the Fleet screen to arrange every
other machine by ID.

Rendering (`render`) and key handling (`ConsoleModel.handle`) are pure so
they can be tested; `ConsoleUI` draws them with curses.
"""
from __future__ import annotations

import asyncio
import os
import threading

from . import roles as R

BLOCK = "#" if os.environ.get("GRAVITYGATE_ASCII") else "█"

# 5x5 glyphs for the big machine ID (hex digits only).
_FONT = {
    "0": ["01110", "10011", "10101", "11001", "01110"], "1": ["00100", "01100", "00100", "00100", "01110"],
    "2": ["11110", "00001", "01110", "10000", "11111"], "3": ["11110", "00001", "00110", "00001", "11110"],
    "4": ["10010", "10010", "11111", "00010", "00010"], "5": ["11111", "10000", "11110", "00001", "11110"],
    "6": ["01110", "10000", "11110", "10001", "01110"], "7": ["11111", "00010", "00100", "01000", "01000"],
    "8": ["01110", "10001", "01110", "10001", "01110"], "9": ["01110", "10001", "01111", "00001", "01110"],
    "A": ["01110", "10001", "11111", "10001", "10001"], "B": ["11110", "10001", "11110", "10001", "11110"],
    "C": ["01111", "10000", "10000", "10000", "01111"], "D": ["11110", "10001", "10001", "10001", "11110"],
    "E": ["11111", "10000", "11110", "10000", "11111"], "F": ["11111", "10000", "11110", "10000", "10000"],
}


def big_text(text: str, scale: int = 2) -> list[str]:
    rows = []
    for r in range(5):
        line = "   ".join("".join((BLOCK if b == "1" else " ") * scale for b in _FONT.get(ch, ["00000"] * 5)[r])
                          for ch in text.upper())
        rows.append(line)
    return rows


def hardware_summary(m: dict) -> str:
    parts = [f"Display {'YES ' + ','.join(m['hdmi']) if m.get('hdmi') else 'no'}",
             f"Camera {({'pi': 'YES Pi camera', 'usb': 'YES USB (not yet supported)'}).get(m.get('camera'), 'no')}",
             f"Timer port {'YES ' + m['serial'][0] if m.get('serial') else 'no'}"]
    return "   ".join(parts)


def short_hw(m: dict) -> str:
    tags = []
    if m.get("hdmi"):
        tags.append("display")
    if m.get("camera"):
        tags.append(f"{m['camera']}-cam")
    if m.get("serial"):
        tags.append("timer")
    return " ".join(tags) or "-"


class ConsoleModel:
    def __init__(self):
        self.mode = "home"      # "home" | "fleet"
        self.sel = 0
        self.pending_brain = False

    def handle(self, key: str, view: dict):
        """Returns an action: ("local", roles) | ("brain", msg) | None."""
        k = key.lower() if len(key) == 1 else key
        roles = list(view["roles"])
        if self.mode == "home":
            if k == "b":
                if R.BRAIN in roles:
                    return ("local", [r for r in roles if r != R.BRAIN])
                if view.get("brain_url") and not self.pending_brain:
                    self.pending_brain = True   # another brain exists: ask for a second press
                    return None
                self.pending_brain = False
                return ("local", roles + [R.BRAIN])
            self.pending_brain = False
            if k in ("f", "d"):
                role = R.FINISH if k == "f" else R.DISPLAY
                return ("local", [r for r in roles if r != role] if role in roles else roles + [role])
            if k == "s":
                return ("local", [r for r in roles if r == R.BRAIN])
            if k in ("enter", "\n", "tab") and R.BRAIN in roles:
                self.mode, self.sel = "fleet", 0
            return None
        # fleet mode (brain only)
        fleet = [m for m in view.get("fleet", []) if not m.get("is_self")]
        if k in ("escape", "tab", "q"):
            self.mode = "home"
            return None
        if k == "up":
            self.sel = max(0, self.sel - 1)
            return None
        if k == "down":
            self.sel = min(max(len(fleet) - 1, 0), self.sel + 1)
            return None
        if k == "a":
            return ("brain", {"type": "auto_arrange"})
        if not fleet:
            return None
        target = fleet[min(self.sel, len(fleet) - 1)]
        cur = [r for r in target.get("roles", []) if r in R.ASSIGNABLE]
        if k in ("f", "d"):
            role = R.FINISH if k == "f" else R.DISPLAY
            new = [r for r in cur if r != role] if role in cur else cur + [role]
            return ("brain", {"type": "assign", "id": target["id"], "roles": new})
        if k == "s":
            return ("brain", {"type": "assign", "id": target["id"], "roles": []})
        if k == "i":
            return ("brain", {"type": "identify", "id": target["id"]})
        return None


def render(view: dict, model: ConsoleModel, width: int = 100) -> list[tuple[str, str]]:
    """Lines of (text, style). Styles: title, big, normal, dim, good, warn, sel, flash."""
    m = view["machine"]
    out: list[tuple[str, str]] = []
    add = lambda text="", style="normal": out.append((text[:width - 1], style))
    add(f" GRAVITY GATE  -  machine setup".ljust(width - 12) + f"v{m.get('version', '')}", "title")
    if model.mode == "fleet":
        return out + _render_fleet(view, model, width)
    add()
    if view.get("identify"):
        add("  >>> THIS IS THE MACHINE YOU ARE LOOKING FOR <<<", "flash")
    for row in big_text(m["id"]):
        add("   " + row, "flash" if view.get("identify") else "big")
    add()
    add(f"  ID {m['id']}    IP {', '.join(m.get('ips') or ['(no network yet)'])}    host {m.get('hostname', '')}")
    add(f"  {m.get('model', '')}", "dim")
    add()
    add(f"  Hardware:  {hardware_summary(m)}")
    procs = view.get("processes", {})
    state = ", ".join(f"{R.ROLES[k].name} {v.get('state')}" for k, v in procs.items() if k in R.ROLES)
    add(f"  Role:      {view['role_label']}" + (f"   ({state})" if state else ""), "good" if view["roles"] else "normal")
    if R.BRAIN in view["roles"]:
        brain = "THIS MACHINE is the brain" + ("   race control: http://" + (m.get("ips") or ["<ip>"])[0] + ":8000/admin")
    elif view.get("brain_url"):
        brain = ("connected to " if view.get("brain_link") else "found, connecting to ") + view["brain_url"]
    else:
        brain = "searching the network for the brain..."
    add(f"  Brain:     {brain}", "good" if view.get("brain_link") or R.BRAIN in view["roles"] else "warn")
    if view.get("conflict"):
        add(f"  WARNING: another machine also claims to be the brain ({', '.join(view['conflict'])}). Keep only one.", "warn")
    add()
    add("  Choose this machine's role with its keyboard (keys toggle):")
    add("    [B] Brain     [F] Finish line     [D] Display     [S] Standby (clear)")
    if R.BRAIN in view["roles"]:
        add("    [Enter] Fleet: see every machine and assign roles by ID", "good")
    else:
        add(f'  ...or leave this screen alone and assign "{m["id"]}" from the brain.', "dim")
    if model.pending_brain:
        add("  A brain is already running on the network. Press B again to make THIS machine the brain anyway.", "warn")
    if view.get("message"):
        add()
        add("  " + view["message"], "good")
    return out


def _render_fleet(view: dict, model: ConsoleModel, width: int) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    add = lambda text="", style="normal": out.append((text[:width - 1], style))
    fleet = view.get("fleet", [])
    others = [x for x in fleet if not x.get("is_self")]
    online = sum(1 for x in fleet if x.get("online"))
    add()
    add(f"  FLEET  -  {online} machine(s) online", "big")
    add()
    add(f"    {'ID':<6}{'IP':<17}{'Hardware':<24}{'Roles':<24}Status", "dim")
    me = next((x for x in fleet if x.get("is_self")), None)
    if me:
        add(f"    {me['id']:<6}{(me.get('ips') or ['-'])[0]:<17}{short_hw(me):<24}{me.get('role_label', ''):<24}this machine", "dim")
    if not others:
        add("    No other machines yet. Power them on with the same image; they appear here within seconds.", "warn")
    for i, x in enumerate(others):
        status = "online" if x.get("online") else "OFFLINE"
        line = f"  {'>' if i == model.sel else ' '} {x['id']:<6}{(x.get('ips') or [x.get('source_ip', '-')])[0]:<17}" \
               f"{short_hw(x):<24}{x.get('role_label', ''):<24}{status}"
        add(line, "sel" if i == model.sel else ("normal" if x.get("online") else "warn"))
    add()
    f = view.get("features", {})
    photo = "ON" if f.get("finish_camera") == "pi" else "off (no finish-line camera online)"
    timer = "from the finish line" if f.get("finish_online") else "no finish-line machine online"
    add(f"  Photo finish: {photo}     Timer: {timer}     Displays: {f.get('displays', 0)}")
    add()
    add("  Up/Down select   [F] finish line   [D] display   [S] standby   [I] identify (flash its screen)", "dim")
    add("  [A] auto-arrange by hardware        [Esc] back", "dim")
    if view.get("message"):
        add("  " + view["message"], "good")
    return out


class ConsoleUI:
    """curses front end. Runs in a thread; actions are sent to the agent's event loop."""

    def __init__(self):
        self.model = ConsoleModel()
        self.stop_flag = threading.Event()
        self.thread: threading.Thread | None = None

    def stop(self) -> None:
        """Leave curses cleanly (restores the text console) before the process exits."""
        self.stop_flag.set()
        if self.thread:
            self.thread.join(2)

    async def run(self, agent) -> None:
        loop = asyncio.get_running_loop()
        done = asyncio.Event()

        def act(action):
            if action is None:
                return
            kind, payload = action
            coro = agent.set_local_roles(payload) if kind == "local" else agent.brain_command(payload)
            asyncio.run_coroutine_threadsafe(coro, loop)

        def worker():
            import curses
            try:
                curses.wrapper(lambda scr: self._main(scr, agent, act))
            finally:
                loop.call_soon_threadsafe(done.set)

        self.thread = threading.Thread(target=worker, name="console", daemon=True)
        self.thread.start()
        await done.wait()

    def _main(self, scr, agent, act) -> None:
        import curses
        curses.curs_set(0)
        scr.timeout(250)
        styles = {"normal": curses.A_NORMAL, "dim": curses.A_DIM, "title": curses.A_REVERSE | curses.A_BOLD,
                  "big": curses.A_BOLD, "sel": curses.A_REVERSE, "flash": curses.A_BOLD | curses.A_BLINK}
        if curses.has_colors():
            curses.start_color()
            curses.init_pair(1, curses.COLOR_GREEN, curses.COLOR_BLACK)
            curses.init_pair(2, curses.COLOR_YELLOW, curses.COLOR_BLACK)
            curses.init_pair(3, curses.COLOR_BLACK, curses.COLOR_YELLOW)
            styles.update(good=curses.color_pair(1), warn=curses.color_pair(2) | curses.A_BOLD,
                          title=curses.color_pair(3) | curses.A_BOLD, big=curses.color_pair(2) | curses.A_BOLD)
        keys = {curses.KEY_UP: "up", curses.KEY_DOWN: "down", 10: "enter", 13: "enter", curses.KEY_ENTER: "enter",
                27: "escape", 9: "tab"}
        while not self.stop_flag.is_set():
            h, w = scr.getmaxyx()
            scr.erase()
            for y, (text, style) in enumerate(render(agent.view(), self.model, w)[:h - 1]):
                try:
                    scr.addstr(y, 0, text, styles.get(style, curses.A_NORMAL))
                except curses.error:
                    pass
            scr.refresh()
            ch = scr.getch()
            if ch == -1:
                continue
            key = keys.get(ch) or (chr(ch) if 32 <= ch < 127 else "")
            if key:
                act(self.model.handle(key, agent.view()))
