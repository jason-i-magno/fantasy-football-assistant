"""The playoff race: what the win column alone cannot tell you.

The theme here is that "how many wins do I need" is a question about the *schedule*, not
about the table. Nine rivals cannot all win out, because they play each other, and the one
test that matters most is the one where three teams on identical records get three
different answers. `test_the_flow_math_matches_exhaustive_enumeration` is the backstop: it
plays out every outcome of small leagues by hand and insists the flow agrees.
"""

import math
import random
from itertools import combinations

import pytest

from ffa import playoffs as P
from ffa import strength as S
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


# -- the odds for each win total -------------------------------------------------------


def test_each_win_total_gets_a_row_with_the_record_it_finishes_on():
    standings = table((5, 3), (4, 4), (4, 4), (3, 5))
    remaining = ((1, 2), (3, 4), (1, 3), (2, 4))
    picture = P.build_picture(table=standings, remaining=remaining, spots=2, final_week=14)
    for outlook in picture.teams:
        rows = outlook.by_wins
        assert [r.wins for r in rows] == list(range(outlook.games_left + 1))
        assert [r.label for r in rows] == [
            str(outlook.standing.wins + k) for k in range(outlook.games_left + 1)
        ], "the column a manager counts in is the final one, not the gain"


def test_the_row_turns_certain_at_the_clinch_number():
    """The one invariant tying the two figures together.

    The by-wins row and the clinch number are different instruments -- bisected flow on
    one side, counted coin flips on the other -- and the row is where they are drawn side
    by side. So the first total the row calls certain has to be the clinch number itself,
    and nothing below the survival number may carry a chance at all, by construction
    rather than by luck.
    """
    records = [(7, 2), (5, 4), (5, 4), (5, 4), (4, 5), (4, 5), (3, 6), (0, 9)]
    standings = table(*records)
    ids = [s.roster_id for s in standings]
    remaining = tuple((ids[i], ids[i + 1]) for i in range(0, 8, 2)) + \
                tuple((ids[i], ids[(i + 3) % 8]) for i in range(0, 8, 2)) + \
                tuple((ids[i], ids[(i + 5) % 8]) for i in range(0, 8, 2))
    picture = P.build_picture(table=standings, remaining=remaining, spots=4, final_week=14)
    for outlook in picture.teams:
        certain = [r.wins for r in outlook.by_wins if r.certain]
        possible = [r.wins for r in outlook.by_wins if not r.impossible]
        assert (certain[0] if certain else None) == outlook.wins_to_clinch
        assert (possible[0] if possible else None) == outlook.wins_to_survive
        assert all(r.odds == 1.0 and r.trials == 0 for r in outlook.by_wins if r.certain)
        assert all(r.odds == 0.0 and r.trials == 0 for r in outlook.by_wins if r.impossible)


def test_a_certain_total_is_not_the_same_claim_as_one_sampled_at_a_hundred():
    """A total the coin flips never saw miss still is not a guarantee.

    One place short of a clinch, the simulation breaks the tie on points scored so far and
    the leader wins it every time, so the total samples at a flat 100% while the
    arithmetic -- which assumes the tiebreak lost, because those points are not yet
    scored -- says it can still miss. Both numbers are right about different questions,
    which is why the renderers mark certainty instead of reading it off the percentage.
    """
    standings = table((5, 1), (4, 2), (4, 2), (1, 5))  # roster 1 leads on points too
    picture = P.build_picture(table=standings, remaining=((2, 3), (1, 4)), spots=1,
                              final_week=14)
    leader = picture.team(1)
    assert leader.wins_to_clinch == 1, "level on 5 wins, the tiebreak is assumed lost"
    losing = leader.by_wins[0]
    assert losing.odds == 1.0 and losing.trials > 0 and not losing.certain
    assert leader.by_wins[1].certain and leader.by_wins[1].trials == 0


def test_the_odds_for_each_win_total_match_exhaustive_enumeration():
    """The oracle again, for the conditional: every outcome, split by the team's own wins.

    The sampler's answer is `made / played` inside one bucket, so this is the check that
    the bucket is the right bucket -- that the win count pulled out of the draw by bit
    arithmetic is the same count the outcome actually had.
    """
    rng = random.Random(4)
    for _ in range(18):
        size = rng.choice([4, 6])
        standings = table(*[(rng.randint(0, 3), rng.randint(0, 3)) for _ in range(size)])
        ids = [s.roster_id for s in standings]
        remaining = []
        for _ in range(rng.randint(2, 3)):
            order = ids[:]
            rng.shuffle(order)
            remaining += [(order[i], order[i + 1]) for i in range(0, size, 2)]
        remaining = tuple(remaining)
        spots = rng.randint(1, size - 1)
        picture = P.build_picture(table=standings, remaining=remaining, spots=spots,
                                 final_week=14, trials=12_000)
        exact = _enumerate_by_wins(standings, remaining, spots)
        for outlook in picture.teams:
            for row in outlook.by_wins:
                if row.certain or row.impossible or row.odds is None:
                    continue
                want = exact[outlook.roster_id][row.wins]
                # Four standard errors of the bucket's own size: the thin buckets at the
                # ends of a row are sampled a hundredth as often as the middle ones.
                slack = 4 * (want * (1 - want) / row.trials) ** 0.5 + 0.01
                assert abs(row.odds - want) <= slack, (
                    f"roster {outlook.roster_id} at +{row.wins}: {row.odds} vs {want} "
                    f"over {remaining}, {spots} spots"
                )


def _enumerate_by_wins(standings, remaining, spots):
    """P(make | exactly k own wins), by hand, with the simulation's own tiebreak."""
    ids = [s.roster_id for s in standings]
    record = {s.roster_id: s.record for s in standings}
    points = {s.roster_id: s.points_for for s in standings}
    out = {}
    for team in ids:
        own = [i for i, game in enumerate(remaining) if team in game]
        made = [0] * (len(own) + 1)
        seen = [0] * (len(own) + 1)
        for mask in range(1 << len(remaining)):
            final = dict(record)
            for index, (first, second) in enumerate(remaining):
                final[first if (mask >> index) & 1 else second] += 1.0
            won = sum(1 for i in own if (remaining[i][0] == team) == bool((mask >> i) & 1))
            order = sorted(ids, key=lambda t: (-final[t], -points[t]))
            seen[won] += 1
            if team in order[:spots]:
                made[won] += 1
        out[team] = [m / s for m, s in zip(made, seen)]
    return out


def test_more_wins_never_lower_the_odds():
    """Winning one more of your own games raises your record and denies a rival one, so it
    cannot cost you a place. The conditional is therefore monotone, and a row that dips on
    screen is a bug however small the dip. Both the leagues and the sampler are seeded, so
    this either holds or it does not."""
    rng = random.Random(11)
    for case in range(15):
        size = rng.choice([8, 10, 12])
        standings = table(*[(rng.randint(0, 8), 0) for _ in range(size)])
        ids = [s.roster_id for s in standings]
        remaining = []
        for _ in range(rng.randint(3, 6)):
            order = ids[:]
            rng.shuffle(order)
            remaining += [(order[i], order[i + 1]) for i in range(0, size, 2)]
        # Fewer trials than the real figure uses: thinner buckets are noisier, and noise
        # is the only thing that can break a monotone quantity.
        picture = P.build_picture(table=standings, remaining=tuple(remaining),
                                  spots=size // 2, final_week=14, trials=4_000)
        for outlook in picture.teams:
            drawn = [r.odds for r in outlook.by_wins if r.odds is not None]
            assert drawn == sorted(drawn), f"roster {outlook.roster_id}: {drawn}"

        # Again with scores rather than coin flips, where a topped-up cell and its
        # neighbour are no longer even the same sample. On half the leagues: the scored
        # path costs three times the coin-flip one and this is a sweep, not a proof.
        if case % 2:
            continue
        means = {s.roster_id: 110.0 + rng.uniform(0, 40) for s in standings}
        scored = P.build_picture(table=standings, remaining=tuple(remaining),
                                 spots=size // 2, final_week=14, trials=3_000,
                                 scoring=scoring(means))
        for outlook in scored.teams:
            drawn = [r.odds for r in outlook.by_wins if r.odds is not None]
            assert drawn == sorted(drawn), f"roster {outlook.roster_id}: {drawn}"


def test_a_total_too_rare_to_have_been_sampled_is_left_blank():
    """With fourteen games left, losing out is rarer than one trial in ten thousand. A
    bucket nobody drew is a blank, not a zero and not the overall figure."""
    standings = table((3, 2), (3, 2), (3, 2), (2, 3), (2, 3), (2, 3))
    remaining = ((1, 2), (3, 4), (5, 6), (1, 3), (2, 5), (4, 6), (1, 4), (2, 6), (3, 5))
    picture = P.build_picture(table=standings, remaining=remaining, spots=3,
                              final_week=14, trials=3)
    blanks = [r for t in picture.teams for r in t.by_wins if r.odds is None]
    assert blanks, "three trials cannot have covered every win total"
    assert all(r.trials == 0 and not r.certain and not r.impossible for r in blanks)


def test_the_buckets_are_the_same_sample_as_the_headline_odds():
    """One simulation, read two ways. The buckets partition the very trials the headline
    percentage counts, which is what stops the breakdown and the summary disagreeing about
    a league: every trial lands in exactly one bucket, and the ones that made the playoffs
    land in the same bucket they were counted in."""
    standings = table((5, 3), (4, 4), (4, 4), (3, 5), (3, 5), (2, 6))
    remaining = ((1, 2), (3, 4), (5, 6), (1, 3), (2, 5), (4, 6))
    record = {s.roster_id: s.record for s in standings}
    points = {s.roster_id: s.points_for for s in standings}
    chances, buckets, _ = P._odds(record, points, remaining, 3, 1_000)
    for team, rows in buckets.items():
        assert sum(played for _, played in rows) == 1_000
        assert sum(made for made, _ in rows) == pytest.approx(chances[team] * 1_000)


# -- when the odds stop being coin flips ------------------------------------------------


def scoring(means, sd=22.0, reliability=0.7, weight=0.0):
    """A `Scoring` built by hand, so these tests never touch the estimator."""
    return S.Scoring(
        teams=tuple(S.Strength(roster_id=roster, mean=mean, sd=sd, scored=mean,
                               projected=None, weeks=8) for roster, mean in means.items()),
        reliability=reliability, league_mean=sum(means.values()) / len(means),
        weeks=8, sd=sd, projection_weight=weight,
    )


def even_league(weeks=3, size=6):
    """Six teams, three spots, every team playing every week, and nobody level by luck."""
    standings = table(*[(3, 3)] * size)
    ids = [s.roster_id for s in standings]
    remaining = []
    for turn in range(weeks):
        order = ids[turn:] + ids[:turn]
        remaining += [(order[i], order[i + 1]) for i in range(0, size, 2)]
    return standings, tuple(remaining)


def test_a_team_that_scores_more_is_given_better_odds():
    standings, remaining = even_league()
    ids = [s.roster_id for s in standings]
    strong, weak = ids[0], ids[1]
    means = dict.fromkeys(ids, 125.0) | {strong: 150.0, weak: 100.0}
    picture = P.build_picture(table=standings, remaining=remaining, spots=3,
                              final_week=14, scoring=scoring(means))
    assert picture.team(strong).odds > picture.team(weak).odds + 0.3
    # And the record alone would have said nothing: they are on the same one.
    assert picture.team(strong).standing.record == picture.team(weak).standing.record


def test_equal_scoring_is_the_coin_flip_it_replaced():
    """The degenerate case, and the one that says the draw is not quietly tilted.

    Two draws from the same distribution give the better team nothing, so a league of
    identical teams has to produce the same fair coin as the bit draw it replaced. Checked
    on how often each team wins exactly k of its own games, which is the binomial either
    way -- and checked there rather than on the odds, because the odds also carry the
    tiebreak, which the scored path deliberately no longer freezes.
    """
    standings, remaining = even_league(weeks=5, size=6)
    record = {s.roster_id: s.record for s in standings}
    points = {s.roster_id: s.points_for for s in standings}
    order = sorted(record)
    index = {roster: slot for slot, roster in enumerate(order)}
    pairs = [(index[a], index[b]) for a, b in remaining]
    trials = 40_000
    _, _, seen, _ = P._score_trials([record[r] for r in order], [points[r] for r in order],
                                 pairs, 3, trials, random.Random(P.ODDS_SEED),
                                 [130.0] * len(order), 22.0)
    for team, buckets in enumerate(seen):
        games = len(buckets) - 1
        for wins, drawn in enumerate(buckets):
            expected = math.comb(games, wins) / 2 ** games
            assert drawn / trials == pytest.approx(expected, abs=0.02), (team, wins)


def test_the_tiebreak_stops_being_frozen_at_todays_points():
    """Two teams level on record, one 30 points ahead on points-for with nine games still
    to be scored. Frozen, that 30 decides every tie in the simulation and nothing can
    change it; once the simulation scores the remaining weeks the gap is mostly noise, and
    two teams this alike should come out alike.
    """
    standings, remaining = even_league()
    ids = [s.roster_id for s in standings]
    ahead, behind = ids[0], ids[1]
    frozen = P.build_picture(table=standings, remaining=remaining, spots=3, final_week=14)
    live = P.build_picture(table=standings, remaining=remaining, spots=3, final_week=14,
                           scoring=scoring(dict.fromkeys(ids, 125.0)))
    gap = abs(frozen.team(ahead).odds - frozen.team(behind).odds)
    closed = abs(live.team(ahead).odds - live.team(behind).odds)
    assert gap > 0.05, "the frozen tiebreak separates them on points nobody has scored yet"
    assert closed < gap / 2


def test_the_odds_still_add_up_to_the_number_of_places_when_scores_are_drawn():
    standings, remaining = even_league()
    means = {s.roster_id: 110.0 + 8 * s.roster_id for s in standings}
    for spots in (1, 2, 4):
        picture = P.build_picture(table=standings, remaining=remaining, spots=spots,
                                  final_week=14, scoring=scoring(means))
        assert sum(t.odds for t in picture.teams) == pytest.approx(spots, abs=1e-9)


def test_a_thin_cell_is_drawn_again_rather_than_left_thin():
    """A good team loses out about thirty times in ten thousand seasons, and a percentage
    off thirty trials has no business being drawn to the whole number."""
    standings, remaining = even_league(weeks=5, size=6)
    ids = [s.roster_id for s in standings]
    means = dict.fromkeys(ids, 120.0) | {ids[0]: 170.0}
    picture = P.build_picture(table=standings, remaining=remaining, spots=3,
                              final_week=14, scoring=scoring(means))
    sampled = [row for team in picture.teams for row in team.by_wins
               if not row.certain and not row.impossible]
    assert sampled, "this league has cells the arithmetic cannot settle"
    assert all(row.trials >= 400 for row in sampled), "400 being MIN_BUCKET_TRIALS"

    # And the claim that makes the floor worth having: the plain sample does not reach it.
    # The strongest team here loses all five of its games in well under a hundred of ten
    # thousand seasons, which is the cell a manager goes looking for.
    record = {s.roster_id: s.record for s in standings}
    points = {s.roster_id: s.points_for for s in standings}
    order = sorted(record)
    index = {roster: slot for slot, roster in enumerate(order)}
    _, _, seen, _ = P._score_trials(
        [record[r] for r in order], [points[r] for r in order],
        [(index[a], index[b]) for a, b in remaining], 3, P.ODDS_TRIALS,
        random.Random(P.ODDS_SEED), [means[r] for r in order], 22.0)
    assert seen[index[ids[0]]][0] < 100


def test_a_cell_drawn_again_agrees_with_the_one_it_replaces():
    """The top-up is a second estimator of the same number, so where the first one has
    enough trials to be trusted the two have to agree. This is what says the conditional
    draw and the forced scores have not quietly biased the thing they sample."""
    standings, remaining = even_league(weeks=4, size=6)
    ids = [s.roster_id for s in standings]
    means = {roster: 110.0 + 7 * index for index, roster in enumerate(ids)}
    record = {s.roster_id: s.record for s in standings}
    points = {s.roster_id: s.points_for for s in standings}
    order = sorted(record)
    index = {roster: slot for slot, roster in enumerate(order)}
    base = [record[r] for r in order]
    start = [points[r] for r in order]
    pairs = [(index[a], index[b]) for a, b in remaining]
    mu = [means[r] for r in order]
    rng = random.Random(3)
    _, hits, seen, _ = P._score_trials(base, start, pairs, 3, 40_000, rng, mu, 22.0)
    checked = 0
    for team in range(len(order)):
        for wins in range(len(seen[team])):
            if seen[team][wins] < 4_000:
                continue
            plain = hits[team][wins] / seen[team][wins]
            drawn, made = P._stratum(team, wins, 4_000, base, start, pairs, 3, rng, mu, 22.0)
            spread = (max(plain * (1 - plain), 1e-4) * (1 / drawn + 1 / seen[team][wins])) ** 0.5
            assert abs(plain - made / drawn) < 4 * spread + 0.01, (team, wins)
            checked += 1
    assert checked >= 8


def test_the_conditional_draw_picks_the_games_it_was_favoured_in():
    """Exactly k wins every time, and the games chosen in proportion to how likely each
    was -- a team that wins two of three has most likely won the two it was favoured in,
    not a uniform pair. Compared against the weights worked out by hand."""
    chances = [0.8, 0.5, 0.2]
    table_ = P._conditional(chances)
    rng = random.Random(9)
    for wins in (0, 1, 2, 3):
        counts = {}
        for _ in range(20_000):
            drawn = tuple(P._draw_wins(chances, table_, wins, rng))
            assert sum(drawn) == wins
            counts[drawn] = counts.get(drawn, 0) + 1
        exact = {}
        for mask in range(8):
            pattern = tuple(bool(mask >> i & 1) for i in range(3))
            if sum(pattern) != wins:
                continue
            weight = 1.0
            for chance, won in zip(chances, pattern):
                weight *= chance if won else (1 - chance)
            exact[pattern] = weight
        total = sum(exact.values())
        for pattern, weight in exact.items():
            assert counts.get(pattern, 0) / 20_000 == pytest.approx(weight / total, abs=0.02)


def test_a_forced_result_is_scored_like_one_rather_than_bolted_on():
    """The team made to win has to win on the scoreboard too, and win the way it would
    have anyway -- narrowly, when it was the underdog. Checked against rejection sampling,
    which is the obvious correct method and far too slow for a one-in-three-hundred total.
    """
    import statistics as stats
    mine, theirs, sd = 120.0, 140.0, 22.0
    spread = sd * 2 ** 0.5
    gap = stats.NormalDist(mine - theirs, spread)
    cut = gap.cdf(0.0)
    rng = random.Random(4)
    forced = [P._split_scores(gap, cut, mine + theirs, spread, True, rng) for _ in range(20_000)]
    assert all(ours > rival for ours, rival in forced), "the forced winner always wins"

    keep = []
    while len(keep) < 20_000:
        ours, rival = rng.gauss(mine, sd), rng.gauss(theirs, sd)
        if ours > rival:
            keep.append((ours, rival))
    for slot in (0, 1):
        assert (stats.mean(side[slot] for side in forced)
                == pytest.approx(stats.mean(side[slot] for side in keep), abs=1.0))
        assert (stats.pstdev(side[slot] for side in forced)
                == pytest.approx(stats.pstdev(side[slot] for side in keep), rel=0.1))


def test_the_weeks_already_scored_are_the_other_half_of_the_boundary():
    assert P.scored_weeks(SETTINGS) == (1, 2, 3, 4, 5, 6, 7, 8, 9)
    assert P.scored_weeks(league(start_week=2)["settings"]) == (2, 3, 4, 5, 6, 7, 8, 9)
    assert P.scored_weeks(SETTINGS, final_week=5) == (1, 2, 3, 4, 5)
    assert P.scored_weeks({"start_week": 1}) == ()
    # No week belongs to both: one in progress is scored but not recorded, and is in
    # neither, which is the whole reason both read the same two fields.
    assert not set(P.scored_weeks(SETTINGS)) & set(P.undecided_weeks(SETTINGS, final_week=14))


# -- which other games to watch ---------------------------------------------------------


def test_the_swing_of_each_fixture_matches_exhaustive_enumeration():
    """The oracle again, for the third reading of the trials.

    Every outcome of a small league, counted both ways round each watched fixture. What is
    being checked is not the arithmetic of a difference but the bucketing: that the trials
    where a side won are the trials being averaged, for the right side of the right game.
    """
    standings = table((5, 3), (4, 4), (4, 4), (3, 5))
    remaining = ((1, 2), (3, 4), (1, 3), (2, 4))
    for focus in (1, 2, 3, 4):
        picture = P.build_picture(table=standings, remaining=remaining, spots=2,
                                  final_week=14, trials=20_000, focus=focus,
                                  spotlight=len(remaining))
        exact = _enumerate_swings(standings, remaining, 2, focus)
        assert len(picture.swings) == len(remaining)
        for swing in picture.swings:
            want_first, want_second = exact[(swing.first, swing.second)]
            assert swing.if_first == pytest.approx(want_first, abs=0.02), (focus, swing)
            assert swing.if_second == pytest.approx(want_second, abs=0.02), (focus, swing)


def _enumerate_swings(standings, remaining, spots, focus):
    """P(focus makes it | each side wins), per fixture, by playing out every outcome."""
    ids = [s.roster_id for s in standings]
    record = {s.roster_id: s.record for s in standings}
    points = {s.roster_id: s.points_for for s in standings}
    tally = {game: [[0, 0], [0, 0]] for game in remaining}  # [made, seen] per side
    for mask in range(1 << len(remaining)):
        final = dict(record)
        for index, (first, second) in enumerate(remaining):
            final[first if (mask >> index) & 1 else second] += 1.0
        order = sorted(ids, key=lambda t: (-final[t], -points[t]))
        made = focus in order[:spots]
        for index, game in enumerate(remaining):
            side = (mask >> index) & 1
            tally[game][side][1] += 1
            if made:
                tally[game][side][0] += 1
    return {game: (sides[1][0] / sides[1][1], sides[0][0] / sides[0][1])
            for game, sides in tally.items()}


def test_the_two_sides_average_back_to_the_headline():
    """One sample read three ways, so they have to agree: the odds given each result,
    weighted by how often that result came up, are the odds."""
    standings = table((5, 3), (4, 4), (4, 4), (3, 5), (3, 5), (2, 6))
    remaining = ((1, 2), (3, 4), (5, 6), (1, 3), (2, 5), (4, 6))
    record = {s.roster_id: s.record for s in standings}
    points = {s.roster_id: s.points_for for s in standings}
    chances, _, swung = P._odds(record, points, remaining, 3, 4_000,
                                focus=2, spotlight=range(len(remaining)))
    for (made, seen) in swung:
        total = seen[0] + seen[1]
        blended = (made[0] + made[1]) / total
        assert blended == pytest.approx(chances[2], abs=1e-9)
        assert total == 4_000


@pytest.mark.parametrize("model", ["coin flips", "scoring"])
def test_your_own_game_is_the_one_that_moves_your_odds_most(model):
    """Not a law -- a pivotal rival fixture can beat it -- but it is true here, and the
    sign is: winning your own game cannot be worth less than losing it.

    Run against both simulations on purpose. The swing counters live in each trial loop
    separately, and a real build uses the scored one, so a test that only ever flips coins
    leaves the shipped path unexercised -- which it did, until a mutation of the scored
    loop's fixture offset went unnoticed.
    """
    standings, remaining = even_league(weeks=3, size=6)
    ids = [s.roster_id for s in standings]
    means = {roster: 110.0 + 6 * index for index, roster in enumerate(ids)}
    picture = P.build_picture(
        table=standings, remaining=remaining, spots=3, final_week=14,
        focus=ids[0], spotlight=len(remaining),
        scoring=scoring(means) if model == "scoring" else None,
    )
    mine = [s for s in picture.swings if ids[0] in (s.first, s.second)]
    assert mine, "the focus roster plays in this week"
    for swing in mine:
        winning = swing.if_first if swing.first == ids[0] else swing.if_second
        losing = swing.if_second if swing.first == ids[0] else swing.if_first
        assert winning > losing
    assert max(s.swing for s in mine) == max(s.swing for s in picture.swings)


def test_only_the_fixtures_asked_about_come_back():
    standings, remaining = even_league(weeks=3, size=6)
    picture = P.build_picture(table=standings, remaining=remaining, spots=3,
                              final_week=14, focus=standings[0].roster_id,
                              spotlight=3)
    assert {(s.first, s.second) for s in picture.swings} == set(remaining[:3])
    assert [s.swing for s in picture.swings] == sorted(
        (s.swing for s in picture.swings), reverse=True), "biggest mover first"


def test_nobody_to_focus_on_is_nothing_to_watch():
    standings, remaining = even_league(weeks=3, size=6)
    bare = P.build_picture(table=standings, remaining=remaining, spots=3, final_week=14)
    assert bare.swings == () and bare.focus is None
    stranger = P.build_picture(table=standings, remaining=remaining, spots=3,
                               final_week=14, focus=999, spotlight=len(remaining))
    assert stranger.swings == (), "a roster that is not in this league watches nothing"


def test_a_team_already_through_has_nothing_riding_on_sunday():
    standings = table((8, 0), (7, 1), (1, 7), (0, 8))
    remaining = ((1, 2), (3, 4))
    picture = P.build_picture(table=standings, remaining=remaining, spots=2,
                              final_week=14, focus=1, spotlight=len(remaining))
    assert picture.team(1).clinched
    assert all(s.swing == 0.0 for s in picture.swings)


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
