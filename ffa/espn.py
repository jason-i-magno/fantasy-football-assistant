"""ESPN's public NFL scoreboard, for the kickoff times Sleeper doesn't publish.

The endpoint is undocumented but needs no key. Only the fields used here are read, with
`.get()`, so a missing field drops one game rather than crashing.
"""

from datetime import datetime

import httpx

from . import __version__

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
REGULAR_SEASON = 2  # ESPN's seasontype: 1 preseason, 2 regular season, 3 postseason
# ESPN and Sleeper agree on every team abbreviation except Washington's.
ESPN_TO_SLEEPER = {"WSH": "WAS"}


class ESPNError(Exception):
    """Any failure fetching or reading the ESPN scoreboard."""


def week_kickoffs(season: int, week: int, *, http: httpx.Client | None = None) -> dict[str, datetime]:
    """Kickoff time (UTC) for each Sleeper team abbreviation in one regular-season week.

    Teams on bye are absent from the result.
    """
    client = http or httpx.Client(timeout=10.0, headers={"User-Agent": f"ffa/{__version__}"})
    try:
        response = client.get(
            SCOREBOARD_URL, params={"seasontype": REGULAR_SEASON, "week": week, "dates": season}
        )
        response.raise_for_status()
        events = response.json().get("events") or []
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        raise ESPNError(f"Could not load the week {week} NFL schedule from ESPN: {exc}") from exc
    finally:
        if http is None:
            client.close()

    kickoffs: dict[str, datetime] = {}
    for event in events:
        kickoff = _parse_time((event or {}).get("date"))
        competition = ((event or {}).get("competitions") or [{}])[0] or {}
        for competitor in competition.get("competitors") or ():
            abbr = ((competitor or {}).get("team") or {}).get("abbreviation")
            if abbr and kickoff is not None:
                kickoffs[ESPN_TO_SLEEPER.get(abbr, abbr)] = kickoff
    if not kickoffs:
        # An empty week would make every player look like a free agent. Fail instead.
        raise ESPNError(f"ESPN returned no games for week {week} of the {season} season.")
    return kickoffs


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
