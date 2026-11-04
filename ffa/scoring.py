"""Scoring a raw Sleeper stat line with a league's own `scoring_settings`.

Sleeper publishes `pts_ppr` on every projection row and `players_points` on played
matchups, and neither answers the question a lineup call actually asks: what would this
player score *in this league* in a week that hasn't happened yet. `pts_ppr` is RotoWire's
number under standard PPR, and `players_points` only exists once the games are over. So
every projection in `ffa/lineup` is re-scored from raw stats here, against the league's own
settings.

Verified against the `.env` league's official `players_points`: 625 of 625 player-weeks
across four completed weeks matched to the cent, kickers and team defenses included. That
check is the gate for trusting any projection, and `tests/test_scoring.py` keeps a scrubbed
slice of it. Re-run it against a completed week after changing anything here.

Two quirks of the projection feed, handled by `clean_projection` rather than here, because
they are artifacts of the *source* and not of scoring:

- **Return yards are double-counted on team defenses.** Every RotoWire D/ST row carries
  `pr_yd`/`kr_yd` (an individual returner's line) alongside the `def_pr_yd`/`def_kr_yd` that
  Sleeper actually scores for the team. A league that scores return yards would pay twice.
- **Points- and yards-allowed buckets are certainties, not probabilities.** RotoWire sets
  the single bucket containing the projected mean to 1.0, so a defense projected to allow 21
  points takes the whole `pts_allow_21_27` value and none of the neighbouring risk. ESPN's
  equivalents are real probabilities, which is why `ffa/projections.py` prefers ESPN for
  team defenses.
"""

from collections.abc import Mapping
from typing import Any

# An individual returner's yardage, duplicated onto the team line RotoWire projects. Sleeper
# scores the team's return game from the `def_`-prefixed keys instead.
DST_DUPLICATE_STATS: tuple[str, ...] = ("pr_yd", "kr_yd", "pr", "kr")

# Keys that are commentary rather than scoring input. `gp` is games played (1.0 on a weekly
# projection) and the `adp_` family is draft ordering; no league scores either, but a typo in
# a custom scoring setting shouldn't silently multiply them.
NON_SCORING_STATS: tuple[str, ...] = ("gp", "adp_dd_ppr", "pos_adp_dd_ppr", "pts_ppr",
                                      "pts_half_ppr", "pts_std")


def as_number(value: Any) -> float | None:
    """Coerce one Sleeper stat value, or None if it isn't a usable number.

    Sleeper mixes ints, floats and numeric strings, and a bool would otherwise score as 1.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def score_stats(stats: Mapping[str, Any] | None, scoring: Mapping[str, Any]) -> float:
    """Points this stat line is worth under `scoring`.

    Only keys the league actually scores contribute, so the many descriptive stats in a
    projection row (`rec_tgt`, `cmp_pct`, the `bonus_*` family a league may not use) are
    ignored. A missing stat is zero, which is what makes a player with no row at all score
    0.0 — exactly how Sleeper reports someone who didn't play.
    """
    if not stats:
        return 0.0
    total = 0.0
    for key, weight in scoring.items():
        if key in NON_SCORING_STATS:
            continue
        w = as_number(weight)
        v = as_number(stats.get(key))
        if w is None or v is None:
            continue
        total += w * v
    return total


def clean_projection(stats: Mapping[str, Any] | None, position: str) -> dict[str, float]:
    """A projection row's stats, with the source's artifacts removed.

    Drops the duplicated return yardage from team-defense rows (see the module docstring),
    and anything that isn't a number, so a malformed row degrades to the stats that parsed.
    """
    if not stats:
        return {}
    drop = DST_DUPLICATE_STATS if position == "DEF" else ()
    cleaned: dict[str, float] = {}
    for key, value in stats.items():
        if key in drop:
            continue
        number = as_number(value)
        if number is not None:
            cleaned[key] = number
    return cleaned
