"""N-lane single / double / triple elimination.

Racers accumulate losses. Bracket letters follow the old software's display:
Bracket A = 0 losses, Bracket B = 1 loss, Bracket C = 2 losses. A racer is
out when losses == max_losses (1 = single, 2 = double, 3 = triple).

Each round every bracket is split into balanced heats of at most N cars.
In each heat (a "matchup") the bottom k finishers take a loss:
    losers_per_heat = "half" -> k = floor(size / 2)
    losers_per_heat = "last" -> k = 1
A matchup is either one run, or one run per lane with lanes rotated
("runs_per_matchup": "lanes") in which case summed time decides.

When everyone left fits in one heat the brackets merge into a Final.
An undefeated racer still needs max_losses losses to be knocked out, so a
true double-elimination "if necessary" final happens on its own.

The engine is pure: state is recomputed from heats + results every time, so
reruns and manual corrections can never desynchronize the bracket.
"""
from __future__ import annotations

import itertools
import math
import random
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Sequence

BRACKET_LETTERS = "ABCDEFG"


@dataclass
class ElimConfig:
    max_losses: int = 2
    losers_per_heat: str = "half"          # "half" | "last"
    runs_per_matchup: str = "single"       # "single" | "lanes"
    avoid_rematch: bool = True
    dnf_time: float = 9.999
    seed: int | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "ElimConfig":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class ElimState:
    round: int = 0
    losses: dict[int, int] = field(default_factory=dict)
    alive: list[int] = field(default_factory=list)
    brackets: dict[str, list[int]] = field(default_factory=dict)
    eliminated_round: dict[int, int] = field(default_factory=dict)
    last_group_rank: dict[int, int] = field(default_factory=dict)
    best_time: dict[int, float] = field(default_factory=dict)
    lane_use: dict[int, dict[int, int]] = field(default_factory=dict)
    pairs: dict[frozenset, int] = field(default_factory=dict)
    pending_heats: int = 0
    needs_rerun: list[int] = field(default_factory=list)
    champion: int | None = None
    complete: bool = False
    group_outcomes: dict[str, dict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "round": self.round,
            "losses": self.losses,
            "alive": self.alive,
            "brackets": self.brackets,
            "eliminated_round": self.eliminated_round,
            "pending_heats": self.pending_heats,
            "needs_rerun": self.needs_rerun,
            "champion": self.champion,
            "complete": self.complete,
            "group_outcomes": self.group_outcomes,
        }


def bracket_letter(losses: int) -> str:
    return BRACKET_LETTERS[losses]


def split_sizes(m: int, lanes: int) -> list[int]:
    """Balanced heat sizes: 7 cars on 3 lanes -> [3, 2, 2]."""
    if m <= 0:
        return []
    h = math.ceil(m / lanes)
    base, extra = divmod(m, h)
    return [base + 1] * extra + [base] * (h - extra)


def losers_count(size: int, policy: str) -> int:
    if size < 2:
        return 0
    return size // 2 if policy == "half" else 1


class EliminationEngine:
    def __init__(self, racer_ids: Sequence[int], lanes: Sequence[int], config: ElimConfig):
        self.racer_ids = list(racer_ids)
        self.lanes = list(lanes)
        self.config = config
        if len(self.lanes) < 2:
            raise ValueError("elimination needs at least 2 active lanes")

    # -- state -----------------------------------------------------------
    def compute(self, heats: Sequence[dict], results: Sequence[dict]) -> ElimState:
        cfg = self.config
        st = ElimState()
        st.losses = {rid: 0 for rid in self.racer_ids}
        st.lane_use = {rid: defaultdict(int) for rid in self.racer_ids}
        by_heat: dict[int, list[dict]] = defaultdict(list)
        for r in results:
            by_heat[r["heat_id"]].append(r)

        groups: dict[str, list[dict]] = defaultdict(list)
        for h in heats:
            if h["status"] == "cancelled":
                continue
            groups[h["group_key"] or f"h{h['id']}"].append(h)
            st.round = max(st.round, h["round"])
            for lane, rid in h["lanes"].items():
                if rid in st.lane_use:
                    st.lane_use[rid][lane] += 1
            ids = list(h["lanes"].values())
            for a, b in itertools.combinations(ids, 2):
                key = frozenset((a, b))
                st.pairs[key] = st.pairs.get(key, 0) + 1

        # Apply group outcomes in round order so elimination rounds are right.
        ordered = sorted(groups.items(), key=lambda kv: (kv[1][0]["round"], kv[1][0]["seq"]))
        for gkey, gheats in ordered:
            done = all(by_heat.get(h["id"]) for h in gheats)
            if not done:
                st.pending_heats += sum(1 for h in gheats if not by_heat.get(h["id"]))
                continue
            outcome = self._group_outcome(gheats, by_heat)
            st.group_outcomes[gkey] = outcome
            for rid, t in outcome["best"].items():
                st.best_time[rid] = min(st.best_time.get(rid, math.inf), t)
            if outcome["tie_on_boundary"]:
                st.needs_rerun.extend(h["id"] for h in gheats)
                continue
            rnd = gheats[0]["round"]
            for pos, rid in enumerate(outcome["order"], start=1):
                st.last_group_rank[rid] = pos
            for rid in outcome["losers"]:
                if st.losses[rid] < cfg.max_losses:
                    st.losses[rid] += 1
                    if st.losses[rid] >= cfg.max_losses:
                        st.eliminated_round[rid] = rnd

        st.alive = [rid for rid in self.racer_ids if st.losses[rid] < cfg.max_losses]
        st.brackets = {}
        for rid in st.alive:
            st.brackets.setdefault(bracket_letter(st.losses[rid]), []).append(rid)
        if len(st.alive) == 1 and st.pending_heats == 0 and not st.needs_rerun:
            st.champion = st.alive[0]
            st.complete = True
        return st

    def _group_outcome(self, gheats: list[dict], by_heat: dict[int, list[dict]]) -> dict:
        cfg = self.config
        total: dict[int, float] = defaultdict(float)
        best: dict[int, float] = {}
        place_sum: dict[int, float] = defaultdict(float)
        for h in gheats:
            for r in by_heat[h["id"]]:
                t = cfg.dnf_time if (r["dnf"] or r["time"] is None) else r["time"]
                total[r["racer_id"]] += t
                best[r["racer_id"]] = min(best.get(r["racer_id"], math.inf), t)
                place_sum[r["racer_id"]] += r["place"] if r["place"] else 99
        members = list(gheats[0]["lanes"].values())
        if len(gheats) == 1:
            key = lambda rid: (place_sum[rid], total[rid])
        else:
            key = lambda rid: (round(total[rid], 4), place_sum[rid])
        order = sorted(members, key=key)
        k = losers_count(len(order), cfg.losers_per_heat)
        tie = False
        if 0 < k < len(order):
            last_safe, first_loser = order[-k - 1], order[-k]
            tie = key(last_safe) == key(first_loser)
        return {"order": order, "losers": order[len(order) - k:] if k else [],
                "totals": dict(total), "best": best, "tie_on_boundary": tie}

    # -- next round ------------------------------------------------------
    def next_round(self, heats: Sequence[dict], results: Sequence[dict]) -> list[dict]:
        """Heats for the next round, or [] if the round isn't finished / event is over."""
        st = self.compute(heats, results)
        if st.pending_heats or st.needs_rerun or st.complete or len(st.alive) <= 1:
            return []
        rng = random.Random(None if self.config.seed is None else self.config.seed + st.round)
        rnd = st.round + 1
        n = len(self.lanes)

        pools: list[tuple[str, list[int]]] = []
        if len(st.alive) <= n:
            pools.append(("F", list(st.alive)))
        else:
            letters = sorted(st.brackets)
            if all(len(st.brackets[b]) < 2 for b in letters):
                pools.append(("F", list(st.alive)))
            else:
                for b in letters:
                    if len(st.brackets[b]) >= 2:
                        pools.append((b, list(st.brackets[b])))
                    # A lone racer in a bracket gets a bye this round.

        out: list[dict] = []
        race_no = 0
        for gi, (bracket, members) in enumerate(pools):
            groups = self._group(members, st, rng)
            for hi, grp in enumerate(groups):
                if len(grp) < 2:
                    continue  # bye: a car never races alone
                lanes_for = self._assign_lanes(grp, st)
                gkey = f"r{rnd}-{bracket}-{hi + 1}"
                runs = [lanes_for]
                if self.config.runs_per_matchup == "lanes" and len(grp) > 1:
                    used = sorted(lanes_for)
                    order = [lanes_for[l] for l in used]
                    runs = []
                    for shift in range(len(used)):
                        runs.append({used[(i + shift) % len(used)]: rid for i, rid in enumerate(order)})
                for run in runs:
                    race_no += 1
                    out.append({"round": rnd, "race_in_round": race_no, "bracket": bracket,
                                "group_key": gkey, "lanes": run})
        return out

    def _group(self, members: list[int], st: ElimState, rng: random.Random) -> list[list[int]]:
        sizes = split_sizes(len(members), len(self.lanes))
        best, best_score = None, None
        tries = 200 if self.config.avoid_rematch else 1
        for _ in range(tries):
            pool = members[:]
            rng.shuffle(pool)
            groups, i = [], 0
            for s in sizes:
                groups.append(pool[i:i + s])
                i += s
            score = sum(st.pairs.get(frozenset(p), 0)
                        for g in groups for p in itertools.combinations(g, 2))
            if best_score is None or score < best_score:
                best, best_score = groups, score
                if score == 0:
                    break
        return best or []

    def _assign_lanes(self, grp: list[int], st: ElimState) -> dict[int, int]:
        """Pick lanes minimizing each racer's repeat use of the same lane."""
        def cost(assign: Sequence[int]) -> int:
            return sum(st.lane_use.get(rid, {}).get(lane, 0) ** 2 for rid, lane in zip(grp, assign))

        if len(self.lanes) <= 6:
            best = min(itertools.permutations(self.lanes, len(grp)), key=cost)
        else:
            best, free = [], list(self.lanes)
            for rid in grp:
                lane = min(free, key=lambda l: st.lane_use.get(rid, {}).get(l, 0))
                free.remove(lane)
                best.append(lane)
        for rid, lane in zip(grp, best):
            st.lane_use.setdefault(rid, defaultdict(int))[lane] += 1
        return {lane: rid for rid, lane in zip(grp, best)}

    # -- placings --------------------------------------------------------
    def final_standings(self, st: ElimState) -> list[dict]:
        """Champion first, then by losses / elimination round (later = better)."""
        def key(rid: int):
            alive = rid in st.alive
            return (
                0 if rid == st.champion else 1,
                0 if alive else 1,
                st.losses[rid],
                -st.eliminated_round.get(rid, 10 ** 6),
                st.last_group_rank.get(rid, 99),
                st.best_time.get(rid, math.inf),
            )
        ordered = sorted(self.racer_ids, key=key)
        return [{"place": i, "racer_id": rid, "losses": st.losses[rid],
                 "alive": rid in st.alive, "eliminated_round": st.eliminated_round.get(rid),
                 "best": None if st.best_time.get(rid, math.inf) == math.inf else st.best_time[rid]}
                for i, rid in enumerate(ordered, start=1)]
