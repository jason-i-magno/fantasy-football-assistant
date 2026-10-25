"""Web-search-backed research on the shortlist, via the Anthropic API.

This layer is an enhancement, never a dependency: every failure path returns None or raises
ResearchError, and the caller falls back to the heuristic shortlist. Nothing here ever blocks
the draft clock — `ResearchWorker` runs calls on a background thread so the recommendation is
already warm by the time the user is on the clock.
"""

import json
import logging
import re
import threading
import time
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .board import pick_label, round_for_pick
from ..config import (
    RESEARCH_EFFORT,
    RESEARCH_TIMEOUT_SECONDS,
    WEB_SEARCH_MAX_USES,
    web_search_tool_type,
)
from .session import DraftView

log = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent / "prompts" / "research.md"
MAX_TOKENS = 4000
MAX_PAUSE_CONTINUATIONS = 2  # server tool use can return stop_reason="pause_turn"
CONFIDENCES = ("high", "medium", "low")


class ResearchError(Exception):
    """The research call failed. Callers degrade to the heuristic shortlist."""


class BudgetExhausted(ResearchError):
    """RESEARCH_BUDGET calls have been made this session; hard stop."""


@dataclass(frozen=True, slots=True)
class PlayerVerdict:
    player_id: str
    name: str
    verdict: str
    confidence: str
    rationale: str
    tier: int | None = None


@dataclass(frozen=True)
class Research:
    pick_no: int
    summary: str
    recommendation_id: str | None
    ranked: tuple[PlayerVerdict, ...]
    sources: tuple[str, ...]
    model: str
    elapsed_seconds: float

    def recommended(self) -> PlayerVerdict | None:
        return next((v for v in self.ranked if v.player_id == self.recommendation_id), None)


def shortlist_key(view: DraftView) -> tuple:
    """Identifies a shortlist: the pick it's for, its candidates, and the roster behind it."""
    return (
        view.shortlist.decision_pick_no,
        tuple(c.player.player_id for c in view.shortlist.candidates),
        tuple(p.player_id for p in view.state.my_players),
    )


# -- prompt ---------------------------------------------------------------------------


def _roster_lines(view: DraftView) -> str:
    lines = []
    for slot in view.fill.slots:
        if slot.filled:
            player = slot.player
            lines.append(f"- {slot.slot}: {player.name} ({player.position}, {player.team or 'FA'})")
        elif slot.is_starter:
            lines.append(f"- {slot.slot}: (open)")
    return "\n".join(lines) or "- (nothing drafted yet)"


def _candidate_lines(view: DraftView) -> str:
    lines = []
    for i, c in enumerate(view.shortlist.candidates, start=1):
        p = c.player
        bits = [f"search_rank {p.search_rank}"]
        if p.injury_status:
            bits.append(f"injury: {p.injury_status}")
        if c.likely_gone:
            bits.append("may be gone before my pick")
        lines.append(f"{i}. player_id={p.player_id} | {p.name} | {p.position} | "
                     f"{p.team or 'FA'} | {', '.join(bits)}")
    for defense in view.shortlist.defense_options[:32]:
        lines.append(f"- player_id={defense.player_id} | {defense.name} | DEF | "
                     f"{defense.team} | Sleeper publishes no rank for defenses")
    return "\n".join(lines)


def _dropoff_lines(view: DraftView) -> str:
    lines = []
    for pos, drop in view.shortlist.dropoffs.items():
        if drop.best_now is None or pos in view.shortlist.blocked:
            continue
        then = f"{drop.best_then.name} (#{drop.best_then.search_rank})" if drop.best_then else "nobody"
        lines.append(f"- {pos}: {drop.value} — {drop.best_now.name} "
                     f"(#{drop.best_now.search_rank}) -> {then}")
    return "\n".join(lines) or "- (no following pick; this is my last one)"


def _pick_context(view: DraftView) -> str:
    state, shortlist = view.state, view.shortlist
    teams = state.settings.teams
    decision = shortlist.decision_pick_no
    rnd = round_for_pick(decision, teams)
    text = f"I pick at {pick_label(decision, teams)} (pick #{decision} overall, round {rnd})."
    if shortlist.on_the_clock:
        text += " I am on the clock right now."
    elif state.current_pick_no is not None:
        before = state.open_picks_between(state.current_pick_no, decision)
        text += f" {before} selection(s) happen before it."
    if shortlist.following_pick_no is not None:
        text += (f" My following pick is {pick_label(shortlist.following_pick_no, teams)} "
                 f"(#{shortlist.following_pick_no}).")
    return text


def build_prompt(view: DraftView, template: str | None = None) -> str:
    settings = view.state.settings
    fmt = f"{settings.teams}-team {settings.draft_type} draft, {settings.rounds} rounds"
    if settings.reversal_round:
        fmt += f", order reverses again from round {settings.reversal_round}"
    open_slots = view.fill.unfilled_starter_slots
    return (template or PROMPT_PATH.read_text()).format(
        season=settings.season or "current",
        format=fmt,
        scoring=view.session.scoring_summary,
        lineup=view.fill.requirements.describe(),
        roster=_roster_lines(view),
        open_slots=", ".join(open_slots) if open_slots else "none — every starter is filled",
        pick_context=_pick_context(view),
        dropoffs=_dropoff_lines(view),
        candidates=_candidate_lines(view),
    )


# -- response parsing -----------------------------------------------------------------


def _strip_fences(text: str) -> str:
    fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    return fenced.group(1) if fenced else text


def parse_response(text: str, names: dict[str, str]) -> tuple[str, str | None, tuple[PlayerVerdict, ...]]:
    """Pull the JSON payload out of the model's reply. Defensive by design."""
    candidate = _strip_fences(text).strip()
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end <= start:
        raise ResearchError("no JSON object in the research response")
    try:
        payload = json.loads(candidate[start : end + 1])
    except ValueError as exc:
        raise ResearchError(f"research response was not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ResearchError("research response was not a JSON object")

    recommendation = payload.get("recommendation")
    recommendation = recommendation if isinstance(recommendation, dict) else {}
    summary = str(recommendation.get("summary") or "").strip()
    pick_id = recommendation.get("player_id")
    pick_id = str(pick_id) if pick_id is not None else None

    ranked = []
    for entry in payload.get("ranked") or ():
        if not isinstance(entry, dict) or not entry.get("player_id"):
            continue
        player_id = str(entry["player_id"])
        confidence = str(entry.get("confidence") or "").lower()
        ranked.append(PlayerVerdict(
            player_id=player_id,
            name=names.get(player_id, player_id),
            verdict=str(entry.get("verdict") or "").strip(),
            confidence=confidence if confidence in CONFIDENCES else "unknown",
            rationale=str(entry.get("rationale") or "").strip(),
            tier=entry.get("tier") if isinstance(entry.get("tier"), int) else None,
        ))
    if not summary and not ranked:
        raise ResearchError("research response had neither a recommendation nor a ranking")
    if pick_id is None and ranked:
        pick_id = ranked[0].player_id
    return summary, pick_id, tuple(ranked)


def _collect(message: Any) -> tuple[str, list[str]]:
    """Text blocks joined, plus the URLs the web search actually cited."""
    text_parts, sources = [], []
    for block in getattr(message, "content", ()) or ():
        kind = getattr(block, "type", None)
        if kind == "text":
            text_parts.append(getattr(block, "text", ""))
        elif kind == "web_search_tool_result":
            content = getattr(block, "content", None)
            # A failed search returns a single error object rather than a list of results.
            for result in content if isinstance(content, list) else ():
                url = getattr(result, "url", None)
                if url:
                    sources.append(url)
    return "".join(text_parts), sources


# -- client ---------------------------------------------------------------------------


class ResearchClient:
    """One web-search-backed call per shortlist, with a hard per-session budget."""

    def __init__(self, api_key: str | None, model: str, budget: int, *, client: Any = None,
                 timeout: float = RESEARCH_TIMEOUT_SECONDS):
        self.model = model
        self.budget = budget
        self.calls = 0
        self._lock = threading.Lock()
        if client is not None:
            self._client = client
        else:
            import anthropic

            # No api_key means "resolve credentials the usual way": ANTHROPIC_AUTH_TOKEN, an
            # OAuth profile from `ant auth login`, or workload identity federation.
            options = {"timeout": timeout, "max_retries": 1}
            if api_key:
                options["api_key"] = api_key
            self._client = anthropic.Anthropic(**options)

    @property
    def budget_left(self) -> int:
        return max(0, self.budget - self.calls)

    def research(self, view: DraftView) -> Research:
        if not view.shortlist.candidates and not view.shortlist.defense_options:
            raise ResearchError("nothing to research")
        with self._lock:
            if self.calls >= self.budget:
                raise BudgetExhausted(f"research budget of {self.budget} calls is used up")
            self.calls += 1

        names = {c.player.player_id: c.player.name for c in view.shortlist.candidates}
        names.update({p.player_id: p.name for p in view.shortlist.defense_options})
        started = time.monotonic()
        messages: list[dict[str, Any]] = [{"role": "user", "content": build_prompt(view)}]
        text, sources = "", []

        try:
            for _ in range(MAX_PAUSE_CONTINUATIONS + 1):
                message = self._client.messages.create(
                    model=self.model,
                    max_tokens=MAX_TOKENS,
                    thinking={"type": "adaptive"},
                    output_config={"effort": RESEARCH_EFFORT},
                    tools=[{
                        "type": web_search_tool_type(self.model),
                        "name": "web_search",
                        "max_uses": WEB_SEARCH_MAX_USES,
                    }],
                    messages=messages,
                )
                chunk_text, chunk_sources = _collect(message)
                text += chunk_text
                sources += chunk_sources
                if getattr(message, "stop_reason", None) != "pause_turn":
                    break
                # The server paused mid tool use; hand its own content back to continue.
                messages = [*messages, {"role": "assistant", "content": message.content}]
        except ResearchError:
            raise
        except Exception as exc:  # noqa: BLE001 - any SDK failure degrades to the shortlist
            raise ResearchError(f"research call failed: {exc}") from exc

        summary, pick_id, ranked = parse_response(text, names)
        return Research(
            pick_no=view.shortlist.decision_pick_no,
            summary=summary,
            recommendation_id=pick_id,
            ranked=ranked,
            sources=tuple(dict.fromkeys(sources)),
            model=self.model,
            elapsed_seconds=time.monotonic() - started,
        )


# -- prefetch worker ------------------------------------------------------------------


class ResearchWorker:
    """Keeps the current shortlist's research warm on background threads.

    On every poll the caller hands over the latest view; if that shortlist hasn't been
    researched yet, a job starts immediately — during other managers' picks, which is the
    whole point. Results are cached for the session and never block the caller.
    """

    def __init__(self, client: ResearchClient, concurrency: int = 4):
        self._client = client
        self._pool = ThreadPoolExecutor(max_workers=max(1, concurrency),
                                        thread_name_prefix="ffa-research")
        self._lock = threading.Lock()
        self._results: dict[tuple, Research] = {}
        self._failures: dict[tuple, str] = {}
        self._pending: dict[tuple, Future] = {}

    def ensure(self, view: DraftView) -> None:
        """Start research for this shortlist if it isn't already done or running."""
        if view.shortlist.unavailable or not view.shortlist.candidates:
            return
        key = shortlist_key(view)
        with self._lock:
            if key in self._results or key in self._pending or key in self._failures:
                return
            if self._client.budget_left <= 0:
                self._failures[key] = "research budget used up"
                return
            self._pending[key] = self._pool.submit(self._run, key, view)

    def _run(self, key: tuple, view: DraftView) -> Research | None:
        try:
            result = self._client.research(view)
        except ResearchError as exc:
            log.warning("Research failed for pick %s: %s", view.shortlist.decision_pick_no, exc)
            with self._lock:
                self._failures[key] = str(exc)
                self._pending.pop(key, None)
            return None
        log.info("Research for pick %s landed in %.1fs", result.pick_no, result.elapsed_seconds)
        with self._lock:
            self._results[key] = result
            self._pending.pop(key, None)
        return result

    def get(self, view: DraftView) -> Research | None:
        with self._lock:
            return self._results.get(shortlist_key(view))

    def wait(self, view: DraftView, seconds: float) -> Research | None:
        """Block up to `seconds` for a result. Used by one-shot `recommend`, never by watch."""
        self.ensure(view)
        key = shortlist_key(view)
        with self._lock:
            pending = self._pending.get(key)
            done = self._results.get(key)
        if done is not None:
            return done
        if pending is None:
            return None
        try:
            pending.result(timeout=seconds)
        except Exception:  # noqa: BLE001 - timeout or failure both mean "degrade"
            return None
        return self.get(view)

    def status(self, view: DraftView) -> str:
        """One of: ready, researching, failed, idle."""
        key = shortlist_key(view)
        with self._lock:
            if key in self._results:
                return "ready"
            if key in self._pending:
                return "researching"
            if key in self._failures:
                return "failed"
        return "idle"

    def failure(self, view: DraftView) -> str | None:
        with self._lock:
            return self._failures.get(shortlist_key(view))

    @property
    def calls_made(self) -> int:
        return self._client.calls

    @property
    def budget_left(self) -> int:
        return self._client.budget_left

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
