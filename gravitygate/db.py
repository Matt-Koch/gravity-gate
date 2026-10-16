"""SQLite storage. One file, WAL mode, safe to copy to a USB stick for backup."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS racers (
    id INTEGER PRIMARY KEY,
    car_number INTEGER UNIQUE,
    first_name TEXT NOT NULL DEFAULT '',
    last_name TEXT NOT NULL DEFAULT '',
    den TEXT NOT NULL DEFAULT '',
    rank TEXT NOT NULL DEFAULT '',
    checked_in INTEGER NOT NULL DEFAULT 0,
    passed_inspection INTEGER NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('chart', 'elimination')),
    config TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS session_racers (
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    racer_id INTEGER NOT NULL REFERENCES racers(id) ON DELETE CASCADE,
    seed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, racer_id)
);
CREATE TABLE IF NOT EXISTS heats (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    round INTEGER NOT NULL DEFAULT 1,
    race_in_round INTEGER NOT NULL DEFAULT 1,
    bracket TEXT,
    group_key TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS heat_lanes (
    heat_id INTEGER NOT NULL REFERENCES heats(id) ON DELETE CASCADE,
    lane INTEGER NOT NULL,
    racer_id INTEGER NOT NULL REFERENCES racers(id) ON DELETE CASCADE,
    PRIMARY KEY (heat_id, lane)
);
CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY,
    heat_id INTEGER NOT NULL REFERENCES heats(id) ON DELETE CASCADE,
    attempt INTEGER NOT NULL,
    lane INTEGER NOT NULL,
    racer_id INTEGER NOT NULL,
    time REAL,
    place INTEGER,
    dnf INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'timer',
    valid INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY,
    heat_id INTEGER NOT NULL,
    attempt INTEGER NOT NULL,
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    margin_ms REAL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_heats_session ON heats(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_results_heat ON results(heat_id, valid);
"""

DEFAULT_SETTINGS: dict[str, Any] = {
    "event_name": "Pinewood Derby",
    "lane_count": 3,
    "lane_mask": [],            # physical lanes that are out of service
    "runs_per_lane": 2,         # qualifiers: 3 lanes x 2 runs = 6 races per car
    "standings_method": "average",  # "average" run time | "points" (heat points)
    "drop_runs": "none",        # "none" | "slowest" | "fastest_slowest"
    "photo_margin_ms": 10.0,
    "photo_enabled": False,
    "photo_post_roll_s": 0.3,
    "auto_advance": False,
    "sound": True,
    "dnf_time": 9.999,
    "track_length_ft": 42.0,
    "scale_factor": 25.0,
    "admin_pin": "",
    "display_mode": "race",
    "roster_csv_url": "",
    "auto_advance_s": 8,
    "current_session": None,
    "timer_kind": "simulator",      # simulator | serial | remote | none
    "timer_port": "/dev/ttyUSB0",
    "timer_profile": "generic",
    "camera_kind": "none",          # none | pi | synthetic
    "camera_calibration": None,
    "sync_url": "",                 # Apps Script web app URL
    "sync_secret": "",
    "sync_sheet_id": "",            # service-account alternative
    "sync_credentials_file": "",
    "node_token": "",
    "public_url": "",               # e.g. http://derby.local:8000 ; blank = auto-detect LAN address
    "wifi_ssid": "",                # shown as a "join Wi-Fi" QR code on the display/poster
    "wifi_password": "",
}


class Database:
    """Thin wrapper around sqlite3. Thread-safe via one lock; fine for a Pi."""

    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            if self.path != ":memory:":
                self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.executescript(SCHEMA)

    # -- low level -------------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, tuple(params)).fetchall()]

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def transaction(self):
        return _Tx(self)

    # -- settings --------------------------------------------------------
    def get_setting(self, key: str) -> Any:
        row = self.one("SELECT value FROM settings WHERE key = ?", (key,))
        if row is None:
            return DEFAULT_SETTINGS.get(key)
        return json.loads(row["value"])

    def set_setting(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    def all_settings(self) -> dict[str, Any]:
        out = dict(DEFAULT_SETTINGS)
        for row in self.query("SELECT key, value FROM settings"):
            out[row["key"]] = json.loads(row["value"])
        return out

    def active_lanes(self) -> list[int]:
        """Physical lane numbers (1-based) currently in service."""
        mask = set(self.get_setting("lane_mask") or [])
        return [lane for lane in range(1, int(self.get_setting("lane_count")) + 1) if lane not in mask]

    # -- racers ----------------------------------------------------------
    def racers(self, checked_in_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM racers"
        if checked_in_only:
            sql += " WHERE checked_in = 1"
        return self.query(sql + " ORDER BY den, car_number")

    def racer_map(self) -> dict[int, dict]:
        return {r["id"]: r for r in self.racers()}

    def next_car_number(self) -> int:
        row = self.one("SELECT COALESCE(MAX(car_number), 0) + 1 AS n FROM racers")
        return int(row["n"])

    def add_racer(self, first_name: str, last_name: str, den: str = "", rank: str = "",
                  car_number: int | None = None, checked_in: bool = False) -> int:
        if car_number is None:
            car_number = self.next_car_number()
        cur = self.execute(
            "INSERT INTO racers(car_number, first_name, last_name, den, rank, checked_in) VALUES(?,?,?,?,?,?)",
            (car_number, first_name.strip(), last_name.strip(), den.strip(), rank.strip(), int(checked_in)),
        )
        return int(cur.lastrowid)

    def update_racer(self, racer_id: int, **fields: Any) -> None:
        allowed = {"car_number", "first_name", "last_name", "den", "rank",
                   "checked_in", "passed_inspection", "notes"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE racers SET {cols} WHERE id = ?", (*fields.values(), racer_id))

    def delete_racer(self, racer_id: int) -> None:
        self.execute("DELETE FROM racers WHERE id = ?", (racer_id,))

    # -- sessions & heats ------------------------------------------------
    def create_session(self, name: str, kind: str, config: dict, racer_ids: list[int]) -> int:
        with self.transaction():
            cur = self.execute(
                "INSERT INTO sessions(name, kind, config, created_at) VALUES(?,?,?,?)",
                (name, kind, json.dumps(config), time.time()),
            )
            sid = int(cur.lastrowid)
            for seed, rid in enumerate(racer_ids):
                self.execute("INSERT INTO session_racers(session_id, racer_id, seed) VALUES(?,?,?)",
                             (sid, rid, seed))
        return sid

    def session(self, session_id: int) -> dict | None:
        row = self.one("SELECT * FROM sessions WHERE id = ?", (session_id,))
        if row:
            row["config"] = json.loads(row["config"])
        return row

    def sessions(self) -> list[dict]:
        rows = self.query("SELECT * FROM sessions ORDER BY id")
        for r in rows:
            r["config"] = json.loads(r["config"])
        return rows

    def session_racer_ids(self, session_id: int) -> list[int]:
        return [r["racer_id"] for r in self.query(
            "SELECT racer_id FROM session_racers WHERE session_id = ? ORDER BY seed", (session_id,))]

    def add_session_racer(self, session_id: int, racer_id: int) -> None:
        row = self.one("SELECT COALESCE(MAX(seed), -1) + 1 AS s FROM session_racers WHERE session_id = ?",
                       (session_id,))
        self.execute("INSERT OR IGNORE INTO session_racers(session_id, racer_id, seed) VALUES(?,?,?)",
                     (session_id, racer_id, row["s"]))

    def add_heats(self, session_id: int, heats: list[dict]) -> list[int]:
        """heats: [{round, race_in_round, bracket, group_key, lanes: {lane: racer_id}}]"""
        ids = []
        with self.transaction():
            row = self.one("SELECT COALESCE(MAX(seq), 0) AS s FROM heats WHERE session_id = ?", (session_id,))
            seq = row["s"]
            for h in heats:
                seq += 1
                cur = self.execute(
                    "INSERT INTO heats(session_id, seq, round, race_in_round, bracket, group_key) "
                    "VALUES(?,?,?,?,?,?)",
                    (session_id, seq, h.get("round", 1), h.get("race_in_round", seq),
                     h.get("bracket"), h.get("group_key")),
                )
                hid = int(cur.lastrowid)
                for lane, rid in h["lanes"].items():
                    self.execute("INSERT INTO heat_lanes(heat_id, lane, racer_id) VALUES(?,?,?)",
                                 (hid, int(lane), rid))
                ids.append(hid)
        return ids

    def heats(self, session_id: int, status: str | None = None) -> list[dict]:
        sql = "SELECT * FROM heats WHERE session_id = ?"
        params: list[Any] = [session_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        rows = self.query(sql + " ORDER BY seq", params)
        if not rows:
            return rows
        ids = [r["id"] for r in rows]
        marks = ",".join("?" * len(ids))
        lanes: dict[int, dict[int, int]] = {}
        for lr in self.query(f"SELECT * FROM heat_lanes WHERE heat_id IN ({marks})", ids):
            lanes.setdefault(lr["heat_id"], {})[lr["lane"]] = lr["racer_id"]
        for r in rows:
            r["lanes"] = lanes.get(r["id"], {})
        return rows

    def heat(self, heat_id: int) -> dict | None:
        row = self.one("SELECT * FROM heats WHERE id = ?", (heat_id,))
        if row:
            row["lanes"] = {lr["lane"]: lr["racer_id"] for lr in
                            self.query("SELECT * FROM heat_lanes WHERE heat_id = ?", (heat_id,))}
        return row

    def set_heat_status(self, heat_id: int, status: str) -> None:
        self.execute("UPDATE heats SET status = ? WHERE id = ?", (status, heat_id))

    def delete_pending_heats(self, session_id: int) -> None:
        self.execute("DELETE FROM heats WHERE session_id = ? AND status = 'pending'", (session_id,))

    # -- results ---------------------------------------------------------
    def next_attempt(self, heat_id: int) -> int:
        row = self.one("SELECT COALESCE(MAX(attempt), 0) + 1 AS a FROM results WHERE heat_id = ?", (heat_id,))
        return int(row["a"])

    def record_results(self, heat_id: int, rows: list[dict], source: str) -> int:
        """rows: [{lane, racer_id, time, place, dnf}]. Invalidates earlier attempts."""
        with self.transaction():
            attempt = self.next_attempt(heat_id)
            self.execute("UPDATE results SET valid = 0 WHERE heat_id = ?", (heat_id,))
            now = time.time()
            for r in rows:
                self.execute(
                    "INSERT INTO results(heat_id, attempt, lane, racer_id, time, place, dnf, source, valid, created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,1,?)",
                    (heat_id, attempt, r["lane"], r["racer_id"], r.get("time"), r.get("place"),
                     int(bool(r.get("dnf"))), source, now),
                )
        return attempt

    def invalidate_results(self, heat_id: int) -> None:
        self.execute("UPDATE results SET valid = 0 WHERE heat_id = ?", (heat_id,))

    def valid_results(self, session_id: int | None = None, heat_id: int | None = None) -> list[dict]:
        sql = ("SELECT r.*, h.session_id, h.seq, h.round, h.race_in_round, h.bracket, h.group_key FROM results r "
               "JOIN heats h ON h.id = r.heat_id WHERE r.valid = 1")
        params: list[Any] = []
        if session_id is not None:
            sql += " AND h.session_id = ?"
            params.append(session_id)
        if heat_id is not None:
            sql += " AND r.heat_id = ?"
            params.append(heat_id)
        return self.query(sql + " ORDER BY h.seq, r.place", params)

    # -- photos ----------------------------------------------------------
    def add_photo(self, heat_id: int, attempt: int, kind: str, path: str, margin_ms: float | None) -> int:
        cur = self.execute(
            "INSERT INTO photos(heat_id, attempt, kind, path, margin_ms, created_at) VALUES(?,?,?,?,?,?)",
            (heat_id, attempt, kind, path, margin_ms, time.time()),
        )
        return int(cur.lastrowid)

    def photos(self, heat_id: int | None = None, limit: int = 50) -> list[dict]:
        if heat_id is None:
            return self.query("SELECT * FROM photos ORDER BY id DESC LIMIT ?", (limit,))
        return self.query("SELECT * FROM photos WHERE heat_id = ? ORDER BY id DESC", (heat_id,))


class _Tx:
    def __init__(self, db: Database):
        self.db = db

    def __enter__(self):
        self.db._lock.acquire()
        self.db._conn.execute("SAVEPOINT tx")
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.db._conn.execute("RELEASE tx")
            else:
                self.db._conn.execute("ROLLBACK TO tx")
                self.db._conn.execute("RELEASE tx")
        finally:
            self.db._lock.release()
        return False
