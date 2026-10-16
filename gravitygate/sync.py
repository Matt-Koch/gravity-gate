"""Google Sheets two-way sync: pull the roster, push results and standings.

Two backends:

* **Apps Script web app** (recommended, no Google Cloud project): paste
  deploy/google-apps-script.gs into the roster sheet's Extensions -> Apps
  Script, deploy as a web app, and put its URL + shared secret in Settings.
  GET returns the roster tab as rows; POST writes Results / Standings tabs.
* **Service account** via gspread (roster.push_to_google) for IT-managed packs.

Sync is offline-first: race day never waits on the internet. A background
loop pushes whenever results changed, and keeps retrying with backoff while
the Pi has no uplink.
"""
from __future__ import annotations

import asyncio
import csv
import json
import logging
import time
import urllib.parse
import urllib.request
from typing import Any

from . import roster
from .db import Database

log = logging.getLogger("gravitygate.sync")


def results_rows(db: Database) -> list[list[Any]]:
    """One row per car per run, all sessions, valid attempts only."""
    racers = db.racer_map()
    sessions = {s["id"]: s for s in db.sessions()}
    rows = [["Session", "Heat", "Round", "Race", "Bracket", "Lane", "Car", "Name", "Den",
             "Time", "Place", "DNF", "Source", "Attempt"]]
    for r in db.valid_results():
        rc = racers.get(r["racer_id"], {})
        rows.append([sessions.get(r["session_id"], {}).get("name", ""), r["seq"], r["round"],
                     r.get("race_in_round", ""), r["bracket"] or "", r["lane"], rc.get("car_number"),
                     f"{rc.get('first_name', '')} {rc.get('last_name', '')}".strip(), rc.get("den", ""),
                     r["time"], r["place"], "Y" if r["dnf"] else "", r["source"], r["attempt"]])
    return rows


def standings_rows(table: dict) -> list[list[Any]]:
    rows = [["Pack Rank", "Den", "Den Rank", "Car", "Name", "Average", "Best", "Points", "Runs", "Losses"]]
    den_rank = {r["racer_id"]: r["rank"] for d in table.get("dens", {}).values() for r in d}
    for r in table.get("overall", []):
        avg = r.get("average")
        rows.append([r["rank"], r.get("den", ""), den_rank.get(r["racer_id"]), r.get("car_number"),
                     r.get("name", ""), round(avg, 4) if avg else "", r.get("best") or "",
                     r.get("points", ""), r.get("runs", ""), r.get("losses", "")])
    return rows


def build_payload(db: Database, controller) -> dict:
    tabs: dict[str, list[list[Any]]] = {"Gravity Gate Results": results_rows(db)}
    for s in db.sessions():
        name = f"Standings - {s['name']}"[:90]
        tabs[name] = standings_rows(controller.standings(s["id"]))
    tabs["Gravity Gate Roster"] = list(csv.reader(roster.export_csv(db).splitlines()))
    return {"secret": db.get_setting("sync_secret") or "", "event": db.get_setting("event_name"),
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "tabs": tabs}


def _post_json(url: str, payload: dict, timeout: float = 20) -> dict:
    data = json.dumps(payload, default=str).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "Gravity Gate"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # Apps Script redirects; urllib follows
        body = resp.read().decode("utf-8", "replace")
    try:
        return json.loads(body)
    except ValueError:
        return {"ok": False, "error": body[:200]}


def _get_json(url: str, timeout: float = 20) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "Gravity Gate"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def pull_roster(db: Database) -> dict:
    """Pull from the Apps Script web app if configured, else the CSV link."""
    url = db.get_setting("sync_url")
    if url:
        sep = "&" if "?" in url else "?"
        data = _get_json(f"{url}{sep}secret={urllib.parse.quote(db.get_setting('sync_secret') or '')}")
        if not data.get("ok"):
            raise RuntimeError(data.get("error", "sync endpoint refused"))
        text = "\n".join(",".join(_csv_cell(c) for c in row) for row in data["rows"])
        return roster.import_rows(db, roster.parse_csv(text))
    csv_url = db.get_setting("roster_csv_url")
    if not csv_url:
        raise RuntimeError("no roster link configured")
    return roster.import_from_url(db, csv_url)


def _csv_cell(v: Any) -> str:
    s = "" if v is None else str(v)
    return '"' + s.replace('"', '""') + '"' if any(c in s for c in ',"\n') else s


class SyncService:
    """Background results push. Call mark_dirty() whenever results change."""

    def __init__(self, db: Database, controller):
        self.db = db
        self.controller = controller
        self.dirty = asyncio.Event()
        self.last_ok: float | None = None
        self.last_error: str | None = None
        self.pending = False
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def mark_dirty(self) -> None:
        self.pending = True
        self.dirty.set()

    def status(self) -> dict:
        return {"configured": self.configured(), "pending": self.pending,
                "last_ok": self.last_ok, "last_error": self.last_error}

    def configured(self) -> bool:
        return bool(self.db.get_setting("sync_url") or self.db.get_setting("sync_sheet_id"))

    async def push_now(self) -> dict:
        payload = build_payload(self.db, self.controller)
        url = self.db.get_setting("sync_url")
        if url:
            res = await asyncio.to_thread(_post_json, url, payload)
        elif self.db.get_setting("sync_sheet_id"):
            res = await asyncio.to_thread(self._push_gspread, payload)
        else:
            raise RuntimeError("Google Sheets sync is not configured")
        if not res.get("ok"):
            raise RuntimeError(res.get("error", "sync failed"))
        self.last_ok, self.last_error, self.pending = time.time(), None, False
        return res

    def _push_gspread(self, payload: dict) -> dict:
        import gspread  # optional dependency
        gc = gspread.service_account(filename=self.db.get_setting("sync_credentials_file"))
        sh = gc.open_by_key(self.db.get_setting("sync_sheet_id"))
        for title, rows in payload["tabs"].items():
            try:
                ws = sh.worksheet(title)
                ws.clear()
            except gspread.WorksheetNotFound:
                ws = sh.add_worksheet(title, rows=max(len(rows), 10), cols=max(len(rows[0]), 5))
            ws.update(rows, "A1")
        return {"ok": True, "tabs": list(payload["tabs"])}

    async def _loop(self) -> None:
        backoff = 5.0
        while True:
            await self.dirty.wait()
            await asyncio.sleep(3)  # coalesce bursts (a heat result + next-round generation)
            self.dirty.clear()
            if not self.configured():
                continue
            try:
                await self.push_now()
                backoff = 5.0
            except Exception as exc:  # offline, bad URL, quota
                self.last_error = str(exc)
                log.info("sheets sync failed, will retry in %.0fs: %s", backoff, exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)
                self.dirty.set()
