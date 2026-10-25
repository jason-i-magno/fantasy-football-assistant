"""Roster construction rules: which starting slots are still open, and what that implies."""

import logging
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .models import FANTASY_POSITIONS, Player, RosterSlot

log = logging.getLogger(__name__)

FLEX_ELIGIBILITY: dict[str, frozenset[str]] = {
    "FLEX": frozenset({"RB", "WR", "TE"}),
    "WRRB_FLEX": frozenset({"RB", "WR"}),
    "REC_FLEX": frozenset({"WR", "TE"}),
    "SUPER_FLEX": frozenset({"QB", "RB", "WR", "TE"}),
}
BENCH = "BN"

# Guardrails the shortlist must respect. These are depth caps: a league that *starts* more
# than the cap (a true 2QB roster) raises its own cap, or the position could never be filled.
QB_CAP = 1
QB_CAP_SUPERFLEX = 2
TE_CAP = 2
K_DEF_FINAL_ROUNDS = 2  # K and DEF only surface in the draft's last N rounds

# Need weights, 0..1. Fed into the candidate score in scarcity.py.
NEED_STARTER = 1.0  # fills an open concrete starting slot
NEED_FLEX = 0.6  # fills an open flex slot only
NEED_BENCH = {"RB": 0.35, "WR": 0.35, "QB": 0.1, "TE": 0.1}  # injury-replacement value

# Draft settings `slots_*` keys map to roster slot names by upper-casing the suffix
# (slots_super_flex -> SUPER_FLEX). This fixes the display order for mock drafts.
_SLOT_ORDER = ("QB", "RB", "WR", "TE", "WRRB_FLEX", "FLEX", "REC_FLEX", "SUPER_FLEX", "K", "DEF", BENCH)


@dataclass(frozen=True)
class RosterRequirements:
    positions: tuple[str, ...]  # the league's roster_positions, in order
    starters: Mapping[str, int]  # concrete position -> starting slots
    flex: tuple[str, ...]  # flex slot names, in roster order
    bench: int
    ignored: tuple[str, ...] = ()  # slots this tool doesn't model (IDP, unknown)

    @property
    def superflex(self) -> bool:
        return "SUPER_FLEX" in self.flex

    def accepts(self, position: str) -> bool:
        """Can a player at `position` ever start for this roster?"""
        return position in self.starters or any(position in FLEX_ELIGIBILITY[f] for f in self.flex)

    def depth_cap(self, position: str) -> int | None:
        """How many of `position` are worth rostering, or None when uncapped."""
        if position == "QB":
            # SUPER_FLEX is the only flex a QB can start in, so it raises the cap; a plain
            # 2QB league raises it through its two concrete QB slots.
            starting = self.starters.get("QB", 0) + self.flex.count("SUPER_FLEX")
            return max(QB_CAP_SUPERFLEX if self.superflex else QB_CAP, starting)
        if position == "TE":
            # Flex slots deliberately don't raise this: a third TE for a FLEX is not the plan.
            return max(TE_CAP, self.starters.get("TE", 0))
        return None

    def describe(self) -> str:
        parts = [f"{pos}×{n}" if n > 1 else pos for pos, n in self.starters.items() if pos not in ("K", "DEF")]
        for name, n in Counter(self.flex).items():
            eligible = "/".join(p for p in FANTASY_POSITIONS if p in FLEX_ELIGIBILITY[name])
            parts.append(f"{name}×{n} ({eligible})" if n > 1 else f"{name} ({eligible})")
        parts += [pos for pos in ("K", "DEF") for _ in range(self.starters.get(pos, 0))]
        if self.bench:
            parts.append(f"BN×{self.bench}")
        return ", ".join(parts)


def parse_roster_positions(positions: Sequence[str]) -> RosterRequirements:
    starters: Counter[str] = Counter()
    flex: list[str] = []
    bench = 0
    ignored: list[str] = []
    for raw in positions:
        slot = str(raw).upper()
        if slot in FANTASY_POSITIONS:
            starters[slot] += 1
        elif slot in FLEX_ELIGIBILITY:
            flex.append(slot)
        elif slot == BENCH:
            bench += 1
        else:
            ignored.append(slot)
    if ignored:
        log.warning("Roster slots not modeled (ignored for needs): %s", sorted(set(ignored)))
    ordered = {pos: starters[pos] for pos in FANTASY_POSITIONS if starters[pos]}
    return RosterRequirements(
        positions=tuple(str(p).upper() for p in positions),
        starters=ordered,
        flex=tuple(flex),
        bench=bench,
        ignored=tuple(ignored),
    )


def requirements_from_draft_settings(settings: Mapping[str, Any]) -> RosterRequirements:
    """Mock drafts have no league, so roster slots come from the draft's `slots_*` keys."""
    counts: dict[str, int] = {}
    for key, value in settings.items():
        if key.startswith("slots_"):
            try:
                counts[key[len("slots_"):].upper()] = int(value or 0)
            except (TypeError, ValueError):
                continue
    order = {name: i for i, name in enumerate(_SLOT_ORDER)}
    names = sorted(counts, key=lambda n: (order.get(n, len(order)), n))
    return parse_roster_positions([name for name in names for _ in range(counts[name])])


@dataclass(frozen=True)
class RosterFill:
    requirements: RosterRequirements
    slots: tuple[RosterSlot, ...]  # starters in league order, then bench
    position_counts: Mapping[str, int] = field(default_factory=dict)

    @property
    def unfilled_starter_slots(self) -> tuple[str, ...]:
        """Starting slots that still need a body — including ones held by an Out/IR player."""
        return tuple(
            s.slot for s in self.slots
            if s.is_starter and not (s.filled and s.player.can_play)
        )

    def count(self, position: str) -> int:
        return self.position_counts.get(position, 0)


def fill_roster(requirements: RosterRequirements, players: Sequence[Player]) -> RosterFill:
    """Seat drafted players: concrete positions first, then flex (most restrictive first), then bench."""
    starters = [
        [slot, None] for slot in requirements.positions
        if slot in FANTASY_POSITIONS or slot in FLEX_ELIGIBILITY
    ]

    # Only players who can take the field are seated in a starting slot. An Out/IR player
    # goes to the bench, where he belongs, instead of holding a slot a healthy replacement
    # should have — and instead of hiding a hole the needs model has to report.
    leftover = []
    for player in (p for p in players if p.can_play):
        positions = (player.position, *(p for p in player.fantasy_positions if p != player.position))
        seat = next(
            (s for pos in positions for s in starters if s[0] == pos and s[1] is None),
            None,
        )
        if seat is None:
            leftover.append(player)
        else:
            seat[1] = player

    open_flex = [(i, seat[0]) for i, seat in enumerate(starters)
                 if seat[0] in FLEX_ELIGIBILITY and seat[1] is None]
    seated = _seat_flex([name for _, name in open_flex], leftover)
    for seat_index, player_index in seated.items():
        starters[open_flex[seat_index][0]][1] = leftover[player_index]
    taken = set(seated.values())
    started = {
        player.player_id for slot, player in starters if player is not None
    }
    # Bench keeps pick order and picks up everyone who didn't start, injured or not.
    bench = [player for player in players if player.player_id not in started]

    slots = [RosterSlot(slot, player) for slot, player in starters]
    slots += [RosterSlot(BENCH, player) for player in bench]
    slots += [RosterSlot(BENCH) for _ in range(max(0, requirements.bench - len(bench)))]
    return RosterFill(
        requirements=requirements,
        slots=tuple(slots),
        # Caps count bodies you can start: an IR tight end shouldn't block drafting one.
        position_counts=Counter(p.position for p in players if p.can_play),
    )


def _seat_flex(seats: Sequence[str], players: Sequence[Player]) -> dict[int, int]:
    """Match leftover players to open flex seats, filling as many as possible.

    First-fit is wrong when two flex types accept equal-sized but non-overlapping sets:
    with WRRB_FLEX (RB/WR) and REC_FLEX (WR/TE) open, seating a WR first-fit into WRRB_FLEX
    strands a later RB on the bench and invents a phantom open starter. This is the standard
    augmenting-path matching, which is exact and trivially fast at roster sizes.
    """
    seat_to_player: dict[int, int] = {}

    def assign(player_index: int, tried: set[int]) -> bool:
        for seat_index, seat in enumerate(seats):
            if seat_index in tried:
                continue
            if not FLEX_ELIGIBILITY[seat].intersection(players[player_index].fantasy_positions):
                continue
            tried.add(seat_index)
            if seat_index not in seat_to_player or assign(seat_to_player[seat_index], tried):
                seat_to_player[seat_index] = player_index
                return True
        return False

    for index in range(len(players)):
        assign(index, set())
    return seat_to_player


def need_weight(position: str, fill: RosterFill) -> float:
    open_slots = fill.unfilled_starter_slots
    if position in open_slots:
        return NEED_STARTER
    if any(slot in FLEX_ELIGIBILITY and position in FLEX_ELIGIBILITY[slot] for slot in open_slots):
        return NEED_FLEX
    return NEED_BENCH.get(position, 0.0)


def block_reason(
    position: str,
    fill: RosterFill,
    *,
    decision_round: int,
    total_rounds: int,
    picks_left: int,
) -> str | None:
    """Why the shortlist must not surface `position` for this pick, or None if it may."""
    req = fill.requirements
    if not req.accepts(position):
        return f"no {position} slot in this league"
    cap = req.depth_cap(position)
    if cap is not None and fill.count(position) >= cap:
        return f"{position} cap reached ({cap})"
    # Never finish the draft with an empty starting slot: once remaining picks only just
    # cover the open starters, every pick has to fill one.
    open_slots = fill.unfilled_starter_slots
    must_fill = picks_left <= len(open_slots)
    if position in ("K", "DEF"):
        if position not in open_slots:
            return f"{position} slot already filled"
        late = decision_round > total_rounds - K_DEF_FINAL_ROUNDS
        # ...but K and DEF still wait, unless they are all that's left to fill.
        forced = picks_left <= sum(1 for slot in open_slots if slot in ("K", "DEF"))
        if not late and not forced:
            return f"waits for the final {K_DEF_FINAL_ROUNDS} rounds"
    if must_fill and need_weight(position, fill) < NEED_FLEX:
        return "remaining picks are needed for open starting slots"
    return None
