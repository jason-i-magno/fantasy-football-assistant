"""Fetching several things at once has to behave exactly like fetching them in turn.

A cold lineup build was five seconds, three and a half of them requests waiting their turn
for nothing. Replacing the loops with a pool is only safe if the pool keeps the loop's
promises: the same keys, the first failure, and no dependence on who finishes first. These
tests are about those promises, not about speed -- except one, which proves the jobs really
do overlap, because a `gather` that quietly ran things one at a time would pass every other
test here.
"""

import threading
import time

import pytest

from ffa.fanout import gather


def test_results_are_keyed_by_what_was_asked_for():
    out = gather({"a": lambda: 1, "b": lambda: 2, "c": lambda: 3})
    assert out == {"a": 1, "b": 2, "c": 3}


def test_nothing_asked_for_is_nothing_fetched():
    ran = []
    assert gather({}) == {}
    assert gather({"only": lambda: ran.append(1) or "x"}) == {"only": "x"}
    assert ran == [1]


def test_one_job_runs_where_it_was_called():
    """No pool for a single job: a thread to wait on one request is a thread for nothing,
    and it keeps a traceback in the caller's own stack where it is readable."""
    where = {}

    def job():
        where["thread"] = threading.current_thread()
        return "done"

    assert gather({"one": job}) == {"one": "done"}
    assert where["thread"] is threading.current_thread()


def test_the_jobs_actually_overlap():
    """The point of the exercise, proved without timing anything.

    Three jobs meet at a barrier. If `gather` ran them one at a time the first would wait
    for two partners that cannot arrive until it returns, and the barrier would break
    instead of releasing. No sleeps, no tolerances: it either overlaps or it fails.
    """
    barrier = threading.Barrier(3, timeout=5)
    out = gather({n: (lambda n=n: barrier.wait() and n or n) for n in range(3)})
    assert sorted(out) == [0, 1, 2]


def test_the_first_failure_is_the_one_that_is_raised():
    """A loop stops at its first failing iteration and reports that one. Whichever thread
    happens to finish first must not change which error the user is shown."""
    def boom(which):
        def job():
            raise ValueError(which)
        return job

    with pytest.raises(ValueError, match="first"):
        gather({"a": boom("first"), "b": boom("second"), "c": lambda: "fine"})


def test_a_failure_waits_for_the_others_before_it_is_raised():
    """Nothing may still be running once `gather` returns: the caller is entitled to
    assume its own objects have stopped being written to."""
    stopped = threading.Event()

    def slow():
        time.sleep(0.05)
        stopped.set()
        return "late"

    def fail():
        raise RuntimeError("no")

    with pytest.raises(RuntimeError):
        gather({"fail": fail, "slow": slow})
    assert stopped.is_set()


def test_the_pool_is_no_bigger_than_the_work():
    """Four workers is the ceiling, not the order: two jobs must not open four threads."""
    seen = set()

    def job():
        seen.add(threading.current_thread().name)
        time.sleep(0.02)
        return 1

    gather({"a": job, "b": job}, workers=4)
    assert len(seen) <= 2


# -- the promise that had to move inside a job -----------------------------------------


def test_losing_espn_is_still_not_losing_the_build():
    """A second opinion is an improvement, not a dependency.

    This used to be a `try` around the call. A fan-out raises the first exception any job
    raised, so the soft failure had to move inside the job or ESPN being down would take
    the whole build with it -- which is the sort of thing that only shows up on the day
    ESPN is down.
    """
    from ffa.espn_fantasy import ESPNFantasyError
    from ffa.lineup.source import _espn_or_reason

    def down(season, weeks):
        raise ESPNFantasyError("ESPN said no")

    weeks, reason = _espn_or_reason(down, 2026, (9, 10))
    assert weeks == {} and "ESPN said no" in str(reason)

    def fine(season, weeks):
        return {week: [f"row{week}"] for week in weeks}

    assert _espn_or_reason(fine, 2026, (9,)) == ({9: ["row9"]}, None)


def test_a_failure_that_is_not_espn_s_still_stops_the_build():
    """Only ESPN's own failure is soft. A bug in the parser is not a second opinion."""
    from ffa.lineup.source import _espn_or_reason

    def broken(season, weeks):
        raise KeyError("players")

    with pytest.raises(KeyError):
        _espn_or_reason(broken, 2026, (9,))
