"""Plain data types shared across the draft pipeline.

Sleeper payloads are loosely shaped (the docs' own pick example is missing fields), so every
`from_sleeper` constructor reads with `.get()` and tolerates missing or mistyped values.
"""

from dataclasses import dataclass
from typing import Any

FANTASY_POSITIONS: tuple[str, ...] = ("QB", "RB", "WR", "TE", "K", "DEF")

# Sleeper's real vocabulary, counted off the live player map: injury_status is one of
# Out / IR / PUP / DNR / Sus / Questionable / NA / null, and status is Active / Inactive.
# "Questionable" still plays; "NA" appears alongside status Active and means no designation.
UNAVAILABLE_INJURY = frozenset({"OUT", "IR", "PUP", "DNR", "SUS", "SUSPENDED", "NFI"})
UNAVAILABLE_STATUS = frozenset({
    "INACTIVE", "INJURED RESERVE", "PHYSICALLY UNABLE TO PERFORM", "SUSPENDED",
    "NON FOOTBALL INJURY", "RESERVE/COVID-19",
})


def to_int(value: Any) -> int | None:
    """Coerce Sleeper's mixed int/str numbers; anything unparseable becomes None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class Player:
    player_id: str  # numeric string, or a team abbreviation ("DET") for team defenses
    name: str
    position: str  # primary fantasy position, one of FANTASY_POSITIONS when known
    fantasy_positions: tuple[str, ...]  # fantasy-relevant positions only
    team: str | None
    search_rank: int | None  # Sleeper's ordering (ADP proxy), never a projection
    active: bool = True
    injury_status: str | None = None
    status: str | None = None  # Sleeper's roster status, e.g. "Injured Reserve"
    age: int | None = None
    years_exp: int | None = None
    depth_chart_order: int | None = None

    @property
    def is_defense(self) -> bool:
        return self.position == "DEF"

    @property
    def can_play(self) -> bool:
        """False when Sleeper says this player cannot take the field right now.

        A starting slot filled by one of these is still a hole, and such a player must not
        anchor the "best available" end of a positional dropoff.
        """
        if (self.injury_status or "").upper() in UNAVAILABLE_INJURY:
            return False
        return (self.status or "").upper() not in UNAVAILABLE_STATUS

    @classmethod
    def from_sleeper(cls, player_id: str, raw: dict[str, Any]) -> "Player | None":
        """Parse a `/players/nfl` entry. Returns None when no fantasy-relevant position."""
        positions = tuple(p for p in (raw.get("fantasy_positions") or ()) if p in FANTASY_POSITIONS)
        primary = raw.get("position")
        if primary not in FANTASY_POSITIONS:
            # e.g. a DB who is also WR-eligible is a WR for fantasy purposes.
            primary = positions[0] if positions else None
        if primary is None:
            return None
        if primary not in positions:
            positions = (primary, *positions)
        name = raw.get("full_name") or " ".join(
            part for part in (raw.get("first_name"), raw.get("last_name")) if part
        )
        return cls(
            player_id=str(player_id),
            name=name or str(player_id),
            position=primary,
            fantasy_positions=positions,
            team=raw.get("team") or None,
            search_rank=to_int(raw.get("search_rank")),
            active=raw.get("active") is True,
            injury_status=raw.get("injury_status") or None,
            status=raw.get("status") or None,
            age=to_int(raw.get("age")),
            years_exp=to_int(raw.get("years_exp")),
            depth_chart_order=to_int(raw.get("depth_chart_order")),
        )

    @classmethod
    def from_pick(cls, pick: "Pick") -> "Player":
        """Fallback for a drafted player missing from the player map (uses pick metadata)."""
        known = pick.position in FANTASY_POSITIONS
        return cls(
            player_id=pick.player_id,
            name=pick.name or pick.player_id,
            position=pick.position or "?",
            fantasy_positions=(pick.position,) if known else (),
            team=pick.team,
            search_rank=None,
        )


@dataclass(frozen=True, slots=True)
class Pick:
    pick_no: int  # 1-indexed, global across the draft
    player_id: str
    roster_id: int | None = None
    picked_by: str | None = None  # user_id; Sleeper sends "" when no user is attached
    round: int | None = None
    draft_slot: int | None = None
    is_keeper: bool = False
    name: str = ""
    position: str | None = None
    team: str | None = None

    @classmethod
    def from_sleeper(cls, raw: dict[str, Any]) -> "Pick | None":
        """Parse a `/draft/<id>/picks` entry. Returns None if it has no usable pick_no/player."""
        pick_no = to_int(raw.get("pick_no"))
        meta = raw.get("metadata") or {}
        player_id = raw.get("player_id") or meta.get("player_id")
        if pick_no is None or pick_no < 1 or not player_id:
            return None
        name = " ".join(part for part in (meta.get("first_name"), meta.get("last_name")) if part)
        return cls(
            pick_no=pick_no,
            player_id=str(player_id),
            roster_id=to_int(raw.get("roster_id")),
            picked_by=str(raw.get("picked_by") or "") or None,
            round=to_int(raw.get("round")),
            draft_slot=to_int(raw.get("draft_slot")),
            is_keeper=raw.get("is_keeper") is True,
            name=name,
            position=meta.get("position") or None,
            team=meta.get("team") or None,
        )


@dataclass(frozen=True, slots=True)
class TradedPick:
    round: int
    roster_id: int  # original owner's roster
    owner_id: int  # current owner's roster
    season: str | None = None
    previous_owner_id: int | None = None

    @classmethod
    def from_sleeper(cls, raw: dict[str, Any]) -> "TradedPick | None":
        rnd = to_int(raw.get("round"))
        roster_id = to_int(raw.get("roster_id"))
        owner_id = to_int(raw.get("owner_id"))
        if rnd is None or roster_id is None or owner_id is None:
            return None
        season = raw.get("season")
        return cls(
            round=rnd,
            roster_id=roster_id,
            owner_id=owner_id,
            season=str(season) if season is not None else None,
            previous_owner_id=to_int(raw.get("previous_owner_id")),
        )


@dataclass(frozen=True, slots=True)
class DraftSettings:
    """The parts of `GET /draft/<id>` the pick math depends on."""

    draft_id: str
    draft_type: str  # "snake" or "linear"
    status: str  # pre_draft | drafting | paused | complete
    teams: int
    rounds: int
    pick_timer: int  # seconds; 0 means no timer
    reversal_round: int | None
    season: str | None
    league_id: str | None
    start_time: int | None  # UTC epoch ms
    last_picked: int | None  # UTC epoch ms
    draft_order: dict[str, int] | None  # user_id -> slot; None until the order is set
    slot_to_roster_id: dict[int, int]
    slot_to_roster_inferred: bool  # True when Sleeper sent no mapping and we assumed slot == roster
    scoring_type: str | None
    raw_settings: dict[str, Any]

    @property
    def total_picks(self) -> int:
        return self.teams * self.rounds


@dataclass(frozen=True, slots=True)
class RosterSlot:
    slot: str  # "QB", "FLEX", "BN", ...
    player: Player | None = None

    @property
    def filled(self) -> bool:
        return self.player is not None

    @property
    def is_starter(self) -> bool:
        return self.slot != "BN"


@dataclass(frozen=True)
class DraftState:
    """Board state rebuilt from scratch on every poll."""

    settings: DraftSettings
    picks: tuple[Pick, ...]  # sorted by pick_no, deduplicated
    occupied: frozenset[int]  # pick numbers already used (including keeper slots)
    current_pick_no: int | None  # None once the draft is complete
    on_clock_roster_id: int | None
    on_clock_name: str | None
    on_clock_via: str | None  # original owner, when the current pick was traded
    my_user_id: str
    my_slot: int | None
    my_roster_id: int | None
    my_picks: tuple[Pick, ...]
    my_players: tuple[Player, ...]  # parallel to my_picks
    my_remaining_picks: tuple[int, ...]  # every future pick_no I own, soonest first
    roster_names: dict[int, str]  # roster_id -> manager display name
    pick_rosters: dict[int, int | None]  # pick_no -> roster that made it, derived when absent
    available: tuple[Player, ...]  # ranked pool, best search_rank first
    available_defenses: tuple[Player, ...]  # Sleeper ranks no team defenses

    @property
    def status(self) -> str:
        return self.settings.status

    @property
    def is_complete(self) -> bool:
        return self.current_pick_no is None

    @property
    def is_my_turn(self) -> bool:
        return (
            self.current_pick_no is not None
            and bool(self.my_remaining_picks)
            and self.my_remaining_picks[0] == self.current_pick_no
        )

    @property
    def my_upcoming_picks(self) -> tuple[int, ...]:
        return self.my_remaining_picks[:3]

    @property
    def my_next_pick_no(self) -> int | None:
        """My first pick strictly after the one currently on the clock."""
        if self.current_pick_no is None:
            return None
        return next((n for n in self.my_remaining_picks if n > self.current_pick_no), None)

    @property
    def picks_until_my_next_turn(self) -> int | None:
        """Selections made from now (inclusive) until my next turn: the scarcity `gap`."""
        nxt = self.my_next_pick_no
        if nxt is None:
            return None
        return self.open_picks_between(self.current_pick_no, nxt)

    def open_picks_between(self, start: int, end: int) -> int:
        """Count of pick numbers in [start, end) not already used (keepers pre-fill some)."""
        return sum(1 for n in range(start, end) if n not in self.occupied)
