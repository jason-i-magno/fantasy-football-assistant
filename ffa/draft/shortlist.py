"""Candidate generation and ranking for the user's next pick, plus the pasteable context block.

The heuristic's job is to narrow a few hundred available players to the handful worth
thinking about. Judgment (Week 1 usage, injuries, role changes) happens in research or in
the Claude conversation the context block is pasted into.
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from .board import pick_label, round_for_pick
from ..models import FANTASY_POSITIONS, DraftState, Pick, Player
from .needs import RosterFill, block_reason, need_weight
from .scarcity import Dropoff, candidate_score, positional_dropoffs

SHORTLIST_SIZE = 10
# Candidates projected gone before your pick are shown, not hidden: the 'Gone?' flag says
# so, and hiding them made the flags look self-contradictory (a tied rank can push a name
# you expect to see just outside the simulated cut).


@dataclass(frozen=True, slots=True)
class Candidate:
    player: Player
    adj: float  # adjusted rank, lower is better
    need: float
    dropoff: int
    likely_gone: bool  # projected to be taken before your next pick


@dataclass(frozen=True)
class Shortlist:
    candidates: tuple[Candidate, ...] = ()
    decision_pick_no: int | None = None  # the pick this shortlist is for
    following_pick_no: int | None = None  # your pick after that one
    on_the_clock: bool = False
    dropoffs: Mapping[str, Dropoff] = field(default_factory=dict)
    blocked: Mapping[str, str] = field(default_factory=dict)  # position -> reason
    defense_options: tuple[Player, ...] = ()  # unranked; listed when DEF may be drafted
    unavailable: str | None = None  # why there is no shortlist right now


def build_shortlist(state: DraftState, fill: RosterFill, *, size: int = SHORTLIST_SIZE) -> Shortlist:
    if state.is_complete:
        return Shortlist(unavailable="The draft is complete.")
    if state.my_roster_id is None:
        if state.my_slot is not None:
            return Shortlist(
                unavailable=f"Draft slot {state.my_slot} isn't in this "
                f"{state.settings.teams}-team draft. Use --slot 1..{state.settings.teams}."
            )
        return Shortlist(
            unavailable="Your draft slot isn't known yet (draft order not set). Pass --slot N to plan anyway."
        )
    remaining = state.my_remaining_picks
    if not remaining:
        return Shortlist(unavailable="You have no picks left.")

    settings = state.settings
    decision = remaining[0]
    following = remaining[1] if len(remaining) > 1 else None
    picks_before = state.open_picks_between(state.current_pick_no, decision)
    picks_between = state.open_picks_between(decision, following) if following else None
    dropoffs = positional_dropoffs(state.available, picks_before, picks_between)

    decision_round = round_for_pick(decision, settings.teams)
    blocked = {
        pos: reason
        for pos in FANTASY_POSITIONS
        if (reason := block_reason(
            pos, fill, decision_round=decision_round, total_rounds=settings.rounds, picks_left=len(remaining)
        ))
    }

    gone_ids = {p.player_id for p in state.available[: state.picks_until_my_next_turn or 0]}
    scored = []
    for player in state.available:
        if player.position in blocked:
            continue
        need = need_weight(player.position, fill)
        drop = dropoffs[player.position].value if player.position in dropoffs else 0
        scored.append(Candidate(
            player=player,
            adj=candidate_score(player, need, drop, settings.teams),
            need=need,
            dropoff=drop,
            likely_gone=player.player_id in gone_ids,
        ))
    scored.sort(key=lambda c: (c.adj, c.player.search_rank, c.player.player_id))

    # With only enough picks left to cover the open starting slots, make sure each of those
    # positions is represented: otherwise a run of tight ends can hide the last kicker.
    must_fill = len(remaining) <= len(fill.unfilled_starter_slots)
    promoted: list[Candidate] = []
    if must_fill:
        for slot in dict.fromkeys(fill.unfilled_starter_slots):
            best = next((c for c in scored if c.player.position == slot), None)
            if best is not None:
                promoted.append(best)
        promoted = promoted[:size]

    on_clock = state.is_my_turn
    chosen: list[Candidate] = list(promoted)
    for candidate in scored:
        if len(chosen) >= size:
            break
        if candidate in promoted:
            continue
        chosen.append(candidate)
    chosen.sort(key=lambda c: (c.adj, c.player.search_rank, c.player.player_id))

    return Shortlist(
        candidates=tuple(chosen),
        decision_pick_no=decision,
        following_pick_no=following,
        on_the_clock=on_clock,
        dropoffs=dropoffs,
        blocked=blocked,
        defense_options=() if "DEF" in blocked else state.available_defenses,
    )


def likely_gone_label(shortlist: Shortlist, *, first_person: bool = False) -> str:
    whose = "my" if first_person else "your"
    if shortlist.on_the_clock:
        return f"won't last to {whose} next pick"
    return f"likely gone before {whose} pick"


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def player_status(player: Player) -> str:
    """Sleeper's injury_status and status, verbatim, so nothing has to be looked up."""
    bits = [b for b in (player.injury_status, player.status) if b]
    # "Active"/"Active" adds nothing; anything else is worth reading.
    bits = [b for b in bits if b.lower() != "active"]
    return " / ".join(dict.fromkeys(bits))


def _player_line(player: Player) -> str:
    status = player_status(player)
    return f"{player.name} ({player.position}, {player.team or 'FA'}{', ' + status if status else ''})"


def context_block(
    state: DraftState,
    fill: RosterFill,
    shortlist: Shortlist,
    *,
    scoring_summary: str,
    players: Mapping[str, Player],
    recent: Sequence[Pick] = (),
    runs: Sequence[tuple[str, int, int]] = (),
) -> str:
    """Markdown the user can paste into a Claude conversation for a second opinion."""
    s = state.settings
    teams = s.teams

    def label(n: int) -> str:
        return f"{pick_label(n, teams)} (#{n})"

    fmt = f"{teams}-team {s.draft_type} draft, {s.rounds} rounds"
    if s.reversal_round:
        fmt += f", order reverses again from round {s.reversal_round}"

    out = [
        "# Fantasy football draft: help me make my pick",
        "",
        f"Generated {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')}"
        + (f", {s.season} season." if s.season else "."),
        "",
        "This draft is happening after NFL Week 1. Weight actual Week 1 usage (snap share, "
        "target share, carries, goal-line work, backfield splits) above preseason projections and ADP.",
        "",
        "## League",
        f"- Format: {fmt}",
        f"- Scoring: {scoring_summary}",
        f"- Starting lineup: {fill.requirements.describe()}",
        "",
        "## Where the draft stands",
    ]
    if state.current_pick_no is not None:
        if shortlist.on_the_clock:
            out.append(f"- I am on the clock at pick {label(state.current_pick_no)}.")
        else:
            on_clock = state.on_clock_name or "?"
            out.append(f"- Current pick: {label(state.current_pick_no)}, {on_clock} is on the clock.")
            if shortlist.decision_pick_no is not None:
                before = state.open_picks_between(state.current_pick_no, shortlist.decision_pick_no)
                out.append(
                    f"- My next pick: {label(shortlist.decision_pick_no)}, "
                    f"{plural(before, 'selection')} from now."
                )
    later = [n for n in state.my_upcoming_picks if n != shortlist.decision_pick_no]
    if later:
        out.append("- My picks after that: " + ", ".join(label(n) for n in later))
    if recent:
        counts = Counter(
            (players.get(p.player_id) or Player.from_pick(p)).position for p in recent
        )
        summary = ", ".join(f"{pos}x{n}" for pos, n in counts.most_common())
        out.append(f"- Last {len(recent)} picks: {summary}")
        out.append("  " + "; ".join(
            f"{pick_label(p.pick_no, teams)} {_player_line(players.get(p.player_id) or Player.from_pick(p))}"
            for p in reversed(recent)
        ))
    for pos, count, window in runs:
        out.append(f"- Positional run in progress: {pos}, {count} of the last {window} picks")

    out += ["", "## My roster so far"]
    for slot in fill.slots:
        if slot.filled:
            out.append(f"- {slot.slot}: {_player_line(slot.player)}")
        elif slot.is_starter:
            out.append(f"- {slot.slot}: (open)")

    if shortlist.unavailable:
        out += ["", f"_No shortlist: {shortlist.unavailable}_"]
        return "\n".join(out)

    if shortlist.following_pick_no is not None:
        out += [
            "",
            f"## Positional dropoff from pick {label(shortlist.decision_pick_no)} to "
            f"{label(shortlist.following_pick_no)}",
            "search_rank spots between the best player likely available at my pick and the best "
            "likely left at my following pick (bigger means the position is drying up):",
        ]
        for pos, drop in shortlist.dropoffs.items():
            if pos in shortlist.blocked or drop.best_now is None:
                continue
            then = f"{drop.best_then.name} #{drop.best_then.search_rank}" if drop.best_then else "nobody"
            out.append(f"- {pos}: {drop.value} ({drop.best_now.name} #{drop.best_now.search_rank} -> {then})")

    gone = sum(1 for c in shortlist.candidates if c.likely_gone)
    out += [
        "",
        "## Shortlist",
        "Ranked by a heuristic blending Sleeper's search_rank (an ADP proxy, not a projection), "
        "my roster needs, and positional dropoff.",
        f"{gone} of these are projected gone before my pick (the simulation assumes managers "
        "take the top of the board by search_rank). Sleeper ties many players at the same "
        "search_rank, so the players right at that cut-off are a coin flip, not a ranking.",
        "",
        f"| # | Player | Pos | Team | search_rank | Status | "
        f"{likely_gone_label(shortlist, first_person=True).capitalize()} |",
        "|---|---|---|---|---|---|---|",
    ]
    for i, c in enumerate(shortlist.candidates, start=1):
        p = c.player
        out.append(
            f"| {i} | {p.name} | {p.position} | {p.team or 'FA'} | {p.search_rank} | "
            f"{player_status(p) or '-'} | {'yes' if c.likely_gone else 'no'} |"
        )
    if shortlist.defense_options:
        out += ["", "Team defenses still available (Sleeper doesn't rank defenses): "
                + ", ".join(p.player_id for p in shortlist.defense_options)]
    if shortlist.blocked:
        out += ["", "Excluded by roster rules: " + "; ".join(f"{pos} ({why})" for pos, why in shortlist.blocked.items())]

    pick_text = label(shortlist.decision_pick_no)
    out += [
        "",
        "## Question",
        f"Rank these into tiers for best-available at pick {pick_text}, and flag any player I "
        "should take over a higher-ranked one for structural reasons (positional scarcity, my "
        "open starting slots, a bye or schedule conflict).",
        "",
        "Give me a conditional plan rather than a single name, because several of these will be "
        'gone by the time I pick: "if X is there, him; if X and Y are both gone, then Z." '
        "For each player you rank, check current role and depth chart (and any change in the last "
        "month), Week 1 usage, injury status, offensive context (QB situation, pace, competition "
        "for touches), and notable early-season or fantasy-playoff schedule factors.",
    ]
    return "\n".join(out)
