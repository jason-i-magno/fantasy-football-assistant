"""Run independent fetches at the same time, and behave exactly like the loop they replace.

A cold lineup build was five seconds, and three and a half of them were requests waiting
their turn for no reason: five pages of ESPN's player feed, a scoreboard for each of five
weeks, a projection for each week still to be played, a matchup for each week already
played. Nothing in that list needs anything else in it.

The point of putting it here rather than writing `ThreadPoolExecutor` at each call site is
that the error and ordering behaviour has to be the loop's, or a failure stops looking like
a failure:

- the first exception propagates, and the rest are discarded, which is what a `for` loop
  does when its second iteration raises;
- the result is keyed, so callers never depend on completion order;
- the pool is small on purpose. These are somebody else's servers, and a build that opens
  twelve sockets at once to shave a tenth of a second is rude arithmetic.

Threads rather than asyncio because every client here is `httpx`'s synchronous one, shared
and thread-safe, and because one blocking call in an async rewrite would quietly serialise
the whole thing again.
"""

import logging
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

log = logging.getLogger(__name__)

# Enough to overlap the slow feeds without arriving as a burst. Measured: the ESPN player
# feed's five pages take 0.81s one after another and 0.30s four at a time, and a sixth
# worker bought nothing.
MAX_WORKERS = 4

K = TypeVar("K")
V = TypeVar("V")


def gather(jobs: Mapping[K, Callable[[], V]], *, workers: int = MAX_WORKERS) -> dict[K, V]:
    """Run each job at once and return `{key: result}`.

    Raises the first exception any job raised, after the others have finished, so a caller
    cannot be left with threads still writing into something it has given up on.
    """
    if not jobs:
        return {}
    if len(jobs) == 1:
        (key, job), = jobs.items()
        return {key: job()}
    with ThreadPoolExecutor(max_workers=min(workers, len(jobs)),
                            thread_name_prefix="ffa-fetch") as pool:
        futures = {key: pool.submit(job) for key, job in jobs.items()}
        # Collected in the caller's key order, so the first failure reported is the first
        # one a loop would have hit rather than whichever thread happened to finish first.
        results: dict[K, V] = {}
        failure: BaseException | None = None
        for key, future in futures.items():
            try:
                results[key] = future.result()
            except BaseException as exc:  # noqa: BLE001 - re-raised below, in key order
                if failure is None:
                    failure = exc
                else:
                    log.debug("Further failure fetching %s, discarded: %s", key, exc)
    if failure is not None:
        raise failure
    return results
