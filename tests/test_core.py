import random
from collections import Counter

import pytest

from gravitygate import scheduler, scoring
from gravitygate.db import Database
from gravitygate.elimination import ElimConfig, EliminationEngine, split_sizes
from gravitygate.timers.parsers import LineAssembler, PROFILES, parse_line


# -- scheduler --------------------------------------------------------------

@pytest.mark.parametrize("cars,lanes", [(2, 3), (3, 3), (7, 3), (12, 4), (36, 3), (50, 6), (5, 2)])
def test_rotation_every_car_every_lane_once(cars, lanes):
    ids = list(range(cars))
    heats = scheduler.rotation_chart(ids, list(range(1, lanes + 1)), rounds=1, rng=random.Random(1))
    stats = scheduler.chart_stats(heats)
    for rid in ids:
        assert stats["lane_counts"][rid] == {l: 1 for l in range(1, lanes + 1)}
    for h in heats:
        assert len(set(h.values())) == len(h)  # no car twice in a heat


def test_rotation_opponent_variety_and_spacing():
    heats = scheduler.rotation_chart(list(range(36)), [1, 2, 3], rng=random.Random(5))
    stats = scheduler.chart_stats(heats)
    # 3 lanes -> each car can meet at most 6 distinct opponents; the Golomb search should hit it.
    assert min(stats["distinct_opponents"].values()) == 6
    assert stats["min_gap"] >= 2  # never back-to-back


def test_two_rounds_double_the_runs():
    heats = scheduler.rotation_chart(list(range(10)), [1, 2, 3], rounds=2, rng=random.Random(2))
    stats = scheduler.chart_stats(heats)
    assert all(sum(v.values()) == 6 for v in stats["lane_counts"].values())


def test_den_interleave_mixes_dens():
    racers = [{"id": i, "den": d} for i, d in enumerate(["Wolves"] * 10 + ["Bears"] * 10 + ["Lions"] * 4)]
    out = scheduler.den_interleave(racers, random.Random(3))
    assert sorted(r["id"] for r in out) == list(range(24))
    longest = run = 1
    for a, b in zip(out, out[1:]):
        run = run + 1 if a["den"] == b["den"] else 1
        longest = max(longest, run)
    assert longest <= 3


def test_lane_mask_uses_only_good_lanes():
    heats = scheduler.rotation_chart(list(range(8)), [1, 3, 4], rng=random.Random(0))
    assert all(set(h) <= {1, 3, 4} for h in heats)


# -- scoring ----------------------------------------------------------------

def test_placements_with_tie_and_dnf():
    assert scoring.placements({1: 2.5, 2: 2.4, 3: None}) == {1: 2, 2: 1, 3: None}
    assert scoring.placements({1: 2.5, 2: 2.5, 3: 2.6}) == {1: 1, 2: 1, 3: 3}


def _res(rid, lane, t, place):
    return {"racer_id": rid, "lane": lane, "time": t, "place": place, "dnf": t is None}


def test_standings_per_den_and_overall():
    racers = {1: {"car_number": 1, "first_name": "A", "last_name": "", "den": "Wolves"},
              2: {"car_number": 2, "first_name": "B", "last_name": "", "den": "Wolves"},
              3: {"car_number": 3, "first_name": "C", "last_name": "", "den": "Bears"}}
    results = [_res(1, 1, 2.50, 1), _res(2, 2, 2.60, 2), _res(3, 3, 2.70, 3),
               _res(1, 2, 2.52, 1), _res(2, 3, 2.61, 2), _res(3, 1, None, None)]
    t = scoring.standings(results, racers, 3, "average", dnf_time=9.999)
    assert [r["racer_id"] for r in t["overall"]] == [1, 2, 3]
    assert t["dens"]["Bears"][0]["rank"] == 1
    assert t["dens"]["Wolves"][1]["rank"] == 2
    pts = scoring.standings(results, racers, 3, "points")
    assert pts["overall"][0]["points"] == 6
    awards = scoring.awards(t, per_den=1, overall=1)
    cats = Counter(a["category"] for a in awards)
    assert cats == {"Bears": 1, "Wolves": 1, "Pack Overall": 1}
    assert scoring.seed_from_dens(t, 1) == [3, 1]


# -- elimination ------------------------------------------------------------

def test_split_sizes():
    assert split_sizes(7, 3) == [3, 2, 2]
    assert split_sizes(6, 3) == [3, 3]
    assert split_sizes(4, 3) == [2, 2]
    assert split_sizes(2, 3) == [2]


def run_elimination(n_cars, lanes, cfg, rng_seed=0, speeds=None):
    """Drive the engine to completion with a deterministic 'physics'."""
    rng = random.Random(rng_seed)
    speeds = speeds or {i: rng.uniform(2.4, 3.2) for i in range(n_cars)}
    eng = EliminationEngine(list(range(n_cars)), lanes, cfg)
    heats, results = [], []
    hid = 0
    for _ in range(200):
        new = eng.next_round(heats, results)
        if not new:
            st = eng.compute(heats, results)
            assert st.complete
            return eng, st, heats, speeds
        for h in new:
            hid += 1
            heat = {"id": hid, "seq": hid, "status": "done", **h}
            heats.append(heat)
            times = {lane: speeds[rid] + 0.001 * lane for lane, rid in h["lanes"].items()}
            places = scoring.placements(times)
            for lane, rid in h["lanes"].items():
                results.append({"heat_id": hid, "lane": lane, "racer_id": rid, "time": times[lane],
                                "place": places[lane], "dnf": False})
    raise AssertionError("elimination did not finish")


@pytest.mark.parametrize("n", [2, 3, 5, 8, 13, 24, 40])
@pytest.mark.parametrize("lanes", [[1, 2], [1, 2, 3], [1, 2, 3, 4]])
def test_double_elimination_fastest_car_wins(n, lanes):
    eng, st, heats, speeds = run_elimination(n, lanes, ElimConfig(max_losses=2, seed=1))
    fastest = min(speeds, key=speeds.get)
    assert st.champion == fastest
    # Nobody is knocked out before taking two losses.
    assert all(st.losses[r] == 2 for r in range(n) if r != st.champion)
    assert st.losses[st.champion] < 2
    standings = eng.final_standings(st)
    assert standings[0]["racer_id"] == fastest
    assert len(standings) == n


def test_double_elimination_runner_up_is_second_fastest_with_deterministic_speeds():
    eng, st, heats, speeds = run_elimination(16, [1, 2], ElimConfig(max_losses=2, seed=4))
    ranked = sorted(speeds, key=speeds.get)
    assert [r["racer_id"] for r in eng.final_standings(st)[:2]] == ranked[:2]


def test_brackets_labelled_by_losses():
    eng, st, heats, _ = run_elimination(9, [1, 2, 3], ElimConfig(max_losses=2, seed=2))
    assert {h["bracket"] for h in heats} <= {"A", "B", "F"}
    assert heats[0]["bracket"] == "A"
    assert any(h["bracket"] == "B" for h in heats)


@pytest.mark.parametrize("max_losses", [1, 3])
def test_single_and_triple(max_losses):
    eng, st, heats, speeds = run_elimination(10, [1, 2, 3], ElimConfig(max_losses=max_losses, seed=3))
    assert st.champion == min(speeds, key=speeds.get)


def test_triple_on_two_lanes_terminates():
    run_elimination(3, [1, 2], ElimConfig(max_losses=3, seed=9))


def test_lane_rotated_matchups_run_each_lane():
    eng = EliminationEngine(list(range(6)), [1, 2, 3], ElimConfig(runs_per_matchup="lanes", seed=1))
    first = eng.next_round([], [])
    groups = Counter(h["group_key"] for h in first)
    assert set(groups.values()) == {3}
    for g in groups:
        runs = [h["lanes"] for h in first if h["group_key"] == g]
        for rid in runs[0].values():
            assert sorted(l for r in runs for l, x in r.items() if x == rid) == [1, 2, 3]
    eng2, st, _, speeds = run_elimination(9, [1, 2, 3], ElimConfig(runs_per_matchup="lanes", seed=1))
    assert st.champion == min(speeds, key=speeds.get)


def test_tie_on_loss_boundary_requires_rerun():
    eng = EliminationEngine([1, 2], [1, 2], ElimConfig())
    heats = [{"id": 1, "seq": 1, "round": 1, "status": "done", "group_key": "r1-A-1", "lanes": {1: 1, 2: 2}}]
    results = [{"heat_id": 1, "lane": 1, "racer_id": 1, "time": 2.5, "place": 1, "dnf": False},
               {"heat_id": 1, "lane": 2, "racer_id": 2, "time": 2.5, "place": 1, "dnf": False}]
    st = eng.compute(heats, results)
    assert st.needs_rerun == [1]
    assert eng.next_round(heats, results) == []


def test_rematch_avoidance():
    eng, st, heats, _ = run_elimination(12, [1, 2], ElimConfig(seed=7))
    early = [frozenset(h["lanes"].values()) for h in heats if h["round"] <= 3]
    assert len(early) - len(set(early)) <= 1


# -- timer parsers ----------------------------------------------------------

def test_parse_microwizard():
    r = parse_line('A=2.5060! B=2.5430" C=2.6730#', 3)
    assert r.times == {1: 2.506, 2: 2.543, 3: 2.673}
    assert r.places == {1: 1, 2: 2, 3: 3}


def test_parse_champ_style():
    r = parse_line("1=2.5060a 2=2.5430b 3=2.6730c", 3)
    assert r.times == {1: 2.506, 2: 2.543, 3: 2.673}
    assert r.places == {1: 1, 2: 2, 3: 3}


def test_parse_positional_and_bare():
    assert parse_line("1 2.5060 2 2.5430 3 2.6730", 3).times == {1: 2.506, 2: 2.543, 3: 2.673}
    assert parse_line("2.5060 2.5430 2.6730", 3).times == {1: 2.506, 2: 2.543, 3: 2.673}
    assert parse_line("Firmware v1.25", 3) is None
    assert parse_line("", 3) is None


def test_parse_dnf_zero_time():
    r = parse_line("A=2.5060! B=0.0000 C=9.9999", 3)
    assert r.times == {1: 2.506, 2: None, 3: None}


def test_assembler_one_lane_per_line():
    asm = LineAssembler(3, PROFILES["generic"])
    assert asm.feed("1=2.5060") is None
    assert asm.feed("2=2.5430") is None
    res = asm.feed("3=2.6730")
    assert res.times == {1: 2.506, 2: 2.543, 3: 2.673}


def test_assembler_respects_expected_lanes():
    asm = LineAssembler(3, PROFILES["generic"])
    res = asm.feed("1=2.5060", expected_lanes={1})
    assert res.times == {1: 2.506}


# -- database ---------------------------------------------------------------

def test_rerun_keeps_history():
    db = Database()
    a, b = db.add_racer("A", "X", "Wolves"), db.add_racer("B", "Y", "Bears")
    sid = db.create_session("t", "chart", {}, [a, b])
    (hid,) = db.add_heats(sid, [{"lanes": {1: a, 2: b}}])
    db.record_results(hid, [{"lane": 1, "racer_id": a, "time": 2.5, "place": 1},
                            {"lane": 2, "racer_id": b, "time": 2.6, "place": 2}], "timer")
    db.record_results(hid, [{"lane": 1, "racer_id": a, "time": 2.7, "place": 2},
                            {"lane": 2, "racer_id": b, "time": 2.6, "place": 1}], "timer")
    valid = db.valid_results(sid)
    assert {r["time"] for r in valid} == {2.7, 2.6}
    assert len(db.query("SELECT * FROM results")) == 4


# -- round robin qualifiers ---------------------------------------------------

import itertools


@pytest.mark.parametrize("n,lanes", [(2, 3), (3, 3), (5, 3), (6, 3), (12, 3), (30, 3), (7, 4), (9, 2), (16, 6)])
def test_round_robin_every_pair_meets(n, lanes):
    heats = scheduler.round_robin(list(range(n)), list(range(1, lanes + 1)), random.Random(4))
    met = {frozenset(p) for h in heats for p in itertools.combinations(h.values(), 2)}
    assert met == {frozenset(p) for p in itertools.combinations(range(n), 2)}
    stats = scheduler.chart_stats(heats)
    runs = [sum(v.values()) for v in stats["lane_counts"].values()]
    assert max(runs) - min(runs) <= 1 or n <= lanes  # near-equal number of races
    # Close to the theoretical minimum number of heats.
    assert len(heats) <= scheduler.estimate_round_robin_heats(n, lanes) * 1.25 + 1


def test_round_robin_small_group_runs_every_lane():
    heats = scheduler.round_robin([1, 2, 3], [1, 2, 3], random.Random(0))
    stats = scheduler.chart_stats(heats)
    assert all(v == {1: 1, 2: 1, 3: 1} for v in stats["lane_counts"].values())


def test_round_robin_per_den_keeps_dens_apart_and_alternates():
    racers = [{"id": i, "den": d} for i, d in enumerate(["Lions"] * 5 + ["Tigers"] * 6 + ["Wolves"] * 7)]
    den = {r["id"]: r["den"] for r in racers}
    heats = scheduler.build_chart(racers, [1, 2, 3], fmt="round_robin", scope="den", seed=2)
    assert all(len({den[c] for c in h["lanes"].values()}) == 1 for h in heats)
    for d in ("Lions", "Tigers", "Wolves"):
        ids = [r["id"] for r in racers if r["den"] == d]
        met = {frozenset(p) for h in heats for p in itertools.combinations(h["lanes"].values(), 2)}
        assert all(frozenset(p) in met for p in itertools.combinations(ids, 2))
    dens_in_order = [den[next(iter(h["lanes"].values()))] for h in heats]
    assert sum(a != b for a, b in zip(dens_in_order, dens_in_order[1:])) > len(heats) // 2


def test_round_robin_pack_scope_mixes_dens():
    racers = [{"id": i, "den": d} for i, d in enumerate(["Lions"] * 4 + ["Tigers"] * 4)]
    heats = scheduler.build_chart(racers, [1, 2, 3], fmt="round_robin", scope="pack", seed=1)
    met = {frozenset(p) for h in heats for p in itertools.combinations(h["lanes"].values(), 2)}
    assert len(met) == 28



def test_drop_fastest_and_slowest_keeps_middle_runs():
    rs = [_res(1, l, t, 1) for l, t in zip([1, 2, 3, 1, 2, 3], [2.40, 2.50, 2.52, 2.54, 2.56, 2.90])]
    racers = {1: {"car_number": 1, "first_name": "A", "last_name": "", "den": "Wolves"}}
    keep_all = scoring.standings(rs, racers, 3, "average")["overall"][0]
    middle = scoring.standings(rs, racers, 3, "average", drop_runs="fastest_slowest")["overall"][0]
    slowest = scoring.standings(rs, racers, 3, "average", drop_runs="slowest")["overall"][0]
    assert abs(middle["average"] - (2.50 + 2.52 + 2.54 + 2.56) / 4) < 1e-9
    assert abs(slowest["average"] - (2.40 + 2.50 + 2.52 + 2.54 + 2.56) / 5) < 1e-9
    assert keep_all["counted_runs"] == 6 and middle["counted_runs"] == 4


def test_heat_points_fastest_gets_lane_count():
    rs = [_res(1, 1, 2.5, 1), _res(2, 2, 2.6, 2), _res(3, 3, 2.7, 3)]
    racers = {i: {"car_number": i, "first_name": str(i), "last_name": "", "den": "D"} for i in (1, 2, 3)}
    t = scoring.standings(rs, racers, 3, "points")
    assert [(r["racer_id"], r["points"]) for r in t["overall"]] == [(1, 3), (2, 2), (3, 1)]


def test_runs_per_lane_rotation_six_races_each():
    heats = scheduler.build_chart([{"id": i, "den": "D"} for i in range(9)], [1, 2, 3], rounds=2, seed=1)
    stats = scheduler.chart_stats([h["lanes"] for h in heats])
    assert all(v == {1: 2, 2: 2, 3: 2} for v in stats["lane_counts"].values())
