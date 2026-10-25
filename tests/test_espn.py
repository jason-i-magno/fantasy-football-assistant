"""ESPN scoreboard: kickoff times per Sleeper team abbreviation."""

from datetime import datetime, timezone

import httpx
import pytest

from ffa.espn import ESPNError, week_kickoffs


def game(date, away, home):
    return {"date": date, "competitions": [{"competitors": [
        {"team": {"abbreviation": home}}, {"team": {"abbreviation": away}},
    ]}]}


def scoreboard(payload, status=200, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(dict(request.url.params))
        return httpx.Response(status, json=payload)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_kickoffs_map_espn_abbreviations_to_sleepers():
    seen = []
    payload = {"events": [game("2026-10-23T00:15Z", "NE", "CHI"), game("2026-10-25T17:00Z", "WSH", "DAL")]}
    kickoffs = week_kickoffs(2026, 7, http=scoreboard(payload, seen=seen))
    thursday = datetime(2026, 10, 23, 0, 15, tzinfo=timezone.utc)
    assert kickoffs["NE"] == kickoffs["CHI"] == thursday
    assert "WAS" in kickoffs and "WSH" not in kickoffs
    assert seen == [{"seasontype": "2", "week": "7", "dates": "2026"}]


def test_malformed_games_are_skipped_not_fatal():
    payload = {"events": [{"date": "2026-10-25T17:00Z"}, game("not a date", "A", "B"),
                          game("2026-10-25T17:00Z", "NYJ", "MIA")]}
    assert set(week_kickoffs(2026, 7, http=scoreboard(payload))) == {"NYJ", "MIA"}


@pytest.mark.parametrize("payload, status", [({"events": []}, 200), ({}, 500), (["not", "a", "dict"], 200)])
def test_no_usable_schedule_raises(payload, status):
    # An empty week would make every player look like a free agent.
    with pytest.raises(ESPNError):
        week_kickoffs(2026, 7, http=scoreboard(payload, status=status))
