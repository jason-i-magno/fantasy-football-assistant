# fantasy-football-assistant

Terminal tools for Sleeper fantasy football leagues, run as `ffa <tool> <command>`. There is
one tool so far, `ffa draft`, a live snake-draft assistant; in-season tools will sit alongside
it.

## Setup

```bash
uv sync
cp .env.example .env   # fill in SLEEPER_USERNAME and SLEEPER_LEAGUE_ID (or DRAFT_ID)
```

## Draft assistant

A terminal sidekick for a live Sleeper snake draft. It polls the draft board, works out
whose turn it is and what is left, and ranks a shortlist for your next pick by blending
Sleeper's `search_rank` with your roster needs and positional scarcity. It never submits
picks: the Sleeper API is read-only, so you still click the button yourself.

`docs/draft-spec.md` is the design doc and the source of truth.

**Status:** M1-M3 are built and verified against a live Sleeper draft. M4 (web-search-backed
research) and the prefetch half of M5 are built; research runs on background threads during
other managers' picks so the recommendation is warm before the clock reaches you.

### Commands

```bash
uv run ffa draft doctor          # validate config, resolve IDs, dump raw draft settings
uv run ffa draft watch           # live board + shortlist + recommendation, polls continuously
uv run ffa draft shortlist       # one-shot: ranked candidates + a block to paste into Claude
uv run ffa draft recommend       # one-shot: researched pick, degrades to shortlist after 5s
uv run ffa draft replay FILE     # replay a recording offline, no network
```

Useful flags:

- `--user NAME` (`doctor`, `watch`, `shortlist`, `recommend`) — run as a different Sleeper
  username without touching `.env`. Two managers in the same league can each run their own
  `watch` from one checkout; give them separate `--record` paths.
- `--slot N` (`watch`, `shortlist`, `replay`) — read the board as the manager in draft slot
  `N`. Use it before the commissioner sets the draft order, in mock drafts, or from a
  co-owner account that isn't in `draft_order`.
- `--record PATH` (`watch`) — append every raw API snapshot to a JSON Lines recording.
- `--snapshot N` (`replay`) — render one recorded frame instead of playing the whole draft.

### Before draft day

1. `ffa draft doctor` — confirms the IDs resolve, prints the raw `settings` dict, and warns
   about any draft setting the pick math doesn't understand.
2. Start a free Sleeper mock draft, put its ID in `DRAFT_ID`, and run
   `ffa draft watch --record cache/mock1.jsonl`. Check the board against the Sleeper app:
   whose turn it is, the countdown, your upcoming picks.
3. Keep the recording. `ffa draft replay cache/mock1.jsonl` re-runs the whole pipeline
   offline, and dropping it in `tests/fixtures/` makes the test suite exercise your real
   league's roster settings.

### How the shortlist is built

1. **Available pool** — everyone not drafted, active, on an NFL team, with a sane
   `search_rank`. Sleeper's `search_rank` is a *search* ordering, not an ADP list: it still
   ranks retired players (Tom Brady at 74), which is why the team filter matters.
2. **Needs** — `roster_positions` parsed into concrete starters, flex slots and bench.
   Guardrails: no K/DEF before the final two rounds, QB capped at 1 (2 in superflex, more if
   the league starts more), TE at 2, and once your remaining picks only cover your open
   starting slots, every pick has to fill one.
3. **Scarcity** — for each position, the best player available at your pick versus the best
   likely left at your following pick, simulating other managers taking the top of the board
   by `search_rank`.
4. **Score** — an adjusted rank blending those three terms. The weights are named constants
   at the top of `ffa/draft/scarcity.py`; tune them between drafts.

Team defenses are a special case: Sleeper publishes no `search_rank` for them at all, so
they are listed unranked rather than given a made-up ranking.

### Research

With `ANTHROPIC_API_KEY` set, `watch` researches each shortlist in the background as soon as
it changes — during other managers' picks, so the answer is already there when you're on the
clock. One web-search-backed call per shortlist, capped by `RESEARCH_BUDGET` for the session.
The prompt lives in `ffa/draft/prompts/research.md`, weights actual Week 1 usage above
preseason projections, and demands strict JSON that is parsed defensively.

The research layer is an enhancement, never a dependency: if the API fails, the budget runs
out, or a call is slow, you get the heuristic shortlist with a note saying so. `recommend`
waits 5 seconds and then degrades — pass `--wait 60` to wait longer when you aren't on the
clock.

## Tests

```bash
uv run pytest
```

Fixtures in `tests/fixtures/`:

- `sleeper_friends_2018/` — raw payloads recorded from a real, completed 12-team Sleeper
  draft. The snake math is checked against all 180 of its picks.
- `players_nfl.json` — a trimmed real `/players/nfl` response.
- `replay_mock_12team.jsonl` — a synthetic in-progress draft over that real player map,
  covering pre-draft, the turn, a traded pick, the late rounds, and a completed board.
  Regenerate with `python tests/fixtures/build_synthetic_replay.py`.

## License

MIT — see [LICENSE](LICENSE).
