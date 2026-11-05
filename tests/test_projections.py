"""Joining and blending two projection feeds, and the quirks of each.

Most of these pin a specific way one of the feeds misleads you. They were all found against
live data and each one silently produced a wrong recommendation before it was fixed.
"""

import json

import httpx
import pytest

from ffa import projections as proj
from ffa.espn_fantasy import (
    ESPNPlayer,
    PLAYER_FEED_URL,
    fetch_weeks,
    parse_player,
    translate_stats,
    week_projections,
)
from ffa.models import Player

PPR = {"rec": 1.0, "rec_yd": 0.1, "rec_td": 6.0, "rush_yd": 0.1, "rush_td": 6.0,
       "sack": 1.0, "pts_allow_7_13": 4.0, "fgm_30_39": 3.0}


def player(player_id, position, name, team="SF", espn_id=None, **kwargs):
    return Player(player_id=player_id, name=name, position=position,
                  fantasy_positions=(position,), team=team, search_rank=1,
                  espn_id=espn_id, **kwargs)


def sleeper_row(player_id, stats, **extra):
    return {"player_id": player_id, "stats": {"pts_ppr": 0.0, **stats}, **extra}


def espn_row(espn_id, name, position, team, stats):
    return ESPNPlayer(espn_id=espn_id, name=name, position=position, team=team,
                      stats=stats, applied_total=None)


# -- the ESPN feed's own shape --------------------------------------------------------


def test_duplicate_espn_stat_ids_are_not_counted_twice():
    """ESPN repeats receptions, receiving yards and rushing yards under second ids."""
    stats = translate_stats({"53": 4.0, "41": 4.0, "42": 38.8, "61": 38.8, "24": 102.9, "40": 102.9})
    assert stats == {"rec": 4.0, "rec_yd": 38.8, "rush_yd": 102.9}


def test_split_buckets_are_added_back_together():
    """One Sleeper tier is two ids on ESPN's side; both halves belong to it."""
    assert translate_stats({"92": 0.3, "121": 0.2})["pts_allow_14_20"] == pytest.approx(0.5)
    assert translate_stats({"93": 0.1, "101": 0.2, "102": 0.3})["st_td"] == pytest.approx(0.6)


def test_only_the_requested_week_of_the_requested_season_is_read():
    """The payload carries every remaining week, and two seasons of them.

    Matching on the period alone silently returns last season's same-numbered week.
    """
    raw = {"player": {"fullName": "A Back", "defaultPositionId": 2, "proTeamId": 25, "stats": [
        {"statSourceId": 1, "statSplitTypeId": 1, "scoringPeriodId": 9, "seasonId": 2025,
         "appliedTotal": 99.0, "stats": {"24": 999.0}},
        {"statSourceId": 0, "statSplitTypeId": 1, "scoringPeriodId": 9, "seasonId": 2026,
         "appliedTotal": 3.0, "stats": {"24": 3.0}},
        {"statSourceId": 1, "statSplitTypeId": 1, "scoringPeriodId": 10, "seasonId": 2026,
         "appliedTotal": 50.0, "stats": {"24": 50.0}},
        {"statSourceId": 1, "statSplitTypeId": 1, "scoringPeriodId": 9, "seasonId": 2026,
         "appliedTotal": 12.0, "stats": {"24": 60.0}},
    ]}, "id": 1}
    parsed = parse_player(raw, 2026, 9)
    assert parsed.stats == {"rush_yd": 60.0} and parsed.applied_total == 12.0
    assert parsed.team == "SF" and parsed.position == "RB"


def test_the_request_carries_no_stats_filter():
    """Any `filterStatsFor*` key makes ESPN drop the projections and return only actuals."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"players": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        week_projections(2026, 9, http=client, pages=1)
    assert str(seen[0].url).startswith(PLAYER_FEED_URL.format(season=2026))
    sent = json.loads(seen[0].headers["X-Fantasy-Filter"])
    assert set(sent["players"]) == {"sortPercOwned", "limit", "offset"}
    assert not any(key.startswith("filterStatsFor") for key in sent["players"])


def test_paging_stops_when_a_page_comes_back_short():
    pages = []

    def handler(request: httpx.Request) -> httpx.Response:
        pages.append(request)
        return httpx.Response(200, json={"players": [
            {"id": len(pages), "player": {"fullName": "X", "defaultPositionId": 3,
                                          "proTeamId": 25, "stats": []}}
        ]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        rows = week_projections(2026, 9, http=client, pages=5, page_size=50)
    assert len(pages) == 1 and len(rows) == 1


# -- the join -------------------------------------------------------------------------


def test_players_join_on_espn_id_then_name_and_team():
    players = {
        "1": player("1", "RB", "By Id", espn_id="777"),
        "2": player("2", "WR", "By Name", team="KC"),
    }
    built = proj.build(
        players=players, scoring=PPR,
        sleeper_week=[],
        espn_week=[espn_row("777", "Totally Different", "RB", "SF", {"rush_yd": 100.0}),
                   espn_row("999", "By Name", "WR", "KC", {"rec": 5.0})],
    )
    assert built["1"].espn == pytest.approx(10.0)
    assert built["2"].espn == pytest.approx(5.0)


def test_name_suffixes_do_not_break_the_join():
    """ESPN and Sleeper disagree about Jr/III often enough to lose real players."""
    players = {"1": player("1", "TE", "Harold Fannin Jr.", team="CLE")}
    built = proj.build(players=players, scoring=PPR, sleeper_week=[],
                       espn_week=[espn_row("5", "Harold Fannin", "TE", "CLE", {"rec": 6.0})])
    assert built["1"].espn == pytest.approx(6.0)
    assert proj.normalize_name("Luther Burden III") == "luther burden"


def test_two_players_with_the_same_name_are_left_unjoined():
    """A wrong join is worse than a missing one: it reports another man's week."""
    players = {"1": player("1", "WR", "Mike Williams", team="NYJ"),
               "2": player("2", "WR", "Mike Williams", team="LAC")}
    built = proj.build(players=players, scoring=PPR, sleeper_week=[],
                       espn_week=[espn_row("5", "Mike Williams", "WR", "SEA", {"rec": 9.0})])
    assert all(p.espn is None for p in built.values())


def test_a_team_defense_joins_on_its_team_not_its_name():
    players = {"SEA": player("SEA", "DEF", "Seattle Seahawks", team="SEA")}
    built = proj.build(players=players, scoring=PPR, sleeper_week=[],
                       espn_week=[espn_row("-16026", "Seahawks D/ST", "DEF", "SEA",
                                           {"sack": 3.0})])
    assert built["SEA"].espn == pytest.approx(3.0)


# -- the blend ------------------------------------------------------------------------


def test_the_blend_is_the_mean_and_the_spread_is_reported():
    players = {"1": player("1", "WR", "Wide One")}
    built = proj.build(
        players=players, scoring=PPR,
        sleeper_week=[sleeper_row("1", {"rec": 10.0})],
        espn_week=[espn_row("5", "Wide One", "WR", "SF", {"rec": 20.0})],
    )
    one = built["1"]
    assert one.sleeper == 10.0 and one.espn == 20.0
    assert one.points == pytest.approx(15.0)
    assert one.spread == pytest.approx(10.0) and one.disputed
    assert set(one.sources) == {"sleeper", "espn"}


def test_one_source_is_used_alone_and_flagged():
    players = {"1": player("1", "WR", "Wide One")}
    built = proj.build(players=players, scoring=PPR,
                       sleeper_week=[sleeper_row("1", {"rec": 10.0})])
    assert built["1"].points == 10.0 and built["1"].single_source
    assert built["1"].spread is None and not built["1"].disputed


def test_kickers_come_from_sleeper_only():
    """ESPN reports made field goals under 40 as one bucket; leagues score three tiers."""
    players = {"1": player("1", "K", "Boot Leg")}
    built = proj.build(
        players=players, scoring=PPR,
        sleeper_week=[sleeper_row("1", {"fgm_30_39": 2.0})],
        espn_week=[espn_row("5", "Boot Leg", "K", "SF", {"rec": 50.0})],
    )
    assert built["1"].espn is None and built["1"].points == pytest.approx(6.0)


# -- byes, and what is not a bye ------------------------------------------------------


def test_a_bye_is_zero_and_says_so():
    players = {"1": player("1", "WR", "Wide One", team="PIT")}
    built = proj.build(players=players, scoring=PPR,
                       sleeper_week=[sleeper_row("1", {"rec": 10.0})],
                       playing_teams=frozenset({"SF", "KC"}))
    assert built["1"].on_bye and built["1"].points == 0.0 and built["1"].sources == ()
    assert not built["1"].playable


def test_a_player_with_no_nfl_team_is_not_on_a_bye():
    """Hundreds of unsigned players have no team; calling that a bye is a wrong reason."""
    players = {"1": player("1", "WR", "Free Agent", team=None)}
    built = proj.build(players=players, scoring=PPR,
                       sleeper_week=[sleeper_row("1", {"rec": 10.0})],
                       playing_teams=frozenset({"SF"}))
    assert built["1"].no_team and not built["1"].on_bye
    assert built["1"].points == 0.0 and not built["1"].playable


# -- the season row, and the role change it gives away --------------------------------


def test_season_rows_are_divided_by_their_games():
    players = {"1": player("1", "RB", "Back One")}
    built = proj.build(players=players, scoring=PPR, sleeper_week=[],
                       sleeper_season=[sleeper_row("1", {"rush_yd": 1800.0, "gp": 18.0})])
    assert built["1"].season_per_game == pytest.approx(10.0)


def test_a_defense_season_row_is_not_read_as_a_single_game():
    """Every team defense reports `gp: 1.0` where every other position reports 18.0.

    Taken at face value that values a defense at a whole season's points per week, which
    makes it look untouchable in any drop comparison.
    """
    players = {"SEA": player("SEA", "DEF", "Seattle Seahawks", team="SEA")}
    built = proj.build(players=players, scoring=PPR, sleeper_week=[],
                       sleeper_season=[sleeper_row("SEA", {"sack": 45.0, "gp": 1.0})])
    assert built["SEA"].season_per_game == pytest.approx(45.0 / 18.0)


def test_a_fresh_week_far_above_the_season_pace_is_a_role_change():
    """The signal that finds a back who just inherited a backfield."""
    players = {"1": player("1", "RB", "Back One")}
    built = proj.build(
        players=players, scoring=PPR,
        sleeper_week=[sleeper_row("1", {"rush_yd": 60.0})],
        espn_week=[espn_row("5", "Back One", "RB", "SF", {"rush_yd": 113.0})],
        sleeper_season=[sleeper_row("1", {"rush_yd": 234.0, "gp": 18.0})],
    )
    one = built["1"]
    assert one.season_per_game == pytest.approx(1.3)
    assert one.role_change == pytest.approx(11.3 - 1.3)  # ESPN's weekly, not the blend
    assert one.role_changed


# -- the playoff window -----------------------------------------------------------------


def test_the_playoff_window_comes_from_the_league_not_a_guess():
    six = {"playoff_week_start": 15, "playoff_teams": 6, "playoff_round_type": 0}
    assert proj.playoff_weeks(six) == (15, 16, 17)  # bye for the top two, three rounds
    assert proj.playoff_weeks({**six, "playoff_teams": 4}) == (15, 16)
    assert proj.playoff_weeks({**six, "playoff_teams": 8, "playoff_round_type": 1}) == (15, 16, 17, 18)
    assert proj.playoff_weeks({**six, "playoff_round_type": 2}) == (15, 16, 17, 18)


def test_the_playoff_window_stops_at_the_end_of_the_season():
    assert proj.playoff_weeks({"playoff_week_start": 17, "playoff_teams": 8}) == (17, 18)


def test_a_league_with_no_playoff_settings_has_no_window():
    assert proj.playoff_weeks({}) == ()
    assert proj.playoff_weeks({"playoff_week_start": "nonsense"}) == ()


def test_the_playoff_number_is_a_mean_over_the_window():
    players = {"1": player("1", "WR", "Wide One")}
    per_game = proj.window_per_game(
        players=players, scoring=PPR, weeks=[15, 16, 17],
        sleeper_weeks={
            15: [sleeper_row("1", {"rec": 10.0})],
            16: [sleeper_row("1", {"rec": 20.0})],
            17: [sleeper_row("1", {"rec": 30.0})],
        },
    )
    assert per_game["1"] == pytest.approx(20.0)


def test_a_week_nobody_projected_is_not_counted_as_a_zero():
    """Averaging in a missing week would punish a player for a gap in the feed."""
    players = {"1": player("1", "WR", "Wide One")}
    per_game = proj.window_per_game(
        players=players, scoring=PPR, weeks=[15, 16, 17],
        sleeper_weeks={15: [sleeper_row("1", {"rec": 10.0})], 16: [], 17: []},
    )
    assert per_game["1"] == pytest.approx(10.0)


def test_the_playoff_number_blends_both_sources_like_any_other_week():
    players = {"1": player("1", "WR", "Wide One")}
    per_game = proj.window_per_game(
        players=players, scoring=PPR, weeks=[15],
        sleeper_weeks={15: [sleeper_row("1", {"rec": 10.0})]},
        espn_weeks={15: [espn_row("5", "Wide One", "WR", "SF", {"rec": 20.0})]},
    )
    assert per_game["1"] == pytest.approx(15.0)


def test_the_playoff_figure_rides_along_on_the_projection():
    players = {"1": player("1", "WR", "Wide One")}
    built = proj.build(players=players, scoring=PPR,
                       sleeper_week=[sleeper_row("1", {"rec": 5.0})],
                       playoff={"1": 12.5})
    assert built["1"].playoff_per_game == 12.5
    assert built["1"].points == 5.0  # and it does not disturb this week


def test_several_weeks_are_read_out_of_one_espn_response():
    """The payload already carries every remaining week, so this must not refetch."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"players": [{
            "id": 1,
            "player": {"fullName": "A Back", "defaultPositionId": 2, "proTeamId": 25, "stats": [
                {"statSourceId": 1, "statSplitTypeId": 1, "scoringPeriodId": wk,
                 "seasonId": 2026, "appliedTotal": wk, "stats": {"24": wk * 10}}
                for wk in (9, 15, 16, 17)
            ]},
        }]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        weeks = fetch_weeks(2026, [9, 15, 16, 17], http=client, pages=1)
    assert len(calls) == 1
    assert {w: rows[0].stats["rush_yd"] for w, rows in weeks.items()} == {
        9: 90.0, 15: 150.0, 16: 160.0, 17: 170.0
    }
