# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync                                          # install deps into .venv
uv run pytest                                    # full suite, offline, ~1.5s
uv run pytest tests/test_board.py::test_name     # one test (or -k PATTERN)
uv run ffa draft replay tests/fixtures/replay_mock_12team.jsonl --snapshot 2   # offline smoke test of the whole pipeline
uv run ffa draft doctor                          # live: validate .env, resolve IDs, dump raw draft settings
```

No linter, formatter or type checker is configured. Config comes from `./.env` (see
`.env.example`); real environment variables win over the file.

## Layout

The CLI is `ffa <tool> <command>`. `ffa draft` (a live Sleeper snake-draft assistant) is the
only tool so far; the project is meant to grow in-season tools alongside it.

- `ffa/` top level: code shared across tools — `sleeper.py` (API client), `config.py`, `models.py`.
- `ffa/draft/`: everything draft-specific.
- `ffa/cli.py`: the single entry point. Each tool is a subparser group; the draft commands
  still live in this file. Split shared plumbing out when a second tool lands.
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
- The repo is public. `cache/` (gitignored) holds the player map, logs (`ffa.log`) and real
  draft recordings containing real Sleeper usernames and league IDs. Scrub them before
  turning one into a test fixture.

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
