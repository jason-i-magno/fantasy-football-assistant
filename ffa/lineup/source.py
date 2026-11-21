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
from functools import partial

import httpx

from ..espn import week_kickoffs
from ..espn_fantasy import ESPNFantasyError, ESPNPlayer, fetch_weeks
from ..fanout import gather, start
from ..models import Player, to_int
from ..playoffs import regular_season_end, scored_weeks, undecided_weeks
from ..projections import (
    LAST_WEEK,
    Projection,
    average_per_game,
    build,
    playoff_weeks,
    weekly_projections,
)
from ..sleeper import SleeperClient
from ..waivers.status import LAST_REGULAR_WEEK, build_report
from .advice import Horizon, LineupAdvice, build_advice, target_week
from .league import LeagueView, build_league_view

log = logging.getLogger(__name__)

# How far ahead to look for the week that still has games left. Two is enough: the only time
# the current week has no games to come is after its last kickoff, and then the next one does.
WEEKS_AHEAD = 2
# The build's one fan-out covers five feeds at once -- the waiver report, Sleeper's weekly
# and season projections, the fixtures still to come and the scores already in -- so it is
# given more room than a single feed's pages get. Measured on the real wave of 22 jobs:
# six at a time took 1.51s and twelve took 0.52s, because one slow job in each of six slots
# leaves everything else queued behind it.
FETCH_WORKERS = 12


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
) -> tuple[LineupAdvice, LeagueView, LineupSources]:
    """Build this week's advice for `username` in `league_id`, and every team's card.

    One build for both: the league view needs the same projections, the same kickoffs and
    the same week, and the only request it adds is the league's user list. Splitting them
    would double the expensive half of this function to save a cached call.
    """
    state = client.nfl_state()
    season = int(state.get("season") or now.year)
    # The waiver report derives its own week from this same cached `nfl_state`, so reading
    # it here costs nothing and removes the only reason the report had to finish first.
    # It is used to pick which weeks of scoreboard to fetch and never as the target week,
    # which `target_week` decides from the kickoffs themselves -- `nfl_state.week` has read
    # 9 while the app still showed week 8.
    base = to_int(state.get("week")) or 1

    # The league first, because the ESPN window below needs to know the last week this
    # league plays -- one cached request, and it is wanted a few lines later regardless.
    league = client.league(league_id)
    scoring = league.get("scoring_settings") or {}
    if not scoring:
        raise LineupUnavailable("This league publishes no scoring settings, so nothing can be projected.")
    settings = league.get("settings") or {}

    # ESPN's player feed is the longest single thing in a build, and the only one that
    # does not need to know the target week: it needs a *floor* for the weeks to read, and
    # no kickoff can put us earlier than the week before Sleeper's. Started here and
    # collected at the end, it runs underneath the rest of the build instead of after it.
    # The weeks read out of it are sliced by the real target week once that is known, so
    # this is a wider net and never a different answer -- checked against ESPN: the same
    # request carrying a different `scoringPeriodId` returns identical rows for every week
    # either one asks about.
    # The feed's pages are revalidated rather than re-downloaded; `cache_dir` is where
    # last build's copies live, beside the player map.
    fetch_espn = espn_for or (
        lambda s, w: fetch_weeks(s, w, http=http, cache_dir=client.cache_dir))
    # Every week a kickoff could put this build in, through the last one the league plays.
    # Both feeds are asked for this rather than for the target week, because the target
    # week is not known yet and this is the smallest range certain to contain it; what
    # comes back is sliced once it is. Over-fetching is one or two small weekly requests;
    # waiting instead costs a whole round trip to ESPN's scoreboard.
    forward_weeks = tuple(range(max(1, base - 1), _final_week(settings) + 1)) or (base,)
    espn_ready = start(partial(_espn_or_reason, fetch_espn, season, forward_weeks))

    # Every scoreboard at once -- a quarter of a second each, and they were fetched one
    # week at a time. The waiver report wants three of these same weeks, so it is handed
    # what has already been fetched instead of asking ESPN again: running it alongside
    # them instead only looked faster, because both halves then raced for week 9 and
    # fetched it twice.
    wanted = range(max(1, base - 1), min(LAST_REGULAR_WEEK, base + WEEKS_AHEAD) + 1)
    schedules_ready = start(partial(
        gather, {w: partial(kickoffs_for, season, w) for w in wanted}))

    def kickoffs_in_hand(for_season: int, for_week: int) -> Mapping[str, datetime]:
        """A week already fetched, or a request for one nobody expected to be asked for.

        The report runs as a job in the wave and reads the scoreboards through here, so
        this waits for them rather than fetching its own: both halves want the same three
        weeks, and when they each fetched them the saving was three requests in the bin.
        """
        schedules = schedules_ready()
        if for_season == season and for_week in schedules:
            return schedules[for_week]
        return kickoffs_for(for_season, for_week)

    # The waiver report goes in the wave below rather than before it. Nothing between here
    # and there needs it -- the target week comes from the scoreboards, and the report is
    # not read until the advice is built -- and it is a third of a second of fetching that
    # was holding everything else up.
    waivers = partial(build_report, client, league_id, now, kickoffs_in_hand)

    raw_players, _ = client.players()
    players = {
        pid: player
        for pid, raw in raw_players.items()
        if (player := Player.from_sleeper(pid, raw)) is not None
    }

    open_weeks = undecided_weeks(settings, final_week=regular_season_end(settings))
    played = scored_weeks(settings, final_week=regular_season_end(settings))

    # Everything the target week makes fetchable, fetched together. These are four feeds
    # that have nothing to say to each other -- ESPN's player pages, Sleeper's projections
    # for each week still to be played, the fixtures still to come, and the scores already
    # in -- and waiting for each in turn was most of a cold build. Keys are distinct, so
    # no two jobs race for the same cached response.
    jobs: dict[tuple[str, int], Callable[[], object]] = {
        ("report", 0): waivers,
        ("season", 0): partial(client.projections_season, str(season)),
    }
    jobs.update({("sleeper", w): partial(client.projections_week, str(season), w)
                 for w in forward_weeks})
    # The weeks whose results are still to come, for the playoff race; and the weeks
    # already played, which is what each team actually scores.
    jobs.update({("fixture", w): partial(_matchups, client, league_id, w)
                 for w in open_weeks})
    jobs.update({("scored", w): partial(_matchups, client, league_id, w) for w in played})
    done = gather(jobs, workers=FETCH_WORKERS)

    # Only now is the week known, and everything above gets sliced to it. The scoreboards
    # arrived long ago: the report in the wave was waiting on them.
    schedules = schedules_ready()
    week = target_week(schedules, now)
    if week is None:
        raise LineupUnavailable(
            "No NFL games are left in the weeks around Sleeper's current week, so there is "
            "no lineup to set. Check back next season."
        )
    kickoffs = schedules[week]
    # Two windows, both built only from weeks still to be played. The fantasy playoffs are
    # a separate question from the rest of the season -- a player can be worth a point a
    # week in one and nothing in the other, because the schedule is different -- and
    # neither of them is Sleeper's season row, which was written in August.
    window = tuple(w for w in playoff_weeks(settings) if w >= week)
    rest = tuple(range(week, _final_week(settings) + 1))

    # Collected last: by now it has had the scoreboards, the report and the whole wave
    # above to finish in.
    report = done[("report", 0)]
    espn_weeks, espn_error = espn_ready()
    sleeper_by_week = {w: done[("sleeper", w)] for w in rest}
    sleeper_week = sleeper_by_week.get(week) or client.projections_week(str(season), week)
    sleeper_season = done[("season", 0)]
    schedule = {w: done[("fixture", w)] for w in open_weeks}
    history = {w: _week_points(done[("scored", w)]) for w in played}
    matchups = done.get(("fixture", week)) or _matchups(client, league_id, week)
    if espn_error is not None:
        # A second opinion is an improvement, not a dependency. Losing it costs the spread
        # check and the role-change gap, so say so rather than silently halving the inputs.
        log.warning("ESPN projections unavailable: %s", espn_error)
    sources = LineupSources(
        sleeper=bool(sleeper_week),
        espn=bool(espn_weeks.get(week)),
        espn_note=None if espn_error is None else str(espn_error),
        rest_weeks=rest, playoff_weeks=window,
    )
    espn_rows = espn_weeks.get(week, [])
    # One blend per week, shared by both windows. The playoff weeks are the last of the
    # weeks still to be played, so a window built on its own blended weeks 15 to 17 twice
    # -- a tenth of a second of doing the same arithmetic again.
    blended: dict[int, Mapping[str, Projection]] = {}
    # One announcement per bad row. The playoff weeks are the last of the weeks still to
    # be played, so a row dropped from the rest-of-season window is usually dropped from
    # the playoff one too, and saying it twice reads like two findings.
    reported: set[tuple[str, int]] = set()
    per_game: dict[str, dict] = {}
    for name, weeks in (("rest", rest), ("playoff", window)):
        if not weeks:
            continue
        weekly_projections(
            players=players,
            scoring=scoring,
            weeks=weeks,
            sleeper_weeks={w: sleeper_by_week.get(w, ()) for w in weeks},
            espn_weeks=espn_weeks,
            blended=blended,
        )
        per_game[name] = average_per_game(blended, weeks, reported)

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
    starters = _starters_for_week(matchups, user_id, rosters)

    kwargs = {} if max_adds is None else {"max_adds": max_adds}
    advice = build_advice(
        report=report, league=league, rosters=rosters, user_id=user_id,
        projections=projections, players=players, week=week, kickoffs=kickoffs, now=now,
        current_starters=starters, rest_weeks=rest, playoff_weeks=window, **kwargs,
    )
    view = build_league_view(
        league=league, rosters=rosters, users=client.league_users(league_id),
        matchups=matchups, projections=projections, players=players, week=week,
        kickoffs=kickoffs, now=now, my_roster_id=advice.roster_id,
        horizon=Horizon(rest_weeks=len(rest), playoff_weeks=len(window)),
        schedule=schedule, history=history,
    )
    return advice, view, sources


def _final_week(settings: Mapping) -> int:
    """The last week this league plays, so "rest of season" stops where the league does.

    Week 18 exists in the NFL and in almost no fantasy league: counting it would value a
    player on the strength of a week nobody in the league will ever start him.
    """
    weeks = playoff_weeks(settings)
    return max(weeks) if weeks else LAST_WEEK


def _espn_or_reason(fetch, season: int, weeks: Sequence[int]):
    """ESPN's weeks, or the reason there are none. Never raises.

    The soft failure has to happen inside the job: a fan-out raises the first exception any
    job raised, and losing ESPN is not a reason to lose the build.
    """
    try:
        return fetch(season, weeks), None
    except ESPNFantasyError as exc:
        return {}, exc


def _week_points(entries: Sequence[Mapping]) -> dict[int, float]:
    """One week's scores by roster. A roster with no entry simply isn't in it."""
    out: dict[int, float] = {}
    for entry in entries:
        roster_id = to_int(entry.get("roster_id"))
        if roster_id is not None:
            out[roster_id] = float(entry.get("points") or 0.0)
    return out


def _matchups(client: SleeperClient, league_id: str, week: int) -> list[dict]:
    """The week's matchups, or nothing. Sleeper has not always built them yet."""
    try:
        return client.matchups(league_id, week) or []
    except Exception as exc:  # a missing matchup is normal before Sleeper builds the week
        log.debug("No matchups for week %s: %s", week, exc)
        return []


def _starters_for_week(matchups, user_id: str, rosters) -> list[str] | None:
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
    for entry in matchups or ():
        if entry.get("roster_id") == roster_id:
            starters = [str(p) for p in (entry.get("starters") or ()) if p]
            return starters or None
    return None
