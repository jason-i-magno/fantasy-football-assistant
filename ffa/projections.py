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
  That is a bug if you average it in and a *feature* if you difference it: a backup whose
  fresh weekly number is far above his stale season pace has just inherited a role.
  `Projection.role_change` is that gap, and ranking by it is what surfaces the week's real
  waiver adds instead of the names everyone already owns.
- **The house effects differ.** ESPN runs a couple of points per week hotter than Sleeper
  across the board. That is a scale difference, not staleness, and the two still rank
  players the same way, so the mean of the pair is a better point estimate than either and
  the orderings it produces are what the lineup solver consumes.

Byes come from the kickoff schedule, never from the feeds: both still publish a row for a
team on bye, Sleeper's with no stats and ESPN's with `appliedTotal` 0, so "no projection"
finds nothing. A team absent from the week's kickoffs is on bye, full stop.
"""

import logging
import re
from collections.abc import Iterable, Mapping
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
        """Fresh weekly number less the stale season pace: positive means a bigger role."""
        if self.season_per_game is None:
            return None
        fresh = self.espn if self.espn is not None else self.sleeper
        return None if fresh is None else fresh - self.season_per_game

    @property
    def role_changed(self) -> bool:
        gap = self.role_change
        return gap is not None and gap >= ROLE_CHANGE_FLAG

    @property
    def single_source(self) -> bool:
        return len(self.sources) == 1


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
            on_bye=on_bye,
            no_team=no_team,
            sources=sources,
        )
    return out
