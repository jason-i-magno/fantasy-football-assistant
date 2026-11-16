"""How much each team scores a week, and how much of that is real.

The playoff odds used to flip a coin for every remaining game, which says the worst team in
the league is as likely to win on Sunday as the best. Replacing that with "the better team
usually wins" needs a number for how much better, and the trap is that a scoring average
over eight or nine weeks is mostly noise. Measured on two real leagues the same week:

    league   spread of team averages   week-to-week noise   signal
    A                           14.4                 21.5      72%
    B                            8.4                 26.3       0%

In B the teams' averages are *further apart than nine weeks of randomness alone would
produce* -- which is to say, not further apart at all: the spread is what you would see if
every team were identical. Out of sample it showed: the higher-scoring team won 15 of 30.
In A the same test went 21 of 25. One estimator cannot be told which league it is looking
at, so it measures instead, and shrinks each team's average toward the league's by the
fraction that survives the test. League B's teams all collapse onto the league mean and the
simulation goes back to flipping coins, which is the honest answer there and nobody has to
decide it by hand.

That is the whole design. `reliability` is the fraction, from a variance decomposition: the
spread of team averages contains both the real spread and `within / weeks` of noise, so
subtracting the second leaves an estimate of the first, floored at zero because a negative
variance means there was nothing there.

**Two channels, because each is blind to what the other sees.** Scored weeks know how the
manager actually sets his lineup and what his players really did; they cannot see that his
running back tore an ACL on Sunday. The projections can see exactly that and nothing else:
they are a roster's strength on paper, today. They are blended on the points scale, each
centred on its own league average, so the level comes from the scores alone. That is
deliberate -- a projected *best* lineup is worth more than anyone actually scores, because
every manager leaves something on his bench, and centring cancels a bias that no data here
could measure.

`PROJECTION_WEIGHT` is the one number here that no available data can settle: it would take
a season of stored projections to know which channel predicts better, and this tool stores
none. Half each is the honest statement of not knowing, and it is a named constant so that
changing it is a decision rather than an edit.
"""

import math
import statistics as st
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

# Below this many scored weeks a variance decomposition is measuring itself. Four is the
# earliest the estimate stops being dominated by its own error; it is also about when a
# fantasy season has produced anything worth reading.
MIN_WEEKS = 4
# Points a week, one standard deviation, for a league with no scoring history yet. Measured
# at 21.5 and 26.3 in two real leagues; the midpoint is used rather than either.
DEFAULT_WEEK_SD = 24.0
# How much of a team's strength comes from its roster on paper rather than from what it has
# scored. See the module docstring: this is a judgement, not a measurement.
PROJECTION_WEIGHT = 0.5


@dataclass(frozen=True, slots=True)
class Strength:
    """What one roster is expected to score in a week still to be played."""

    roster_id: int
    mean: float  # the number the simulation draws around
    sd: float  # week-to-week spread, league-wide rather than per team (see `estimate`)
    scored: float | None  # its raw weekly average, before any shrinking
    projected: float | None  # the best lineup it could field, per game
    weeks: int  # scored weeks behind `scored`


@dataclass(frozen=True, slots=True)
class Scoring:
    """Every roster's scoring, and how much the league's spread can be trusted."""

    teams: tuple[Strength, ...]
    reliability: float  # 0..1: the share of the spread between teams that is signal
    league_mean: float
    weeks: int  # scored weeks behind the estimate
    sd: float
    projection_weight: float  # 0 when nothing was projected, 1 when there is no history

    def team(self, roster_id: int | None) -> Strength | None:
        return next((t for t in self.teams if t.roster_id == roster_id), None)

    @property
    def means(self) -> dict[int, float]:
        return {t.roster_id: t.mean for t in self.teams}

    @property
    def flat(self) -> bool:
        """Is every team the same? Then the games are coin flips, and should be called so.

        True for a league whose scoring has not separated its teams at all, which is a
        real state and not a failure: it is what league B above looks like.
        """
        means = [t.mean for t in self.teams]
        return not means or max(means) - min(means) < 0.05


def estimate(
    weekly: Mapping[int, Mapping[int, float]],
    *,
    projected: Mapping[int, float] | None = None,
) -> Scoring | None:
    """Per-roster scoring from the weeks already played, the roster on paper, or both.

    `weekly` is week -> roster -> points, carrying the week so that a phantom one can be
    dropped and so `backtest` can walk it forward. Returns None when neither channel has
    anything, which leaves the caller flipping coins -- the right behaviour in week one.
    """
    scores = _by_roster(weekly)
    # A roster projecting nothing is one nobody has projections for -- week one, or a test
    # fixture -- and not a team expected to score zero. Treated as data it would put the
    # simulated points-for on a scale of nought, which is noise where the tiebreak lives.
    projected = {r: p for r, p in (projected or {}).items() if p}
    if not scores and not projected:
        return None

    weeks = max((len(v) for v in scores.values()), default=0)
    played = {r: v for r, v in scores.items() if len(v) >= 2}
    within = st.mean([st.variance(v) for v in played.values()]) if len(played) >= 2 else None
    averages = {r: st.mean(v) for r, v in scores.items() if v}

    reliability, true_var = 0.0, 0.0
    if averages and within is not None and weeks >= MIN_WEEKS and len(averages) >= 3:
        between = st.pvariance(list(averages.values()))
        # The spread you can see is the real spread plus the noise in each average. What is
        # left after taking the noise out is the only part worth believing.
        true_var = max(0.0, between - within / weeks)
        reliability = true_var / (true_var + within / weeks) if true_var else 0.0

    level = st.mean(averages.values()) if averages else st.mean(projected.values())
    # A team's own average is pulled toward the league's by however little of it is signal.
    scored_edge = {r: reliability * (m - level) for r, m in averages.items()}
    proj_level = st.mean(projected.values()) if projected else 0.0
    proj_edge = {r: p - proj_level for r, p in projected.items()}

    if not proj_edge:
        weight = 0.0
    elif not averages:
        weight = 1.0  # nothing has been scored yet: the roster on paper is all there is
    else:
        weight = PROJECTION_WEIGHT

    sd = math.sqrt(within + (1 - reliability) * true_var) if within else DEFAULT_WEEK_SD
    rosters = sorted(set(averages) | set(projected))
    teams = tuple(
        Strength(
            roster_id=roster,
            mean=level + (1 - weight) * scored_edge.get(roster, 0.0)
            + weight * proj_edge.get(roster, 0.0),
            sd=sd,
            scored=averages.get(roster),
            projected=projected.get(roster),
            weeks=len(scores.get(roster, ())),
        )
        for roster in rosters
    )
    return Scoring(teams=teams, reliability=reliability, league_mean=level, weeks=weeks,
                   sd=sd, projection_weight=weight)


def _by_roster(weekly: Mapping[int, Mapping[int, float]]) -> dict[int, list[float]]:
    """Flatten week -> roster -> points, dropping the weeks that are not results.

    A week where every team scored nothing is not a week anybody played: one `.env` league
    starts in week 2 and its week 1 comes back fully paired with zeros throughout. Counted
    as a result it drags every average down by an eighth and invents variance that no team
    produced. The same guard covers a week Sleeper has built but not yet scored.
    """
    out: dict[int, list[float]] = {}
    for week in sorted(weekly):
        row = weekly[week] or {}
        if not row or not any(points for points in row.values()):
            continue
        for roster, points in row.items():
            out.setdefault(roster, []).append(float(points))
    return out


def win_probability(first: float, second: float, sd: float) -> float:
    """How often a team averaging `first` beats one averaging `second`.

    Two independent draws with the same spread, so the difference has a standard deviation
    of `sd * sqrt(2)` -- which is why a ten-point edge is worth about 63% and not the 90%
    that comparing two averages side by side suggests. Fantasy weeks are mostly noise.
    """
    if sd <= 0:
        return 1.0 if first > second else (0.0 if first < second else 0.5)
    return 0.5 * (1 + math.erf((first - second) / (sd * math.sqrt(2) * math.sqrt(2))))


@dataclass(frozen=True, slots=True)
class Backtest:
    """What the estimator would have called, week by week, knowing only what came before."""

    games: int
    brier: float  # mean squared error of the probability; lower is better
    baseline: float  # the same score for calling every game 50/50, which is 0.25
    accuracy: float  # share of games where the favourite won

    @property
    def skill(self) -> float:
        """Share of the coin flip's error removed. Zero or less means no skill at all."""
        return 0.0 if not self.baseline else (self.baseline - self.brier) / self.baseline


def backtest(
    weekly: Mapping[int, Mapping[int, float]],
    fixtures: Mapping[int, Sequence[tuple[int, int]]],
    *,
    warmup: int = MIN_WEEKS,
) -> Backtest | None:
    """Replay the season: estimate from the weeks before, then score the week's results.

    This is the acceptance test for the whole idea, and it is in the library rather than in
    a notebook because it is the thing that should be re-run before trusting any change to
    the estimator. A Brier score at or above the 0.25 of a coin flip means the model knows
    nothing and the coin should be left alone.
    """
    played = sorted(w for w in weekly if any((weekly[w] or {}).values()))
    rows: list[tuple[float, bool]] = []
    for index, week in enumerate(played):
        if index < warmup:
            continue
        history = {w: weekly[w] for w in played[:index]}
        scoring = estimate(history)
        if scoring is None:
            continue
        means, sd = scoring.means, scoring.sd
        points = weekly[week] or {}
        for first, second in fixtures.get(week) or ():
            if first not in points or second not in points or points[first] == points[second]:
                continue
            chance = win_probability(means.get(first, scoring.league_mean),
                                     means.get(second, scoring.league_mean), sd)
            rows.append((chance, points[first] > points[second]))
    if not rows:
        return None
    brier = st.mean([(chance - float(won)) ** 2 for chance, won in rows])
    # Only games the model had an opinion about. A flat league calls everything 50/50 and
    # has no favourites at all, which is nought for nought and not nought out of thirty.
    decided = [(chance, won) for chance, won in rows if chance != 0.5]
    right = sum(1 for chance, won in decided if (chance > 0.5) == won)
    return Backtest(games=len(rows), brier=brier, baseline=0.25,
                    accuracy=right / len(decided) if decided else 0.5)
