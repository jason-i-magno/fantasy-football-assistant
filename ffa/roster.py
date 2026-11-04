"""The league's starting-slot vocabulary, shared by the draft and lineup tools.

`roster_positions` is a flat list with one entry per slot, repeats included, in the order
Sleeper displays them: `["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "FLEX", "K", "DEF",
"BN", ...]`. The index of a starting slot in that list is also its index in a roster's
`starters` array, which is how `ffa/lineup` knows which slot a player is currently filling.
"""

from collections.abc import Sequence

from .models import FANTASY_POSITIONS

# Sleeper's multi-position slots and what may start in each.
FLEX_ELIGIBILITY: dict[str, frozenset[str]] = {
    "FLEX": frozenset({"RB", "WR", "TE"}),
    "WRRB_FLEX": frozenset({"RB", "WR"}),
    "REC_FLEX": frozenset({"WR", "TE"}),
    "SUPER_FLEX": frozenset({"QB", "RB", "WR", "TE"}),
}
BENCH = "BN"
# Slots that hold a player without starting him. IR is not in `starters` at all.
NON_STARTING_SLOTS: frozenset[str] = frozenset({BENCH, "IR", "TAXI"})


def eligible_positions(slot: str) -> frozenset[str]:
    """Which fantasy positions may fill this slot. Empty for a slot we don't model."""
    if slot in FLEX_ELIGIBILITY:
        return FLEX_ELIGIBILITY[slot]
    return frozenset({slot}) if slot in FANTASY_POSITIONS else frozenset()


def starting_slots(roster_positions: Sequence[str]) -> tuple[str, ...]:
    """The starting slots, in `starters` order, with bench and reserve slots dropped."""
    return tuple(slot for slot in roster_positions if slot not in NON_STARTING_SLOTS)
