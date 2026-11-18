"""Who am I and what is plugged into me?

Everything reads from /sys, /dev and /proc, with the roots injectable so the
detection can be tested against a fake filesystem. Works on Raspberry Pi OS
and on Debian/Ubuntu x86 boxes such as Protectli.
"""
from __future__ import annotations

import glob
import os
import re
import socket
import subprocess
from dataclasses import asdict, dataclass, field

# Video4Linux devices that are part of the Pi's media pipeline, not cameras.
_NOT_CAMERAS = re.compile(r"codec|isp|hevc|pispbe|scaler|decoder|encoder|bcm2835-v4l2-|rpivid|mem2mem", re.I)
# CSI camera receivers: a sensor is attached through these.
_CSI = re.compile(r"unicam|rp1-cfe", re.I)


@dataclass
class Machine:
    id: str                         # last 4 hex digits of the primary MAC, e.g. "A3F2"
    mac: str
    hostname: str
    ips: list[str]
    model: str
    hdmi: list[str] = field(default_factory=list)      # connected display outputs, e.g. ["HDMI-A-1"]
    camera: str | None = None                          # "pi" (CSI), "usb", or None
    serial: list[str] = field(default_factory=list)    # likely timer ports, e.g. ["/dev/ttyUSB0"]
    version: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def has_display(self) -> bool:
        return bool(self.hdmi)


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip().strip("\x00")
    except OSError:
        return ""


def primary_mac(sys_root: str = "/sys") -> str:
    """Wired NIC first (stable across Wi-Fi changes), then Wi-Fi, then anything."""
    ifaces = []
    for path in sorted(glob.glob(os.path.join(sys_root, "class/net/*"))):
        name = os.path.basename(path)
        mac = _read(os.path.join(path, "address")).lower()
        if name == "lo" or not mac or mac == "00:00:00:00:00:00":
            continue
        if os.path.exists(os.path.join(path, "bridge")) or name.startswith(("docker", "veth", "br-", "virbr")):
            continue
        rank = 0 if name.startswith(("eth", "en")) else 1 if name.startswith(("wl", "wlan")) else 2
        ifaces.append((rank, name, mac))
    return min(ifaces)[2] if ifaces else ""


def short_id(mac: str) -> str:
    hexes = re.sub(r"[^0-9a-fA-F]", "", mac)
    return hexes[-4:].upper() if len(hexes) >= 4 else "0000"


def ip_addresses() -> list[str]:
    """IPv4 addresses on this machine, excluding loopback."""
    out: list[str] = []
    try:
        text = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True,
                              timeout=3).stdout
        for m in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+)/", text):
            if not m.group(1).startswith("127."):
                out.append(m.group(1))
    except (OSError, subprocess.SubprocessError):
        pass
    if not out:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.254.254.254", 1))
            ip = s.getsockname()[0]
            if not ip.startswith("127."):
                out.append(ip)
        except OSError:
            pass
        finally:
            s.close()
    return out


def detect_hdmi(sys_root: str = "/sys") -> list[str]:
    """Display outputs with a monitor/projector physically connected."""
    found = []
    for path in sorted(glob.glob(os.path.join(sys_root, "class/drm/card*-*"))):
        name = os.path.basename(path).split("-", 1)[1]
        if not re.match(r"(HDMI|DP|DVI|VGA|eDP)", name):
            continue
        if _read(os.path.join(path, "status")) == "connected":
            found.append(name)
    return found


def detect_camera(sys_root: str = "/sys") -> str | None:
    """'pi' for a CSI camera module, 'usb' for a webcam, None if nothing usable."""
    kinds = set()
    for path in sorted(glob.glob(os.path.join(sys_root, "class/video4linux/video*"))):
        name = _read(os.path.join(path, "name"))
        if not name or _NOT_CAMERAS.search(name):
            continue
        kinds.add("pi" if _CSI.search(name) else "usb")
    if "pi" in kinds:
        return "pi"
    return "usb" if kinds else None


def detect_serial(dev_root: str = "/dev") -> list[str]:
    """USB-serial adapters and CDC devices: where a gantry timer shows up."""
    ports = sorted(glob.glob(os.path.join(dev_root, "ttyUSB*")) + glob.glob(os.path.join(dev_root, "ttyACM*")))
    return ports


def detect_model(proc_root: str = "/proc", sys_root: str = "/sys") -> str:
    model = _read(os.path.join(proc_root, "device-tree/model"))
    if model:
        return model
    vendor = _read(os.path.join(sys_root, "class/dmi/id/sys_vendor"))
    product = _read(os.path.join(sys_root, "class/dmi/id/product_name"))
    return " ".join(x for x in (vendor, product) if x) or "Unknown machine"


def identify(sys_root: str = "/sys", dev_root: str = "/dev", proc_root: str = "/proc") -> Machine:
    from .. import __version__
    mac = primary_mac(sys_root)
    return Machine(
        id=short_id(mac), mac=mac, hostname=socket.gethostname(), ips=ip_addresses(),
        model=detect_model(proc_root, sys_root), hdmi=detect_hdmi(sys_root),
        camera=detect_camera(sys_root), serial=detect_serial(dev_root), version=__version__,
    )
