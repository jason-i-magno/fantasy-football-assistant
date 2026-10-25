"""Positional dropoff and the candidate score."""

from helpers import make_player, make_pool

from draftbot import scarcity
from draftbot.scarcity import DROPOFF_CAP, candidate_score, positional_dropoffs


def pool_sorted(players):
    return sorted(players.values(), key=lambda p: (p.search_rank, p.player_id))


def test_dropoff_is_the_rank_gap_to_the_next_man_up():
    # Ranks: RB1=1, WR1=2, RB2=3, WR2=4, RB3=5, WR3=6 ...
    pool = pool_sorted(make_pool({"RB": 4, "WR": 4}))
    drops = positional_dropoffs(pool, picks_before=0, picks_between=4)
    # The top 4 come off the board: RB1, WR1, RB2, WR2. RB goes 1 -> 5, WR 2 -> 6.
    assert drops["RB"].best_now.player_id == "RB1"
    assert drops["RB"].best_then.player_id == "RB3"
    assert drops["RB"].value == 4
    assert drops["WR"].value == 4


def test_picks_before_shift_the_starting_pool():
    pool = pool_sorted(make_pool({"RB": 4, "WR": 4}))
    drops = positional_dropoffs(pool, picks_before=2, picks_between=2)
    # Two picks happen before my turn (RB1, WR1), so "now" already starts at RB2/WR2.
    assert drops["RB"].best_now.player_id == "RB2"
    assert drops["RB"].best_then.player_id == "RB3"


def test_no_following_pick_means_no_dropoff():
    pool = pool_sorted(make_pool({"RB": 3}))
    drops = positional_dropoffs(pool, picks_before=0, picks_between=None)
    assert drops["RB"].value == 0
    assert drops["RB"].best_then is drops["RB"].best_now


def test_position_running_dry_is_capped():
    pool = pool_sorted(make_pool({"TE": 1, "RB": 9}))
    drops = positional_dropoffs(pool, picks_before=0, picks_between=5)
    assert drops["TE"].best_then is None
    assert drops["TE"].value == DROPOFF_CAP


def test_empty_position_has_no_dropoff():
    pool = pool_sorted(make_pool({"RB": 2}))
    assert positional_dropoffs(pool, 0, 2)["QB"].value == 0


def test_score_prefers_rank_then_need_then_dropoff():
    rb = make_player("rb", "RB", 20)
    wr = make_player("wr", "WR", 20)
    teams = 12
    # Same rank: the one that fills a starting slot wins (lower adjusted rank).
    assert candidate_score(rb, 1.0, 0, teams) < candidate_score(wr, 0.35, 0, teams)
    # Same rank and need: the scarcer position wins.
    assert candidate_score(rb, 1.0, 30, teams) < candidate_score(wr, 1.0, 0, teams)
    # A full starter need is worth about one round of search_rank.
    assert candidate_score(rb, 1.0, 0, teams) == 20 - scarcity.W_NEED_ROUNDS * teams


def test_injured_players_do_not_anchor_the_dropoff():
    """An IR receiver at the top of the board invented cliffs everywhere below him."""
    pool = pool_sorted(make_pool({"WR": 4}))
    healthy = positional_dropoffs(pool, picks_before=0, picks_between=2)["WR"]
    assert healthy.best_now.player_id == "WR1" and healthy.value == 2

    hurt = [make_player("WR0", "WR", 0, injury_status="IR", team="PHI"), *pool]
    drop = positional_dropoffs(hurt, picks_before=0, picks_between=2)["WR"]
    # Neither end of the comparison may be a player who cannot take the field...
    assert drop.best_now.player_id == "WR1"
    assert drop.best_then.player_id == "WR2"
    assert drop.best_then.can_play
    # ...but he still occupies one of the simulated picks, because managers do draft
    # stashes — someone took Brock Bowers off the board while he was Out.
    assert drop.value == 1


def test_unavailable_players_are_penalised_but_still_draftable():
    healthy = make_player("a", "WR", 30)
    hurt = make_player("b", "WR", 17, injury_status="Out")
    assert candidate_score(hurt, 1.0, 0, 10) > candidate_score(healthy, 1.0, 0, 10)
    assert candidate_score(hurt, 1.0, 0, 10) == 17 - 10 + scarcity.UNAVAILABLE_PENALTY
