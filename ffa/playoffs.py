"""The playoff race: who is in, who is out, and how many wins it takes to be sure.

"How many wins do I need" is two questions wearing one set of words, and they do not have
the same answer. The number that puts a team through *whatever else happens* is arithmetic.
The number that usually proves enough is a guess. Both are reported, separately and
labelled, because a manager deciding whether to trade his season away wants the first and a
manager deciding whether to bother wants the second.

**The guaranteed numbers** come from the schedule, not from win totals. It is tempting to
say a rival can finish on `wins + games_left` and count the teams above you, but the games
left are played *against each other*: nine rivals cannot all win out, and every game hands
its win to exactly one of two teams. Counting fixtures instead of solving that constraint
claims a team can be caught by rivals who would have to beat each other to do it, which
overstates every clinch number on the board. `_reachable` and `_assignable` are the
constraint, as a max-flow from games to the teams that may win them.

Tiebreaks never enter the guaranteed numbers. A clinch assumes every team level with you
takes the tiebreak, an elimination assumes you take it, so the answer holds whatever
happens to points-for -- which is the only honest treatment, since the tiebreak is decided
by points nobody has scored yet.

**The odds** are a simulation of the remaining schedule with every game a coin flip. That
is not a forecast: it says an 0-8 team is as likely as an 8-0 team to win on Sunday, which
is false. It is reported anyway, and labelled, because it answers the question the
guaranteed number cannot -- whether two wins is probably enough -- without inventing a
model of team strength to do it. Ties on record are broken by points-for as they stand,
which future scoring will move.

Not every league shape is modelled. A league that plays the weekly median
(`league_average_match`) awards a second result per week, and one with divisions seeds
division winners first; both would make these numbers wrong rather than approximate, so
they are refused by name in `unsupported` instead of being guessed at.
"""

import math
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations

from .models import to_int

# Coin-flip trials behind the odds. Ten thousand puts the standard error on a 50% chance at
# half a point, which is below the whole percent the figure is shown to.
ODDS_TRIALS = 10_000
# Fixed, so the same standings give the same odds twice. A number that moves when nothing
# else has is a number nobody can check.
ODDS_SEED = 1_000_003

Game = tuple[int, int]  # the two roster ids, in no particular order


@dataclass(frozen=True, slots=True)
class Standing:
    """One roster's record as the league table has it."""

    roster_id: int
    wins: int
    losses: int
    ties: int
    points_for: float

    @property
    def played(self) -> int:
        return self.wins + self.losses + self.ties

    @property
    def record(self) -> float:
        """Wins, counting a tie as half of one, which is how a tie is worth anything."""
        return self.wins + 0.5 * self.ties

    @property
    def label(self) -> str:
        return f"{self.wins}-{self.losses}" + (f"-{self.ties}" if self.ties else "")


@dataclass(frozen=True, slots=True)
class TeamOutlook:
    """What one roster is still playing for."""

    roster_id: int
    standing: Standing
    seed: int  # where it sits in the table now, 1 for first
    games_left: int
    wins_to_clinch: int | None  # None when no win total guarantees a place
    wins_to_survive: int | None  # fewest wins leaving any path at all; None when out
    odds: float  # 0..1, on coin flips

    @property
    def clinched(self) -> bool:
        return self.wins_to_clinch == 0

    @property
    def eliminated(self) -> bool:
        return self.wins_to_survive is None

    @property
    def summary(self) -> str:
        """The headline: a number of wins, or the reason there isn't one."""
        if self.eliminated:
            return "out"
        if self.clinched:
            return "in"
        if self.wins_to_clinch is None:
            return "—"  # can still get in, but no win total makes it certain
        return str(self.wins_to_clinch)


@dataclass(frozen=True, slots=True)
class PlayoffPicture:
    """The race for one league, at one moment."""

    spots: int
    final_week: int  # the last week of the regular season
    weeks_left: tuple[int, ...]  # regular-season weeks whose results are not in the table
    games_left: int  # games still to be played across the whole league
    trials: int
    teams: tuple[TeamOutlook, ...] = ()
    notes: tuple[str, ...] = ()

    def team(self, roster_id: int | None) -> TeamOutlook | None:
        return next((t for t in self.teams if t.roster_id == roster_id), None)


def standings(rosters: Sequence[Mapping]) -> tuple[Standing, ...]:
    """Every roster's record, best first.

    Ordered the way Sleeper orders the table: by record, then by points scored. Points are
    stored as a whole number and a remainder in hundredths, never as one number.
    """
    rows: list[Standing] = []
    for roster in rosters:
        roster_id = to_int(roster.get("roster_id"))
        if roster_id is None:
            continue
        settings = roster.get("settings") or {}
        rows.append(Standing(
            roster_id=roster_id,
            wins=to_int(settings.get("wins")) or 0,
            losses=to_int(settings.get("losses")) or 0,
            ties=to_int(settings.get("ties")) or 0,
            points_for=(to_int(settings.get("fpts")) or 0)
            + (to_int(settings.get("fpts_decimal")) or 0) / 100.0,
        ))
    return tuple(sorted(rows, key=lambda s: (-s.record, -s.points_for)))


def regular_season_end(settings: Mapping, playoff_start: int | None = None) -> int:
    """The last week that counts towards the table.

    `playoff_week_start` is the first week of the bracket, so the regular season ends the
    week before. Weeks beyond it still come back from the matchups endpoint, fully paired
    and indistinguishable from a regular-season fixture, so a schedule read without this
    would count three playoff rounds as games anyone can still win a place with.
    """
    if playoff_start is None:
        playoff_start = to_int(settings.get("playoff_week_start"))
    return max(1, (playoff_start or 15) - 1)


def unsupported(settings: Mapping) -> str | None:
    """Why this league's race cannot be computed, or None when it can.

    Refused rather than approximated: both of these change what a win is, so the numbers
    would be wrong in a way nothing on the card could warn about.
    """
    if to_int(settings.get("league_average_match")):
        return ("this league scores every team against the weekly median as well, which is "
                "a second result a week that the race below does not model")
    if to_int(settings.get("divisions")):
        return ("this league has divisions, which seed their winners ahead of the table "
                "and change who a win is worth a place against")
    return None


def undecided_weeks(settings: Mapping, *, final_week: int) -> tuple[int, ...]:
    """Regular-season weeks whose results are not yet in the table, earliest first.

    Two traps make this more than `range(nfl_state.week, final_week + 1)`, and the league
    settings answer both without reading a single matchup:

    - **A week in progress is scored but not recorded.** On a live Sunday every week-10
      matchup carries points and nobody's record has moved. `last_scored_leg` is the last
      week actually in the table -- 9 while `leg` reads 10 -- so the open week is the one
      after it, and Sleeper's `nfl_state.week` (which has read 9 while the app still showed
      week 8) is not consulted.
    - **A league can start late.** One `.env` league has `start_week: 2`, and its week 1
      comes back from the matchups endpoint with ten fully paired entries and no points at
      all. Counted as a fixture it is a game nobody will ever play.
    """
    start = max(1, to_int(settings.get("start_week")) or 1)
    last_scored = to_int(settings.get("last_scored_leg"))
    first_open = start if last_scored is None else max(start, last_scored + 1)
    return tuple(range(first_open, final_week + 1))


def reconciles(settings: Mapping, *, played: int) -> bool:
    """Does the published boundary agree with the games the table says have been played?

    `last_scored_leg` and `start_week` are both undocumented, like everything else here, so
    the one cross-check available is run rather than trusted: the weeks between them should
    be exactly the games in the table. Both `.env` leagues reconcile, one of them only
    because `start_week` is 2. A league that does not gets a note on the card instead of a
    number quietly built on the wrong week.
    """
    start = to_int(settings.get("start_week")) or 1
    last_scored = to_int(settings.get("last_scored_leg"))
    if last_scored is None:
        return False
    return max(0, last_scored - max(1, start) + 1) == played


def games(schedule: Mapping[int, Sequence[Mapping]], weeks: Iterable[int]) -> tuple[Game, ...]:
    """Every fixture in those weeks, as pairs of roster ids.

    Sleeper pairs two rosters by a shared `matchup_id` within a week. A roster alone in its
    matchup has a bye and no game to win.
    """
    out: list[Game] = []
    for week in sorted(set(weeks)):
        pairs: dict[object, list[int]] = {}
        for entry in schedule.get(week) or ():
            key, roster_id = entry.get("matchup_id"), to_int(entry.get("roster_id"))
            if key is None or roster_id is None:
                continue
            pairs.setdefault(key, []).append(roster_id)
        for members in pairs.values():
            if len(members) == 2:
                out.append((members[0], members[1]))
    return tuple(out)


def build_picture(
    *,
    table: Sequence[Standing],
    remaining: Sequence[Game],
    spots: int,
    final_week: int,
    weeks_left: Sequence[int] = (),
    trials: int = ODDS_TRIALS,
    notes: Sequence[str] = (),
) -> PlayoffPicture:
    """Solve the race. Pure: everything here is arithmetic over what was passed in."""
    record = {s.roster_id: s.record for s in table}
    points = {s.roster_id: s.points_for for s in table}
    spots = max(0, min(spots, len(table)))
    chances = _odds(record, points, remaining, spots, trials)

    outlooks: list[TeamOutlook] = []
    for seed, standing in enumerate(table, start=1):
        team = standing.roster_id
        own = sum(1 for game in remaining if team in game)
        clinch = _first_true(0, own, lambda k, t=team: not _can_miss(
            t, k, record=record, remaining=remaining, spots=spots))
        survive = _first_true(0, own, lambda k, t=team: _can_make(
            t, k, record=record, remaining=remaining, spots=spots))
        outlooks.append(TeamOutlook(
            roster_id=team, standing=standing, seed=seed, games_left=own,
            wins_to_clinch=clinch, wins_to_survive=survive,
            odds=chances.get(team, 0.0),
        ))
    return PlayoffPicture(
        spots=spots, final_week=final_week, weeks_left=tuple(weeks_left),
        games_left=len(remaining), trials=trials,
        teams=tuple(outlooks), notes=tuple(notes),
    )


def _first_true(low: int, high: int, predicate) -> int | None:
    """The smallest k in [low, high] where `predicate` holds, or None if none does.

    Both predicates here are monotone in k: an extra win raises your own record and denies
    an opponent one, so it can never turn a place you had into a place you lost. That is
    what lets this bisect instead of walking every total, which matters because the costly
    calls are the ones that have to exhaust every outcome to answer "no".
    """
    answer: int | None = None
    while low <= high:
        mid = (low + high) // 2
        if predicate(mid):
            answer, high = mid, mid - 1
        else:
            low = mid + 1
    return answer


def _split(team: int, remaining: Sequence[Game]) -> tuple[list[Game], list[Game]]:
    return ([g for g in remaining if team in g], [g for g in remaining if team not in g])


def _opponent(game: Game, team: int) -> int:
    return game[0] if game[1] == team else game[1]


def _can_miss(team: int, wins: int, *, record, remaining, spots) -> bool:
    """Could `team` win `wins` of its remaining games and still miss the playoffs?

    Everything falls the wrong way: the games it does not win go to its opponents, it is
    free to have won the games that help its rivals least, and every team level with it on
    record takes the tiebreak. The first k where this is False is the clinch number.
    """
    own, rest = _split(team, remaining)
    if wins > len(own):
        return False
    target = record[team] + wins
    for won in combinations(range(len(own)), wins):
        base = dict(record)
        for index, game in enumerate(own):
            if index not in won:
                base[_opponent(game, team)] += 1.0
        rivals = [t for t in base if t != team]
        # Level is enough to pass it, the tiebreak being assumed lost.
        ahead = [t for t in rivals if base[t] >= target]
        if len(ahead) >= spots:
            return True
        need = {t: math.ceil(target - base[t]) for t in rivals if base[t] < target}
        able = {t: n for t, n in need.items() if n <= sum(1 for g in rest if t in g)}
        wanted = spots - len(ahead)
        if len(able) < wanted:
            continue
        # Strongest chasers first, so a feasible group is usually the first one tried.
        order = sorted(able, key=lambda t: -base[t])
        if any(_reachable({t: able[t] for t in group}, rest)
               for group in combinations(order, wanted)):
            return True
    return False


def _can_make(team: int, wins: int, *, record, remaining, spots) -> bool:
    """Is there any outcome where `team` wins `wins` of its games and makes the playoffs?

    The mirror of `_can_miss`: everything falls its way and it takes every tiebreak, so a
    rival only passes it by finishing strictly above. The first k where this is True is the
    fewest wins that leave a path; no such k means eliminated.
    """
    own, rest = _split(team, remaining)
    if wins > len(own):
        return False
    target = record[team] + wins
    for won in combinations(range(len(own)), wins):
        base = dict(record)
        for index, game in enumerate(own):
            if index not in won:
                base[_opponent(game, team)] += 1.0
        rivals = [t for t in base if t != team]
        past = [t for t in rivals if base[t] > target]
        if len(past) >= spots:
            continue  # already too many above it, whatever happens next
        # Everyone else has to be held level or below, except this many who may pass it
        # without costing the last place.
        allowance = spots - 1 - len(past)
        movable = [t for t in rivals if base[t] <= target
                   and base[t] + sum(1 for g in rest if t in g) > target]
        free_count = min(allowance, len(movable))
        for free in combinations(sorted(movable, key=lambda t: -base[t]), free_count):
            caps = {}
            for t in rivals:
                caps[t] = (len(rest) if t in free or base[t] > target
                           else int(target - base[t]))
            if _assignable(rest, caps):
                return True
    return False


def _reachable(need: Mapping[int, int], pool: Sequence[Game]) -> bool:
    """Can every team in `need` win at least that many games out of `pool`?

    A count of each team's own fixtures is not the answer: two teams that need a win from
    the same game cannot both have it. Flow from each game to the teams allowed to win it
    is that constraint, and saturating the demand is the group being achievable.
    """
    wanted = sum(max(0, n) for n in need.values())
    if wanted <= 0:
        return True
    relevant = [g for g in pool if g[0] in need or g[1] in need]
    if len(relevant) < wanted:
        return False
    slot = {team: 2 + index for index, team in enumerate(need)}
    net = _Flow(2 + len(need) + len(relevant))
    for offset, game in enumerate(relevant):
        node = 2 + len(need) + offset
        net.add(0, node, 1)
        for member in game:
            if member in need:
                net.add(node, slot[member], 1)
    for team, count in need.items():
        if count > 0:
            net.add(slot[team], 1, count)
    return net.flow(0, 1) == wanted


def _assignable(pool: Sequence[Game], caps: Mapping[int, int]) -> bool:
    """Can every game in `pool` be won by someone without exceeding anyone's cap?

    The other direction of the same constraint: holding rivals down is limited by the fact
    that somebody has to win each game, so two capped teams playing each other force one of
    them upwards. Every game must be assigned, hence the demand is the whole pool.
    """
    if not pool:
        return True
    teams = sorted({t for game in pool for t in game})
    if any(caps.get(t, 0) < 0 for t in teams):
        return False
    slot = {team: 2 + index for index, team in enumerate(teams)}
    net = _Flow(2 + len(teams) + len(pool))
    for offset, game in enumerate(pool):
        node = 2 + len(teams) + offset
        net.add(0, node, 1)
        for member in game:
            net.add(node, slot[member], 1)
    for team in teams:
        net.add(slot[team], 1, caps.get(team, 0))
    return net.flow(0, 1) == len(pool)


class _Flow:
    """Dinic's algorithm, on graphs small enough that nothing here needs to be clever.

    Every path is source -> game -> team -> sink, so the recursion is four deep whatever
    the league looks like.
    """

    __slots__ = ("to", "cap", "head")

    def __init__(self, nodes: int) -> None:
        self.to: list[int] = []
        self.cap: list[int] = []
        self.head: list[list[int]] = [[] for _ in range(nodes)]

    def add(self, source: int, target: int, cap: int) -> None:
        self.head[source].append(len(self.to))
        self.to.append(target)
        self.cap.append(cap)
        self.head[target].append(len(self.to))
        self.to.append(source)
        self.cap.append(0)

    def flow(self, source: int, sink: int) -> int:
        total = 0
        while True:
            level = [-1] * len(self.head)
            level[source] = 0
            queue = [source]
            for node in queue:
                for edge in self.head[node]:
                    if self.cap[edge] > 0 and level[self.to[edge]] < 0:
                        level[self.to[edge]] = level[node] + 1
                        queue.append(self.to[edge])
            if level[sink] < 0:
                return total
            progress = [0] * len(self.head)
            while (pushed := self._push(source, sink, level, progress, 1 << 30)):
                total += pushed

    def _push(self, node: int, sink: int, level, progress, limit: int) -> int:
        if node == sink:
            return limit
        while progress[node] < len(self.head[node]):
            edge = self.head[node][progress[node]]
            nxt = self.to[edge]
            if self.cap[edge] > 0 and level[nxt] == level[node] + 1:
                pushed = self._push(nxt, sink, level, progress, min(limit, self.cap[edge]))
                if pushed:
                    self.cap[edge] -= pushed
                    self.cap[edge ^ 1] += pushed
                    return pushed
            progress[node] += 1
        return 0


def _odds(
    record: Mapping[int, float],
    points: Mapping[int, float],
    remaining: Sequence[Game],
    spots: int,
    trials: int,
) -> dict[int, float]:
    """How often each roster makes the playoffs with every remaining game a coin flip.

    Not a forecast, and labelled as such wherever it is shown: a coin flip says the worst
    team in the league is as likely to win on Sunday as the best. What it does give is the
    shape the guaranteed numbers cannot -- that two wins is nearly always enough from 5-3,
    and nearly never from 2-6 -- without a model of team strength that would be a guess
    dressed as a projection.

    One `getrandbits` call settles a whole week of fixtures, and the seed is fixed, so the
    same table produces the same percentages every time it is drawn.
    """
    teams = sorted(record)
    if not teams or spots <= 0:
        return dict.fromkeys(teams, 0.0)
    if spots >= len(teams):
        return dict.fromkeys(teams, 1.0)
    index = {team: offset for offset, team in enumerate(teams)}
    base = [record[t] for t in teams]
    tiebreak = [points.get(t, 0.0) for t in teams]
    pairs = [(index[a], index[b]) for a, b in remaining if a in index and b in index]
    if not pairs:
        ranked = sorted(range(len(teams)), key=lambda i: (-base[i], -tiebreak[i]))
        return {teams[i]: 1.0 if place < spots else 0.0
                for place, i in enumerate(ranked)}

    rng = random.Random(ODDS_SEED)
    width = len(pairs)
    made = [0] * len(teams)
    trials = max(1, trials)
    for _ in range(trials):
        tally = base.copy()
        draw = rng.getrandbits(width)
        for offset, (first, second) in enumerate(pairs):
            tally[first if (draw >> offset) & 1 else second] += 1.0
        order = sorted(range(len(teams)), key=lambda i: (-tally[i], -tiebreak[i]))
        for i in order[:spots]:
            made[i] += 1
    return {team: made[index[team]] / trials for team in teams}
