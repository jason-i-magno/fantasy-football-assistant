"""Turning projections into advice: which bugs this layer is allowed to have is the point.

Each test here pins something that was wrong against live data first. The recurring theme
is that a plausible-looking number is not advice: a reshuffle worth nothing, a drop judged
on the one week a player happens to be idle, or a waiver claim on someone who would never
crack the lineup.
"""

from datetime import datetime, timedelta, timezone

import pytest

from ffa.config import load_config
from ffa.leagues import UnknownLeague, directory, resolve
from ffa.lineup.advice import (
    MAX_ADDS_PER_POSITION,
    NoRosterError,
    build_advice,
    resolve_roster,
    target_week,
)
from ffa.models import Player
from ffa.projections import Projection
from ffa.sleeper import SleeperError
from ffa.waivers.status import FREE_AGENT, ROSTERED, WAIVERS, WaiverReport, WaiverRules, WaiverStatus

NOW = datetime(2026, 11, 3, 20, 0, tzinfo=timezone.utc)
THURSDAY = datetime(2026, 11, 5, 1, 15, tzinfo=timezone.utc)
SUNDAY = datetime(2026, 11, 8, 18, 0, tzinfo=timezone.utc)
SLOTS = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "FLEX", "K", "DEF"]
LEAGUE = {"name": "A League", "roster_positions": SLOTS + ["BN"] * 5,
          "scoring_settings": {"rec": 1.0}}
KICKOFFS = {t: SUNDAY for t in ("SF", "KC", "DET", "BUF", "NYJ", "MIA", "LAR", "SEA")}


def player(player_id, position, name=None, team="SF", **kwargs):
    return Player(player_id=player_id, name=name or f"{position} {player_id}",
                  position=position, fantasy_positions=(position,), team=team,
                  search_rank=1, **kwargs)


def projection(player_id, position, points, *, season=None, team="SF", **kwargs):
    return Projection(player_id=player_id, position=position, team=team, points=points,
                      sleeper=points, season_per_game=season, sources=("sleeper",), **kwargs)


def roster_world(starters, bench, *, free_agents=(), now=NOW, kickoffs=None):
    """Assemble the whole input set for `build_advice` from (id, pos, points, season) tuples."""
    players, projections, statuses = {}, {}, []
    owned = []
    for group, rostered in ((starters, True), (bench, True), (free_agents, False)):
        for entry in group:
            pid, position, points, season = entry[0], entry[1], entry[2], entry[3]
            team = entry[4] if len(entry) > 4 else "SF"
            extra = entry[5] if len(entry) > 5 else {}
            players[pid] = player(pid, position, team=team, **extra.get("player", {}))
            projections[pid] = projection(pid, position, points, season=season, team=team,
                                          **extra.get("projection", {}))
            if rostered:
                owned.append(pid)
            statuses.append(WaiverStatus(
                player=players[pid],
                status=ROSTERED if rostered else (extra.get("status") or FREE_AGENT),
                clears_at=extra.get("clears_at"),
                rostered_by="me" if rostered else None,
            ))
    report = WaiverReport(
        league_name="A League", week=9, now=now,
        rules=WaiverRules(clear_weekday=2, drop_hold=timedelta(hours=47)),
        statuses=statuses, locked_teams={},
    )
    rosters = [{"roster_id": 3, "owner_id": "u1", "players": owned,
                "starters": [s[0] for s in starters]}]
    return dict(report=report, league=LEAGUE, rosters=rosters, user_id="u1",
                projections=projections, players=players, week=9,
                kickoffs=KICKOFFS if kickoffs is None else kickoffs, now=now)


# A lineup that is already optimal, used wherever the starting point shouldn't matter.
SETTLED_STARTERS = [
    ("qb", "QB", 19.0, 19.0), ("rb1", "RB", 13.0, 13.0), ("rb2", "RB", 12.0, 12.0),
    ("wr1", "WR", 21.0, 21.0), ("wr2", "WR", 14.0, 14.0), ("te1", "TE", 18.0, 18.0),
    ("wr3", "WR", 14.5, 14.5), ("te2", "TE", 11.0, 11.0),
    ("k", "K", 7.0, 7.0), ("def", "DEF", 8.0, 8.0),
]
SETTLED_BENCH = [("rb3", "RB", 9.0, 9.0), ("wr4", "WR", 6.0, 6.0)]


# -- which week ------------------------------------------------------------------------


def test_the_target_week_is_the_first_with_a_game_still_to_come():
    schedules = {8: {"SF": NOW - timedelta(days=2)}, 9: {"SF": THURSDAY}, 10: {"SF": SUNDAY + timedelta(days=7)}}
    assert target_week(schedules, NOW) == 9


def test_the_target_week_stays_put_while_the_week_is_still_being_played():
    """Mid-Sunday, most of the lineup is still changeable; jumping to next week is wrong."""
    midday = SUNDAY + timedelta(hours=2)
    schedules = {9: {"SF": SUNDAY, "KC": SUNDAY + timedelta(hours=27)}, 10: {"SF": SUNDAY + timedelta(days=7)}}
    assert target_week(schedules, midday) == 9


def test_the_target_week_rolls_over_once_the_last_game_has_started():
    schedules = {9: {"SF": SUNDAY}, 10: {"SF": SUNDAY + timedelta(days=7)}}
    assert target_week(schedules, SUNDAY + timedelta(minutes=1)) == 10


def test_no_games_left_at_all_has_no_target_week():
    assert target_week({9: {"SF": NOW - timedelta(days=9)}}, NOW) is None


# -- the roster ------------------------------------------------------------------------


def test_a_co_owned_roster_still_resolves():
    rosters = [{"roster_id": 1, "owner_id": "someone"},
               {"roster_id": 2, "owner_id": "other", "co_owners": ["u1"]}]
    assert resolve_roster(rosters, "u1")["roster_id"] == 2


def test_a_user_with_no_roster_is_an_error_not_an_empty_lineup():
    with pytest.raises(NoRosterError):
        resolve_roster([{"roster_id": 1, "owner_id": "someone"}], "u1")


# -- swaps -----------------------------------------------------------------------------


def test_moving_a_player_between_two_slots_he_both_fits_is_not_advice():
    """A WR in FLEX and a WR in WR score the same however you label the seats.

    Reported seat by seat, the solver's relabelling reads as two confident swaps on a week
    where the real gain is zero.
    """
    advice = build_advice(**roster_world(SETTLED_STARTERS, SETTLED_BENCH))
    assert advice.gain == pytest.approx(0.0)
    assert advice.swaps == () and advice.settled


def test_a_real_improvement_is_reported_once_with_its_gain():
    starters = list(SETTLED_STARTERS)
    starters[1] = ("rb1", "RB", 3.0, 3.0)  # a weak starter with a better man benched
    advice = build_advice(**roster_world(starters, [("rb3", "RB", 15.0, 15.0)]))
    assert len(advice.swaps) == 1
    swap = advice.swaps[0]
    assert swap.into.player_id == "rb3" and swap.out.player_id == "rb1"
    assert swap.gain == pytest.approx(12.0)
    assert advice.gain == pytest.approx(12.0)
    assert not advice.settled


def test_a_change_worth_a_rounding_error_is_not_reported():
    starters = list(SETTLED_STARTERS)
    starters[1] = ("rb1", "RB", 9.01, 9.01)
    advice = build_advice(**roster_world(starters, [("rb3", "RB", 9.06, 9.06)]))
    assert advice.swaps == ()


# -- byes, injuries and locks -----------------------------------------------------------


def test_a_bye_week_starter_is_zero_and_says_why():
    starters = list(SETTLED_STARTERS)
    starters[3] = ("wr1", "WR", 21.0, 21.0, "PIT")  # PIT has no kickoff this week
    advice = build_advice(**roster_world(starters, SETTLED_BENCH))
    line = advice.lines["wr1"]
    assert line.points == 0.0 and "his team has no game this week" in line.reasons


def test_a_player_ruled_out_does_not_start_whatever_the_feed_says():
    """Both feeds will happily project a player his team has already ruled out."""
    starters = list(SETTLED_STARTERS)
    starters[1] = ("rb1", "RB", 20.0, 20.0, "SF", {"player": {"injury_status": "Out"}})
    advice = build_advice(**roster_world(starters, [("rb3", "RB", 8.0, 8.0)]))
    assert advice.lines["rb1"].points == 0.0
    assert "rb3" in advice.optimal.started


def test_questionable_is_flagged_but_still_plays():
    """It is the designation most likely to be a stale in-game tag nobody cleared."""
    starters = list(SETTLED_STARTERS)
    starters[1] = ("rb1", "RB", 13.0, 13.0, "SF", {"player": {"injury_status": "Questionable"}})
    advice = build_advice(**roster_world(starters, SETTLED_BENCH))
    line = advice.lines["rb1"]
    assert line.points == pytest.approx(13.0)
    assert any("questionable" in reason for reason in line.reasons)


def test_a_started_player_whose_game_began_cannot_be_moved():
    kickoffs = {**KICKOFFS, "DET": NOW - timedelta(hours=1)}
    starters = list(SETTLED_STARTERS)
    starters[1] = ("rb1", "RB", 2.0, 2.0, "DET")  # locked in, however bad
    world = roster_world(starters, [("rb3", "RB", 20.0, 20.0)], kickoffs=kickoffs)
    advice = build_advice(**world)
    assert advice.lines["rb1"].locked
    assert advice.optimal.seats[1].player_id == "rb1"
    assert "rb1" in advice.optimal.started


# -- adds ------------------------------------------------------------------------------


def test_an_add_is_ranked_by_what_it_does_to_the_lineup_not_by_its_projection():
    """A receiver behind better receivers adds little; a tight end filling a hole adds a lot.

    The free agent with the bigger projection is the worse pickup here, which is the whole
    reason the ranking is a lineup solve and not a sort.
    """
    free_agents = [("fa_wr", "WR", 14.4, 14.4), ("fa_te", "TE", 12.0, 12.0)]
    starters = list(SETTLED_STARTERS)
    starters[5] = ("te1", "TE", 2.0, 2.0)  # a genuine hole: both tight ends are useless
    starters[7] = ("te2", "TE", 1.0, 1.0)
    advice = build_advice(**roster_world(starters, SETTLED_BENCH, free_agents=free_agents))
    assert advice.adds, "a hole at TE should produce an add"
    best = advice.adds[0]
    assert best.line.player_id == "fa_te"  # despite projecting fewer points than fa_wr
    assert best.gain == pytest.approx(10.0)
    receiver = next(c for c in advice.adds if c.line.player_id == "fa_wr")
    assert receiver.gain < best.gain


def test_a_full_roster_names_the_drop_and_spares_a_bye_week_starter():
    """Judged on this week alone every bye player projects zero and looks free to drop."""
    bench = [("on_bye", "WR", 0.0, 12.0, "PIT"), ("deadweight", "RB", 1.0, 1.0)]
    world = roster_world(SETTLED_STARTERS, bench,
                         free_agents=[("fa_def", "DEF", 18.0, 18.0)])
    # A full roster: every slot in roster_positions is occupied.
    world["rosters"][0]["players"] += [f"filler{i}" for i in range(15 - len(world["rosters"][0]["players"]))]
    advice = build_advice(**world)
    assert advice.adds
    drop = advice.adds[0].drop
    assert drop is not None and drop.player_id == "deadweight"


def test_one_bye_does_not_fill_the_list_with_interchangeable_streamers():
    free_agents = [(f"fa_def{i}", "DEF", 18.0 - i, 18.0 - i) for i in range(6)]
    starters = list(SETTLED_STARTERS)
    starters[9] = ("def", "DEF", 0.0, 0.0, "PIT")
    advice = build_advice(**roster_world(starters, SETTLED_BENCH, free_agents=free_agents))
    defenses = [c for c in advice.adds if c.line.player.position == "DEF"]
    assert 0 < len(defenses) <= MAX_ADDS_PER_POSITION


def test_an_add_carries_whether_it_can_be_made_now():
    clears = NOW + timedelta(hours=5)
    free_agents = [
        ("fa_te", "TE", 12.0, 12.0, "SF", {"status": WAIVERS, "clears_at": clears}),
    ]
    starters = list(SETTLED_STARTERS)
    starters[5] = ("te1", "TE", 2.0, 2.0)
    advice = build_advice(**roster_world(starters, SETTLED_BENCH, free_agents=free_agents))
    assert advice.adds[0].clears_at == clears
    assert not advice.adds[0].available_now


# -- the watchlist -----------------------------------------------------------------------


def test_the_watchlist_is_about_the_rest_of_the_season_not_this_week():
    """A back who just took over a backfield may not help on Sunday and still be the claim."""
    free_agents = [("riser", "RB", 11.0, 16.0)]  # modest this week, strong going forward
    advice = build_advice(**roster_world(SETTLED_STARTERS, SETTLED_BENCH, free_agents=free_agents))
    assert not any(c.line.player_id == "riser" for c in advice.adds)
    assert [c.line.player_id for c in advice.watch] == ["riser"]


def test_a_backup_who_would_never_start_is_not_on_the_watchlist():
    """One quarterback slot, two better quarterbacks already owned."""
    free_agents = [("backup_qb", "QB", 17.0, 17.0)]
    advice = build_advice(**roster_world(SETTLED_STARTERS,
                                         SETTLED_BENCH + [("qb2", "QB", 18.0, 18.0)],
                                         free_agents=free_agents))
    assert not any(c.line.player_id == "backup_qb" for c in advice.watch)


# -- what the two surfaces show ---------------------------------------------------------


def _rendered(advice):
    from rich.console import Console

    from ffa.lineup.source import LineupSources
    from ffa.lineup.ui import render

    console = Console(width=200, no_color=True)
    with console.capture() as capture:
        console.print(render(advice, LineupSources(sleeper=True, espn=False)))
    return capture.get()


def test_a_waiver_candidate_shows_both_horizons_and_not_just_this_week():
    """A claim is kept for weeks, so "what is he worth later" has to be on the row too.

    The playoff figure arrived first and the rest-of-season one was left on the bench table
    only, which read as though a candidate had no value past Sunday at all.
    """
    from ffa.lineup.source import LineupSources
    from ffa.lineup.ui import advice_json

    free_agents = [("fa_te", "TE", 12.0, 9.0, "SF", {"projection": {"rest_per_game": 17.0}})]
    advice = build_advice(**roster_world(SETTLED_STARTERS, SETTLED_BENCH,
                                         free_agents=free_agents))
    assert [c.line.player_id for c in advice.adds] == ["fa_te"]
    assert advice.adds[0].line.rest == 17.0  # the fresh rate, not the 9.0 preseason row

    adds = _rendered(advice).split("Add this week")[1]
    assert "Rest of season" in adds and "17.0/g" in adds

    payload = advice_json(advice, LineupSources(sleeper=True, espn=False))
    assert payload["adds"][0]["player"]["rest"] == 17.0


def test_the_figure_shown_is_the_fresh_one_not_the_preseason_row():
    """Against live data a back's preseason row had him in a committee at 9.5/g while the
    same provider's weekly rows had him leading a backfield at 14.5/g. The card showed the
    9.5 and called it rest of season."""
    bench = SETTLED_BENCH + [("warren", "RB", 0.0, 9.5, "PIT",
                              {"projection": {"rest_per_game": 14.5, "on_bye": True}})]
    advice = build_advice(**roster_world(SETTLED_STARTERS, bench))
    line = advice.line_of("warren")
    assert line.rest == 14.5
    assert line.keep == 9.5  # the drop floor is still the slow number, deliberately
    assert "14.5/g" in _rendered(advice)


def test_a_player_the_weekly_feed_has_no_row_for_keeps_his_season_floor():
    """Only the *display* falls back to a dash. Ranking him at zero would make a player
    nobody is projecting this week the obvious man to drop, which is the old bug."""
    free_agents = [("no_rows", "RB", 0.0, 16.0)]  # season row only, no fresh rate
    advice = build_advice(**roster_world(SETTLED_STARTERS, SETTLED_BENCH,
                                         free_agents=free_agents))
    watch = {c.line.player_id: c for c in advice.watch}
    assert "no_rows" in watch and watch["no_rows"].line.rest is None
    assert watch["no_rows"].line.keep == 16.0


def test_the_watchlist_ranks_on_the_fresh_rate_not_the_stale_row():
    """Two free agents the preseason rows rank backwards: one has taken over a role and
    one has lost it, and only the weekly feed knows."""
    free_agents = [
        ("riser", "RB", 10.0, 6.0, "SF", {"projection": {"rest_per_game": 16.0}}),
        ("fader", "RB", 10.0, 16.0, "SF", {"projection": {"rest_per_game": 6.0}}),
    ]
    advice = build_advice(**roster_world(SETTLED_STARTERS, SETTLED_BENCH,
                                         free_agents=free_agents))
    assert [c.line.player_id for c in advice.watch] == ["riser"]


# -- more than one league ---------------------------------------------------------------


class FakeLeagues:
    def __init__(self, names, fail=()):
        self._names = names
        self._fail = set(fail)

    def league(self, league_id):
        if league_id in self._fail:
            raise SleeperError(f"nope: {league_id}")
        return {"name": self._names[league_id]}


def two_league_config():
    return load_config(
        environ={"SLEEPER_USERNAME": "u", "SLEEPER_LEAGUE_ID": "111",
                 "SLEEPER_LEAGUE_IDS": "111, 222"},
        env_file=None,
    )


def test_the_configured_leagues_keep_their_order_with_the_default_first():
    config = two_league_config()
    assert config.league_ids == ("111", "222") and config.league_id == "111"
    refs = directory(FakeLeagues({"111": "First", "222": "Second"}), config)
    assert [(r.league_id, r.name, r.is_default) for r in refs] == [
        ("111", "First", True), ("222", "Second", False)
    ]


def test_a_list_on_its_own_supplies_the_default():
    config = load_config(environ={"SLEEPER_USERNAME": "u", "SLEEPER_LEAGUE_IDS": "777,888"},
                         env_file=None)
    assert config.league_id == "777" and config.league_ids == ("777", "888")


def test_a_league_that_cannot_be_read_does_not_break_the_picker():
    """One deleted or renamed league must not cost you the others."""
    refs = directory(FakeLeagues({"111": "First", "222": ""}, fail=["222"]), two_league_config())
    assert [r.available for r in refs] == [True, False]
    assert refs[1].label == "222"  # falls back to the id, so it can still be chosen


def test_a_league_can_be_named_instead_of_numbered():
    client, config = FakeLeagues({"111": "The Example Bowl", "222": "Dynasty"}), two_league_config()
    assert resolve(client, config, "Dynasty") == "222"
    assert resolve(client, config, "dynasty") == "222"  # case doesn't matter
    assert resolve(client, config, "example") == "111"  # nor does a partial name
    assert resolve(client, config, "222") == "222"  # an id still passes through
    assert resolve(client, config, None) == "111"  # and nothing means the default


def test_an_ambiguous_name_is_refused_rather_than_guessed():
    client = FakeLeagues({"111": "Dynasty A", "222": "Dynasty B"})
    with pytest.raises(UnknownLeague, match="more than one"):
        resolve(client, two_league_config(), "Dynasty")


def test_an_unknown_name_lists_what_is_configured():
    client = FakeLeagues({"111": "First", "222": "Second"})
    with pytest.raises(UnknownLeague, match="'First', 'Second'"):
        resolve(client, two_league_config(), "Third")
