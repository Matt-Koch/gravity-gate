"""Chart (round-robin style) heat generation.

Lane-balanced rotation: with C cars in a ring and N lanes, heat h puts the
car at ring position (h + offset[l]) mod C in lane l. Every car therefore
races every lane exactly once per round. Offsets are chosen so their pairwise
differences are as distinct as possible (a modular Golomb ruler), which
maximizes how many different opponents each car meets, and so no difference
is tiny (a difference of 1 would make a car race in back-to-back heats).

Cars are shuffled and den-interleaved before being placed on the ring, so
heats mix dens while standings can still be reported per den.
"""
from __future__ import annotations

import itertools
import math
import random
from collections import defaultdict
from typing import Hashable, Sequence

BYE = None


def den_interleave(racers: Sequence[dict], rng: random.Random) -> list[dict]:
    """Shuffle within each den, then deal round-robin across dens.

    Dens are dealt largest-first so a big den doesn't cluster at the end.
    """
    by_den: dict[str, list[dict]] = defaultdict(list)
    for r in racers:
        by_den[r.get("den", "")].append(r)
    groups = list(by_den.values())
    for g in groups:
        rng.shuffle(g)
    rng.shuffle(groups)
    groups.sort(key=len, reverse=True)
    # Spread each den evenly over the output using fractional positions.
    placed: list[tuple[float, float, dict]] = []
    for g in groups:
        n = len(g)
        jitter = rng.random()
        for i, r in enumerate(g):
            placed.append(((i + jitter) / n, rng.random(), r))
    placed.sort(key=lambda t: (t[0], t[1]))
    return [r for _, _, r in placed]


def _offset_score(offsets: Sequence[int], ring: int) -> tuple[int, int]:
    """(distinct opponent offsets, min circular gap between a car's heats)."""
    diffs = set()
    min_gap = ring
    for a, b in itertools.permutations(offsets, 2):
        d = (b - a) % ring
        diffs.add(d)
        min_gap = min(min_gap, d, ring - d)
    return len(diffs), min_gap


def choose_offsets(ring: int, lanes: int, rng: random.Random, tries: int = 4000) -> list[int]:
    """Pick lane offsets that maximize opponent variety and heat spacing."""
    if lanes <= 1:
        return [0]
    if ring <= lanes:
        return list(range(lanes))
    best: list[int] = list(range(lanes))
    best_score = _offset_score(best, ring)
    candidates = list(range(1, ring))
    total = 1
    for k in range(lanes - 1):
        total = total * (ring - 1 - k) // (k + 1)
        if total > tries:
            break
    if total <= tries:
        pool = (list(c) for c in itertools.combinations(candidates, lanes - 1))
    else:
        pool = (sorted(rng.sample(candidates, lanes - 1)) for _ in range(tries))
    for rest in pool:
        offs = [0, *rest]
        score = _offset_score(offs, ring)
        if score > best_score:
            best, best_score = offs, score
    order = best[:]
    rng.shuffle(order)  # which lane gets which offset is arbitrary
    return order


def rotation_chart(
    racer_ids: Sequence[Hashable],
    lanes: Sequence[int],
    rounds: int = 1,
    rng: random.Random | None = None,
) -> list[dict[int, Hashable]]:
    """Return a list of heats, each {physical_lane: racer_id}.

    Each racer runs once in each lane per round. Byes fill the ring when
    there are fewer racers than lanes; bye lanes are simply left empty.
    """
    rng = rng or random.Random()
    lanes = list(lanes)
    n = len(lanes)
    if n == 0 or not racer_ids:
        return []
    heats: list[dict[int, Hashable]] = []
    for _ in range(rounds):
        ring: list[Hashable | None] = list(racer_ids)
        while len(ring) < n:
            ring.append(BYE)
        # Spread byes around the ring so empty lanes don't cluster.
        if BYE in ring:
            real = [r for r in ring if r is not BYE]
            byes = len(ring) - len(real)
            ring = []
            step = len(real) / byes if byes else 0
            bi = 0
            for i, r in enumerate(real):
                ring.append(r)
                while bi < byes and (bi + 1) * step <= i + 1:
                    ring.append(BYE)
                    bi += 1
            ring.extend([BYE] * (byes - bi))
        c = len(ring)
        offsets = choose_offsets(c, n, rng)
        for h in range(c):
            heat = {}
            for li, lane in enumerate(lanes):
                rid = ring[(h + offsets[li]) % c]
                if rid is not BYE:
                    heat[lane] = rid
            if heat:
                heats.append(heat)
        rng.shuffle(lanes)  # later rounds use a different lane/offset pairing
    return heats


def makeup_heats(racer_id: Hashable, lanes: Sequence[int]) -> list[dict[int, Hashable]]:
    """One heat per lane for a late arrival."""
    return [{lane: racer_id} for lane in lanes]


def chart_stats(heats: Sequence[dict[int, Hashable]]) -> dict:
    """Diagnostics: lane counts per racer, distinct opponents, min heat gap."""
    lane_counts: dict[Hashable, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    opponents: dict[Hashable, set] = defaultdict(set)
    last_seen: dict[Hashable, int] = {}
    min_gap = None
    for i, heat in enumerate(heats):
        ids = list(heat.values())
        for lane, rid in heat.items():
            lane_counts[rid][lane] += 1
            opponents[rid].update(x for x in ids if x != rid)
            if rid in last_seen:
                gap = i - last_seen[rid]
                min_gap = gap if min_gap is None else min(min_gap, gap)
            last_seen[rid] = i
    return {
        "heats": len(heats),
        "lane_counts": {k: dict(v) for k, v in lane_counts.items()},
        "distinct_opponents": {k: len(v) for k, v in opponents.items()},
        "min_gap": min_gap,
    }


def _assign_lanes_balanced(group: Sequence[Hashable], lanes: Sequence[int],
                           lane_counts: dict[Hashable, dict[int, int]]) -> dict[int, Hashable]:
    """Put each car in the lane it has used least so far (exact for <= 6 lanes)."""
    def cost(assign):
        return sum((lane_counts[c][l] + 1) ** 2 for c, l in zip(group, assign))
    if len(lanes) <= 6:
        best = min(itertools.permutations(lanes, len(group)), key=cost)
    else:
        best, free = [], list(lanes)
        for c in group:
            l = min(free, key=lambda x: lane_counts[c][x])
            free.remove(l)
            best.append(l)
    for c, l in zip(group, best):
        lane_counts[c][l] += 1
    return {l: c for c, l in zip(group, best)}


def round_robin(racer_ids: Sequence[Hashable], lanes: Sequence[int],
                rng: random.Random | None = None) -> list[dict[int, Hashable]]:
    """Every car races every other car at least once.

    Heats of up to N cars are packed greedily so each heat covers as many
    not-yet-raced pairings as possible (a covering design), preferring cars
    that have raced least and longest ago. Lanes are then assigned so each
    car's lane usage stays as even as possible. Groups that fit on the track
    in one heat (n <= N) run once per lane instead, so lane luck cancels out.
    """
    rng = rng or random.Random()
    ids = list(racer_ids)
    lanes = list(lanes)
    n, k = len(ids), len(lanes)
    if n < 2 or k < 2:
        return []
    if n <= k:
        rng.shuffle(ids)
        return [{lanes[(i + shift) % k]: c for i, c in enumerate(ids)} for shift in range(k)]

    uncovered = {frozenset(p) for p in itertools.combinations(ids, 2)}
    raced: dict[Hashable, int] = {c: 0 for c in ids}
    last: dict[Hashable, int] = {c: -10 for c in ids}
    lane_counts: dict[Hashable, dict[int, int]] = {c: defaultdict(int) for c in ids}
    order = ids[:]
    rng.shuffle(order)
    heats: list[dict[int, Hashable]] = []
    while uncovered:
        h = len(heats)
        open_pairs = {c: sum(1 for d in ids if d != c and frozenset((c, d)) in uncovered) for c in ids}
        # Seed: most outstanding pairings, then fewest races, then rested longest.
        seed = max(order, key=lambda c: (open_pairs[c] > 0, -raced[c], open_pairs[c], h - last[c] > 1, -last[c]))
        group = [seed]
        while len(group) < k:
            cands = [c for c in order if c not in group]
            def gain(c):
                return sum(1 for g in group if frozenset((c, g)) in uncovered)
            nxt = max(cands, key=lambda c: (gain(c), h - last[c] > 1, -raced[c], open_pairs[c]))
            group.append(nxt)
        for a, b in itertools.combinations(group, 2):
            uncovered.discard(frozenset((a, b)))
        for c in group:
            raced[c] += 1
            last[c] = h
        rng.shuffle(group)
        heats.append(_assign_lanes_balanced(group, lanes, lane_counts))
    return space_heats(heats)


def space_heats(heats: list[dict[int, Hashable]], window: int = 2) -> list[dict[int, Hashable]]:
    """Reorder so cars rest between races: avoid anyone from the previous `window` heats."""
    left = heats[:]
    out: list[dict[int, Hashable]] = []
    while left:
        for w in range(window, -1, -1):
            recent = {c for h in out[-w:] for c in h.values()} if w else set()
            pick = next((i for i, h in enumerate(left) if not recent & set(h.values())), None)
            if pick is not None:
                break
        out.append(left.pop(pick or 0))
    return out


def interleave_heat_lists(lists: Sequence[list[dict]], rng: random.Random) -> list[dict]:
    """Deal heats from several groups (e.g. dens) in turn, so groups alternate on the track."""
    lists = [lst[:] for lst in lists if lst]
    rng.shuffle(lists)
    lists.sort(key=len, reverse=True)
    out: list[dict] = []
    placed = []
    for lst in lists:
        n = len(lst)
        for i, h in enumerate(lst):
            placed.append(((i + 0.5) / n, rng.random(), h))
    placed.sort(key=lambda t: (t[0], t[1]))
    return [h for _, _, h in placed]


def estimate_round_robin_heats(n: int, lanes: int) -> int:
    if n < 2:
        return 0
    if n <= lanes:
        return lanes
    pairs = n * (n - 1) // 2
    return math.ceil(pairs / (lanes * (lanes - 1) // 2))


def build_chart(racers: Sequence[dict], lanes: Sequence[int], rounds: int = 1,
                seed: int | None = None, mix_dens: bool = True, fmt: str = "rotation",
                scope: str = "den") -> list[dict]:
    """High level: racers (dicts with id, den) -> heats ready for Database.add_heats.

    fmt="rotation"     every car runs every lane `rounds` times (lane-balanced chart)
    fmt="round_robin"  every car races every other car; scope="den" runs a round
                       robin inside each den (heats from different dens alternate on
                       the track), scope="pack" runs one round robin across the pack
    """
    rng = random.Random(seed)
    if fmt == "round_robin":
        if scope == "den":
            by_den: dict[str, list] = defaultdict(list)
            for r in racers:
                by_den[r.get("den", "")].append(r["id"])
            per_den = [[{"lanes": h, "den": d} for h in round_robin(ids, lanes, rng)]
                       for d, ids in sorted(by_den.items())]
            raw = [x["lanes"] for x in interleave_heat_lists(per_den, rng)]
        else:
            raw = round_robin([r["id"] for r in den_interleave(racers, rng)], lanes, rng)
        raw = raw * max(1, rounds) if rounds > 1 else raw
        return [{"round": 1 + i // max(1, len(raw) // max(1, rounds)), "race_in_round": i + 1,
                 "bracket": None, "group_key": None, "lanes": h} for i, h in enumerate(raw)]
    ordered = den_interleave(racers, rng) if mix_dens else rng.sample(list(racers), len(racers))
    ids = [r["id"] for r in ordered]
    raw = rotation_chart(ids, lanes, rounds, rng)
    per_round = max(1, len(raw) // rounds) if rounds else len(raw)
    return [{"round": i // per_round + 1, "race_in_round": i % per_round + 1,
             "bracket": None, "group_key": None, "lanes": heat} for i, heat in enumerate(raw)]
