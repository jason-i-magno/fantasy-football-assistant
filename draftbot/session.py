"""Session assembly: resolve IDs once, then poll (or replay) the board on a loop.

`LiveSource` and `ReplaySource` produce the same `Snapshot`, so `watch` and `replay` run the
exact same pipeline — one against Sleeper, one against recorded JSON with no network.
"""

import json
import logging
import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import board, needs
from .config import Config
from .models import DraftState, Pick, Player, to_int
from .shortlist import Shortlist, build_shortlist
from .sleeper import SleeperClient

log = logging.getLogger(__name__)

RECORDING_FORMAT = "draftbot-recording/1"
RECENT_PICKS_SHOWN = 10  # positional runs are the signal search_rank can't capture
EXCLUDE_FILE = "exclude.txt"  # one name or player_id per line; re-read on every poll
ACTIVE_DRAFT_STATUSES = ("pre_draft", "drafting", "paused")
_SCORING_TYPE_NAMES = {
    "std": "Standard (no PPR)",
    "ppr": "PPR",
    "half_ppr": "Half PPR",
    "2qb": "Superflex / 2QB",
    "dynasty_ppr": "Dynasty, PPR",
    "dynasty_std": "Dynasty, standard",
    "dynasty_half_ppr": "Dynasty, half PPR",
}


class SessionError(Exception):
    """Could not assemble a draft session from the configured IDs."""


@dataclass(frozen=True)
class Session:
    """Everything resolved once per run and reused on every poll."""

    username: str
    user_id: str
    display_name: str
    draft_id: str
    users: Mapping[str, str]  # user_id -> display name
    requirements: needs.RosterRequirements
    scoring_summary: str
    players: Mapping[str, Player]
    players_date: str
    league_name: str | None = None
    league_id: str | None = None


@dataclass(frozen=True)
class Snapshot:
    draft: Mapping[str, Any]
    picks: Sequence[Mapping[str, Any]]
    traded_picks: Sequence[Mapping[str, Any]]
    fetched_at_ms: int


@dataclass(frozen=True)
class DraftView:
    """One rendered frame's worth of derived state."""

    session: Session
    state: DraftState
    fill: needs.RosterFill
    shortlist: Shortlist
    recent: Sequence[Pick]
    runs: Sequence[tuple[str, int, int]]
    warnings: Sequence[str]
    fetched_at_ms: int
    research_note: str | None = None  # why research is unavailable, when it is


def now_ms() -> int:
    return int(time.time() * 1000)


# -- league context ------------------------------------------------------------------


def summarize_scoring(scoring: Mapping[str, Any] | None, scoring_type: str | None) -> str:
    """One line of the scoring rules that actually change draft value."""
    if not scoring:
        return _SCORING_TYPE_NAMES.get(scoring_type or "", scoring_type or "unknown")

    def value(key, default=0.0):
        try:
            return float(scoring.get(key, default) or 0.0)
        except (TypeError, ValueError):
            return default

    # Sleeper stores these as float32, so 0.1 arrives as 0.10000000149011612.
    def close(a: float, b: float) -> bool:
        return math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-9)

    rec = value("rec")
    if close(rec, 1.0):
        parts = ["PPR (1/reception)"]
    elif close(rec, 0.5):
        parts = ["Half PPR (0.5/reception)"]
    elif close(rec, 0.0):
        parts = ["Standard (no PPR)"]
    else:
        parts = [f"{rec:g} pts/reception"]
    if te_bonus := value("bonus_rec_te"):
        parts.append(f"TE premium +{te_bonus:g}/reception")
    parts.append(f"{value('pass_td', 4.0):g}-pt passing TD")
    if pass_yd := value("pass_yd", 0.04):
        parts.append(f"1 pt/{round(1 / pass_yd)} passing yds")
    rush_yd = value("rush_yd", 0.1)
    if rush_yd and not close(rush_yd, 0.1):
        parts.append(f"1 pt/{round(1 / rush_yd)} rushing yds")
    if interception := value("pass_int", -2.0):
        parts.append(f"INT {interception:g}")
    if first_down := value("bonus_rec_first_down"):
        parts.append(f"+{first_down:g} per reception first down")
    return ", ".join(parts)


def build_session(
    *,
    username: str,
    user: Mapping[str, Any],
    draft_id: str,
    draft: Mapping[str, Any],
    league: Mapping[str, Any] | None,
    league_users: Sequence[Mapping[str, Any]],
    players_raw: Mapping[str, Mapping[str, Any]],
    players_date: str,
) -> Session:
    users = {
        str(entry["user_id"]): str(entry.get("display_name") or entry.get("username") or entry["user_id"])
        for entry in league_users
        if entry.get("user_id")
    }
    user_id = str(user.get("user_id") or "")
    display_name = str(user.get("display_name") or username)
    users.setdefault(user_id, display_name)

    if league and league.get("roster_positions"):
        requirements = needs.parse_roster_positions(league["roster_positions"])
    else:
        # Mock drafts have no league: roster slots come from the draft's own settings.
        requirements = needs.requirements_from_draft_settings(draft.get("settings") or {})

    players: dict[str, Player] = {}
    for player_id, raw in players_raw.items():
        player = Player.from_sleeper(player_id, raw)
        if player is not None:
            players[player.player_id] = player

    return Session(
        username=username,
        user_id=user_id,
        display_name=display_name,
        draft_id=str(draft_id),
        users=users,
        requirements=requirements,
        scoring_summary=summarize_scoring(
            league.get("scoring_settings") if league else None,
            (draft.get("metadata") or {}).get("scoring_type"),
        ),
        players=players,
        players_date=players_date,
        league_name=str(league["name"]) if league and league.get("name") else None,
        league_id=str(league["league_id"]) if league and league.get("league_id") else None,
    )


def resolve_draft_id(config: Config, client: SleeperClient) -> str:
    """DRAFT_ID wins; otherwise take the league's live draft, else its most recent one."""
    if config.draft_id:
        return config.draft_id
    drafts = client.league_drafts(config.league_id)
    if not drafts:
        raise SessionError(f"League {config.league_id} has no drafts yet.")
    def rank(draft):
        status = draft.get("status")
        return (
            status in ACTIVE_DRAFT_STATUSES,
            status == "drafting",
            to_int(draft.get("start_time")) or to_int(draft.get("created")) or 0,
        )
    chosen = max(drafts, key=rank)
    draft_id = chosen.get("draft_id")
    if not draft_id:
        raise SessionError(f"League {config.league_id} returned a draft with no draft_id.")
    return str(draft_id)


def open_session(config: Config, client: SleeperClient) -> Session:
    """Resolve user, league, draft and player map. Raises SleeperError / SessionError."""
    user = client.user(config.sleeper_username)
    draft_id = resolve_draft_id(config, client)
    draft = client.draft(draft_id)

    league_id = draft.get("league_id")
    league_id = str(league_id) if league_id not in (None, "", "0", 0) else None
    if league_id is None and not config.draft_id:
        league_id = config.league_id
    if league_id and config.league_id and league_id != config.league_id:
        log.warning(
            "Draft %s belongs to league %s, not the configured SLEEPER_LEAGUE_ID %s; using the draft's league.",
            draft_id, league_id, config.league_id,
        )

    league = client.league(league_id) if league_id else None
    league_users = client.league_users(league_id) if league_id else []
    players_raw, players_date = client.players()
    return build_session(
        username=config.sleeper_username,
        user=user,
        draft_id=draft_id,
        draft=draft,
        league=league,
        league_users=league_users,
        players_raw=players_raw,
        players_date=players_date,
    )


# -- polling sources -----------------------------------------------------------------


class LiveSource:
    """Polls Sleeper. /picks every call; the draft object and traded picks per cache policy."""

    def __init__(self, client: SleeperClient, draft_id: str):
        self._client = client
        self._draft_id = draft_id
        self.finished = False

    def poll(self) -> Snapshot:
        picks = self._client.picks(self._draft_id)
        count = len(picks)
        return Snapshot(
            draft=self._client.draft(self._draft_id, pick_count=count),
            picks=picks,
            traded_picks=self._client.traded_picks(self._draft_id, pick_count=count),
            fetched_at_ms=now_ms(),
        )


class ReplaySource:
    """Serves recorded snapshots in order, then repeats the last one."""

    def __init__(self, snapshots: Sequence[Snapshot]):
        if not snapshots:
            raise SessionError("Recording contains no snapshots.")
        self._snapshots = list(snapshots)
        self._index = -1
        self.finished = False

    def poll(self) -> Snapshot:
        if self._index < len(self._snapshots) - 1:
            self._index += 1
        self.finished = self._index >= len(self._snapshots) - 1
        return self._snapshots[self._index]


# -- recording and replay ------------------------------------------------------------


@dataclass
class Recording:
    header: Mapping[str, Any]
    snapshots: list[Snapshot] = field(default_factory=list)

    def session(self) -> Session:
        header = self.header
        if not self.snapshots:
            raise SessionError("Recording contains no snapshots.")
        return build_session(
            username=header.get("username", "?"),
            user=header.get("user") or {"user_id": header.get("user_id", "?")},
            draft_id=str(header.get("draft_id") or self.snapshots[0].draft.get("draft_id")),
            draft=self.snapshots[0].draft,
            league=header.get("league"),
            league_users=header.get("league_users") or [],
            players_raw=header.get("players") or {},
            players_date=str(header.get("players_date") or "unknown"),
        )


def _snapshot_from_json(entry: Mapping[str, Any]) -> Snapshot:
    return Snapshot(
        draft=entry.get("draft") or {},
        picks=entry.get("picks") or [],
        traded_picks=entry.get("traded_picks") or [],
        fetched_at_ms=int(entry.get("fetched_at_ms") or 0),
    )


def load_recording(path: Path | str) -> Recording:
    """Read a recording written by `watch --record` (JSON Lines) or a single-object fixture."""
    path = Path(path)
    text = path.read_text()
    try:
        payload = json.loads(text)
    except ValueError:
        lines = [json.loads(line) for line in text.splitlines() if line.strip()]
        if not lines:
            raise SessionError(f"{path} is empty.") from None
        header, entries = lines[0], lines[1:]
    else:
        if not isinstance(payload, dict):
            raise SessionError(f"{path} is not a draftbot recording.")
        header = {k: v for k, v in payload.items() if k != "snapshots"}
        entries = payload.get("snapshots") or []
    if header.get("format") != RECORDING_FORMAT:
        raise SessionError(f"{path} is not a {RECORDING_FORMAT} recording (got {header.get('format')!r}).")
    return Recording(header=header, snapshots=[_snapshot_from_json(e) for e in entries])


class Recorder:
    """Appends raw API snapshots to a JSON Lines file, one line per change of the board."""

    def __init__(self, path: Path, header: Mapping[str, Any]):
        self.path = path
        self._fingerprint: tuple | None = None
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as handle:
            json.dump({"format": RECORDING_FORMAT, **header}, handle)
            handle.write("\n")

    def add(self, snapshot: Snapshot) -> bool:
        draft = snapshot.draft or {}
        fingerprint = (
            len(snapshot.picks),
            draft.get("status"),
            draft.get("draft_order") is not None,
            len(snapshot.traded_picks),
        )
        if fingerprint == self._fingerprint:
            return False
        self._fingerprint = fingerprint
        with self.path.open("a") as handle:
            json.dump(
                {
                    "fetched_at_ms": snapshot.fetched_at_ms,
                    "draft": snapshot.draft,
                    "picks": list(snapshot.picks),
                    "traded_picks": list(snapshot.traded_picks),
                },
                handle,
            )
            handle.write("\n")
        return True


def recording_header(session: Session, client: SleeperClient) -> dict[str, Any]:
    players_raw, players_date = client.players()
    league = client.league(session.league_id) if session.league_id else None
    return {
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "username": session.username,
        "user": {"user_id": session.user_id, "display_name": session.display_name},
        "draft_id": session.draft_id,
        "league": league,
        "league_users": [
            {"user_id": uid, "display_name": name} for uid, name in session.users.items()
        ],
        "players_date": players_date,
        "players": players_raw,
    }


# -- pipeline ------------------------------------------------------------------------


def load_exclusions(cache_dir: Path) -> list[str]:
    """Players to keep off the board, from cache/exclude.txt. Missing file means none.

    Read on every poll on purpose: you can add a name mid-draft without restarting `watch`.
    """
    path = Path(cache_dir) / EXCLUDE_FILE
    try:
        lines = path.read_text().splitlines()
    except (OSError, ValueError):
        return []
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def compute_view(
    session: Session, snapshot: Snapshot, *, my_slot: int | None = None,
    excluded: Sequence[str] = (),
) -> DraftView:
    """Run one snapshot through board -> needs -> shortlist."""
    settings = board.parse_draft(snapshot.draft)
    state = board.build_state(
        settings,
        board.parse_picks(snapshot.picks),
        board.parse_traded_picks(snapshot.traded_picks),
        my_user_id=session.user_id,
        users=session.users,
        players=session.players,
        my_slot=my_slot,
        excluded=excluded,
    )
    fill = needs.fill_roster(session.requirements, state.my_players)
    return DraftView(
        session=session,
        state=state,
        fill=fill,
        shortlist=build_shortlist(state, fill),
        recent=board.recent_picks_by_others(state, limit=RECENT_PICKS_SHOWN),
        runs=board.positional_runs(state, session.players),
        warnings=board.settings_warnings(settings),
        fetched_at_ms=snapshot.fetched_at_ms,
    )
