"""Roster requirements, slot filling, need weights, and the shortlist guardrails."""

import json
import pytest
from conftest import FIXTURES
from helpers import make_player

from ffa.draft.needs import (
    NEED_BENCH,
    NEED_FLEX,
    NEED_STARTER,
    QB_CAP,
    QB_CAP_SUPERFLEX,
    TE_CAP,
    block_reason,
    fill_roster,
    need_weight,
    parse_roster_positions,
    requirements_from_draft_settings,
)

SPEC_POSITIONS = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "FLEX", "K", "DEF"] + ["BN"] * 6


def test_parse_spec_roster():
    req = parse_roster_positions(SPEC_POSITIONS)
    assert req.starters == {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "K": 1, "DEF": 1}
    assert req.flex == ("FLEX", "FLEX")
    assert req.bench == 6
    assert not req.superflex
    assert req.accepts("RB") and req.accepts("K") and not req.accepts("QB2")


def test_parse_superflex_and_rec_flex():
    req = parse_roster_positions(["QB", "SUPER_FLEX", "REC_FLEX", "WR", "BN"])
    assert req.superflex
    assert req.flex == ("SUPER_FLEX", "REC_FLEX")
    assert req.accepts("TE")  # via REC_FLEX


def test_unknown_slots_are_recorded_not_crashed():
    req = parse_roster_positions(["QB", "DL", "LB", "IDP_FLEX", "BN"])
    assert req.ignored == ("DL", "LB", "IDP_FLEX")
    assert req.starters == {"QB": 1}


def test_requirements_from_mock_draft_settings():
    req = requirements_from_draft_settings({
        "slots_qb": 1, "slots_rb": 2, "slots_wr": 3, "slots_te": 1,
        "slots_super_flex": 1, "slots_k": 1, "slots_def": 1, "slots_bn": 5, "teams": 12,
    })
    assert req.starters == {"QB": 1, "RB": 2, "WR": 3, "TE": 1, "K": 1, "DEF": 1}
    assert req.flex == ("SUPER_FLEX",)
    assert req.bench == 5


def test_real_league_roster_positions(real_draft):
    """The needs model against a real league's roster_positions array, not a generic one."""
    req = parse_roster_positions(real_draft["league"]["roster_positions"])
    assert req.starters["RB"] == 2 and req.starters["DEF"] == 1
    assert req.flex == ("FLEX", "FLEX")
    assert "K" not in req.starters and not req.accepts("K")


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.jsonl")))
def test_recorded_league_roster_positions_parse(path):
    """Any recording dropped into tests/fixtures (including a real one) must parse."""
    header = json.loads(path.read_text().splitlines()[0])
    league = header.get("league") or {}
    if not league.get("roster_positions"):
        pytest.skip(f"{path.name} has no league roster_positions")
    req = parse_roster_positions(league["roster_positions"])
    assert len(req.positions) == len(league["roster_positions"])
    assert sum(req.starters.values()) + len(req.flex) + req.bench + len(req.ignored) == len(req.positions)


# -- filling -------------------------------------------------------------------------


def fill(positions, players):
    return fill_roster(parse_roster_positions(positions), players)


def test_concrete_slots_fill_before_flex():
    rb1, rb2, rb3 = (make_player(f"rb{i}", "RB", i) for i in (1, 2, 3))
    roster = fill(SPEC_POSITIONS, [rb1, rb2, rb3])
    seated = [(s.slot, s.player.player_id if s.player else None) for s in roster.slots if s.filled]
    assert seated == [("RB", "rb1"), ("RB", "rb2"), ("FLEX", "rb3")]
    assert "RB" not in roster.unfilled_starter_slots
    assert roster.count("RB") == 3


def test_overflow_goes_to_the_bench():
    players = [make_player(f"te{i}", "TE", i) for i in range(1, 5)]
    roster = fill(SPEC_POSITIONS, players)
    assert [s.slot for s in roster.slots if s.filled] == ["TE", "FLEX", "FLEX", "BN"]


def test_most_restrictive_flex_is_filled_first():
    roster = fill(["WR", "FLEX", "REC_FLEX", "BN"], [
        make_player("wr1", "WR", 1), make_player("te1", "TE", 2), make_player("rb1", "RB", 3),
    ])
    seated = {s.slot: s.player.player_id for s in roster.slots if s.filled}
    assert seated == {"WR": "wr1", "REC_FLEX": "te1", "FLEX": "rb1"}


def test_multi_position_player_prefers_its_primary_slot():
    hunter = make_player("hunter", "WR", 5, fantasy_positions=("WR", "TE"))
    roster = fill(["WR", "TE", "BN"], [hunter])
    assert {s.slot: s.player.player_id for s in roster.slots if s.filled} == {"WR": "hunter"}


def test_unknown_position_lands_on_the_bench():
    mystery = make_player("m", "?", None, fantasy_positions=())
    roster = fill(SPEC_POSITIONS, [mystery])
    assert [s.slot for s in roster.slots if s.filled] == ["BN"]


# -- weights and guardrails ----------------------------------------------------------


def test_need_weight_tiers():
    roster = fill(SPEC_POSITIONS, [make_player("rb1", "RB", 1), make_player("rb2", "RB", 2)])
    assert need_weight("WR", roster) == NEED_STARTER  # WR slots still open
    assert need_weight("RB", roster) == NEED_FLEX  # RB starters full, FLEX open
    full = fill(["QB", "RB", "BN"], [make_player("qb", "QB", 1), make_player("rb", "RB", 2)])
    assert need_weight("RB", full) == NEED_BENCH["RB"]
    assert need_weight("QB", full) == NEED_BENCH["QB"]


def block(position, roster, *, rnd=3, rounds=16, picks_left=10):
    return block_reason(position, roster, decision_round=rnd, total_rounds=rounds, picks_left=picks_left)


def test_kicker_and_defense_wait_for_the_final_two_rounds():
    roster = fill(SPEC_POSITIONS, [])
    assert block("K", roster, rnd=14, rounds=16) is not None
    assert block("DEF", roster, rnd=14, rounds=16) is not None
    assert block("K", roster, rnd=15, rounds=16, picks_left=2) is None
    assert block("DEF", roster, rnd=16, rounds=16, picks_left=1) is None


def test_kicker_surfaces_early_only_when_nothing_else_is_left_to_fill():
    """An empty roster with 10 picks and 10 open slots must not reach for a kicker."""
    empty = fill(SPEC_POSITIONS, [])
    assert block("K", empty, rnd=14, rounds=16, picks_left=10) is not None
    starters = [make_player(f"rb{i}", "RB", i) for i in (1, 2)]
    starters += [make_player(f"wr{i}", "WR", i) for i in (3, 4, 5, 6)]
    starters += [make_player("qb", "QB", 7), make_player("te", "TE", 8)]
    only_k_def = fill(SPEC_POSITIONS, starters)
    assert block("K", only_k_def, rnd=13, rounds=16, picks_left=2) is None


def test_defense_is_not_surfaced_twice():
    roster = fill(SPEC_POSITIONS, [make_player("DET", "DEF", None, team="DET")])
    assert block("DEF", roster, rnd=16, rounds=16, picks_left=1) == "DEF slot already filled"


def test_position_absent_from_the_league_is_blocked():
    roster = fill(["QB", "RB", "WR", "TE", "FLEX", "BN"], [])
    assert block("K", roster) == "no K slot in this league"


def test_a_two_quarterback_league_can_draft_two_quarterbacks():
    """The cap is a depth cap; it must never sit below the starters the league requires."""
    positions = ["QB", "QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "K", "DEF"] + ["BN"] * 6
    one_qb = fill(positions, [make_player("qb1", "QB", 1)])
    assert one_qb.requirements.depth_cap("QB") == 2
    assert need_weight("QB", one_qb) == NEED_STARTER
    assert block("QB", one_qb) is None  # the need and the guardrail must agree
    two_qbs = fill(positions, [make_player("qb1", "QB", 1), make_player("qb2", "QB", 2)])
    assert block("QB", two_qbs) == "QB cap reached (2)"


def test_flex_seating_is_order_independent():
    """WRRB_FLEX and REC_FLEX accept equal-sized, non-nested sets: first-fit strands a player."""
    rb, wr = make_player("rb", "RB", 1), make_player("wr", "WR", 2)
    for order in ([rb, wr], [wr, rb]):
        roster = fill(["WRRB_FLEX", "REC_FLEX", "BN", "BN"], order)
        assert roster.unfilled_starter_slots == ()
        assert {s.slot: s.player.player_id for s in roster.slots if s.filled} == {
            "WRRB_FLEX": "rb", "REC_FLEX": "wr",
        }


def test_quarterback_and_tight_end_caps():
    one_qb = fill(SPEC_POSITIONS, [make_player("qb1", "QB", 1)])
    assert block("QB", one_qb) == f"QB cap reached ({QB_CAP})"

    superflex = fill(["QB", "SUPER_FLEX", "BN", "BN"], [make_player("qb1", "QB", 1)])
    assert block("QB", superflex) is None
    two_qbs = fill(["QB", "SUPER_FLEX", "BN", "BN"], [make_player("qb1", "QB", 1), make_player("qb2", "QB", 2)])
    assert two_qbs.requirements.superflex
    assert block("QB", two_qbs) == f"QB cap reached ({QB_CAP_SUPERFLEX})"

    two_tes = fill(SPEC_POSITIONS, [make_player(f"te{i}", "TE", i) for i in (1, 2)])
    assert block("TE", two_tes) == f"TE cap reached ({TE_CAP})"
    # Flex slots don't raise the TE cap: a third tight end for a FLEX isn't the plan.
    assert parse_roster_positions(SPEC_POSITIONS).depth_cap("TE") == TE_CAP


def test_last_picks_are_reserved_for_open_starting_slots():
    """Two picks left, K and DEF still empty: nothing else may be surfaced."""
    drafted = [make_player(f"rb{i}", "RB", i) for i in range(1, 3)]
    drafted += [make_player(f"wr{i}", "WR", i) for i in range(1, 5)]
    drafted += [make_player("qb", "QB", 9), make_player("te", "TE", 10)]
    roster = fill(SPEC_POSITIONS, drafted)
    assert set(roster.unfilled_starter_slots) == {"K", "DEF"}
    assert block("RB", roster, rnd=15, rounds=16, picks_left=2) == "remaining picks are needed for open starting slots"
    assert block("K", roster, rnd=15, rounds=16, picks_left=2) is None
    assert block("DEF", roster, rnd=15, rounds=16, picks_left=2) is None


# -- availability ---------------------------------------------------------------------


def test_out_and_ir_players_do_not_fill_a_starting_slot():
    """A starter you cannot start is still a hole — the whole point of the Bowers case."""
    bowers = make_player("bowers", "TE", 22, injury_status="Out")
    roster = fill(SPEC_POSITIONS, [bowers])
    assert "TE" in roster.unfilled_starter_slots  # the slot is still a hole
    assert not any(s.filled for s in roster.slots if s.is_starter)  # he holds no slot
    assert [s.player.player_id for s in roster.slots if s.filled] == ["bowers"]  # benched
    assert need_weight("TE", roster) == NEED_STARTER
    # ...and he doesn't consume the depth cap either.
    assert roster.count("TE") == 0
    assert block("TE", roster) is None


def test_questionable_players_still_count_as_starters():
    roster = fill(SPEC_POSITIONS, [make_player("q", "TE", 22, injury_status="Questionable")])
    assert "TE" not in roster.unfilled_starter_slots
    assert [s.slot for s in roster.slots if s.filled] == ["TE"]


def test_inactive_status_counts_as_unavailable_even_without_an_injury_status():
    roster = fill(SPEC_POSITIONS, [make_player("x", "TE", 22, status="Inactive")])
    assert "TE" in roster.unfilled_starter_slots


def test_na_designation_with_an_active_status_is_playable():
    """Sleeper's one 'NA' in the live map sits on an Active player: no designation, not out."""
    jacobs = make_player("jacobs", "RB", 20, injury_status="NA", status="Active")
    assert jacobs.can_play
    roster = fill(SPEC_POSITIONS, [jacobs])
    assert "RB" in roster.unfilled_starter_slots  # only one of the two RB slots is filled
    assert roster.count("RB") == 1


def test_a_healthy_replacement_takes_the_slot_from_an_injured_starter():
    """Drafting Fannin while Bowers is Out must promote Fannin, not bench him."""
    bowers = make_player("bowers", "TE", 22, injury_status="Out")
    fannin = make_player("fannin", "TE", 63)
    roster = fill(SPEC_POSITIONS, [bowers, fannin])  # Bowers drafted first
    seated = [(s.slot, s.player.player_id) for s in roster.slots if s.filled]
    assert ("TE", "fannin") in seated  # the healthy TE starts...
    assert ("TE", "bowers") not in seated  # ...and the injured one does not hold the slot
    assert "TE" not in roster.unfilled_starter_slots
    # The TE starting slot is covered; a tight end is now only worth a FLEX, not a starter.
    assert need_weight("TE", roster) == NEED_FLEX

    # He goes to the bench, not into an open FLEX slot he equally cannot fill.
    benched = [s.player.player_id for s in roster.slots if s.slot == "BN" and s.filled]
    assert benched == ["bowers"]
    assert all(s.player.can_play for s in roster.slots if s.is_starter and s.filled)
