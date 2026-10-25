"""Shortlist ranking, guardrails, and the pasteable context block."""

from helpers import make_draft_raw, make_pick_raw, make_player, make_pool

from draftbot.board import build_state, parse_draft, parse_picks
from draftbot.needs import fill_roster, parse_roster_positions
from draftbot.shortlist import build_shortlist, context_block

SPEC_POSITIONS = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "FLEX", "K", "DEF"] + ["BN"] * 6


def scenario(n_picks, *, players=None, rounds=16, my_user="user8", positions=SPEC_POSITIONS,
             extra_picks=(), teams=12):
    players = players or make_pool({"RB": 60, "WR": 60, "QB": 20, "TE": 20, "K": 10})
    settings = parse_draft(make_draft_raw(teams=teams, rounds=rounds))
    picks = [make_pick_raw(i, f"taken{i}", teams=teams) for i in range(1, n_picks + 1)]
    picks += list(extra_picks)
    state = build_state(
        settings,
        parse_picks(picks),
        [],
        my_user_id=my_user,
        users={f"user{s}": f"Manager {s}" for s in range(1, teams + 1)},
        players=players,
    )
    fill = fill_roster(parse_roster_positions(positions), state.my_players)
    return state, fill, build_shortlist(state, fill)


def test_shortlist_is_ranked_and_sized():
    state, fill, shortlist = scenario(7)
    assert shortlist.on_the_clock and shortlist.decision_pick_no == 8
    assert shortlist.following_pick_no == 17
    assert len(shortlist.candidates) == 10
    assert [c.adj for c in shortlist.candidates] == sorted(c.adj for c in shortlist.candidates)


def test_kickers_and_defenses_are_not_surfaced_early():
    state, fill, shortlist = scenario(7)
    assert "K" in shortlist.blocked and "DEF" in shortlist.blocked
    assert shortlist.defense_options == ()
    assert not any(c.player.position in ("K", "DEF") for c in shortlist.candidates)


def test_defenses_appear_in_the_final_rounds_with_no_rank():
    players = make_pool({"RB": 60, "WR": 60, "QB": 20, "TE": 20, "K": 10})
    players["DET"] = make_player("DET", "DEF", None, team="DET", name="Detroit Lions")
    players["PHI"] = make_player("PHI", "DEF", None, team="PHI", name="Philadelphia Eagles")
    # Round 15 of 16: my pick is #176, with a roster that still needs K and DEF.
    state, fill, shortlist = scenario(175, players=players)
    assert shortlist.decision_pick_no == 176
    assert "DEF" not in shortlist.blocked
    assert {p.player_id for p in shortlist.defense_options} == {"DET", "PHI"}
    assert any(c.player.position == "K" for c in shortlist.candidates)


def test_endgame_shows_a_candidate_for_every_open_starting_slot():
    """Two picks left with TE, K and DEF open: a run of tight ends must not hide the kicker."""
    players = make_pool({"TE": 20, "K": 10, "RB": 40, "WR": 40, "QB": 10})
    state, fill, shortlist = scenario(175, players=players)
    assert set(fill.unfilled_starter_slots) >= {"K"}
    assert any(c.player.position == "K" for c in shortlist.candidates)


def test_quarterback_cap_removes_the_position():
    qb_pick = make_pick_raw(8, "QB1", roster_id=108, position="QB")
    players = make_pool({"RB": 60, "WR": 60, "QB": 20, "TE": 20, "K": 10})
    state, fill, shortlist = scenario(12, players=players, extra_picks=[qb_pick])
    assert fill.count("QB") == 1
    assert shortlist.blocked.get("QB") == "QB cap reached (1)"
    assert not any(c.player.position == "QB" for c in shortlist.candidates)


def test_likely_gone_candidates_are_flagged_not_hidden():
    """Hiding them made the flags look self-contradictory and dropped real names."""
    state, fill, shortlist = scenario(3)  # pick 4 on the clock, mine is 8
    assert not shortlist.on_the_clock
    assert state.picks_until_my_next_turn == 4
    flagged = [c for c in shortlist.candidates if c.likely_gone]
    assert len(flagged) == 4  # every player the simulation expects to be gone is shown
    assert len(shortlist.candidates) == 10
    # The top of the board by adjusted rank is present, gone or not.
    best = min(state.available, key=lambda p: (p.search_rank, p.player_id))
    assert best.player_id in {c.player.player_id for c in shortlist.candidates}


def test_on_the_clock_keeps_every_candidate():
    state, fill, shortlist = scenario(7)
    # Flagged here means "won't be back at your next pick", so they are not filtered out.
    assert shortlist.on_the_clock
    assert len(shortlist.candidates) == 10


def test_no_shortlist_without_a_draft_slot():
    settings = parse_draft(make_draft_raw(draft_order=None))
    state = build_state(settings, [], [], my_user_id="user8", users={}, players={})
    shortlist = build_shortlist(state, fill_roster(parse_roster_positions(SPEC_POSITIONS), ()))
    assert "draft order not set" in shortlist.unavailable
    assert shortlist.candidates == ()


def test_defense_only_endgame_shows_the_defenses_instead_of_an_empty_table():
    """Defenses can never be candidates (no rank), so the panel must not just go blank."""
    from rich.console import Console

    from draftbot import ui
    from draftbot.session import DraftView

    players = make_pool({"RB": 40, "WR": 40, "QB": 10, "TE": 10, "K": 10})
    players["DET"] = make_player("DET", "DEF", None, team="DET", name="Detroit Lions")
    state, _, _ = scenario(184, players=players)  # one pick left, in the final round
    assert state.my_remaining_picks == (185,)

    drafted = [make_player("qb", "QB", 1), make_player("te", "TE", 2), make_player("k", "K", 3)]
    drafted += [make_player(f"rb{i}", "RB", i) for i in (4, 5)]
    drafted += [make_player(f"wr{i}", "WR", i) for i in (6, 7, 8, 9)]
    fill = fill_roster(parse_roster_positions(SPEC_POSITIONS), drafted)
    assert fill.unfilled_starter_slots == ("DEF",)

    shortlist = build_shortlist(state, fill)
    assert shortlist.candidates == ()
    assert [p.player_id for p in shortlist.defense_options] == ["DET"]

    view = DraftView(None, state, fill, shortlist, (), (), (), 0)
    console = Console(width=100, height=20, record=True, file=open("/dev/null", "w"))
    console.print(ui.shortlist_panel(view))
    rendered = console.export_text()
    assert "Only a team defense is left to fill." in rendered
    assert "DET" in rendered
    console.file.close()


def test_out_of_range_slot_says_so():
    settings = parse_draft(make_draft_raw())
    state = build_state(settings, [], [], my_user_id="user8", users={}, players={}, my_slot=99)
    shortlist = build_shortlist(state, fill_roster(parse_roster_positions(SPEC_POSITIONS), ()))
    assert shortlist.unavailable == "Draft slot 99 isn't in this 12-team draft. Use --slot 1..12."


def test_no_shortlist_when_my_picks_are_used_up():
    state, fill, shortlist = scenario(191)
    assert shortlist.unavailable == "You have no picks left."


def test_context_block_has_what_claude_needs():
    state, fill, shortlist = scenario(7)
    text = context_block(
        state, fill, shortlist, scoring_summary="PPR (1/reception)", players={}, recent=(), runs=()
    )
    assert "after NFL Week 1" in text
    assert "PPR (1/reception)" in text
    assert "12-team snake draft, 16 rounds" in text
    assert "## Shortlist" in text
    assert shortlist.candidates[0].player.name in text
    assert "search_rank" in text and "not a projection" in text
    assert "1.08 (#8)" in text  # the pick it is asking about
    # Asks for tiers and a conditional, because the board will have moved by then.
    assert "tiers" in text and "conditional" in text
    assert "structural reasons" in text
    assert "Generated 20" in text  # dated, so nothing anchors on stale information
    assert "ties many players at the same search_rank" in text
    # The roster list already marks open slots; the summary line that repeated it is gone.
    assert "- QB: (open)" in text
    assert "Open starting slots" not in text


def test_context_block_survives_an_empty_shortlist():
    settings = parse_draft(make_draft_raw(draft_order=None))
    state = build_state(settings, [], [], my_user_id="user8", users={}, players={})
    fill = fill_roster(parse_roster_positions(SPEC_POSITIONS), ())
    text = context_block(state, fill, build_shortlist(state, fill), scoring_summary="PPR", players={})
    assert "No shortlist" in text
