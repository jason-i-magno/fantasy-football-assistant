"""Weekly projections for one league's scoring, from two independent sources.

Neither source is used as published. Sleeper serves RotoWire's numbers scored under standard
PPR; ESPN serves its own under ESPN's default league. Both are re-scored here from raw stats
with the league's own `scoring_settings` (see `ffa/scoring.py`), so what comes out is
"points in *this* league", which is the only unit a lineup decision can be made in.

Why two sources, when blending costs a second fetch and a fuzzy join:

- **One source cannot be checked.** Sleeper's projections are RotoWire alone, so a bad
  RotoWire number is invisible from inside Sleeper. When the two disagree by `SPREAD_FLAG`
  points or more, `Projection.disputed` is set and the advice says so out loud rather than
  quietly averaging a five-point argument.
- **Season projections go stale in a way weekly ones don't.** Sleeper's season row reflects
  the preseason depth chart and is not revised when the starter ahead of a player goes down.
  Against live data one back's season row had him at 172 carries for the year while the same
  provider's weekly rows gave him 14.5 a game -- a committee back and a lead back, five
  points apart, from one feed. That is a bug if you average it in and a *feature* if you
  difference it: a player whose fresh rate is far above his stale season pace has taken over
  a role. `Projection.role_change` is that gap, and ranking by it is what surfaces the week's
  real waiver adds instead of the names everyone already owns.
  So the number *shown* as rest-of-season is `rest_per_game`, a fresh weekly blend over every
  week still to play, never the season row. The season row is now a last resort only, used
  when no feed projects a player in any week still to be played (`advice.Horizon.later`),
  where the alternative is scoring him zero and making him the obvious man to drop.
- **The house effects differ.** ESPN runs a couple of points per week hotter than Sleeper
  across the board. That is a scale difference, not staleness, and the two still rank
  players the same way, so the mean of the pair is a better point estimate than either and
  the orderings it produces are what the lineup solver consumes.

Byes come from the kickoff schedule, never from the feeds: both still publish a row for a
team on bye, Sleeper's with no stats and ESPN's with `appliedTotal` 0, so "no projection"
finds nothing. A team absent from the week's kickoffs is on bye, full stop.
"""

import logging
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .espn_fantasy import ESPNPlayer
from .models import Player
from .scoring import clean_projection, score_stats

log = logging.getLogger(__name__)

# Flag a player whose two sources disagree by at least this many points per week. A gap this
# wide means the recommendation rests on which source you believe, so it gets said.
SPREAD_FLAG = 5.0
# A weekly number this far above the stale season pace is a role change, not noise.
ROLE_CHANGE_FLAG = 4.0
# Games in a season projection, used when a row's own `gp` is unusable. Every skill-position
# season row reports 18.0; team defenses report 1.0 for all 32 of them, which would read a
# whole season's points as a single game's and value a defense at about 98 points a week.
SEASON_GAMES = 18.0
MIN_PLAUSIBLE_GAMES = 2.0
LAST_WEEK = 18  # the NFL regular season, and the last week any league can play
# Positions taken from Sleeper alone. ESPN reports made field goals under 40 yards as one
# bucket where leagues score 0-19, 20-29 and 30-39 separately, so its kicker rows cannot be
# re-scored honestly. A kicker is a fixed slot anyway: there is no decision in it.
SLEEPER_ONLY_POSITIONS = frozenset({"K"})

_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "v"})
_PUNCTUATION = re.compile(r"[^a-z0-9 ]")


def normalize_name(name: str) -> str:
    """A name reduced to what both feeds agree on.

    Suffixes have to go: ESPN and Sleeper disagree about them often enough to silently lose
    players like Harold Fannin Jr. and Luther Burden III, and a lost join looks exactly like
    a missing projection.
    """
    flat = _PUNCTUATION.sub("", (name or "").lower())
    parts = [p for p in flat.split() if p and p not in _SUFFIXES]
    return " ".join(parts)


@dataclass(frozen=True, slots=True)
class Projection:
    """What one player is worth next week, in this league's points."""

    player_id: str
    position: str
    team: str | None
    points: float  # the blended number the lineup math uses
    sleeper: float | None = None
    espn: float | None = None
    season_per_game: float | None = None  # Sleeper's season total over its projected games
    rest_per_game: float | None = None  # fresh weekly blend over every week still to play
    rest_sources: tuple[str, ...] = ()  # which feeds stand behind that blend
    playoff_per_game: float | None = None  # average over the league's playoff weeks
    playoff_sources: tuple[str, ...] = ()
    on_bye: bool = False  # his team has no game this week
    no_team: bool = False  # not on an NFL roster at all, which is not the same as a bye
    sources: tuple[str, ...] = ()

    @property
    def playable(self) -> bool:
        """False when nothing he does this week can score, whatever the feeds say."""
        return not (self.on_bye or self.no_team)

    @property
    def spread(self) -> float | None:
        """How far apart the two sources are, when both have an opinion."""
        if self.sleeper is None or self.espn is None:
            return None
        return abs(self.sleeper - self.espn)

    @property
    def disputed(self) -> bool:
        spread = self.spread
        return spread is not None and spread >= SPREAD_FLAG

    @property
    def role_change(self) -> float | None:
        """Fresh weekly rate less the stale season pace: positive means a bigger role.

        Measured against every week still to play rather than against the one in front of
        you. A single week is the wrong ruler twice over: a hard matchup makes a lead back
        read like a committee back, and a player on a bye projects zero, so the check built
        to find a role change was blind to every rostered player on a bye -- which is the
        week his stale season row is the only number anyone can see.
        """
        if self.season_per_game is None:
            return None
        fresh = self.rest_per_game
        if fresh is None:
            fresh = self.espn if self.espn is not None else self.sleeper
        return None if fresh is None else fresh - self.season_per_game

    @property
    def role_changed(self) -> bool:
        gap = self.role_change
        return gap is not None and gap >= ROLE_CHANGE_FLAG

    @property
    def single_source(self) -> bool:
        return len(self.sources) == 1

    @property
    def forward_single_source(self) -> bool:
        """True when one feed alone stands behind the figures a roster move is judged on.

        Read off the rest-of-season window, which contains the playoff weeks: a player two
        feeds agree about over the season is not a player one feed invented. It is a
        different question from `single_source`, which is about this Sunday -- a player can
        have both opinions this week and only one about December, which is exactly the
        shape of a backup the other feed has not written up yet.
        """
        return len(self.rest_sources) == 1


def _index_sleeper(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """player_id -> that row's stats, keeping only rows with real projected stats."""
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        player_id = row.get("player_id")
        stats = row.get("stats") or {}
        if not player_id or "pts_ppr" not in stats:
            continue  # the pool rows carry nothing but a draft-ADP field
        out[str(player_id)] = stats
    return out


def _index_espn(
    espn_rows: Iterable[ESPNPlayer], players: Mapping[str, Player]
) -> dict[str, ESPNPlayer]:
    """Resolve each ESPN row to a Sleeper player_id.

    Three joins, in order of how much they can be trusted: Sleeper's own `espn_id` where it
    is filled (under half the pool), then normalized name plus team, then normalized name
    and position when that is unique across the league. A team defense joins on its team,
    since ESPN gives it a synthetic id and a name Sleeper doesn't use.
    """
    by_espn_id: dict[str, str] = {}
    by_name_team: dict[tuple[str, str], list[str]] = {}
    by_name_pos: dict[tuple[str, str], list[str]] = {}
    for player_id, player in players.items():
        if player.is_defense:
            continue
        espn_id = player.espn_id
        if espn_id:
            by_espn_id[str(espn_id)] = player_id
        name = normalize_name(player.name)
        if not name:
            continue
        if player.team:
            by_name_team.setdefault((name, player.team), []).append(player_id)
        by_name_pos.setdefault((name, player.position), []).append(player_id)

    resolved: dict[str, ESPNPlayer] = {}
    unmatched = 0
    for row in espn_rows:
        if row.is_defense:
            if row.team and row.team in players:
                resolved[row.team] = row
            continue
        name = normalize_name(row.name)
        player_id = by_espn_id.get(row.espn_id)
        if player_id is None and row.team:
            candidates = by_name_team.get((name, row.team)) or []
            player_id = candidates[0] if len(candidates) == 1 else None
        if player_id is None:
            candidates = by_name_pos.get((name, row.position)) or []
            player_id = candidates[0] if len(candidates) == 1 else None
        if player_id is None:
            unmatched += 1
            continue
        resolved.setdefault(player_id, row)
    if unmatched:
        log.debug("ESPN rows with no Sleeper match: %s", unmatched)
    return resolved


def playoff_weeks(settings: Mapping[str, Any]) -> tuple[int, ...]:
    """The weeks a league's fantasy playoffs are played in.

    Derived rather than assumed: `playoff_week_start` says when they begin and the bracket
    size says how long they run, with `playoff_round_type` for the leagues that give the
    championship, or every round, two weeks. Capped at week 18, which is where the NFL
    regular season and every fantasy league end.
    """
    try:
        start = int(settings.get("playoff_week_start"))
    except (TypeError, ValueError):
        return ()
    if start < 1:
        return ()
    try:
        teams = int(settings.get("playoff_teams") or 0)
    except (TypeError, ValueError):
        teams = 0
    rounds = max(1, math.ceil(math.log2(teams))) if teams > 1 else 1
    try:
        round_type = int(settings.get("playoff_round_type") or 0)
    except (TypeError, ValueError):
        round_type = 0
    weeks = rounds + 1 if round_type == 1 else rounds * 2 if round_type == 2 else rounds
    return tuple(week for week in range(start, start + weeks) if week <= LAST_WEEK)


@dataclass(frozen=True, slots=True)
class WindowValue:
    """A per-game figure over a set of weeks, and what it rests on."""

    points: float
    sources: tuple[str, ...]  # every feed that contributed to any week in the window


def window_per_game(
    *,
    players: Mapping[str, Player],
    scoring: Mapping[str, Any],
    weeks: Sequence[int],
    sleeper_weeks: Mapping[int, Iterable[Mapping[str, Any]]] | None = None,
    espn_weeks: Mapping[int, Iterable[ESPNPlayer]] | None = None,
) -> dict[str, WindowValue]:
    """Average points per game over a set of weeks, blended the same way as any other week.

    A mean over the window, not a total: a player missing from one week's feed should not
    look worse than one the feed happens to cover, and the number is read as "per game".

    The sources are carried out with it because the mean hides them. A figure both feeds
    built and one ESPN built alone look identical as a number, and the second is the shape
    of a backup one feed has written up and the other has not -- worth saying out loud now
    that these figures decide drops and rank the watchlist.
    """
    sleeper_weeks = sleeper_weeks or {}
    espn_weeks = espn_weeks or {}
    totals: dict[str, list[float]] = {}
    seen: dict[str, set[str]] = {}
    for week in weeks:
        weekly = build(
            players=players,
            scoring=scoring,
            sleeper_week=sleeper_weeks.get(week, ()),
            espn_week=espn_weeks.get(week, ()),
        )
        for player_id, projection in weekly.items():
            if projection.sources:  # a week nobody projected him for is not a zero
                totals.setdefault(player_id, []).append(projection.points)
                seen.setdefault(player_id, set()).update(projection.sources)
    return {
        pid: WindowValue(points=sum(values) / len(values),
                         sources=tuple(sorted(seen.get(pid, ()))))
        for pid, values in totals.items() if values
    }


def _season_per_game(stats: Mapping[str, Any] | None, scoring: Mapping[str, Any]) -> float | None:
    """A *season* projection row expressed per game, or None when it has no usable total.

    Takes season rows only. A row whose `gp` is missing or implausibly small is still a
    season total -- team defenses all report 1.0 -- so it is divided by the season length
    the rest of the feed agrees on rather than taken at face value.
    """
    if not stats:
        return None
    try:
        games = float(stats.get("gp"))
    except (TypeError, ValueError):
        games = 0.0
    played = games if games >= MIN_PLAUSIBLE_GAMES else SEASON_GAMES
    return score_stats(stats, scoring) / played


def build(
    *,
    players: Mapping[str, Player],
    scoring: Mapping[str, Any],
    sleeper_week: Iterable[Mapping[str, Any]],
    sleeper_season: Iterable[Mapping[str, Any]] = (),
    espn_week: Iterable[ESPNPlayer] = (),
    playing_teams: frozenset[str] | None = None,
    rest: Mapping[str, "WindowValue"] | None = None,
    playoff: Mapping[str, "WindowValue"] | None = None,
) -> dict[str, Projection]:
    """A `Projection` for every player either source has a weekly number for.

    `playing_teams` is the set of teams with a kickoff in the target week; anyone whose team
    is absent is on bye and projects zero. Passing None skips bye handling, which is what
    the tests do when they aren't exercising it.
    """
    week_rows = _index_sleeper(sleeper_week)
    season_rows = _index_sleeper(sleeper_season)
    espn_by_id = _index_espn(espn_week, players)

    out: dict[str, Projection] = {}
    # Season-only players are included deliberately. One who is missing from this week's
    # feed still has a rest-of-season value, and leaving him out would give him none --
    # which reads as "worthless" and makes him the obvious man to drop.
    for player_id in set(week_rows) | set(espn_by_id) | set(season_rows):
        player = players.get(player_id)
        position = player.position if player is not None else None
        team = player.team if player is not None else None
        espn_row = espn_by_id.get(player_id)
        rest_window = (rest or {}).get(player_id)
        playoff_window = (playoff or {}).get(player_id)
        if position is None:
            if espn_row is None:
                continue  # a Sleeper row for someone outside the fantasy player map
            position, team = espn_row.position, espn_row.team

        # A player with no NFL team scores nothing, but he is not on a bye -- saying so
        # would be a different and wrong explanation on the lineup card.
        no_team = team is None
        on_bye = not no_team and playing_teams is not None and team not in playing_teams

        sleeper_points: float | None = None
        if (raw := week_rows.get(player_id)) is not None:
            sleeper_points = score_stats(clean_projection(raw, position), scoring)
        espn_points: float | None = None
        if espn_row is not None and espn_row.stats and position not in SLEEPER_ONLY_POSITIONS:
            espn_points = score_stats(espn_row.stats, scoring)

        if on_bye or no_team:
            points, sources = 0.0, ()
        else:
            available = [(n, v) for n, v in (("sleeper", sleeper_points), ("espn", espn_points))
                         if v is not None]
            sources = tuple(n for n, _ in available)
            points = sum(v for _, v in available) / len(available) if available else 0.0

        out[player_id] = Projection(
            player_id=player_id,
            position=position,
            team=team,
            points=points,
            sleeper=sleeper_points,
            espn=espn_points,
            season_per_game=_season_per_game(season_rows.get(player_id), scoring),
            rest_per_game=rest_window.points if rest_window is not None else None,
            rest_sources=rest_window.sources if rest_window is not None else (),
            playoff_per_game=playoff_window.points if playoff_window is not None else None,
            playoff_sources=playoff_window.sources if playoff_window is not None else (),
            on_bye=on_bye,
            no_team=no_team,
            sources=sources,
        )
    return out
