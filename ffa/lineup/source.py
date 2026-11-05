"""Fetching everything a week's lineup advice needs, once.

`advice.py` is pure so it can be tested against recorded payloads; this is the half that
touches the network. Both schedule and ESPN access are injected, so a test can drive the
whole tool with no sockets at all.

The fetch is deliberately wide and infrequent rather than narrow and repeated: a week's
projections change on the timescale of injury news, not of page refreshes, so callers cache
a built report (`ffa/web/state.py` keeps one for minutes at a time) instead of asking again.
"""

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

import httpx

from ..espn import week_kickoffs
from ..espn_fantasy import ESPNFantasyError, ESPNPlayer, fetch_weeks
from ..models import Player
from ..projections import LAST_WEEK, Projection, build, playoff_weeks, window_per_game
from ..sleeper import SleeperClient
from ..waivers.status import LAST_REGULAR_WEEK, build_report
from .advice import LineupAdvice, build_advice, target_week

log = logging.getLogger(__name__)

# How far ahead to look for the week that still has games left. Two is enough: the only time
# the current week has no games to come is after its last kickoff, and then the next one does.
WEEKS_AHEAD = 2


class LineupUnavailable(Exception):
    """The week's advice cannot be built, with a reason fit to show the user."""


@dataclass(frozen=True)
class LineupSources:
    """Which sources answered, for the card that says where the numbers came from."""

    sleeper: bool = False
    espn: bool = False
    espn_note: str | None = None
    rest_weeks: tuple[int, ...] = ()  # the window behind the per-game rest-of-season number
    playoff_weeks: tuple[int, ...] = ()  # the window behind the per-game playoff number


def load_advice(
    client: SleeperClient,
    *,
    league_id: str,
    username: str,
    now: datetime,
    kickoffs_for: Callable[[int, int], Mapping[str, datetime]] = week_kickoffs,
    espn_for: Callable[[int, Sequence[int]], dict[int, list[ESPNPlayer]]] | None = None,
    http: httpx.Client | None = None,
    max_adds: int | None = None,
) -> tuple[LineupAdvice, LineupSources]:
    """Build this week's advice for `username` in `league_id`."""
    report = build_report(client, league_id, now, kickoffs_for)
    state = client.nfl_state()
    season = int(state.get("season") or now.year)

    base = report.week
    schedules = {
        week: kickoffs_for(season, week)
        for week in range(max(1, base - 1), min(LAST_REGULAR_WEEK, base + WEEKS_AHEAD) + 1)
    }
    week = target_week(schedules, now)
    if week is None:
        raise LineupUnavailable(
            "No NFL games are left in the weeks around Sleeper's current week, so there is "
            "no lineup to set. Check back next season."
        )
    kickoffs = schedules[week]

    league = client.league(league_id)
    scoring = league.get("scoring_settings") or {}
    if not scoring:
        raise LineupUnavailable("This league publishes no scoring settings, so nothing can be projected.")

    raw_players, _ = client.players()
    players = {
        pid: player
        for pid, raw in raw_players.items()
        if (player := Player.from_sleeper(pid, raw)) is not None
    }

    sleeper_week = client.projections_week(str(season), week)
    sleeper_season = client.projections_season(str(season))

    # Two windows, both built only from weeks still to be played. The fantasy playoffs are
    # a separate question from the rest of the season -- a player can be worth a point a
    # week in one and nothing in the other, because the schedule is different -- and
    # neither of them is Sleeper's season row, which was written in August.
    settings = league.get("settings") or {}
    window = tuple(w for w in playoff_weeks(settings) if w >= week)
    rest = tuple(range(week, _final_week(settings) + 1))
    sources = LineupSources(sleeper=bool(sleeper_week), rest_weeks=rest, playoff_weeks=window)

    espn_weeks: dict[int, list[ESPNPlayer]] = {}
    # One ESPN request carries every remaining week, so both windows are free here.
    fetch_espn = espn_for or (lambda s, w: fetch_weeks(s, w, http=http))
    try:
        espn_weeks = fetch_espn(season, rest or (week,))
        sources = LineupSources(sleeper=sources.sleeper, espn=bool(espn_weeks.get(week)),
                                rest_weeks=rest, playoff_weeks=window)
    except ESPNFantasyError as exc:
        # A second opinion is an improvement, not a dependency. Losing it costs the spread
        # check and the role-change gap, so say so rather than silently halving the inputs.
        log.warning("ESPN projections unavailable: %s", exc)
        sources = LineupSources(sleeper=sources.sleeper, espn=False, espn_note=str(exc),
                                rest_weeks=rest, playoff_weeks=window)
    espn_rows = espn_weeks.get(week, [])

    # Sleeper is asked per week, so each week in the window costs a request; they are cached
    # for the run and the whole report is cached for minutes, so it is paid rarely. Fetched
    # once and sliced: the playoff weeks are a subset of the weeks still to be played.
    sleeper_by_week = {w: client.projections_week(str(season), w) for w in rest}
    per_game = {
        name: window_per_game(
            players=players,
            scoring=scoring,
            weeks=weeks,
            sleeper_weeks={w: sleeper_by_week.get(w, ()) for w in weeks},
            espn_weeks=espn_weeks,
        )
        for name, weeks in (("rest", rest), ("playoff", window))
        if weeks
    }

    projections: Mapping[str, Projection] = build(
        players=players,
        scoring=scoring,
        sleeper_week=sleeper_week,
        sleeper_season=sleeper_season,
        espn_week=espn_rows,
        playing_teams=frozenset(kickoffs),
        rest=per_game.get("rest", {}),
        playoff=per_game.get("playoff", {}),
    )
    if not projections:
        raise LineupUnavailable(
            f"Neither Sleeper nor ESPN published week {week} projections yet."
        )

    user = client.user(username)
    user_id = str(user.get("user_id") or "")
    rosters = client.rosters(league_id)
    starters = _starters_for_week(client, league_id, week, user_id, rosters)

    kwargs = {} if max_adds is None else {"max_adds": max_adds}
    advice = build_advice(
        report=report, league=league, rosters=rosters, user_id=user_id,
        projections=projections, players=players, week=week, kickoffs=kickoffs, now=now,
        current_starters=starters, **kwargs,
    )
    return advice, sources


def _final_week(settings: Mapping) -> int:
    """The last week this league plays, so "rest of season" stops where the league does.

    Week 18 exists in the NFL and in almost no fantasy league: counting it would value a
    player on the strength of a week nobody in the league will ever start him.
    """
    weeks = playoff_weeks(settings)
    return max(weeks) if weeks else LAST_WEEK


def _starters_for_week(
    client: SleeperClient,
    league_id: str,
    week: int,
    user_id: str,
    rosters,
) -> list[str] | None:
    """The lineup as set for the target week, preferring the week's own matchup.

    A roster's `starters` is the lineup for Sleeper's current week, which is not always the
    week being advised on. The matchup for the target week carries the lineup that will
    actually play, so it wins when it exists; before Sleeper creates it, the roster's own
    list is the best available answer.
    """
    roster_id = None
    for roster in rosters:
        owners = {str(roster.get("owner_id") or "")} | {str(c) for c in (roster.get("co_owners") or ())}
        if user_id in owners:
            roster_id = roster.get("roster_id")
            break
    if roster_id is None:
        return None
    try:
        matchups = client.matchups(league_id, week)
    except Exception as exc:  # a missing matchup is normal before Sleeper builds the week
        log.debug("No matchup for week %s: %s", week, exc)
        return None
    for entry in matchups or ():
        if entry.get("roster_id") == roster_id:
            starters = [str(p) for p in (entry.get("starters") or ()) if p]
            return starters or None
    return None
