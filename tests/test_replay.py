"""End-to-end: the whole pipeline over recorded snapshots, with no network."""

import json

import pytest
from conftest import FIXTURES
from rich.console import Console

from draftbot import cli
from draftbot.board import owner_of_pick, parse_traded_picks, traded_overrides
from draftbot.session import (
    ReplaySource,
    Snapshot,
    compute_view,
    summarize_scoring,
)

RECORDING = FIXTURES / "replay_mock_12team.jsonl"


def view_at(session, recording, index, **kwargs):
    return compute_view(session, recording.snapshots[index], **kwargs)


def test_recording_loads_with_league_context(mock_session, recording):
    assert len(recording.snapshots) == 10
    assert mock_session.draft_id == "999000000000000001"
    assert mock_session.league_name == "Synthetic Mock League"
    assert mock_session.requirements.starters["RB"] == 2
    assert "PPR" in mock_session.scoring_summary
    assert len(mock_session.players) > 400


def test_pre_draft_snapshot_waits_for_the_draft_order(mock_session, recording):
    view = view_at(mock_session, recording, 0)
    assert view.state.settings.status == "pre_draft"
    assert view.state.my_slot is None
    assert "draft order not set" in view.shortlist.unavailable
    # ...and --slot lets you plan anyway.
    with_slot = view_at(mock_session, recording, 0, my_slot=8)
    assert with_slot.shortlist.candidates
    assert with_slot.shortlist.decision_pick_no == 8


def test_my_turn_snapshot(mock_session, recording):
    view = view_at(mock_session, recording, 2)  # 7 picks in, slot 8 is up
    state = view.state
    assert state.my_slot == 8 and state.my_roster_id == 7
    assert state.current_pick_no == 8 and state.is_my_turn
    assert state.on_clock_name == "Manager 8"
    assert view.shortlist.on_the_clock and view.shortlist.candidates
    assert len(state.available) + len(state.picks) > 400


def test_traded_pick_is_attributed_to_its_new_owner(mock_session, recording):
    """Slot 5's round-3 pick (#29) was traded to slot 11 in this recording."""
    snapshot = recording.snapshots[4]  # 28 picks in: #29 is on the clock
    view = compute_view(mock_session, snapshot)
    overrides = traded_overrides(parse_traded_picks(snapshot.traded_picks), view.state.settings.season)
    slot_11_roster = view.state.settings.slot_to_roster_id[11]

    assert view.state.current_pick_no == 29
    assert owner_of_pick(29, view.state.settings, overrides) == slot_11_roster
    assert view.state.on_clock_roster_id == slot_11_roster
    assert view.state.on_clock_via == "Manager 5"  # original owner shown for context

    made = next(p for p in view_at(mock_session, recording, 5).state.picks if p.pick_no == 29)
    assert made.roster_id == slot_11_roster


def test_mid_draft_roster_and_shortlist(mock_session, recording):
    view = view_at(mock_session, recording, 5)  # 30 picks in
    assert len(view.state.my_picks) == 2
    assert [s.slot for s in view.fill.slots if s.filled]
    shortlist = view.shortlist
    assert shortlist.decision_pick_no == 32
    assert 1 <= len(shortlist.candidates) <= 10
    assert all(c.player.team for c in shortlist.candidates)  # no teamless stale records
    assert all(c.player.position not in ("K", "DEF") for c in shortlist.candidates)
    assert view.recent and len(view.recent) <= 10  # positional runs need a real window


def test_late_rounds_surface_kickers_and_defenses(mock_session, recording):
    view = view_at(mock_session, recording, 7)  # 175 picks in, round 15
    shortlist = view.shortlist
    assert shortlist.decision_pick_no == 176
    surfaced = {c.player.position for c in shortlist.candidates}
    assert "K" in surfaced  # the last open starting slots are always represented
    assert shortlist.defense_options and all(p.search_rank is None for p in shortlist.defense_options)
    assert "DEF" not in shortlist.blocked


def test_final_snapshot_is_complete(mock_session, recording):
    view = view_at(mock_session, recording, 9)
    assert view.state.is_complete
    assert view.shortlist.unavailable == "The draft is complete."
    assert len(view.state.picks) == 192


def test_replay_source_serves_snapshots_in_order_then_finishes(recording):
    source = ReplaySource(recording.snapshots)
    seen = [source.poll() for _ in recording.snapshots]
    assert [s.fetched_at_ms for s in seen] == [s.fetched_at_ms for s in recording.snapshots]
    assert source.finished
    assert source.poll() is seen[-1]  # stays on the last frame


def test_scoring_summary_from_real_league(real_draft):
    """Sleeper sends float32 values (0.1 arrives as 0.10000000149011612)."""
    summary = summarize_scoring(real_draft["league"]["scoring_settings"], "ppr")
    assert summary.startswith("PPR (1/reception)")
    assert "6-pt passing TD" in summary
    assert "1 pt/25 passing yds" in summary
    assert "rushing" not in summary  # standard rushing yardage isn't worth a mention


def test_scoring_summary_mentions_unusual_rules():
    summary = summarize_scoring({"rec": 0.5, "bonus_rec_te": 0.5, "pass_td": 4, "rush_yd": 0.2}, None)
    assert "Half PPR" in summary and "TE premium +0.5" in summary and "1 pt/5 rushing yds" in summary


def test_scoring_summary_without_a_league():
    assert summarize_scoring(None, "half_ppr") == "Half PPR"


@pytest.mark.parametrize("width", [80, 100, 160])
def test_dashboard_renders_every_snapshot(mock_session, recording, width):
    """Every recorded frame must render at every terminal width without blowing up."""
    from draftbot import ui

    console = Console(width=width, height=60, record=True, file=open("/dev/null", "w"))
    for index in range(len(recording.snapshots)):
        view = view_at(mock_session, recording, index, my_slot=8)
        console.print(ui.dashboard(view, now_ms=view.fetched_at_ms, width=width))
    output = console.export_text()
    assert "draftbot" in output
    assert "Shortlist" in output
    assert "Your roster" in output
    assert max(len(line) for line in output.splitlines()) <= width
    console.file.close()


# -- CLI ------------------------------------------------------------------------------


def test_cli_replay_single_snapshot(capsys):
    assert cli.main(["replay", str(RECORDING), "--snapshot", "2"]) == 0
    output = capsys.readouterr().out
    assert "Shortlist" in output
    assert "paste into Claude" in output
    assert "after NFL Week 1" in output  # the context block made it to stdout


def test_cli_replay_runs_the_whole_recording(monkeypatch, capsys):
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    assert cli.main(["replay", str(RECORDING), "--delay", "0", "--slot", "8"]) == 0
    assert "Draft complete" in capsys.readouterr().out


def test_pre_draft_does_not_shout_your_pick(mock_session, recording):
    """Pick 1 is nominally on the clock for the whole pre-draft window; don't cry wolf."""
    from draftbot import ui

    view = view_at(mock_session, recording, 0, my_slot=1)
    assert view.state.is_my_turn  # the board fact is true...
    console = Console(width=120, height=20, record=True, file=open("/dev/null", "w"))
    console.print(ui.header_panel(view, view.fetched_at_ms))
    header = console.export_text()
    assert "YOUR PICK" not in header  # ...but the alarm waits for the draft to start
    assert "not started" in header
    console.file.close()

    live = view_at(mock_session, recording, 2, my_slot=8)
    console = Console(width=120, height=20, record=True, file=open("/dev/null", "w"))
    console.print(ui.header_panel(live, live.fetched_at_ms))
    assert "YOUR PICK" in console.export_text()
    console.file.close()


def test_watch_loop_retries_a_malformed_draft_payload(mock_session, recording):
    """One garbled /draft response is a bad poll, not a bad draft."""

    class Flaky:
        def __init__(self):
            self.calls = 0
            self.finished = False

        def poll(self):
            self.calls += 1
            if self.calls == 1:
                return Snapshot({"draft_id": "d1", "error": "hiccup"}, [], [], 0)
            return recording.snapshots[2]

    source = Flaky()
    console = Console(width=200, height=60, file=open("/dev/null", "w"))
    cli._run_loop(console, mock_session, source, interval=0, sleep=lambda _: None, max_frames=2)
    assert source.calls >= 2
    console.file.close()


def test_watch_loop_survives_an_unexpected_error(mock_session, recording, capsys):
    """One bad poll must not end a live draft session."""

    class FlakySource:
        def __init__(self):
            self.calls = 0
            self.finished = False

        def poll(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("kaboom")
            return recording.snapshots[2]

    source = FlakySource()
    console = Console(width=200, height=60, file=open("/dev/null", "w"))
    cli._run_loop(console, mock_session, source, interval=0, sleep=lambda _: None, max_frames=2)
    assert source.calls >= 2  # it kept polling after the failure
    console.file.close()


def test_cli_replay_rejects_a_bad_snapshot_index(capsys):
    assert cli.main(["replay", str(RECORDING), "--snapshot", "99"]) == cli.EXIT_ERROR
    assert "--snapshot must be" in capsys.readouterr().out


def test_cli_rejects_a_file_that_is_not_a_recording(tmp_path, capsys):
    bogus = tmp_path / "nope.json"
    bogus.write_text(json.dumps({"hello": "world"}))
    assert cli.main(["replay", str(bogus)]) == cli.EXIT_ERROR
    assert "recording (got None)" in capsys.readouterr().out


def test_cli_config_error_exits_two(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("SLEEPER_USERNAME", raising=False)
    monkeypatch.delenv("SLEEPER_LEAGUE_ID", raising=False)
    monkeypatch.delenv("DRAFT_ID", raising=False)
    assert cli.main(["--env-file", str(tmp_path / "missing.env"), "doctor"]) == cli.EXIT_CONFIG
    assert "SLEEPER_USERNAME" in capsys.readouterr().out


def test_user_flag_overrides_the_configured_username(tmp_path, monkeypatch):
    """One checkout, two managers: --user swaps who the board is read for."""
    env = tmp_path / ".env"
    env.write_text("SLEEPER_USERNAME=manager_one\nSLEEPER_LEAGUE_ID=123456789012345678\n")
    for name in ("SLEEPER_USERNAME", "SLEEPER_LEAGUE_ID", "DRAFT_ID"):
        monkeypatch.delenv(name, raising=False)

    overridden = cli._parse_args(["--env-file", str(env), "shortlist", "--user", "manager_two"])
    assert cli._config_for(overridden).sleeper_username == "manager_two"

    default = cli._parse_args(["--env-file", str(env), "watch"])
    assert cli._config_for(default).sleeper_username == "manager_one"
    # Everything else still comes from the file.
    assert cli._config_for(overridden).league_id == "123456789012345678"


@pytest.mark.parametrize("height", [24, 30, 45])
def test_short_terminal_never_drops_a_candidate_silently(mock_session, recording, height):
    """A candidate the terminal ate looks exactly like one the heuristic rejected."""
    from draftbot import ui

    view = view_at(mock_session, recording, 5)
    names = [c.player.name for c in view.shortlist.candidates]
    console = Console(width=100, height=height, record=True, file=open("/dev/null", "w"))
    console.print(ui.dashboard(view, now_ms=view.fetched_at_ms, width=100, height=height))
    text = console.export_text()
    console.file.close()

    missing = [n for n in names if n.split()[-1][:6] not in text]
    if missing:
        assert "more —" in text, "rows were cropped with no indication"
    # A wide terminal fits the whole shortlist at any of these heights.
    wide = Console(width=140, height=height, record=True, file=open("/dev/null", "w"))
    wide.print(ui.dashboard(view, now_ms=view.fetched_at_ms, width=140, height=height))
    wide_text = wide.export_text()
    wide.file.close()
    assert not [n for n in names if n.split()[-1][:6] not in wide_text]
