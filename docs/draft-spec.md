# Sleeper Draft Assistant — Build Spec

> Written when the project was `draftbot`. The package is now `ffa`, the draft modules live
> in `ffa/draft/`, and the commands are `ffa draft <command>`.

**Read this entire file before writing any code.**

Reference: the Sleeper API is documented at https://docs.sleeper.com. It is a free,
read-only HTTP API with no authentication. Base URL: `https://api.sleeper.app/v1`.

---

## 1. What we are building

A terminal tool that runs alongside a live Sleeper snake draft. It:

1. Polls the draft and tracks board state (whose turn, what's been picked, what's left).
2. Computes a shortlist of candidates for the user's next pick, ranked by a blend of
   Sleeper's `search_rank` and positional need/scarcity.
3. Runs web-search-backed research on the viable candidates via the Anthropic API and
   prints a recommended pick with a written rationale.

Two output modes:

- **`shortlist`** — prints the ranked candidates plus a formatted context block the user
  can paste into a Claude conversation for a second opinion.
- **`recommend`** — the tool does the research itself and prints its own pick and summary.

Both must work. `shortlist` is the fallback when the research layer fails or is too slow.

---

## 2. Hard constraints

These shape the architecture. Do not design around them later.

| Constraint | Consequence |
|---|---|
| Sleeper API is read-only | The tool can never submit a pick. It advises; the user clicks. |
| Stay under 1000 req/min or risk IP block | Poll `/picks` every 3s (~20 req/min). Nowhere near the cap, so don't over-engineer this, but never poll in a tight loop. |
| Pick timer is typically 60–120s | **Research must be precomputed during other managers' picks.** If research starts when it's already your turn, the tool has failed. |
| `/players/nfl` is ~5MB, "once per day at most" | Fetch once at startup, cache to disk with a date stamp, never refetch mid-draft. |
| No websocket / no push | Polling only. Sleeper has an undocumented socket; do not use it. |
| This draft happens after NFL Week 1 | Research prompts must explicitly weight actual Week 1 usage (snap share, target share, backfield splits) over preseason projections. This is the tool's main edge. |

---

## 3. Stack

- Python 3.11+
- `httpx` (HTTP, sync is fine; the concurrency need is in the research layer only)
- `rich` (live terminal UI)
- `anthropic` (official SDK)
- `pytest` for tests
- Dependency management: `uv` preferred, `pip` + `venv` acceptable

No database. JSON files on disk under `./cache/`. No web server, no frontend.

---

## 4. Project layout

```
draftbot/
  __init__.py
  config.py        # env loading, validation
  sleeper.py       # API client + disk cache
  models.py        # dataclasses: Player, Pick, DraftState, RosterSlot
  board.py         # snake math, whose-turn, availability
  needs.py         # roster construction rules, unfilled slots
  scarcity.py      # positional dropoff / value-over-next-available
  shortlist.py     # candidate generation and ranking
  research.py      # Anthropic calls, caching, prefetch worker
  ui.py            # rich rendering
  cli.py           # entrypoint
  prompts/
    research.md    # the research prompt template
cache/             # gitignored
tests/
  fixtures/        # recorded raw API responses
```

---

## 5. Configuration

Read from environment, with a `.env` file supported. Validate all of it at startup and
fail loudly with a readable message — never start a draft session with a bad config.

```
SLEEPER_USERNAME=          # required, used to resolve user_id
SLEEPER_LEAGUE_ID=         # required unless DRAFT_ID is set directly
DRAFT_ID=                  # optional override; useful for mock drafts
ANTHROPIC_API_KEY=         # required for `recommend`, optional for `shortlist`
DRAFTBOT_MODEL=            # default to a Sonnet-tier model string
POLL_INTERVAL_SECONDS=3
RESEARCH_CONCURRENCY=4
RESEARCH_BUDGET=60         # max research calls per draft session, hard stop
```

Note on `SLEEPER_USERNAME`: usernames can change over time, so resolve to `user_id` once
at startup and use the ID everywhere after that.

---

## 6. Sleeper data layer

Implement `sleeper.py` as a thin client with explicit cache policies per endpoint.

| Call | Purpose | Cache policy |
|---|---|---|
| `GET /user/<username>` | resolve `user_id` | once per run |
| `GET /league/<league_id>` | `roster_positions`, `scoring_settings`, `total_rosters` | once per run |
| `GET /league/<league_id>/users` | display names for the board | once per run |
| `GET /league/<league_id>/drafts` | resolve `draft_id` | once per run |
| `GET /draft/<draft_id>` | `settings`, `status`, `draft_order`, `slot_to_roster_id`, `last_picked` | refetch every 30s (order and status can change) |
| `GET /draft/<draft_id>/picks` | the live board | **every poll interval** |
| `GET /draft/<draft_id>/traded_picks` | pick ownership overrides | once at startup, refetch on change of pick count |
| `GET /players/nfl?active=true` | the player map | once per calendar day, on disk |

The `/players/nfl` endpoint accepts `?position=QB` and `?active=true`, individually or
combined, and filtered responses are far smaller than the full 5MB payload. Use
`?active=true` and drop anything without fantasy-relevant positions.

Error handling: retry 5xx with exponential backoff; on 429 back off hard and log a
warning; on 404 fail with a message naming the ID that wasn't found.

---

## 7. Board state and snake math

This is the highest-risk code in the project. Get it right and unit test it thoroughly.

### Pick numbering

`pick_no` is 1-indexed and global across the draft.

```python
def slot_for_pick(pick_no: int, teams: int, draft_type: str, reversal_round: int | None) -> int:
    rnd = ((pick_no - 1) // teams) + 1
    idx = ((pick_no - 1) % teams) + 1          # position within the round, 1..teams

    if draft_type == "linear":
        return idx

    is_reversed = (rnd % 2 == 0)
    if reversal_round and rnd >= reversal_round:
        is_reversed = not is_reversed          # third-round reversal flips parity onward

    return (teams - idx + 1) if is_reversed else idx
```

**Verify the reversal setting key name against the live draft object.** It is not present
in the published docs sample. On startup, log the entire raw `settings` dict from
`GET /draft/<draft_id>` so the user can eyeball it. If an unrecognized key is present,
warn rather than silently ignoring it.

### Whose turn it is

There is no "current picker" field. Derive it:

```
next_pick_no = len(picks) + 1
slot         = slot_for_pick(next_pick_no, teams, type, reversal_round)
roster_id    = draft["slot_to_roster_id"][str(slot)]
user_id      = invert(draft["draft_order"])[slot]        # draft_order maps user_id -> slot
display_name = league_users[user_id]
```

Apply `traded_picks` as an override: a traded pick record carries `season`, `round`, the
original owner's `roster_id`, and the current `owner_id`. If a record matches the round
and original roster, the pick belongs to `owner_id` instead.

### Clock

`draft["last_picked"]` is a millisecond epoch timestamp and `settings.pick_timer` is in
seconds. Deadline for the current pick is `last_picked + pick_timer * 1000`. A
`pick_timer` of `0` means no timer — handle that without dividing by zero or showing a
negative countdown.

### My upcoming picks

Compute `my_slot` once, then generate the next three `pick_no` values that belong to it.
The scarcity model needs `picks_until_my_next_turn`, and the UI should show pick numbers
two turns out so the user can plan a positional pairing.

---

## 8. Available player pool

There is no "available players" endpoint. Derive:

```
available = all_players - {pick.player_id for pick in picks}
```

Filtering rules for the pool:

- `active` is true
- `fantasy_positions` intersects `{QB, RB, WR, TE, K, DEF}`
- `search_rank` is not null and below a sanity threshold (Sleeper assigns very large
  sentinel values to irrelevant players; treat anything above ~10000 as unranked)

Note that team defenses use the team abbreviation as the player ID (`"DET"`, `"PHI"`),
not a numeric string. Handle both shapes.

`search_rank` is Sleeper's own ordering and serves as the ADP proxy throughout. It is not
a projection and should never be presented to the user as one.

---

## 9. Roster needs model

Parse `league["roster_positions"]`, which is an ordered array like:

```
["QB","RB","RB","WR","WR","TE","FLEX","FLEX","K","DEF","BN","BN","BN","BN","BN","BN"]
```

Build a requirements model from it:

- Count required starters per concrete position.
- `FLEX` accepts RB/WR/TE. `SUPER_FLEX` additionally accepts QB. `REC_FLEX` accepts WR/TE.
- `BN` count is bench depth, not a need.

Track which slots the user's existing picks have filled, filling concrete positions before
flex slots. Expose `unfilled_starter_slots` as the primary need signal.

Guardrails the shortlist must respect:

- Never surface K or DEF until the final two rounds.
- Cap QB at 1 (2 in superflex leagues) and TE at 2.
- Once starters are filled, weight bench depth toward RB and WR, which carry the most
  injury-replacement value.

---

## 10. Scarcity: value over next available

This is the core heuristic and the reason the tool is more useful than just reading a
rankings list. For each position `p`:

```
best_now(p)   = highest-ranked available player at p (lowest search_rank)
gap           = my_next_pick_no - current_pick_no
simulated     = remove the top `gap` available players by search_rank from the pool
best_then(p)  = highest-ranked player at p in `simulated`
dropoff(p)    = best_then(p).search_rank - best_now(p).search_rank
```

A large `dropoff` means the position is about to fall off a cliff and should be addressed
now. A small one means you can wait a round and take the other position first.

Final candidate score blends three terms — raw `search_rank`, need weight from §9, and
`dropoff(p)` — with weights defined as named constants at the top of `scarcity.py` so
they can be tuned between drafts.

This model is deliberately crude. `search_rank` as an ADP stand-in ignores who is actually
in the room and how they draft. That's acceptable: the heuristic layer's job is to narrow
~300 available players down to 8–12 worth thinking about. The judgment happens in §11.

---

## 11. Research layer

### Call shape

Use the `anthropic` SDK's messages API with the server-side web search tool enabled.
The tool type string is versioned and differs by model generation — newer models use a
later version than the original `web_search_20250305`. Do not hardcode it blindly; read
https://docs.claude.com/en/api/overview and the tool-use docs for the version matching the
configured model, and make it a config constant.

Cap `max_uses` per request (3–5 searches is plenty per player) to bound latency and cost.

### What to research

For each shortlisted candidate, the prompt must ask for:

1. **Current role** — depth chart position, and whether it changed in the last month.
2. **Week 1 usage** — snap share, target or carry share, goal-line work. This is the
   single most valuable input and must be weighted above preseason projections.
3. **Injury status** — current designation, and anything from Week 1.
4. **Offensive context** — quarterback situation, pace, competition for touches.
5. **Schedule** — any notable early-season or fantasy-playoff considerations.

Provide the model with: league scoring settings, roster requirements, the user's roster so
far, current round and pick, and the positional dropoff figures from §10.

Require strict JSON output — a ranked list with `player_id`, a one-line verdict, a
confidence, and a short rationale — plus a top-level `recommendation` with a two-to-three
sentence summary. Parse defensively and strip markdown fences before parsing.

Store the prompt in `prompts/research.md` as a template, not inline in Python.

### Prefetch (the part that makes it usable)

On every poll:

1. Recompute the shortlist for the user's *next* pick.
2. For any candidate not already in the research cache, enqueue a research job.
3. Run jobs on a small thread pool (`RESEARCH_CONCURRENCY`, default 4).
4. Cache results keyed by `player_id`, valid for the whole session.

By the time the user is on the clock, the shortlist is already warm and `recommend`
returns instantly. Enforce `RESEARCH_BUDGET` as a hard stop on total calls per session.

### Failure behavior

The research layer is an enhancement, never a dependency. If the API errors, times out, or
the budget is exhausted, print the heuristic shortlist with a clear note that research is
unavailable. **Never block on the clock.** A `recommend` call that can't complete within
5 seconds should degrade to `shortlist` output automatically.

---

## 12. CLI

```
draftbot doctor       # validate config, resolve IDs, dump raw draft settings, exit
draftbot watch        # live UI, polls continuously, prefetches research
draftbot shortlist    # one-shot: print candidates + pasteable context block
draftbot recommend    # one-shot: print researched recommendation
draftbot replay FILE  # run against recorded fixtures, no network
```

`doctor` should be the first thing built and the first thing run before a real draft.

---

## 13. Terminal UI (`watch`)

A `rich` live layout, refreshed each poll:

- **Header** — round/pick, whose turn, countdown, draft status
- **Left** — the user's roster so far, grouped by slot, with unfilled slots highlighted
- **Center** — shortlist table: name, pos, team, `search_rank`, dropoff, a flag for
  "likely gone by your next pick", and a research-ready indicator
- **Right** — the recommendation panel once research lands
- **Footer** — last 5 picks by other managers, positional run detection

Highlight the header loudly when it becomes the user's turn.

---

## 14. Edge cases

Every one of these is real. Handle them explicitly.

- `draft_order` is `null` until the commissioner sets or randomizes the order, often only
  minutes before the draft. `watch` must poll for it rather than crashing at startup.
- `picked_by` can be an empty string when a slot has no user attached. Prefer `roster_id`,
  derived from `slot_to_roster_id`, for identifying the user's own picks.
- Pick objects in the wild are missing fields — the docs' own example shows a pick without
  `roster_id`, `round`, or `draft_slot`. Use `.get()` everywhere, never bracket access.
- Traded picks change ownership of a given round's selection.
- `is_keeper` picks consume a pick slot; exclude those players from the pool but don't
  attribute them as in-draft selections.
- Auction drafts (`type` is neither `snake` nor `linear`) break all pick math. Detect and
  exit with a clear message rather than producing garbage.
- Draft `status` can be `paused`. Show it; don't treat the stalled clock as an error.
- Treat the `/picks` response as the source of truth on every poll — rebuild state from it
  rather than incrementally diffing, which avoids ordering and duplicate bugs.
- `last_picked` is UTC milliseconds. Don't assume local time.
- The cached player map goes stale across days; check the date stamp on load.

---

## 15. Testing

- **Unit tests for snake math** are mandatory: multiple team counts (8, 10, 12, 14), round
  boundaries, the first and last pick of each round, linear drafts, and reversal drafts.
- **Fixtures**: record raw JSON from `/picks`, `/draft/<id>`, and a filtered `/players/nfl`
  at several points in a real draft. `replay` runs the whole pipeline offline against them.
- **Needs model**: test against the actual `roster_positions` array from the user's league,
  not a generic one.
- **Live validation**: run `doctor` and then `watch` against a free Sleeper mock draft
  before the real thing. This is the only way to confirm the snake math and `draft_order`
  handling against live data. Treat it as part of the definition of done.

---

## 16. Build order

Ship in this order. Each milestone should be independently runnable.

| M | Scope | Done when |
|---|---|---|
| M1 | Config, Sleeper client, disk cache, `doctor` | `doctor` prints resolved IDs and raw draft settings |
| M2 | Board state: snake math, whose turn, picked list, my roster | `watch` shows a correct live board with no shortlist |
| M3 | Needs + scarcity + `shortlist` | Shortlist is sane in a mock draft |
| M4 | Research layer + `recommend` | Recommendation returns with cited reasoning |
| M5 | Prefetch worker + full live UI | Recommendation is warm before the clock starts |

**M1–M3 are load-bearing.** If board state is wrong, the research layer produces confident
recommendations about the wrong draft. Do not start M4 until M2 is verified against a live
mock draft.

---

## 17. Non-goals

- No writes to Sleeper. The API is read-only; there is no path to auto-drafting.
- No auction draft support.
- No multi-league concurrent monitoring.
- No projection modeling. `search_rank` plus web research is the whole value model.
- No persistent storage beyond the session cache.
