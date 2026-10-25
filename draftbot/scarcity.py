"""Positional scarcity (value over next available) and the candidate score.

For each position p:

    best_now(p)  = best player at p still there when you make the decision pick
    best_then(p) = best player at p after the `gap` picks up to your following turn
    dropoff(p)   = best_then(p).search_rank - best_now(p).search_rank

"Picks" are simulated crudely: other managers take the top of the pool by search_rank.
When you are on the clock this is exactly gap = my_next_pick_no - current_pick_no. When you
are not, the same comparison is anchored at your upcoming pick, because that is the decision
the shortlist is for: take this position now, or wait a round?
"""

from collections.abc import Sequence
from dataclasses import dataclass

from .models import Player

# ---- Tunable weights ---------------------------------------------------------------
# The score is an adjusted rank in search_rank units (about one pick of ADP each).
# Lower is better. Tune these between drafts.
W_RANK = 1.0  # multiplier on the player's own search_rank
W_NEED_ROUNDS = 1.0  # rounds of search_rank credited at need weight 1.0 (x team count)
W_DROPOFF = 0.5  # fraction of the positional dropoff credited
# Rank spots added to a player who cannot take the field. Keeps Out/IR players draftable as
# stashes while stopping them outranking someone who can start this week.
UNAVAILABLE_PENALTY = 60
DROPOFF_CAP = 48  # keeps an exhausted position from swamping everything else

SCARCITY_POSITIONS = ("QB", "RB", "WR", "TE", "K")  # team defenses have no search_rank


@dataclass(frozen=True, slots=True)
class Dropoff:
    position: str
    best_now: Player | None
    best_then: Player | None
    value: int  # capped, in search_rank spots


def best_by_position(pool: Sequence[Player]) -> dict[str, Player]:
    """First startable player per primary position in a best-first pool.

    Players who cannot play are skipped: an IR receiver sitting at the top of the board
    inflates every dropoff below him and makes the position look like a cliff it isn't.
    """
    best: dict[str, Player] = {}
    for player in pool:
        if player.can_play:
            best.setdefault(player.position, player)
    return best


def positional_dropoffs(
    pool: Sequence[Player], picks_before: int, picks_between: int | None
) -> dict[str, Dropoff]:
    """Dropoff per position.

    pool: ranked available players, best first.
    picks_before: selections before the decision pick (0 when you are on the clock).
    picks_between: selections from the decision pick up to your following pick, including
        your own; None when the decision pick is your last.
    """
    now_pool = pool[picks_before:]
    now_best = best_by_position(now_pool)
    then_best = best_by_position(now_pool[picks_between:]) if picks_between is not None else now_best
    return {
        pos: Dropoff(pos, now_best.get(pos), then_best.get(pos), _dropoff_value(now_best.get(pos), then_best.get(pos)))
        for pos in SCARCITY_POSITIONS
    }


def _dropoff_value(now: Player | None, then: Player | None) -> int:
    if now is None:
        return 0
    if then is None:
        return DROPOFF_CAP  # the position runs dry before you pick again
    return min(DROPOFF_CAP, max(0, then.search_rank - now.search_rank))


def candidate_score(player: Player, need: float, dropoff: int, teams: int) -> float:
    """Adjusted rank blending search_rank, roster need, and positional dropoff. Lower is better."""
    penalty = 0 if player.can_play else UNAVAILABLE_PENALTY
    return W_RANK * player.search_rank - W_NEED_ROUNDS * teams * need - W_DROPOFF * dropoff + penalty
