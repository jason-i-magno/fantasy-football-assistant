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

**The odds for each win total** are the same simulation, read a row at a time: of the
trials where a team won exactly k of its own remaining games, how many ended with a place.
That is the question a single percentage cannot answer -- not "will I make it" but "is
three enough" -- and it is where the two numbers meet, because the row reaches a certain
100% at exactly `wins_to_clinch` and is 0% below `wins_to_survive`. Those two ends are
filled in from the arithmetic rather than counted, since a bucket the sampler visited three
hundred times cannot be trusted to the whole percent it is drawn to, and the arithmetic is
exact.

Not every league shape is modelled. A league that plays the weekly median
(`league_average_match`) awards a second result per week, and one with divisions seeds
division winners first; both would make these numbers wrong rather than approximate, so
they are refused by name in `unsupported` instead of being guessed at.
"""

import math
import random
import statistics as st
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations

from .models import to_int
from .strength import Scoring, win_probability

# Coin-flip trials behind the odds. Ten thousand puts the standard error on a 50% chance at
# half a point, which is below the whole percent the figure is shown to.
ODDS_TRIALS = 10_000
# Fixed, so the same standings give the same odds twice. A number that moves when nothing
# else has is a number nobody can check.
ODDS_SEED = 1_000_003
# A by-wins cell drawn from fewer trials than this is not worth the whole percent it would
# be shown to, so it is sampled again on its own terms. Under coin flips the thinnest cell
# of a five-game run holds 312 trials; once the teams have different strengths the same
# cell can hold 34, because a good team rarely loses out. See `_stratum`.
MIN_BUCKET_TRIALS = 400
# A hard stop on that top-up, so a strange league cannot turn one build into a minute.
TOPUP_BUDGET = 25_000

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
class WinOdds:
    """What one win total from here is worth to one team.

    `odds` is None only when nothing can answer: the arithmetic is silent and the sampler
    never drew this total. With fourteen games left the extreme totals are rarer than one
    trial in ten thousand, and a blank is the honest rendering of a bucket nobody sampled.
    """

    wins: int  # wins from here, 0..games_left
    record: float  # the record it finishes on, a tie still counting half
    odds: float | None  # 0..1, on coin flips, unless `certain` or `impossible`
    trials: int  # trials behind `odds`; 0 when the arithmetic answered instead
    certain: bool  # this total takes a place whatever else happens
    impossible: bool  # this total misses whatever else happens

    @property
    def label(self) -> str:
        """The final win column, which is what a manager counts in."""
        whole = int(self.record)
        return str(whole) if self.record == whole else f"{self.record:.1f}"


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
    by_wins: tuple[WinOdds, ...] = ()  # one row per win total still open to it

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
    scoring: Scoring | None = None  # None when every game was a coin flip

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


def scored_weeks(settings: Mapping, *, final_week: int | None = None) -> tuple[int, ...]:
    """Regular-season weeks whose results *are* in the table, earliest first.

    The complement of `undecided_weeks`, and deliberately the same two fields, so the week
    a league is in is decided in one place. A week in progress belongs to neither: it is
    scored but not recorded, and its points are half a Sunday's worth, which would make a
    team's scoring average a lie about a week nobody has finished.
    """
    start = max(1, to_int(settings.get("start_week")) or 1)
    last = to_int(settings.get("last_scored_leg"))
    if last is None:
        return ()
    end = last if final_week is None else min(last, final_week)
    return tuple(range(start, end + 1))


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
    scoring: Scoring | None = None,
) -> PlayoffPicture:
    """Solve the race. Pure: everything here is arithmetic over what was passed in.

    `scoring` turns the simulation from coin flips into draws from each team's own scoring,
    with the points accumulating into the tiebreak. Left out, every game is a coin flip and
    the tiebreak stays frozen at the points already scored. It changes nothing above: the
    clinch and survival numbers are arithmetic over the schedule and have no model in them.
    """
    record = {s.roster_id: s.record for s in table}
    points = {s.roster_id: s.points_for for s in table}
    spots = max(0, min(spots, len(table)))

    # The guaranteed numbers first, because they say which by-wins cells will be drawn from
    # the sample at all -- and a cell the arithmetic answers is not worth a trial.
    own = {s.roster_id: sum(1 for game in remaining if s.roster_id in game) for s in table}
    clinches, survivals = {}, {}
    for standing in table:
        team = standing.roster_id
        clinches[team] = _first_true(0, own[team], lambda k, t=team: not _can_miss(
            t, k, record=record, remaining=remaining, spots=spots))
        survivals[team] = _first_true(0, own[team], lambda k, t=team: _can_make(
            t, k, record=record, remaining=remaining, spots=spots))
    wanted = {
        team: {k for k in range(count + 1)
               if not _settled(k, clinches[team], survivals[team])}
        for team, count in own.items()
    }
    chances, buckets = _odds(record, points, remaining, spots, trials,
                             scoring=scoring, wanted=wanted)

    outlooks: list[TeamOutlook] = []
    for seed, standing in enumerate(table, start=1):
        team = standing.roster_id
        clinch, survive = clinches[team], survivals[team]
        outlooks.append(TeamOutlook(
            roster_id=team, standing=standing, seed=seed, games_left=own[team],
            wins_to_clinch=clinch, wins_to_survive=survive,
            odds=chances.get(team, 0.0),
            by_wins=_by_wins(standing, own[team], clinch, survive, buckets.get(team, ())),
        ))
    return PlayoffPicture(
        spots=spots, final_week=final_week, weeks_left=tuple(weeks_left),
        games_left=len(remaining), trials=trials,
        teams=tuple(outlooks), notes=tuple(notes), scoring=scoring,
    )


def _settled(wins: int, clinch: int | None, survive: int | None) -> bool:
    """Is this win total already answered by the arithmetic, sample or no sample?"""
    return (clinch is not None and wins >= clinch) or survive is None or wins < survive


def _by_wins(
    standing: Standing,
    games_left: int,
    clinch: int | None,
    survive: int | None,
    counts: Sequence[tuple[int, int]],
) -> tuple[WinOdds, ...]:
    """One row per win total: certain, impossible, or what the coin flips made of it.

    The arithmetic goes first wherever it has an answer, because it is exact and the
    sampler is not. It also keeps this row and the clinch number from contradicting each
    other on screen: the smallest total shown as a certainty is `wins_to_clinch`, by
    construction rather than by luck, and no total below `wins_to_survive` is shown with a
    chance the sampler happened to find.

    The sampled middle is where the two differ for good reason. The clinch number assumes
    every tiebreak lost and the survival number assumes every one won; the simulation
    breaks ties on points-for as they stand. So a total can be sampled at 100% and still
    not be `certain` -- which is the honest answer, and why the renderers mark certainty
    rather than reading it off the percentage.
    """
    rows: list[WinOdds] = []
    for wins in range(games_left + 1):
        certain = clinch is not None and wins >= clinch
        impossible = survive is None or wins < survive
        made, played = counts[wins] if wins < len(counts) else (0, 0)
        if certain:
            odds, played = 1.0, 0
        elif impossible:
            odds, played = 0.0, 0
        else:
            odds = made / played if played else None
        rows.append(WinOdds(
            wins=wins, record=standing.record + wins, odds=odds,
            trials=played, certain=certain, impossible=impossible,
        ))
    return tuple(rows)


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
    *,
    scoring: Scoring | None = None,
    wanted: Mapping[int, set[int]] | None = None,
) -> tuple[dict[int, float], dict[int, tuple[tuple[int, int], ...]]]:
    """How often each roster makes the playoffs over `trials` simulated seasons.

    Returns the chance for each roster, and the same trials split by how many of its own
    games it won: `(made, played)` per win total, which is the conditional the by-wins row
    is read off. Splitting them costs one pass over the teams per trial and no extra
    trials, so the two figures are the same sample and cannot disagree about a league.

    With no `scoring` every game is a coin flip -- the worst team in the league as likely
    to win on Sunday as the best -- and the tiebreak stays frozen at the points already
    scored. It is the right model for week one, when nothing has been played, and the
    wrong one by week nine, so `scoring` replaces it with two draws from each team's own
    scoring: the higher score takes the win, and both are added to the points-for that
    breaks ties, which is the half of the tiebreak the frozen version could never get
    right. One `getrandbits` call settles a whole week of coin-flip fixtures; the seed is
    fixed either way, so the same table produces the same percentages every time.

    `wanted` names the cells the arithmetic has *not* already settled. Those are the only
    ones worth sampling, and the thin ones among them are drawn again by `_stratum`.
    """
    teams = sorted(record)
    if not teams or spots <= 0:
        return dict.fromkeys(teams, 0.0), {}
    if spots >= len(teams):
        return dict.fromkeys(teams, 1.0), {}
    index = {team: offset for offset, team in enumerate(teams)}
    base = [record[t] for t in teams]
    start = [points.get(t, 0.0) for t in teams]
    pairs = [(index[a], index[b]) for a, b in remaining if a in index and b in index]
    if not pairs:
        ranked = sorted(range(len(teams)), key=lambda i: (-base[i], -start[i]))
        return ({teams[i]: 1.0 if place < spots else 0.0
                 for place, i in enumerate(ranked)}, {})

    rng = random.Random(ODDS_SEED)
    trials = max(1, trials)
    if scoring is None:
        made, hits, seen = _coin_trials(base, start, pairs, spots, trials, rng)
    else:
        lookup = scoring.means
        means = [lookup.get(t, scoring.league_mean) for t in teams]
        made, hits, seen = _score_trials(base, start, pairs, spots, trials, rng,
                                         means, scoring.sd)
        _top_up(hits, seen, base, start, pairs, spots, rng, means, scoring.sd,
                {index[t]: ks for t, ks in (wanted or {}).items() if t in index})
    return ({team: made[index[team]] / trials for team in teams},
            {team: tuple(zip(hits[index[team]], seen[index[team]])) for team in teams})


def _coin_trials(base, start, pairs, spots, trials, rng):
    """Every game a coin flip, and the tiebreak frozen at the points already scored.

    A team's own wins come out of the same draw in one operation: `want` holds the bits
    that mean this roster won, so where the draw agrees with it inside `mask`, it won, and
    the agreements are counted with `bit_count`.
    """
    count = len(base)
    width = len(pairs)
    mask = [0] * count
    want = [0] * count
    for offset, (first, second) in enumerate(pairs):
        bit = 1 << offset
        mask[first] |= bit
        mask[second] |= bit
        want[first] |= bit  # a set bit is the first roster of the pair winning
    hits = [[0] * (bits.bit_count() + 1) for bits in mask]
    seen = [[0] * (bits.bit_count() + 1) for bits in mask]
    made = [0] * count
    for _ in range(trials):
        tally = base.copy()
        draw = rng.getrandbits(width)
        for offset, (first, second) in enumerate(pairs):
            tally[first if (draw >> offset) & 1 else second] += 1.0
        order = sorted(range(count), key=lambda i: (-tally[i], -start[i]))
        wins = [(~(draw ^ want[i]) & mask[i]).bit_count() for i in range(count)]
        for i, won in enumerate(wins):
            seen[i][won] += 1
        for i in order[:spots]:
            made[i] += 1
            hits[i][wins[i]] += 1
    return made, hits, seen


def _score_trials(base, start, pairs, spots, trials, rng, means, sd):
    """Two scores a game, drawn from what each team scores, and both kept.

    The win goes to the higher score and the scores themselves go into the points-for that
    breaks ties -- so a team that scores its way to a tie on record carries the points it
    scored getting there, which is the thing the frozen tiebreak cannot do. One spread
    serves every team: eight games is far too few to tell a volatile roster from a steady
    one, and a per-team variance estimated from that would be noise given a name.
    """
    count = len(base)
    own = [0] * count
    for first, second in pairs:
        own[first] += 1
        own[second] += 1
    hits = [[0] * (games + 1) for games in own]
    seen = [[0] * (games + 1) for games in own]
    made = [0] * count
    gauss = rng.gauss
    for _ in range(trials):
        tally = base.copy()
        total = start.copy()
        for first, second in pairs:
            ours, theirs = gauss(means[first], sd), gauss(means[second], sd)
            total[first] += ours
            total[second] += theirs
            tally[first if ours > theirs else second] += 1.0
        order = sorted(range(count), key=lambda i: (-tally[i], -total[i]))
        wins = [round(tally[i] - base[i]) for i in range(count)]
        for i, won in enumerate(wins):
            seen[i][won] += 1
        for i in order[:spots]:
            made[i] += 1
            hits[i][wins[i]] += 1
    return made, hits, seen


def _top_up(hits, seen, base, start, pairs, spots, rng, means, sd, wanted):
    """Draw the thinly sampled cells again, on their own terms.

    Coin flips spread a five-game run 312/1562/3125/3125/1562/312, which is thin at the
    ends and tolerable. Once the teams have different strengths it is far worse: the best
    team in a real league loses all five games in 34 trials out of ten thousand, and a
    percentage off 34 trials has no business being drawn to the whole number. These cells
    are exactly the ones a manager looks for -- what happens if I lose out -- so they are
    sampled directly instead of being left to turn up.
    """
    budget = TOPUP_BUDGET
    thin = sorted(
        ((team, wins) for team, totals in wanted.items() for wins in totals
         if wins < len(seen[team]) and seen[team][wins] < MIN_BUCKET_TRIALS),
        key=lambda cell: seen[cell[0]][cell[1]],
    )
    for team, wins in thin:
        need = min(MIN_BUCKET_TRIALS - seen[team][wins], budget)
        if need <= 0:
            break
        drawn, made = _stratum(team, wins, need, base, start, pairs, spots, rng, means, sd)
        hits[team][wins] += made
        seen[team][wins] += drawn
        budget -= drawn


def _stratum(team, wins, trials, base, start, pairs, spots, rng, means, sd):
    """Seasons where one team wins exactly this many of its own games, and nothing else.

    Conditioning is exact rather than by rejection, which for a one-in-three-hundred total
    would mean three hundred thrown-away seasons each time. Two pieces do it. Which games
    it wins comes from the conditional Bernoulli (`_conditional` and `_draw_wins`): the
    games are independent but not equally likely, so a team that wins three of five has
    most likely won the three it was favoured in. And the scores behind a forced result
    come from `_split_scores`, because a win drawn without its scores would hand the team a
    record it did not score the points for, and the points are what breaks the tie.
    """
    own = [(offset, _opponent(pair, team)) for offset, pair in enumerate(pairs)
           if team in pair]
    if wins > len(own):
        return 0, 0
    rest = [pair for pair in pairs if team not in pair]
    chances = [win_probability(means[team], means[rival], sd) for _, rival in own]
    spread = sd * math.sqrt(2)
    gaps = [st.NormalDist(means[team] - means[rival], spread) for _, rival in own]
    cuts = [gap.cdf(0.0) for gap in gaps]
    table = _conditional(chances)
    count = len(base)
    gauss = rng.gauss
    made = 0
    for _ in range(trials):
        tally = base.copy()
        total = start.copy()
        for slot, won in enumerate(_draw_wins(chances, table, wins, rng)):
            rival = own[slot][1]
            ours, theirs = _split_scores(gaps[slot], cuts[slot], means[team] + means[rival],
                                         spread, won, rng)
            total[team] += ours
            total[rival] += theirs
            tally[team if won else rival] += 1.0
        for first, second in rest:
            ours, theirs = gauss(means[first], sd), gauss(means[second], sd)
            total[first] += ours
            total[second] += theirs
            tally[first if ours > theirs else second] += 1.0
        order = sorted(range(count), key=lambda i: (-tally[i], -total[i]))
        if team in order[:spots]:
            made += 1
    return trials, made


def _conditional(chances: Sequence[float]) -> list[list[float]]:
    """`table[j][c]`: the chance that exactly c of the games from j onwards are won."""
    count = len(chances)
    table = [[0.0] * (count + 1) for _ in range(count + 1)]
    table[count][0] = 1.0
    for game in range(count - 1, -1, -1):
        chance = chances[game]
        for won in range(count - game + 1):
            table[game][won] = (1 - chance) * table[game + 1][won]
            if won:
                table[game][won] += chance * table[game + 1][won - 1]
    return table


def _draw_wins(chances, table, wins, rng):
    """Which games were won, given that exactly `wins` of them were.

    Walks the games in order, taking each with the chance that a win there still leaves the
    rest able to supply what remains -- which is the conditional Bernoulli, and reduces to
    picking a uniform subset when every game is a coin flip.
    """
    need = wins
    for game, chance in enumerate(chances):
        total = table[game][need]
        if need <= 0:
            won = False
        elif total <= 0:
            won = True  # the rest cannot supply it, so this one has to
        else:
            won = rng.random() < chance * table[game + 1][need - 1] / total
        if won:
            need -= 1
        yield won


def _split_scores(gap, cut, total_mean, spread, won, rng):
    """Two scores that produce a result already decided, without distorting either.

    Equal spreads make the sum of two scores independent of their difference, so the
    difference is drawn from its own distribution truncated to the side that was forced,
    the sum is drawn freely, and the two are put back together. The team that was made to
    win therefore wins the way it would have won anyway -- narrowly if it was an upset --
    which is what keeps the points it banks honest.
    """
    low, high = (cut, 1.0) if won else (0.0, cut)
    draw = min(max(rng.uniform(low, high), 1e-12), 1 - 1e-12)
    difference = gap.inv_cdf(draw)
    total = rng.gauss(total_mean, spread)
    return (total + difference) / 2, (total - difference) / 2
