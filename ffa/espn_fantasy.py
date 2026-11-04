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

One request carries every remaining week of the season, which is why `week_projections`
takes the week it wants and reads it out of the response rather than asking per week.
"""

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from . import __version__

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


def _week_stats(raw: Mapping[str, Any], season: int, week: int) -> dict[str, Any] | None:
    """The projected stat row for one week, with the season pinned."""
    for entry in raw.get("stats") or ():
        if (
            entry.get("statSourceId") == PROJECTED
            and entry.get("statSplitTypeId") == SEASON_SPLIT
            and entry.get("scoringPeriodId") == week
            and entry.get("seasonId") == season
        ):
            return entry
    return None


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


def parse_player(row: Mapping[str, Any], season: int, week: int) -> ESPNPlayer | None:
    """One `players[]` entry, or None when it has no usable identity or position."""
    raw = row.get("player") or {}
    position = POSITIONS.get(raw.get("defaultPositionId"))
    espn_id = row.get("id")
    if position is None or espn_id is None:
        return None
    entry = _week_stats(raw, season, week)
    team = PRO_TEAMS.get(raw.get("proTeamId"))
    applied = entry.get("appliedTotal") if entry else None
    return ESPNPlayer(
        espn_id=str(espn_id),
        name=str(raw.get("fullName") or "").strip(),
        position=position,
        team=team,
        stats=translate_stats(entry.get("stats") if entry else None),
        applied_total=float(applied) if isinstance(applied, (int, float)) else None,
    )


def week_projections(
    season: int,
    week: int,
    *,
    http: httpx.Client | None = None,
    pages: int = DEFAULT_PAGES,
    page_size: int = PAGE_SIZE,
) -> list[ESPNPlayer]:
    """ESPN's projections for one week, most-owned players first.

    Pages stop early once a page comes back short, so a smaller pool than `pages` asks for
    costs one wasted request at most.
    """
    client = http or httpx.Client(
        timeout=30.0, headers={"User-Agent": f"ffa/{__version__}", "Accept": "application/json"}
    )
    url = PLAYER_FEED_URL.format(season=season)
    out: list[ESPNPlayer] = []
    try:
        for page in range(max(1, pages)):
            offset = page * page_size
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
            if not rows:
                break
            for row in rows:
                if isinstance(row, dict) and (parsed := parse_player(row, season, week)) is not None:
                    out.append(parsed)
            if len(rows) < page_size:
                break
    finally:
        if http is None:
            client.close()
    log.debug("ESPN week %s: parsed %s players", week, len(out))
    return out
