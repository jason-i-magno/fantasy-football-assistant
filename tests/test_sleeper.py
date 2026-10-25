"""Sleeper client: cache policies, retries, and error messages."""

import json
from datetime import date

import httpx
import pytest

from draftbot.sleeper import (
    BASE_URL,
    SleeperClient,
    SleeperError,
    SleeperNotFound,
    SleeperRateLimited,
    SleeperUnavailable,
    filter_players,
)


class Recorder:
    """Scripted transport: records requests and returns queued responses per path."""

    def __init__(self, responses):
        self.responses = responses  # path -> list of (status, json) or single
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        queued = self.responses[request.url.path]
        entry = queued.pop(0) if isinstance(queued, list) else queued
        status, payload = entry if isinstance(entry, tuple) else (200, entry)
        if payload is None:
            # Sleeper answers unknown users with HTTP 200 and a literal `null` body.
            return httpx.Response(status, content=b"null")
        return httpx.Response(status, json=payload)

    def count(self, path):
        return sum(1 for call in self.calls if call == path)


def make_client(tmp_path, responses, **kwargs):
    recorder = Recorder(responses)
    http = httpx.Client(transport=httpx.MockTransport(recorder), base_url=BASE_URL)
    client = SleeperClient(tmp_path, http=http, sleep=lambda _: None, **kwargs)
    return client, recorder


def test_user_lookup_and_once_per_run_cache(tmp_path):
    client, recorder = make_client(tmp_path, {"/v1/user/jason": {"user_id": "42", "display_name": "J"}})
    assert client.user("jason")["user_id"] == "42"
    client.user("jason")
    assert recorder.count("/v1/user/jason") == 1


def test_unknown_user_returns_null_body_not_404(tmp_path):
    client, _ = make_client(tmp_path, {"/v1/user/nope": (200, None)})
    with pytest.raises(SleeperNotFound, match="nope"):
        client.user("nope")


def test_404_names_the_id(tmp_path):
    client, _ = make_client(tmp_path, {"/v1/draft/bad": (404, None)})
    with pytest.raises(SleeperNotFound, match="draft bad"):
        client.draft("bad")


def test_5xx_retries_then_succeeds(tmp_path):
    client, recorder = make_client(
        tmp_path, {"/v1/draft/d1": [(503, None), (500, None), (200, {"draft_id": "d1"})]}
    )
    assert client.draft("d1")["draft_id"] == "d1"
    assert recorder.count("/v1/draft/d1") == 3


def test_5xx_gives_up_after_max_retries(tmp_path):
    client, recorder = make_client(tmp_path, {"/v1/draft/d1": [(500, None)] * 5}, max_retries=2)
    with pytest.raises(SleeperUnavailable, match="HTTP 500"):
        client.draft("d1")
    assert recorder.count("/v1/draft/d1") == 3


def test_rate_limit_raises_immediately_so_the_caller_can_back_off(tmp_path):
    client, recorder = make_client(tmp_path, {"/v1/draft/d1": [(429, None)] * 3})
    with pytest.raises(SleeperRateLimited):
        client.draft("d1")
    assert recorder.count("/v1/draft/d1") == 1  # no retry storm against a rate limit


def test_transport_errors_retry(tmp_path):
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json=[{"pick_no": 1}])

    http = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE_URL)
    client = SleeperClient(tmp_path, http=http, sleep=lambda _: None)
    assert client.picks("d1") == [{"pick_no": 1}]
    assert attempts["n"] == 3


def test_draft_cache_refreshes_on_pick_count_change_and_ttl(tmp_path):
    now = {"t": 1000.0}
    client, recorder = make_client(
        tmp_path, {"/v1/draft/d1": [(200, {"draft_id": "d1", "last_picked": n}) for n in range(5)]},
        clock=lambda: now["t"],
    )
    client.draft("d1", pick_count=3)
    client.draft("d1", pick_count=3)
    assert recorder.count("/v1/draft/d1") == 1  # inside the TTL, same pick count
    client.draft("d1", pick_count=4)
    assert recorder.count("/v1/draft/d1") == 2  # a pick landed: refetch for a fresh clock
    now["t"] += 31
    client.draft("d1", pick_count=4)
    assert recorder.count("/v1/draft/d1") == 3  # TTL expired


def test_unusable_draft_payload_is_not_cached(tmp_path):
    """A dict-shaped error body must not be served from cache for the next 30 seconds."""
    client, recorder = make_client(
        tmp_path, {"/v1/draft/d1": [(200, {"error": "nope"}), (200, {"draft_id": "d1"})]}
    )
    with pytest.raises(SleeperError):
        client.draft("d1", pick_count=1)
    assert client.draft("d1", pick_count=1)["draft_id"] == "d1"
    assert recorder.count("/v1/draft/d1") == 2


def test_picks_are_never_cached(tmp_path):
    client, recorder = make_client(tmp_path, {"/v1/draft/d1/picks": [(200, [])] * 3})
    client.picks("d1")
    client.picks("d1")
    assert recorder.count("/v1/draft/d1/picks") == 2


def test_traded_picks_refetch_only_when_the_pick_count_changes(tmp_path):
    client, recorder = make_client(tmp_path, {"/v1/draft/d1/traded_picks": [(200, [])] * 3})
    client.traded_picks("d1", pick_count=5)
    client.traded_picks("d1", pick_count=5)
    assert recorder.count("/v1/draft/d1/traded_picks") == 1
    client.traded_picks("d1", pick_count=6)
    assert recorder.count("/v1/draft/d1/traded_picks") == 2


# -- player map ----------------------------------------------------------------------

RAW_PLAYERS = {
    "1": {"player_id": "1", "position": "RB", "fantasy_positions": ["RB"], "active": True,
          "search_rank": 5, "team": "SF", "full_name": "Back One", "espn_id": 77},
    "2": {"player_id": "2", "position": "OL", "fantasy_positions": ["OL"], "active": True},
    "DET": {"player_id": "DET", "position": "DEF", "fantasy_positions": ["DEF"], "team": "DET",
            "active": True},
}


def test_filter_players_drops_non_fantasy_and_trims_fields():
    filtered = filter_players(RAW_PLAYERS)
    assert set(filtered) == {"1", "DET"}
    assert "espn_id" not in filtered["1"]
    assert filtered["1"]["search_rank"] == 5


def test_players_cached_to_disk_for_the_day(tmp_path):
    client, recorder = make_client(
        tmp_path, {"/v1/players/nfl": [(200, RAW_PLAYERS)] * 3}, today=lambda: date(2026, 9, 15)
    )
    players, stamp = client.players()
    assert stamp == "2026-09-15" and set(players) == {"1", "DET"}
    cached = json.loads((tmp_path / "players_nfl.json").read_text())
    assert cached["date"] == "2026-09-15"

    # A fresh client on the same day reads the disk cache instead of the network.
    client2, recorder2 = make_client(
        tmp_path, {"/v1/players/nfl": [(200, RAW_PLAYERS)]}, today=lambda: date(2026, 9, 15)
    )
    assert client2.players()[0].keys() == players.keys()
    assert recorder2.count("/v1/players/nfl") == 0


def test_players_cache_goes_stale_the_next_day(tmp_path):
    client, _ = make_client(tmp_path, {"/v1/players/nfl": [(200, RAW_PLAYERS)]},
                            today=lambda: date(2026, 9, 15))
    client.players()
    client2, recorder2 = make_client(tmp_path, {"/v1/players/nfl": [(200, RAW_PLAYERS)]},
                                     today=lambda: date(2026, 9, 16))
    assert client2.players()[1] == "2026-09-16"
    assert recorder2.count("/v1/players/nfl") == 1


def test_stale_player_cache_is_used_when_the_refresh_fails(tmp_path):
    client, _ = make_client(tmp_path, {"/v1/players/nfl": [(200, RAW_PLAYERS)]},
                            today=lambda: date(2026, 9, 15))
    client.players()
    client2, _ = make_client(tmp_path, {"/v1/players/nfl": [(500, None)] * 5},
                             today=lambda: date(2026, 9, 16), max_retries=1)
    players, stamp = client2.players()
    assert stamp == "2026-09-15" and set(players) == {"1", "DET"}


def test_player_fetch_failure_without_a_cache_is_fatal(tmp_path):
    client, _ = make_client(tmp_path, {"/v1/players/nfl": [(500, None)] * 5}, max_retries=1)
    with pytest.raises(SleeperError):
        client.players()
