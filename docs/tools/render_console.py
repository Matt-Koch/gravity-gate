"""Render the fleet console (what each box shows on HDMI) to PNGs for the docs.

    python docs/tools/render_console.py

Uses the real console renderer with example machines, drawn in Linux-console colors.
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from gravitygate.fleet.console import ConsoleModel, render

OUT = Path(__file__).resolve().parents[1] / "screenshots"
FONT_DIR = "/usr/share/fonts/truetype/dejavu/"
FONT = ImageFont.truetype(FONT_DIR + "DejaVuSansMono.ttf", 18)
BOLD = ImageFont.truetype(FONT_DIR + "DejaVuSansMono-Bold.ttf", 18)
COLS, CW, CH = 100, 11, 22
STYLES = {"normal": ((200, 200, 200), None, FONT), "dim": ((120, 120, 120), None, FONT),
          "title": ((0, 0, 0), (230, 190, 40), BOLD), "big": ((240, 200, 50), None, BOLD),
          "good": ((90, 210, 110), None, FONT), "warn": ((240, 200, 50), None, BOLD),
          "sel": ((0, 0, 0), (200, 200, 200), FONT), "flash": ((0, 0, 0), (240, 200, 50), BOLD)}


def shot(lines, name, rows):
    img = Image.new("RGB", (COLS * CW + 20, rows * CH + 20), (0, 0, 0))
    d = ImageDraw.Draw(img)
    for y, (text, style) in enumerate(lines[:rows]):
        fg, bg, font = STYLES.get(style, STYLES["normal"])
        if bg:
            d.rectangle([10, 10 + y * CH, 10 + COLS * CW, 10 + (y + 1) * CH], fill=bg)
        d.text((10, 10 + y * CH), text, font=font, fill=fg)
    img.save(OUT / f"{name}.png")


FINISH = {"id": "A3F2", "mac": "dc:a6:32:11:a3:f2", "hostname": "gg-a3f2", "ips": ["10.42.0.17"],
          "model": "Raspberry Pi 4 Model B Rev 1.5", "hdmi": ["HDMI-A-1"], "camera": "pi",
          "serial": ["/dev/ttyUSB0"], "version": "0.1.0"}
BRAIN = dict(FINISH, id="0B11", hostname="gg-0b11", ips=["10.42.0.1"], model="Raspberry Pi 5 Model B Rev 1.0",
             camera=None, serial=[])
DISPLAY = dict(FINISH, id="7C10", hostname="gg-7c10", ips=["10.42.0.18"], model="Protectli VP2420",
               camera=None, serial=[])


def view(machine, **kw):
    v = {"machine": machine, "roles": [], "role_label": "Standby", "brain_url": None, "brain_link": False,
         "conflict": [], "processes": {}, "fleet": [], "features": {}, "message": "", "identify": False}
    v.update(kw)
    return v


def main():
    home = ConsoleModel()
    shot(render(view(FINISH), home, COLS), "console-searching", 22)
    shot(render(view(FINISH, brain_url="http://10.42.0.1:8000", brain_link=True), home, COLS), "console-boot", 22)
    shot(render(view(BRAIN, roles=["brain"], role_label="Brain", brain_link=True,
                     processes={"brain": {"state": "running"}}, message="This machine is now: Brain"),
                home, COLS), "console-brain", 22)
    shot(render(view(DISPLAY, roles=["display"], role_label="Display", brain_url="http://10.42.0.1:8000",
                     brain_link=True, processes={"display": {"state": "running"}},
                     message="The brain assigned: Display"), home, COLS), "console-display", 22)
    shot(render(view(DISPLAY, brain_url="http://10.42.0.1:8000", brain_link=True, identify=True), home, COLS),
         "console-identify", 22)
    fleet = [
        {**BRAIN, "roles": ["brain", "display"], "role_label": "Brain + Display", "online": True, "is_self": True},
        {**DISPLAY, "roles": ["display"], "role_label": "Display", "online": True},
        {**FINISH, "roles": ["finish"], "role_label": "Finish line", "online": True},
        {**dict(DISPLAY, id="E402", ips=["10.42.0.19"]), "roles": [], "role_label": "Standby", "online": True},
    ]
    fm = ConsoleModel()
    fm.mode, fm.sel = "fleet", 2
    shot(render(view(BRAIN, roles=["brain", "display"], role_label="Brain + Display", brain_link=True, fleet=fleet,
                     features={"finish_online": True, "finish_camera": "pi", "displays": 2},
                     message="A3F2: Finish line"), fm, COLS), "console-fleet", 17)


if __name__ == "__main__":
    main()
