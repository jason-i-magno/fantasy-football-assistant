"""Terminal rendering. Every function returns a rich renderable; nothing here fetches."""

from rich.console import Console, Group, RenderableType
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .board import pick_deadline_ms, pick_label, round_for_pick, seconds_remaining
from .models import Player
from .needs import NEED_FLEX, NEED_STARTER
from .session import DraftView
from .shortlist import Shortlist, likely_gone_label, plural

STATUS_STYLES = {
    "drafting": "green",
    "paused": "yellow",
    "pre_draft": "cyan",
    "complete": "dim",
}


def clock_text(view: DraftView, now_ms: int) -> Text:
    state = view.state
    if state.is_complete or state.status == "complete":
        return Text("draft complete", style="dim")
    if state.status == "paused":
        return Text("PAUSED", style="bold yellow")
    if state.status == "pre_draft":
        return Text("not started", style="cyan")
    if state.settings.pick_timer <= 0:
        return Text("no pick timer", style="dim")
    if pick_deadline_ms(state.settings) is None:
        return Text("clock unknown", style="dim")
    left = seconds_remaining(state.settings, now_ms) or 0
    style = "bold red" if left <= 15 else "bold yellow" if left <= 30 else "bold"
    return Text(f"{left // 60}:{left % 60:02d}", style=style)


def _pick_text(pick_no: int | None, teams: int) -> str:
    if pick_no is None:
        return "-"
    return f"{pick_label(pick_no, teams)} (#{pick_no})"


def header_panel(view: DraftView, now_ms: int, note: str | None = None) -> Panel:
    state, teams = view.state, view.state.settings.teams
    # Don't scream "YOUR PICK" before the draft has actually started: the order is often set
    # minutes ahead, and pick 1 is nominally "on the clock" the whole time.
    mine = state.is_my_turn and state.status in ("drafting", "paused")
    lines: list[Text] = []

    if state.is_complete:
        headline = Text("Draft complete", style="bold")
    else:
        current = state.current_pick_no
        headline = Text()
        headline.append(f"Round {round_for_pick(current, teams)} · Pick {_pick_text(current, teams)}", style="bold")
        headline.append(f" of {state.settings.total_picks}  ")
        if mine:
            headline.append("YOUR PICK", style="bold white on red3")
        else:
            headline.append("on the clock: ", style="dim")
            headline.append(state.on_clock_name or "?", style="bold")
            if state.on_clock_via:
                headline.append(f" (traded from {state.on_clock_via})", style="dim")
        headline.append("  ")
        headline.append(clock_text(view, now_ms))
    lines.append(headline)

    second = Text()
    if state.my_slot is not None and state.my_roster_id is None:
        second.append(
            f"Slot {state.my_slot} isn't in this {teams}-team draft — pass --slot 1..{teams}",
            style="bold yellow",
        )
    elif state.my_slot is None:
        second.append("Your draft slot is unknown — waiting for the draft order", style="yellow")
    else:
        second.append(f"You are slot {state.my_slot}", style="dim")
        upcoming = [n for n in state.my_upcoming_picks]
        if upcoming:
            gap = state.picks_until_my_next_turn
            second.append(" · your picks: ")
            second.append(", ".join(_pick_text(n, teams) for n in upcoming), style="bold cyan")
            if gap is not None:
                second.append(
                    f" · {plural(gap, 'selection')} "
                    + ("until you're back up" if mine else "away"),
                    style="dim",
                )
        else:
            second.append(" · no picks left", style="dim")
    lines.append(second)

    status = Text()
    status.append(f"{view.session.draft_id} · ", style="dim")
    status.append(state.status, style=STATUS_STYLES.get(state.status, "white"))
    if note:
        status.append(f" · {note}", style="yellow")
    for warning in view.warnings:
        status.append(f"\n! {warning}", style="yellow")
    lines.append(status)

    return Panel(
        Group(*lines),
        border_style="red" if mine else "blue",
        style="on grey15" if mine else "",
        title="[bold]draftbot[/bold]",
        title_align="left",
    )


def roster_panel(view: DraftView) -> Panel:
    table = Table.grid(padding=(0, 1))
    table.add_column(style="bold", width=6)
    table.add_column(overflow="ellipsis", no_wrap=True)
    keepers = {p.player_id for p in view.state.my_picks if p.is_keeper}
    for slot in view.fill.slots:
        if slot.filled:
            player = slot.player
            label = Text(f"{player.name} ", style="")
            label.append(f"{player.position} {player.team or '--'}", style="dim")
            if player.player_id in keepers:
                label.append(" K", style="magenta")
            if player.injury_status:
                label.append(f" {player.injury_status}", style="red")
            unusable = slot.is_starter and not player.can_play
            if unusable:
                label.append("  NEEDS REPLACEMENT", style="bold yellow")
            table.add_row(
                Text(slot.slot, style="bold yellow" if unusable else "bold" if slot.is_starter else "dim"),
                label,
            )
        elif slot.is_starter:
            table.add_row(Text(slot.slot, style="bold yellow"), Text("— open —", style="yellow"))
        else:
            table.add_row(Text(slot.slot, style="dim"), Text("—", style="dim"))
    open_slots = view.fill.unfilled_starter_slots
    footer = Text(f"\nOpen starters: {', '.join(open_slots)}" if open_slots else "\nStarters full", style="yellow" if open_slots else "green")
    return Panel(Group(table, footer), title="Your roster", border_style="blue", title_align="left")


def _need_label(need: float) -> Text:
    if need >= NEED_STARTER:
        return Text("starter", style="bold green")
    if need >= NEED_FLEX:
        return Text("flex", style="green")
    if need > 0:
        return Text("bench", style="dim")
    return Text("-", style="dim")


def defense_lines(shortlist: Shortlist, limit: int = 12) -> Text:
    """Team defenses have no search_rank in Sleeper's data, so they are listed, not ranked."""
    options = shortlist.defense_options
    shown = ", ".join(p.player_id for p in options[:limit])
    if len(options) > limit:
        shown += f", +{len(options) - limit} more"
    return Text(
        f"Team defenses available, alphabetically — Sleeper publishes no rank for them:\n{shown}",
        style="dim",
    )


CONFIDENCE_STYLES = {"high": "bold green", "medium": "yellow", "low": "dim", "unknown": "dim"}


def research_panel(view: DraftView, research=None, status: str = "off") -> Panel:
    """The recommendation, once research lands. Scarcity lives underneath it."""
    parts: list[RenderableType] = []
    if status == "off":
        parts.append(Text("Research off (no ANTHROPIC_API_KEY).", style="dim italic"))
    elif research is None:
        label = {
            "researching": "Researching candidates…",
            "failed": f"Research unavailable: {view.research_note or 'failed'}",
            "idle": "Research queued…",
        }.get(status, "Research pending…")
        parts.append(Text(label, style="yellow" if status == "failed" else "cyan"))
    else:
        pick = research.recommended()
        headline = Text()
        headline.append("PICK: ", style="dim")
        headline.append(pick.name if pick else "see below", style="bold green")
        if pick and pick.confidence in CONFIDENCE_STYLES:
            headline.append(f"  ({pick.confidence} confidence)", style=CONFIDENCE_STYLES[pick.confidence])
        parts.append(headline)
        parts.append(Text(research.summary, style=""))
        runners = [v for v in research.ranked if not pick or v.player_id != pick.player_id][:3]
        if runners:
            table = Table.grid(padding=(0, 1))
            table.add_column(style="bold", width=18, overflow="ellipsis", no_wrap=True)
            table.add_column(overflow="fold")
            for verdict in runners:
                table.add_row(
                    Text(verdict.name, style=CONFIDENCE_STYLES.get(verdict.confidence, "")),
                    Text(verdict.verdict, style="dim"),
                )
            parts.append(Text("\nalso considered", style="dim"))
            parts.append(table)
        if research.sources:
            parts.append(Text(f"\n{len(research.sources)} sources · {research.elapsed_seconds:.0f}s",
                              style="dim"))
    parts.append(Text(""))
    parts.append(_scarcity_grid(view))
    return Panel(Group(*parts), title="Recommendation", border_style="green" if research else "blue",
                 title_align="left")


def shortlist_table(view: DraftView, research=None, max_rows: int | None = None) -> RenderableType:
    shortlist = view.shortlist
    if shortlist.unavailable:
        return Text(shortlist.unavailable, style="yellow")
    if not shortlist.candidates:
        # The endgame can leave DEF as the only thing left to draft; defenses are never
        # candidates because they carry no rank, so show them as the content, not a caption.
        if shortlist.defense_options:
            return Group(
                Text("Only a team defense is left to fill.", style="bold yellow"),
                defense_lines(shortlist, limit=32),
            )
        return Text("No candidates: every position is blocked by your roster rules.", style="yellow")
    teams = view.state.settings.teams
    table = Table(expand=True, pad_edge=False, box=None, header_style="bold dim")
    table.add_column("#", width=2, justify="right")
    table.add_column("Player", overflow="ellipsis", no_wrap=True)
    table.add_column("Pos", width=3)
    table.add_column("Tm", width=3)
    table.add_column("Rank", width=4, justify="right")
    table.add_column("Adj", width=5, justify="right")
    table.add_column("Drop", width=4, justify="right")
    table.add_column("Need", width=7)
    table.add_column("Gone?", width=5)
    table.add_column("Inj", width=4)
    verdicts = {v.player_id: v for v in research.ranked} if research else {}
    top_pick = research.recommendation_id if research else None
    shown = shortlist.candidates
    hidden = 0
    if max_rows is not None and len(shown) > max_rows:
        # Never crop silently: a candidate the terminal ate looks like a candidate the
        # heuristic rejected.
        shown, hidden = shown[: max(1, max_rows)], len(shown) - max(1, max_rows)
    for i, c in enumerate(shown, start=1):
        p = c.player
        verdict = verdicts.get(p.player_id)
        # Research indicator: a dot per candidate, coloured by confidence, so it costs no
        # width in a table that is already tight.
        name = Text()
        if verdict is not None:
            name.append("★ " if p.player_id == top_pick else "● ",
                        style=CONFIDENCE_STYLES.get(verdict.confidence, "dim"))
        elif research is not None:
            name.append("· ", style="dim")
        name.append(p.name, style="bold" if i == 1 else "")
        table.add_row(
            str(i),
            name,
            p.position,
            p.team or "--",
            str(p.search_rank),
            f"{c.adj:.0f}",
            str(c.dropoff),
            _need_label(c.need),
            Text("gone", style="red") if c.likely_gone else Text("here", style="dim"),
            Text(p.injury_status or "", style="red"),
        )
    caption = Text(f"for pick {_pick_text(shortlist.decision_pick_no, teams)}", style="dim")
    if hidden:
        caption.append(f"  (+{hidden} more — taller terminal, or run `draftbot shortlist`)",
                       style="bold yellow")
    if shortlist.defense_options:
        return Group(table, caption, defense_lines(shortlist))
    return Group(table, caption)


def shortlist_panel(view: DraftView, research=None, max_rows: int | None = None) -> Panel:
    return Panel(
        shortlist_table(view, research, max_rows),
        title=f"Shortlist — 'Gone?' = {likely_gone_label(view.shortlist)}",
        border_style="blue",
        title_align="left",
    )


def _scarcity_grid(view: DraftView) -> RenderableType:
    shortlist = view.shortlist
    if shortlist.unavailable or not shortlist.dropoffs:
        body: RenderableType = Text("—", style="dim")
    else:
        table = Table.grid(padding=(0, 1))
        table.add_column(style="bold", width=4)
        table.add_column(justify="right", width=4)
        table.add_column(overflow="ellipsis")
        for pos, drop in shortlist.dropoffs.items():
            if drop.best_now is None or pos in shortlist.blocked:
                continue
            best_then = drop.best_then.name if drop.best_then else "nobody"
            table.add_row(
                pos,
                Text(str(drop.value), style="bold red" if drop.value >= 20 else ""),
                Text(f"{drop.best_now.name} → {best_then}", style="dim"),
            )
        note = Text(
            f"\nDropoff from {_pick_text(shortlist.decision_pick_no, view.state.settings.teams)}"
            f" to {_pick_text(shortlist.following_pick_no, view.state.settings.teams)},"
            " in search_rank spots.",
            style="dim",
        )
        body = Group(table, note)
    return body


def footer_panel(view: DraftView) -> Panel:
    teams = view.state.settings.teams
    lines: list[RenderableType] = []
    if view.recent:
        table = Table.grid(padding=(0, 1))
        table.add_column(style="dim", width=10)
        table.add_column(width=18, overflow="ellipsis")
        table.add_column(overflow="ellipsis")
        for pick in view.recent:
            player = view.session.players.get(pick.player_id) or Player.from_pick(pick)
            roster = view.state.pick_rosters.get(pick.pick_no)
            manager = view.state.roster_names.get(roster, "?") if roster is not None else "?"
            table.add_row(
                f"{pick_label(pick.pick_no, teams)} #{pick.pick_no}",
                manager,
                f"{player.name} · {player.position} {player.team or '--'}",
            )
        lines.append(table)
    else:
        lines.append(Text("No picks yet.", style="dim"))
    for pos, count, window in view.runs:
        lines.append(Text(f"RUN: {pos} — {count} of the last {window} picks", style="bold magenta"))
    return Panel(Group(*lines), title="Recent picks", border_style="blue", title_align="left")


def dashboard(view: DraftView, *, now_ms: int, note: str | None = None, width: int = 120,
              height: int = 40, research=None, research_status: str = "off") -> Layout:
    """Header / roster / shortlist / recommendation / recent picks, folded to fit the terminal.

    A short terminal shrinks the header and footer before it touches the shortlist, and the
    shortlist trims explicitly rather than letting rich crop rows off the bottom.
    """
    header_size, footer_size = (7, 9) if height >= 40 else (6, 7) if height >= 30 else (5, 5)
    body_rows = max(6, height - header_size - footer_size)
    # Rows the shortlist panel actually gets, minus its border, header row and caption.
    share = 1.0 if width >= 120 else 0.6 if width >= 90 else 3 / 7
    max_rows = max(3, int(body_rows * share) - 4)

    layout = Layout()
    layout.split_column(
        Layout(header_panel(view, now_ms, note), name="header", size=header_size),
        Layout(name="body"),
        Layout(footer_panel(view), name="footer", size=footer_size),
    )
    body = layout["body"]
    if width >= 120:
        body.split_row(
            Layout(roster_panel(view), name="roster", ratio=3),
            Layout(shortlist_panel(view, research, max_rows), name="shortlist", ratio=6),
            Layout(research_panel(view, research, research_status), name="research", ratio=4),
        )
    elif width >= 90:
        body.split_row(
            Layout(roster_panel(view), name="roster", ratio=2),
            Layout(name="right", ratio=5),
        )
        body["right"].split_column(
            Layout(shortlist_panel(view, research, max_rows), name="shortlist", ratio=3),
            Layout(research_panel(view, research, research_status), name="research", ratio=2),
        )
    else:  # narrow terminal: everything stacks, most decision-relevant first
        body.split_column(
            Layout(research_panel(view, research, research_status), name="research", ratio=2),
            Layout(shortlist_panel(view, research, max_rows), name="shortlist", ratio=3),
            Layout(roster_panel(view), name="roster", ratio=2),
        )
    return layout


def print_one_shot(console: Console, view: DraftView, context: str | None, research=None,
                   research_status: str = "off") -> None:
    """Static output for `shortlist` / `recommend`: the board, then a pasteable block."""
    console.print(header_panel(view, view.fetched_at_ms))
    console.print(roster_panel(view))
    console.print(shortlist_panel(view, research))
    console.print(research_panel(view, research, research_status))
    console.print(footer_panel(view))
    if context:
        console.rule("[bold]paste into Claude[/bold]")
        # Plain print: no rich markup or panels, so copy-paste stays clean.
        print(context)
