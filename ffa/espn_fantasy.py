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
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
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
# Where revalidated pages live, under the cache directory the rest of the tool uses. The
# version is in the filename: a change to what is kept invalidates by not matching.
CACHE_SUBDIR = "espn"
CACHE_VERSION = 2
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


def _prune_cache(directory: Path, keep: str) -> None:
    """Drop pages from other weeks, and from older versions of what is kept.

    Only the current week's pages are ever revalidated again, and a page is megabytes, so
    last week's would sit there for the season. The version goes the same way: a bump
    means the old files can never be read again, and nothing else would ever delete them.
    """
    suffix = f".v{CACHE_VERSION}.page"
    try:
        for stale in directory.glob("*.page"):
            if not stale.name.startswith(keep) or not stale.name.endswith(suffix):
                stale.unlink(missing_ok=True)
    except OSError as exc:
        log.debug("Could not prune the ESPN page cache: %s", exc)


def _cache_file(cache_dir: Path, season: int, week: int, offset: int) -> Path:
    return (Path(cache_dir) / CACHE_SUBDIR
            / f"{season}-{week}-{offset}.v{CACHE_VERSION}.page")


class _Page(NamedTuple):
    """What a page is worth keeping: how many rows it held, and what we read out of them."""

    rows: int  # the page's own length, which is what decides whether the walk goes on
    players: list[dict[str, Any]]


def _extract(rows: Sequence[Any], season: int) -> _Page:
    """The parts of a page this tool ever reads, and nothing else.

    A page is four megabytes of ownership percentages, news, ratings and draft ranks around
    the few hundred bytes that matter. Keeping the page meant `json.loads` on 25MB every
    build -- as slow as the download it was meant to replace -- so what is kept is this:
    who the player is, and his projected stat line for each week the payload carries.
    """
    players: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        who = _identity(row)
        if who is None:
            continue
        weeks: dict[str, Any] = {}
        for entry in who.raw.get("stats") or ():
            if (
                entry.get("statSourceId") == PROJECTED
                and entry.get("statSplitTypeId") == SEASON_SPLIT
                and entry.get("seasonId") == season
                and isinstance(entry.get("scoringPeriodId"), int)
            ):
                # First row wins, as it did when each week was scanned for on its own.
                # Stats are translated here rather than on the way out: ESPN's numeric
                # ids become Sleeper's keys once, when the page is read, instead of for
                # every player in every week of every build.
                weeks.setdefault(str(entry["scoringPeriodId"]), {
                    "applied": entry.get("appliedTotal"),
                    "stats": translate_stats(entry.get("stats")),
                })
        players.append({"id": who.espn_id, "name": who.name, "pos": who.position,
                        "team": who.team, "weeks": weeks})
    return _Page(rows=len(rows), players=players)


def _players_for_week(page: _Page, week: int) -> list[ESPNPlayer]:
    """One week's players, built from an extract rather than from the page it came from.

    The stats arrive already translated, which is the whole of what this does per build:
    a thousand players times eight weeks is eight thousand dictionaries that used to be
    rebuilt from ESPN's stat ids every time, and the ids do not change between builds.
    """
    key = str(week)
    out = []
    for kept in page.players:
        entry = (kept.get("weeks") or {}).get(key)
        applied = entry.get("applied") if entry else None
        out.append(ESPNPlayer(
            espn_id=kept["id"],
            name=kept["name"],
            position=kept["pos"],
            team=kept["team"],
            stats=entry.get("stats") or {} if entry else {},
            applied_total=float(applied) if isinstance(applied, (int, float)) else None,
        ))
    return out


def _read_page_cache(path: Path) -> tuple[str | None, _Page] | None:
    """A page kept from last time, as `(etag, body)`, or None if there isn't a usable one.

    Anything wrong with the file -- missing, truncated, half-written, unreadable -- means
    no cache rather than an error. A cache is an optimisation and must never be a way for
    a build to fail.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    etag, sep, body = raw.partition(b"\n")
    if not sep or not body:
        return None
    try:
        kept = json.loads(body)
        page = _Page(rows=int(kept["rows"]), players=list(kept["players"]))
    except (ValueError, TypeError, KeyError):
        return None
    return (etag.decode("utf-8", "replace") or None), page


def _write_page_cache(path: Path, etag: str | None, page: _Page) -> None:
    """Keep a page for next time, written where a half-written file cannot be read.

    The pages of one build are fetched at once and two processes can be building at once,
    so the file is written beside its name and moved onto it, which is atomic on every
    filesystem this runs on.
    """
    if not etag:
        return  # nothing to revalidate against later, so nothing worth keeping
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps({"rows": page.rows, "players": page.players}).encode()
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temporary.write_bytes(etag.encode() + b"\n" + body)
        os.replace(temporary, path)
    except OSError as exc:  # a full or read-only disk is not a reason to fail a build
        log.debug("Could not cache ESPN page %s: %s", path.name, exc)


def _page(client: httpx.Client, url: str, season: int, week: int, offset: int,
          page_size: int, cache_dir: Path | None = None) -> _Page:
    """One page of the player feed, read down to what this tool uses.

    The feed is eleven megabytes a build and the slowest thing in one, so the page is
    revalidated rather than re-downloaded: ESPN sends an `ETag`, and answers a request
    carrying it with `304 Not Modified` in 0.02s and no body at all, against 0.23s and
    4.1MB for the first page alone. The server decides whether the data changed, so this
    is not a staleness trade -- a changed projection comes back as a 200 with the new
    body, exactly as if nothing were cached.
    """
    path = _cache_file(cache_dir, season, week, offset) if cache_dir is not None else None
    cached = _read_page_cache(path) if path is not None else None
    headers = {"X-Fantasy-Filter": _filter_header(offset, page_size)}
    if cached is not None and cached[0]:
        headers["If-None-Match"] = cached[0]
    try:
        response = client.get(
            url,
            params={"view": "kona_player_info", "scoringPeriodId": week},
            headers=headers,
        )
        if response.status_code == 304 and cached is not None:
            return cached[1]
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as exc:
        raise ESPNFantasyError(f"Could not reach ESPN for week {week} projections: {exc}") from exc
    except ValueError as exc:
        raise ESPNFantasyError(f"ESPN sent invalid JSON for week {week} projections.") from exc
    rows = payload.get("players") if isinstance(payload, dict) else None
    page = _extract(rows if isinstance(rows, list) else [], season)
    if path is not None:
        _write_page_cache(path, response.headers.get("etag"), page)
    return page


def fetch_weeks(
    season: int,
    weeks: Sequence[int],
    *,
    http: httpx.Client | None = None,
    pages: int = DEFAULT_PAGES,
    page_size: int = PAGE_SIZE,
    cache_dir: Path | None = None,
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
            page: partial(_page, client, url, season, first, page * page_size, page_size,
                          cache_dir)
            for page in range(budget)
        })
        if cache_dir is not None:
            _prune_cache(Path(cache_dir) / CACHE_SUBDIR, f"{season}-{first}-")
        for page in range(budget):
            kept = fetched.get(page)
            if kept is None or not kept.rows:
                break
            for week in wanted:
                out[week].extend(_players_for_week(kept, week))
            if kept.rows < page_size:
                break
    finally:
        if http is None:
            client.close()
    log.debug("ESPN weeks %s: parsed %s", wanted, {w: len(v) for w, v in out.items()})
    return out


def week_projections(season: int, week: int, **kwargs) -> list[ESPNPlayer]:
    """ESPN's projections for one week, most-owned players first."""
    return fetch_weeks(season, [week], **kwargs).get(week, [])
