"""Trim a raw /players/nfl?active=true response into the players_nfl.json test fixture.

Usage: python tests/fixtures/trim_players.py RAW.json [DATE]

Keeps the top-ranked fantasy-relevant players, every team defense, and a few deliberate
oddballs (a ranked player with no NFL team, a multi-position player) so the pool filters
stay honest. Output matches the on-disk player cache shape.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ffa.sleeper import filter_players  # noqa: E402

TOP_RANKED = 450
ODDBALLS = ("2315", "12530")  # Todd Gurley (ranked, no team), Travis Hunter (DB/WR)
NO_TEAM_SAMPLES = 3


def main() -> None:
    raw = json.loads(Path(sys.argv[1]).read_text())
    stamp = sys.argv[2] if len(sys.argv) > 2 else "2026-09-15"
    players = filter_players(raw)

    ranked = sorted(
        (p for p in players.values() if isinstance(p.get("search_rank"), int) and p["search_rank"] <= 10_000),
        key=lambda p: (p["search_rank"], p["player_id"]),
    )
    # Sleeper's search_rank is a search ordering, not an ADP list: it still ranks retired
    # players (Tom Brady at 74) who carry no team. Those are excluded from the draft pool.
    keep = {p["player_id"]: p for p in ranked if p.get("team")}
    keep = dict(list(keep.items())[:TOP_RANKED])
    keep.update({pid: p for pid, p in players.items() if p.get("position") == "DEF"})
    keep.update({pid: players[pid] for pid in ODDBALLS if pid in players})
    # A ranked player with no NFL team is a stale Sleeper record; keep a few on purpose so
    # the availability filter is exercised.
    no_team = [p for p in ranked if not p.get("team")][:NO_TEAM_SAMPLES]
    keep.update({p["player_id"]: p for p in no_team})

    out = Path(__file__).parent / "players_nfl.json"
    out.write_text(json.dumps({"date": stamp, "players": keep}, sort_keys=True))
    print(f"wrote {out} with {len(keep)} players")


if __name__ == "__main__":
    main()
