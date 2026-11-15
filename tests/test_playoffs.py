"""The playoff race: what the win column alone cannot tell you.

The theme here is that "how many wins do I need" is a question about the *schedule*, not
about the table. Nine rivals cannot all win out, because they play each other, and the one
test that matters most is the one where three teams on identical records get three
different answers. `test_the_flow_math_matches_exhaustive_enumeration` is the backstop: it
plays out every outcome of small leagues by hand and insists the flow agrees.
"""

import random
from itertools import combinations

import pytest

from ffa import playoffs as P
from ffa.lineup.league import build_league_view, race

SETTINGS = {
    "playoff_teams": 6,
    "playoff_week_start": 15,
    "start_week": 1,
    "last_scored_leg": 9,
}


def league(**overrides) -> dict:
    return {"name": "A League", "roster_positions": ["QB", "RB", "WR", "TE", "K", "DEF", "BN"],
            "settings": {**SETTINGS, **overrides}}


def table(*records) -> tuple[P.Standing, ...]:
    """A table from (wins, losses) pairs, roster ids counting from 1, points descending."""
    rows = [
        P.Standing(roster_id=index, wins=wins, losses=losses, ties=0,
                   points_for=1000.0 - index)
        for index, (wins, losses) in enumerate(records, start=1)
    ]
    return tuple(sorted(rows, key=lambda s: (-s.record, -s.points_for)))


def rosters(*records) -> list[dict]:
    """Rosters from (wins, losses) pairs. The totals have to match `last_scored_leg` and
    `start_week`, or `race` refuses the table -- which is the check working, and caught
    these fixtures disagreeing with their own settings."""
    return [
        {"roster_id": index, "owner_id": f"u{index}",
         "settings": {"wins": wins, "losses": losses, "ties": 0,
                      "fpts": 1000 - index, "fpts_decimal": 50}}
        for index, (wins, losses) in enumerate(records, start=1)
    ]


def week(pairs, *, scored=False) -> list[dict]:
    """One week of matchups from a list of (roster, roster) pairs."""
    entries = []
    for matchup_id, (first, second) in enumerate(pairs, start=1):
        for roster_id in (first, second):
            if roster_id is None:
                continue
            entries.append({"roster_id": roster_id, "matchup_id": matchup_id,
                            "points": 100.0 if scored else 0.0})
    return entries


# -- the thing a win column gets wrong -------------------------------------------------


def test_the_schedule_decides_who_clinches_not_the_win_column():
    """Three teams level on record, and only one of them clinches by winning out.

    Found in a real league: three rosters at 3-5 with five games each, all reaching eight
    wins by winning out, and only the one whose remaining fixtures include the other two
    was through for certain. Beating them is what stops them matching it; the other two
    each leave a rival free to win out and take the tiebreak.

    Shrunk to four teams here. B plays both of the other chasers, C and D play one each.
    Counting fixtures instead of solving the schedule gives all three the same answer, and
    it is wrong for two of them.
    """
    standings = table((3, 0), (1, 2), (1, 2), (1, 2))  # A, then B, C, D on points
    remaining = ((2, 3), (1, 4), (2, 4), (1, 3))  # wk1: B-C, A-D.  wk2: B-D, A-C
    picture = P.build_picture(table=standings, remaining=remaining, spots=2, final_week=14)
    needs = {t.roster_id: t.wins_to_clinch for t in picture.teams}
    assert needs[2] == 2, "B wins out and is through: it has beaten both rivals doing it"
    assert needs[3] is None, "C wins out to the same record and D can still match it"
    assert needs[4] is None, "and the mirror image for D"
    # All three are alive, and all three are alive at the same number.
    assert {t.wins_to_survive for t in picture.teams if t.roster_id in (2, 3, 4)} == {1}


def test_a_rival_who_has_to_be_beaten_cannot_also_win_out():
    """The constraint in isolation: two teams needing a win from the same game.

    `_reachable` is asked for two wins out of one shared fixture, which no result can
    produce. A count of each team's own games says yes to this.
    """
    assert P._reachable({1: 1, 2: 1}, [(1, 2)]) is False
    assert P._reachable({1: 1}, [(1, 2)]) is True
    assert P._reachable({1: 1, 2: 1}, [(1, 2), (1, 3), (2, 4)]) is True
    # Three teams, three wins, but every game is between two of them: at most three wins
    # exist and they need three, which the round robin can just about supply.
    assert P._reachable({1: 1, 2: 1, 3: 1}, [(1, 2), (2, 3), (1, 3)]) is True
    assert P._reachable({1: 2, 2: 2, 3: 2}, [(1, 2), (2, 3), (1, 3)]) is False


def test_the_flow_math_matches_exhaustive_enumeration():
    """The backstop: play out every outcome of small leagues and compare.

    The flow formulation replaced no simpler thing -- there is no correct simpler thing --
    so the only way to trust it is an independent answer. This enumerates all 2^games
    results, which is only affordable because the leagues are tiny; the real one is 2^25.
    """
    rng = random.Random(11)
    for _ in range(60):
        size = rng.choice([4, 6])
        ids = list(range(1, size + 1))
        spots = rng.randint(1, size - 1)
        standings = tuple(sorted(
            (P.Standing(roster_id=i, wins=rng.randint(0, 5), losses=rng.randint(0, 5),
                        ties=rng.choice([0, 0, 1]), points_for=float(rng.randint(800, 1400)))
             for i in ids),
            key=lambda s: (-s.record, -s.points_for)))
        remaining = []
        for _week in range(rng.choice([1, 2, 3])):
            shuffled = ids[:]
            rng.shuffle(shuffled)
            remaining += [(shuffled[i], shuffled[i + 1]) for i in range(0, size, 2)]
        remaining = tuple(remaining)
        picture = P.build_picture(table=standings, remaining=remaining, spots=spots,
                                  final_week=14, trials=1)
        expected = _enumerate(standings, remaining, spots)
        for outlook in picture.teams:
            assert (outlook.wins_to_clinch, outlook.wins_to_survive) == expected[outlook.roster_id], (
                f"roster {outlook.roster_id} of {standings} over {remaining}, {spots} spots"
            )


def _enumerate(standings, remaining, spots):
    """Every outcome, by hand. No flow and no subsets: an independent oracle."""
    ids = [s.roster_id for s in standings]
    record = {s.roster_id: s.record for s in standings}
    out = {}
    for team in ids:
        own = [i for i, game in enumerate(remaining) if team in game]
        miss, make = set(), set()
        for mask in range(1 << len(remaining)):
            final = dict(record)
            for index, (first, second) in enumerate(remaining):
                final[first if (mask >> index) & 1 else second] += 1.0
            won = sum(1 for i in own if (remaining[i][0] == team) == bool((mask >> i) & 1))
            if sum(1 for t in ids if t != team and final[t] >= final[team]) >= spots:
                miss.add(won)  # level counts as ahead: the clinch assumes the tiebreak lost
            if sum(1 for t in ids if t != team and final[t] > final[team]) <= spots - 1:
                make.add(won)  # strictly ahead only: survival assumes the tiebreak won
        out[team] = (next((k for k in range(len(own) + 1) if k not in miss), None),
                     next((k for k in range(len(own) + 1) if k in make), None))
    return out


# -- which weeks are still open --------------------------------------------------------


def test_a_week_in_progress_is_not_in_the_table_yet():
    """Live Sunday: every week-10 matchup has points and nobody's record has moved."""
    assert P.undecided_weeks(SETTINGS, final_week=14) == (10, 11, 12, 13, 14)


def test_a_league_that_starts_late_has_no_week_one():
    """One `.env` league has `start_week: 2`, and its week 1 is ten paired entries of
    nothing: a fixture nobody will ever play, and a free win if it is counted."""
    settings = {**SETTINGS, "start_week": 2, "last_scored_leg": 0}
    assert P.undecided_weeks(settings, final_week=14)[0] == 2
    assert P.reconciles(settings, played=0) is True
    assert P.reconciles({**SETTINGS, "start_week": 2, "last_scored_leg": 9}, played=8) is True
    # The same boundary read without `start_week` claims a game more than has been played.
    assert P.reconciles({**SETTINGS, "last_scored_leg": 9}, played=8) is False


def test_the_regular_season_ends_the_week_before_the_bracket():
    """Playoff weeks come back from the matchups endpoint fully paired and look exactly
    like a regular-season fixture, so the cut has to be explicit."""
    assert P.regular_season_end({"playoff_week_start": 15}) == 14
    assert P.undecided_weeks(SETTINGS, final_week=14)[-1] == 14
    assert P.regular_season_end({}) == 14  # the common shape, when nothing says otherwise


def test_only_the_weeks_asked_for_become_games():
    schedule = {10: week([(1, 2), (3, 4)]), 15: week([(1, 3), (2, 4)])}
    assert sorted(map(sorted, P.games(schedule, [10]))) == [[1, 2], [3, 4]]
    assert P.games(schedule, []) == ()


def test_a_roster_alone_in_its_matchup_has_no_game():
    """An odd number of teams leaves someone unpaired, which is a bye and not an error."""
    schedule = {10: week([(1, 2), (3, None)])}
    assert sorted(map(sorted, P.games(schedule, [10]))) == [[1, 2]]


# -- the odds --------------------------------------------------------------------------


def test_odds_are_the_same_every_time_they_are_drawn():
    """A number that moves when nothing else has is a number nobody can check."""
    standings = table((5, 3), (5, 3), (4, 4), (2, 6))
    remaining = ((1, 2), (3, 4))
    first = P.build_picture(table=standings, remaining=remaining, spots=2, final_week=14)
    second = P.build_picture(table=standings, remaining=remaining, spots=2, final_week=14)
    assert [t.odds for t in first.teams] == [t.odds for t in second.teams]


def test_the_odds_add_up_to_the_number_of_places():
    """Exactly `spots` teams make it in every single trial, so the chances sum to `spots`.

    A cheap, total check on the simulation: it catches a ranking that seats the wrong
    number of teams, a tiebreak that drops one, and an off-by-one in the cut, none of which
    any individual percentage would look wrong for.
    """
    records = [(7, 2), (5, 4), (5, 4), (5, 4), (4, 5), (4, 5), (3, 6), (3, 6)]
    standings = table(*records)
    ids = [s.roster_id for s in standings]
    remaining = tuple((ids[i], ids[i + 1]) for i in range(0, 8, 2)) + \
                tuple((ids[i], ids[(i + 3) % 8]) for i in range(0, 8, 2))
    for spots in (1, 3, 4, 6):
        picture = P.build_picture(table=standings, remaining=remaining, spots=spots,
                                  final_week=14)
        assert sum(t.odds for t in picture.teams) == pytest.approx(spots, abs=1e-9)


def test_a_team_nobody_can_catch_is_certain_and_an_eliminated_one_is_not():
    standings = table((8, 0), (7, 1), (1, 7), (0, 8))
    remaining = ((1, 2), (3, 4))
    picture = P.build_picture(table=standings, remaining=remaining, spots=2, final_week=14)
    best, worst = picture.teams[0], picture.teams[-1]
    assert best.clinched and best.odds == 1.0 and best.summary == "in"
    assert worst.eliminated and worst.odds == 0.0 and worst.summary == "out"
    assert worst.wins_to_survive is None


def test_with_a_place_for_everyone_nobody_needs_a_win():
    standings = table((3, 1), (1, 3))
    picture = P.build_picture(table=standings, remaining=((1, 2),), spots=2, final_week=14)
    assert all(t.clinched and t.odds == 1.0 for t in picture.teams)


# -- the league shapes that are refused rather than guessed ----------------------------


def test_a_median_league_is_refused_by_name():
    """Scoring against the weekly median is a second result a week. The race would not be
    approximate, it would be wrong, and nothing on the card could warn about it."""
    assert "median" in (P.unsupported({"league_average_match": 1}) or "")
    assert P.unsupported({"divisions": 2}) and "division" in P.unsupported({"divisions": 2})
    assert P.unsupported(SETTINGS) is None


def test_a_refused_league_shows_the_reason_instead_of_a_table():
    view = race(league(league_average_match=1), rosters((5, 3), (3, 5)),
                {10: week([(1, 2)])})
    assert view.teams == ()
    assert view.notes and "median" in view.notes[0]


def test_a_table_that_will_not_reconcile_is_refused_too():
    """Both boundary fields are undocumented, so the one cross-check available is run."""
    view = race(league(last_scored_leg=4), rosters((5, 3), (3, 5)), {10: week([(1, 2)])})
    assert view.teams == ()
    assert view.notes and "last-scored" in view.notes[0]


# -- what the league view carries ------------------------------------------------------


def test_the_league_view_carries_the_race_only_when_the_schedule_is_fetched():
    """The race is the one thing here that needs weeks other than this one, so it is the
    one thing a caller can decline to pay for."""
    shared = dict(league=league(), rosters=rosters((5, 4), (4, 5)), users=[],
                  matchups=[], projections={}, players={}, week=10, kickoffs={},
                  now=None, my_roster_id=1)
    assert build_league_view(**shared).playoffs is None
    schedule = {w: week([(1, 2)]) for w in range(10, 15)}
    view = build_league_view(**shared, schedule=schedule)
    assert view.playoffs is not None
    assert view.playoffs.spots == 2  # six places in a two-team league is clamped to the field
    assert view.playoffs.games_left == 5
    assert view.playoffs.weeks_left == (10, 11, 12, 13, 14)


def test_every_team_in_the_league_gets_a_row():
    """The request was for every team, not for the one being advised."""
    records = [(7, 2), (5, 4), (5, 4), (5, 4), (5, 4), (4, 5), (4, 5), (4, 5), (3, 6), (3, 6)]
    ids = list(range(1, 11))
    schedule = {}
    for offset, w in enumerate(range(10, 15)):
        rotated = ids[:1] + ids[1 + offset:] + ids[1:1 + offset]
        schedule[w] = week([(rotated[i], rotated[i + 1]) for i in range(0, 10, 2)])
    view = build_league_view(
        league=league(), rosters=rosters(*records), users=[], matchups=[], projections={},
        players={}, week=10, kickoffs={}, now=None, my_roster_id=3, schedule=schedule,
    )
    picture = view.playoffs
    assert len(picture.teams) == 10
    assert [t.seed for t in picture.teams] == list(range(1, 11))
    assert picture.teams[0].standing.label == "7-2"
    assert picture.teams[-1].standing.label == "3-6"
    assert all(t.games_left == 5 for t in picture.teams)
    assert all(0.0 <= t.odds <= 1.0 for t in picture.teams)
    assert picture.teams[0].odds > picture.teams[-1].odds
    # Every team gets a number of some kind: a win total, or the reason there isn't one.
    assert all(t.summary for t in picture.teams)
    assert picture.team(3) is not None and picture.team(99) is None


def test_a_tie_is_worth_half_a_win():
    """Sleeper keeps ties in their own column, and a team that has one is not level with a
    team that has a loss instead."""
    drawn = P.Standing(roster_id=1, wins=5, losses=3, ties=1, points_for=1000.0)
    beaten = P.Standing(roster_id=2, wins=5, losses=4, ties=0, points_for=1000.0)
    assert drawn.record > beaten.record
    assert drawn.label == "5-3-1" and beaten.label == "5-4"
    assert drawn.played == 9 and beaten.played == 9


def test_points_are_read_as_whole_and_hundredths():
    """`fpts_decimal` is hundredths, so a single digit is .0x and not .x0."""
    rows = P.standings([
        {"roster_id": 1, "settings": {"wins": 5, "losses": 3, "fpts": 1214, "fpts_decimal": 74}},
        {"roster_id": 2, "settings": {"wins": 5, "losses": 3, "fpts": 1214, "fpts_decimal": 4}},
    ])
    assert rows[0].points_for == pytest.approx(1214.74)
    assert rows[1].points_for == pytest.approx(1214.04)
    assert [r.roster_id for r in rows] == [1, 2]  # ordered by points when level on record


def test_a_roster_without_an_id_is_skipped_rather_than_crashing():
    rows = P.standings([{"settings": {"wins": 1}}, {"roster_id": 2, "settings": {"wins": 2}}])
    assert [r.roster_id for r in rows] == [2]


def test_subsets_of_a_reachable_group_are_reachable():
    """The property the subset search leans on: dropping a requirement cannot make a group
    impossible, so checking groups of exactly the size needed is enough."""
    pool = [(1, 2), (2, 3), (3, 4), (4, 1)]
    need = {1: 1, 2: 1, 3: 1, 4: 1}
    assert P._reachable(need, pool) is True
    for size in (1, 2, 3):
        for group in combinations(need, size):
            assert P._reachable({t: need[t] for t in group}, pool) is True
