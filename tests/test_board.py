"""Snake math and board state. This is the code every other layer trusts."""

import pytest
from helpers import make_draft_raw, make_pick_raw, make_player

from draftbot import board
from draftbot.board import (
    UnsupportedDraftError,
    build_state,
    parse_draft,
    parse_picks,
    parse_traded_picks,
    pick_label,
    pick_no_for,
    round_for_pick,
    seconds_remaining,
    slot_for_pick,
)

TEAM_COUNTS = (8, 10, 12, 14)


def snake(pick_no, teams):
    return slot_for_pick(pick_no, teams, "snake", None)


# -- pick numbering ------------------------------------------------------------------


@pytest.mark.parametrize("teams", TEAM_COUNTS)
def test_first_and_last_pick_of_each_round(teams):
    for rnd in range(1, 17):
        first = (rnd - 1) * teams + 1
        last = rnd * teams
        if rnd % 2 == 1:
            assert snake(first, teams) == 1
            assert snake(last, teams) == teams
        else:
            assert snake(first, teams) == teams
            assert snake(last, teams) == 1


@pytest.mark.parametrize("teams", TEAM_COUNTS)
def test_every_round_is_a_permutation_of_slots(teams):
    for rnd in range(1, 17):
        picks = range((rnd - 1) * teams + 1, rnd * teams + 1)
        assert sorted(snake(n, teams) for n in picks) == list(range(1, teams + 1))


@pytest.mark.parametrize("teams", TEAM_COUNTS)
def test_round_boundaries_repeat_the_same_slot(teams):
    """The turn: the last pick of a round and the first of the next belong to one manager."""
    for rnd in range(1, 16):
        last_of_round = rnd * teams
        assert snake(last_of_round, teams) == snake(last_of_round + 1, teams)


def test_known_12_team_values():
    assert [snake(n, 12) for n in (1, 12, 13, 24, 25, 36, 37)] == [1, 12, 12, 1, 1, 12, 12]


@pytest.mark.parametrize("teams", TEAM_COUNTS)
def test_linear_draft_never_reverses(teams):
    for rnd in range(1, 17):
        for idx in range(1, teams + 1):
            pick_no = (rnd - 1) * teams + idx
            assert slot_for_pick(pick_no, teams, "linear", None) == idx


def test_third_round_reversal():
    # Rounds 1-2 are a normal snake; round 3 repeats round 2's order, then it alternates.
    reversal = 3
    slots = [slot_for_pick(n, 12, "snake", reversal) for n in (1, 12, 13, 24, 25, 36, 37, 48, 49)]
    assert slots == [1, 12, 12, 1, 12, 1, 1, 12, 12]


def test_reversal_gives_the_last_slot_back_to_back_to_back_picks():
    """In 3RR the 1.12 manager picks 12, 13 and 25."""
    picks = [n for n in range(1, 37) if slot_for_pick(n, 12, "snake", 3) == 12]
    assert picks[:3] == [12, 13, 25]


@pytest.mark.parametrize("teams", TEAM_COUNTS)
@pytest.mark.parametrize("reversal", [None, 3])
def test_pick_no_for_inverts_slot_for_pick(teams, reversal):
    for pick_no in range(1, teams * 16 + 1):
        rnd = round_for_pick(pick_no, teams)
        slot = slot_for_pick(pick_no, teams, "snake", reversal)
        assert pick_no_for(rnd, slot, teams, "snake", reversal) == pick_no


def test_pick_label():
    assert pick_label(1, 12) == "1.01"
    assert pick_label(29, 12) == "3.05"
    assert pick_label(24, 12) == "2.12"


# -- a real recorded draft -----------------------------------------------------------


def test_snake_math_matches_a_real_completed_draft(real_draft):
    """Every pick of a real 12-team, 180-pick Sleeper draft, checked against live data."""
    settings = parse_draft(real_draft["draft"])
    assert (settings.teams, settings.rounds, settings.draft_type) == (12, 15, "snake")
    assert len(real_draft["picks"]) == 180
    for raw in real_draft["picks"]:
        pick_no = raw["pick_no"]
        slot = slot_for_pick(pick_no, settings.teams, settings.draft_type, settings.reversal_round)
        assert slot == raw["draft_slot"], f"slot mismatch at pick {pick_no}"
        assert round_for_pick(pick_no, settings.teams) == raw["round"]
        assert settings.slot_to_roster_id[slot] == raw["roster_id"], f"roster mismatch at {pick_no}"


def test_real_draft_state_is_complete(real_draft, players_fixture):
    settings = parse_draft(real_draft["draft"])
    state = build_state(
        settings,
        parse_picks(real_draft["picks"]),
        parse_traded_picks(real_draft["traded_picks"]),
        my_user_id="76888557872365568",  # slot 1 in this draft's draft_order
        users={u["user_id"]: u["display_name"] for u in real_draft["league_users"]},
        players={},
    )
    assert state.my_slot == 1
    assert state.is_complete
    assert state.current_pick_no is None
    assert len(state.my_picks) == 15  # one per round


# -- parsing -------------------------------------------------------------------------


def test_auction_draft_is_rejected():
    with pytest.raises(UnsupportedDraftError, match="auction"):
        parse_draft(make_draft_raw(draft_type="auction"))


def test_a_broken_payload_is_not_the_same_as_an_unsupported_draft():
    """An auction is fatal; a garbled payload is one bad poll the watch loop can retry."""
    with pytest.raises(board.MalformedDraftError):
        parse_draft(make_draft_raw(settings={"teams": None}))
    with pytest.raises(board.MalformedDraftError):
        parse_draft({"draft_id": "d1", "error": "something"})
    assert not issubclass(board.MalformedDraftError, UnsupportedDraftError)


def test_reversal_round_zero_means_off():
    assert parse_draft(make_draft_raw(settings={"reversal_round": 0})).reversal_round is None
    assert parse_draft(make_draft_raw(settings={"reversal_round": 3})).reversal_round == 3


def test_null_draft_order_and_missing_slot_map():
    settings = parse_draft(make_draft_raw(draft_order=None, slot_to_roster_id=None))
    assert settings.draft_order is None
    assert settings.slot_to_roster_inferred
    assert settings.slot_to_roster_id[7] == 7


def test_settings_warnings_flag_unknown_and_reversal_like_keys():
    settings = parse_draft(make_draft_raw(settings={"third_round_reversal": 1, "mystery": 4}))
    warnings = " ".join(board.settings_warnings(settings))
    assert "third_round_reversal" in warnings
    assert "reversal_round" in warnings  # names the key the math actually honors
    assert "mystery" in warnings


def test_picks_missing_fields_are_still_usable():
    """The docs' own example pick has no roster_id, round or draft_slot."""
    raw = make_pick_raw(5, "111", omit=("roster_id", "round", "draft_slot"))
    picks = parse_picks([raw, {"garbage": True}])
    assert len(picks) == 1
    assert picks[0].roster_id is None and picks[0].pick_no == 5


def test_empty_picked_by_becomes_none():
    assert parse_picks([make_pick_raw(1, "1", picked_by="")])[0].picked_by is None


# -- whose turn ----------------------------------------------------------------------


def state_after(n_picks, *, teams=12, my_user="user8", players=None, traded=(), extra_picks=(),
                **draft_kwargs):
    raw = make_draft_raw(teams=teams, **draft_kwargs)
    settings = parse_draft(raw)
    picks = [make_pick_raw(i, f"p{i}", teams=teams) for i in range(1, n_picks + 1)]
    picks += list(extra_picks)
    return build_state(
        settings,
        parse_picks(picks),
        parse_traded_picks(traded),
        my_user_id=my_user,
        users={f"user{slot}": f"Manager {slot}" for slot in range(1, teams + 1)},
        players=players or {},
    )


def test_whose_turn_and_my_upcoming_picks():
    state = state_after(7)  # picks 1-7 gone, pick 8 is on the clock
    assert state.current_pick_no == 8
    assert state.on_clock_name == "Manager 8"
    assert state.my_slot == 8 and state.my_roster_id == 108
    assert state.is_my_turn
    # Slot 8 in a 12-team snake: 8, then 17 (round 2 reversed), then 32.
    assert state.my_upcoming_picks == (8, 17, 32)
    assert state.picks_until_my_next_turn == 9  # 8..16 inclusive


def test_not_my_turn_counts_selections_until_my_pick():
    state = state_after(3)
    assert state.current_pick_no == 4
    assert not state.is_my_turn
    assert state.my_remaining_picks[0] == 8
    assert state.picks_until_my_next_turn == 4  # picks 4,5,6,7


def test_my_picks_are_found_by_roster_id_not_username():
    state = state_after(20)
    assert [p.pick_no for p in state.my_picks] == [8, 17]


def test_traded_pick_changes_who_is_on_the_clock():
    traded = [{"season": "2026", "round": 1, "roster_id": 108, "owner_id": 103}]
    state = state_after(7, traded=traded)
    assert state.on_clock_roster_id == 103
    assert state.on_clock_name == "Manager 3"
    assert state.on_clock_via == "Manager 8"
    assert not state.is_my_turn
    assert state.my_remaining_picks[0] == 17  # round 1 pick now belongs to someone else


def test_traded_pick_from_another_season_is_ignored():
    traded = [{"season": "2027", "round": 1, "roster_id": 108, "owner_id": 103}]
    assert state_after(7, traded=traded).is_my_turn


def test_keeper_occupies_a_pick_slot_without_being_an_in_draft_selection():
    keeper = make_pick_raw(17, "kept1", roster_id=108, is_keeper=True, position="WR")
    state = state_after(7, extra_picks=[keeper])
    assert state.current_pick_no == 8  # not len(picks) + 1
    assert 17 in state.occupied
    assert state.my_remaining_picks[:2] == (8, 32)  # round 2 slot already used by the keeper
    assert "kept1" in {p.player_id for p in state.my_picks}
    assert 17 not in {p.pick_no for p in board.recent_picks_by_others(state)}


def test_duplicate_pick_numbers_are_deduplicated():
    dupe = make_pick_raw(3, "other")
    state = state_after(5, extra_picks=[dupe])
    assert len(state.picks) == 5
    assert state.picks[2].player_id == "other"  # last write wins


def test_a_deduplicated_pick_still_takes_its_player_off_the_board():
    """Collapsing two records onto one pick_no must not hand a drafted player back."""
    players = {f"p{i}": make_player(f"p{i}", "RB", i) for i in range(1, 6)}
    dupe = make_pick_raw(3, "p3")
    state = state_after(2, players=players, extra_picks=[make_pick_raw(3, "p4"), dupe])
    available = {p.player_id for p in state.available}
    assert "p3" not in available and "p4" not in available


def test_pick_rosters_fall_back_to_the_snake_math():
    """Picks in the wild are missing roster_id; the footer still needs a manager."""
    stripped = [make_pick_raw(n, f"p{n}", omit=("roster_id",)) for n in range(1, 5)]
    raw = make_draft_raw()
    settings = parse_draft(raw)
    state = build_state(
        settings, parse_picks(stripped), [], my_user_id="user8",
        users={f"user{s}": f"Manager {s}" for s in range(1, 13)}, players={},
    )
    assert state.pick_rosters == {1: 101, 2: 102, 3: 103, 4: 104}
    assert all(state.roster_names[r] for r in state.pick_rosters.values())


def test_missing_slot_map_is_learned_from_the_picks():
    """Without slot_to_roster_id, don't derive my roster and my future picks two ways."""
    picks = [make_pick_raw(n, f"p{n}", roster_id=200 + n) for n in range(1, 9)]
    settings = parse_draft(make_draft_raw(slot_to_roster_id=None))
    state = build_state(
        settings, parse_picks(picks), [], my_user_id="user1", users={}, players={}, my_slot=3
    )
    assert state.my_roster_id == 203  # learned from pick 3, not the assumed slot == roster
    assert [p.pick_no for p in state.my_picks] == [3]
    assert state.my_remaining_picks[0] == 22  # slot 3's round-2 pick, same manager


def test_traded_pick_chain_resolves_the_same_in_any_order():
    """Two hops on one pick: the end of the chain owns it, whatever order they arrive in."""
    hop1 = {"season": "2026", "round": 1, "roster_id": 108, "previous_owner_id": 108, "owner_id": 103}
    hop2 = {"season": "2026", "round": 1, "roster_id": 108, "previous_owner_id": 103, "owner_id": 111}
    forward = state_after(7, traded=[hop1, hop2])
    backward = state_after(7, traded=[hop2, hop1])
    assert forward.on_clock_roster_id == backward.on_clock_roster_id == 111


def test_draft_order_missing_leaves_slot_unknown_until_overridden():
    state = state_after(3, draft_order=None)
    assert state.my_slot is None and state.my_roster_id is None
    assert state.my_remaining_picks == ()

    raw = make_draft_raw(draft_order=None)
    settings = parse_draft(raw)
    with_override = build_state(
        settings, parse_picks([]), [], my_user_id="user8", users={}, players={}, my_slot=8
    )
    assert with_override.my_slot == 8
    assert with_override.my_roster_id == 108  # slot 8 -> roster 108 in this fixture's map


def test_complete_draft_has_no_current_pick():
    state = state_after(0, status="complete")
    assert state.is_complete and state.my_remaining_picks == ()


# -- clock ---------------------------------------------------------------------------


def test_countdown_counts_down_and_clamps_at_zero():
    settings = parse_draft(make_draft_raw(last_picked=1_000_000))
    assert seconds_remaining(settings, 1_000_000) == 90
    assert seconds_remaining(settings, 1_030_000) == 60
    assert seconds_remaining(settings, 9_000_000) == 0  # overdue, never negative


def test_no_timer_and_paused_have_no_countdown():
    untimed = parse_draft(make_draft_raw(settings={"pick_timer": 0}))
    assert board.pick_deadline_ms(untimed) is None
    assert seconds_remaining(untimed, 1_000_000) is None
    paused = parse_draft(make_draft_raw(status="paused"))
    assert seconds_remaining(paused, 1_000_000) is None


def test_clock_falls_back_to_start_time_before_the_first_pick():
    settings = parse_draft(make_draft_raw(last_picked=None, start_time=5_000_000))
    assert board.pick_deadline_ms(settings) == 5_000_000 + 90_000


# -- availability and runs -----------------------------------------------------------


def test_available_pool_filters():
    players = {
        "ok": make_player("ok", "RB", 10),
        "taken": make_player("taken", "WR", 5),
        "inactive": make_player("inactive", "WR", 6, active=False),
        "no_team": make_player("no_team", "WR", 7, team=None),
        "unranked": make_player("unranked", "WR", None),
        "sentinel": make_player("sentinel", "WR", 9_999_999),
        "DET": make_player("DET", "DEF", None, team="DET"),
    }
    ranked, defenses = board.available_players(players.values(), {"taken"})
    assert [p.player_id for p in ranked] == ["ok"]
    assert [p.player_id for p in defenses] == ["DET"]


def test_positional_runs():
    players = {f"p{i}": make_player(f"p{i}", "WR" if i > 4 else "RB", i) for i in range(1, 11)}
    state = state_after(10, players=players)
    runs = board.positional_runs(state, players)
    assert ("WR", 6, 8) in runs
