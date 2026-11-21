# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

The project runs on free-threaded CPython (`.python-version` is `3.14t`, which `uv`
downloads on its own). `requires-python` is still 3.11: the floor has not moved, and CI
tests 3.11, 3.14 and 3.14t. Delete `.python-version` to go back to a stock interpreter --
nothing depends on the free-threaded build beyond being faster on it.

```bash
uv sync                                          # install deps into .venv
uv run pytest                                    # full suite, offline, ~8s
uv run pytest tests/test_board.py::test_name     # one test (or -k PATTERN)
uv run ffa draft replay tests/fixtures/replay_mock_12team.jsonl --snapshot 2   # offline smoke test of the whole pipeline
uv run ffa draft doctor                          # live: validate .env, resolve IDs, dump raw draft settings
uv run ffa waivers --league ID --player NAME     # live: free agent or on waivers, and until when
uv run ffa lineup                                # live: start/sit and waiver calls for the open week
uv run ffa lineup --league NAME                  # another configured league, by name
uv run ffa lineup --teams                        # live: every manager's lineup as set
uv run ffa serve --replay tests/fixtures/replay_mock_12team.jsonl   # offline smoke test of the web app
uv run ffa serve                                 # live: the web app, on every interface
```

No linter, formatter or type checker is configured. Config comes from `./.env` (see
`.env.example`); real environment variables win over the file.

`main` is protected: changes land through pull requests, and the `tests` check
(`.github/workflows/tests.yml`, pytest on 3.11, 3.14 and free-threaded 3.14t) must pass to
merge. CI runs with `--locked`, so a dependency change must ship with its updated
`uv.lock`. Auto-merge is enabled on the repository, so `gh pr merge <n> --squash --auto`
queues the merge against that check rather than waiting on it -- GitHub merges when it
goes green and does nothing if it does not.

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
  `lineup/league.py` is every *other* manager's roster, scored by the same
  `advice.player_line` so a rival's card cannot drift from your own.
- `ffa/scoring.py`, `ffa/projections.py`, `ffa/espn_fantasy.py`, `ffa/roster.py`: the
  projection layer, shared because more than one tool needs points and slot rules.
- `ffa/playoffs.py`: the playoff race (see below). Pure arithmetic over a table, a
  schedule and -- since the odds stopped being coin flips -- a mean and a spread per team.
  It never learns where those came from, which is why it is here and not in `lineup/`.
- `ffa/strength.py`: what each team scores a week and how much of that is signal (see
  below). Also pure: weekly scores in, a `Scoring` out, and a `backtest` that replays a
  season against itself.
- `ffa/fanout.py`: `gather`, which runs independent fetches at once and keeps the loop's
  promises -- keyed results, the first exception by key rather than by whichever thread
  lost, and nothing still running when it returns; and `start`, for the one fetch that
  should already be running while the caller does something else. Used wherever a build
  was waiting on requests in turn (see below).
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

- **The build is one wave, not a queue.** A cold build measured 5.07s, of which 3.5s was
  requests waiting their turn for nothing: five pages of ESPN's player feed, a scoreboard
  per week, a Sleeper projection per week still to be played, a matchup per week already
  played. Nothing in that list needs anything from anything else in it, so it all goes
  through `fanout.gather` at once -- and *fewer* requests than before (38 against 41),
  because the scoreboards the waiver report wants are handed to it rather than fetched
  again. 5.07s to 1.30s, medians of cold builds against a real league. Past that point the
  measured breakdown is: ESPN's revalidated feed 0.23s of the critical path, the Sleeper
  wave about 0.5s, and 0.5s of CPU after it -- there is no single pole left to pull.
- **Free-threading is why the default interpreter is `3.14t`.** The wave is CPU that the
  GIL used to serialise, so taking the GIL away is the largest single win left and it cost
  one file: same interpreter, same packages, `PYTHON_GIL=1` against `PYTHON_GIL=0`, three
  pairs, **1.61s against 1.17s**. The CPU total barely moves (1.65s against 1.54s), which
  is the whole story -- the same work, now on more than one core instead of queued.
  Nothing in the code changed for it. The one shared-state pattern that matters,
  `SleeperClient._once_per_run`, is check-then-set across an HTTP call, so the GIL never
  made it atomic either; the worst case was and is a duplicated fetch, never a wrong
  answer. The web app's feeds were already behind locks: eight concurrent cold requests
  return eight identical bodies from one build.
- **The build is CPU-bound, not network-bound: 1.55s wall against 1.58s of CPU.** That is
  the single most useful number here and it was measured late, after a lot of work that
  assumed the opposite. The structural fixes above were still right -- they removed waits
  that were real -- but what is left is not waiting.
- **The Sleeper wave is JSON, and the GIL serialises it.** A weekly projection looks like
  it takes 0.6s inside a build and 0.06s outside one, which looks like throttling or
  connection setup and is neither. The request the client makes carries `order_by=ppr` and
  a position filter and comes back **2.28MB, 3301 rows**; eight of those is 18MB of body
  to decompress and parse, about 0.43s of CPU, and every thread doing it holds the GIL.
  Hence: eight payloads one at a time take 0.59s wall and 0.31s CPU, eight at once take
  0.44s and 0.43s -- concurrency buys 0.15s and no more. That is also why the build is flat
  from four workers to sixteen, so `FETCH_WORKERS` is not a dial worth turning.
  **Measure the request the client actually sends.** An earlier note here blamed TLS
  handshakes, from a probe that fetched the same endpoint without those query parameters
  and got 0.08MB back -- a payload thirty times smaller, measuring latency where the build
  spends CPU.
- **Blending inside the fetch jobs is slower, not faster.** The arithmetic needs only that
  week's two feeds, so folding it into the job that fetched the week looks free -- it
  measured 0.05s *worse* in three pairs out of three. The blend has to wait for the ESPN
  feed, and eight jobs waiting on it inside a twelve-worker pool starve the fixtures and
  the report behind them. Measure before trying it again. Three things that
  each looked small and were not: the waiver report belongs *in* the wave, since nothing
  before the advice reads it; the pool needs room for the whole wave, because one slow job
  in each of six slots leaves the other sixteen queued (six at a time measured 1.51s
  against twelve at 0.52s); and each week is blended once, shared between the two windows,
  rather than once per window. Two things to keep in mind when adding a fetch here. The soft
  failure for ESPN lives inside its job (`_espn_or_reason`), because a fan-out raises the
  first exception any job raised and losing a second opinion must not lose the build. And
  the jobs are keyed `(feed, week)` so that no two of them can ask for the same cached
  response at the same time -- `SleeperClient`'s cache is check-then-set, which races into
  a wasted request rather than a wrong answer, but a wasted request is the thing being
  removed.
- **Nothing waits for the target week that does not have to.** The week is decided by the
  kickoffs, which is a round trip to ESPN's scoreboard, and almost nothing in the build
  actually needs it: the fixtures and the scored weeks come from the league's settings,
  the waiver report brings its own weeks, and both projection feeds can be asked for every
  week a kickoff could possibly land on -- `forward_weeks`, from the week before Sleeper's
  through the league's last -- and sliced once the week is known. So the scoreboards and
  ESPN's feed are both started with `fanout.start` and collected at the end, and the week
  is settled *after* the wave rather than before it. The over-fetch is one or two small
  weekly requests; waiting for it was a whole round trip, 0.29s of every build.
  Two things make that safe, and both were checked rather than assumed.
  The same request carrying a different `scoringPeriodId` returns identical rows for every
  week either one asks about, so a wider net is never a different answer. And the window
  is sliced afterwards by the real target week, which is only sound because `target_week`
  always answers with one of the weeks it was given -- a property with its own test now,
  because if it ever answered otherwise the chosen week's projections would come back
  *empty* rather than wrong.
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
- **Drops are judged on what a player is worth from here, never on this week.** On this
  week alone every bye-week starter projects zero and looks free to drop. `Horizon` is that
  judgement and is used on both sides of a move -- the floor under a drop and the ranking
  behind "worth owning" -- because a drop and an add are the same call pointed in opposite
  directions. A playoff week counts `PLAYOFF_WEEK_WEIGHT` regular-season weeks, which with
  the weight at 2.0 puts half the weight on the playoffs in week 9 and six sevenths of it by
  week 14, tightening on its own as the rest-of-season window shrinks. This week is excluded
  deliberately: a drop is chosen on this week's lineup *first* and the floor only breaks the
  ties, so the players it decides between are the ones not starting, whose points this week
  are points they will not score for you.
- **Never show the season row as a rest-of-season number.** Sleeper's season row is a
  preseason projection that is never revised: against live data one back's row had him at
  172 carries for the year while the same provider's weekly rows had him at 14.5 a game,
  nine points of season row versus fifteen of fresh rate. Both displayed figures are
  therefore weekly blends over weeks still to be played -- `rest_per_game` over week-now to
  the league's final week, `playoff_per_game` over `projections.playoff_weeks` -- and are
  labelled "rest of season" and "playoffs" so nobody reads one as the other. The season row
  is a last resort only, for a player no feed projects in any week still to be played, where
  the alternative is scoring him zero and naming him the drop. One ESPN request carries
  every remaining week,
  so both windows come out of one response; only Sleeper costs a request per week, and the
  rest window is fetched once and sliced for the playoff one.
- **One bad weekly row wrecks a window, not a week.** A feed serves the occasional
  nonsense figure -- live, a kicker's week-15 row came back at 60.5 points against 5.5 to
  5.9 in all six other weeks still to be played. In the week itself that is one visible
  number beside both feeds' own; averaged into a seven-week window it put his
  rest-of-season rate at 13.5 a game and his playoff rate at 24.0, and ranked a kicker
  averaging under 4 the best add on the board. Kickers are `SLEEPER_ONLY_POSITIONS`, so at
  the one position where it happened there was no second feed to disagree. `_spike_weeks`
  leaves such a week out of `window_per_game`, testing against the *highest* of the
  player's other weeks rather than the middle one, because a real role change is a step
  (every week after it is high, so nothing fires) while a bad row is alone. A median would
  throw away the first week of a demotion as corrupt. `WindowValue.excluded` carries what
  was dropped; the target week is left alone deliberately, having no other week to be
  judged against. It is logged **once per bad row, not once per window**: the playoff
  weeks are the last of the weeks still to be played, so a row dropped from the
  rest-of-season window is usually dropped from the playoff one too, and a warning that
  always came in pairs read like two findings. `average_per_game` takes a set of the
  player-weeks already announced and `load_advice` passes the same one to both windows --
  the same shape the blends themselves are shared with. Both windows still drop the row;
  only the saying is shared, and a window judging alone (`window_per_game`, with nobody to
  share with) still speaks up.
- **`role_change` is measured over the window, not over one week.** A hard matchup is not a
  demotion, and a player on a bye projects zero -- which made the one check that can see a
  changed role blind in precisely the week the stale row was the only number on the card.
- **Say when one feed alone is behind a number.** `window_per_game` carries each window's
  sources out with its mean, because a figure both feeds built and one ESPN built alone are
  identical as numbers -- and the second is the shape of a backup one feed has written up
  and the other has not. Found live: an unrostered rookie quarterback reached the watchlist
  on ESPN's word alone, with nothing on the card saying so. `Projection.forward_single_source`
  is that check and is separate from `single_source`, which is about this Sunday; the note is
  added in `player_line`, outside `_effective`, because `_effective` returns early on a bye
  and a bye-week card is all forward figures.
- **One builder for every row.** `advice.player_line` builds your lineup, your bench, every
  waiver candidate and every other manager's roster. Candidates used to be built inline and
  silently missed the label above; a second constructor is a second set of rules.
- **Another manager's card advises nothing.** `league.py` solves every roster's best
  lineup, because "he has left eleven points on his bench" is the most useful thing to know
  about an opponent on a Sunday, but it suggests no swap, add or drop for a roster you
  cannot change. It rides on the same build as your own advice: one extra cached request
  (the league's user list) and no second fetch of anything expensive.
- **A reserve slot is not a bench spot.** Sleeper's `players` is every player on a roster,
  IR and taxi squad included, and `reserve`/`taxi` name the ones outside the active roster;
  matchup entries repeat the same combined list, so this is true of every team in the
  league. Read literally it makes a player on IR look like a bench player, which went wrong
  three ways at once: he was named as the drop to make room for an add (reported from the
  phone as "it says drop him, but he is on IR so I can't do that") because he projects
  nothing and is worth least from here; he counted as occupying a roster spot, so a bench
  with a free place read as full; and he stayed in the pool the optimal lineup is solved
  from, which in the second `.env` league was live advice to start a back out of an IR slot
  over a healthy tight end -- 0.4 of the week's claimed gain resting on a move Sleeper would
  not accept without activating him first. `roster.reserved` is the fact and
  `roster.active_slots` the capacity -- reserve capacity lives in `settings.reserve_slots`
  in both `.env` leagues and in `roster_positions` in others, so counting that list as it
  comes is wrong in one direction or the other. The slot is also checked ahead of the bye in
  `_effective`: the one IR player in the `.env` league was on a bye too, and his card said
  "on bye", which is true and is not the news.
  **And he has no rate from here either.** Both feeds keep publishing weekly rows for a
  player on IR, so his card read 0.0 for the week -- correctly -- next to 2.6 a game for
  the rest of the season, in the very columns that rank the watchlist and set the floor
  under a drop. `player_line` carries `rest` and `playoff` as None whenever `reserved` is
  set. The contrast to keep is the bye: a player on a bye also projects nothing this week,
  and his forward rate is the most useful number on his card, because he is coming back
  next week and an IR player is not.
- **Byes come from the kickoff schedule.** Both feeds publish a row for a team on bye
  (Sleeper's empty, ESPN's with `appliedTotal` 0), so "no projection" finds nothing. A
  player with no NFL team at all is a different thing and must not be called a bye.

## Playoff race

`ffa/playoffs.py` answers "how many wins do I need", for every team. It is pure: a table, a
schedule and a number of places in, a `PlayoffPicture` out. `lineup/league.py::race` is the
only thing that assembles it from Sleeper payloads, and it rides on the lineup build, so the
League tab and `ffa lineup --teams` draw the same numbers.

- **The odds are drawn from what the teams score, when there is anything to draw from.**
  `ffa/strength.py` is the model and `## Scoring` below is the whole of it. Everything the
  race does with it is in `_score_trials`: two scores a game, the higher one takes the win,
  and both go into the points-for that breaks ties. That last part is half the point --
  the old simulation froze the tiebreak at the points already scored, so a team 30 behind
  on points-for lost every simulated tie for the rest of the season. With no scoring to go
  on (week one, or a hand-made fixture) `scoring` is None and every game is a coin flip,
  which is the old behaviour exactly, tests and all.
- **The guaranteed numbers never see the model.** `wins_to_clinch` and `wins_to_survive`
  are arithmetic over the schedule and have no probability in them at all. That is the
  point of having two instruments: a scoring model that turns out to be wrong moves the
  odds and cannot touch the Needs column. `build_picture` solves them first for a second
  reason -- a by-wins cell the arithmetic settles is one the sampler should not spend a
  trial on.
- **Two numbers, never one.** `wins_to_clinch` is arithmetic: the wins that take a place
  whatever else happens. `odds` is a coin-flip simulation. They disagree constantly -- an
  8-0 team needs one win to be *certain* and is at ~100% without it -- and conflating them
  would make the honest number look wrong. Both are labelled wherever they are drawn.
- **Every win total, not just the one that clinches.** `TeamOutlook.by_wins` is the same
  simulation read a row at a time: of the trials where a team won exactly k of its own
  games, how many ended with a place. One pass over the teams per trial splits it, so it is
  the same sample as the headline and the two cannot disagree about a league. Three things
  keep it honest. The ends are filled in from the flow rather than counted, so the first
  total the row calls certain *is* `wins_to_clinch` and nothing below `wins_to_survive`
  carries a chance. A total the sampler never saw miss is still not a guarantee -- the
  simulation breaks ties on points-for as they stand, the clinch assumes them lost -- so it
  is drawn `~100` and only an arithmetic certainty is drawn `100`. And the buckets are
  uneven: five games left splits ten thousand trials 312/1562/3125/3125/1562/312, which is
  the right way round, because the thin ends are the totals the arithmetic answers outright.
  The row is monotone (an extra win also denies a rival one), and a dip on screen is noise,
  so `test_more_wins_never_lower_the_odds` sweeps seeded leagues for one.
- **Which other games to watch is the third reading of the same trials.** The race says
  what a team needs; it cannot say which of the twenty fixtures it is *not* playing in
  decide whether it gets there, and on the cut line those are most of what a Sunday
  consists of. `Swing` is, per fixture, that roster's odds in the trials where each side
  won it -- so it costs the bucketing and nothing else, five milliseconds measured against
  the race's sixty. Only the first open week is watched, because a list of games to root
  for is worth drawing for the Sunday somebody can actually watch.
  **`spotlight` is a count, not a list of fixtures.** The same two rosters can meet twice
  in a season, so a `(first, second)` pair does not identify a game; `games` emits them in
  week order, which makes position the only honest key. A two-team league plays the same
  pair every week and is what found this.
  The swing counters live in each trial loop separately, so a test that only flips coins
  leaves the *shipped* path unexercised -- `test_your_own_game_is_the_one_that_moves_your_odds_most`
  runs against both simulations for that reason, after a mutation of the scored loop went
  unnoticed by the coin-only tests.
- **Tiebreaks stay out of the guaranteed numbers.** A clinch assumes every team level with
  you wins the tiebreak, an elimination assumes you do. That is what makes the number a
  guarantee: the tiebreak is points-for, and those points have not been scored yet. The
  odds do use points-for as it stands, because a simulation has to rank somehow, and say so.
- **It is a question about the schedule, not the win column.** The tempting version adds
  `games_left` to every rival's wins and counts who is above you. It is wrong, because the
  games left are played *against each other*: nine rivals cannot all win out. Three rosters
  sat on 3-5 in the `.env` league with five games each, all reaching eight wins by winning
  out, and only one of them clinched by doing it -- the one whose remaining fixtures
  included the other two, because beating them is what stops them matching it. A fixture
  count gives all three the same answer and is wrong twice. `_reachable` and `_assignable`
  are the real constraint, as max-flow from each game to the teams allowed to win it.
- **Which weeks are still open comes from the league, not the calendar.** Three traps:
  `nfl_state.week` is not to be trusted (it has read 9 while the app showed week 8); a week
  in progress is *scored but not recorded*, so on a live Sunday every week-10 matchup has
  points and nobody's record has moved; and a league can start late -- one `.env` league has
  `start_week: 2` and its week 1 comes back fully paired with no points at all, a fixture
  nobody will ever play. `last_scored_leg` and `start_week` answer all three without reading
  a matchup, and `reconciles` checks them against the games in the table, because both
  fields are undocumented. Only the open weeks are fetched.
- **Playoff weeks are not regular-season games.** Weeks 15-17 come back from the matchups
  endpoint fully paired and identical in shape to a real fixture. Counted in, they add three
  rounds of games anyone can still win a place with. `regular_season_end` is the cut.
- **A league shape that is not modelled is refused by name.** `league_average_match` (every
  team also scored against the weekly median) is a second result a week, and divisions seed
  their winners ahead of the table. Both would make the numbers wrong rather than
  approximate, so `unsupported` returns a reason and the card shows it instead of a table.
- **A thin by-wins cell is drawn again rather than left thin.** Coin flips spread a
  five-game run 312/1562/3125/3125/1562/312; once the teams have different strengths, the
  best team in a real league loses all five in 34 trials of ten thousand, and that cell is
  exactly the one a manager goes looking for. `_stratum` re-draws any unsettled cell under
  `MIN_BUCKET_TRIALS`, conditioning exactly rather than by rejection: `_draw_wins` picks
  which games were won from the conditional Bernoulli (a team that wins three of five most
  likely won the three it was favoured in), and `_split_scores` draws the scores behind a
  forced result -- a win without its scores would hand a team a record it did not score the
  points for, and the points are what breaks the tie. Equal spreads make a pair's sum
  independent of its difference, which is what lets the difference be truncated to the
  side that was forced and the two put back together. The topped-up cells are no longer the
  same sample as the headline odds; `test_a_cell_drawn_again_agrees_with_the_one_it_replaces`
  is what says the two estimators still agree.
- `tests/test_playoffs.py` keeps a brute-force cross-check: small leagues played out over
  every one of their 2^games outcomes, with the flow required to agree. There is no simpler
  correct implementation to check against, so there is an independent one.

## Scoring

`ffa/strength.py` answers "how much does this team score, and how much of that is real".
The second half is the whole module. Measured the same week on the two `.env` leagues:

| | league A | league B |
|---|---|---|
| spread of team scoring averages | 14.4 | 8.4 |
| week-to-week noise within a team | 21.5 | 26.3 |
| share of the gap that is signal | 72% | 0% |
| higher scorer won, out of sample | 21/25 | 15/30 |

- **League B's teams are not far apart; nine weeks of randomness are.** A spread of 8.4 is
  what ten identical teams produce over nine weeks, and out of sample it called games at
  exactly 50%. Nothing in a table says which league you are looking at, so `estimate`
  measures it: the spread you can see is the real spread plus `within / weeks` of noise, so
  taking the noise out leaves `reliability`, and each team's average is pulled toward the
  league's by whatever fraction survives. League B's teams collapse onto one number and the
  odds go back to coin flips without anyone deciding that by hand. Do not replace this with
  a raw average, however tempting its simplicity: that is the version that would have told
  this user his 8.4-point edge was worth something.
- **Two channels, because each is blind to what the other sees.** Scored weeks know how a
  manager actually sets his lineup; they cannot see that his back tore an ACL on Sunday.
  The projections see exactly that and nothing else. `lineup/league.py` solves each roster's
  best lineup on the rest-of-season rate for the second channel, which is one more call to
  the same solver.
- **The blend is an average of two estimates, not a discount applied twice.** The scoring
  term is shrunk because its own noise was measured; the projected term is taken as it
  comes, because measurement says it needs no shrinking -- in league A the projected edges
  spread 8.8 against a latent spread of 11.1, projections being conservative on their own.
  The two channels then contribute about equally (sd 4.7 and 4.4 of the 8.6 the model ends
  up with). Re-measure that before giving the projections more weight.
- **Both channels are centred before they are blended, so the level comes from the scores
  alone.** A projected *best* lineup is worth more than anyone scores, because every manager
  leaves something on his bench; centring cancels a bias no data here can measure.
  `PROJECTION_WEIGHT` is the one number nothing available can settle -- it would take a
  season of stored projections, and this tool stores none -- so half each is the honest
  statement of not knowing, and it is a constant rather than an edit.
- **One spread for every team.** Eight games cannot tell a volatile roster from a steady
  one, and a per-team variance off eight games would be noise with a name on it. It is also
  what makes `_split_scores` exact.
- **`backtest` is the acceptance test and lives in the library, not in a notebook.** It
  replays the season, estimating from the weeks before each one, and scores the result
  against the 0.25 of a coin flip. It validates the *scoring* channel only: nothing stores
  what the projections said in week 3, so the roster channel cannot be replayed and its
  weight rests on the measurement above rather than on a score. Run it on a real league before trusting any change here:
  on league A it scored 0.188 (25% of the coin flip's error removed, favourite winning 90%
  of 20 games); on league B it scored exactly 0.25, which is the estimator declining to
  guess and the right outcome there.

## Web app

`ffa serve` is a WSGI app with no framework and no new dependency: `ffa/web/app.py` routes,
`state.py` holds the shared feeds, `payload.py` turns a `DraftView` into JSON, and
`static/` is the whole UI (three files, vanilla JS, no build step — they ship inside the
package, like `draft/prompts/research.md`). Four tabs: Lineup (the default), League,
Waivers, Draft. Lineup and League are drawn from one `/api/lineup` response, so opening the
second costs nothing and cannot show a number the first disagrees with. The League tab leads
with the playoff race for the whole league -- what each team needs, this week's fixtures
ranked by how much each moves *your* odds, and a grid of every team's odds at every win
total still open to it -- then a picker for one team's lineup.

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
  caches for ten minutes and builds synchronously: a cold build measured about 1.3s with
  the ESPN pages revalidated and about 1.9s with that cache empty, which is a better trade than
  a worker thread and a progress protocol. It was 5.1s until the
  fetches were made to overlap and ESPN's was started first (see Lineup above); the phone
  says "working it out…" for exactly that long, so measure before adding anything to the
  wave. `load_advice` is driven offline by `tests/test_source.py`; for what a fake client
  cannot model, the check is still to diff `ffa lineup --json` against `main` (see Tests).
  The
  weeks already played, for the scoring model, are eight more cached requests and 0.17s of
  that -- measure this sort of thing rather than diffing two timed builds, because the run
  to run spread on a cold build is five times the number being looked for.
- The league picker is one control in the header, shared by the lineup and waivers tabs,
  because two controls for one choice is one of them being wrong. It is hidden on the draft
  tab, which follows `DRAFT_ID` instead.
- The by-wins grid sets its own `grid-template-columns` from JS, because the column count is
  the number of games left. Track widths there are in pixels: an `em` resolves against the
  row's font size, not the cells', which at a phone's width cost the team column fifty
  pixels and most of a name. Names are `nowrap` so the inherited ellipsis fires, and the
  grid sits in a `.scrollx` for the fifteen columns week one would want.
- `show()` toggles the `hidden` *attribute*, and an author `display` rule outranks the user
  agent's `[hidden] { display: none }` — a flex or grid element would stay visible. There is
  now an explicit `[hidden]` rule in `styles.css`; don't remove it.
- Warnings go to stderr. Rich's default console is stdout, which is where `--json` puts
  its document, so a single warning in front of it breaks `ffa lineup --json | jq`. The
  handler is built with an explicit stderr console in `cli._setup_logging`; keep it.
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
- **The ESPN feed is revalidated, not re-downloaded.** It sends an `ETag` and answers a
  request carrying it with `304 Not Modified` in 0.02s and no body, against 0.23s and
  4.1MB for one page. So each page is kept under `cache/espn` with its tag and sent back
  as `If-None-Match`. This is not a staleness trade and must not become one: the *server*
  decides whether the data changed, and a changed projection comes back as a 200 with the
  new body. Never add a time-based expiry here -- that would be the trade, and the whole
  point is not making it.
- **What is kept is the extract, not the page.** A page is four megabytes of ownership
  percentages, news and draft ranks around the few hundred bytes this tool reads, and
  keeping the page meant `json.loads` on 25MB every build -- as slow as the download it
  replaced, which is why the first version of this cache bought 0.05s. `_extract` keeps
  identity plus each week's projected stat line, and `CACHE_VERSION` is in the filename so
  that changing what is kept invalidates by not matching; `_prune_cache` deletes the older
  versions, since nothing else ever would.
- **The stats are translated on the way in, not on the way out.** ESPN's numeric stat ids
  become Sleeper's keys when the page is read rather than for every player in every week
  of every build -- a thousand players by eight weeks is eight thousand dictionaries that
  were being rebuilt from ids that had not changed. The warm feed went 0.237s to 0.137s
  and the cache 4.3MB to 2.1MB, because the untranslatable ids are dropped on the way in
  too.
- The ESPN player feed is paged, and all the pages are fetched at once. They used to go
  one-then-four, so that a pool smaller than one page could not waste four requests -- a
  case ESPN's feed never produces, since it is a thousand players deep all season, and one
  that cost 0.66s of *every* build, because the first page is as slow as the other four
  together. The page budget is what gets asked for; a short page still ends the reading,
  which is what decides the answer. A row is parsed once for every week asked for, not
  once per week.
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
- `cache/` (gitignored) holds the player map, logs (`ffa.log`), the kept ESPN pages
  (`espn/`, a few megabytes, pruned to the current week) and real draft recordings
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

`tests/test_strength.py` is mostly about league B above: a model that cannot report "there
is nothing here" dresses noise up as a forecast, which on this card would be worse than the
coin flip it replaced. It checks the estimator on synthetic seasons whose true spread is
known, including one with no spread at all.

`tests/test_source.py` drives `load_advice` end to end over a fake Sleeper client, and is
the only test that exercises the assembly itself: which week the build settles on, which
weeks it fetches and how often, that the ESPN window covers whatever week the kickoffs
pick, and that ESPN being down costs the second opinion and nothing else. The fake records
every call, because *which* weeks are fetched is half of what that function decides and
fetching one twice is invisible in the output. For anything a fake cannot model -- a real
feed's shape, a league's own settings -- the check is still to diff `ffa lineup --json`
against `main`, which should differ only in `now`. Interleave the runs (main, branch,
main) when you do: a roster move or a projection update between two runs minutes apart
shows up as dozens of differences that have nothing to do with the change, and the second
`main` is what tells you which you are looking at.

The lineup tests are hand-made rather than recorded: `tests/test_lineup.py` builds a roster
from tuples, so no real league data is involved and each test pins one way a feed or a
shortcut misled the tool against live data.
