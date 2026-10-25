"""Free agent or on waivers: Sleeper's waiver rules, applied to one league right now.

Sleeper's public API has no waiver-status field, and an unrostered player is not
necessarily a free agent. With custom daily waivers off, two rules put him on waivers, and
he clears at whichever ends later:

1. After-game lock: once his NFL team kicks off, he stays on waivers until the league's
   clear day at 12:05 AM Pacific (`waiver_day_of_week`; "Tue After Day" clears Wednesday).
2. Post-drop hold: a dropped player stays on waivers for `waiver_clear_days` days less an
   hour (Sleeper's "2 days" is 47 hours), and claims on him process when the hold ends.

Both rules were checked against a season of real transactions in two leagues: no free-agent
add fell inside either window, and post-drop claims processed 47.0 hours after the drop.
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from ..models import Player, to_int

PACIFIC = ZoneInfo("America/Los_Angeles")
CLEAR_TIME = time(0, 5)  # after-game waivers clear at 12:05 AM Pacific
DROP_HOLD_SHORTFALL = timedelta(hours=1)  # "2 days" held players for 47.0h in real leagues
LAST_REGULAR_WEEK = 18

FREE_AGENT = "free_agent"
WAIVERS = "waivers"
ROSTERED = "rostered"


class UnsupportedWaivers(Exception):
    """Waiver settings this module doesn't model. Guessing would mislabel players."""


@dataclass(frozen=True)
class WaiverRules:
    clear_weekday: int  # Python weekday (Monday=0) whose 12:05 AM Pacific clears the lock
    drop_hold: timedelta

    @classmethod
    def from_settings(cls, settings: Mapping[str, Any]) -> "WaiverRules":
        if to_int(settings.get("daily_waivers")):
            raise UnsupportedWaivers("This league uses custom daily waivers, which ffa doesn't model yet.")
        day = to_int(settings.get("waiver_day_of_week"))
        if day is None or not 0 <= day <= 6:
            raise UnsupportedWaivers(
                f"Unrecognized waiver_day_of_week {settings.get('waiver_day_of_week')!r}."
            )
        # 2 ("Tue After Day") clears at 12:05 AM Pacific on Wednesday, Python weekday 2. The
        # other values are assumed to follow the same numbering; only 2 is verified.
        days = to_int(settings.get("waiver_clear_days")) or 0
        hold = timedelta(days=days) - DROP_HOLD_SHORTFALL if days > 0 else timedelta(0)
        return cls(clear_weekday=day, drop_hold=hold)

    def after_game_clear(self, kickoff: datetime) -> datetime:
        """When a player whose team kicked off at `kickoff` comes off waivers."""
        local = kickoff.astimezone(PACIFIC)
        days_ahead = (self.clear_weekday - local.weekday()) % 7
        clear = datetime.combine(local.date() + timedelta(days=days_ahead), CLEAR_TIME, PACIFIC)
        return clear if clear > kickoff else clear + timedelta(days=7)


@dataclass(frozen=True)
class WaiverStatus:
    player: Player
    status: str  # FREE_AGENT, WAIVERS or ROSTERED
    clears_at: datetime | None = None  # when a player on waivers becomes a free agent
    kicked_off: datetime | None = None  # set when the after-game lock applies
    dropped_at: datetime | None = None  # set when the post-drop hold applies
    rostered_by: str | None = None


def classify(
    player: Player,
    rules: WaiverRules,
    now: datetime,
    *,
    last_kickoff: datetime | None = None,
    dropped_at: datetime | None = None,
    rostered_by: str | None = None,
) -> WaiverStatus:
    """Apply both rules. `last_kickoff` is his team's most recent kickoff at or before now."""
    if rostered_by is not None:
        return WaiverStatus(player, ROSTERED, rostered_by=rostered_by)
    game_clear = rules.after_game_clear(last_kickoff) if last_kickoff is not None else None
    drop_clear = dropped_at + rules.drop_hold if dropped_at is not None else None
    locked = game_clear is not None and game_clear > now
    held = drop_clear is not None and drop_clear > now
    if not (locked or held):
        return WaiverStatus(player, FREE_AGENT)
    return WaiverStatus(
        player,
        WAIVERS,
        clears_at=max(clear for clear, on in ((game_clear, locked), (drop_clear, held)) if on),
        kicked_off=last_kickoff if locked else None,
        dropped_at=dropped_at if held else None,
    )


def last_kickoffs(weeks: Iterable[Mapping[str, datetime]], now: datetime) -> dict[str, datetime]:
    """Each team's most recent kickoff at or before `now`, across several weeks' schedules."""
    latest: dict[str, datetime] = {}
    for kickoffs in weeks:
        for team, kickoff in kickoffs.items():
            if kickoff <= now and (team not in latest or kickoff > latest[team]):
                latest[team] = kickoff
    return latest


def recent_drops(transactions: Iterable[Mapping[str, Any]], since: datetime) -> dict[str, datetime]:
    """Latest completed drop per player at or after `since`.

    A player dropped and re-added is rostered again, which the caller checks separately.
    """
    drops: dict[str, datetime] = {}
    for transaction in transactions:
        if transaction.get("status") != "complete":
            continue
        millis = to_int(transaction.get("status_updated"))
        if millis is None:
            continue
        when = datetime.fromtimestamp(millis / 1000, since.tzinfo)
        if when < since:
            continue
        for player_id in transaction.get("drops") or {}:
            if str(player_id) not in drops or when > drops[str(player_id)]:
                drops[str(player_id)] = when
    return drops


def roster_owners(rosters: Iterable[Mapping[str, Any]], users: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """player_id -> owning team's name, for every rostered player (IR and taxi included)."""
    names = {
        str(user.get("user_id")): (user.get("metadata") or {}).get("team_name") or user.get("display_name") or "?"
        for user in users
    }
    owners: dict[str, str] = {}
    for roster in rosters:
        owner = names.get(str(roster.get("owner_id")), f"roster {roster.get('roster_id')}")
        for player_id in (*(roster.get("players") or ()), *(roster.get("reserve") or ()), *(roster.get("taxi") or ())):
            owners[str(player_id)] = owner
    return owners


def select_statuses(
    statuses: Sequence[WaiverStatus],
    *,
    positions: Sequence[str] = (),
    names: Sequence[str] = (),
    limit: int | None = None,
) -> list[WaiverStatus]:
    """Named players (rostered or not), or else unrostered players on an NFL team, by search_rank."""
    if names:
        needles = [name.lower() for name in names]
        picked = [s for s in statuses if any(needle in s.player.name.lower() for needle in needles)]
    else:
        picked = [s for s in statuses if s.status != ROSTERED and s.player.team and s.player.active]
    if positions:
        wanted = {position.upper() for position in positions}
        picked = [s for s in picked if wanted.intersection(s.player.fantasy_positions)]
    picked.sort(key=lambda s: (s.player.search_rank is None, s.player.search_rank or 0, s.player.name))
    return picked[:limit] if limit else picked


class LeagueSource(Protocol):
    def league(self, league_id: str) -> dict[str, Any]: ...
    def league_users(self, league_id: str) -> list[dict[str, Any]]: ...
    def rosters(self, league_id: str) -> list[dict[str, Any]]: ...
    def transactions(self, league_id: str, week: int) -> list[dict[str, Any]]: ...
    def nfl_state(self) -> dict[str, Any]: ...
    def players(self) -> tuple[dict[str, dict[str, Any]], str]: ...


@dataclass(frozen=True)
class WaiverReport:
    league_name: str
    week: int
    now: datetime
    rules: WaiverRules
    statuses: list[WaiverStatus]  # every fantasy-relevant player, rostered ones included
    locked_teams: dict[str, datetime]  # team -> lock clear, for teams locked right now


def build_report(
    client: LeagueSource,
    league_id: str,
    now: datetime,
    kickoffs_for: Callable[[int, int], Mapping[str, datetime]],
) -> WaiverReport:
    """Fetch everything the rules need and classify every player in the player map."""
    league = client.league(league_id)
    rules = WaiverRules.from_settings(league.get("settings") or {})
    state = client.nfl_state()
    if state.get("season_type") != "regular":
        raise UnsupportedWaivers(
            f"Only the NFL regular season is modeled; Sleeper says it's {state.get('season_type')!r}."
        )
    season, week = int(state["season"]), to_int(state.get("week")) or 1
    # Transactions are filed under a week that runs roughly Wednesday to Tuesday, so a drop
    # inside the hold can sit in the previous week's list. Last week's late games can still
    # hold a lock, and next week's are fetched in case Sleeper's week hasn't flipped yet.
    weeks = [w for w in (week - 1, week, week + 1) if 1 <= w <= LAST_REGULAR_WEEK]
    kickoffs = last_kickoffs((kickoffs_for(season, w) for w in weeks), now)
    drops = recent_drops(
        (t for w in weeks for t in client.transactions(league_id, w)), since=now - rules.drop_hold
    )
    owners = roster_owners(client.rosters(league_id), client.league_users(league_id))
    players_raw, _ = client.players()

    statuses = []
    for player_id, raw in players_raw.items():
        player = Player.from_sleeper(player_id, raw)
        if player is None:
            continue
        statuses.append(classify(
            player,
            rules,
            now,
            last_kickoff=kickoffs.get(player.team) if player.team else None,
            dropped_at=drops.get(player.player_id),
            rostered_by=owners.get(player.player_id),
        ))
    locked = {team: clear for team, kickoff in kickoffs.items() if (clear := rules.after_game_clear(kickoff)) > now}
    return WaiverReport(league.get("name") or league_id, week, now, rules, statuses, locked)
