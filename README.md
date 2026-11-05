# fantasy-football-assistant

Tools for Sleeper fantasy football leagues, run as `ffa <tool> [command]`:

- `ffa draft` — a live snake-draft assistant.
- `ffa waivers` — which unrostered players are free agents, and which are on waivers until when.
- `ffa lineup` — what to change about your lineup this week, and who to claim.
- `ffa serve` — all of those as a web app, for the phone in your hand.

## Setup

```bash
uv sync
cp .env.example .env   # fill in SLEEPER_USERNAME and SLEEPER_LEAGUE_ID (or DRAFT_ID)
```

### More than one league

Most managers are in several. List them in `SLEEPER_LEAGUE_IDS`, default first:

```bash
SLEEPER_LEAGUE_ID=11111111111111111       # the default
SLEEPER_LEAGUE_IDS=11111111111111111,2222222222222222
```

Then refer to them the way you say them rather than by nineteen digits — `--league` takes a
name, matched case-insensitively on exact, prefix, then substring, and refuses an ambiguous
one instead of guessing:

```bash
uv run ffa lineup --league dynasty
uv run ffa waivers --league "work league" --pos RB
```

On the phone they appear as chips under the tabs, shared by the Lineup and Waivers tabs so
the two can never be looking at different leagues. The bar only appears when you have more
than one.

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

## Waivers

```bash
uv run ffa waivers                       # top unrostered players: free agent, or on waivers until when
uv run ffa waivers --pos RB --pos WR     # only these positions
uv run ffa waivers --player "Pitts"      # look players up by name, rostered or not
uv run ffa waivers --league dynasty --json  # another league, by name or ID; JSON for scripts
```

Sleeper's API has no waiver-status field, and an unrostered player isn't necessarily a free
agent. With custom daily waivers off, three rules put him on waivers, and he clears at
whichever ends last:

1. **After-game lock** — once his NFL team kicks off, he's on waivers until the league's clear
   day at 12:05 AM Pacific (`waiver_day_of_week`; "Tue After Day" clears Wednesday). Teams on
   bye aren't locked.
2. **Post-drop hold** — a dropped player stays on waivers for `waiver_clear_days` days less an
   hour (Sleeper's "2 days" is 47 hours), and claims on him process when the hold ends.
3. **Week-closed lock** — once the last game of an NFL week has kicked off, *everyone*
   unrostered is on waivers until the clear, teams on bye included. Rule 1 keys the lock to
   the player's own team, so without this one a bye-week player looks addable all week; real
   claim records say he isn't.

Rules 1 and 2 were checked against a season of real transactions, and rule 3 against the
bye-week claims that blind spot hid. Kickoff times come from ESPN's
public scoreboard API, since Sleeper doesn't publish them. Leagues using custom daily waivers
are refused rather than guessed at. Times print in your machine's timezone.

## Lineup

```bash
uv run ffa lineup                  # this week: what to start, who to add, who to drop
uv run ffa lineup --league dynasty # another league, by name or by ID
uv run ffa lineup --adds 10        # rank more waiver candidates
uv run ffa lineup --json
```

Answers the question you actually have a couple of times a week: *given what I own, what
should I change before kickoff?*

**Which week.** The one you can still change — the earliest week with a game still to kick
off. That keeps it on the coming Sunday all the way through that Sunday's late games and
rolls over only once the week's last game has started. It is not Sleeper's `nfl_state.week`,
which flips on its own schedule. The report always names the week it used.

**Where the numbers come from.** Two independent sources, neither used as published:

- **Sleeper** serves RotoWire's projections, scored under standard PPR.
- **ESPN** serves its own, scored under ESPN's default league.

Both are re-scored from raw stats with *your* league's `scoring_settings`, so what you see
is points in your league rather than generic ones. That scorer is checked against reality
rather than trusted: it reproduces Sleeper's own official `players_points` exactly — 625 of
625 player-weeks across four completed weeks of the `.env` league, kickers and team defenses
included. Run that check again after touching the scoring code.

Two sources rather than one because a single source cannot be checked. When they disagree by
five points or more the player is flagged rather than quietly averaged. Kickers come from
Sleeper alone: ESPN reports made field goals under 40 yards as one bucket where most leagues
score three separate tiers, and there is no honest way to split it.

**Every number is lineup impact.** Not raw points — what a change does to the best lineup you
can actually field. Points on the bench are worth zero, so a good receiver behind two better
receivers is worth nothing this week and the tool says so instead of recommending him. The
lineup itself is solved exactly, as a maximum-weight assignment, which matters more than it
sounds: a greedy fill spends a flex slot before it has seen the tight end, and the obvious
shortcut of fixing how many start at each position is wrong as soon as one player is eligible
at two of them.

**Adds are asked twice**, because "who helps me on Sunday" and "who should I own" have
different answers:

- **Add this week** values everyone at this week's projection, which is what makes a
  streaming defense the right call when yours is on a bye.
- **Worth owning** values everyone at his per-game pace, which is what surfaces a back who
  took over a backfield on Sunday and whose projection has not caught up — and what stops a
  backup quarterback with one big week from looking like a pickup.

**Two horizons, labelled as what they are.** Every player on the card carries a *rest of
season* and a *playoffs* figure. Both are game-by-game projections of weeks still to be
played, blended from both sources and re-scored in your league's settings, and they answer
different questions:

- **Rest of season** covers every week from the one you are setting through your league's
  final week — not week 18, which almost no league plays.
- **Playoffs** covers only the weeks your league actually plays its playoffs in, derived
  from `playoff_week_start` and the bracket size rather than assumed. A player can be strong
  in one window and ordinary in the other, because the schedule is different; the point of
  showing both is to see when they disagree.

Neither is Sleeper's *season* projection. That row is written before the season and never
revised, so a player who has since taken over a backfield still reads as the committee back
he was in August — one real case was nine points a game on the season row against fifteen on
the same provider's weekly rows. It is a last resort only, for a player no feed projects in
any week still to come, where the alternative is calling him worthless.

It costs almost nothing: one ESPN request already carries every remaining week, so both
windows come out of one response, and only Sleeper is asked per week.

On a full roster every add names its drop, chosen on this week's lineup first and then, among
drops this week cannot tell apart, on what the player is worth from here. Judged on one week
alone, every starter on a bye projects zero and looks free to drop, which is how you trade a
season-long starter for a one-week streamer.

"From here" counts a fantasy playoff week as two regular-season weeks, so the weighting
tightens on its own as the season runs out — half the weight on the playoffs in week 9, six
sevenths of it by week 14 — and the same number ranks the watchlist, because a drop and an
add are the same call pointed in opposite directions.
Whether you can actually make the claim comes from the same rules as `ffa waivers`: free
agent now, or on waivers until a stated time.

## Phone app

```bash
uv run ffa serve                 # prints the URL to open on your phone, same wi-fi
uv run ffa serve --token SECRET  # require a token (or set FFA_WEB_TOKEN in .env)
uv run ffa serve --replay tests/fixtures/replay_mock_12team.jsonl   # try it with no draft on
```

`ffa serve` puts all three tools behind a small web app: this week's lineup call on one tab,
the waiver list on the second, and the live draft board on the third. It opens on Lineup,
since a draft happens on a handful of days a year and a lineup every week. It binds to every
interface so the phone can reach it, and prints the LAN URL to type in.

- **The same pipeline.** Each poll runs `compute_view` exactly as `watch` does, and the page
  renders that `DraftView` as JSON — the phone and the terminal can't disagree, because they
  are the same numbers.
- **One poll serves every screen.** The board is fetched at `POLL_INTERVAL_SECONDS` and
  shared; five open tabs are still one request, and nothing is polled while no screen is
  open. Waiver reports are rebuilt at most once a minute and lineup advice every ten,
  each from a fresh client so rosters and transactions don't go stale on a server that
  stays up for days.
- **Read-only, like everything else here.** Sleeper's API can't be written to and the app has
  no route that changes anything, so the worst someone on your network can do is read your
  league. If that matters, `--token SECRET` locks it: open `http://host:8765/?token=SECRET`
  once and the browser keeps the token for later requests.
- **Research** behaves as it does in `watch`: prefetched in the background during other
  managers' picks, with the card saying where it has got to, and the heuristic shortlist
  standing on its own whenever research is off or slow.
- **Copy context for Claude** hands you the same markdown block `ffa draft shortlist` prints,
  to paste into the Claude app on your phone.

No new dependencies: a stdlib WSGI server, three static files, no build step. The tests drive
it in-process through httpx's WSGI transport, so they stay offline like the rest of the suite.

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
