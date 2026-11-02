"""A `DraftView` as JSON, for the web UI. Pure functions of a view; nothing here fetches.

`ffa/draft/ui.py` and this module render the same `DraftView`, so the phone and the terminal
show the same numbers — only the formatting differs. Every field the UI draws comes from
here, which keeps the browser free of fantasy logic: no scoring, no ranking, no clock math
beyond ticking a countdown the server already computed.
"""

from typing import Any

from ..draft.board import pick_deadline_ms, pick_label, round_for_pick, seconds_remaining
from ..draft.needs import NEED_FLEX, NEED_STARTER
from ..draft.session import DraftView
from ..draft.shortlist import likely_gone_label, player_status
from ..models import Player


def need_label(need: float) -> str:
    """The same three buckets the terminal shows: starter / flex / bench."""
    if need >= NEED_STARTER:
        return "starter"
    if need >= NEED_FLEX:
        return "flex"
    return "bench" if need > 0 else "-"


def player_json(player: Player | None) -> dict[str, Any] | None:
    if player is None:
        return None
    return {
        "player_id": player.player_id,
        "name": player.name,
        "position": player.position,
        "team": player.team,
        "search_rank": player.search_rank,
        "injury_status": player.injury_status,
        "status_note": player_status(player) or None,
        "can_play": player.can_play,
    }


def _pick_json(pick_no: int | None, teams: int) -> dict[str, Any] | None:
    if pick_no is None:
        return None
    return {"pick_no": pick_no, "label": pick_label(pick_no, teams), "round": round_for_pick(pick_no, teams)}


def clock_json(view: DraftView, now_ms: int) -> dict[str, Any]:
    """Enough for the browser to tick a countdown without asking again."""
    settings = view.state.settings
    return {
        "pick_timer": settings.pick_timer,
        "deadline_ms": pick_deadline_ms(settings),
        "seconds_remaining": seconds_remaining(settings, now_ms),
    }


def board_json(view: DraftView) -> dict[str, Any]:
    state = view.state
    teams = state.settings.teams
    return {
        "complete": state.is_complete,
        "status": state.status,
        # The terminal holds back "YOUR PICK" until the draft is actually running: pick 1 is
        # nominally on the clock for as long as the lobby sits there.
        "is_my_turn": state.is_my_turn and state.status in ("drafting", "paused"),
        "current": _pick_json(state.current_pick_no, teams),
        "total_picks": state.settings.total_picks,
        "on_clock_name": state.on_clock_name,
        "on_clock_via": state.on_clock_via,
        "my_picks": [_pick_json(n, teams) for n in state.my_upcoming_picks],
        "picks_until_my_turn": state.picks_until_my_next_turn,
    }


def roster_json(view: DraftView) -> dict[str, Any]:
    keepers = {pick.player_id for pick in view.state.my_picks if pick.is_keeper}
    slots = []
    for slot in view.fill.slots:
        player = player_json(slot.player)
        slots.append({
            "slot": slot.slot,
            "starter": slot.is_starter,
            "player": player,
            "keeper": slot.filled and slot.player.player_id in keepers,
            # A starting slot held by an Out/IR player is still a hole to fill.
            "needs_replacement": slot.is_starter and slot.filled and not slot.player.can_play,
        })
    return {"slots": slots, "open_starters": list(view.fill.unfilled_starter_slots)}


def shortlist_json(view: DraftView, research: Any = None) -> dict[str, Any]:
    shortlist = view.shortlist
    teams = view.state.settings.teams
    verdicts = {v.player_id: v for v in research.ranked} if research else {}
    recommended = research.recommendation_id if research else None
    candidates = []
    for candidate in shortlist.candidates:
        verdict = verdicts.get(candidate.player.player_id)
        candidates.append({
            **player_json(candidate.player),
            "adj": round(candidate.adj, 1),
            "dropoff": candidate.dropoff,
            "need": candidate.need,
            "need_label": need_label(candidate.need),
            "likely_gone": candidate.likely_gone,
            "recommended": candidate.player.player_id == recommended,
            "verdict": verdict_json(verdict),
        })
    dropoffs = [
        {
            "position": position,
            "value": drop.value,
            "best_now": drop.best_now.name,
            "best_then": drop.best_then.name if drop.best_then else None,
        }
        for position, drop in shortlist.dropoffs.items()
        if drop.best_now is not None and position not in shortlist.blocked
    ]
    return {
        "unavailable": shortlist.unavailable,
        "on_the_clock": shortlist.on_the_clock,
        "decision": _pick_json(shortlist.decision_pick_no, teams),
        "following": _pick_json(shortlist.following_pick_no, teams),
        "gone_label": likely_gone_label(shortlist),
        "candidates": candidates,
        "dropoffs": dropoffs,
        "blocked": dict(shortlist.blocked),
        # Sleeper publishes no search_rank for team defenses, so these are listed, not ranked.
        "defenses": [player.player_id for player in shortlist.defense_options],
    }


def verdict_json(verdict: Any) -> dict[str, Any] | None:
    if verdict is None:
        return None
    return {
        "player_id": verdict.player_id,
        "name": verdict.name,
        "verdict": verdict.verdict,
        "confidence": verdict.confidence,
        "rationale": verdict.rationale,
        "tier": verdict.tier,
    }


def research_json(view: DraftView, research: Any, status: str, budget_left: int | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": status,  # off | idle | researching | ready | failed
        "note": view.research_note,
        "budget_left": budget_left,
        "summary": None,
        "pick": None,
        "ranked": [],
        "sources": [],
        "elapsed_seconds": None,
    }
    if research is None:
        return payload
    recommended = research.recommended()
    payload.update(
        summary=research.summary,
        pick=verdict_json(recommended),
        ranked=[verdict_json(v) for v in research.ranked],
        sources=list(research.sources),
        elapsed_seconds=round(research.elapsed_seconds, 1),
        model=research.model,
    )
    return payload


def recent_json(view: DraftView) -> list[dict[str, Any]]:
    teams = view.state.settings.teams
    rows = []
    for pick in view.recent:
        player = view.session.players.get(pick.player_id) or Player.from_pick(pick)
        roster_id = view.state.pick_rosters.get(pick.pick_no)
        rows.append({
            "pick_no": pick.pick_no,
            "label": pick_label(pick.pick_no, teams),
            "manager": view.state.roster_names.get(roster_id, "?") if roster_id is not None else "?",
            "player": player_json(player),
        })
    return rows


def draft_json(
    view: DraftView,
    *,
    now_ms: int,
    note: str | None = None,
    research: Any = None,
    research_status: str = "off",
    next_poll_in: float | None = None,
    budget_left: int | None = None,
) -> dict[str, Any]:
    """Everything the draft screen draws, in one response."""
    session, settings = view.session, view.state.settings
    return {
        "ok": True,
        "note": note,
        "fetched_at_ms": view.fetched_at_ms,
        "server_now_ms": now_ms,
        "next_poll_in": round(next_poll_in, 1) if next_poll_in is not None else None,
        "league": {
            "draft_id": session.draft_id,
            "name": session.league_name,
            "season": settings.season,
            "draft_type": settings.draft_type,
            "teams": settings.teams,
            "rounds": settings.rounds,
            "scoring": session.scoring_summary,
            "lineup": view.fill.requirements.describe(),
            "warnings": list(view.warnings),
        },
        "you": {
            "display_name": session.display_name,
            "slot": view.state.my_slot,
            "roster_id": view.state.my_roster_id,
        },
        "clock": clock_json(view, now_ms),
        "board": board_json(view),
        "roster": roster_json(view),
        "shortlist": shortlist_json(view, research),
        "research": research_json(view, research, research_status, budget_left),
        "recent": recent_json(view),
        "runs": [{"position": pos, "count": count, "window": window} for pos, count, window in view.runs],
    }
