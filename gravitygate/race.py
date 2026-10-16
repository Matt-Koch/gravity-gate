"""Race controller: the live state machine behind the admin, display and spectator views.

States
    idle     no heat staged (between sessions, or display cancelled)
    staged   current heat is on the track, timer armed, waiting for a result
    results  results for the current heat are showing; "Go to Next Race" advances
"""
from __future__ import annotations

import asyncio
import logging
import statistics
import time
from collections import defaultdict
from typing import Any, Awaitable, Callable

from . import scheduler, scoring
from .db import Database
from .elimination import ElimConfig, EliminationEngine
from .timers import BaseTimer, NullTimer, ParsedResult

log = logging.getLogger("gravitygate.race")

DISPLAY_MODES = ("race", "standings", "awards", "bracket", "idle")
Publisher = Callable[[dict], Awaitable[None]]
PhotoRequester = Callable[[dict], Awaitable[None]]


def racer_label(r: dict | None) -> dict:
    if not r:
        return {}
    return {"racer_id": r["id"], "car_number": r["car_number"],
            "name": f"{r['first_name']} {r['last_name']}".strip(), "den": r["den"]}


class RaceController:
    def __init__(self, db: Database, timer: BaseTimer | None = None):
        self.db = db
        self.timer: BaseTimer = timer or NullTimer(db.get_setting("lane_count"), self.on_timer_event)
        self.state = "idle"
        self.current_heat_id: int | None = None
        self.awards_revealed = 0
        self.subscribers: set[Publisher] = set()
        self.photo_requesters: set[PhotoRequester] = set()
        self.change_listeners: list[Callable[[], None]] = []  # e.g. Sheets sync
        self.arm_listeners: list[Callable[[dict], None]] = []     # e.g. local camera
        self.lock = asyncio.Lock()
        self.last_photo: dict | None = None
        self.heat_finished_at: list[float] = []
        self._advance_task: asyncio.Task | None = None

    # -- plumbing --------------------------------------------------------
    def set_timer(self, timer: BaseTimer) -> None:
        self.timer = timer

    async def publish(self, kind: str = "state", extra: dict | None = None) -> None:
        msg = {"type": kind, **(extra or {})}
        if kind == "state":
            msg["state"] = self.snapshot()
        for sub in list(self.subscribers):
            try:
                await sub(msg)
            except Exception:
                self.subscribers.discard(sub)

    @property
    def session_id(self) -> int | None:
        return self.db.get_setting("current_session")

    def set_session(self, session_id: int | None) -> None:
        self.db.set_setting("current_session", session_id)
        self.current_heat_id = None
        self.state = "idle"

    # -- sessions --------------------------------------------------------
    def create_chart_session(self, name: str, racer_ids: list[int], runs_per_lane: int | None = None,
                             seed: int | None = None, mix_dens: bool = True, fmt: str = "rotation",
                             scope: str = "den") -> int:
        """Qualifier race.

        fmt="rotation"     round robin through the lanes: every car runs every lane
                           `runs_per_lane` times (3 lanes x 2 runs = 6 races per car)
        fmt="round_robin"  every car meets every other car (within each den or the pack)

        Scoring (average time vs heat points, dropped runs) is a live setting, not
        fixed per session, so flipping it in Settings re-ranks immediately.
        """
        lanes = self.db.active_lanes()
        rounds = int(runs_per_lane or self.db.get_setting("runs_per_lane") or 1)
        cfg = {"rounds": rounds, "seed": seed, "lanes": lanes, "mix_dens": mix_dens,
               "format": fmt, "scope": scope}
        sid = self.db.create_session(name, "chart", cfg, racer_ids)
        racers = [r for r in self.db.racers() if r["id"] in set(racer_ids)]
        self.db.add_heats(sid, scheduler.build_chart(racers, lanes, rounds, seed, mix_dens, fmt, scope))
        return sid

    def create_elimination_session(self, name: str, racer_ids: list[int], max_losses: int = 2,
                                   losers_per_heat: str = "half", runs_per_matchup: str = "single",
                                   seed: int | None = None) -> int:
        lanes = self.db.active_lanes()
        cfg = {"max_losses": max_losses, "losers_per_heat": losers_per_heat,
               "runs_per_matchup": runs_per_matchup, "seed": seed, "lanes": lanes,
               "dnf_time": self.db.get_setting("dnf_time")}
        sid = self.db.create_session(name, "elimination", cfg, racer_ids)
        self.advance_elimination(sid)
        return sid

    def engine(self, session_id: int) -> EliminationEngine:
        s = self.db.session(session_id)
        return EliminationEngine(self.db.session_racer_ids(session_id), s["config"]["lanes"],
                                 ElimConfig.from_dict(s["config"]))

    def advance_elimination(self, session_id: int) -> int:
        """Generate the next round if the current one is complete. Returns heats added."""
        eng = self.engine(session_id)
        heats = self.db.heats(session_id)
        results = self.db.valid_results(session_id)
        new = eng.next_round(heats, results)
        if new:
            self.db.add_heats(session_id, new)
        else:
            st = eng.compute(heats, results)
            if st.complete:
                self.db.execute("UPDATE sessions SET status = 'complete' WHERE id = ?", (session_id,))
        return len(new)

    def add_late_racer(self, session_id: int, racer_id: int) -> int:
        s = self.db.session(session_id)
        if s["kind"] != "chart":
            raise ValueError("late racers can only join chart sessions")
        self.db.add_session_racer(session_id, racer_id)
        lanes = s["config"]["lanes"]
        heats = []
        for _ in range(s["config"].get("rounds", 1)):
            heats += [{"round": 99, "race_in_round": i + 1, "bracket": None, "group_key": "makeup",
                       "lanes": h} for i, h in enumerate(scheduler.makeup_heats(racer_id, lanes))]
        return len(self.db.add_heats(session_id, heats))

    # -- heat flow -------------------------------------------------------
    def pending_heats(self, limit: int | None = None) -> list[dict]:
        if self.session_id is None:
            return []
        heats = self.db.heats(self.session_id, "pending")
        return heats[:limit] if limit else heats

    async def next_race(self) -> dict | None:
        """'Go to Next Race': stage the next pending heat and arm the timer."""
        async with self.lock:
            self._cancel_auto_advance()
            sid = self.session_id
            if sid is None:
                return None
            s = self.db.session(sid)
            if s and s["kind"] == "elimination":
                self.advance_elimination(sid)
            pending = self.pending_heats(1)
            if not pending:
                self.state = "idle"
                self.current_heat_id = None
                await self.publish()
                return None
            heat = pending[0]
            self.current_heat_id = heat["id"]
            self.state = "staged"
            self.last_photo = None
            self._arm(heat)
        await self.publish()
        await self.publish("sound", {"sound": "staged"})
        return heat

    def _arm(self, heat: dict) -> None:
        if hasattr(self.timer, "set_cars"):
            self.timer.set_cars(heat["lanes"])
        self.timer.arm(set(heat["lanes"]))
        for fn in self.arm_listeners:
            fn(heat)

    async def rerun(self) -> dict | None:
        """'Rerun Last Race': discard the latest results of the current heat and re-arm."""
        async with self.lock:
            self._cancel_auto_advance()
            hid = self.current_heat_id or self._last_done_heat_id()
            if hid is None:
                return None
            heat = self.db.heat(hid)
            self.db.invalidate_results(hid)
            self.db.set_heat_status(hid, "pending")
            if heat["session_id"] == self.session_id:
                self._drop_stale_elimination_heats(heat)
            self.current_heat_id = hid
            self.state = "staged"
            self.last_photo = None
            self._arm(heat)
        self._changed()
        await self.publish()
        return heat

    def _drop_stale_elimination_heats(self, heat: dict) -> None:
        """A rerun in round r invalidates any already-generated later rounds that haven't run."""
        s = self.db.session(heat["session_id"])
        if s["kind"] != "elimination":
            return
        self.db.execute("DELETE FROM heats WHERE session_id = ? AND round > ? AND status = 'pending'",
                        (heat["session_id"], heat["round"]))

    def _last_done_heat_id(self) -> int | None:
        if self.session_id is None:
            return None
        row = self.db.one("SELECT id FROM heats WHERE session_id = ? AND status = 'done' ORDER BY seq DESC LIMIT 1",
                          (self.session_id,))
        return row["id"] if row else None

    async def skip_heat(self, heat_id: int) -> None:
        self.db.set_heat_status(heat_id, "cancelled")
        if heat_id == self.current_heat_id:
            self.current_heat_id = None
            self.state = "idle"
        await self.publish()

    async def on_timer_event(self, kind: str, payload: Any) -> None:
        if kind == "result":
            await self.on_timer_result(payload)
        elif kind == "status":
            await self.publish("timer", {"timer": payload})
        elif kind == "raw":
            await self.publish("raw", {"line": payload})

    async def on_timer_result(self, parsed: ParsedResult, source: str = "timer") -> dict | None:
        async with self.lock:
            if self.state != "staged" or self.current_heat_id is None:
                log.info("ignoring timer result while %s: %s", self.state, parsed.raw)
                await self.publish("raw", {"line": f"(ignored, not staged) {parsed.raw}"})
                return None
            heat = self.db.heat(self.current_heat_id)
            rows = self._rows_from_times(heat, parsed.times, parsed.places)
            outcome = self._store(heat, rows, source)
        await self._after_results(heat, outcome)
        return outcome

    async def manual_entry(self, heat_id: int, entries: dict[int, dict]) -> dict:
        """entries: {lane: {"time": float|None, "place": int|None, "dnf": bool}}"""
        async with self.lock:
            heat = self.db.heat(heat_id)
            if heat is None:
                raise ValueError("no such heat")
            times = {}
            places = {}
            for lane, rid in heat["lanes"].items():
                e = entries.get(lane) or entries.get(str(lane)) or {}
                times[lane] = None if e.get("dnf") else e.get("time")
                if e.get("place"):
                    places[lane] = int(e["place"])
            if all(t is None for t in times.values()) and places:
                rows = [{"lane": l, "racer_id": rid, "time": None, "place": places.get(l),
                         "dnf": l not in places} for l, rid in heat["lanes"].items()]
            else:
                rows = self._rows_from_times(heat, times, places)
            self.current_heat_id = heat_id
            outcome = self._store(heat, rows, "manual")
        await self._after_results(heat, outcome)
        return outcome

    def _rows_from_times(self, heat: dict, times: dict[int, float | None],
                         timer_places: dict[int, int]) -> list[dict]:
        lane_times = {lane: times.get(lane) for lane in heat["lanes"]}
        places = scoring.placements(lane_times)
        # Dead heat on time: fall back to the timer's own place call if it made one.
        ties = [l for l in places if places[l] is not None and list(places.values()).count(places[l]) > 1]
        if ties and timer_places:
            for l in ties:
                if l in timer_places:
                    places[l] = timer_places[l]
        return [{"lane": lane, "racer_id": rid,
                 "time": None if lane_times[lane] is None else round(lane_times[lane], 4),
                 "place": places[lane], "dnf": lane_times[lane] is None}
                for lane, rid in heat["lanes"].items()]

    def _store(self, heat: dict, rows: list[dict], source: str) -> dict:
        attempt = self.db.record_results(heat["id"], rows, source)
        self.db.set_heat_status(heat["id"], "done")
        self.state = "results"
        self.heat_finished_at.append(time.time())
        finished = sorted(r["time"] for r in rows if r["time"] is not None)
        margins = [round((b - a) * 1000, 1) for a, b in zip(finished, finished[1:])]
        return {"heat_id": heat["id"], "attempt": attempt, "rows": rows,
                "min_margin_ms": min(margins) if margins else None}

    def _changed(self) -> None:
        for fn in self.change_listeners:
            fn()

    async def _after_results(self, heat: dict, outcome: dict) -> None:
        self._changed()
        s = self.db.session(heat["session_id"])
        if s and s["kind"] == "elimination":
            self.advance_elimination(s["id"])
        margin = outcome["min_margin_ms"]
        close = margin is not None and margin < float(self.db.get_setting("photo_margin_ms"))
        await self.publish()
        await self.publish("sound", {"sound": "photo" if close else "finish"})
        # Every heat is photographed so spectators can look up any finish;
        # only close ones (under photo_margin_ms) pop up on the projector.
        if self.db.get_setting("photo_enabled"):
            req = {"heat_id": heat["id"], "attempt": outcome["attempt"], "margin_ms": margin, "close": close,
                   "post_roll_s": float(self.db.get_setting("photo_post_roll_s") or 0),
                   "times": {r["lane"]: r["time"] for r in outcome["rows"]}}
            for requester in list(self.photo_requesters):
                try:
                    await requester(req)
                except Exception as exc:
                    log.warning("photo request failed: %s", exc)
        if self.db.get_setting("auto_advance"):
            self._advance_task = asyncio.create_task(self._auto_advance(float(self.db.get_setting("auto_advance_s") or 8)))

    async def _auto_advance(self, delay: float) -> None:
        await asyncio.sleep(delay)
        if self.state == "results":
            await self.next_race()

    def _cancel_auto_advance(self) -> None:
        if self._advance_task and not self._advance_task.done() and self._advance_task is not asyncio.current_task():
            self._advance_task.cancel()
        self._advance_task = None

    def is_close(self, margin_ms: float | None) -> bool:
        return margin_ms is not None and margin_ms < float(self.db.get_setting("photo_margin_ms"))

    async def photo_ready(self, heat_id: int, attempt: int, images: dict[str, str], margin_ms: float | None) -> None:
        for kind, path in images.items():
            self.db.add_photo(heat_id, attempt, kind, path, margin_ms)
        self.last_photo = {"heat_id": heat_id, "attempt": attempt, "images": images, "margin_ms": margin_ms,
                           "close": self.is_close(margin_ms)}
        await self.publish()
        await self.publish("photo", {"photo": self.last_photo})

    async def set_display(self, mode: str) -> None:
        if mode not in DISPLAY_MODES:
            raise ValueError(mode)
        self.db.set_setting("display_mode", mode)
        if mode == "awards":
            self.awards_revealed = 0
        await self.publish()

    async def reveal_next_award(self) -> None:
        self.awards_revealed += 1
        await self.publish()
        await self.publish("sound", {"sound": "award"})

    # -- views -----------------------------------------------------------
    def heat_view(self, heat: dict | None, racers: dict[int, dict]) -> dict | None:
        if heat is None:
            return None
        results = {r["lane"]: r for r in self.db.valid_results(heat_id=heat["id"])}
        lanes = []
        for lane, rid in sorted(heat["lanes"].items()):
            res = results.get(lane)
            lanes.append({"lane": lane, **racer_label(racers.get(rid)),
                          "time": res["time"] if res else None,
                          "place": res["place"] if res else None,
                          "dnf": bool(res["dnf"]) if res else False})
        return {"heat_id": heat["id"], "seq": heat["seq"], "round": heat["round"],
                "race": heat["race_in_round"], "bracket": heat["bracket"], "status": heat["status"],
                "lanes": lanes}

    def snapshot(self) -> dict:
        racers = self.db.racer_map()
        sid = self.session_id
        session = self.db.session(sid) if sid else None
        current = self.heat_view(self.db.heat(self.current_heat_id), racers) if self.current_heat_id else None
        upcoming = self.pending_heats(6)
        if current and upcoming and upcoming[0]["id"] == current["heat_id"]:
            upcoming = upcoming[1:]
        counts = {"done": 0, "pending": 0}
        if sid:
            for row in self.db.query("SELECT status, COUNT(*) AS n FROM heats WHERE session_id = ? GROUP BY status",
                                     (sid,)):
                counts[row["status"]] = row["n"]
        settings = self.db.all_settings()
        return {
            "event_name": settings["event_name"],
            "state": self.state,
            "display_mode": settings["display_mode"],
            "sound": settings["sound"],
            "photo_margin_ms": settings["photo_margin_ms"],
            "lanes": self.db.active_lanes(),
            "lane_count": settings["lane_count"],
            "session": {"id": session["id"], "name": session["name"], "kind": session["kind"],
                        "status": session["status"], "config": session["config"]} if session else None,
            "current": current,
            "on_deck": [self.heat_view(h, racers) for h in upcoming[:4]],
            "heat_counts": counts,
            "timer": self.timer.status(),
            "photo": self.last_photo,
            "awards_revealed": self.awards_revealed,
            "server_time": time.time(),
        }

    def standings(self, session_id: int | None = None) -> dict:
        sid = session_id or self.session_id
        if sid is None:
            return {"overall": [], "dens": {}}
        s = self.db.session(sid)
        racers = self.db.racer_map()
        results = self.db.valid_results(sid)
        if s["kind"] == "elimination":
            eng = self.engine(sid)
            st = eng.compute(self.db.heats(sid), results)
            rows = []
            for row in eng.final_standings(st):
                rows.append({**row, **racer_label(racers.get(row["racer_id"])), "rank": row["place"]})
            dens: dict[str, list] = defaultdict(list)
            for row in rows:
                dens[row["den"]].append(row)
            return {"kind": "elimination", "overall": rows, "dens": dict(dens),
                    "champion": st.champion, "complete": st.complete}
        cfg = s["config"]
        table = scoring.standings(results, racers, len(cfg["lanes"]),
                                  self.db.get_setting("standings_method") or "average",
                                  self.db.get_setting("dnf_time"), self.db.get_setting("drop_runs") or "none")
        table["kind"] = "chart"
        return table

    def awards(self, session_id: int | None = None, per_den: int = 3, overall: int = 3) -> list[dict]:
        sid = session_id or self.session_id
        if sid is None:
            return []
        table = self.standings(sid)
        if table.get("kind") == "elimination":
            return [{"category": "Champions Bracket", "place": r["place"], "tied": False, **r}
                    for r in reversed(table["overall"][:overall])]
        return scoring.awards(table, per_den, overall)

    def bracket(self, session_id: int | None = None) -> dict:
        sid = session_id or self.session_id
        if sid is None:
            return {}
        s = self.db.session(sid)
        if s["kind"] != "elimination":
            return {"kind": s["kind"]}
        racers = self.db.racer_map()
        eng = self.engine(sid)
        heats = self.db.heats(sid)
        st = eng.compute(heats, self.db.valid_results(sid))
        rounds: dict[int, list] = defaultdict(list)
        seen_groups = set()
        for h in heats:
            if h["status"] == "cancelled" or h["group_key"] in seen_groups:
                continue
            seen_groups.add(h["group_key"])
            outcome = st.group_outcomes.get(h["group_key"])
            rounds[h["round"]].append({
                "group_key": h["group_key"], "bracket": h["bracket"], "race": h["race_in_round"],
                "racers": [{**racer_label(racers[rid]),
                            "lost": bool(outcome and rid in outcome["losers"]),
                            "out": st.eliminated_round.get(rid) == h["round"]
                            and bool(outcome and rid in outcome["losers"]),
                            "total": outcome["totals"].get(rid) if outcome else None}
                           for rid in h["lanes"].values()],
                "done": outcome is not None,
            })
        return {"kind": "elimination", "max_losses": eng.config.max_losses,
                "rounds": [{"round": r, "matchups": m} for r, m in sorted(rounds.items())],
                "brackets": {b: [racer_label(racers[rid]) | {"losses": st.losses[rid]} for rid in ids]
                             for b, ids in st.brackets.items()},
                "champion": racer_label(racers.get(st.champion)) if st.champion else None,
                "needs_rerun": st.needs_rerun}

    def stats(self) -> dict:
        """Fun numbers for the spectator view."""
        racers = self.db.racer_map()
        sid = self.session_id
        results = [r for r in self.db.valid_results(sid) if not r["dnf"] and r["time"]] if sid else []
        settings = self.db.all_settings()
        out: dict[str, Any] = {"runs": len(results)}
        if not results:
            return out
        fastest = min(results, key=lambda r: r["time"])
        mph = settings["track_length_ft"] / fastest["time"] * 3600 / 5280
        out["fastest"] = {**racer_label(racers.get(fastest["racer_id"])), "time": fastest["time"],
                          "lane": fastest["lane"], "scale_mph": round(mph * settings["scale_factor"], 1),
                          "real_mph": round(mph, 1)}
        by_lane: dict[int, list[float]] = defaultdict(list)
        by_den: dict[str, dict] = {}
        for r in results:
            by_lane[r["lane"]].append(r["time"])
            den = racers.get(r["racer_id"], {}).get("den", "")
            if den not in by_den or r["time"] < by_den[den]["time"]:
                by_den[den] = {**racer_label(racers.get(r["racer_id"])), "time": r["time"]}
        out["lanes"] = [{"lane": l, "average": round(statistics.mean(ts), 4), "runs": len(ts),
                         "best": min(ts)} for l, ts in sorted(by_lane.items())]
        out["fastest_by_den"] = dict(sorted(by_den.items()))
        by_heat: dict[int, list[dict]] = defaultdict(list)
        for r in results:
            by_heat[r["heat_id"]].append(r)
        closest = None
        for hid, rs in by_heat.items():
            ts = sorted(rs, key=lambda r: r["time"])
            if len(ts) >= 2:
                m = (ts[1]["time"] - ts[0]["time"]) * 1000
                if closest is None or m < closest["margin_ms"]:
                    closest = {"heat_id": hid, "margin_ms": round(m, 1),
                               "winner": racer_label(racers.get(ts[0]["racer_id"])),
                               "runner_up": racer_label(racers.get(ts[1]["racer_id"]))}
        out["closest_finish"] = closest
        done = self.heat_finished_at[-20:]
        pending = len(self.pending_heats())
        if len(done) >= 2:
            cadence = (done[-1] - done[0]) / (len(done) - 1)
            out["cadence_s"] = round(cadence, 1)
            out["eta_min"] = round(pending * cadence / 60, 1)
        out["heats_remaining"] = pending
        return out

    def heat_photo(self, heat_id: int, attempt: int | None = None) -> dict | None:
        """Finish photo for a heat's current (valid) attempt: {images, margin_ms, close}."""
        rows = self.db.photos(heat_id)
        if attempt is not None:
            rows = [p for p in rows if p["attempt"] == attempt]
        elif rows:
            rows = [p for p in rows if p["attempt"] == rows[0]["attempt"]]
        if not rows:
            return None
        margin = rows[0]["margin_ms"]
        return {"images": {p["kind"]: p["path"] for p in rows}, "margin_ms": margin, "close": self.is_close(margin)}

    def recent_finishes(self, limit: int = 12) -> list[dict]:
        """Latest photographed heats in the active session, newest first, for the phone gallery."""
        racers = self.db.racer_map()
        out = []
        for row in self.db.query(
                "SELECT DISTINCT p.heat_id, p.attempt FROM photos p JOIN heats h ON h.id = p.heat_id "
                "WHERE h.session_id = ? AND h.status = 'done' AND EXISTS (SELECT 1 FROM results r "
                "WHERE r.heat_id = p.heat_id AND r.attempt = p.attempt AND r.valid = 1) "
                "ORDER BY p.id DESC LIMIT ?", (self.session_id, limit)):
            heat = self.heat_view(self.db.heat(row["heat_id"]), racers)
            out.append({**heat, "photo": self.heat_photo(row["heat_id"], row["attempt"])})
        return out

    def racer_detail(self, racer_id: int) -> dict:
        racers = self.db.racer_map()
        r = racers.get(racer_id)
        if r is None:
            raise KeyError(racer_id)
        runs = [x for x in self.db.valid_results() if x["racer_id"] == racer_id]
        upcoming = []
        for i, h in enumerate(self.pending_heats()):
            if racer_id in h["lanes"].values():
                lane = next(l for l, rid in h["lanes"].items() if rid == racer_id)
                upcoming.append({"heat_id": h["id"], "heats_away": i, "lane": lane,
                                 "round": h["round"], "race": h["race_in_round"]})
        rank = None
        table = self.standings()
        for row in table.get("dens", {}).get(r["den"], []):
            if row["racer_id"] == racer_id:
                rank = row["rank"]
        return {**racer_label(r), "runs": [{"time": x["time"], "lane": x["lane"], "place": x["place"],
                                            "dnf": bool(x["dnf"]), "heat_id": x["heat_id"],
                                            "photo": self.heat_photo(x["heat_id"], x["attempt"])} for x in runs],
                "upcoming": upcoming, "den_rank": rank}
