"""Generate replay_mock_12team.jsonl: a synthetic in-progress 12-team draft.

Real player data (tests/fixtures/players_nfl.json), synthetic picks. Recorded drafts from a
live mock (`draftbot watch --record`) are the real thing; this exists so the pipeline has a
deterministic in-progress board to run against offline, including the late rounds where K
and DEF appear.

The pick order is computed here from first principles rather than imported from
draftbot.board, so the fixture stays an independent check of the snake math.

Usage: python tests/fixtures/build_synthetic_replay.py
"""

import json
import random
from pathlib import Path

HERE = Path(__file__).parent
TEAMS = 12
ROUNDS = 16
PICK_TIMER = 90
MY_SLOT = 8
SEED = 20260915
START_MS = 1789000000000  # arbitrary fixed UTC epoch ms
SECONDS_PER_PICK = 45

ROSTER_POSITIONS = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "FLEX", "K", "DEF"] + ["BN"] * 6
# Deliberately not the identity mapping, so slot -> roster_id translation is exercised.
SLOT_TO_ROSTER = {1: 10, 2: 3, 3: 12, 4: 5, 5: 1, 6: 8, 7: 2, 8: 7, 9: 11, 10: 4, 11: 6, 12: 9}
CPU_SLOT = 12  # no user attached: draft_order skips it and picked_by is empty
# Slot 5's third-round pick was traded to slot 11.
TRADED = [{"season": "2026", "round": 3, "roster_id": SLOT_TO_ROSTER[5],
           "previous_owner_id": SLOT_TO_ROSTER[5], "owner_id": SLOT_TO_ROSTER[11]}]

SNAPSHOT_PICK_COUNTS = [0, 7, 16, 28, 30, 100, 175, 191, 192]
POSITION_LIMITS = {"QB": 2, "RB": 6, "WR": 7, "TE": 2, "K": 1, "DEF": 1}
STARTERS_NEEDED = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "K": 1, "DEF": 1}


def slot_for_pick(pick_no: int) -> int:
    rnd = (pick_no - 1) // TEAMS + 1
    idx = (pick_no - 1) % TEAMS + 1
    return TEAMS - idx + 1 if rnd % 2 == 0 else idx


def owner_roster(pick_no: int) -> int:
    roster = SLOT_TO_ROSTER[slot_for_pick(pick_no)]
    rnd = (pick_no - 1) // TEAMS + 1
    for trade in TRADED:
        if trade["round"] == rnd and trade["roster_id"] == roster:
            return trade["owner_id"]
    return roster


def user_id_for_slot(slot: int) -> str:
    return "" if slot == CPU_SLOT else f"100000000000000{slot:02d}"


def build_picks(players: dict) -> list[dict]:
    """Simulate a full draft: managers take a near-best available player they still need."""
    rng = random.Random(SEED)
    pool = sorted(
        (p for p in players.values() if p.get("team") and p.get("position") != "DEF"
         and isinstance(p.get("search_rank"), int)),
        key=lambda p: (p["search_rank"], p["player_id"]),
    )
    defenses = sorted((p for p in players.values() if p.get("position") == "DEF"),
                      key=lambda p: p["player_id"])
    roster_to_slot = {roster: slot for slot, roster in SLOT_TO_ROSTER.items()}
    counts: dict[int, dict[str, int]] = {roster: {} for roster in SLOT_TO_ROSTER.values()}
    taken: set[str] = set()
    picks = []

    for pick_no in range(1, TEAMS * ROUNDS + 1):
        rnd = (pick_no - 1) // TEAMS + 1
        roster = owner_roster(pick_no)
        have = counts[roster]
        late = rnd > ROUNDS - 2

        def wanted(position: str) -> bool:
            if position in ("K", "DEF"):
                return late and have.get(position, 0) < 1
            if have.get(position, 0) >= POSITION_LIMITS.get(position, 99):
                return False
            return True

        if late and have.get("DEF", 0) < 1 and rng.random() < 0.5:
            candidates = [d for d in defenses if d["player_id"] not in taken][:3]
        else:
            candidates = []
            for player in pool:
                if player["player_id"] in taken:
                    continue
                position = player["position"] if player["position"] in POSITION_LIMITS else "WR"
                if wanted(position):
                    candidates.append(player)
                if len(candidates) == 3:
                    break
        if not candidates:
            candidates = [p for p in pool if p["player_id"] not in taken][:1]
        chosen = rng.choice(candidates)
        taken.add(chosen["player_id"])
        position = chosen["position"] if chosen["position"] in POSITION_LIMITS else "WR"
        have[position] = have.get(position, 0) + 1

        slot = slot_for_pick(pick_no)
        picks.append({
            "draft_id": "999000000000000001",
            "draft_slot": slot,
            "is_keeper": None,
            "metadata": {
                "first_name": chosen.get("first_name", ""),
                "last_name": chosen.get("last_name", ""),
                "injury_status": chosen.get("injury_status") or "",
                "number": str(chosen.get("number") or ""),
                "player_id": chosen["player_id"],
                "position": chosen.get("position", ""),
                "sport": "nfl",
                "status": chosen.get("status") or "Active",
                "team": chosen.get("team") or "",
            },
            "pick_no": pick_no,
            "picked_by": user_id_for_slot(roster_to_slot.get(roster, slot)),
            "player_id": chosen["player_id"],
            "reactions": None,
            "roster_id": roster,
            "round": rnd,
        })
    return picks


def draft_object(status: str, pick_count: int, *, order: bool) -> dict:
    return {
        "created": START_MS - 86_400_000,
        "creators": None,
        "draft_id": "999000000000000001",
        "draft_order": (
            {user_id_for_slot(slot): slot for slot in range(1, TEAMS + 1) if slot != CPU_SLOT}
            if order else None
        ),
        "last_picked": START_MS + pick_count * SECONDS_PER_PICK * 1000 if pick_count else None,
        "league_id": "999000000000000000",
        "metadata": {"description": "", "name": "Synthetic Mock", "scoring_type": "ppr"},
        "season": "2026",
        "season_type": "regular",
        "settings": {
            "alpha_sort": 0,
            "cpu_autopick": 1,
            "enforce_position_limits": 1,
            "pick_timer": PICK_TIMER,
            "player_type": 0,
            "reversal_round": 0,
            "rounds": ROUNDS,
            "slots_bn": 6,
            "slots_def": 1,
            "slots_flex": 2,
            "slots_k": 1,
            "slots_qb": 1,
            "slots_rb": 2,
            "slots_te": 1,
            "slots_wr": 2,
            "teams": TEAMS,
        },
        "slot_to_roster_id": {str(slot): roster for slot, roster in SLOT_TO_ROSTER.items()},
        "sport": "nfl",
        "start_time": START_MS,
        "status": status,
        "type": "snake",
    }


def main() -> None:
    fixture = json.loads((HERE / "players_nfl.json").read_text())
    players = fixture["players"]
    picks = build_picks(players)

    users = [
        {"user_id": user_id_for_slot(slot), "display_name": f"Manager {slot}",
         "username": f"manager{slot}", "league_id": "999000000000000000"}
        for slot in range(1, TEAMS + 1) if slot != CPU_SLOT
    ]
    header = {
        "format": "draftbot-recording/1",
        "notes": "Synthetic 12-team snake draft over a real (trimmed) Sleeper player map.",
        "recorded_at": "2026-09-15T00:00:00Z",
        "username": "draftbot_tester",
        "user": {"user_id": user_id_for_slot(MY_SLOT), "display_name": "Manager 8",
                 "username": "draftbot_tester"},
        "draft_id": "999000000000000001",
        "league": {
            "league_id": "999000000000000000",
            "name": "Synthetic Mock League",
            "season": "2026",
            "status": "in_season",
            "total_rosters": TEAMS,
            "roster_positions": ROSTER_POSITIONS,
            "scoring_settings": {"rec": 1.0, "pass_td": 4.0, "pass_yd": 0.04, "rush_yd": 0.1,
                                 "rec_yd": 0.1, "pass_int": -2.0, "fum_lost": -2.0,
                                 "rush_td": 6.0, "rec_td": 6.0, "bonus_rec_te": 0.0},
        },
        "league_users": users,
        "players_date": fixture["date"],
        "players": players,
    }

    lines = [json.dumps(header, sort_keys=True)]
    snapshots = [("pre_draft", 0, False)] + [
        ("complete" if n == TEAMS * ROUNDS else "drafting", n, True) for n in SNAPSHOT_PICK_COUNTS
    ]
    for status, count, order in snapshots:
        lines.append(json.dumps({
            "fetched_at_ms": START_MS + count * SECONDS_PER_PICK * 1000 + 5_000,
            "draft": draft_object(status, count, order=order),
            "picks": picks[:count],
            "traded_picks": TRADED,
        }, sort_keys=True))

    out = HERE / "replay_mock_12team.jsonl"
    out.write_text("\n".join(lines) + "\n")
    print(f"wrote {out} with {len(lines) - 1} snapshots")


if __name__ == "__main__":
    main()
