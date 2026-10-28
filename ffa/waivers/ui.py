"""Terminal and JSON rendering for `ffa waivers`. Nothing here fetches."""

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from .status import FREE_AGENT, WAIVERS, WaiverReport, WaiverStatus


def fmt_time(moment: datetime) -> str:
    """`Wed 10-28 1:05 AM MDT`, in this machine's timezone."""
    local = moment.astimezone()
    return f"{local:%a %m-%d} {local.hour % 12 or 12}:{local:%M %p %Z}"


def _hours(moment: datetime, now: datetime) -> str:
    hours = (moment - now).total_seconds() / 3600
    return f"in {hours:.0f}h" if hours < 48 else f"in {hours / 24:.1f}d"


def header(report: WaiverReport) -> RenderableType:
    next_clear = report.rules.after_game_clear(report.now)
    hold_hours = report.rules.drop_hold.total_seconds() / 3600
    lines = [
        Text.assemble((report.league_name, "bold"), f" · NFL week {report.week} · {fmt_time(report.now)}"),
        Text(f"After-game waivers next clear {fmt_time(next_clear)} (12:05 AM Pacific). "
             f"Dropped players are held {hold_hours:g}h.", style="dim"),
    ]
    week_clear = (
        report.rules.after_game_clear(report.week_closed) if report.week_closed is not None else None
    )
    week_over = week_clear is not None and week_clear > report.now
    if week_over:
        lines.append(Text.assemble(
            ("Every unrostered player is on waivers", "yellow"),
            f" until {fmt_time(week_clear)}: the week's last game kicked off "
            f"{fmt_time(report.week_closed)}, so teams on bye are locked too.",
        ))
    by_clear: dict[datetime, list[str]] = {}
    for team, clear in report.locked_teams.items():
        by_clear.setdefault(clear, []).append(team)
    for clear, teams in sorted(by_clear.items()):
        lines.append(Text.assemble(
            "Locked after kickoff: ", (", ".join(sorted(teams)), "yellow"), f" until {fmt_time(clear)}"
        ))
    if not report.locked_teams and not week_over:
        lines.append(Text("No team is locked right now: no game has kicked off since the last clear."))
    return Group(*lines)


def table(statuses: Sequence[WaiverStatus], now: datetime) -> Table:
    grid = Table(show_edge=False, pad_edge=False)
    grid.add_column("Player")
    grid.add_column("Pos")
    grid.add_column("Team")
    grid.add_column("Status")
    grid.add_column("Clears")
    grid.add_column("Why", style="dim")
    for s in statuses:
        if s.status == FREE_AGENT:
            status, clears = Text("free agent", style="green"), ""
        elif s.status == WAIVERS:
            status = Text("waivers", style="yellow")
            clears = f"{fmt_time(s.clears_at)} ({_hours(s.clears_at, now)})"
        else:
            status, clears = Text(f"rostered: {s.rostered_by}", style="dim"), ""
        why = []
        if s.kicked_off is not None:
            why.append(f"game kicked off {fmt_time(s.kicked_off)}")
        if s.dropped_at is not None:
            why.append(f"dropped {fmt_time(s.dropped_at)}")
        if s.week_closed is not None:
            why.append(f"week's last game kicked off {fmt_time(s.week_closed)}")
        grid.add_row(s.player.name, s.player.position, s.player.team or "FA", status, clears, "; ".join(why))
    return grid


def report_json(report: WaiverReport, statuses: Sequence[WaiverStatus]) -> dict[str, Any]:
    def iso(moment: datetime | None) -> str | None:
        return moment.isoformat() if moment is not None else None

    return {
        "league": report.league_name,
        "week": report.week,
        "now": iso(report.now),
        "next_after_game_clear": iso(report.rules.after_game_clear(report.now)),
        "drop_hold_hours": report.rules.drop_hold.total_seconds() / 3600,
        "locked_teams": {team: iso(clear) for team, clear in sorted(report.locked_teams.items())},
        "week_closed": iso(report.week_closed),
        "players": [
            {
                "player_id": s.player.player_id,
                "name": s.player.name,
                "position": s.player.position,
                "team": s.player.team,
                "status": s.status,
                "clears_at": iso(s.clears_at),
                "kicked_off": iso(s.kicked_off),
                "dropped_at": iso(s.dropped_at),
                "week_closed": iso(s.week_closed),
                "rostered_by": s.rostered_by,
            }
            for s in statuses
        ],
    }
