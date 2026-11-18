"""ESPN's fantasy player feed, used as a second opinion on weekly projections.

Sleeper's projections come from exactly one source (RotoWire), so a lineup call that rests
on them alone has no way to notice when that source is wrong or stale. ESPN publishes its
own weekly projections through the endpoint behind its public player list, with no key and
no login, which makes it an independent check.

Three things about this endpoint are not obvious, and all three cost real debugging once:

1. **Any stats filter silently removes the projections.** Including any `filterStatsFor*`
   key in the `X-Fantasy-Filter` header returns only `statSourceId: 0` rows -- actuals --
   with no error and no missing-field hint. The header must carry paging and sorting only.
2. **The payload holds two seasons at once.** Every player carries stat rows for the same
   `scoringPeriodId` in more than one `seasonId`, so matching on period alone returns *last*
   season's same-numbered week. `seasonId` has to be pinned.
3. **A team on bye still has a row**, with `appliedTotal` 0 and empty stats, so "no row"
   finds no byes. `ffa/projections.py` reads byes off the kickoff schedule instead.

One request carries every remaining week of the season, which is why `fetch_weeks` reads
several weeks out of a single response rather than asking per week. That is what makes a
playoff-window number free: weeks 15 to 17 are already in the payload fetched for week 9.
"""

import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any, NamedTuple

import httpx

from . import __version__
from .fanout import gather

log = logging.getLogger(__name__)

PLAYER_FEED_URL = (
    "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}"
    "/segments/0/leaguedefaults/3"
)
PAGE_SIZE = 200
DEFAULT_PAGES = 5  # ~1000 players by percent owned: every rostered player and every
                   # plausible waiver target. The whole pool is only ~1050.
PROJECTED = 1  # statSourceId: 1 projected, 0 actual
SEASON_SPLIT = 1  # statSplitTypeId: 1 is the single-week split

# ESPN's numeric pro-team ids. Verified against Sleeper's own team for every player who
# joins by `espn_id`: all 32 teams agreed unanimously. 0 means "no pro team".
PRO_TEAMS: dict[int, str] = {
    1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE", 6: "DAL", 7: "DEN", 8: "DET",
    9: "GB", 10: "TEN", 11: "IND", 12: "KC", 13: "LV", 14: "LAR", 15: "MIA", 16: "MIN",
    17: "NE", 18: "NO", 19: "NYG", 20: "NYJ", 21: "PHI", 22: "ARI", 23: "PIT", 24: "LAC",
    25: "SF", 26: "SEA", 27: "TB", 28: "WAS", 29: "CAR", 30: "JAX", 33: "BAL", 34: "HOU",
}
# defaultPositionId. Fullbacks (ESPN's own FB) come through as 2, which is what fantasy
# wants. 16 is a team defense, whose ESPN id is -16000 minus the pro-team id.
POSITIONS: dict[int, str] = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DEF"}
DST_POSITION_ID = 16

# ESPN stat id -> Sleeper stat key, so a projection can be re-scored with the league's own
# `scoring_settings`. Derived by matching ESPN's week-2 actuals against Sleeper's week-2 raw
# stats across 607 joined players; every id below agreed on all of them.
#
# ESPN repeats several stats under more than one id (41 == 53 receptions, 61 == 42 receiving
# yards, 40 == 24 rushing yards). Only one of each pair is listed, or the stat would score
# twice.
STAT_IDS: dict[int, str] = {
    3: "pass_yd", 4: "pass_td", 19: "pass_2pt", 20: "pass_int",
    23: "rush_att", 24: "rush_yd", 25: "rush_td", 26: "rush_2pt",
    53: "rec", 42: "rec_yd", 43: "rec_td", 44: "rec_2pt", 58: "rec_tgt",
    68: "fum", 72: "fum_lost",
    # Team defense. ESPN's points- and yards-allowed buckets are genuine probabilities that
    # sum to 1, unlike Sleeper's, which award the whole bucket containing the projected mean.
    99: "sack", 95: "int", 96: "fum_rec", 98: "safe", 97: "blk_kick", 106: "ff",
    94: "def_td",
    120: "pts_allow", 127: "yds_allow",
    89: "pts_allow_0", 90: "pts_allow_1_6", 91: "pts_allow_7_13",
    122: "pts_allow_21_27", 123: "pts_allow_28_34",
    128: "yds_allow_0_100", 129: "yds_allow_100_199", 130: "yds_allow_200_299",
    131: "yds_allow_300_349", 132: "yds_allow_350_399", 133: "yds_allow_400_449",
    134: "yds_allow_450_499", 135: "yds_allow_500_549", 136: "yds_allow_550p",
}
# Where ESPN splits one Sleeper tier across two ids, the values are added.
STAT_ID_GROUPS: dict[str, tuple[int, ...]] = {
    "pts_allow_14_20": (92, 121),
    "pts_allow_35p": (124, 125),
    "st_td": (93, 101, 102),  # kick, punt and other special-teams return touchdowns
}
# Kickers are deliberately absent. ESPN reports made field goals under 40 yards as a single
# bucket (id 80), while Sleeper and most leagues score 0-19, 20-29 and 30-39 separately, so
# there is no honest way to split it. `ffa/projections.py` takes kickers from Sleeper only,
# which costs nothing: a kicker is a fixed slot with no start-or-sit decision in it.


class ESPNFantasyError(Exception):
    """Any failure fetching or reading ESPN's player feed."""


@dataclass(frozen=True, slots=True)
class ESPNPlayer:
    espn_id: str
    name: str
    position: str
    team: str | None
    stats: dict[str, float]  # keyed by Sleeper stat key
    applied_total: float | None  # ESPN's own points, under ESPN's default scoring

    @property
    def is_defense(self) -> bool:
        return self.position == "DEF"


def _filter_header(offset: int, limit: int) -> str:
    """The paging header. Deliberately carries no stats filter -- see the module docstring."""
    return json.dumps(
        {"players": {"sortPercOwned": {"sortAsc": False, "sortPriority": 1},
                     "limit": limit, "offset": offset}}
    )


def _weekly_stats(raw: Mapping[str, Any], season: int,
                  weeks: Iterable[int]) -> dict[int, dict[str, Any]]:
    """The projected stat rows for several weeks, from one pass over the player's stats.

    A player carries a few dozen stat rows and this used to be walked once per week asked
    for, which over a thousand players and five weeks is most of a second spent re-reading
    the same list. First match wins, as it did when each week scanned alone.
    """
    wanted = set(weeks)
    found: dict[int, dict[str, Any]] = {}
    for entry in raw.get("stats") or ():
        week = entry.get("scoringPeriodId")
        if (
            week in wanted
            and entry.get("statSourceId") == PROJECTED
            and entry.get("statSplitTypeId") == SEASON_SPLIT
            and entry.get("seasonId") == season
        ):
            found.setdefault(week, entry)
    return found


def _week_stats(raw: Mapping[str, Any], season: int, week: int) -> dict[str, Any] | None:
    """The projected stat row for one week, with the season pinned."""
    return _weekly_stats(raw, season, (week,)).get(week)


def translate_stats(espn_stats: Mapping[str, Any] | None) -> dict[str, float]:
    """ESPN's numeric stat ids rendered as Sleeper stat keys."""
    if not espn_stats:
        return {}
    numbers: dict[int, float] = {}
    for key, value in espn_stats.items():
        try:
            numbers[int(key)] = float(value)
        except (TypeError, ValueError):
            continue
    out: dict[str, float] = {}
    for stat_id, name in STAT_IDS.items():
        if stat_id in numbers:
            out[name] = numbers[stat_id]
    for name, ids in STAT_ID_GROUPS.items():
        parts = [numbers[i] for i in ids if i in numbers]
        if parts:
            out[name] = sum(parts)
    return out


class _Identity(NamedTuple):
    """The half of a row that does not change with the week."""

    raw: Mapping[str, Any]
    espn_id: str
    name: str
    position: str
    team: str | None


def _identity(row: Mapping[str, Any]) -> _Identity | None:
    """Who this row is, or None when it has no usable identity or position."""
    raw = row.get("player") or {}
    position = POSITIONS.get(raw.get("defaultPositionId"))
    espn_id = row.get("id")
    if position is None or espn_id is None:
        return None
    return _Identity(
        raw=raw,
        espn_id=str(espn_id),
        name=str(raw.get("fullName") or "").strip(),
        position=position,
        team=PRO_TEAMS.get(raw.get("proTeamId")),
    )


def _player_for(who: _Identity, entry: Mapping[str, Any] | None) -> ESPNPlayer:
    applied = entry.get("appliedTotal") if entry else None
    return ESPNPlayer(
        espn_id=who.espn_id,
        name=who.name,
        position=who.position,
        team=who.team,
        stats=translate_stats(entry.get("stats") if entry else None),
        applied_total=float(applied) if isinstance(applied, (int, float)) else None,
    )


def parse_player(row: Mapping[str, Any], season: int, week: int) -> ESPNPlayer | None:
    """One `players[]` entry, or None when it has no usable identity or position."""
    who = _identity(row)
    return None if who is None else _player_for(who, _week_stats(row.get("player") or {}, season, week))


def _page(client: httpx.Client, url: str, week: int, offset: int,
          page_size: int) -> list[Any]:
    """One page of the player feed, as rows. An empty page ends the walk."""
    try:
        response = client.get(
            url,
            params={"view": "kona_player_info", "scoringPeriodId": week},
            headers={"X-Fantasy-Filter": _filter_header(offset, page_size)},
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as exc:
        raise ESPNFantasyError(f"Could not reach ESPN for week {week} projections: {exc}") from exc
    except ValueError as exc:
        raise ESPNFantasyError(f"ESPN sent invalid JSON for week {week} projections.") from exc
    rows = payload.get("players") if isinstance(payload, dict) else None
    return rows if isinstance(rows, list) else []


def fetch_weeks(
    season: int,
    weeks: Sequence[int],
    *,
    http: httpx.Client | None = None,
    pages: int = DEFAULT_PAGES,
    page_size: int = PAGE_SIZE,
) -> dict[int, list[ESPNPlayer]]:
    """ESPN's projections for several weeks, from one pass over the player feed.

    The response to a single request carries every remaining week, so asking for the coming
    week and the three playoff weeks costs exactly what asking for one costs. Pages stop
    early once one comes back short, so a smaller pool than `pages` asks for wastes at most
    one request.
    """
    wanted = sorted({int(week) for week in weeks})
    if not wanted:
        return {}
    client = http or httpx.Client(
        timeout=30.0, headers={"User-Agent": f"ffa/{__version__}", "Accept": "application/json"}
    )
    url = PLAYER_FEED_URL.format(season=season)
    out: dict[int, list[ESPNPlayer]] = {week: [] for week in wanted}
    first = wanted[0]
    budget = max(1, pages)
    try:
        # Every page asks the same question with a different offset, so they all go at
        # once. This used to fetch the first alone and the rest only if it came back
        # full, which saved four requests in a case that never happens -- ESPN's pool is
        # a thousand players deep every week of the season -- and cost 0.66s of every
        # single build, measured inside one, because the first page is as slow as the
        # other four together. The budget is therefore what is *fetched*, not a ceiling
        # a short page cuts short; the loop below still stops reading at a short page,
        # which is what decides the answer.
        fetched = gather({
            page: partial(_page, client, url, first, page * page_size, page_size)
            for page in range(budget)
        })
        for page in range(budget):
            rows = fetched.get(page) or ()
            if not rows:
                break
            for row in rows:
                if not isinstance(row, dict):
                    continue
                who = _identity(row)
                if who is None:
                    continue
                # One pass over this player's stats for every week asked for, rather than
                # one pass per week: the row is read once and sliced.
                entries = _weekly_stats(who.raw, season, wanted)
                for week in wanted:
                    out[week].append(_player_for(who, entries.get(week)))
            if len(rows) < page_size:
                break
    finally:
        if http is None:
            client.close()
    log.debug("ESPN weeks %s: parsed %s", wanted, {w: len(v) for w, v in out.items()})
    return out


def week_projections(season: int, week: int, **kwargs) -> list[ESPNPlayer]:
    """ESPN's projections for one week, most-owned players first."""
    return fetch_weeks(season, [week], **kwargs).get(week, [])
