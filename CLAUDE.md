# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync                                          # install deps into .venv
uv run pytest                                    # full suite, offline, ~1.5s
uv run pytest tests/test_board.py::test_name     # one test (or -k PATTERN)
uv run ffa draft replay tests/fixtures/replay_mock_12team.jsonl --snapshot 2   # offline smoke test of the whole pipeline
uv run ffa draft doctor                          # live: validate .env, resolve IDs, dump raw draft settings
uv run ffa waivers --league ID --player NAME     # live: free agent or on waivers, and until when
```

No linter, formatter or type checker is configured. Config comes from `./.env` (see
`.env.example`); real environment variables win over the file.

`main` is protected: changes land through pull requests, and the `tests` check
(`.github/workflows/tests.yml`, pytest on Python 3.11 and 3.14) must pass to merge. CI runs
with `--locked`, so a dependency change must ship with its updated `uv.lock`.

## Privacy

The repo is public. Never put the user's real league names, league or draft IDs, Sleeper
usernames, team names, or real transactions into files, commit messages, or PR/issue text;
write "a real league" or "the `.env` league" instead. Check before posting: editing a PR
description afterwards still leaves the original in GitHub's public edit history. If a
local check blocks a commit or `gh` command for a sensitive term, rewrite the text rather
than working around the check.

## Layout

The CLI is `ffa <tool> <command>`. There are two tools: `ffa draft` (a live Sleeper
snake-draft assistant) and `ffa waivers` (free agent or on waivers, right now).

- `ffa/` top level: code shared across tools — `sleeper.py` (API client), `espn.py` (NFL
  kickoff times, which Sleeper doesn't publish), `config.py`, `models.py`.
- `ffa/draft/` and `ffa/waivers/`: each tool's logic and rendering.
- `ffa/cli.py`: the single entry point. Each tool is a subparser; the thin command functions
  for both tools live here, with shared helpers like `_config_for` at the bottom.
- `docs/draft-spec.md`: the original design doc and source of truth for the draft tool. It
  predates the `draftbot` → `ffa` rename, and research has since moved from per-player to
  per-shortlist (see below).

## Draft pipeline

Every command (`watch`, `shortlist`, `recommend`, `replay`) runs the same pipeline:

1. `open_session()` (`draft/session.py`) resolves user, league and draft IDs once and loads
   the player map and roster requirements into a `Session`.
2. Each poll, a source yields a `Snapshot` (raw draft, picks, traded picks, fetch time).
   `LiveSource` hits Sleeper; `ReplaySource` reads a recording. This is why `replay` and the
   tests exercise the real code path with no network.
3. `compute_view(session, snapshot, ...)` rebuilds everything into a `DraftView`: board state
   (`board.py`), roster fill (`needs.py`), and the ranked shortlist (`shortlist.py`, scored
   with `scarcity.py`). State is rebuilt from the full `/picks` response every poll, never
   diffed. `board.py` is pure functions of the payloads.
4. `ui.py` renders a `DraftView` and never fetches. `research.py` consumes the same view.

Research (`draft/research.py`) is one web-search-backed Anthropic call per shortlist, keyed
by `shortlist_key(view)`. `ResearchWorker` prefetches on a thread pool during other
managers' picks so the result is warm when the user is on the clock. It is an enhancement,
never a dependency: every failure path falls back to the heuristic shortlist, `recommend`
degrades after 5s, and `RESEARCH_BUDGET` is a hard stop. The prompt template is
`draft/prompts/research.md`. The web search tool version depends on the model: when adding
a model, update `WEB_SEARCH_MODERN_MODELS` in `config.py`.

## Waivers

An unrostered player is not necessarily a free agent. Before telling the user a player can
be added, run `ffa waivers --player NAME` (add `--league ID` for a league other than the
`.env` one). `ffa/waivers/status.py` applies three rules, clearing at whichever ends last:
the after-game lock (from his team's kickoff until the league's clear day, 12:05 AM Pacific),
the post-drop hold (`waiver_clear_days` less an hour), and the week-closed lock (once the
week's last game has kicked off, everyone is on waivers until the clear, teams on bye
included). Its docstring has the details and how they were verified. Only
`waiver_day_of_week: 2` has been checked against real claims; custom daily waivers raise
`UnsupportedWaivers` instead of guessing. Rule 3 exists because rules 1 and 2 were verified
only against adds that happened, which cannot catch a player wrongly reported as a free
agent: check any new rule in that direction too.

## Constraints and gotchas

- The Sleeper API is read-only and unauthenticated. The tool advises; it can never pick.
  Never poll in a tight loop (`MIN_POLL_INTERVAL_SECONDS`), and don't use Sleeper's
  undocumented websocket. Per-endpoint cache rules are in the `sleeper.py` docstring.
- Sleeper payloads are loosely shaped and often missing fields: read with `.get()`, never
  bracket access. Team defenses use the team abbreviation (`"DET"`) as `player_id`.
- `search_rank` is Sleeper's search ordering, used as an ADP proxy. It is not a projection
  and must never be shown as one. It also ranks retired players, so pool filters matter.
- Tunable shortlist weights are named constants at the top of `draft/scarcity.py`.
- Recordings are JSON Lines (header, then snapshots) tagged `"draftbot-recording/1"`. Keep
  that tag: existing recordings depend on it.
- `cache/` (gitignored) holds the player map, logs (`ffa.log`) and real draft recordings
  containing real Sleeper usernames and league IDs. Scrub them before turning one into a
  test fixture.

## Tests

Tests never touch the network: Sleeper calls go through `httpx.MockTransport`, and
`ResearchClient` takes an injected fake `client=`. `tests/helpers.py` builds hand-made
payloads (`slot_to_roster_id` maps slot N to roster N+100 on purpose, to catch slot/roster
mixups). Fixtures in `tests/fixtures/`:

- `sleeper_friends_2018/`: a real completed 12-team draft; snake math is checked against
  all 180 picks.
- `replay_mock_12team.jsonl`: a synthetic draft over real player data. Regenerate with
  `python tests/fixtures/build_synthetic_replay.py`, which computes pick order on its own
  (it deliberately doesn't import `ffa.draft.board`) so it stays an independent check.
- `players_nfl.json`: a trimmed `/players/nfl` response, produced by `trim_players.py`.
