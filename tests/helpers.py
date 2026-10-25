"""Builders for hand-made Sleeper payloads used by the unit tests."""

from typing import Any

from draftbot.models import Player

DEFAULT_SETTINGS = {
    "teams": 12,
    "rounds": 16,
    "pick_timer": 90,
    "reversal_round": 0,
    "slots_qb": 1,
    "slots_rb": 2,
    "slots_wr": 2,
    "slots_te": 1,
    "slots_flex": 2,
    "slots_k": 1,
    "slots_def": 1,
    "slots_bn": 6,
}


def make_draft_raw(
    *,
    teams: int = 12,
    rounds: int = 16,
    draft_type: str = "snake",
    status: str = "drafting",
    settings: dict[str, Any] | None = None,
    draft_order: dict[str, int] | None = "default",
    slot_to_roster_id: dict[str, int] | None = "default",
    **overrides: Any,
) -> dict[str, Any]:
    merged = {**DEFAULT_SETTINGS, "teams": teams, "rounds": rounds}
    merged.update(settings or {})
    if draft_order == "default":
        draft_order = {f"user{slot}": slot for slot in range(1, teams + 1)}
    if slot_to_roster_id == "default":
        # Non-identity on purpose: slot N maps to roster N+100.
        slot_to_roster_id = {str(slot): slot + 100 for slot in range(1, teams + 1)}
    raw = {
        "draft_id": "draft1",
        "type": draft_type,
        "status": status,
        "season": "2026",
        "league_id": "league1",
        "start_time": 1_700_000_000_000,
        "last_picked": 1_700_000_100_000,
        "settings": merged,
        "draft_order": draft_order,
        "slot_to_roster_id": slot_to_roster_id,
        "metadata": {"scoring_type": "ppr"},
    }
    raw.update(overrides)
    return raw


def make_pick_raw(pick_no: int, player_id: str, *, teams: int = 12, roster_id: int | None = None,
                  position: str = "RB", team: str = "SF", is_keeper: Any = None,
                  picked_by: str | None = None, omit: tuple[str, ...] = ()) -> dict[str, Any]:
    rnd = (pick_no - 1) // teams + 1
    idx = (pick_no - 1) % teams + 1
    slot = teams - idx + 1 if rnd % 2 == 0 else idx
    raw = {
        "pick_no": pick_no,
        "player_id": player_id,
        "roster_id": slot + 100 if roster_id is None else roster_id,
        "picked_by": f"user{slot}" if picked_by is None else picked_by,
        "round": rnd,
        "draft_slot": slot,
        "is_keeper": is_keeper,
        "draft_id": "draft1",
        "metadata": {
            "first_name": f"Player{player_id}",
            "last_name": "X",
            "position": position,
            "team": team,
            "player_id": player_id,
        },
    }
    for key in omit:
        raw.pop(key, None)
    return raw


def make_player(player_id: str, position: str, rank: int | None, *, name: str | None = None,
                team: str | None = "SF", active: bool = True,
                fantasy_positions: tuple[str, ...] | None = None, **kwargs) -> Player:
    return Player(
        player_id=player_id,
        name=name or f"{position}{player_id}",
        position=position,
        fantasy_positions=fantasy_positions or (position,),
        team=team,
        search_rank=rank,
        active=active,
        **kwargs,
    )


def make_pool(counts: dict[str, int], *, start_rank: int = 1) -> dict[str, Player]:
    """Interleave positions into a ranked pool: {"RB": 3, "WR": 2} -> RB1, WR1, RB2, ..."""
    players: dict[str, Player] = {}
    rank = start_rank
    remaining = dict(counts)
    while any(remaining.values()):
        for position in list(remaining):
            if remaining[position] <= 0:
                continue
            remaining[position] -= 1
            index = counts[position] - remaining[position]
            player_id = f"{position}{index}"
            players[player_id] = make_player(player_id, position, rank)
            rank += 1
    return players
