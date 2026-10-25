"""Research layer: prompt assembly, defensive parsing, budget, and the prefetch worker."""

import json
import threading
from types import SimpleNamespace

import pytest

from ffa.config import (
    WEB_SEARCH_TOOL_TYPE,
    WEB_SEARCH_TOOL_TYPE_LEGACY,
    web_search_tool_type,
)
from ffa.draft.research import (
    BudgetExhausted,
    ResearchClient,
    ResearchError,
    ResearchWorker,
    build_prompt,
    parse_response,
    shortlist_key,
)

GOOD_JSON = {
    "recommendation": {"player_id": "9509", "summary": "Bijan is the pick. Week 1 bell cow."},
    "ranked": [
        {"player_id": "9509", "verdict": "Every-down back", "confidence": "high",
         "rationale": "92% snap share in Week 1."},
        {"player_id": "7564", "verdict": "Elite target share", "confidence": "MEDIUM",
         "rationale": "31% target share."},
    ],
}
NAMES = {"9509": "Bijan Robinson", "7564": "Ja'Marr Chase"}


def block(kind, **kwargs):
    return SimpleNamespace(type=kind, **kwargs)


def message(text, *, stop_reason="end_turn", sources=()):
    content = [block("text", text=text)]
    if sources:
        content.insert(0, block(
            "web_search_tool_result",
            content=[SimpleNamespace(url=url, title="t") for url in sources],
        ))
    return SimpleNamespace(content=content, stop_reason=stop_reason)


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class FakeAnthropic:
    def __init__(self, *responses):
        self.messages = FakeMessages(responses)


# -- parsing --------------------------------------------------------------------------


def test_parse_plain_json():
    summary, pick, ranked = parse_response(json.dumps(GOOD_JSON), NAMES)
    assert pick == "9509"
    assert summary.startswith("Bijan is the pick")
    assert [v.player_id for v in ranked] == ["9509", "7564"]
    assert ranked[0].name == "Bijan Robinson"
    assert ranked[1].confidence == "medium"  # normalized


def test_parse_strips_markdown_fences_and_prose():
    text = f"Here you go:\n```json\n{json.dumps(GOOD_JSON)}\n```\nHope that helps!"
    summary, pick, ranked = parse_response(text, NAMES)
    assert pick == "9509" and len(ranked) == 2


def test_parse_tolerates_junk_entries_and_missing_recommendation():
    payload = {"ranked": [{"nope": 1}, GOOD_JSON["ranked"][0], "garbage"]}
    summary, pick, ranked = parse_response(json.dumps(payload), NAMES)
    assert summary == ""
    assert pick == "9509"  # falls back to the top of the ranking
    assert len(ranked) == 1


@pytest.mark.parametrize("text", ["", "no json here", "{not json at all}", "[]"])
def test_unparseable_responses_raise(text):
    with pytest.raises(ResearchError):
        parse_response(text, NAMES)


def test_unknown_confidence_is_not_trusted():
    payload = {"ranked": [{"player_id": "9509", "confidence": "extremely high"}]}
    _, _, ranked = parse_response(json.dumps(payload), NAMES)
    assert ranked[0].confidence == "unknown"


# -- prompt ---------------------------------------------------------------------------


def test_prompt_has_the_context_the_spec_requires(mock_session, recording):
    from ffa.draft.session import compute_view

    view = compute_view(mock_session, recording.snapshots[5])
    prompt = build_prompt(view)
    assert "after Week 1 has been played" in prompt
    assert "PPR" in prompt  # scoring settings
    assert "QB, RB×2" in prompt  # roster requirements
    assert "Open starting slots:" in prompt
    assert "3.08 (pick #32" in prompt  # current round and pick
    assert "Positional dropoff" in prompt
    for candidate in view.shortlist.candidates:
        assert f"player_id={candidate.player.player_id}" in prompt
    assert '"recommendation"' in prompt and '"ranked"' in prompt  # strict JSON contract


def test_web_search_tool_version_matches_the_model():
    assert web_search_tool_type("claude-sonnet-5") == WEB_SEARCH_TOOL_TYPE
    assert web_search_tool_type("claude-opus-5") == WEB_SEARCH_TOOL_TYPE
    assert web_search_tool_type("claude-3-5-sonnet-20241022") == WEB_SEARCH_TOOL_TYPE_LEGACY


# -- client ---------------------------------------------------------------------------


def view_for(mock_session, recording, index=5):
    from ffa.draft.session import compute_view

    return compute_view(mock_session, recording.snapshots[index])


def test_research_call_shape_and_result(mock_session, recording):
    view = view_for(mock_session, recording)
    fake = FakeAnthropic(message(json.dumps(GOOD_JSON), sources=["https://example.com/a"]))
    client = ResearchClient("k", "claude-sonnet-5", budget=5, client=fake)
    result = client.research(view)

    sent = fake.messages.calls[0]
    assert sent["model"] == "claude-sonnet-5"
    assert sent["tools"][0]["type"] == WEB_SEARCH_TOOL_TYPE
    assert sent["tools"][0]["name"] == "web_search"
    assert 3 <= sent["tools"][0]["max_uses"] <= 5  # bounded latency and cost
    assert sent["thinking"] == {"type": "adaptive"}
    assert result.recommended().player_id == "9509"
    # A player_id the model invented (or that isn't on this shortlist) still round-trips,
    # it just has no name to show.
    assert result.recommended().name == "9509"
    assert result.sources == ("https://example.com/a",)
    assert result.pick_no == view.shortlist.decision_pick_no


def test_names_resolve_for_players_on_the_shortlist(mock_session, recording):
    view = view_for(mock_session, recording)
    candidate = view.shortlist.candidates[0].player
    payload = {"recommendation": {"player_id": candidate.player_id, "summary": "take him"},
               "ranked": [{"player_id": candidate.player_id, "verdict": "v",
                           "confidence": "high", "rationale": "r"}]}
    fake = FakeAnthropic(message(json.dumps(payload)))
    result = ResearchClient("k", "claude-sonnet-5", budget=5, client=fake).research(view)
    assert result.recommended().name == candidate.name


def test_paused_turn_is_continued(mock_session, recording):
    """Server-side tool use can stop with pause_turn; the call has to be resumed."""
    view = view_for(mock_session, recording)
    fake = FakeAnthropic(
        message("partial", stop_reason="pause_turn", sources=["https://a"]),
        message(json.dumps(GOOD_JSON), sources=["https://b"]),
    )
    client = ResearchClient("k", "claude-sonnet-5", budget=5, client=fake)
    result = client.research(view)
    assert len(fake.messages.calls) == 2
    assert fake.messages.calls[1]["messages"][-1]["role"] == "assistant"
    assert result.sources == ("https://a", "https://b")


def test_budget_is_a_hard_stop(mock_session, recording):
    view = view_for(mock_session, recording)
    fake = FakeAnthropic(*[message(json.dumps(GOOD_JSON)) for _ in range(3)])
    client = ResearchClient("k", "claude-sonnet-5", budget=2, client=fake)
    client.research(view)
    client.research(view)
    with pytest.raises(BudgetExhausted):
        client.research(view)
    assert len(fake.messages.calls) == 2
    assert client.budget_left == 0


def test_sdk_failures_become_research_errors(mock_session, recording):
    view = view_for(mock_session, recording)

    class Boom:
        messages = SimpleNamespace(create=lambda **kw: (_ for _ in ()).throw(RuntimeError("503")))

    client = ResearchClient("k", "claude-sonnet-5", budget=5, client=Boom())
    with pytest.raises(ResearchError, match="503"):
        client.research(view)


# -- worker ---------------------------------------------------------------------------


class StubClient:
    """Stands in for ResearchClient: records calls, can block or fail on demand."""

    def __init__(self, result=None, error=None, gate=None, budget=10):
        self.result, self.error, self.gate = result, error, gate
        self.calls = 0
        self.budget = budget

    @property
    def budget_left(self):
        return self.budget - self.calls

    def research(self, view):
        self.calls += 1
        if self.gate is not None:
            self.gate.wait(5)
        if self.error is not None:
            raise self.error
        return self.result


def fake_result(pick_no):
    from ffa.draft.research import PlayerVerdict, Research

    return Research(pick_no, "summary", "9509",
                    (PlayerVerdict("9509", "Bijan Robinson", "v", "high", "r"),), (), "m", 1.0)


def test_worker_prefetches_once_per_shortlist(mock_session, recording):
    view = view_for(mock_session, recording)
    stub = StubClient(result=fake_result(view.shortlist.decision_pick_no))
    worker = ResearchWorker(stub, concurrency=2)
    try:
        worker.ensure(view)
        assert worker.wait(view, 5) is not None
        for _ in range(5):  # every poll re-offers the same shortlist
            worker.ensure(view)
        assert stub.calls == 1  # cached for the session
        assert worker.status(view) == "ready"
        assert worker.get(view).recommended().name == "Bijan Robinson"
    finally:
        worker.shutdown()


def test_worker_never_raises_when_research_fails(mock_session, recording):
    view = view_for(mock_session, recording)
    worker = ResearchWorker(StubClient(error=ResearchError("api down")), concurrency=1)
    try:
        assert worker.wait(view, 5) is None
        assert worker.status(view) == "failed"
        assert "api down" in worker.failure(view)
        worker.ensure(view)  # a failure is not retried in a tight loop
        assert worker.status(view) == "failed"
    finally:
        worker.shutdown()


def test_wait_degrades_when_research_is_slow(mock_session, recording):
    """`recommend` must not block the clock: a slow call returns None, fast."""
    view = view_for(mock_session, recording)
    gate = threading.Event()
    worker = ResearchWorker(StubClient(result=fake_result(32), gate=gate), concurrency=1)
    try:
        assert worker.wait(view, 0.1) is None
        assert worker.status(view) == "researching"
    finally:
        gate.set()
        worker.shutdown()


def test_worker_respects_an_exhausted_budget(mock_session, recording):
    view = view_for(mock_session, recording)
    worker = ResearchWorker(StubClient(budget=0), concurrency=1)
    try:
        worker.ensure(view)
        assert worker.status(view) == "failed"
        assert "budget" in worker.failure(view)
    finally:
        worker.shutdown()


def test_shortlist_key_changes_with_the_board(mock_session, recording):
    early, later = view_for(mock_session, recording, 2), view_for(mock_session, recording, 5)
    assert shortlist_key(early) != shortlist_key(later)
    assert shortlist_key(early) == shortlist_key(view_for(mock_session, recording, 2))


def test_worker_skips_an_empty_shortlist(mock_session, recording):
    complete = view_for(mock_session, recording, 9)  # draft over, nothing to research
    stub = StubClient(result=fake_result(1))
    worker = ResearchWorker(stub, concurrency=1)
    try:
        worker.ensure(complete)
        assert stub.calls == 0
    finally:
        worker.shutdown()
