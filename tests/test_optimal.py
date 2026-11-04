"""The lineup solver, checked against an independent brute force.

The solver is the one component where "nearly right" is indistinguishable from right until
it quietly costs points: every number the tool reports is a difference between two solved
lineups. An earlier version decomposed the problem by position counts, which is exact only
when no player has two positions; it lost up to 12 points on 66 of 400 random rosters. The
cross-check below is what caught that, so it stays.
"""

import random
from functools import lru_cache

import pytest

from ffa.lineup.optimal import best_lineup
from ffa.roster import eligible_positions

STANDARD = ("QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "FLEX", "K", "DEF")
LAYOUTS = [
    STANDARD,
    ("QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "K", "DEF"),
    ("QB", "QB", "RB", "RB", "WR", "WR", "TE", "SUPER_FLEX", "FLEX", "K", "DEF"),
    ("QB", "RB", "WR", "WR", "REC_FLEX", "WRRB_FLEX", "TE", "K", "DEF"),
]


def brute_force(slots, points, positions, pinned=None, unavailable=()):
    """Exact maximum-weight assignment by bitmask search over players.

    Deliberately a different algorithm from the one under test: it enumerates rather than
    solving, so the two can only agree by both being right.
    """
    pinned = dict(pinned or {})
    ids = sorted(set(points) - set(unavailable) - set(pinned.values()))
    open_slots = [i for i in range(len(slots)) if i not in pinned]
    base = sum(points.get(p, 0.0) for p in pinned.values())

    @lru_cache(maxsize=None)
    def best(index, used):
        if index == len(open_slots):
            return 0.0
        allowed = eligible_positions(slots[open_slots[index]])
        top = best(index + 1, used)  # leaving a slot empty is always legal
        for bit, player in enumerate(ids):
            if used >> bit & 1 or not (allowed & positions.get(player, frozenset())):
                continue
            top = max(top, points[player] + best(index + 1, used | 1 << bit))
        return top

    total = base + best(0, 0)
    best.cache_clear()
    return total


def test_flex_goes_to_the_best_player_not_the_obvious_one():
    """The trap a greedy fill walks into: it spends a FLEX before seeing the tight end."""
    points = {"qb": 20, "rb1": 18, "rb2": 14, "rb3": 9, "wr1": 17, "wr2": 13, "wr3": 8,
              "te1": 16, "te2": 11, "k": 9, "def": 8}
    positions = {p: frozenset({"".join(c for c in p if c.isalpha()).upper()}) for p in points}
    lineup = best_lineup(STANDARD, points, positions)
    flex = {seat.player_id for seat in lineup.seats if seat.slot == "FLEX"}
    assert flex == {"te2", "rb3"}  # not wr3, who scores less
    assert lineup.total == 135


def test_a_dual_eligible_player_fills_only_one_slot():
    """He appears in two positions' rankings; counting him in both inflates the lineup."""
    points = {"rb1": 20, "rb2": 10, "wr1": 18, "swing": 15, "te1": 5, "qb": 5, "k": 1, "def": 1}
    positions = {"rb1": frozenset({"RB"}), "rb2": frozenset({"RB"}), "wr1": frozenset({"WR"}),
                 "swing": frozenset({"RB", "WR"}), "te1": frozenset({"TE"}),
                 "qb": frozenset({"QB"}), "k": frozenset({"K"}), "def": frozenset({"DEF"})}
    lineup = best_lineup(STANDARD, points, positions)
    seated = [s.player_id for s in lineup.seats if s.player_id == "swing"]
    assert len(seated) == 1
    assert lineup.total == pytest.approx(brute_force(STANDARD, points, positions))


def test_a_locked_starter_keeps_his_slot_and_a_locked_bench_player_stays_out():
    """Sleeper freezes a player at his kickoff; advice that ignores that is unusable."""
    points = {"rb1": 5, "rb2": 20, "rb3": 19, "wr1": 10, "wr2": 9, "qb": 10,
              "te1": 4, "k": 1, "def": 1}
    positions = {p: frozenset({"".join(c for c in p if c.isalpha()).upper()}) for p in points}
    lineup = best_lineup(STANDARD, points, positions, pinned={1: "rb1"}, unavailable=["rb3"])
    assert lineup.seats[1].player_id == "rb1" and lineup.seats[1].locked
    assert "rb3" not in lineup.started


def test_a_slot_with_nobody_eligible_is_left_empty():
    points = {"qb": 10}
    lineup = best_lineup(("QB", "TE"), points, {"qb": frozenset({"QB"})})
    assert lineup.seats[0].player_id == "qb"
    assert lineup.seats[1].empty and lineup.total == 10


def test_matches_brute_force_across_random_rosters():
    """Random rosters with heavy dual eligibility, locks and byes, over four slot layouts.

    Roster sizes stay modest because the reference is exponential in them; the bug this
    guards against showed up at any size, and 300 rosters over four layouts is a wide net
    that still leaves the whole suite under two seconds.
    """
    rng = random.Random(20261103)
    for _ in range(300):
        slots = rng.choice(LAYOUTS)
        points, positions = {}, {}
        for index in range(rng.randint(9, 14)):
            player = f"p{index}"
            base = rng.choice(["QB", "RB", "WR", "TE", "K", "DEF"])
            extra = {rng.choice(["RB", "WR", "TE"])} if rng.random() < 0.4 else set()
            points[player] = round(rng.uniform(0, 28), 2)
            positions[player] = frozenset({base} | extra)
        unavailable = {p for p in points if rng.random() < 0.12}
        pinned = {}
        if rng.random() < 0.4:
            index = rng.randrange(len(slots))
            seatable = [p for p in points
                        if p not in unavailable and eligible_positions(slots[index]) & positions[p]]
            if seatable:
                pinned[index] = rng.choice(seatable)
        mine = best_lineup(slots, points, positions, pinned=pinned, unavailable=unavailable)
        # approx, not equality: the two algorithms sum the same seats in a different order.
        assert mine.total == pytest.approx(
            brute_force(slots, points, positions, pinned, unavailable)
        )


def test_the_lineup_is_stable_between_identical_solves():
    """Two equal players must not swap places every poll and read as a change."""
    points = {"a": 10.0, "b": 10.0, "qb": 1, "k": 1, "def": 1}
    positions = {"a": frozenset({"RB"}), "b": frozenset({"RB"}), "qb": frozenset({"QB"}),
                 "k": frozenset({"K"}), "def": frozenset({"DEF"})}
    first = best_lineup(STANDARD, points, positions)
    assert [s.player_id for s in first.seats] == [s.player_id for s in
                                                  best_lineup(STANDARD, points, positions).seats]
