"""Roster import/export: CSV files, Google Sheets "Publish to web" CSV links,
with results write-back handled by gravitygate.sync.

Easiest path (no credentials): in Google Sheets choose
File -> Share -> Publish to web -> (roster tab) -> CSV, and paste the link
into Settings. Any normal sheet URL is also accepted and rewritten to the
CSV export URL, which works if the sheet is shared "anyone with the link".
"""
from __future__ import annotations

import csv
import io
import re
import urllib.request
from typing import Iterable

from .db import Database

# Header aliases, compared case-insensitively with punctuation removed.
ALIASES = {
    "car_number": {"car", "carnumber", "car#", "carno", "number", "no", "#"},
    "first_name": {"first", "firstname", "given", "scout", "scoutfirst", "givenname"},
    "last_name": {"last", "lastname", "surname", "family", "familyname", "scoutlast"},
    "name": {"name", "scoutname", "racer", "racername", "fullname"},
    "den": {"den", "group", "class", "patrol", "denname", "dennumber"},
    "rank": {"rank", "level", "grade", "age"},
    "checked_in": {"checkedin", "checkin", "present", "here"},
}


def _norm(h: str) -> str:
    return re.sub(r"[^a-z0-9#]", "", h.lower())


def map_headers(headers: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for i, h in enumerate(headers):
        n = _norm(h)
        for field, names in ALIASES.items():
            if n in names and field not in out:
                out[field] = i
    return out


def sheet_csv_url(url: str) -> str:
    """Turn a normal Google Sheets URL into its CSV export URL."""
    url = url.strip()
    if "output=csv" in url or "format=csv" in url:
        return url
    m = re.search(r"docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]+)", url)
    if not m:
        return url
    gid = re.search(r"[#&?]gid=(\d+)", url)
    out = f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv"
    return out + (f"&gid={gid.group(1)}" if gid else "")


def parse_csv(text: str) -> list[dict]:
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    if not rows:
        return []
    cols = map_headers(rows[0])
    if "first_name" not in cols and "name" not in cols:
        raise ValueError("roster needs a Name or First/Last column; found: " + ", ".join(rows[0]))
    out = []
    for row in rows[1:]:
        if not any(c.strip() for c in row):
            continue
        get = lambda f: row[cols[f]].strip() if f in cols and cols[f] < len(row) else ""
        first, last = get("first_name"), get("last_name")
        if not first and get("name"):
            parts = get("name").split()
            first, last = parts[0], " ".join(parts[1:])
        car = get("car_number").lstrip("#")
        out.append({
            "first_name": first, "last_name": last,
            "den": get("den"), "rank": get("rank"),
            "car_number": int(car) if car.isdigit() else None,
            "checked_in": get("checked_in").lower() in {"y", "yes", "x", "true", "1"},
        })
    return out


def import_rows(db: Database, rows: list[dict]) -> dict:
    """Upsert by car number, else by (first, last, den). Never deletes racers."""
    existing = db.racers()
    by_car = {r["car_number"]: r for r in existing if r["car_number"] is not None}
    by_name = {(r["first_name"].lower(), r["last_name"].lower()): r for r in existing}
    added = updated = 0
    for row in rows:
        match = by_car.get(row["car_number"]) if row["car_number"] else None
        match = match or by_name.get((row["first_name"].lower(), row["last_name"].lower()))
        if match:
            fields = {k: row[k] for k in ("first_name", "last_name", "den", "rank") if row[k]}
            if row["car_number"] and row["car_number"] != match["car_number"] and row["car_number"] not in by_car:
                fields["car_number"] = row["car_number"]
            if row["checked_in"]:
                fields["checked_in"] = 1
            db.update_racer(match["id"], **fields)
            updated += 1
        else:
            car = row["car_number"] if row["car_number"] and row["car_number"] not in by_car else None
            rid = db.add_racer(row["first_name"], row["last_name"], row["den"], row["rank"], car,
                               row["checked_in"])
            new = db.one("SELECT * FROM racers WHERE id = ?", (rid,))
            by_car[new["car_number"]] = new
            by_name[(new["first_name"].lower(), new["last_name"].lower())] = new
            added += 1
    return {"added": added, "updated": updated, "total": len(rows)}


def fetch_url(url: str, timeout: float = 15) -> str:
    req = urllib.request.Request(sheet_csv_url(url), headers={"User-Agent": "Gravity Gate"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8-sig")


def import_from_url(db: Database, url: str) -> dict:
    return import_rows(db, parse_csv(fetch_url(url)))


def export_csv(db: Database) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Car", "First", "Last", "Den", "Rank", "Checked In", "Inspected"])
    for r in db.racers():
        w.writerow([r["car_number"], r["first_name"], r["last_name"], r["den"], r["rank"],
                    "Y" if r["checked_in"] else "", "Y" if r["passed_inspection"] else ""])
    return buf.getvalue()

