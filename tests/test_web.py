"""`ffa serve`: the JSON payloads, the shared feeds, and the static UI.

No sockets and no network: httpx's WSGI transport calls the app in-process, the draft feed
is fed a recording, and the waivers feed gets the same `FakeSleeper` the CLI tests use.
"""

from pathlib import Path

import httpx
import pytest

from ffa.config import load_config
from ffa.draft.research import PlayerVerdict, Research
from ffa.draft.session import ReplaySource, SessionError
from ffa.lineup.source import LineupUnavailable
from ffa.sleeper import SleeperError, SleeperRateLimited
from ffa.waivers.status import FREE_AGENT, WAIVERS
from ffa.web import AppState, DraftFeed, LineupFeed, WaiversFeed, create_app, urls
from ffa.web.state import LINEUP_TTL_SECONDS, OPEN_RETRY_SECONDS, WAIVERS_TTL_SECONDS

# The waiver fakes live with the tests that drive them hardest; `tests/` is importable.
from test_waivers import SAT_NIGHT, FakeSleeper, fake_kickoffs

INTERVAL = 2.0
MY_SLOT = 8


class Clock:
    """A monotonic clock the test moves by hand."""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class CountingSource:
    """A ReplaySource that counts polls and can be told to fail."""

    def __init__(self, snapshots):
        self._inner = ReplaySource(snapshots)
        self.polls = 0
        self.error: Exception | None = None

    def poll(self):
        self.polls += 1
        if self.error is not None:
            raise self.error
        return self._inner.poll()


class ReadyWorker:
    """Stands in for ResearchWorker: research is always warm, and picks the top candidate."""

    budget_left = 7

    def __init__(self):
        self.ensured = 0
        self.stopped = False

    def ensure(self, view):
        self.ensured += 1

    def get(self, view):
        top = view.shortlist.candidates[0].player
        return Research(
            pick_no=view.shortlist.decision_pick_no,
            summary="Take him and don't think about it.",
            recommendation_id=top.player_id,
            ranked=(PlayerVerdict(top.player_id, top.name, "clear best available", "high", "volume"),),
            sources=("https://example.com/a",),
            model="test-model",
            elapsed_seconds=4.2,
        )

    def status(self, view):
        return "ready"

    def failure(self, view):
        return None

    def shutdown(self):
        self.stopped = True


def make_config(**overrides):
    """Config from an explicit environment: never the developer's real `.env`."""
    environ = {"SLEEPER_USERNAME": "manager_one", "SLEEPER_LEAGUE_ID": "123", **overrides}
    return load_config(environ=environ, env_file=None)


def build_state(
    recording,
    *,
    clock=None,
    token=None,
    worker=None,
    opener=None,
    source=None,
    client_factory=None,
    config=None,
    lineup_loader=None,
):
    clock = clock or Clock()
    config = config or make_config()
    if opener is None:
        session = recording.session()
        source = source or CountingSource(recording.snapshots)
        opener = lambda: (session, source)  # noqa: E731
    draft = DraftFeed(
        config,
        opener=opener,
        interval=INTERVAL,
        my_slot=MY_SLOT,
        exclusions=lambda: [],  # cache/exclude.txt is the developer's, not the test's
        worker_factory=lambda: (worker, None if worker else "no credentials in tests"),
        monotonic=clock,
    )
    waivers = WaiversFeed(
        config,
        client_factory=client_factory or (lambda: FakeSleeper(SAT_NIGHT)),
        kickoffs=fake_kickoffs(SAT_NIGHT),
        now=lambda: SAT_NIGHT,
        monotonic=clock,
    )
    lineup = LineupFeed(
        config,
        client_factory=client_factory or (lambda: FakeSleeper(SAT_NIGHT)),
        loader=lineup_loader or _no_projections,
        now=lambda: SAT_NIGHT,
        monotonic=clock,
    )
    state = AppState(config=config, draft=draft, waivers=waivers, lineup=lineup, token=token)
    return state, clock, source


def _no_projections(client, **kwargs):
    """The default lineup loader here: no projections, so the route 503s with a reason.

    The projection layer has its own tests in `tests/test_lineup.py`; these ones are about
    the web app, and a test that needed both would be testing neither well.
    """
    raise LineupUnavailable("No projections in tests.")


@pytest.fixture
def web(recording):
    """An httpx client wired straight to the WSGI app, plus the state behind it."""
    state, clock, source = build_state(recording)
    with httpx.Client(transport=httpx.WSGITransport(app=create_app(state)),
                      base_url="http://ffa.test") as client:
        yield client, state, clock, source


# -- the draft endpoint ---------------------------------------------------------------


def test_draft_payload_carries_the_whole_screen(web):
    client, _, _, _ = web
    body = client.get("/api/draft").json()

    assert body["ok"] is True
    assert body["league"]["teams"] == 12 and body["league"]["rounds"] == 16
    assert body["you"]["slot"] == MY_SLOT
    assert body["board"]["current"]["label"] == "1.01"
    assert body["board"]["total_picks"] == 192
    assert body["clock"]["pick_timer"] == 90

    candidates = body["shortlist"]["candidates"]
    assert candidates and all(c["search_rank"] for c in candidates)
    assert {c["need_label"] for c in candidates} <= {"starter", "flex", "bench", "-"}
    assert body["shortlist"]["decision"]["pick_no"] == MY_SLOT  # slot 8 picks 8th in round 1
    assert [s["slot"] for s in body["roster"]["slots"][:3]] == ["QB", "RB", "RB"]
    assert body["research"]["status"] == "off"
    assert "no credentials" in body["research"]["note"]


def test_draft_poll_is_shared_and_rate_limited(web):
    """Two requests inside the interval must not become two Sleeper polls."""
    client, _, clock, source = web

    client.get("/api/draft")
    client.get("/api/draft")
    assert source.polls == 1

    clock.advance(INTERVAL)
    client.get("/api/draft")
    assert source.polls == 2


def test_draft_keeps_the_last_good_view_when_sleeper_fails(web):
    client, _, clock, source = web
    first = client.get("/api/draft").json()
    assert first["note"] is None

    source.error = SleeperError("Sleeper is having a day")
    clock.advance(INTERVAL)
    body = client.get("/api/draft").json()

    assert body["ok"] is True  # the board is still there, with the problem attached
    assert "Sleeper is having a day" in body["note"]
    assert body["board"]["current"]["label"] == first["board"]["current"]["label"]


def test_draft_rate_limit_backs_off_past_the_poll_interval(web):
    client, _, clock, source = web
    client.get("/api/draft")
    source.error = SleeperRateLimited("slow down", retry_after=90.0)
    clock.advance(INTERVAL)
    assert "rate limited" in client.get("/api/draft").json()["note"]

    source.error = None
    clock.advance(INTERVAL)  # inside the backoff: no new poll
    polls = source.polls
    client.get("/api/draft")
    assert source.polls == polls

    clock.advance(90.0)
    client.get("/api/draft")
    assert source.polls == polls + 1


def test_draft_context_is_the_pasteable_markdown(web):
    client, _, _, _ = web
    response = client.get("/api/draft/context")

    assert response.headers["content-type"].startswith("text/markdown")
    assert response.text.startswith("# Fantasy football draft")
    assert "## Shortlist" in response.text


def test_research_result_reaches_the_payload(recording):
    worker = ReadyWorker()
    state, _, _ = build_state(recording, worker=worker)
    with httpx.Client(transport=httpx.WSGITransport(app=create_app(state)),
                      base_url="http://ffa.test") as client:
        body = client.get("/api/draft").json()

    research = body["research"]
    assert research["status"] == "ready"
    assert research["pick"]["confidence"] == "high"
    assert research["budget_left"] == 7
    assert research["sources"] == ["https://example.com/a"]
    top = body["shortlist"]["candidates"][0]
    assert top["recommended"] is True and top["verdict"]["verdict"] == "clear best available"
    assert worker.ensured == 1

    state.shutdown()
    assert worker.stopped is True


def test_a_draft_that_cannot_be_opened_is_a_503_that_explains_itself(recording):
    calls = []

    def opener():
        calls.append(1)
        raise SessionError("League 123 has no drafts yet.")

    state, clock, _ = build_state(recording, opener=opener)
    with httpx.Client(transport=httpx.WSGITransport(app=create_app(state)),
                      base_url="http://ffa.test") as client:
        response = client.get("/api/draft")
        assert response.status_code == 503
        assert response.json() == {"ok": False, "error": "League 123 has no drafts yet."}

        client.get("/api/draft")
        assert len(calls) == 1  # don't re-resolve the league on every phone poll

        clock.advance(OPEN_RETRY_SECONDS)
        client.get("/api/draft")
        assert len(calls) == 2


# -- the waivers endpoint -------------------------------------------------------------


def test_waivers_payload_classifies_players(web):
    client, _, _, _ = web
    body = client.get("/api/waivers").json()

    assert body["ok"] is True and body["week"] == 7
    assert {p["name"]: p["status"] for p in body["players"]} == {
        "Locked Back": WAIVERS, "Dropped Back": WAIVERS, "Free Back": FREE_AGENT,
        "New England Patriots": WAIVERS,
    }
    assert list(body["locked_teams"]) == ["NE"]
    assert body["drop_hold_hours"] == 47.0


def test_waivers_report_is_cached_between_requests(recording):
    built = []

    def client_factory():
        built.append(1)
        return FakeSleeper(SAT_NIGHT)

    state, clock, _ = build_state(recording, client_factory=client_factory)
    with httpx.Client(transport=httpx.WSGITransport(app=create_app(state)),
                      base_url="http://ffa.test") as client:
        client.get("/api/waivers")
        client.get("/api/waivers")
        assert len(built) == 1

        clock.advance(WAIVERS_TTL_SECONDS)
        client.get("/api/waivers")
        assert len(built) == 2


def test_waivers_filters_by_name_and_position(web):
    client, _, _, _ = web

    named = client.get("/api/waivers", params={"q": "rostered"}).json()
    assert [p["rostered_by"] for p in named["players"]] == ["Team One"]

    defenses = client.get("/api/waivers", params={"pos": "DEF"}).json()
    assert [p["position"] for p in defenses["players"]] == ["DEF"]


def test_waivers_refuses_a_league_that_is_not_an_id(web):
    client, _, _, _ = web
    response = client.get("/api/waivers", params={"league": "../etc/passwd"})

    assert response.status_code == 503
    assert "numeric Sleeper league ID" in response.json()["error"]


def test_waivers_needs_a_league_from_somewhere(recording):
    config = make_config(SLEEPER_LEAGUE_ID="", DRAFT_ID="456")
    state, _, _ = build_state(recording, config=config)
    with httpx.Client(transport=httpx.WSGITransport(app=create_app(state)),
                      base_url="http://ffa.test") as client:
        response = client.get("/api/waivers")

    assert response.status_code == 503
    assert "SLEEPER_LEAGUE_ID" in response.json()["error"]


# -- the app itself -------------------------------------------------------------------


def test_static_ui_is_served_and_nothing_else_is(web):
    client, _, _, _ = web

    index = client.get("/")
    assert index.status_code == 200
    assert index.headers["content-type"].startswith("text/html")
    assert "ffa" in index.text
    assert client.get("/app.js").headers["content-type"].startswith("text/javascript")
    assert client.get("/styles.css").status_code == 200

    # Serving anything outside ffa/web/static would hand out the developer's files.
    escape = client.get("/../../ffa/config.py")
    assert escape.status_code == 404
    assert client.get("/api/nope").json() == {"ok": False, "error": "No such endpoint: /api/nope"}


def test_only_reads_are_allowed(web):
    client, _, _, _ = web
    response = client.post("/api/draft")

    assert response.status_code == 405
    assert "only reads" in response.json()["error"]


def test_health_reports_what_the_server_is_doing(web):
    client, state, _, _ = web
    body = client.get("/api/health").json()

    assert body == {
        "ok": True,
        "version": body["version"],
        "user": "manager_one",
        "league_configured": True,
        "poll_interval": INTERVAL,
        "token_required": False,
    }


def test_a_token_locks_the_whole_app(recording):
    state, _, _ = build_state(recording, token="s3cret")
    with httpx.Client(transport=httpx.WSGITransport(app=create_app(state)),
                      base_url="http://ffa.test") as client:
        assert client.get("/api/draft").status_code == 401
        assert client.get("/").status_code == 401
        assert client.get("/api/draft", headers={"X-FFA-Token": "s3cret"}).status_code == 200
        # The phone arrives with the token in the URL, then switches to the header.
        assert client.get("/", params={"token": "s3cret"}).status_code == 200
        assert client.get("/", params={"token": "wrong"}).status_code == 401


def test_the_poll_interval_never_goes_below_the_floor(recording):
    config = make_config(POLL_INTERVAL_SECONDS="1")
    state, _, _ = build_state(recording, config=config)
    assert state.draft.interval >= 1.0

    session = recording.session()
    feed = DraftFeed(config, opener=lambda: (session, ReplaySource(recording.snapshots)), interval=0.01)
    assert feed.interval == 1.0  # MIN_POLL_INTERVAL_SECONDS wins


def test_urls_are_the_ones_to_hand_the_user():
    assert urls("127.0.0.1", 8765) == ["http://127.0.0.1:8765/"]
    assert urls("127.0.0.1", 8765, "abc") == ["http://127.0.0.1:8765/?token=abc"]
    assert urls("0.0.0.0", 9000)[0] == "http://localhost:9000/"


def test_the_static_ui_only_talks_to_endpoints_that_exist():
    """The UI is plain JS with no tests of its own; at least keep its routes honest."""
    from ffa.web.app import ROUTES

    script = (Path(__file__).parent.parent / "ffa" / "web" / "static" / "app.js").read_text()
    for route in ROUTES:
        if route != "/api/health":
            assert f'"{route}"' in script, f"{route} is served but the UI never calls it"


# -- the lineup endpoint ---------------------------------------------------------------


def _advice_loader(calls):
    """A loader that builds real advice from the hand-made roster in test_lineup."""
    from test_lineup import SETTLED_BENCH, SETTLED_STARTERS, roster_world
    from ffa.lineup.advice import build_advice
    from ffa.lineup.source import LineupSources

    def load(client, **kwargs):
        calls.append(kwargs)
        world = roster_world(SETTLED_STARTERS, SETTLED_BENCH,
                             free_agents=[("fa_te", "TE", 12.0, 12.0)])
        return build_advice(**world), LineupSources(sleeper=True, espn=True)

    return load


def test_lineup_payload_carries_the_whole_screen(recording):
    calls = []
    state, _, _ = build_state(recording, lineup_loader=_advice_loader(calls))
    with httpx.Client(transport=httpx.WSGITransport(app=create_app(state)),
                      base_url="http://ffa.test") as client:
        body = client.get("/api/lineup").json()
    assert body["ok"] and body["week"] == 9
    assert [seat["slot"] for seat in body["lineup"]] == [
        "QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "FLEX", "K", "DEF"
    ]
    assert body["settled"] is True and body["swaps"] == []
    assert body["sources"] == {"sleeper": True, "espn": True, "espn_note": None}
    # Every number the phone draws is already computed: no arithmetic left in the browser.
    first = body["lineup"][0]["player"]
    assert {"points", "keep", "reasons", "on_bye", "disputed"} <= set(first)


def test_one_build_serves_every_screen(recording):
    """A build costs seconds and fifty megabytes from ESPN; two phones must not cause two."""
    calls = []
    state, clock, _ = build_state(recording, lineup_loader=_advice_loader(calls))
    with httpx.Client(transport=httpx.WSGITransport(app=create_app(state)),
                      base_url="http://ffa.test") as client:
        client.get("/api/lineup")
        client.get("/api/lineup")
        assert len(calls) == 1
        clock.advance(LINEUP_TTL_SECONDS + 1)
        client.get("/api/lineup")
    assert len(calls) == 2


def test_a_league_with_no_projections_explains_itself(recording):
    state, _, _ = build_state(recording)  # the default loader raises LineupUnavailable
    with httpx.Client(transport=httpx.WSGITransport(app=create_app(state)),
                      base_url="http://ffa.test") as client:
        response = client.get("/api/lineup")
    assert response.status_code == 503
    assert "No projections" in response.json()["error"]


def test_a_bad_league_id_is_refused_before_anything_is_fetched(recording):
    calls = []
    state, _, _ = build_state(recording, lineup_loader=_advice_loader(calls))
    with httpx.Client(transport=httpx.WSGITransport(app=create_app(state)),
                      base_url="http://ffa.test") as client:
        response = client.get("/api/lineup", params={"league": "../etc"})
    assert response.status_code == 503 and calls == []
