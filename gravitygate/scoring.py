"""Standings and awards for chart sessions.

Rankings are computed pack-wide and per den from the same per-racer stats,
so dens stay together in reports while heats mix all dens.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable

METHODS = ("average", "total", "best", "points")
# drop_runs: which run times to discard before averaging.
#   "none" | "slowest" | "fastest_slowest" (3 lanes x 2 runs -> average of the middle 4)
DROP_MODES = ("none", "slowest", "fastest_slowest")


def placements(lane_times: dict[int, float | None]) -> dict[int, int | None]:
    """Places from times (lower is better). DNF (None) gets no place. Ties share a place."""
    finished = sorted((t, lane) for lane, t in lane_times.items() if t is not None)
    out: dict[int, int | None] = {lane: None for lane in lane_times}
    prev_t, prev_place = None, 0
    for i, (t, lane) in enumerate(finished, start=1):
        place = prev_place if prev_t is not None and abs(t - prev_t) < 1e-9 else i
        out[lane] = place
        prev_t, prev_place = t, place
    return out


def racer_stats(results: Iterable[dict], lane_count: int, dnf_time: float,
                drop_runs: str = "none") -> dict[int, dict]:
    runs: dict[int, list[dict]] = defaultdict(list)
    for r in results:
        runs[r["racer_id"]].append(r)
    out = {}
    for rid, rs in runs.items():
        times = [dnf_time if (x["dnf"] or x["time"] is None) else x["time"] for x in rs]
        # Heat points: fastest in a heat earns as many points as there are lanes,
        # each place after that one fewer, down to 1. DNF earns 0.
        pts = [0 if (x["dnf"] or x["place"] is None) else max(lane_count - x["place"] + 1, 1) for x in rs]
        counted = sorted(times)
        if drop_runs == "slowest" and len(counted) > 1:
            counted = counted[:-1]
        elif drop_runs == "fastest_slowest" and len(counted) > 2:
            counted = counted[1:-1]
        out[rid] = {
            "racer_id": rid,
            "runs": len(rs),
            "times": times,
            "average": sum(counted) / len(counted),
            "total": sum(counted),
            "best": min(times),
            "counted_runs": len(counted),
            "points": sum(pts),
            "points_per_run": round(sum(pts) / len(pts), 3),
            "lanes": sorted(x["lane"] for x in rs),
        }
    return out


def _sort_key(method: str):
    if method == "points":
        # Per run, so round-robin cars with one extra heat aren't favoured.
        return lambda s: (-s["points_per_run"], s["average"])
    return lambda s: (s[method], s["best"])


def _rank(rows: list[dict], method: str) -> None:
    key = _sort_key(method)
    rows.sort(key=key)
    prev, prev_rank = None, 0
    counts: dict[int, int] = defaultdict(int)
    for i, row in enumerate(rows, start=1):
        k = key(row)
        rank = prev_rank if k == prev else i
        row["rank"] = rank
        counts[rank] += 1
        prev, prev_rank = k, rank
    for row in rows:
        row["tied"] = counts[row["rank"]] > 1


def standings(results: Iterable[dict], racers: dict[int, dict], lane_count: int,
              method: str = "average", dnf_time: float = 9.999, drop_runs: str = "none") -> dict:
    if method not in METHODS:
        raise ValueError(f"unknown scoring method {method!r}")
    if drop_runs not in DROP_MODES:
        raise ValueError(f"unknown drop mode {drop_runs!r}")
    stats = racer_stats(results, lane_count, dnf_time, drop_runs)
    rows = []
    for rid, s in stats.items():
        r = racers.get(rid, {})
        rows.append({**s, "car_number": r.get("car_number"),
                     "name": f"{r.get('first_name', '')} {r.get('last_name', '')}".strip(),
                     "den": r.get("den", "")})
    overall = [dict(x) for x in rows]
    _rank(overall, method)
    dens: dict[str, list[dict]] = defaultdict(list)
    for x in rows:
        dens[x["den"]].append(dict(x))
    for den_rows in dens.values():
        _rank(den_rows, method)
    return {"method": method, "drop_runs": drop_runs, "overall": overall, "dens": dict(sorted(dens.items()))}


def awards(table: dict, per_den: int = 3, overall: int = 3) -> list[dict]:
    """Award list in reveal order (lowest place first, dens then pack)."""
    out = []
    for den, rows in table["dens"].items():
        for row in rows:
            if row["rank"] <= per_den:
                out.append({"category": f"{den or 'Unassigned'}", "place": row["rank"],
                            "tied": row["tied"], **_brief(row)})
    for row in table["overall"]:
        if row["rank"] <= overall:
            out.append({"category": "Pack Overall", "place": row["rank"], "tied": row["tied"], **_brief(row)})
    out.sort(key=lambda a: (a["category"] == "Pack Overall", a["category"], -a["place"]))
    return out


def _brief(row: dict) -> dict:
    return {k: row[k] for k in ("racer_id", "car_number", "name", "den", "average", "best", "points")}


def seed_from_dens(table: dict, per_den: int) -> list[int]:
    """Racer ids of the top `per_den` of each den (for a champions bracket)."""
    out = []
    for rows in table["dens"].values():
        out.extend(r["racer_id"] for r in rows if r["rank"] <= per_den)
    return out
