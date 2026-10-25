"""Snake math, whose turn it is, and what's still on the board.

Everything here is a pure function of the latest Sleeper payloads. State is rebuilt from the
full `/picks` response on every poll rather than diffed, which sidesteps ordering and
duplicate bugs.
"""

import logging
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from ..models import DraftSettings, DraftState, Pick, Player, TradedPick, to_int

log = logging.getLogger(__name__)

SUPPORTED_DRAFT_TYPES = frozenset({"snake", "linear"})
# Sleeper gives irrelevant players huge sentinel ranks; anything above this is unranked.
RANK_SANITY_THRESHOLD = 10_000
# Draft settings keys we understand. Anything else is surfaced as a warning, never ignored.
KNOWN_SETTINGS_KEYS = frozenset({
    "teams", "rounds", "pick_timer", "reversal_round", "player_type", "cpu_autopick",
    "autostart", "autopause_enabled", "autopause_start_time", "autopause_end_time",
    "alpha_sort", "nomination_timer", "budget", "enforce_position_limits",
})
KNOWN_SETTINGS_PREFIXES = ("slots_", "position_limit_")
# Positional run detection over the most recent in-draft picks.
RUN_WINDOW = 8
RUN_THRESHOLD = 4


class UnsupportedDraftError(Exception):
    """A draft whose pick order this tool cannot compute at all, e.g. an auction. Fatal."""


class MalformedDraftError(Exception):
    """A draft payload that didn't parse. Transient mid-poll; fatal for a one-shot command."""


# -- pick numbering ------------------------------------------------------------------


def round_for_pick(pick_no: int, teams: int) -> int:
    return (pick_no - 1) // teams + 1


def _is_reversed(rnd: int, draft_type: str, reversal_round: int | None) -> bool:
    if draft_type == "linear":
        return False
    reversed_ = rnd % 2 == 0
    if reversal_round and rnd >= reversal_round:
        reversed_ = not reversed_  # third-round reversal flips parity from that round onward
    return reversed_


def slot_for_pick(pick_no: int, teams: int, draft_type: str, reversal_round: int | None) -> int:
    """Draft slot (1..teams) that owns global pick `pick_no`, before any trades."""
    rnd = round_for_pick(pick_no, teams)
    idx = (pick_no - 1) % teams + 1  # position within the round, 1..teams
    return teams - idx + 1 if _is_reversed(rnd, draft_type, reversal_round) else idx


def pick_no_for(rnd: int, slot: int, teams: int, draft_type: str, reversal_round: int | None) -> int:
    """Inverse of slot_for_pick: the global pick number for `slot` in round `rnd`."""
    idx = teams - slot + 1 if _is_reversed(rnd, draft_type, reversal_round) else slot
    return (rnd - 1) * teams + idx


def pick_label(pick_no: int, teams: int) -> str:
    """Round.pick notation, e.g. pick 29 in a 12-team draft is '3.05'."""
    return f"{round_for_pick(pick_no, teams)}.{(pick_no - 1) % teams + 1:02d}"


# -- parsing -------------------------------------------------------------------------


def parse_draft(raw: Mapping[str, Any]) -> DraftSettings:
    draft_id = str(raw.get("draft_id") or "?")
    draft_type = str(raw.get("type") or "").lower()
    if not draft_type:
        # No type at all is a broken payload, not a draft format we refuse to support.
        raise MalformedDraftError(f"Draft {draft_id} payload has no 'type' field.")
    if draft_type not in SUPPORTED_DRAFT_TYPES:
        raise UnsupportedDraftError(
            f"Draft {draft_id} is a '{draft_type}' draft. ffa only supports snake and "
            "linear drafts; pick math for anything else would be garbage."
        )
    settings = raw.get("settings") or {}
    teams = to_int(settings.get("teams"))
    rounds = to_int(settings.get("rounds"))
    if not teams or teams < 2 or not rounds or rounds < 1:
        raise MalformedDraftError(
            f"Draft {draft_id} has unusable settings (teams={settings.get('teams')!r}, "
            f"rounds={settings.get('rounds')!r})."
        )

    draft_order = raw.get("draft_order")
    if isinstance(draft_order, dict) and draft_order:
        draft_order = {
            str(user_id): slot
            for user_id, value in draft_order.items()
            if (slot := to_int(value)) is not None
        }
    else:
        draft_order = None

    slot_to_roster: dict[int, int] = {}
    for slot_key, roster in (raw.get("slot_to_roster_id") or {}).items():
        slot, roster_id = to_int(slot_key), to_int(roster)
        if slot is not None and roster_id is not None:
            slot_to_roster[slot] = roster_id
    inferred = not slot_to_roster
    if inferred:
        slot_to_roster = {slot: slot for slot in range(1, teams + 1)}

    metadata = raw.get("metadata") or {}
    season = raw.get("season")
    league_id = raw.get("league_id")
    return DraftSettings(
        draft_id=draft_id,
        draft_type=draft_type,
        status=str(raw.get("status") or "unknown"),
        teams=teams,
        rounds=rounds,
        pick_timer=max(0, to_int(settings.get("pick_timer")) or 0),
        reversal_round=to_int(settings.get("reversal_round")) or None,  # 0 means off
        season=str(season) if season is not None else None,
        league_id=str(league_id) if league_id not in (None, "", "0", 0) else None,
        start_time=to_int(raw.get("start_time")),
        last_picked=to_int(raw.get("last_picked")),
        draft_order=draft_order,
        slot_to_roster_id=slot_to_roster,
        slot_to_roster_inferred=inferred,
        scoring_type=metadata.get("scoring_type") or None,
        raw_settings=dict(settings),
    )


def unrecognized_settings(settings: Mapping[str, Any]) -> list[str]:
    return sorted(
        key for key in settings
        if key not in KNOWN_SETTINGS_KEYS and not key.startswith(KNOWN_SETTINGS_PREFIXES)
    )


def settings_warnings(settings: DraftSettings) -> list[str]:
    """Human-readable warnings about draft settings the pick math might misread."""
    warnings = []
    unknown = unrecognized_settings(settings.raw_settings)
    suspicious = [k for k in unknown if "revers" in k.lower() or "3rr" in k.lower()]
    if suspicious:
        warnings.append(
            f"Unrecognized reversal-like setting(s) {suspicious}: snake math only honors "
            "'reversal_round'. Verify the pick order by hand before trusting the board."
        )
    others = [k for k in unknown if k not in suspicious]
    if others:
        warnings.append(f"Unrecognized draft setting(s), not used by the pick math: {others}")
    if settings.slot_to_roster_inferred:
        warnings.append("Draft has no slot_to_roster_id yet; assuming roster_id == draft slot.")
    return warnings


def parse_picks(raw: Iterable[Mapping[str, Any]]) -> list[Pick]:
    picks = []
    for entry in raw or ():
        pick = Pick.from_sleeper(entry) if isinstance(entry, Mapping) else None
        if pick is None:
            log.warning("Skipping malformed pick record: %r", entry)
        else:
            picks.append(pick)
    return picks


def parse_traded_picks(raw: Iterable[Mapping[str, Any]]) -> list[TradedPick]:
    traded = []
    for entry in raw or ():
        parsed = TradedPick.from_sleeper(entry) if isinstance(entry, Mapping) else None
        if parsed is None:
            log.warning("Skipping malformed traded pick record: %r", entry)
        else:
            traded.append(parsed)
    return traded


# -- ownership -----------------------------------------------------------------------


def traded_overrides(traded: Iterable[TradedPick], season: str | None) -> dict[tuple[int, int], int]:
    """Map (round, original roster_id) -> current owner roster_id.

    Sleeper normally sends one record per traded pick, but if a pick changed hands twice and
    both hops are listed, the end of the chain owns it. Resolving by `previous_owner_id`
    rather than by record order keeps the answer the same whatever order they arrive in.
    """
    chains: dict[tuple[int, int], list[TradedPick]] = {}
    for t in traded:
        if season and t.season and t.season != season:
            continue
        chains.setdefault((t.round, t.roster_id), []).append(t)

    overrides = {}
    for key, records in chains.items():
        handed_on = {r.previous_owner_id for r in records if r.previous_owner_id is not None}
        terminal = [r for r in records if r.owner_id not in handed_on]
        overrides[key] = (terminal[-1] if terminal else records[-1]).owner_id
    return overrides


def original_roster_for_pick(pick_no: int, settings: DraftSettings) -> int | None:
    slot = slot_for_pick(pick_no, settings.teams, settings.draft_type, settings.reversal_round)
    return settings.slot_to_roster_id.get(slot)


def owner_of_pick(
    pick_no: int, settings: DraftSettings, overrides: Mapping[tuple[int, int], int]
) -> int | None:
    """Roster that currently owns `pick_no`, after traded-pick overrides."""
    original = original_roster_for_pick(pick_no, settings)
    if original is None:
        return None
    return overrides.get((round_for_pick(pick_no, settings.teams), original), original)


def roster_names(settings: DraftSettings, users: Mapping[str, str]) -> dict[int, str]:
    """roster_id -> manager display name, via slot_to_roster_id and draft_order."""
    slot_to_user = {slot: user_id for user_id, slot in (settings.draft_order or {}).items()}
    names = {}
    for slot, roster_id in settings.slot_to_roster_id.items():
        user_id = slot_to_user.get(slot)
        names[roster_id] = users.get(user_id) if user_id in users else f"Slot {slot}"
    return names


# -- clock ---------------------------------------------------------------------------


def pick_deadline_ms(settings: DraftSettings) -> int | None:
    """UTC epoch ms when the current pick expires, or None when there is no timer."""
    if settings.pick_timer <= 0:
        return None
    started = settings.last_picked or settings.start_time
    if started is None:
        return None
    return started + settings.pick_timer * 1000


def seconds_remaining(settings: DraftSettings, now_ms: int) -> int | None:
    """Whole seconds left on the clock, never negative. None when paused or untimed."""
    if settings.status != "drafting":
        return None
    deadline = pick_deadline_ms(settings)
    if deadline is None:
        return None
    return max(0, -(-(deadline - now_ms) // 1000))


# -- availability --------------------------------------------------------------------


def matches_exclusion(player: Player, patterns: Iterable[str]) -> bool:
    """Match a player_id exactly, or a name exactly, or a name substring ("jacobs")."""
    name = player.name.lower()
    for raw in patterns:
        pattern = raw.strip().lower()
        if pattern and (pattern == player.player_id.lower() or pattern == name or pattern in name):
            return True
    return False


def available_players(
    players: Iterable[Player], taken: set[str], excluded: Iterable[str] = ()
) -> tuple[tuple[Player, ...], tuple[Player, ...]]:
    """Split the undrafted pool into (ranked players best-first, team defenses).

    Rules: active, on an NFL team, and ranked below the sanity threshold. Team defenses
    carry no search_rank at all in Sleeper's player map, so they are returned separately.
    """
    excluded = tuple(excluded)
    ranked, defenses = [], []
    for player in players:
        if player.player_id in taken or not player.active or not player.team:
            continue
        if excluded and matches_exclusion(player, excluded):
            log.info("Excluding %s (%s) by request", player.name, player.player_id)
            continue
        if player.is_defense:
            defenses.append(player)
        elif player.search_rank is not None and player.search_rank <= RANK_SANITY_THRESHOLD:
            ranked.append(player)
    ranked.sort(key=lambda p: (p.search_rank, p.player_id))
    defenses.sort(key=lambda p: p.player_id)
    return tuple(ranked), tuple(defenses)


# -- state ---------------------------------------------------------------------------


def build_state(
    settings: DraftSettings,
    picks: Sequence[Pick],
    traded: Sequence[TradedPick],
    *,
    my_user_id: str,
    users: Mapping[str, str],
    players: Mapping[str, Player],
    my_slot: int | None = None,
    excluded: Iterable[str] = (),
) -> DraftState:
    """Derive the full board from the latest payloads.

    `my_slot` overrides the slot looked up from draft_order (mock drafts, replays).
    """
    by_number: dict[int, Pick] = {}
    for pick in picks:
        by_number[pick.pick_no] = pick
    ordered = tuple(by_number[n] for n in sorted(by_number))
    occupied = frozenset(by_number)
    # Every player in the payload is off the board, including one whose pick record lost a
    # pick_no collision above: deduplicating the board must not put a player back in the pool.
    taken = {pick.player_id for pick in picks}
    total = settings.total_picks

    if settings.slot_to_roster_inferred:
        # Sleeper sent no slot_to_roster_id, so we assumed roster_id == slot. The picks
        # themselves know better; learn the real mapping rather than derive two different
        # boards (my picks from pick.roster_id, my future picks from the assumed map).
        learned = {
            pick.draft_slot: pick.roster_id
            for pick in ordered
            if pick.draft_slot is not None and pick.roster_id is not None
        }
        if learned:
            settings = replace(settings, slot_to_roster_id={**settings.slot_to_roster_id, **learned})

    # Keepers can pre-fill later slots, so the next pick is the first unused number rather
    # than len(picks) + 1 (the two agree when there are no keepers).
    current = None
    if settings.status != "complete":
        current = next((n for n in range(1, total + 1) if n not in occupied), None)

    overrides = traded_overrides(traded, settings.season)
    names = roster_names(settings, users)

    if my_slot is None and settings.draft_order:
        my_slot = settings.draft_order.get(my_user_id)
    my_roster = settings.slot_to_roster_id.get(my_slot) if my_slot is not None else None

    # Pick records in the wild are missing roster_id; fall back to the snake math so every
    # pick can still be attributed to a manager.
    pick_rosters = {
        pick.pick_no: (
            pick.roster_id if pick.roster_id is not None
            else owner_of_pick(pick.pick_no, settings, overrides)
        )
        for pick in ordered
    }

    if my_roster is not None:
        my_picks = tuple(p for p in ordered if pick_rosters[p.pick_no] == my_roster)
    else:
        my_picks = tuple(p for p in ordered if p.picked_by == my_user_id)

    remaining: tuple[int, ...] = ()
    if my_roster is not None and current is not None:
        remaining = tuple(
            n for n in range(current, total + 1)
            if n not in occupied and owner_of_pick(n, settings, overrides) == my_roster
        )

    on_clock_roster = on_clock_name = on_clock_via = None
    if current is not None:
        original = original_roster_for_pick(current, settings)
        on_clock_roster = owner_of_pick(current, settings, overrides)
        on_clock_name = names.get(on_clock_roster, f"Roster {on_clock_roster}")
        if original is not None and original != on_clock_roster:
            on_clock_via = names.get(original, f"Roster {original}")

    ranked, defenses = available_players(players.values(), taken, excluded)

    return DraftState(
        settings=settings,
        picks=ordered,
        occupied=occupied,
        current_pick_no=current,
        on_clock_roster_id=on_clock_roster,
        on_clock_name=on_clock_name,
        on_clock_via=on_clock_via,
        my_user_id=my_user_id,
        my_slot=my_slot,
        my_roster_id=my_roster,
        my_picks=my_picks,
        my_players=tuple(players.get(p.player_id) or Player.from_pick(p) for p in my_picks),
        my_remaining_picks=remaining,
        roster_names=names,
        pick_rosters=pick_rosters,
        available=ranked,
        available_defenses=defenses,
    )


def pick_position(pick: Pick, players: Mapping[str, Player]) -> str | None:
    player = players.get(pick.player_id)
    return player.position if player else pick.position


def recent_picks_by_others(state: DraftState, limit: int = 5) -> list[Pick]:
    """Latest in-draft selections by other managers, newest first. Keepers are excluded."""
    mine = {p.pick_no for p in state.my_picks}
    others = [p for p in state.picks if not p.is_keeper and p.pick_no not in mine]
    return others[::-1][:limit]


def positional_runs(
    state: DraftState,
    players: Mapping[str, Player],
    window: int = RUN_WINDOW,
    threshold: int = RUN_THRESHOLD,
) -> list[tuple[str, int, int]]:
    """Positions taken at least `threshold` times in the last `window` picks: (pos, count, n)."""
    recent = [p for p in state.picks if not p.is_keeper][-window:]
    counts = Counter(pos for p in recent if (pos := pick_position(p, players)))
    return [(pos, count, len(recent)) for pos, count in counts.most_common() if count >= threshold]
