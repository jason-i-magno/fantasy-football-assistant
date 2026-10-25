"""Thin Sleeper API client with an explicit cache policy per endpoint.

| Endpoint                       | Policy                                                      |
|--------------------------------|-------------------------------------------------------------|
| /user/<username>               | once per run                                                |
| /league/<id>, /users, /drafts  | once per run                                                |
| /draft/<id>                    | every DRAFT_REFRESH_SECONDS, and again whenever the pick    |
|                                | count changes so `last_picked` (the clock) is never stale   |
| /draft/<id>/picks              | never cached; fetched on every poll                         |
| /draft/<id>/traded_picks       | once, then again whenever the pick count changes            |
| /players/nfl?active=true       | once per calendar day on disk; never refetched mid-run      |

Errors: 5xx and network failures retry with exponential backoff. A 429 raises
`SleeperRateLimited` immediately so the caller can back off hard without freezing the live
UI. A 404 raises `SleeperNotFound` naming the ID.
"""

import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from . import __version__
from .models import FANTASY_POSITIONS

log = logging.getLogger(__name__)

BASE_URL = "https://api.sleeper.app/v1"
DRAFT_REFRESH_SECONDS = 30.0
PLAYERS_CACHE_FILE = "players_nfl.json"
# Fields kept from each /players/nfl entry; the other ~40 ID/bio fields are dropped.
PLAYER_FIELDS = (
    "player_id", "first_name", "last_name", "full_name", "position", "fantasy_positions",
    "team", "search_rank", "active", "status", "injury_status", "injury_body_part",
    "age", "years_exp", "depth_chart_position", "depth_chart_order", "number", "news_updated",
)


class SleeperError(Exception):
    """Any failure talking to Sleeper."""


class SleeperNotFound(SleeperError):
    """Sleeper has no such user, league, or draft."""


class SleeperUnavailable(SleeperError):
    """Network failure or 5xx that persisted through retries."""


class SleeperRateLimited(SleeperError):
    def __init__(self, message: str, retry_after: float | None):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class _Cached:
    fetched_at: float
    pick_count: int | None
    data: Any


def filter_players(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Keep fantasy-relevant players only, trimmed to PLAYER_FIELDS."""
    kept: dict[str, dict[str, Any]] = {}
    for player_id, entry in (raw or {}).items():
        if not isinstance(entry, dict):
            continue
        positions = set(entry.get("fantasy_positions") or ()) | {entry.get("position")}
        if positions.isdisjoint(FANTASY_POSITIONS):
            continue
        kept[str(player_id)] = {k: entry[k] for k in PLAYER_FIELDS if k in entry}
    return kept


class SleeperClient:
    def __init__(
        self,
        cache_dir: Path,
        *,
        http: httpx.Client | None = None,
        timeout: float = 10.0,
        max_retries: int = 3,
        backoff_seconds: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        today: Callable[[], date] = date.today,
    ):
        self.cache_dir = cache_dir
        self._http = http or httpx.Client(
            base_url=BASE_URL,
            timeout=timeout,
            headers={"User-Agent": f"ffa/{__version__}"},
        )
        self._max_retries = max_retries
        self._backoff = backoff_seconds
        self._sleep = sleep
        self._clock = clock
        self._today = today
        self._once: dict[str, Any] = {}
        self._drafts: dict[str, _Cached] = {}
        self._traded: dict[str, _Cached] = {}
        self._players: tuple[dict[str, dict[str, Any]], str] | None = None

    def close(self) -> None:
        self._http.close()

    # -- once per run ---------------------------------------------------------------

    def user(self, username: str) -> dict[str, Any]:
        data = self._once_per_run(
            f"user:{username.lower()}", f"/user/{quote(username)}", f"user '{username}'"
        )
        if not isinstance(data, dict) or not data.get("user_id"):
            # Sleeper answers an unknown username with HTTP 200 and a `null` body.
            raise SleeperNotFound(f"Sleeper user '{username}' was not found. Check SLEEPER_USERNAME.")
        return data

    def league(self, league_id: str) -> dict[str, Any]:
        data = self._once_per_run(f"league:{league_id}", f"/league/{league_id}", f"league {league_id}")
        if not isinstance(data, dict):
            raise SleeperNotFound(f"Sleeper league {league_id} was not found. Check SLEEPER_LEAGUE_ID.")
        return data

    def league_users(self, league_id: str) -> list[dict[str, Any]]:
        data = self._once_per_run(
            f"users:{league_id}", f"/league/{league_id}/users", f"users for league {league_id}"
        )
        return data if isinstance(data, list) else []

    def league_drafts(self, league_id: str) -> list[dict[str, Any]]:
        data = self._once_per_run(
            f"drafts:{league_id}", f"/league/{league_id}/drafts", f"drafts for league {league_id}"
        )
        return data if isinstance(data, list) else []

    # -- live board -----------------------------------------------------------------

    def draft(self, draft_id: str, *, pick_count: int | None = None) -> dict[str, Any]:
        cached = self._drafts.get(draft_id)
        now = self._clock()
        if (
            cached is not None
            and now - cached.fetched_at < DRAFT_REFRESH_SECONDS
            and pick_count == cached.pick_count
        ):
            return cached.data
        data = self._get_json(f"/draft/{draft_id}", what=f"draft {draft_id}")
        if not isinstance(data, dict):
            raise SleeperNotFound(f"Sleeper draft {draft_id} was not found. Check DRAFT_ID.")
        if not data.get("draft_id"):
            # A dict-shaped error or partial body. Don't cache it for 30s mid-draft.
            raise SleeperError(f"Sleeper sent an unusable draft payload for draft {draft_id}.")
        self._drafts[draft_id] = _Cached(now, pick_count, data)
        return data

    def picks(self, draft_id: str) -> list[dict[str, Any]]:
        data = self._get_json(f"/draft/{draft_id}/picks", what=f"picks for draft {draft_id}")
        return data if isinstance(data, list) else []

    def traded_picks(self, draft_id: str, *, pick_count: int | None = None) -> list[dict[str, Any]]:
        cached = self._traded.get(draft_id)
        if cached is not None and cached.pick_count == pick_count:
            return cached.data
        data = self._get_json(
            f"/draft/{draft_id}/traded_picks", what=f"traded picks for draft {draft_id}"
        )
        data = data if isinstance(data, list) else []
        self._traded[draft_id] = _Cached(self._clock(), pick_count, data)
        return data

    # -- player map -----------------------------------------------------------------

    def players(self) -> tuple[dict[str, dict[str, Any]], str]:
        """Return (player map, date stamp). Hits the network at most once per day and per run."""
        if self._players is not None:
            return self._players
        path = self.cache_dir / PLAYERS_CACHE_FILE
        today = self._today().isoformat()
        cached = _read_players_cache(path)
        if cached is not None and cached[1] == today:
            self._players = cached
            return cached
        try:
            raw = self._get_json("/players/nfl", params={"active": "true"}, what="the NFL player map")
        except SleeperError as exc:
            if cached is None:
                raise
            log.warning(
                "Could not refresh the player map (%s); using the stale copy from %s.", exc, cached[1]
            )
            self._players = cached
            return cached
        players = filter_players(raw if isinstance(raw, dict) else {})
        _write_players_cache(path, today, players)
        self._players = (players, today)
        return self._players

    # -- plumbing -------------------------------------------------------------------

    def _once_per_run(self, key: str, path: str, what: str) -> Any:
        if key not in self._once:
            self._once[key] = self._get_json(path, what=what)
        return self._once[key]

    def _get_json(self, path: str, *, what: str, params: dict[str, str] | None = None) -> Any:
        for attempt in range(self._max_retries + 1):
            final = attempt == self._max_retries
            try:
                response = self._http.get(path, params=params)
            except httpx.TransportError as exc:
                if final:
                    raise SleeperUnavailable(f"Could not reach Sleeper for {what}: {exc}") from exc
                self._sleep(self._backoff * 2**attempt)
                continue

            status = response.status_code
            if status == 404:
                raise SleeperNotFound(f"Sleeper returned 404 for {what}. Check that ID.")
            if status == 429:
                retry_after = _retry_after_seconds(response)
                log.warning("Sleeper rate limit (HTTP 429) while fetching %s; backing off.", what)
                raise SleeperRateLimited(f"Sleeper is rate limiting requests ({what}).", retry_after)
            if status >= 500:
                if final:
                    raise SleeperUnavailable(
                        f"Sleeper returned HTTP {status} for {what} after {attempt + 1} attempts."
                    )
                log.info("Sleeper HTTP %s for %s; retrying.", status, what)
                self._sleep(self._backoff * 2**attempt)
                continue
            if status >= 400:
                raise SleeperError(f"Sleeper returned HTTP {status} for {what}.")
            try:
                return response.json()
            except ValueError as exc:
                raise SleeperError(f"Sleeper sent invalid JSON for {what}.") from exc
        raise AssertionError("unreachable")


def _retry_after_seconds(response: httpx.Response) -> float | None:
    try:
        return float(response.headers["retry-after"])
    except (KeyError, ValueError):
        return None


def _read_players_cache(path: Path) -> tuple[dict[str, dict[str, Any]], str] | None:
    try:
        payload = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("Ignoring unreadable player cache %s: %s", path, exc)
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("players"), dict):
        return None
    return payload["players"], str(payload.get("date"))


def _write_players_cache(path: Path, stamp: str, players: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"date": stamp, "players": players}))
    os.replace(tmp, path)
