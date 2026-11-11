"""Every other manager's roster, scored the same way as your own.

Scouting a matchup and valuing a trade are the same question asked about someone else's
players, so this reuses `advice.player_line` rather than computing anything of its own: a
number on another manager's card is the number that would appear on yours if you owned him.

What it deliberately does *not* do is advise. The optimal lineup is solved for every team,
because "he has left eleven points on his bench" is the most useful thing you can know
about an opponent on a Sunday morning, but no swap, add or drop is suggested for a roster
you cannot change. Those are decisions, and they belong to the manager who owns them.

Team names come from Sleeper (`metadata.team_name`, falling back to the manager's display
name), never from config, for the same reason league names do: a second place to keep a
name is a second place for it to be wrong.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ..models import Player, to_int
from ..projections import Projection
from ..roster import reserved, starting_slots
from .advice import Horizon, PlayerLine, player_line
from .optimal import best_lineup


@dataclass(frozen=True, slots=True)
class TeamSeat:
    """One starting slot on someone's roster, and who is in it."""

    slot: str
    line: PlayerLine | None  # None when the manager has left the slot empty


@dataclass(frozen=True, slots=True)
class TeamCard:
    """One manager's week: what is set, what it is worth, and what is left over."""

    roster_id: int
    name: str
    manager: str | None
    is_mine: bool
    is_opponent: bool
    seats: tuple[TeamSeat, ...]
    bench: tuple[PlayerLine, ...]
    total: float  # the lineup as set, in this league's points
    best: float  # the best this roster could field, for the gap below
    notes: tuple[str, ...] = ()

    @property
    def left_on_bench(self) -> float:
        """What the manager is giving up by not fielding his best lineup."""
        return max(0.0, self.best - self.total)


@dataclass(frozen=True, slots=True)
class LeagueView:
    """Every team in the league for one week, yours first and your opponent second."""

    league_name: str
    week: int
    teams: tuple[TeamCard, ...] = ()

    def team(self, roster_id: int | None) -> TeamCard | None:
        return next((t for t in self.teams if t.roster_id == roster_id), None)

    @property
    def opponent(self) -> TeamCard | None:
        return next((t for t in self.teams if t.is_opponent), None)


def team_names(users: Sequence[Mapping], rosters: Sequence[Mapping]) -> dict[int, tuple[str, str | None]]:
    """roster_id -> (what to call the team, the manager behind it).

    Sleeper lets a manager name his team and most do; the ones who don't are known by their
    display name, and a roster whose owner has left the league has neither.
    """
    by_user: dict[str, Mapping] = {str(u.get("user_id") or ""): u for u in users}
    out: dict[int, tuple[str, str | None]] = {}
    for roster in rosters:
        roster_id = to_int(roster.get("roster_id"))
        if roster_id is None:
            continue
        user = by_user.get(str(roster.get("owner_id") or "")) or {}
        manager = str(user.get("display_name") or "") or None
        metadata = user.get("metadata")
        named = str((metadata or {}).get("team_name") or "") if isinstance(metadata, Mapping) else ""
        out[roster_id] = (named or manager or f"Roster {roster_id}", manager)
    return out


def opponent_of(matchups: Sequence[Mapping], roster_id: int | None) -> int | None:
    """The roster facing `roster_id` this week, or None in a week with no matchup.

    Sleeper pairs two rosters by a shared `matchup_id`. A bye week in a league with an odd
    number of teams leaves a roster alone in its matchup, which is not an error.
    """
    if roster_id is None:
        return None
    mine = next((m for m in matchups if to_int(m.get("roster_id")) == roster_id), None)
    if mine is None or mine.get("matchup_id") is None:
        return None
    for entry in matchups:
        if entry.get("matchup_id") == mine.get("matchup_id") and to_int(entry.get("roster_id")) != roster_id:
            return to_int(entry.get("roster_id"))
    return None


def build_league_view(
    *,
    league: Mapping,
    rosters: Sequence[Mapping],
    users: Sequence[Mapping],
    matchups: Sequence[Mapping],
    projections: Mapping[str, Projection],
    players: Mapping[str, Player],
    week: int,
    kickoffs: Mapping[str, object],
    now,
    my_roster_id: int | None = None,
    horizon: Horizon | None = None,
) -> LeagueView:
    """Score every roster in the league for one week. Pure: nothing here fetches."""
    horizon = horizon or Horizon()
    slots = starting_slots(league.get("roster_positions") or ())
    names = team_names(users, rosters)
    against = opponent_of(matchups, my_roster_id)
    by_roster = {to_int(m.get("roster_id")): m for m in matchups}

    cards: list[TeamCard] = []
    for roster in rosters:
        roster_id = to_int(roster.get("roster_id"))
        if roster_id is None:
            continue
        entry = by_roster.get(roster_id) or {}
        # The matchup holds the lineup for *this* week; the roster's own list is whatever is
        # set for Sleeper's current week, which is the right fallback and the wrong answer.
        owned = [str(p) for p in (entry.get("players") or roster.get("players") or ()) if p]
        set_starters = [str(p) for p in (entry.get("starters") or roster.get("starters") or ())]
        # From the roster: a matchup entry repeats the combined player list but carries no
        # reserve list of its own, and every team in the `.env` league had someone on IR.
        held = reserved(roster)

        lines = {
            pid: player_line(player, projections.get(pid), kickoffs=kickoffs, now=now,
                             horizon=horizon, reserved=held.get(pid))
            for pid in owned
            if (player := players.get(pid)) is not None
        }
        seats: list[TeamSeat] = []
        for index, slot in enumerate(slots):
            pid = set_starters[index] if index < len(set_starters) else None
            pid = pid if pid and pid != "0" else None
            seats.append(TeamSeat(slot=slot, line=lines.get(pid) if pid else None))

        started = {seat.line.player_id for seat in seats if seat.line is not None}
        bench = tuple(sorted((l for pid, l in lines.items() if pid not in started),
                             key=lambda l: (-l.points, l.player.name)))
        total = sum(seat.line.points for seat in seats if seat.line is not None)
        best = best_lineup(
            slots,
            {pid: l.points for pid, l in lines.items()},
            {pid: frozenset(l.player.fantasy_positions) for pid, l in lines.items()},
            roster=set(lines),
            # His IR players are not his to field either, and counting them would charge
            # him for leaving points on a bench they are not sitting on.
            unavailable=[pid for pid, l in lines.items() if l.reserved],
        ).total

        name, manager = names.get(roster_id, (f"Roster {roster_id}", None))
        cards.append(TeamCard(
            roster_id=roster_id, name=name, manager=manager,
            is_mine=roster_id == my_roster_id, is_opponent=roster_id == against,
            seats=tuple(seats), bench=bench, total=total, best=best,
            notes=_notes(seats),
        ))

    # Yours first and your opponent next, because those are the two anyone opens; the rest
    # by the lineup they have set, which is the closest thing to a standings order here.
    cards.sort(key=lambda c: (not c.is_mine, not c.is_opponent, -c.total))
    return LeagueView(league_name=str(league.get("name") or ""), week=week, teams=tuple(cards))


def _notes(seats: Sequence[TeamSeat]) -> tuple[str, ...]:
    """What is visibly wrong with a lineup as set, in the order it matters."""
    notes: list[str] = []
    empty = [seat.slot for seat in seats if seat.line is None]
    if empty:
        notes.append(f"{len(empty)} empty starting slot" + ("s" if len(empty) > 1 else ""))
    idle = [seat.line.player.name for seat in seats
            if seat.line is not None and seat.line.points <= 0]
    if idle:
        notes.append(f"starting {len(idle)} player" + ("s" if len(idle) > 1 else "")
                     + " projected to score nothing")
    return tuple(notes)
