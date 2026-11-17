"""How much a team scores, and -- the part that matters -- how much of that is real.

The estimator exists because two real leagues looked identical and were not: one's scoring
separated its teams and predicted results, the other's was indistinguishable from nine
weeks of coin flips. Nothing in a table says which you are looking at, so the tests here
are mostly about the second one. A model that cannot report "there is nothing here" will
dress noise up as a forecast, and on this card that would be worse than the coin flip it
replaced.
"""

import random
import statistics as st

import pytest

from ffa import strength as S


def season(rng, *, spread, teams=10, weeks=9, sd=22.0, level=130.0):
    """A synthetic season where the true spread between teams is known."""
    truth = {roster: rng.gauss(level, spread) for roster in range(1, teams + 1)}
    return {week: {roster: rng.gauss(mean, sd) for roster, mean in truth.items()}
            for week in range(1, weeks + 1)}


# -- the thing the whole model rests on -------------------------------------------------


def test_a_league_of_identical_teams_is_reported_as_one():
    """Nine weeks of noise must not come back as nine weeks of evidence.

    This is the `.env` league whose teams' averages were 8.4 apart with 26.3 of week-to-week
    noise: a spread you would see from ten identical teams. Out of sample the higher scorer
    won 15 of 30. The estimator has to shrink that to nothing on its own, because nobody is
    going to look at a league and decide by hand.
    """
    rng = random.Random(5)
    estimates = [S.estimate(season(rng, spread=0.0)).reliability for _ in range(40)]
    assert st.mean(estimates) < 0.15

    flat = S.estimate(season(random.Random(11), spread=0.0, teams=10, weeks=9))
    assert flat.reliability < 0.3
    # Whatever little it keeps, every team stays within a point of the league average.
    assert max(flat.means.values()) - min(flat.means.values()) < 8.0


def test_a_league_whose_teams_really_differ_keeps_most_of_the_gap():
    rng = random.Random(5)
    estimates = [S.estimate(season(rng, spread=12.0)).reliability for _ in range(40)]
    # The truth for this shape is 144 / (144 + 484/9) = 0.73, and the estimate runs a
    # little under it, which is the right way to be wrong.
    assert 0.5 < st.mean(estimates) < 0.8


def test_shrinking_pulls_every_team_toward_the_league_and_never_past_it():
    weekly = season(random.Random(5), spread=12.0)
    scoring = S.estimate(weekly)
    raw = {roster: st.mean([weekly[week][roster] for week in weekly])
           for roster in scoring.means}
    for roster, average in raw.items():
        shrunk = scoring.means[roster]
        assert abs(shrunk - scoring.league_mean) <= abs(average - scoring.league_mean) + 1e-9
        assert (shrunk - scoring.league_mean) * (average - scoring.league_mean) >= 0


def test_too_few_weeks_claims_no_spread_at_all():
    """Three weeks cannot tell a good team from a hot one, and must not pretend to."""
    weekly = season(random.Random(5), spread=20.0, weeks=3)
    scoring = S.estimate(weekly)
    assert scoring.reliability == 0.0
    assert scoring.flat


def test_a_week_nobody_played_is_not_a_week_of_zeros():
    """One `.env` league starts in week 2 and its week 1 comes back fully paired with
    zeros. Averaged in, it drags every team down by an eighth and invents the variance to
    go with it."""
    weekly = season(random.Random(5), spread=12.0)
    phantom = {0: dict.fromkeys(weekly[1], 0.0), **weekly}
    assert S.estimate(phantom).means == S.estimate(weekly).means
    assert S.estimate(phantom).weeks == S.estimate(weekly).weeks


# -- the two channels --------------------------------------------------------------------


def test_the_level_comes_from_the_scores_and_the_projections_only_tilt_it():
    """A projected *best* lineup is worth more than anyone actually scores, because every
    manager leaves something on his bench. Centring both channels cancels a bias no data
    here could measure, so the blend stays in real points."""
    # Scores with no week-to-week movement at all, so the averages are believed outright
    # and the arithmetic is exactly checkable. The projections sit sixty points higher,
    # which is the optimal-lineup bias, and spread twice as wide.
    weekly = {week: {1: 120.0, 2: 130.0, 3: 140.0} for week in range(1, 10)}
    scoring = S.estimate(weekly, projected={1: 210.0, 2: 190.0, 3: 170.0})
    assert scoring.reliability == 1.0
    assert scoring.league_mean == pytest.approx(130.0)
    assert st.mean(scoring.means.values()) == pytest.approx(130.0), "no sixty-point bias"
    # Half of each centred edge: roster 1 is ten below on scoring and twenty above on
    # paper, so it comes out five above the league.
    assert scoring.means == pytest.approx({1: 135.0, 2: 130.0, 3: 125.0})
    # A fixture with no spread at all would otherwise hand the simulation a spread of
    # zero, which is a season with no randomness in it and a division by it in `_stratum`.
    assert scoring.sd == S.DEFAULT_WEEK_SD


def test_channels_that_disagree_exactly_cancel():
    weekly = {week: {1: 120.0, 2: 130.0, 3: 140.0} for week in range(1, 10)}
    scoring = S.estimate(weekly, projected={1: 200.0, 2: 190.0, 3: 180.0})
    assert scoring.means == pytest.approx({1: 130.0, 2: 130.0, 3: 130.0})
    assert scoring.flat, "and a league nothing can separate is called flat"


def test_with_nothing_scored_the_rosters_are_the_whole_model():
    scoring = S.estimate({}, projected={1: 140.0, 2: 120.0})
    assert scoring.projection_weight == 1.0
    assert scoring.league_mean == pytest.approx(130.0)
    assert scoring.means[1] == pytest.approx(140.0)
    assert scoring.sd == S.DEFAULT_WEEK_SD


def test_a_roster_projecting_nothing_is_not_a_roster_expected_to_score_nothing():
    """Week one, or a hand-made fixture: no projections at all. Taken at face value it
    would put the simulated points-for on a scale of nought, which is where the tiebreak
    lives."""
    assert S.estimate({}, projected={1: 0.0, 2: 0.0}) is None
    assert S.estimate({}) is None


def test_both_channels_split_the_difference():
    weekly = {week: {1: 150.0, 2: 110.0} for week in range(1, 10)}
    scoring = S.estimate(weekly, projected={1: 100.0, 2: 160.0})
    # Scores put roster 1 twenty above the league, projections put it thirty below; with
    # two teams there is no spread to estimate, so only the projections carry an edge.
    assert scoring.projection_weight == S.PROJECTION_WEIGHT
    assert scoring.means[1] < scoring.means[2]


# -- what an edge is worth ----------------------------------------------------------------


def test_a_ten_point_edge_is_not_a_ten_point_favourite():
    """Two draws with the same spread, so the gap has sd * sqrt(2) under it. Comparing two
    averages side by side suggests certainty that a fantasy week never has."""
    assert S.win_probability(140.0, 130.0, 22.0) == pytest.approx(0.627, abs=0.005)
    assert S.win_probability(130.0, 130.0, 22.0) == 0.5
    assert S.win_probability(160.0, 130.0, 22.0) == pytest.approx(0.834, abs=0.005)
    assert S.win_probability(140.0, 130.0, 0.0) == 1.0


# -- the acceptance test, as a function --------------------------------------------------


def test_the_backtest_finds_skill_where_there_is_some_and_none_where_there_is_not():
    """Replaying the season is how this feature earns its place: if the estimator cannot
    beat 0.25 on real weeks it has no business replacing a coin flip. Here it is run on
    both kinds of league, with the answer known in advance."""
    for spread, skilled in ((14.0, True), (0.0, False)):
        rng = random.Random(3)
        weekly = season(rng, spread=spread, teams=10, weeks=14)
        rosters = sorted(weekly[1])
        fixtures = {}
        for week in weekly:
            order = rosters[:]
            rng.shuffle(order)
            fixtures[week] = [(order[i], order[i + 1]) for i in range(0, len(order), 2)]
        report = S.backtest(weekly, fixtures)
        assert report.games > 30
        if skilled:
            assert report.brier < report.baseline and report.skill > 0.08
            assert report.accuracy > 0.55
        else:
            # A league of identical teams calls every game 50/50, which scores exactly the
            # baseline. Not beating the coin flip and not losing to it either.
            assert report.brier == pytest.approx(report.baseline, abs=0.02)


def test_a_backtest_needs_weeks_to_learn_from():
    weekly = {week: {1: 120.0, 2: 130.0} for week in range(1, 4)}
    assert S.backtest(weekly, {week: [(1, 2)] for week in weekly}) is None
