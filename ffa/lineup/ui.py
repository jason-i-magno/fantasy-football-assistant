"""Terminal and JSON rendering for `ffa lineup`. Nothing here fetches.

`advice_json` is what the phone draws, and it carries every number already computed so the
browser never does fantasy arithmetic of its own -- the same contract the draft and waiver
tabs follow.
"""

from collections.abc import Container, Mapping, Sequence
from datetime import datetime
from typing import Any

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from ..playoffs import PlayoffPicture, Swing, TeamOutlook, WinOdds
from ..strength import MIN_WEEKS
from ..waivers.status import FREE_AGENT, WAIVERS
from .advice import AddCandidate, LineupAdvice, PlayerLine
from .league import LeagueView, TeamCard
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
    if sources.rest_weeks or sources.playoff_weeks:
        spans = ", ".join(
            f"{label} is {_span(weeks)}"
            for label, weeks in (("Rest of season", sources.rest_weeks),
                                 ("playoffs", sources.playoff_weeks))
            if weeks
        )
        lines.append(Text(
            f"{spans} -- both projected game by game and re-scored here, not Sleeper's "
            f"preseason season row. That row is still the floor under a drop.",
            style="dim",
        ))
    where = "Sleeper (RotoWire)" + (" blended with ESPN" if sources.espn else " only")
    note = f" ESPN unavailable: {sources.espn_note}" if sources.espn_note else ""
    lines.append(Text(f"Projections: {where}, re-scored with this league's settings.{note}",
                      style="dim"))
    for text in advice.notes:
        lines.append(Text(text, style="dim"))
    return Group(*lines)


def _span(weeks) -> str:
    """A week window as a phrase, so a one-week window doesn't read "weeks 17-17"."""
    return f"week {weeks[0]}" if len(weeks) == 1 else f"weeks {weeks[0]}-{weeks[-1]}"


def lineup_table(advice: LineupAdvice) -> Table:
    """The lineup as it stands, with what the solver would do to each slot."""
    grid = Table(show_edge=False, pad_edge=False, title="Lineup", title_justify="left")
    grid.add_column("Slot")
    grid.add_column("Player")
    grid.add_column("Pos")
    grid.add_column("Proj", justify="right")
    # The same two horizons the bench carries. A starter is a keep-or-trade decision too,
    # and the week he is strong in is not always the week the league is decided in.
    grid.add_column("Rest of season", justify="right")
    grid.add_column("Playoffs", justify="right")
    grid.add_column("Change", style="yellow")
    grid.add_column("Why", style="dim")
    changes = {swap.out.player_id: swap for swap in advice.swaps if swap.out is not None}
    for seat in advice.current.seats:
        line = advice.line_of(seat.player_id)
        if line is None:
            grid.add_row(seat.slot, Text("(empty)", style="dim"), "", "", "", "", "", "")
            continue
        # Only a change to who starts is a change; the solver moving a player between two
        # slots he is both eligible for scores identically and is not worth a word.
        swap = changes.get(line.player_id)
        change = f"→ {swap.into.player.name}" if swap is not None else ""
        grid.add_row(
            seat.slot, line.player.name, line.player.position, _points(line.points),
            _per_game(line.rest), _per_game(line.playoff),
            change, "; ".join(line.reasons),
        )
    return grid


def bench_table(advice: LineupAdvice) -> Table:
    grid = Table(show_edge=False, pad_edge=False, title="Bench", title_justify="left")
    grid.add_column("Player")
    grid.add_column("Pos")
    grid.add_column("Proj", justify="right")
    grid.add_column("Rest of season", justify="right")
    grid.add_column("Playoffs", justify="right")
    grid.add_column("Why", style="dim")
    promoted = {swap.into.player_id for swap in advice.swaps}
    for line in advice.bench:
        name = Text(line.player.name, style="bold green" if line.player_id in promoted else "")
        grid.add_row(name, line.player.position, _points(line.points),
                     _per_game(line.rest), _per_game(line.playoff),
                     "; ".join(line.reasons))
    return grid


def _per_game(value: float | None) -> Text:
    return Text(f"{value:.1f}/g" if value is not None else "—", style="dim")


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
    grid.add_column("Rest of season", justify="right")
    grid.add_column("Playoffs", justify="right")
    grid.add_column("Status")
    grid.add_column("Drop")
    grid.add_column("Why", style="dim")
    for candidate in candidates:
        grid.add_row(
            Text(f"+{candidate.gain:.1f}", style="bold green"),
            candidate.line.player.name,
            candidate.line.player.position,
            _points(candidate.line.points),
            _per_game(candidate.line.rest),
            _per_game(candidate.line.playoff),
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


# -- other managers ---------------------------------------------------------------------


def team_table(card: TeamCard) -> Table:
    """One manager's lineup as set, with the same three numbers your own card carries."""
    title = card.name + (" (you)" if card.is_mine else " -- your opponent" if card.is_opponent else "")
    grid = Table(show_edge=False, pad_edge=False, title=title, title_justify="left")
    grid.add_column("Slot")
    grid.add_column("Player")
    grid.add_column("Pos")
    grid.add_column("Proj", justify="right")
    grid.add_column("Rest of season", justify="right")
    grid.add_column("Playoffs", justify="right")
    grid.add_column("Why", style="dim")
    for seat in card.seats:
        if seat.line is None:
            grid.add_row(seat.slot, Text("(empty)", style="yellow"), "", "", "", "", "")
            continue
        grid.add_row(seat.slot, seat.line.player.name, seat.line.player.position,
                     _points(seat.line.points), _per_game(seat.line.rest),
                     _per_game(seat.line.playoff), "; ".join(seat.line.reasons))
    for line in card.bench:
        grid.add_row(Text("BN", style="dim"), Text(line.player.name, style="dim"),
                     line.player.position, _points(line.points), _per_game(line.rest),
                     _per_game(line.playoff), "; ".join(line.reasons))
    return grid


def team_header(card: TeamCard) -> Text:
    bits = [f"{card.total:.1f} pts as set"]
    if card.left_on_bench >= 0.1:
        bits.append(f"{card.left_on_bench:.1f} left on his bench")
    bits.extend(card.notes)
    return Text("  ".join(bits), style="dim")


def league_render(view: LeagueView) -> RenderableType:
    """Every team in the league, yours first and your opponent next."""
    parts: list[RenderableType] = [
        Text.assemble((view.league_name, "bold"), f" · week {view.week} · every lineup as set"),
        Text(),
    ]
    if view.playoffs is not None:
        parts += [playoffs_table(view), Text()]
    for card in view.teams:
        parts += [team_table(card), team_header(card), Text()]
    return Group(*parts)


def playoffs_table(view: LeagueView) -> RenderableType:
    """The race: what each team needs, and what the coin flips say about it."""
    picture = view.playoffs
    if picture is None:
        return Text()
    weeks = len(picture.weeks_left)
    title = (f"Playoff race — {picture.spots} of {len(view.teams)} make it, "
             f"{weeks} week{'s' if weeks != 1 else ''} left through week {picture.final_week}")
    if not picture.teams:
        return Group(Text(title, style="bold"),
                     Text("  " + "; ".join(picture.notes), style="yellow"))

    names = {card.roster_id: card.name for card in view.teams}
    mine = {card.roster_id for card in view.teams if card.is_mine}
    grid = Table(show_edge=False, pad_edge=False, title=title, title_justify="left")
    grid.add_column("#", justify="right")
    grid.add_column("Team")
    grid.add_column("Rec")
    scoring = picture.scoring
    if scoring is not None:
        grid.add_column("Pts/wk", justify="right")
    grid.add_column("Needs", justify="right")
    grid.add_column("Odds", justify="right")
    grid.add_column("Why", style="dim")
    for outlook in picture.teams:
        name = names.get(outlook.roster_id, f"Roster {outlook.roster_id}")
        label = Text(name, style="bold" if outlook.roster_id in mine else "")
        cut = outlook.seed <= picture.spots
        pace = scoring.team(outlook.roster_id) if scoring is not None else None
        grid.add_row(
            Text(str(outlook.seed), style="green" if cut else "dim"),
            label,
            outlook.standing.label,
            *([Text("—" if pace is None else f"{pace.mean:.0f}", style="dim")]
              if scoring is not None else []),
            _needs_text(outlook),
            _odds_text(outlook),
            _outlook_why(outlook),
        )
    footer = Text(
        "Needs = wins that clinch a place whatever else happens.  "
        f"Odds = {odds_basis(picture)}.",
        style="dim",
    )
    parts: list[RenderableType] = [grid, footer]
    watch = watch_table(picture, names)
    if watch is not None:
        parts += [Text(), watch]
    parts += [Text(), by_wins_table(picture, names, mine)]
    if picture.notes:
        parts.append(Text("; ".join(picture.notes), style="yellow"))
    return Group(*parts)


# Below a swing of this much, a fixture is not worth a line: a third of a point of odds
# is inside the simulation's own noise, never mind the week's.
WORTH_WATCHING = 0.01


def watch_table(picture: PlayoffPicture, names: Mapping[int, str]) -> RenderableType | None:
    """This week's fixtures, by what each one does to your odds.

    The race says what you need; it cannot say which of the games you are not playing in
    decides whether you get it. A manager on the cut line spends Sunday watching other
    people's matchups, and until now the card had nothing to say about them -- the list
    below is read out of the trials already drawn, so it costs nothing to know.
    """
    if picture.focus is None or not picture.swings:
        return None
    worth = [s for s in picture.swings if s.swing >= WORTH_WATCHING]
    if not worth:
        return None
    you = picture.team(picture.focus)
    week = picture.swings[0].week
    grid = Table(show_edge=False, pad_edge=False, title_justify="left",
                 title=f"Worth watching in week {week} — your odds are "
                       f"{(you.odds if you else 0) * 100:.0f}%")
    grid.add_column("Fixture")
    grid.add_column("Root for")
    grid.add_column("Swing", justify="right")
    for swing in worth:
        yours = picture.focus in (swing.first, swing.second)
        fixture = (f"{names.get(swing.first, swing.first)}  v  "
                   f"{names.get(swing.second, swing.second)}")
        root = names.get(swing.root_for, "—") if swing.root_for else "—"
        grid.add_row(
            Text(fixture, style="bold" if yours else ""),
            Text("your win" if yours else root,
                 style="green" if swing.swing >= 0.05 else ""),
            Text(f"{swing.swing * 100:.0f}", style="" if swing.swing >= 0.05 else "dim"),
        )
    return Group(grid, Text(
        "Swing = the gap between your odds if one side wins and your odds if the other "
        "does.", style="dim"))


def by_wins_table(picture: PlayoffPicture, names: Mapping[int, str],
                  mine: Container[int]) -> RenderableType:
    """Every win total still open to each team, and what the coin flips did with it.

    The column a row first turns green is its clinch number, which is the one place the
    guaranteed figure and the simulation are drawn as one thing. They are not the same
    claim even where they agree: a total the sampler never saw miss reads `~100`, and only
    a total that cannot miss reads a flat `100`, because ten thousand coin-flip seasons
    are not a proof and the tilde is where that is said.
    """
    width = max((len(outlook.by_wins) for outlook in picture.teams), default=0)
    if width <= 1:  # nothing left to play: the race table has said everything
        return Text()
    grid = Table(show_edge=False, pad_edge=False, title_justify="left",
                 title="Odds by wins from here — how often this many wins is enough")
    grid.add_column("#", justify="right")
    grid.add_column("Team")
    grid.add_column("Rec")
    for wins in range(width):
        grid.add_column(f"+{wins}", justify="right")
    for outlook in picture.teams:
        name = names.get(outlook.roster_id, f"Roster {outlook.roster_id}")
        cells = [_odds_cell(row) for row in outlook.by_wins]
        cells += [Text("")] * (width - len(cells))  # a team with fewer games left
        grid.add_row(
            Text(str(outlook.seed), style="green" if outlook.seed <= picture.spots else "dim"),
            Text(name, style="bold" if outlook.roster_id in mine else ""),
            outlook.standing.label,
            *cells,
        )
    return Group(grid, Text(
        "Columns are wins from here, so +0 is losing out.  "
        "100 = certain, ~100 = never seen to miss, – = not enough whatever else happens.",
        style="dim",
    ))


def pace_text(picture: PlayoffPicture) -> str:
    """What the Pts/wk column is, for a footnote. Empty when there is no such column."""
    scoring = picture.scoring
    if scoring is None:
        return ""
    return ("Pts/wk is what each team is simulated to score: its own scoring and its roster "
            "on paper, pulled toward the league average by however much of the gap is noise.")


def _odds_cell(row: WinOdds) -> Text:
    """One cell, in a column three characters wide.

    `–` and `100` are arithmetic; everything else is a count of simulated seasons, and the
    two are never written the same way. `<1` is a total the sampler never got in on, which
    is not the same as one it cannot: that is the dash.
    """
    if row.impossible:
        return Text("–", style="dim")
    if row.certain:
        return Text("100", style="bold green")
    if row.odds is None:
        return Text("·", style="dim")  # too rare a total to have been sampled at all
    if row.odds >= 0.995:
        return Text("~100", style="green")
    if row.odds < 0.005:
        return Text("<1", style="dim")
    return Text(f"{max(1, round(row.odds * 100))}",
                style="green" if row.odds >= 0.5 else ("dim" if row.odds < 0.1 else ""))


def odds_basis(picture: PlayoffPicture) -> str:
    """What the odds were simulated from, in the words the card shows.

    The odds have been two different things on this card and must never be allowed to look
    like one. Coin flips are what a league with nothing scored gets; once there are weeks
    to read, the draws come from what the teams actually score. The sentence says which,
    and how much of the gap between the teams the data supports -- because a league whose
    scoring has not separated its teams is back to coin flips, and the card should say so
    rather than let a reader assume a model is at work.
    """
    scoring = picture.scoring
    if scoring is None:
        return "if every game left were a coin flip"
    seasons = f"{picture.trials:,} seasons simulated from "
    weeks = f"{scoring.weeks} scored week{'s' if scoring.weeks != 1 else ''}"
    rosters = "the rosters as they stand"
    if scoring.weeks < MIN_WEEKS:
        return f"{seasons}{rosters}, with too little scored yet to go on"
    if scoring.flat:
        return (f"{seasons}{weeks}, which have not separated these teams at all, so they "
                "are coin flips in all but name")
    if not scoring.reliability:
        # The honest sentence for a league like the second `.env` one: nine weeks of
        # scoring that a variance decomposition cannot tell from nine weeks of noise.
        return (f"{seasons}{rosters} — {weeks} have not separated these teams, so only "
                "the rosters tell them apart")
    both = f"{weeks} and {rosters}" if scoring.projection_weight else weeks
    return (f"{seasons}{both}, {scoring.reliability:.0%} of the scoring gap between teams "
            "holding up")


def _needs_text(outlook: TeamOutlook) -> Text:
    if outlook.eliminated:
        return Text("out", style="red")
    if outlook.clinched:
        return Text("in", style="bold green")
    if outlook.wins_to_clinch is None:
        return Text("—", style="dim")
    return Text(f"{outlook.wins_to_clinch} of {outlook.games_left}")


def _odds_text(outlook: TeamOutlook) -> Text:
    if outlook.odds >= 0.995:
        return Text("~100%", style="green")
    if 0 < outlook.odds < 0.005:
        return Text("<1%", style="red")
    style = "green" if outlook.odds >= 0.5 else ("red" if outlook.odds < 0.1 else "")
    return Text(f"{outlook.odds * 100:.0f}%", style=style)


def _outlook_why(outlook: TeamOutlook) -> str:
    """The part a win total cannot say on its own.

    The dash is the case worth words. A team can be short of any number that clinches and
    still be well alive: three 3-5 teams in the `.env` league win out to the same eight
    wins, and only the one that plays the other two clinches by doing it, because beating
    them is what stops them matching it. Nothing about a win total alone can say that.
    """
    if outlook.eliminated:
        return "out of reach"
    if outlook.clinched:
        return "through already"
    if outlook.wins_to_clinch is None:
        return f"alive at {outlook.wins_to_survive}, no total certain"
    if outlook.wins_to_survive:
        return f"alive at {outlook.wins_to_survive}"
    return ""


def _team_line_json(line: PlayerLine | None) -> dict[str, Any] | None:
    """A slimmer row than `_line_json`: another manager's card shows no advice, so it
    needs no spread, no source list and no drop floor."""
    if line is None:
        return None
    projection = line.projection
    return {
        "player_id": line.player_id,
        "name": line.player.name,
        "position": line.player.position,
        "team": line.player.team,
        "points": round(line.points, 2),
        "rest": None if line.rest is None else round(line.rest, 2),
        "playoff": None if line.playoff is None else round(line.playoff, 2),
        "locked": line.locked,
        "reasons": list(line.reasons),
        "on_bye": bool(projection is not None and projection.on_bye),
    }


def playoffs_json(view: LeagueView) -> dict[str, Any] | None:
    """The race for the phone. Names are joined here so `playoffs.py` stays pure maths."""
    picture = view.playoffs
    if picture is None:
        return None
    names = {card.roster_id: (card.name, card.manager, card.is_mine) for card in view.teams}
    pace = ({} if picture.scoring is None
            else {t.roster_id: round(t.mean, 1) for t in picture.scoring.teams})
    return {
        "spots": picture.spots,
        "final_week": picture.final_week,
        "weeks_left": list(picture.weeks_left),
        "games_left": picture.games_left,
        "trials": picture.trials,
        "notes": list(picture.notes),
        "focus": picture.focus,
        "swings": [
            {
                "week": swing.week,
                "first": swing.first,
                "second": swing.second,
                "first_name": names.get(swing.first, (f"Roster {swing.first}",))[0],
                "second_name": names.get(swing.second, (f"Roster {swing.second}",))[0],
                "if_first": round(swing.if_first, 4),
                "if_second": round(swing.if_second, 4),
                "swing": round(swing.swing, 4),
                "root_for": swing.root_for,
                "mine": picture.focus in (swing.first, swing.second),
            }
            for swing in picture.swings if swing.swing >= WORTH_WATCHING
        ],
        "basis": odds_basis(picture),
        "pace_note": pace_text(picture),
        "scoring": None if picture.scoring is None else {
            "weeks": picture.scoring.weeks,
            "reliability": round(picture.scoring.reliability, 3),
            "league_mean": round(picture.scoring.league_mean, 1),
            "sd": round(picture.scoring.sd, 1),
            "projection_weight": picture.scoring.projection_weight,
            "flat": picture.scoring.flat,
        },
        "teams": [
            {
                "roster_id": outlook.roster_id,
                "name": names.get(outlook.roster_id, (f"Roster {outlook.roster_id}", None, False))[0],
                "manager": names.get(outlook.roster_id, (None, None, False))[1],
                "mine": names.get(outlook.roster_id, (None, None, False))[2],
                "seed": outlook.seed,
                "record": outlook.standing.label,
                "wins": outlook.standing.wins,
                "losses": outlook.standing.losses,
                "ties": outlook.standing.ties,
                "points_for": round(outlook.standing.points_for, 2),
                "games_left": outlook.games_left,
                "needs": outlook.summary,
                "wins_to_clinch": outlook.wins_to_clinch,
                "wins_to_survive": outlook.wins_to_survive,
                "clinched": outlook.clinched,
                "eliminated": outlook.eliminated,
                "in_places": outlook.seed <= picture.spots,
                "odds": round(outlook.odds, 4),
                "pace": pace.get(outlook.roster_id),
                "by_wins": [
                    {
                        "wins": row.wins,
                        "record": row.label,
                        "odds": None if row.odds is None else round(row.odds, 4),
                        "trials": row.trials,
                        "certain": row.certain,
                        "impossible": row.impossible,
                    }
                    for row in outlook.by_wins
                ],
                "why": _outlook_why(outlook),
            }
            for outlook in picture.teams
        ],
    }


def league_json(view: LeagueView) -> list[dict[str, Any]]:
    return [
        {
            "roster_id": card.roster_id,
            "name": card.name,
            "manager": card.manager,
            "mine": card.is_mine,
            "opponent": card.is_opponent,
            "total": round(card.total, 2),
            "best": round(card.best, 2),
            "left_on_bench": round(card.left_on_bench, 2),
            "notes": list(card.notes),
            "lineup": [{"slot": seat.slot, "player": _team_line_json(seat.line)}
                       for seat in card.seats],
            "bench": [_team_line_json(line) for line in card.bench],
        }
        for card in view.teams
    ]


def _line_json(line: PlayerLine) -> dict[str, Any]:
    projection = line.projection
    return {
        "player_id": line.player_id,
        "name": line.player.name,
        "position": line.player.position,
        "team": line.player.team,
        "points": round(line.points, 2),
        "keep": round(line.keep, 2),  # the drop floor, not a figure the card shows
        "rest": None if line.rest is None else round(line.rest, 2),
        "playoff": None if line.playoff is None else round(line.playoff, 2),
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
        "sources": {"sleeper": sources.sleeper, "espn": sources.espn,
                    "espn_note": sources.espn_note,
                    "rest_weeks": list(sources.rest_weeks),
                    "playoff_weeks": list(sources.playoff_weeks)},
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
