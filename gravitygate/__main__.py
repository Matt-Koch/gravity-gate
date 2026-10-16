"""Command line: gravitygate serve | node | timer-capture | demo-roster"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="gravitygate")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the hub (top-of-track Pi or single Pi)")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--data", default="data", help="data directory (database, photos)")

    n = sub.add_parser("node", help="run a bottom-of-track node (timer + camera)")
    n.add_argument("--hub", required=True, help="e.g. ws://derby-top.local:8000")
    n.add_argument("--serial", default="/dev/ttyUSB0", help="serial port, or 'sim' for the simulator")
    n.add_argument("--profile", default="generic")
    n.add_argument("--lanes", type=int, default=3)
    n.add_argument("--camera", choices=["none", "pi", "synthetic"], default="none")
    n.add_argument("--token", default="")
    n.add_argument("--calibration", help="JSON file with finish_x and lane_bands")

    c = sub.add_parser("timer-capture", help="print raw timer output and how it parses")
    c.add_argument("--serial", default="/dev/ttyUSB0")
    c.add_argument("--profile", default="generic")
    c.add_argument("--lanes", type=int, default=3)

    d = sub.add_parser("demo-roster", help="load a fake pack into the database")
    d.add_argument("--data", default="data")
    d.add_argument("--count", type=int, default=36)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    if args.cmd == "serve":
        import uvicorn
        from .server import create_app
        uvicorn.run(create_app(args.data), host=args.host, port=args.port, log_level="info")
    elif args.cmd == "node":
        from .node import Node
        asyncio.run(Node(args.hub, args.lanes, args.serial, args.profile, args.camera, args.token,
                         args.calibration).run())
    elif args.cmd == "timer-capture":
        return asyncio.run(_capture(args))
    elif args.cmd == "demo-roster":
        _demo_roster(args.data, args.count)
    return 0


async def _capture(args) -> int:
    from .timers import PROFILES, SerialTimer

    async def show(kind, payload):
        if kind == "raw":
            print(f"RAW    {payload!r}")
        elif kind == "result":
            print(f"RESULT times={payload.times} places={payload.places}")
        else:
            print(f"{kind.upper():6} {payload}")

    timer = SerialTimer(args.lanes, show, args.serial, PROFILES[args.profile])
    await timer.start()
    print(f"Listening on {args.serial} ({PROFILES[args.profile].name}). Run a heat; Ctrl-C to stop.")
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        await timer.stop()
    return 0


def _demo_roster(data: str, count: int) -> None:
    import random
    from pathlib import Path
    from .db import Database

    firsts = ["Avery", "Blake", "Casey", "Drew", "Emery", "Finley", "Gray", "Harper", "Indy", "Jordan",
              "Kai", "Logan", "Morgan", "Noel", "Parker", "Quinn", "Reese", "Sawyer", "Taylor", "Wren"]
    lasts = ["Alvarez", "Becker", "Chen", "Dawson", "Ellis", "Foster", "Grant", "Hayes", "Iverson",
             "Jensen", "Keller", "Lopez", "Novak", "Ortiz", "Patel", "Quist"]
    dens = ["Lions", "Tigers", "Wolves", "Bears", "Webelos", "AOL"]
    db = Database(Path(data) / "derby.sqlite3")
    rng = random.Random(63)
    for i in range(count):
        db.add_racer(rng.choice(firsts), rng.choice(lasts), dens[i % len(dens)], checked_in=True)
    print(f"added {count} racers to {data}/derby.sqlite3")


if __name__ == "__main__":
    sys.exit(main())
