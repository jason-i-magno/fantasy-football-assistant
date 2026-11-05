"""What to change about your lineup for the week you can still change.

The report is built in one direction only: fetch, project, solve, difference. Nothing here
talks to the network -- `ffa/cli.py` and `ffa/web/state.py` hand in a client and a schedule
function, which is what keeps this testable offline against recorded payloads.

Two rules from hard experience shape the whole thing:

- **Value is lineup impact, never raw points.** A player's worth is what he changes about the
  *optimal starting lineup*, so every number reported here is a difference between two
  solved lineups. Points on the bench are worth zero, and a waiver add who would never crack
  the lineup is worth exactly zero no matter how good he looks in isolation.
- **"This week" means the week you can still change.** The target week is the earliest one
  with games still to kick off, which keeps it on the coming Sunday all the way through that
  Sunday's late games, and rolls over only once the last game of the week has started. The
  report always names the week it used rather than leaving you to guess.
"""

import logging
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from ..models import Player, to_int
from ..projections import SLEEPER_ONLY_POSITIONS, SPREAD_FLAG, Projection
from ..roster import starting_slots
from ..waivers.status import FREE_AGENT, ROSTERED, WaiverReport, WaiverStatus
from .optimal import Lineup, Seat, best_lineup

log = logging.getLogger(__name__)

LAST_REGULAR_WEEK = 18
# How many unrostered players to solve a lineup for. They are considered best-projection
# first, and a full solve is well under a millisecond, so this is generous on purpose.
ADD_POOL = 60
# Report at most this many adds, and only ones that actually change the lineup by this much.
MAX_ADDS = 6
# The watchlist is a different question from the add list, so it gets its own cap.
MAX_WATCH = 4
# One bye at a position would otherwise crowd the list with interchangeable replacements.
MAX_ADDS_PER_POSITION = 2
MIN_ADD_GAIN = 0.1
# A whole week's rearranging worth less than this is noise dressed as advice: two
# projections inside a tenth of a point are the same number, and "go change your lineup" is
# the wrong conclusion to draw from one.
MIN_SWAP_GAIN = 0.1
# What one fantasy playoff week is worth, counted in regular-season weeks, when valuing a
# player beyond this Sunday. A roster spot carried into December is carried for the weeks
# that decide the league; two is the mildest number that says so out loud.
PLAYOFF_WEEK_WEIGHT = 2.0


class NoRosterError(Exception):
    """The configured user has no roster in this league."""


@dataclass(frozen=True, slots=True)
class PlayerLine:
    """One player as the lineup card shows him."""

    player: Player
    projection: Projection | None
    points: float  # what he is worth to the lineup, after byes and injuries zero him out
    seat: Seat | None = None  # the slot he currently occupies, None when benched
    locked: bool = False  # his game has kicked off; Sleeper will not let him move
    reasons: tuple[str, ...] = ()  # why his points are what they are
    keep: float = 0.0  # the stale season row per game; the floor on what dropping him costs
    rest: float | None = None  # fresh per game over every week still to play
    playoff: float | None = None  # per game over the league's remaining playoff weeks

    @property
    def player_id(self) -> str:
        return self.player.player_id

    @property
    def starting(self) -> bool:
        return self.seat is not None


@dataclass(frozen=True, slots=True)
class Swap:
    """One change to make: who comes out of a slot and who goes in."""

    slot: str
    slot_index: int
    out: PlayerLine | None
    into: PlayerLine
    gain: float  # points this single change is worth, in isolation


@dataclass(frozen=True, slots=True)
class AddCandidate:
    """An unrostered player worth claiming, and what it would cost to fit him."""

    line: PlayerLine
    gain: float  # lineup points per week, after the drop
    drop: PlayerLine | None
    status: WaiverStatus | None

    @property
    def available_now(self) -> bool:
        return self.status is not None and self.status.status == FREE_AGENT

    @property
    def clears_at(self) -> datetime | None:
        return self.status.clears_at if self.status is not None else None


@dataclass(frozen=True)
class LineupAdvice:
    league_name: str
    week: int
    now: datetime
    roster_id: int
    slots: tuple[str, ...]
    current: Lineup
    optimal: Lineup
    lines: dict[str, PlayerLine]
    swaps: tuple[Swap, ...]
    adds: tuple[AddCandidate, ...]
    watch: tuple[AddCandidate, ...] = ()
    kickoff: datetime | None = None  # the week's first game, for "lineup locks in" copy
    notes: tuple[str, ...] = ()

    @property
    def gain(self) -> float:
        return self.optimal.total - self.current.total

    @property
    def settled(self) -> bool:
        """True when the lineup is already optimal and nothing needs doing."""
        return not self.swaps

    def line_of(self, player_id: str | None) -> PlayerLine | None:
        return self.lines.get(player_id) if player_id else None

    @property
    def bench(self) -> tuple[PlayerLine, ...]:
        """Benched players, best first."""
        return tuple(
            sorted((l for l in self.lines.values() if not l.starting),
                   key=lambda l: (-l.points, l.player.name))
        )


def target_week(schedules: Mapping[int, Mapping[str, datetime]], now: datetime) -> int | None:
    """The earliest week that still has a game to kick off.

    Not "the first week with no game started": that would skip to next week halfway through
    a Sunday, when most of the lineup is still changeable. Not Sleeper's `nfl_state.week`
    either, which flips on its own schedule -- it read 9 while the app was still showing
    week 8's results.
    """
    future = [week for week, kickoffs in sorted(schedules.items())
              if kickoffs and max(kickoffs.values()) > now]
    return future[0] if future else None


def resolve_roster(rosters: Sequence[Mapping], user_id: str) -> Mapping:
    """The configured user's roster, co-owned ones included."""
    for roster in rosters:
        if str(roster.get("owner_id") or "") == user_id:
            return roster
    for roster in rosters:
        if user_id in {str(c) for c in (roster.get("co_owners") or ())}:
            return roster
    raise NoRosterError("The configured Sleeper user has no roster in this league.")


@dataclass(frozen=True, slots=True)
class Horizon:
    """What a player is worth from here, in one definition used on both sides of a move.

    A drop and an add are the same judgement pointed in opposite directions, so the floor
    under a drop and the ranking behind "worth owning" have to be the same number or the
    tool will drop a player for a weakness it would not have noticed in a claim.

    A playoff week counts `PLAYOFF_WEEK_WEIGHT` regular-season weeks. Because the rest-of-
    season window shrinks as the season runs out while the playoff window does not, that
    single rule tightens the weighting on its own and needs no date arithmetic: with the
    weight at 2.0 and playoffs in weeks 15-17, half the weight sits on them in week 9 and
    six sevenths of it by week 14.

    It is written against the two means already computed rather than against the weeks
    themselves. The rest window *contains* the playoff weeks, so counting a playoff week
    twice is the same as adding one extra, playoff-only copy of that mean to the average:
    `(R*rest + (w-1)*P*playoff) / (R + (w-1)*P)`.
    """

    rest_weeks: int = 0
    playoff_weeks: int = 0

    def later(self, projection: Projection) -> float:
        """His per-week value from here, with no regard for the week in front of you."""
        rest, playoff = projection.rest_per_game, projection.playoff_per_game
        if rest is None:
            # Nobody is projecting him in any week still to be played. The preseason row is
            # all that is left, and it beats scoring him zero: zero would make the one
            # player no feed has an opinion on the obvious man to drop.
            return projection.season_per_game or 0.0
        extra = (PLAYOFF_WEEK_WEIGHT - 1.0) * self.playoff_weeks
        if playoff is None or not self.rest_weeks or extra <= 0:
            return rest
        return (self.rest_weeks * rest + extra * playoff) / (self.rest_weeks + extra)

    def keep(self, projection: Projection | None, this_week: float) -> float:
        """What dropping him costs, per week. The floor under every drop.

        The week in front of you is deliberately not part of it. It used to be, as
        `max(this_week, ...)`, to stop a bye from making a player look worthless back when
        the alternative was a preseason row that could be absurdly wrong; a forward mean
        over every remaining week handles a bye on its own. Keeping the max would have
        blunted the playoff weighting on precisely the players it decides between, because
        a drop is chosen on this week's lineup first and the floor only breaks the ties --
        which is to say, among players who are not starting, whose points this week are
        points they will not score for you.

        This week is the fallback only when nothing at all is known about the weeks after
        it, where the alternative is scoring him zero and naming him the obvious drop.
        """
        if projection is None:
            return this_week
        later = self.later(projection)
        return later if later > 0.0 else this_week


def _effective(
    player: Player, projection: Projection | None, *, playing: bool
) -> tuple[float, tuple[str, ...]]:
    """What a player is worth to the lineup, and why.

    A projection is not the last word: Sleeper's own feeds happily project a player his team
    has ruled out, so an explicit Out/IR designation wins over the number. "Questionable" is
    deliberately left alone -- it is the designation most likely to be a stale in-game tag
    that nobody cleared, and benching a healthy starter over one costs more than it saves.
    """
    reasons: list[str] = []
    points = projection.points if projection is not None else 0.0
    if projection is not None and projection.no_team:
        return 0.0, ("not on an NFL team",)
    if projection is not None and projection.on_bye:
        return 0.0, ("on bye",)
    if not playing:
        return 0.0, ("his team has no game this week",)
    if not player.can_play:
        label = (player.injury_status or player.status or "unavailable").lower()
        return 0.0, (f"listed {label}",)
    if player.injury_status and player.injury_status.upper() not in {"NA"}:
        reasons.append(f"listed {player.injury_status.lower()}")
    if projection is None:
        reasons.append("no projection from either source")
    elif projection.disputed:
        spread = projection.spread or 0.0
        reasons.append(f"sources disagree by {spread:.1f} pts")
    elif (
        projection.single_source
        and projection.sources
        and player.position not in SLEEPER_ONLY_POSITIONS
    ):
        # Kickers come from one source by design, so saying so every week is noise.
        reasons.append(f"{projection.sources[0]} only")
    return points, tuple(reasons)


def _swaps(current: Lineup, optimal: Lineup, lines: Mapping[str, PlayerLine]) -> tuple[Swap, ...]:
    """The changes that actually matter: who starts, not which slot he starts in.

    Comparing seat by seat looks right and is wrong. A lineup scores the same however its
    eligible players are distributed across eligible slots, so the solver is free to seat a
    receiver in WR where you had him in FLEX. Reported seat-wise, that reads as two
    confident swaps worth half a point each on a week where the real gain is zero. Only a
    change to the *set* of starters is a change at all.
    """
    coming = sorted(
        (l for pid in optimal.started - current.started if (l := lines.get(pid))),
        key=lambda l: (-l.points, l.player.name),
    )
    leaving = sorted(
        (l for pid in current.started - optimal.started if (l := lines.get(pid))),
        key=lambda l: (-l.points, l.player.name),
    )
    out: list[Swap] = []
    for index, into in enumerate(coming):
        was = leaving[index] if index < len(leaving) else None
        seat = optimal.seat_of(into.player_id)
        gain = into.points - (was.points if was is not None else 0.0)
        out.append(Swap(
            slot=seat.slot if seat else "", slot_index=seat.index if seat else -1,
            out=was, into=into, gain=gain,
        ))
    return tuple(sorted(out, key=lambda s: -s.gain))


# Two drops whose effect on this week differs by less than this are the same drop, and the
# choice between them should be made on the rest of the season instead.
DROP_TIE = 0.05


def _drop_choice(
    candidates: Sequence[PlayerLine], solve: Callable[[frozenset[str]], float], roster: frozenset[str]
) -> tuple[PlayerLine | None, float]:
    """The cheapest player to drop, and the lineup total left behind.

    Chosen on this week's lineup first, then -- among drops this week cannot tell apart --
    on the lowest value from here (`Horizon.keep`, playoff weeks weighted). That second key
    is doing most of the work in practice: a bench player's departure leaves this week's
    lineup untouched, so every bench drop ties and the tie-break decides. It is also what
    stops the obvious disaster: judged on one week alone, every starter on a bye projects
    zero and so looks free to drop, and the tool would cheerfully trade a season-long
    starter for a one-week streamer.
    """
    best: tuple[PlayerLine | None, float] = (None, float("-inf"))
    for line in candidates:
        total = solve(roster - {line.player_id})
        if best[0] is None or total > best[1] + DROP_TIE:
            best = (line, total)
        elif abs(total - best[1]) <= DROP_TIE and line.keep < best[0].keep:
            best = (line, max(total, best[1]))
    return best


def build_advice(
    *,
    report: WaiverReport,
    league: Mapping,
    rosters: Sequence[Mapping],
    user_id: str,
    projections: Mapping[str, Projection],
    players: Mapping[str, Player],
    week: int,
    kickoffs: Mapping[str, datetime],
    now: datetime,
    current_starters: Sequence[str] | None = None,
    rest_weeks: Sequence[int] = (),
    playoff_weeks: Sequence[int] = (),
    max_adds: int = MAX_ADDS,
) -> LineupAdvice:
    """Solve one roster's week. Pure: everything it needs is already fetched."""
    roster = resolve_roster(rosters, user_id)
    horizon = Horizon(rest_weeks=len(rest_weeks), playoff_weeks=len(playoff_weeks))
    roster_id = to_int(roster.get("roster_id")) or 0
    slots = starting_slots(league.get("roster_positions") or ())
    owned = [str(p) for p in (roster.get("players") or ()) if p]
    starters = [str(p) for p in (current_starters if current_starters is not None
                                 else roster.get("starters") or ())]

    statuses: dict[str, WaiverStatus] = {s.player.player_id: s for s in report.statuses}
    lines: dict[str, PlayerLine] = {}
    for player_id in owned:
        player = players.get(player_id)
        if player is None:
            continue
        projection = projections.get(player_id)
        playing = bool(player.team and player.team in kickoffs)
        points, reasons = _effective(player, projection, playing=playing)
        kickoff = kickoffs.get(player.team or "")
        locked = kickoff is not None and kickoff <= now
        lines[player_id] = PlayerLine(
            player=player, projection=projection, points=points,
            locked=locked, reasons=reasons + (("locked: his game started",) if locked else ()),
            keep=horizon.keep(projection, points),
            rest=projection.rest_per_game if projection is not None else None,
            playoff=projection.playoff_per_game if projection is not None else None,
        )

    # Seat the lineup as it stands. Sleeper writes "0" for an empty starting slot.
    seats: list[Seat] = []
    for index, slot in enumerate(slots):
        player_id = starters[index] if index < len(starters) else None
        player_id = player_id if player_id and player_id != "0" else None
        seats.append(Seat(index=index, slot=slot, player_id=player_id,
                          locked=bool(player_id and (l := lines.get(player_id)) and l.locked)))
    current = Lineup(
        seats=tuple(seats),
        total=sum(lines[s.player_id].points for s in seats
                  if s.player_id is not None and s.player_id in lines),
    )
    # Re-seat each starter's line so the card knows where everyone is sitting.
    for seat in seats:
        if seat.player_id and (line := lines.get(seat.player_id)) is not None:
            lines[seat.player_id] = PlayerLine(
                player=line.player, projection=line.projection, points=line.points,
                seat=seat, locked=line.locked, reasons=line.reasons, keep=line.keep,
                rest=line.rest, playoff=line.playoff,
            )

    points = {pid: line.points for pid, line in lines.items()}
    eligibility = {pid: frozenset(line.player.fantasy_positions) for pid, line in lines.items()}
    pinned = {s.index: s.player_id for s in seats if s.locked and s.player_id}
    locked_bench = [pid for pid, line in lines.items() if line.locked and not line.starting]

    optimal = best_lineup(
        slots, points, eligibility,
        roster=set(lines), pinned=pinned, unavailable=locked_bench,
    )
    # Judge the threshold on the week as a whole. The set-based diff below reports only real
    # changes, so if the whole week is worth a rounding error there is nothing to report.
    swaps = _swaps(current, optimal, lines) if optimal.total - current.total >= MIN_SWAP_GAIN else ()

    shared = dict(
        report=report, statuses=statuses, players=players, projections=projections,
        lines=lines, slots=slots, kickoffs=kickoffs, pinned=pinned,
        locked_bench=locked_bench, horizon=horizon,
        roster_full=len(owned) >= len(league.get("roster_positions") or ()),
    )
    adds = _candidates(
        values={pid: l.points for pid, l in lines.items()},
        basis="week", max_adds=max_adds, **shared,
    )
    taken = {c.line.player_id for c in adds}
    watch = tuple(
        c for c in _candidates(
            values={pid: l.keep for pid, l in lines.items()},
            basis="season", max_adds=max_adds + len(taken), **shared,
        )
        if c.line.player_id not in taken
    )[:MAX_WATCH]

    notes: list[str] = []
    bye_teams = sorted({l.player.team for l in lines.values()
                        if l.projection is not None and l.projection.on_bye and l.player.team})
    if bye_teams:
        notes.append(f"On bye: {', '.join(bye_teams)}.")
    if pinned:
        notes.append(f"{len(pinned)} starter(s) already locked; their games have kicked off.")
    disputed = [l for l in lines.values() if l.projection is not None and l.projection.disputed]
    if disputed:
        notes.append(
            f"{len(disputed)} player(s) whose two projection sources disagree by "
            f"{SPREAD_FLAG:g} points or more."
        )
    return LineupAdvice(
        league_name=report.league_name, week=week, now=now, roster_id=roster_id,
        slots=tuple(slots), current=current, optimal=optimal, lines=lines,
        swaps=swaps, adds=adds, watch=watch,
        kickoff=min(kickoffs.values()) if kickoffs else None,
        notes=tuple(notes),
    )



def _candidates(
    *,
    report: WaiverReport,
    statuses: Mapping[str, WaiverStatus],
    players: Mapping[str, Player],
    projections: Mapping[str, Projection],
    lines: Mapping[str, PlayerLine],
    slots: Sequence[str],
    kickoffs: Mapping[str, datetime],
    values: Mapping[str, float],
    pinned: Mapping[int, str],
    locked_bench: Sequence[str],
    roster_full: bool,
    horizon: Horizon,
    basis: str,
    max_adds: int,
) -> tuple[AddCandidate, ...]:
    """Unrostered players ranked by what they would add to the optimal lineup.

    Called twice with the same machinery and a different `values` map, because "who helps me
    on Sunday" and "who should I own" are different questions with different answers:

    - **this week** values everyone at his week-`n` projection, which is what makes a
      streaming defense the right answer when yours is on a bye.
    - **rest of season** values everyone at his per-game rate over the weeks still to be
      played, which is what stops a backup quarterback with a big week-`n` number from
      looking like a pickup in a league that starts one quarterback and already has two.

    Either way the ranking is lineup impact, never raw points: a receiver behind two better
    receivers is worth nothing, however good he looks on his own, and a claim spent on him
    is a claim wasted.
    """
    eligibility = {pid: frozenset(l.player.fantasy_positions) for pid, l in lines.items()}
    points = dict(values)
    roster = frozenset(lines)

    def solve(members: frozenset[str]) -> float:
        return best_lineup(
            slots, points, eligibility, roster=members,
            pinned={i: pid for i, pid in pinned.items() if pid in members},
            unavailable=[pid for pid in locked_bench if pid in members],
        ).total

    baseline = solve(roster)
    droppable = [l for l in lines.values() if not l.locked]

    pool: list[tuple[float, str]] = []
    for status in report.statuses:
        if status.status == ROSTERED:
            continue
        player = status.player
        projection = projections.get(player.player_id)
        if projection is None or not projection.playable or not player.team:
            continue
        if not player.active or not player.can_play:
            continue
        value = projection.points if basis == "week" else horizon.later(projection)
        if value <= 0:
            continue
        # Order the pool by value, nudged up by any role change, so a back who took over a
        # backfield on Sunday is solved for even while his own projection is catching up.
        pool.append((value + max(projection.role_change or 0.0, 0.0) / 2, player.player_id))
    pool.sort(key=lambda item: -item[0])

    out: list[AddCandidate] = []
    for _, player_id in pool[:ADD_POOL]:
        player = players.get(player_id)
        projection = projections.get(player_id)
        if player is None or projection is None:
            continue
        playing = bool(player.team and player.team in kickoffs)
        week_points, reasons = _effective(player, projection, playing=playing)
        keep = horizon.keep(projection, week_points)
        value = week_points if basis == "week" else horizon.later(projection)
        if value <= 0:
            continue
        points[player_id] = value
        eligibility[player_id] = frozenset(player.fantasy_positions)
        with_add = roster | {player_id}
        if roster_full:
            drop, total = _drop_choice(droppable, solve, with_add)
        else:
            drop, total = None, solve(with_add)
        gain = total - baseline
        if gain < MIN_ADD_GAIN:
            continue
        if projection.role_changed:
            reasons = reasons + (f"{projection.role_change:+.1f} vs his preseason row",)
        out.append(AddCandidate(
            line=PlayerLine(player=player, projection=projection, points=week_points,
                            reasons=reasons, keep=keep, rest=projection.rest_per_game,
                            playoff=projection.playoff_per_game),
            gain=gain, drop=drop, status=statuses.get(player_id),
        ))
    out.sort(key=lambda c: -c.gain)

    # A bye at one position would otherwise fill the list with interchangeable streamers:
    # six defenses worth eight points each are one piece of advice, not six.
    kept: list[AddCandidate] = []
    seen: dict[str, int] = {}
    for candidate in out:
        position = candidate.line.player.position
        if seen.get(position, 0) >= MAX_ADDS_PER_POSITION:
            continue
        seen[position] = seen.get(position, 0) + 1
        kept.append(candidate)
        if len(kept) >= max_adds:
            break
    return tuple(kept)
