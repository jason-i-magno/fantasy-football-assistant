"""Waiver status: the after-game lock, the post-drop hold, and the `ffa waivers` command."""

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from helpers import make_player
from ffa import cli
from ffa.waivers.status import (
    FREE_AGENT,
    ROSTERED,
    WAIVERS,
    UnsupportedWaivers,
    WaiverRules,
    build_report,
    classify,
    last_kickoffs,
    recent_drops,
    roster_owners,
    select_statuses,
)

ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")
RULES = WaiverRules.from_settings({"waiver_day_of_week": 2, "waiver_clear_days": 2, "daily_waivers": 0})
WEEK7_CLEAR = datetime(2026, 10, 28, 0, 5, tzinfo=PT)  # Wednesday 12:05 AM Pacific
SAT_NIGHT = datetime(2026, 10, 24, 23, 0, tzinfo=ET)
THURSDAY_GAME = datetime(2026, 10, 22, 20, 15, tzinfo=ET)


def millis(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


# -- rules ---------------------------------------------------------------------------


def test_rules_from_real_league_settings():
    assert RULES.clear_weekday == 2
    assert RULES.drop_hold == timedelta(hours=47)  # Sleeper's "2 days", measured


def test_no_post_drop_hold_when_clear_days_is_zero():
    rules = WaiverRules.from_settings({"waiver_day_of_week": 2, "waiver_clear_days": 0})
    assert rules.drop_hold == timedelta(0)


@pytest.mark.parametrize("settings", [
    {"daily_waivers": 1, "waiver_day_of_week": 2, "waiver_clear_days": 2},
    {"waiver_clear_days": 2},
    {"waiver_day_of_week": 9, "waiver_clear_days": 2},
])
def test_unmodeled_settings_refuse_rather_than_guess(settings):
    with pytest.raises(UnsupportedWaivers):
        WaiverRules.from_settings(settings)


@pytest.mark.parametrize("kickoff, clear", [
    (THURSDAY_GAME, WEEK7_CLEAR),
    (datetime(2026, 10, 25, 13, 0, tzinfo=ET), WEEK7_CLEAR),  # Sunday early
    (datetime(2026, 10, 26, 20, 15, tzinfo=ET), WEEK7_CLEAR),  # Monday night
    # A game just after a clear waits a full week for the next one.
    (datetime(2026, 10, 28, 0, 10, tzinfo=PT), datetime(2026, 11, 4, 0, 5, tzinfo=PT)),
])
def test_after_game_lock_clears_wednesday_1205_pacific(kickoff, clear):
    assert RULES.after_game_clear(kickoff) == clear


def test_after_game_clear_follows_pacific_time_across_dst():
    clear = RULES.after_game_clear(datetime(2026, 11, 1, 13, 0, tzinfo=ET))  # DST ends Nov 1
    assert clear == datetime(2026, 11, 4, 0, 5, tzinfo=PT)
    assert clear.astimezone(timezone.utc).hour == 8  # 12:05 AM PST, not PDT


# -- classify ------------------------------------------------------------------------


def test_player_whose_team_played_is_on_waivers_until_the_clear():
    status = classify(make_player("1", "RB", 10), RULES, SAT_NIGHT, last_kickoff=THURSDAY_GAME)
    assert status.status == WAIVERS
    assert status.clears_at == WEEK7_CLEAR
    assert status.kicked_off == THURSDAY_GAME and status.dropped_at is None


def test_player_whose_game_is_still_ahead_is_a_free_agent():
    # The caller passes only kickoffs at or before now; Sunday's game isn't one yet.
    assert classify(make_player("1", "RB", 10), RULES, SAT_NIGHT).status == FREE_AGENT


def test_last_weeks_lock_has_cleared():
    last_monday = datetime(2026, 10, 19, 20, 15, tzinfo=ET)
    assert classify(make_player("1", "RB", 10), RULES, SAT_NIGHT, last_kickoff=last_monday).status == FREE_AGENT


def test_recent_drop_is_held_47_hours():
    dropped = SAT_NIGHT - timedelta(hours=10)
    status = classify(make_player("1", "RB", 10), RULES, SAT_NIGHT, dropped_at=dropped)
    assert status.status == WAIVERS
    assert status.clears_at == dropped + timedelta(hours=47)
    assert status.kicked_off is None


def test_drop_older_than_the_hold_is_a_free_agent():
    dropped = SAT_NIGHT - timedelta(hours=48)
    assert classify(make_player("1", "RB", 10), RULES, SAT_NIGHT, dropped_at=dropped).status == FREE_AGENT


def test_both_rules_clear_at_whichever_ends_later():
    player = make_player("1", "RB", 10)
    early_drop = classify(player, RULES, SAT_NIGHT, last_kickoff=THURSDAY_GAME, dropped_at=SAT_NIGHT)
    assert early_drop.clears_at == WEEK7_CLEAR
    assert early_drop.kicked_off and early_drop.dropped_at  # both reasons reported

    tuesday = datetime(2026, 10, 27, 20, 0, tzinfo=ET)
    late_drop = classify(player, RULES, tuesday, last_kickoff=THURSDAY_GAME, dropped_at=tuesday)
    assert late_drop.clears_at == tuesday + timedelta(hours=47)


def test_rostered_player_is_never_a_free_agent():
    status = classify(make_player("1", "RB", 10), RULES, SAT_NIGHT, rostered_by="Team A")
    assert status.status == ROSTERED and status.rostered_by == "Team A"


# -- data assembly -------------------------------------------------------------------


def test_last_kickoffs_keeps_each_teams_latest_past_kickoff():
    week6 = {"NE": datetime(2026, 10, 18, 13, 0, tzinfo=ET), "BUF": datetime(2026, 10, 18, 13, 0, tzinfo=ET)}
    week7 = {"NE": THURSDAY_GAME, "PIT": datetime(2026, 10, 25, 9, 30, tzinfo=ET)}
    latest = last_kickoffs([week6, week7], SAT_NIGHT)
    assert latest == {"NE": THURSDAY_GAME, "BUF": week6["BUF"]}  # PIT hasn't kicked off


def test_recent_drops_skips_failed_and_old_transactions_and_keeps_the_latest():
    since = SAT_NIGHT - timedelta(hours=47)
    transactions = [
        {"status": "complete", "status_updated": millis(SAT_NIGHT - timedelta(hours=5)), "drops": {"7": 1}},
        {"status": "complete", "status_updated": millis(SAT_NIGHT - timedelta(hours=1)), "drops": {"7": 1}},
        {"status": "failed", "status_updated": millis(SAT_NIGHT), "drops": {"8": 1}},
        {"status": "complete", "status_updated": millis(SAT_NIGHT - timedelta(hours=60)), "drops": {"9": 1}},
        {"status": "complete", "status_updated": millis(SAT_NIGHT), "drops": None, "adds": {"10": 1}},
    ]
    drops = recent_drops(transactions, since)
    assert set(drops) == {"7"}
    assert drops["7"] == SAT_NIGHT - timedelta(hours=1)


def test_roster_owners_covers_ir_and_taxi_and_prefers_team_names():
    rosters = [
        {"roster_id": 1, "owner_id": "u1", "players": ["1", "2"], "reserve": ["3"], "taxi": ["4"]},
        {"roster_id": 2, "owner_id": "u2", "players": ["5"]},
        {"roster_id": 3, "owner_id": None, "players": ["6"]},
    ]
    users = [
        {"user_id": "u1", "display_name": "one", "metadata": {"team_name": "Team One"}},
        {"user_id": "u2", "display_name": "two", "metadata": {}},
    ]
    owners = roster_owners(rosters, users)
    assert owners == {"1": "Team One", "2": "Team One", "3": "Team One", "4": "Team One",
                      "5": "two", "6": "roster 3"}


def test_select_statuses_defaults_to_unrostered_players_on_an_nfl_team_by_rank():
    statuses = [
        classify(make_player("1", "WR", 30), RULES, SAT_NIGHT),
        classify(make_player("2", "RB", 10), RULES, SAT_NIGHT, rostered_by="Team A"),
        classify(make_player("3", "RB", 20), RULES, SAT_NIGHT),
        classify(make_player("4", "RB", 5, team=None), RULES, SAT_NIGHT),
        classify(make_player("5", "TE", None), RULES, SAT_NIGHT),
    ]
    assert [s.player.player_id for s in select_statuses(statuses)] == ["3", "1", "5"]
    assert [s.player.player_id for s in select_statuses(statuses, positions=["rb"])] == ["3"]
    assert [s.player.player_id for s in select_statuses(statuses, limit=1)] == ["3"]
    # Looking a player up by name finds him whether he's rostered or not.
    assert [s.status for s in select_statuses(statuses, names=["rb2"])] == [ROSTERED]


class FakeSleeper:
    """Duck-typed SleeperClient over canned payloads."""

    def __init__(self, now: datetime, *, season_type: str = "regular"):
        self.season_type = season_type
        self.transaction_weeks: list[int] = []
        self.now = now

    def league(self, league_id):
        return {"name": "Test League", "settings": {"waiver_day_of_week": 2, "waiver_clear_days": 2,
                                                    "daily_waivers": 0}}

    def nfl_state(self):
        return {"season": "2026", "season_type": self.season_type, "week": 7}

    def rosters(self, league_id):
        return [{"roster_id": 1, "owner_id": "u1", "players": ["10"]}]

    def league_users(self, league_id):
        return [{"user_id": "u1", "display_name": "manager_one", "metadata": {"team_name": "Team One"}}]

    def transactions(self, league_id, week):
        self.transaction_weeks.append(week)
        if week != 7:
            return []
        return [{"status": "complete", "status_updated": millis(self.now - timedelta(hours=3)),
                 "drops": {"30": 1}}]

    def players(self):
        raw = {
            "10": {"full_name": "Rostered Back", "position": "RB", "team": "NE", "search_rank": 1, "active": True},
            "20": {"full_name": "Locked Back", "position": "RB", "team": "NE", "search_rank": 2, "active": True},
            "30": {"full_name": "Dropped Back", "position": "RB", "team": "BUF", "search_rank": 3, "active": True},
            "40": {"full_name": "Free Back", "position": "RB", "team": "PIT", "search_rank": 4, "active": True},
            "NE": {"first_name": "New England", "last_name": "Patriots", "position": "DEF", "team": "NE",
                   "search_rank": 50, "active": True},
        }
        return raw, "2026-10-24"


def fake_kickoffs(now: datetime):
    """NE kicked off an hour ago; PIT plays tomorrow; BUF is on bye."""
    def kickoffs_for(season, week):
        assert season == 2026
        return {"NE": now - timedelta(hours=1), "PIT": now + timedelta(days=1)} if week == 7 else {}
    return kickoffs_for


def test_build_report_classifies_every_player():
    now = SAT_NIGHT
    client = FakeSleeper(now)
    report = build_report(client, "123", now, fake_kickoffs(now))

    by_id = {s.player.player_id: s for s in report.statuses}
    assert by_id["10"].status == ROSTERED and by_id["10"].rostered_by == "Team One"
    assert by_id["20"].status == WAIVERS and by_id["20"].clears_at == WEEK7_CLEAR
    assert by_id["NE"].status == WAIVERS  # team defenses lock with their team
    assert by_id["30"].status == WAIVERS and by_id["30"].dropped_at == now - timedelta(hours=3)
    assert by_id["40"].status == FREE_AGENT
    assert report.locked_teams == {"NE": WEEK7_CLEAR}
    assert sorted(client.transaction_weeks) == [6, 7, 8]


def test_build_report_refuses_outside_the_regular_season():
    with pytest.raises(UnsupportedWaivers, match="regular season"):
        build_report(FakeSleeper(SAT_NIGHT, season_type="post"), "123", SAT_NIGHT, fake_kickoffs(SAT_NIGHT))


# -- CLI -----------------------------------------------------------------------------


@pytest.fixture
def waivers_cli(tmp_path, monkeypatch):
    """`ffa waivers` against FakeSleeper, with the league coming from --league."""
    env = tmp_path / ".env"
    env.write_text("SLEEPER_USERNAME=manager_one\n")
    for name in ("SLEEPER_USERNAME", "SLEEPER_LEAGUE_ID", "DRAFT_ID"):
        monkeypatch.delenv(name, raising=False)
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(cli, "SleeperClient", lambda cache_dir: FakeSleeper(now))
    monkeypatch.setattr(cli.espn, "week_kickoffs", fake_kickoffs(now))
    return lambda *argv: cli.main(["--env-file", str(env), "waivers", *argv])


def test_cli_waivers_json(waivers_cli, capsys):
    assert waivers_cli("--league", "123", "--json") == cli.EXIT_OK
    report = json.loads(capsys.readouterr().out)
    assert report["league"] == "Test League"
    assert list(report["locked_teams"]) == ["NE"]
    statuses = {p["name"]: p["status"] for p in report["players"]}
    assert statuses == {"Locked Back": WAIVERS, "Dropped Back": WAIVERS, "Free Back": FREE_AGENT,
                        "New England Patriots": WAIVERS}


def test_cli_waivers_table_looks_up_rostered_players(waivers_cli, capsys):
    assert waivers_cli("--league", "123", "--player", "rostered") == cli.EXIT_OK
    assert "rostered: Team One" in capsys.readouterr().out


def test_cli_waivers_unknown_player_exits_nonzero(waivers_cli, capsys):
    assert waivers_cli("--league", "123", "--player", "nobody") == cli.EXIT_ERROR
    assert "No player matches nobody" in capsys.readouterr().out


def test_cli_waivers_needs_a_league(waivers_cli, monkeypatch, capsys):
    monkeypatch.setenv("DRAFT_ID", "456")  # a config that's valid for a mock draft only
    assert waivers_cli() == cli.EXIT_CONFIG
    assert "--league" in capsys.readouterr().out
