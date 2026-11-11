"""The league's starting-slot vocabulary, shared by the draft and lineup tools.

`roster_positions` is a flat list with one entry per slot, repeats included, in the order
Sleeper displays them: `["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "FLEX", "K", "DEF",
"BN", ...]`. The index of a starting slot in that list is also its index in a roster's
`starters` array, which is how `ffa/lineup` knows which slot a player is currently filling.
"""

from collections.abc import Mapping, Sequence

from .models import FANTASY_POSITIONS

# Sleeper's multi-position slots and what may start in each.
FLEX_ELIGIBILITY: dict[str, frozenset[str]] = {
    "FLEX": frozenset({"RB", "WR", "TE"}),
    "WRRB_FLEX": frozenset({"RB", "WR"}),
    "REC_FLEX": frozenset({"WR", "TE"}),
    "SUPER_FLEX": frozenset({"QB", "RB", "WR", "TE"}),
}
BENCH = "BN"
# Slots that hold a player off the active roster entirely. He is not in `starters`, he
# cannot be moved into the lineup without being activated first, and the spot he sits in is
# not one an added player could take.
RESERVE_SLOTS: frozenset[str] = frozenset({"IR", "TAXI"})
# Slots that hold a player without starting him.
NON_STARTING_SLOTS: frozenset[str] = frozenset({BENCH}) | RESERVE_SLOTS


def eligible_positions(slot: str) -> frozenset[str]:
    """Which fantasy positions may fill this slot. Empty for a slot we don't model."""
    if slot in FLEX_ELIGIBILITY:
        return FLEX_ELIGIBILITY[slot]
    return frozenset({slot}) if slot in FANTASY_POSITIONS else frozenset()


def starting_slots(roster_positions: Sequence[str]) -> tuple[str, ...]:
    """The starting slots, in `starters` order, with bench and reserve slots dropped."""
    return tuple(slot for slot in roster_positions if slot not in NON_STARTING_SLOTS)


def active_slots(roster_positions: Sequence[str]) -> tuple[str, ...]:
    """The starting and bench slots: the spots an added player has to fit into.

    Reserve capacity is spelled two ways and only one of them is in this list. Both leagues
    checked carry `settings.reserve_slots` and list no "IR" entry at all, while other
    leagues list it among the rest -- and counting those entries would have the tool believe
    there is room on a bench that is full.
    """
    return tuple(slot for slot in roster_positions if slot not in RESERVE_SLOTS)


def reserved(roster: Mapping) -> dict[str, str]:
    """Which of a roster's players are held off the active roster, and in which slot.

    Sleeper's `players` is every player on the roster, IR and taxi squad included, and
    `reserve` and `taxi` name the ones that are not on the active roster. Read without
    them, `players` makes a player on IR look like a player on the bench: startable, and --
    projecting nothing this week -- the cheapest thing on the roster to drop. Matchup
    entries repeat the same combined list, so every roster in the league needs this.
    """
    held = {str(p): "IR" for p in (roster.get("reserve") or ()) if p}
    held.update({str(p): "TAXI" for p in (roster.get("taxi") or ()) if p})
    return held
