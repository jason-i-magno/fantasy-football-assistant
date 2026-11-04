"""The scorer, which every projection depends on.

The real check is not in here: it is that `score_stats` reproduced the `.env` league's
official `players_points` for all 625 player-weeks across four completed weeks, kickers and
defenses included. These tests pin the behaviour that check relies on, using the same shapes
Sleeper sends but with made-up numbers.
"""

from ffa.scoring import DST_DUPLICATE_STATS, as_number, clean_projection, score_stats

PPR = {
    "rec": 1.0, "rec_yd": 0.1, "rec_td": 6.0,
    "rush_yd": 0.1, "rush_td": 6.0,
    "pass_yd": 0.04, "pass_td": 4.0, "pass_int": -1.0,
    "fum_lost": -2.0, "fum": 0.0,
}


def test_scores_a_line_against_the_league_settings():
    stats = {"rec": 5, "rec_yd": 60.5, "rec_td": 1, "fum_lost": 1}
    assert round(score_stats(stats, PPR), 3) == round(5 + 6.05 + 6 - 2, 3)


def test_a_player_who_did_not_play_scores_zero():
    """Sleeper simply omits the row, and the official score for that player is 0.0."""
    assert score_stats(None, PPR) == 0.0
    assert score_stats({}, PPR) == 0.0


def test_only_scored_keys_contribute():
    """Projection rows carry descriptive stats no league scores; they must not leak in."""
    stats = {"rec": 2, "rec_tgt": 9, "cmp_pct": 61.2, "bonus_rec_wr": 4.0}
    assert score_stats(stats, PPR) == 2.0


def test_bookkeeping_stats_never_score_even_if_a_league_names_them():
    """`gp` is 1.0 on every weekly row; scoring it would add a point to everyone."""
    assert score_stats({"gp": 1.0, "rec": 1}, {**PPR, "gp": 1.0}) == 1.0
    assert score_stats({"pts_ppr": 22.4, "rec": 1}, {**PPR, "pts_ppr": 1.0}) == 1.0


def test_values_that_are_not_numbers_are_ignored_rather_than_crashing():
    assert score_stats({"rec": "3"}, PPR) == 3.0
    assert score_stats({"rec": None, "rec_yd": "x"}, PPR) == 0.0
    # A bool is not a number here: True would otherwise score as one reception.
    assert score_stats({"rec": True}, PPR) == 0.0
    assert as_number(float("inf")) is None and as_number(float("nan")) is None


def test_team_defense_return_yards_are_not_counted_twice():
    """RotoWire puts a returner's `pr_yd` on the team line beside the `def_` keys Sleeper
    scores, so a league that scores return yardage would pay for the same yards twice."""
    raw = {"pr_yd": 16.5, "def_pr_yd": 96.25, "kr_yd": 4.0, "def_kr_yd": 79.75, "sack": 2.59}
    cleaned = clean_projection(raw, "DEF")
    assert not any(key in cleaned for key in DST_DUPLICATE_STATS)
    assert cleaned == {"def_pr_yd": 96.25, "def_kr_yd": 79.75, "sack": 2.59}


def test_a_returner_keeps_his_own_return_yards():
    assert clean_projection({"pr_yd": 16.5, "rec": 4}, "WR") == {"pr_yd": 16.5, "rec": 4.0}
