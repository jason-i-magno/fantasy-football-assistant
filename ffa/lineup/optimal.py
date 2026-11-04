"""The best starting lineup a roster can field, solved exactly.

Every number this tool reports is a difference between two lineups, so the lineup had better
be optimal rather than nearly so. Two tempting shortcuts are both wrong:

- **Greedy filling** ("best QB, then best RB, then...") decides one slot at a time with no
  view of what the flex slots will need, and will spend a FLEX on a third receiver while a
  better tight end sits.
- **Fixing how many start at each position and taking the top k of each** is exact only when
  every player has exactly one position. A dual-eligible player (Sleeper lists some backs as
  RB/WR) appears in two positions' rankings at once, so the count vector gets scored as if
  he filled both slots, and the lineup that comes back is worse than the estimate that chose
  it. This was measured against brute force: 66 of 400 random rosters came out short, by up
  to 12 points.

So this is solved as what it is -- a maximum-weight assignment of players to slots -- with
the Jonker-Volgenant form of the Hungarian algorithm. It is exact for any slot structure,
including superflex, multiple flex types and dual-eligible players, and at a dozen slots
against a couple of dozen players it costs well under a millisecond.

Locks matter because Sleeper freezes a player when his game kicks off. A locked starter
cannot leave his slot and a locked bench player cannot enter the lineup, so both are handed
in as constraints rather than discovered afterwards: advice that says "start him" about a
player whose game finished two hours ago is worse than no advice.
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

from ..roster import eligible_positions

# Stands in for "this player cannot fill this slot". Large enough that no real lineup ever
# prefers an illegal seat, finite so the algorithm's arithmetic stays well defined.
INELIGIBLE = 1e9


@dataclass(frozen=True, slots=True)
class Seat:
    """One starting slot and who is in it."""

    index: int  # position in the league's `starters` array
    slot: str  # "QB", "FLEX", ...
    player_id: str | None = None
    locked: bool = False  # his game has started; this seat cannot change

    @property
    def empty(self) -> bool:
        return self.player_id is None


@dataclass(frozen=True, slots=True)
class Lineup:
    seats: tuple[Seat, ...]
    total: float

    @property
    def started(self) -> frozenset[str]:
        return frozenset(seat.player_id for seat in self.seats if seat.player_id is not None)

    def seat_of(self, player_id: str) -> Seat | None:
        return next((s for s in self.seats if s.player_id == player_id), None)


def _assign(cost: Sequence[Sequence[float]]) -> list[int]:
    """Minimum-cost assignment of every row to a distinct column; returns each row's column.

    The Jonker-Volgenant shortest-augmenting-path form, which needs rows <= columns. Callers
    pad with zero-cost dummy columns so a slot can always be left empty.
    """
    n = len(cost)
    if n == 0:
        return []
    m = len(cost[0])
    inf = float("inf")
    u = [0.0] * (n + 1)  # row potentials
    v = [0.0] * (m + 1)  # column potentials
    match = [0] * (m + 1)  # column -> row (0 means unmatched)
    parent = [0] * (m + 1)  # column -> previous column on the augmenting path
    for row in range(1, n + 1):
        match[0] = row
        col = 0
        minv = [inf] * (m + 1)
        used = [False] * (m + 1)
        while True:
            used[col] = True
            here, delta, next_col = match[col], inf, -1
            for j in range(1, m + 1):
                if used[j]:
                    continue
                reduced = cost[here - 1][j - 1] - u[here] - v[j]
                if reduced < minv[j]:
                    minv[j], parent[j] = reduced, col
                if minv[j] < delta:
                    delta, next_col = minv[j], j
            if next_col == -1:  # no reachable column; the remaining rows stay unassigned
                break
            for j in range(m + 1):
                if used[j]:
                    u[match[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            col = next_col
            if match[col] == 0:
                break
        while col:
            match[col], col = match[parent[col]], parent[col]

    out = [-1] * n
    for j in range(1, m + 1):
        if match[j]:
            out[match[j] - 1] = j - 1
    return out


def best_lineup(
    slot_names: Sequence[str],
    points: Mapping[str, float],
    positions: Mapping[str, frozenset[str]],
    *,
    roster: Collection[str] | None = None,
    pinned: Mapping[int, str] | None = None,
    unavailable: Collection[str] = (),
) -> Lineup:
    """The highest-scoring legal lineup.

    `positions` maps a player to every position he may fill, so a dual-eligible player needs
    no special case. `pinned` maps a slot index to a player who must stay in it (a locked
    starter), and `unavailable` names players who may not be seated at all (a locked bench
    player, or anyone the caller has decided cannot play).
    """
    pinned = dict(pinned or {})
    candidates = sorted(
        (set(roster if roster is not None else points) - set(unavailable)) - set(pinned.values())
    )

    seats: list[Seat] = []
    open_rows: list[int] = []
    for index, slot in enumerate(slot_names):
        if (held := pinned.get(index)) is not None:
            seats.append(Seat(index=index, slot=slot, player_id=held, locked=True))
        else:
            seats.append(Seat(index=index, slot=slot))
            if eligible_positions(slot):  # a slot type we don't model stays empty
                open_rows.append(index)

    if open_rows and candidates:
        # One row per open slot; one column per candidate, plus a dummy column per row so
        # leaving a slot empty is always available and costs nothing.
        width = len(candidates) + len(open_rows)
        cost: list[list[float]] = []
        for index in open_rows:
            allowed = eligible_positions(slot_names[index])
            row = [
                -points.get(p, 0.0) if allowed & positions.get(p, frozenset()) else INELIGIBLE
                for p in candidates
            ]
            cost.append(row + [0.0] * len(open_rows))
        for row_i, col in enumerate(_assign(cost)):
            index = open_rows[row_i]
            if 0 <= col < len(candidates) and cost[row_i][col] < INELIGIBLE:
                seats[index] = Seat(index=index, slot=slot_names[index], player_id=candidates[col])

    filled = tuple(seats)
    total = sum(points.get(s.player_id, 0.0) for s in filled if s.player_id is not None)
    return Lineup(seats=filled, total=total)
