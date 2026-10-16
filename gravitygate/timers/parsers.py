"""Serial timer profiles and result parsers.

A profile describes how to talk to one family of finish-line gantry timers:
serial settings, the bytes to send to reset/arm it, and how to turn a line
of output into lane times. All profiles share a tolerant parser that accepts
the common output shapes, so an unknown timer often works with "generic".

VERIFY BEFORE RACE DAY: profiles marked verified=False are written from
published manuals and community notes, not tested against hardware here.
Use `gravitygate timer-capture` to log raw bytes and confirm.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Micro Wizard place characters: ! = 1st, " = 2nd, # = 3rd ...
PLACE_CHARS = '!"#$%&\'()'


@dataclass(frozen=True)
class TimerProfile:
    key: str
    name: str
    baud: int = 9600
    bytesize: int = 8
    parity: str = "N"
    stopbits: int = 1
    line_ending: bytes = b"\n"
    reset_cmd: bytes = b""          # sent to clear/arm before each heat
    force_cmd: bytes = b""          # sent to force results (e.g. a lane never finished)
    lane_mask_cmd: str = ""         # format string with {mask}, e.g. "M{mask}\r"
    init_cmds: tuple[bytes, ...] = ()
    lane_letters: bool = False      # lanes reported as A, B, C...
    verified: bool = False
    notes: str = ""
    dtr: bool = True
    rts: bool = True


PROFILES: dict[str, TimerProfile] = {p.key: p for p in [
    TimerProfile(
        key="generic", name="Generic (auto-detect output)",
        notes="Accepts A=2.5060! / 1=2.5060a / '1 2.5060 2 2.5430' / bare times.",
    ),
    TimerProfile(
        key="microwizard", name="Micro Wizard Fast Track (K/P/Q series)",
        reset_cmd=b"RG\r",         # return results when gate resets / race finishes
        force_cmd=b"RA\r",
        init_cmds=(b"N1\r",),      # newer firmware: report lane format (no-op on older)
        lane_letters=True, line_ending=b"\n",
        notes="Output 'A=2.5060! B=2.5430\" C=2.6730#'. Place chars ! \" # $ % &.",
    ),
    TimerProfile(
        key="champ", name="Champ Timer (Best Track / Race Timer Co.)",
        reset_cmd=b"r\r", force_cmd=b"ra\r", init_cmds=(b"od1\r", b"ol1\r"),
        notes="Output '1=2.5060a 2=2.5430b ...' place as letter. Commands vary by firmware.",
    ),
    TimerProfile(
        key="smartline", name="SmartLine / eTekGadget Timer II & DerbyStick",
        reset_cmd=b"", notes="Lane/time pairs; resets itself when the gate closes.",
    ),
    TimerProfile(
        key="newbold", name="NewBold DT8000 / Turbo",
        baud=1200, reset_cmd=b" ", notes="Positional times, slow baud rate.",
    ),
    TimerProfile(
        key="pdt", name="PDT / Arduino open-source timers",
        baud=115200, reset_cmd=b"R\n", force_cmd=b"F\n",
        notes="Open-source timer firmware (e.g. PDT by Dave Shaw) uses lane=time lines.",
    ),
]}


@dataclass
class ParsedResult:
    times: dict[int, float | None]             # lane -> seconds (None = DNF)
    places: dict[int, int] = field(default_factory=dict)
    raw: str = ""


_PAIR = re.compile(
    r"(?P<lane>\b[A-Ha-h]|\b[1-8])\s*[=:]\s*(?P<time>\d{1,2}\.\d{2,5})(?P<place>[!-)]|[a-h](?![=:]))?"
)
_TIME = re.compile(r"\d{1,2}\.\d{2,5}")
_POSITIONAL_PAIR = re.compile(r"\b(?P<lane>[1-8])\s+(?P<time>\d{1,2}\.\d{3,5})\b")


def _lane_num(token: str) -> int:
    return ord(token.upper()) - ord("A") + 1 if token.isalpha() else int(token)


def _place_num(token: str | None) -> int | None:
    if not token:
        return None
    if token in PLACE_CHARS:
        return PLACE_CHARS.index(token) + 1
    if token.isalpha():
        return ord(token.lower()) - ord("a") + 1
    return None


def parse_line(line: str, lane_count: int, profile: TimerProfile | None = None) -> ParsedResult | None:
    """Parse one line of timer output. Returns None if the line isn't a result."""
    text = line.strip()
    if not text:
        return None

    pairs = list(_PAIR.finditer(text))
    if pairs:
        times: dict[int, float | None] = {}
        places: dict[int, int] = {}
        for m in pairs:
            lane = _lane_num(m.group("lane"))
            if not 1 <= lane <= lane_count:
                continue
            t = float(m.group("time"))
            times[lane] = None if t <= 0 or t >= 9.9999 else t
            p = _place_num(m.group("place"))
            if p:
                places[lane] = p
        if times:
            return ParsedResult(times, places, text)

    pos_pairs = list(_POSITIONAL_PAIR.finditer(text))
    if len(pos_pairs) >= 2:
        times = {int(m.group("lane")): float(m.group("time")) for m in pos_pairs
                 if 1 <= int(m.group("lane")) <= lane_count}
        if times:
            return ParsedResult(times, {}, text)

    bare = _TIME.findall(text)
    # Bare positional times only count if there is one per lane (avoids
    # mistaking a firmware version string like "v1.25" for a result).
    if len(bare) == lane_count and not re.search(r"[a-zA-Z]{3,}", text):
        times = {}
        for i, t in enumerate(bare, start=1):
            v = float(t)
            times[i] = None if v <= 0 or v >= 9.9999 else v
        return ParsedResult(times, {}, text)
    return None


class LineAssembler:
    """Some timers emit one lane per line; collect until every lane reported."""

    def __init__(self, lane_count: int, profile: TimerProfile):
        self.lane_count = lane_count
        self.profile = profile
        self.partial: ParsedResult | None = None

    def feed(self, line: str, expected_lanes: set[int] | None = None) -> ParsedResult | None:
        res = parse_line(line, self.lane_count, self.profile)
        if res is None:
            return None
        want = expected_lanes or set(range(1, self.lane_count + 1))
        if self.partial is not None:
            self.partial.times.update(res.times)
            self.partial.places.update(res.places)
            self.partial.raw += "\n" + res.raw
            res = self.partial
        if want.issubset(res.times):
            self.partial = None
            return res
        self.partial = res
        return None

    def flush(self) -> ParsedResult | None:
        res, self.partial = self.partial, None
        return res
