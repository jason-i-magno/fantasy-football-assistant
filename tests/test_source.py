"""`load_advice` end to end, offline: the one seam the rest of the suite reaches around.

Everything else in `ffa/lineup/` is tested on its inputs -- `build_advice` takes a report
and projections, `build_league_view` takes rosters and matchups, and `tests/test_web.py`
injects a whole loader rather than driving the real one. What assembles all of that from a
Sleeper client was checked, for a long time, by running it against the live league and
diffing `ffa lineup --json` against `main`. That is a fine check and a bad test: it needs a
season in progress, it costs forty requests, and a manager making a roster move between the
two runs reads as dozens of differences that have nothing to do with the change.

So: a fake client over hand-made payloads, and the invariants that four restructures of
this function could each have broken quietly.

The fake records every call, because *which* weeks get fetched is half of what this
function decides, and fetching a week twice or not at all is invisible in the output.
"""

from datetime import datetime, timedelta, timezone

import pytest

from ffa.espn_fantasy import ESPNFantasyError, ESPNPlayer
from ffa.lineup.source import load_advice

NOW = datetime(2026, 11, 12, 18, 0, tzinfo=timezone.utc)  # a Thursday
SEASON = 2026
SLOTS = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "K", "DEF", "BN", "BN"]
SCORING = {"pts_ppr": 0.0, "rec": 1.0, "rec_yd": 0.1, "rush_yd": 0.1, "rush_td": 6.0,
           "rec_td": 6.0, "pass_yd": 0.04, "pass_td": 4.0}
# Enough of a roster to fill every slot, plus a bench and one free agent to consider.
SQUAD = [("p1", "QB"), ("p2", "RB"), ("p3", "RB"), ("p4", "WR"), ("p5", "WR"),
         ("p6", "TE"), ("p7", "WR"), ("p8", "K"), ("p9", "DEF"), ("p10", "RB")]
RIVALS = [("p11", "QB"), ("p12", "RB"), ("p13", "WR"), ("p14", "TE"), ("p15", "K"),
          ("p16", "DEF"), ("p17", "WR"), ("p18", "RB")]
FREE = [("p19", "WR"), ("p20", "RB")]
EVERYONE = SQUAD + RIVALS + FREE


class FakeSleeper:
    """Every endpoint `load_advice` touches, answered from hand-made payloads.

    `calls` is the point of it as much as the data: it is how a test says that the weeks
    already scored were read once each and the weeks still to come were read once each,
    which no assertion about the returned advice can say.
    """

    def __init__(self, *, state_week: int = 9, last_scored: int = 8,
                 playoff_start: int = 15):
        self.calls: list[tuple] = []
        self.state_week = state_week
        self.last_scored = last_scored
        self.playoff_start = playoff_start
        self.cache_dir = None  # no ESPN page cache: these tests inject the feed instead

    def _record(self, *call):
        self.calls.append(call)

    def weeks_called(self, endpoint: str) -> list[int]:
        return [call[1] for call in self.calls if call[0] == endpoint]

    def nfl_state(self):
        self._record("nfl_state")
        return {"season": str(SEASON), "week": self.state_week, "season_type": "regular",
                "leg": self.state_week}

    def league(self, league_id):
        self._record("league", league_id)
        return {
            "name": "A League",
            "scoring_settings": SCORING,
            "roster_positions": SLOTS,
            "settings": {
                "waiver_day_of_week": 2, "waiver_clear_days": 2,
                "playoff_teams": 2, "playoff_week_start": self.playoff_start,
                "start_week": 1, "last_scored_leg": self.last_scored,
            },
        }

    def players(self):
        self._record("players")
        return ({pid: {"player_id": pid, "first_name": pos, "last_name": pid,
                       "position": pos, "fantasy_positions": [pos], "team": "SF",
                       "active": True, "search_rank": index + 1}
                 for index, (pid, pos) in enumerate(EVERYONE)}, "v1")

    def user(self, username):
        self._record("user", username)
        return {"user_id": "u1", "username": username}

    def rosters(self, league_id):
        self._record("rosters", league_id)
        return [
            {"roster_id": 3, "owner_id": "u1", "players": [p for p, _ in SQUAD],
             "starters": [p for p, _ in SQUAD[:9]],
             "settings": {"wins": 5, "losses": 3, "ties": 0, "fpts": 900,
                          "fpts_decimal": 0}},
            {"roster_id": 4, "owner_id": "u2", "players": [p for p, _ in RIVALS],
             "starters": [p for p, _ in RIVALS[:9]],
             "settings": {"wins": 3, "losses": 5, "ties": 0, "fpts": 800,
                          "fpts_decimal": 0}},
        ]

    def league_users(self, league_id):
        self._record("league_users", league_id)
        return [{"user_id": "u1", "display_name": "me", "metadata": {"team_name": "Mine"}},
                {"user_id": "u2", "display_name": "them"}]

    def transactions(self, league_id, week):
        self._record("transactions", week)
        return []

    def matchups(self, league_id, week):
        self._record("matchups", week)
        return [{"roster_id": 3, "matchup_id": 1, "points": 100.0,
                 "players": [p for p, _ in SQUAD], "starters": [p for p, _ in SQUAD[:9]]},
                {"roster_id": 4, "matchup_id": 1, "points": 90.0,
                 "players": [p for p, _ in RIVALS], "starters": [p for p, _ in RIVALS[:9]]}]

    def projections_week(self, season, week):
        self._record("projections_week", int(week))
        return [{"player_id": pid, "stats": {"pts_ppr": 10.0, "rec_yd": 100.0, "rec": 5.0}}
                for pid, _ in EVERYONE]

    def projections_season(self, season):
        self._record("projections_season", None)
        return [{"player_id": pid, "stats": {"pts_ppr": 170.0, "rec_yd": 1700.0, "gp": 18.0}}
                for pid, _ in EVERYONE]


def kickoffs_through(last_started: int):
    """Kickoffs where every week up to `last_started` has already begun.

    The target week is the earliest with a game still to come, so this is the dial that
    decides which week the build should pick -- deliberately not Sleeper's own `week`.
    """

    def kickoffs_for(season: int, week: int):
        offset = timedelta(days=7 * (week - 9))
        # Both of a started week's games have to be behind us: a week with one kickoff
        # still to come is still the week to set, which is the next test down.
        start = (NOW - timedelta(hours=5)) if week <= last_started else (NOW + timedelta(hours=5))
        return {"SF": start + offset, "KC": start + offset + timedelta(hours=3)}

    return kickoffs_for


def espn_feed(recorder: list | None = None):
    """ESPN's half of the blend, recording which weeks it was asked for."""

    def espn_for(season: int, weeks):
        if recorder is not None:
            recorder.append(tuple(weeks))
        return {week: [ESPNPlayer(espn_id=pid, name=f"{pos}{pid}", position=pos, team="SF",
                                  stats={"rec_yd": 100.0, "rec": 5.0}, applied_total=15.0)
                       for pid, pos in EVERYONE]
                for week in weeks}

    return espn_for


def build(client=None, *, kickoffs=None, espn=None, now=NOW):
    client = client or FakeSleeper()
    return load_advice(
        client,
        league_id="L1",
        username="me",
        now=now,
        kickoffs_for=kickoffs or kickoffs_through(8),
        espn_for=espn if espn is not None else espn_feed(),
    )


# -- the whole thing, assembled from payloads ------------------------------------------


def test_a_build_produces_advice_a_league_view_and_a_race():
    advice, view, sources = build()
    assert advice.week == 9 and advice.roster_id == 3
    assert [seat.slot for seat in view.teams[0].seats] == SLOTS[:9]
    assert view.teams[0].is_mine and len(view.teams) == 2
    assert sources.sleeper and sources.espn and sources.espn_note is None
    # The race rides on the same build, which is the one thing a lineup-only test misses.
    assert view.playoffs is not None and len(view.playoffs.teams) == 2
    assert view.playoffs.scoring is not None, "eight scored weeks are a scoring model"


# -- which week, and which weeks get fetched -------------------------------------------


def test_the_target_week_comes_from_the_kickoffs_not_from_sleeper():
    """Sleeper's `nfl_state.week` has read 9 while the app still showed week 8.

    Here it reads 9 while week 9's games have all kicked off, so the week to set a lineup
    for is 10. Nothing else in the suite drives this decision through a real build.
    """
    client = FakeSleeper(state_week=9)
    advice, view, _ = build(client, kickoffs=kickoffs_through(9))
    assert advice.week == 10 and view.week == 10


def test_a_week_in_progress_is_still_the_week_to_set():
    client = FakeSleeper(state_week=9)
    advice, _, _ = build(client, kickoffs=kickoffs_through(8))
    assert advice.week == 9, "week 9 has a game still to come, so it is still week 9"


def test_every_week_is_fetched_once_and_only_the_weeks_that_are_needed():
    """Fetching a week twice costs a request and shows up nowhere in the output.

    Scored weeks feed the scoring model, open weeks feed the race, and the two must not
    overlap -- a week in progress belongs to neither, which is the trap `last_scored_leg`
    exists to avoid.
    """
    client = FakeSleeper(state_week=9, last_scored=8, playoff_start=15)
    build(client)
    matchups = client.weeks_called("matchups")
    assert sorted(matchups) == list(range(1, 15)), "weeks 1-8 scored, 9-14 still to play"
    assert len(matchups) == len(set(matchups)), "and each of them exactly once"
    projections = client.weeks_called("projections_week")
    # Two playoff teams is one round, so this league's last week is 15, not 17: the window
    # stops where the league does rather than where the NFL does. It starts one week
    # before Sleeper's, not at the target week, because the projections are asked for
    # before the kickoffs have said which week that is -- the smallest range certain to
    # contain it. One small request over, against a whole round trip waiting.
    assert sorted(projections) == list(range(8, 16))
    assert len(projections) == len(set(projections))
    assert min(projections) >= client.state_week - 1, "the over-fetch stays bounded"


def test_the_espn_window_covers_the_week_the_build_settles_on():
    """The feed is fetched before the target week is known, so it is asked for a window
    that starts no later than any week the kickoffs could pick. If that ever stops being
    true the chosen week's ESPN rows come back *empty* rather than wrong."""
    asked: list[tuple] = []
    client = FakeSleeper(state_week=9)
    advice, _, sources = build(client, kickoffs=kickoffs_through(9), espn=espn_feed(asked))
    window, = asked
    assert advice.week == 10
    assert min(window) <= advice.week <= max(window)
    assert sources.espn, "and the week's rows are in what came back"


# -- the failure that must not be a failure --------------------------------------------


def test_espn_going_down_costs_the_second_opinion_and_nothing_else():
    """A second opinion is an improvement, not a dependency. The feed is fetched in a
    fan-out now, which raises the first exception any job raised, so this is the path that
    would quietly turn 'ESPN is down' into 'no advice today'."""
    def down(season, weeks):
        raise ESPNFantasyError("ESPN said no")

    advice, view, sources = build(espn=down)
    assert advice.week == 9 and advice.roster_id == 3
    assert sources.sleeper and not sources.espn
    assert "ESPN said no" in (sources.espn_note or "")
    assert view.playoffs is not None


def test_a_league_with_no_scoring_settings_says_so():
    client = FakeSleeper()
    client.league = lambda league_id: {"name": "A League", "roster_positions": SLOTS,
                                       "settings": {"waiver_day_of_week": 2}}
    with pytest.raises(Exception, match="no scoring settings"):
        build(client)
