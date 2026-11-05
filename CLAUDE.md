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
uv run ffa lineup                                # live: start/sit and waiver calls for the open week
uv run ffa lineup --league NAME                  # another configured league, by name
uv run ffa serve --replay tests/fixtures/replay_mock_12team.jsonl   # offline smoke test of the web app
uv run ffa serve                                 # live: the web app, on every interface
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

The CLI is `ffa <tool> [command]`. There are four tools: `ffa draft` (a live Sleeper
snake-draft assistant), `ffa waivers` (free agent or on waivers, right now), `ffa lineup`
(start/sit and waiver calls for the week you can still change), and `ffa serve` (all of
those as a web app for a phone on the same network).

- `ffa/` top level: code shared across tools — `sleeper.py` (API client), `espn.py` (NFL
  kickoff times, which Sleeper doesn't publish), `config.py`, `models.py`.
- `ffa/draft/`, `ffa/waivers/` and `ffa/lineup/`: each tool's logic and rendering.
- `ffa/scoring.py`, `ffa/projections.py`, `ffa/espn_fantasy.py`, `ffa/roster.py`: the
  projection layer, shared because more than one tool needs points and slot rules.
- `ffa/leagues.py`: the configured leagues (`SLEEPER_LEAGUE_IDS`) and name lookup, so
  `--league <name>` works and the phone shows chips instead of nineteen-digit ids. Names come
  from Sleeper, never from config: a second place to keep a name is a second place for it
  to be wrong.
- `ffa/web/`: the web app (see below).
- `ffa/cli.py`: the single entry point. Each tool is a subparser; the thin command functions
  for all three live here, with shared helpers like `_config_for` at the bottom.
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

## Lineup

`ffa lineup` answers "what should I change before kickoff". `source.py` fetches, `advice.py`
is pure and does the thinking, `optimal.py` solves, `ui.py` renders. The rules that are easy
to get wrong, in the order they bite:

- **The target week is the earliest with a game still to kick off**, not Sleeper's
  `nfl_state.week` (which read 9 while the app still showed week 8) and not "the first week
  with nothing started" (which would skip to next week halfway through a Sunday). Say which
  week is being used.
- **Re-score everything.** Sleeper's `pts_ppr` is standard PPR and ESPN's `appliedTotal` is
  ESPN's default league; both are re-scored from raw stats with the league's own
  `scoring_settings` by `ffa/scoring.py`. That scorer reproduced all 625 of the `.env`
  league's official `players_points` across four completed weeks — re-run that check
  against a finished week after changing it.
- **Value is lineup impact, never raw points.** Every number reported is a difference
  between two solved lineups, so a player behind two better ones is worth zero. `optimal.py`
  solves it exactly (maximum-weight assignment); the tempting decomposition by position
  counts is wrong whenever a player is eligible at two positions, and lost up to 12 points
  on 66 of 400 random rosters before it was replaced. `tests/test_optimal.py` keeps a
  brute-force cross-check.
- **A diff of starting *sets*, not of seats.** A lineup scores the same however its eligible
  players are spread across eligible slots, so a seat-wise comparison reports confident
  swaps worth nothing on a week whose real gain is zero.
- **Drops are judged on the rest of the season.** On this week alone every bye-week starter
  projects zero and looks free to drop.
- **Never show the season row as a rest-of-season number.** Sleeper's season row is a
  preseason projection that is never revised: against live data one back's row had him at
  172 carries for the year while the same provider's weekly rows had him at 14.5 a game,
  nine points of season row versus fifteen of fresh rate. Both displayed figures are
  therefore weekly blends over weeks still to be played -- `rest_per_game` over week-now to
  the league's final week, `playoff_per_game` over `projections.playoff_weeks` -- and are
  labelled "rest of season" and "playoffs" so nobody reads one as the other. The season row
  survives in exactly one place, `advice._keep_value`, as the floor under a drop, where
  being slow to move is the right property. One ESPN request carries every remaining week,
  so both windows come out of one response; only Sleeper costs a request per week, and the
  rest window is fetched once and sliced for the playoff one.
- **`role_change` is measured over the window, not over one week.** A hard matchup is not a
  demotion, and a player on a bye projects zero -- which made the one check that can see a
  changed role blind in precisely the week the stale row was the only number on the card.
- **Byes come from the kickoff schedule.** Both feeds publish a row for a team on bye
  (Sleeper's empty, ESPN's with `appliedTotal` 0), so "no projection" finds nothing. A
  player with no NFL team at all is a different thing and must not be called a bye.

## Web app

`ffa serve` is a WSGI app with no framework and no new dependency: `ffa/web/app.py` routes,
`state.py` holds the shared feeds, `payload.py` turns a `DraftView` into JSON, and
`static/` is the whole UI (three files, vanilla JS, no build step — they ship inside the
package, like `draft/prompts/research.md`). Three tabs: Lineup (the default), Waivers, Draft.

- Nothing fantasy-related lives in the browser. `payload.py` renders the same `DraftView`
  `ui.py` draws, so a fix to the pipeline reaches both surfaces at once. If the UI needs a
  number, add it to the payload rather than deriving it in JS.
- One poll serves every screen. `DraftFeed` holds the newest view behind a lock and refuses
  to refetch inside the poll interval, so N phones are still one Sleeper cadence, and it
  polls on request rather than on a timer (no screen open, no traffic). Its error policy
  mirrors `cli._run_loop`, so fix both together.
- `WaiversFeed` and `LineupFeed` build each report with a *fresh* `SleeperClient`: that
  client caches rosters, transactions and the NFL state for its own lifetime, which is
  right for a one-shot command and wrong for a server that stays up for days. `LineupFeed`
  caches for ten minutes and builds synchronously: a cold build measured about 4s, most
  of it ESPN, which is a better trade than a worker thread and a progress protocol.
- The league picker is one control in the header, shared by the lineup and waivers tabs,
  because two controls for one choice is one of them being wrong. It is hidden on the draft
  tab, which follows `DRAFT_ID` instead.
- `show()` toggles the `hidden` *attribute*, and an author `display` rule outranks the user
  agent's `[hidden] { display: none }` — a flex or grid element would stay visible. There is
  now an explicit `[hidden]` rule in `styles.css`; don't remove it.
- Read-only by construction: the app rejects anything but GET/HEAD. `--token`/`FFA_WEB_TOKEN`
  gates every route including the static files, and static serving is confined to
  `ffa/web/static` by a `is_relative_to` check — keep both when adding routes.
- Tests drive the app in-process through `httpx.WSGITransport` with a recording behind the
  draft feed and `FakeSleeper` behind the waivers feed, so `tests/test_web.py` stays offline
  and sub-second like everything else. The JS has no test of its own beyond a check that it
  only calls routes that exist; screenshot it if you change the rendering.

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
- **ESPN's projection feed has three traps**, all in the `ffa/espn_fantasy.py` docstring:
  any `filterStatsFor*` key in `X-Fantasy-Filter` silently strips the projections and
  returns only actuals; the payload carries two seasons, so `seasonId` must be pinned or you
  read last season's same-numbered week; and several stats repeat under a second id
  (41==53, 61==42, 40==24), which double-counts if both are mapped.
- Sleeper's *season* projection rows report `gp: 18.0` for every position except team
  defenses, which report `1.0` — taken at face value that values a defense at a whole
  season's points per week.
- Sleeper fills `espn_id` for under half the player map, so the ESPN join falls back to
  normalized name plus team. Strip name suffixes (Jr/III) or players are silently lost.
- Research credentials are checked in one place, `draft/research.py::build_worker`, which
  returns `(worker, reason)` and never raises; the CLI prints the reason, the web app shows
  it on the recommendation card.
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

The lineup tests are hand-made rather than recorded: `tests/test_lineup.py` builds a roster
from tuples, so no real league data is involved and each test pins one way a feed or a
shortcut misled the tool against live data.
