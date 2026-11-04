"""Terminal and JSON rendering for `ffa lineup`. Nothing here fetches.

`advice_json` is what the phone draws, and it carries every number already computed so the
browser never does fantasy arithmetic of its own -- the same contract the draft and waiver
tabs follow.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from ..waivers.status import FREE_AGENT, WAIVERS
from .advice import AddCandidate, LineupAdvice, PlayerLine
from .source import LineupSources


def fmt_time(moment: datetime) -> str:
    """`Thu 11-05 6:15 PM MST`, in this machine's timezone."""
    local = moment.astimezone()
    return f"{local:%a %m-%d} {local.hour % 12 or 12}:{local:%M %p %Z}"


def _points(value: float) -> Text:
    return Text(f"{value:.1f}", style="dim" if value <= 0 else "")


def header(advice: LineupAdvice, sources: LineupSources) -> RenderableType:
    lines = [
        Text.assemble(
            (advice.league_name, "bold"),
            f" · week {advice.week} · lineup set for {advice.current.total:.1f} pts",
        )
    ]
    if advice.kickoff is not None:
        lines.append(Text(f"First game {fmt_time(advice.kickoff)}; players lock at their own "
                          f"kickoff.", style="dim"))
    if advice.settled:
        lines.append(Text("Your lineup is already the best this roster can field.", style="green"))
    else:
        lines.append(Text.assemble(
            ("Change it: ", "yellow"),
            (f"+{advice.gain:.1f} pts", "bold yellow"),
            f" available ({advice.current.total:.1f} → {advice.optimal.total:.1f}).",
        ))
    where = "Sleeper (RotoWire)" + (" blended with ESPN" if sources.espn else " only")
    note = f" ESPN unavailable: {sources.espn_note}" if sources.espn_note else ""
    lines.append(Text(f"Projections: {where}, re-scored with this league's settings.{note}",
                      style="dim"))
    for text in advice.notes:
        lines.append(Text(text, style="dim"))
    return Group(*lines)


def lineup_table(advice: LineupAdvice) -> Table:
    """The lineup as it stands, with what the solver would do to each slot."""
    grid = Table(show_edge=False, pad_edge=False, title="Lineup", title_justify="left")
    grid.add_column("Slot")
    grid.add_column("Player")
    grid.add_column("Pos")
    grid.add_column("Proj", justify="right")
    grid.add_column("Change", style="yellow")
    grid.add_column("Why", style="dim")
    changes = {swap.out.player_id: swap for swap in advice.swaps if swap.out is not None}
    for seat in advice.current.seats:
        line = advice.line_of(seat.player_id)
        if line is None:
            grid.add_row(seat.slot, Text("(empty)", style="dim"), "", "", "", "")
            continue
        # Only a change to who starts is a change; the solver moving a player between two
        # slots he is both eligible for scores identically and is not worth a word.
        swap = changes.get(line.player_id)
        change = f"→ {swap.into.player.name}" if swap is not None else ""
        grid.add_row(
            seat.slot, line.player.name, line.player.position, _points(line.points),
            change, "; ".join(line.reasons),
        )
    return grid


def bench_table(advice: LineupAdvice) -> Table:
    grid = Table(show_edge=False, pad_edge=False, title="Bench", title_justify="left")
    grid.add_column("Player")
    grid.add_column("Pos")
    grid.add_column("Proj", justify="right")
    grid.add_column("Rest of season", justify="right")
    grid.add_column("Why", style="dim")
    promoted = {swap.into.player_id for swap in advice.swaps}
    for line in advice.bench:
        name = Text(line.player.name, style="bold green" if line.player_id in promoted else "")
        grid.add_row(name, line.player.position, _points(line.points),
                     Text(f"{line.keep:.1f}/g", style="dim"), "; ".join(line.reasons))
    return grid


def _status_text(candidate: AddCandidate) -> Text:
    status = candidate.status
    if status is None:
        return Text("?", style="dim")
    if status.status == FREE_AGENT:
        return Text("free agent", style="green")
    if status.status == WAIVERS and status.clears_at is not None:
        return Text(f"waivers → {fmt_time(status.clears_at)}", style="yellow")
    return Text(status.status, style="dim")


def adds_table(candidates: Sequence[AddCandidate], title: str, unit: str) -> Table:
    grid = Table(show_edge=False, pad_edge=False, title=title, title_justify="left")
    grid.add_column(unit, justify="right")
    grid.add_column("Player")
    grid.add_column("Pos")
    grid.add_column("Proj", justify="right")
    grid.add_column("Status")
    grid.add_column("Drop")
    grid.add_column("Why", style="dim")
    for candidate in candidates:
        grid.add_row(
            Text(f"+{candidate.gain:.1f}", style="bold green"),
            candidate.line.player.name,
            candidate.line.player.position,
            _points(candidate.line.points),
            _status_text(candidate),
            candidate.drop.player.name if candidate.drop is not None else Text("—", style="dim"),
            "; ".join(candidate.line.reasons),
        )
    return grid


def render(advice: LineupAdvice, sources: LineupSources) -> RenderableType:
    parts: list[RenderableType] = [header(advice, sources), Text(), lineup_table(advice), Text()]
    parts += [bench_table(advice), Text()]
    if advice.adds:
        parts += [adds_table(advice.adds, "Add this week", "pts/wk"), Text()]
    if advice.watch:
        parts += [adds_table(advice.watch, "Worth owning going forward", "pts/g"), Text()]
    if not advice.adds and not advice.watch:
        parts.append(Text("Nothing unrostered would improve this lineup.", style="dim"))
    return Group(*parts)


def _line_json(line: PlayerLine) -> dict[str, Any]:
    projection = line.projection
    return {
        "player_id": line.player_id,
        "name": line.player.name,
        "position": line.player.position,
        "team": line.player.team,
        "points": round(line.points, 2),
        "keep": round(line.keep, 2),
        "locked": line.locked,
        "injury_status": line.player.injury_status,
        "reasons": list(line.reasons),
        "on_bye": bool(projection is not None and projection.on_bye),
        "sleeper": None if projection is None or projection.sleeper is None else round(projection.sleeper, 2),
        "espn": None if projection is None or projection.espn is None else round(projection.espn, 2),
        "spread": None if projection is None or projection.spread is None else round(projection.spread, 2),
        "disputed": bool(projection is not None and projection.disputed),
        "sources": list(projection.sources) if projection is not None else [],
    }


def _candidate_json(candidate: AddCandidate) -> dict[str, Any]:
    status = candidate.status
    return {
        "gain": round(candidate.gain, 2),
        "player": _line_json(candidate.line),
        "drop": _line_json(candidate.drop) if candidate.drop is not None else None,
        "status": status.status if status is not None else None,
        "clears_at": status.clears_at.isoformat() if status is not None and status.clears_at else None,
        "available_now": candidate.available_now,
    }


def _seat_json(seat, advice: LineupAdvice, changes) -> dict[str, Any]:
    """One starting slot: who is in it, and who should be."""
    line = advice.line_of(seat.player_id)
    swap = changes.get(line.player_id) if line is not None else None
    return {
        "slot": seat.slot,
        "player": _line_json(line) if line is not None else None,
        "swap_to": _line_json(swap.into) if swap is not None else None,
    }


def advice_json(advice: LineupAdvice, sources: LineupSources) -> dict[str, Any]:
    """Everything the phone draws, with no arithmetic left for the browser."""
    changes = {swap.out.player_id: swap for swap in advice.swaps if swap.out is not None}
    return {
        "league": advice.league_name,
        "week": advice.week,
        "now": advice.now.isoformat(),
        "first_kickoff": advice.kickoff.isoformat() if advice.kickoff is not None else None,
        "settled": advice.settled,
        "current_total": round(advice.current.total, 2),
        "optimal_total": round(advice.optimal.total, 2),
        "gain": round(advice.gain, 2),
        "sources": {"sleeper": sources.sleeper, "espn": sources.espn, "espn_note": sources.espn_note},
        "notes": list(advice.notes),
        "lineup": [_seat_json(seat, advice, changes) for seat in advice.current.seats],
        "bench": [_line_json(line) for line in advice.bench],
        "swaps": [
            {
                "slot": swap.slot,
                "out": _line_json(swap.out) if swap.out is not None else None,
                "in": _line_json(swap.into),
                "gain": round(swap.gain, 2),
            }
            for swap in advice.swaps
        ],
        "adds": [_candidate_json(c) for c in advice.adds],
        "watch": [_candidate_json(c) for c in advice.watch],
    }
