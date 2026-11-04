"""Server-side state: one draft session and one waiver report, shared by every request.

A phone polling every two seconds must not turn into a Sleeper request every two seconds,
and a second open tab must not double the traffic. Both feeds here keep the newest result
behind a lock and refuse to refetch inside their own interval, so Sleeper sees the same
cadence as `ffa draft watch` however many screens are watching. Nothing polls while nobody
is looking: a refresh happens on request, not on a timer.

The draft feed mirrors the error policy of the terminal loop (`cli._run_loop`): a rate limit
backs off hard, an ordinary Sleeper error retries with a note attached to the last good
view, and only a draft the pick math cannot model at all is fatal.
"""

import logging
import re
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .. import espn
from ..config import MIN_POLL_INTERVAL_SECONDS, Config, ConfigError
from ..draft import board
from ..draft.session import (
    DraftView,
    LiveSource,
    Session,
    SessionError,
    compute_view,
    load_exclusions,
    open_session,
)
from ..lineup.advice import LineupAdvice, NoRosterError
from ..lineup.source import LineupSources, LineupUnavailable, load_advice
from ..sleeper import SleeperClient, SleeperError, SleeperRateLimited
from ..waivers.status import UnsupportedWaivers, WaiverReport, build_report

log = logging.getLogger(__name__)

RATE_LIMIT_BACKOFF_SECONDS = 30.0
OPEN_RETRY_SECONDS = 15.0  # a league with no draft shouldn't be re-resolved on every request
WAIVERS_TTL_SECONDS = 60.0  # waiver windows move in hours; a minute-old report is current
WAIVERS_ERROR_TTL_SECONDS = 15.0  # don't retry a broken ESPN or Sleeper on every poll
# Projections move with injury news, not with page refreshes, and a build costs a few
# seconds and about fifty megabytes from ESPN. Ten minutes is far fresher than the data.
LINEUP_TTL_SECONDS = 600.0
LINEUP_ERROR_TTL_SECONDS = 30.0
LEAGUE_ID = re.compile(r"^\d{1,32}$")


class FeedUnavailable(Exception):
    """No usable data for this screen. The message is shown to the user as-is."""


@dataclass(frozen=True)
class DraftFrame:
    """One response's worth of draft state, plus whatever research has landed."""

    view: DraftView
    note: str | None
    research: Any
    research_status: str
    next_poll_in: float
    budget_left: int | None


def open_live(config: Config) -> tuple[Session, LiveSource]:
    """Resolve IDs once and start polling Sleeper, exactly as `watch` does."""
    client = SleeperClient(config.cache_dir)
    session = open_session(config, client)
    return session, LiveSource(client, session.draft_id)


class DraftFeed:
    """The draft board, polled no faster than the configured interval.

    `opener` is called on first use (and retried on failure) so the server starts even when
    the league has no draft yet — which is most of the year.
    """

    def __init__(
        self,
        config: Config,
        *,
        opener: Callable[[], tuple[Session, Any]] | None = None,
        interval: float | None = None,
        my_slot: int | None = None,
        exclusions: Callable[[], list[str]] | None = None,
        worker_factory: Callable[[], tuple[Any, str | None]] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self._config = config
        self._opener = opener or (lambda: open_live(config))
        self._interval = max(
            MIN_POLL_INTERVAL_SECONDS,
            config.poll_interval_seconds if interval is None else interval,
        )
        self._my_slot = my_slot
        self._exclusions = exclusions or (lambda: load_exclusions(config.cache_dir))
        self._worker_factory = worker_factory or (lambda: _build_worker(config))
        self._monotonic = monotonic

        self._lock = threading.Lock()
        self._worker_lock = threading.Lock()
        self._session: Session | None = None
        self._source: Any = None
        self._view: DraftView | None = None
        self._note: str | None = None
        self._fatal: str | None = None
        self._errors = 0
        self._next_poll = 0.0
        self._next_open = 0.0
        self._worker: Any = None
        self._worker_note: str | None = None
        self._worker_built = False

    @property
    def interval(self) -> float:
        return self._interval

    def frame(self) -> DraftFrame:
        """The newest view, polling first if the interval has elapsed."""
        with self._lock:
            now = self._monotonic()
            if self._view is None or now >= self._next_poll:
                self._refresh(now)
            if self._view is None:
                raise FeedUnavailable(self._note or "waiting for the first poll")
            view, note = self._view, self._note
            next_poll_in = max(0.0, self._next_poll - self._monotonic())

        # Research runs outside the poll lock: `ensure` hands work to a thread pool and
        # returns at once, but a slow import or client build shouldn't hold up the board.
        research, status = self._research(view)
        if status == "failed" and self._worker is not None:
            view = replace(view, research_note=self._worker.failure(view))
        elif status == "off":
            view = replace(view, research_note=self._worker_note)
        return DraftFrame(
            view=view,
            note=note,
            research=research,
            research_status=status,
            next_poll_in=next_poll_in,
            budget_left=self._worker.budget_left if self._worker is not None else None,
        )

    # -- polling --------------------------------------------------------------------

    def _refresh(self, now: float) -> None:
        """Poll once. Called with the lock held; never raises except FeedUnavailable."""
        if self._fatal is not None:
            raise FeedUnavailable(self._fatal)
        if self._source is None:
            self._open(now)
        try:
            snapshot = self._source.poll()
            self._view = compute_view(
                self._session, snapshot, my_slot=self._my_slot, excluded=self._exclusions()
            )
            self._note, self._errors = None, 0
            self._next_poll = now + self._interval
        except SleeperRateLimited as exc:
            self._errors += 1
            wait = max(exc.retry_after or 0.0, RATE_LIMIT_BACKOFF_SECONDS * self._errors)
            self._backoff(f"rate limited by Sleeper — backing off {wait:.0f}s", now, wait)
        except SleeperError as exc:
            self._errors += 1
            self._backoff(f"{exc} (retry {self._errors})", now, self._interval * min(self._errors, 5))
        except board.UnsupportedDraftError as exc:
            # The pick math can't work here at all; retrying won't change that.
            self._fatal = str(exc)
            raise FeedUnavailable(self._fatal) from exc
        except Exception as exc:  # noqa: BLE001 - a live draft must not take the server down
            self._errors += 1
            log.exception("Unexpected failure while rebuilding the board")
            self._backoff(f"internal error: {exc}", now, self._interval * min(self._errors, 5))

    def _backoff(self, note: str, now: float, wait: float) -> None:
        self._note = note
        self._next_poll = now + wait
        if self._view is None:
            raise FeedUnavailable(note)

    def _open(self, now: float) -> None:
        if now < self._next_open:
            raise FeedUnavailable(self._note or "draft session unavailable")
        try:
            self._session, self._source = self._opener()
        except (ConfigError, SessionError, SleeperError, espn.ESPNError) as exc:
            self._open_failed(str(exc), now)
        except Exception as exc:  # noqa: BLE001 - report it rather than 500 the request
            log.exception("Could not open the draft session")
            self._open_failed(f"could not open the draft session: {exc}", now)

    def _open_failed(self, message: str, now: float) -> None:
        self._note = message
        self._next_open = now + OPEN_RETRY_SECONDS
        raise FeedUnavailable(message)

    # -- research -------------------------------------------------------------------

    def _research(self, view: DraftView) -> tuple[Any, str]:
        worker = self._ensure_worker()
        if worker is None:
            return None, "off"
        worker.ensure(view)
        return worker.get(view), worker.status(view)

    def _ensure_worker(self) -> Any:
        with self._worker_lock:
            if not self._worker_built:
                self._worker, self._worker_note = self._worker_factory()
                self._worker_built = True
            return self._worker

    def shutdown(self) -> None:
        if self._worker is not None:
            self._worker.shutdown()


def _build_worker(config: Config) -> tuple[Any, str | None]:
    from ..draft.research import build_worker  # imported late: an enhancement, not a dependency

    return build_worker(config)


@dataclass
class _Cached:
    expires: float
    report: WaiverReport | None = None
    error: Exception | None = None


class WaiversFeed:
    """Waiver reports per league, rebuilt at most every `ttl` seconds.

    Every report gets a fresh `SleeperClient` on purpose: the client caches rosters,
    transactions and the NFL state for its own lifetime, which is right for a one-shot
    command and wrong for a server that stays up for days.
    """

    def __init__(
        self,
        config: Config,
        *,
        client_factory: Callable[[], Any] | None = None,
        kickoffs: Callable[[int, int], Any] = espn.week_kickoffs,
        ttl: float = WAIVERS_TTL_SECONDS,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self._config = config
        self._client_factory = client_factory or (lambda: SleeperClient(config.cache_dir))
        self._kickoffs = kickoffs
        self._ttl = ttl
        self._now = now
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._cache: dict[str, _Cached] = {}

    def league_id(self, requested: str | None = None) -> str:
        league = (requested or self._config.league_id or "").strip()
        if not league:
            raise FeedUnavailable(
                "No league configured. Set SLEEPER_LEAGUE_ID in .env, or add ?league=ID to the URL."
            )
        if not LEAGUE_ID.match(league):
            raise FeedUnavailable(f"{league!r} is not a numeric Sleeper league ID.")
        return league

    def report(self, requested: str | None = None) -> WaiverReport:
        league = self.league_id(requested)
        with self._lock:
            now = self._monotonic()
            cached = self._cache.get(league)
            if cached is not None and now < cached.expires:
                if cached.error is not None:
                    raise cached.error
                return cached.report
            client = self._client_factory()
            try:
                report = build_report(client, league, self._now(), self._kickoffs)
            except (SleeperError, espn.ESPNError, UnsupportedWaivers) as exc:
                self._cache[league] = _Cached(now + WAIVERS_ERROR_TTL_SECONDS, error=exc)
                raise
            finally:
                close = getattr(client, "close", None)
                if callable(close):
                    close()
            self._cache[league] = _Cached(now + self._ttl, report=report)
            return report


@dataclass
class _CachedAdvice:
    expires: float
    built: tuple[LineupAdvice, LineupSources] | None = None
    error: Exception | None = None


class LineupFeed:
    """Lineup advice per league, rebuilt at most every `ttl` seconds.

    Like `WaiversFeed`, each build gets a fresh `SleeperClient`, because that client caches
    rosters and the NFL state for its own lifetime -- and a lineup read off a roster cached
    yesterday would quietly advise on players who have since been dropped.

    The build is synchronous rather than backgrounded, which is a measurement and not an
    assumption: a cold build takes about three and a half seconds, most of it ESPN's player
    feed, so a phone waiting on it with a spinner is a better trade than a worker thread and
    a progress protocol.
    """

    def __init__(
        self,
        config: Config,
        *,
        client_factory: Callable[[], Any] | None = None,
        loader: Callable[..., tuple[LineupAdvice, LineupSources]] = load_advice,
        ttl: float = LINEUP_TTL_SECONDS,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self._config = config
        self._client_factory = client_factory or (lambda: SleeperClient(config.cache_dir))
        self._loader = loader
        self._ttl = ttl
        self._now = now
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._cache: dict[str, _CachedAdvice] = {}

    def league_id(self, requested: str | None = None) -> str:
        league = (requested or self._config.league_id or "").strip()
        if not league:
            raise FeedUnavailable(
                "No league configured. Set SLEEPER_LEAGUE_ID in .env, or add ?league=ID to the URL."
            )
        if not LEAGUE_ID.match(league):
            raise FeedUnavailable(f"{league!r} is not a numeric Sleeper league ID.")
        return league

    def advice(self, requested: str | None = None) -> tuple[LineupAdvice, LineupSources]:
        league = self.league_id(requested)
        with self._lock:
            now = self._monotonic()
            cached = self._cache.get(league)
            if cached is not None and now < cached.expires:
                if cached.error is not None:
                    raise cached.error
                assert cached.built is not None
                return cached.built
            client = self._client_factory()
            try:
                built = self._loader(
                    client,
                    league_id=league,
                    username=self._config.sleeper_username,
                    now=self._now(),
                )
            except (SleeperError, espn.ESPNError, UnsupportedWaivers,
                    LineupUnavailable, NoRosterError) as exc:
                self._cache[league] = _CachedAdvice(now + LINEUP_ERROR_TTL_SECONDS, error=exc)
                raise
            finally:
                close = getattr(client, "close", None)
                if callable(close):
                    close()
            self._cache[league] = _CachedAdvice(now + self._ttl, built=built)
            return built


@dataclass
class AppState:
    """Everything the WSGI app needs, assembled once by `ffa serve`."""

    config: Config
    draft: DraftFeed
    waivers: WaiversFeed
    lineup: LineupFeed
    token: str | None = None
    static_dir: Path = Path(__file__).parent / "static"

    def shutdown(self) -> None:
        self.draft.shutdown()
