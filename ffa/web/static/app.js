"use strict";
/* The phone UI. All fantasy logic lives on the server: this file fetches JSON, draws it,
   and ticks the pick clock between polls. No build step, no dependencies. */

// -- token ----------------------------------------------------------------------------

const TOKEN_KEY = "ffa-token";

function stored(key) {
  try { return window.localStorage.getItem(key) || ""; } catch { return ""; }
}

function store(key, value) {
  try { window.localStorage.setItem(key, value); } catch { /* private mode: fine */ }
}

let TOKEN = "";
(function readToken() {
  const url = new URL(window.location.href);
  const fromUrl = url.searchParams.get("token");
  if (fromUrl) {
    store(TOKEN_KEY, fromUrl);
    url.searchParams.delete("token");
    // Keep the secret out of the address bar (and out of screenshots).
    window.history.replaceState({}, "", url.pathname + (url.search ? url.search : "") + url.hash);
  }
  TOKEN = fromUrl || stored(TOKEN_KEY);
})();

// -- fetch ----------------------------------------------------------------------------

async function api(path, params) {
  const url = new URL(path, window.location.origin);
  for (const [key, value] of Object.entries(params || {})) {
    if (Array.isArray(value)) value.forEach((v) => url.searchParams.append(key, v));
    else if (value !== null && value !== undefined && value !== "") url.searchParams.set(key, value);
  }
  const response = await fetch(url, {
    headers: TOKEN ? { "X-FFA-Token": TOKEN } : {},
    cache: "no-store",
  });
  const text = await response.text();
  let data = null;
  try { data = JSON.parse(text); } catch { /* the context block is markdown, not JSON */ }
  if (!response.ok) {
    const error = new Error((data && data.error) || `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return data === null ? text : data;
}

// -- tiny DOM helpers -----------------------------------------------------------------

const $ = (selector) => document.querySelector(selector);

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

function show(node, visible) {
  if (node) node.hidden = !visible;
}

function plural(count, noun) {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

function fmtTime(iso) {
  if (!iso) return "";
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return "";
  return when.toLocaleString(undefined, {
    weekday: "short", month: "numeric", day: "numeric", hour: "numeric", minute: "2-digit",
  });
}

function fmtIn(iso, fromIso) {
  if (!iso) return "";
  const hours = (new Date(iso) - new Date(fromIso || Date.now())) / 3600000;
  if (!Number.isFinite(hours)) return "";
  if (hours < 1) return `in ${Math.max(0, Math.round(hours * 60))}m`;
  return hours < 48 ? `in ${Math.round(hours)}h` : `in ${(hours / 24).toFixed(1)}d`;
}

function playerLine(player) {
  if (!player) return "";
  return [player.position, player.team || "FA"].filter(Boolean).join(" ");
}

// -- status line ----------------------------------------------------------------------

const statusNode = $("#status");
const statusLine = document.querySelector(".statusline");
const dot = el("span", "dot");
statusLine.insertBefore(dot, statusNode);

function setStatus(kind, text) {
  statusLine.className = `statusline ${kind}`;
  statusNode.textContent = text;
}

// -- draft ----------------------------------------------------------------------------

const draft = {
  data: null,
  offsetMs: 0,      // server clock minus this device's clock
  fetchedAt: 0,
  failures: 0,
  timer: null,
  error: null,
};

function banner(node, message, kind) {
  if (!message) {
    node.classList.add("hidden");
    return;
  }
  node.className = `banner ${kind || ""}`;
  node.textContent = message;
}

async function pollDraft() {
  window.clearTimeout(draft.timer);
  try {
    const data = await api("/api/draft");
    draft.data = data;
    draft.offsetMs = data.server_now_ms - Date.now();
    draft.fetchedAt = Date.now();
    draft.failures = 0;
    draft.error = null;
    renderDraft(data);
    const wait = Math.max(1000, (data.next_poll_in || 3) * 1000 + 250);
    schedule(wait);
  } catch (error) {
    draft.failures += 1;
    draft.error = error.message;
    if (!draft.data) {
      $("#board-card").replaceChildren(el("div", "skeleton", "No draft board yet."));
      ["#research-card", "#shortlist-card", "#roster-card", "#recent-card"].forEach((id) => show($(id), false));
    }
    banner($("#draft-banner"), error.message, "error");
    setStatus("down", draft.data ? "reconnecting…" : "not available");
    schedule(Math.min(30000, 2000 * draft.failures));
  }
}

function schedule(ms) {
  window.clearTimeout(draft.timer);
  if (state.tab !== "draft" || document.hidden) return;  // don't poll a screen nobody sees
  draft.timer = window.setTimeout(pollDraft, ms);
}

function renderDraft(data) {
  banner($("#draft-banner"), data.note, "");
  renderBoard(data);
  renderResearch(data);
  renderShortlist(data);
  renderRoster(data);
  renderRecent(data);
  const league = data.league.name || "mock draft";
  $("#draft-footnote").textContent =
    `${league} · ${data.league.teams}-team ${data.league.draft_type}, ${data.league.rounds} rounds · ` +
    `${data.league.scoring} · starters: ${data.league.lineup}`;
  tickStatus();
}

function clockText(data) {
  const clock = data.clock;
  const board = data.board;
  if (board.complete || board.status === "complete") return { text: "draft complete", off: true };
  if (board.status === "paused") return { text: "PAUSED", off: true };
  if (board.status === "pre_draft") return { text: "not started", off: true };
  if (!clock.pick_timer || clock.pick_timer <= 0) return { text: "no pick timer", off: true };
  if (!clock.deadline_ms) return { text: "clock unknown", off: true };
  const left = Math.max(0, Math.round((clock.deadline_ms - (Date.now() + draft.offsetMs)) / 1000));
  const text = `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
  return { text, seconds: left };
}

function renderClock() {
  const node = $("#clock");
  if (!node || !draft.data) return;
  const clock = clockText(draft.data);
  node.textContent = clock.text;
  node.className = "clock" + (clock.off ? " off" : clock.seconds <= 15 ? " hot" : clock.seconds <= 30 ? " warn" : "");
}

function renderBoard(data) {
  const board = data.board;
  const card = clear($("#board-card"));
  card.className = "card" + (board.is_my_turn ? " mine" : "");

  const top = el("div", "board-top");
  const pick = el("div", "board-pick");
  if (board.complete || !board.current) {
    pick.textContent = "Draft complete";
  } else {
    pick.append(
      el("span", null, `Round ${board.current.round} · Pick ${board.current.label}`),
      el("span", "of", `  #${board.current.pick_no} of ${board.total_picks}`),
    );
  }
  const clock = el("div", "clock");
  clock.id = "clock";
  top.append(pick, clock);
  card.append(top);

  if (board.is_my_turn) {
    card.append(el("div", "myturn", "YOUR PICK"));
  } else if (!board.complete) {
    const line = el("div", "onclock");
    line.append(el("span", null, "on the clock: "), el("span", "who", board.on_clock_name || "?"));
    if (board.on_clock_via) line.append(el("span", "sub", ` (traded from ${board.on_clock_via})`));
    card.append(line);
  }

  const meta = el("div", "meta");
  if (data.you.slot === null) {
    meta.append(el("span", null, "your draft slot isn't known yet"));
  } else {
    meta.append(label("you", `slot ${data.you.slot}`));
  }
  if (board.my_picks.length) {
    meta.append(label("your picks", board.my_picks.map((p) => p.label).join(", ")));
    if (board.picks_until_my_turn !== null) {
      meta.append(el("span", null, `${plural(board.picks_until_my_turn, "selection")} away`));
    }
  }
  meta.append(label("status", board.status));
  card.append(meta);

  (data.league.warnings || []).forEach((warning) => card.append(el("div", "notice warn", `! ${warning}`)));
  renderClock();
}

function label(name, value) {
  const span = el("span");
  span.append(`${name}: `, el("b", null, value));
  return span;
}

function renderResearch(data) {
  const research = data.research;
  const card = $("#research-card");
  const body = clear(card.querySelector(".body"));
  show(card, true);

  if (research.status === "ready" && research.pick) {
    const head = el("div");
    head.append(el("span", "verdict-pick", research.pick.name));
    if (research.pick.confidence) head.append(el("span", "conf", `  ${research.pick.confidence} confidence`));
    body.append(head);
    if (research.summary) body.append(el("p", "summary", research.summary));
    const others = research.ranked.filter((v) => v.player_id !== research.pick.player_id).slice(0, 3);
    if (others.length) {
      const also = el("div", "also");
      also.append(el("div", "sub", "also considered"));
      others.forEach((verdict) => {
        const row = el("div");
        row.append(el("div", "who", verdict.name), el("div", "what", verdict.verdict || verdict.rationale || ""));
        also.append(row);
      });
      body.append(also);
    }
    const bits = [];
    if (research.sources.length) bits.push(plural(research.sources.length, "source"));
    if (research.elapsed_seconds) bits.push(`${research.elapsed_seconds}s`);
    if (research.budget_left !== null) bits.push(`${research.budget_left} calls left`);
    if (bits.length) body.append(el("div", "sub", bits.join(" · ")));
  } else {
    const text = {
      researching: "Researching candidates…",
      idle: "Research queued…",
      ready: "Research landed but named nobody.",
      failed: `Research unavailable: ${research.note || "failed"}`,
      off: `Research off: ${research.note || "no Anthropic credentials"}`,
    }[research.status] || "Research pending…";
    body.append(el("div", `research-state ${research.status}`, text));
  }

  const shortlist = data.shortlist;
  if (shortlist.dropoffs.length) {
    const scarcity = el("div", "scarcity");
    const head = shortlist.following
      ? `Dropoff from ${shortlist.decision.label} to ${shortlist.following.label}, in search_rank spots`
      : "Positional dropoff";
    scarcity.append(el("div", "sub", head));
    shortlist.dropoffs.forEach((drop) => {
      const pair = el("div", "pair");
      pair.append(
        el("div", "pos", drop.position),
        el("div", `val${drop.value >= 20 ? " big" : ""}`, drop.value),
        el("div", "names", `${drop.best_now} → ${drop.best_then || "nobody"}`),
      );
      scarcity.append(pair);
    });
    body.append(scarcity);
  }
}

function renderShortlist(data) {
  const shortlist = data.shortlist;
  const card = $("#shortlist-card");
  const body = clear(card.querySelector(".body"));
  show(card, true);
  $("#shortlist-sub").textContent = shortlist.decision ? `for pick ${shortlist.decision.label}` : "";

  if (shortlist.unavailable) {
    body.append(el("div", "notice warn", shortlist.unavailable));
    return;
  }
  if (!shortlist.candidates.length) {
    body.append(el("div", "notice warn", shortlist.defenses.length
      ? "Only a team defense is left to fill."
      : "No candidates: every position is blocked by your roster rules."));
  }

  const rows = el("div", "rows");
  shortlist.candidates.forEach((candidate, index) => {
    const row = el("div", "row" + (candidate.recommended ? " pick" : "") + (candidate.likely_gone ? " gone" : ""));
    row.append(el("div", "idx", index + 1));

    const main = el("div", "main");
    const name = el("div", "name", candidate.name);
    if (candidate.recommended) name.prepend(el("span", "tag star", "★"), " ");
    main.append(name);

    const line = el("div", "line");
    line.append(el("span", null, playerLine(candidate)));
    line.append(el("span", null, `adj ${candidate.adj}`));
    if (candidate.dropoff) line.append(el("span", null, `drop ${candidate.dropoff}`));
    if (candidate.need_label !== "-") line.append(el("span", `tag ${candidate.need_label}`, candidate.need_label));
    if (candidate.likely_gone) line.append(el("span", "tag gone", shortlist.on_the_clock ? "won't last" : "gone?"));
    if (candidate.injury_status) line.append(el("span", "tag inj", candidate.injury_status));
    main.append(line);

    if (candidate.verdict && candidate.verdict.verdict) {
      main.append(el("div", "line", candidate.verdict.verdict));
    }
    row.append(main, el("div", "right", `#${candidate.search_rank}`));
    rows.append(row);
  });
  body.append(rows);

  if (shortlist.defenses.length) {
    body.append(el("div", "sub",
      `Team defenses available (Sleeper publishes no rank for them): ${shortlist.defenses.slice(0, 16).join(", ")}`));
  }
  const blocked = Object.entries(shortlist.blocked);
  if (blocked.length) {
    body.append(el("div", "sub",
      "Excluded by roster rules: " + blocked.map(([pos, why]) => `${pos} (${why})`).join("; ")));
  }
}

function renderRoster(data) {
  const card = $("#roster-card");
  const body = clear(card.querySelector(".body"));
  show(card, true);
  const open = data.roster.open_starters;
  $("#roster-sub").textContent = open.length ? `open: ${open.join(", ")}` : "starters full";

  const rows = el("div", "rows");
  data.roster.slots.forEach((slot) => {
    const row = el("div", "row");
    const starter = slot.starter;
    row.append(el("div", `slot${starter ? " starter" : ""}${!slot.player && starter ? " open" : ""}`, slot.slot));
    const main = el("div", "main");
    if (slot.player) {
      const name = el("div", "name", slot.player.name);
      main.append(name);
      const line = el("div", "line");
      line.append(el("span", null, playerLine(slot.player)));
      if (slot.keeper) line.append(el("span", "tag keeper", "keeper"));
      if (slot.player.injury_status) line.append(el("span", "tag inj", slot.player.injury_status));
      if (slot.needs_replacement) line.append(el("span", "tag gone", "needs replacement"));
      main.append(line);
    } else {
      main.append(el("div", starter ? "open-text" : "bench-text", starter ? "— open —" : "—"));
    }
    row.append(main);
    rows.append(row);
  });
  body.append(rows);
}

function renderRecent(data) {
  const card = $("#recent-card");
  const body = clear(card.querySelector(".body"));
  show(card, true);
  if (!data.recent.length) {
    body.append(el("div", "sub", "No picks yet."));
  } else {
    const rows = el("div", "rows");
    data.recent.forEach((pick) => {
      const row = el("div", "row");
      row.append(el("div", "idx", pick.label));
      const main = el("div", "main");
      main.append(el("div", "name", pick.player ? pick.player.name : "?"));
      main.append(el("div", "line", `${pick.manager} · ${playerLine(pick.player)}`));
      row.append(main);
      rows.append(row);
    });
    body.append(rows);
  }
  (data.runs || []).forEach((run) => {
    body.append(el("div", "run", `RUN: ${run.position} — ${run.count} of the last ${run.window} picks`));
  });
}

// -- the context block ----------------------------------------------------------------

$("#copy-context").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const area = $("#context-text");
  button.disabled = true;
  button.textContent = "Fetching…";
  try {
    const text = await api("/api/draft/context");
    let copied = false;
    try {
      // The clipboard API needs a secure context, which http://<lan-ip> is not.
      await navigator.clipboard.writeText(text);
      copied = true;
    } catch { /* fall through to the textarea */ }
    area.value = text;
    if (copied) {
      area.classList.add("hidden");
      button.textContent = "Copied ✓";
    } else {
      area.classList.remove("hidden");
      area.focus();
      area.select();
      button.textContent = "Select all and copy ↓";
    }
  } catch (error) {
    button.textContent = `Failed: ${error.message}`;
  } finally {
    button.disabled = false;
    window.setTimeout(() => { button.textContent = "Copy context for Claude"; }, 4000);
  }
});

// -- waivers --------------------------------------------------------------------------

const waivers = { data: null, limit: 25, positions: new Set(), query: "", timer: null, debounce: null };

async function loadWaivers() {
  window.clearTimeout(waivers.timer);
  const card = $("#waivers-card");
  if (!waivers.data) card.replaceChildren(el("div", "skeleton", "Loading waivers…"));
  try {
    const data = await api("/api/waivers", {
      league: leagues.chosen,
      q: waivers.query,
      pos: [...waivers.positions],
      limit: waivers.limit,
    });
    waivers.data = data;
    banner($("#waivers-banner"), null);
    renderWaivers(data);
  } catch (error) {
    banner($("#waivers-banner"), error.message, "error");
    if (!waivers.data) card.replaceChildren(el("div", "skeleton", "No waiver report."));
  }
  if (state.tab === "waivers" && !document.hidden) {
    waivers.timer = window.setTimeout(loadWaivers, 60000);  // matches the server's report TTL
  }
  tickStatus();
}

function renderWaivers(data) {
  const headerCard = $("#waivers-header-card");
  const head = clear(headerCard.querySelector(".body"));
  show(headerCard, true);
  head.append(el("div", "name", `${data.league} · NFL week ${data.week}`));
  head.append(el("div", "wline",
    `After-game waivers next clear ${fmtTime(data.next_after_game_clear)} (12:05 AM Pacific). ` +
    `Dropped players are held ${data.drop_hold_hours}h.`));

  const weekClear = data.week_closed_clears_at;
  if (weekClear && new Date(weekClear) > new Date(data.now)) {
    head.append(el("div", "notice warn",
      `Every unrostered player is on waivers until ${fmtTime(weekClear)}: the week's last game ` +
      `kicked off ${fmtTime(data.week_closed)}, so teams on bye are locked too.`));
  }
  const locked = Object.entries(data.locked_teams || {});
  if (locked.length) {
    const byClear = new Map();
    locked.forEach(([team, clears]) => {
      if (!byClear.has(clears)) byClear.set(clears, []);
      byClear.get(clears).push(team);
    });
    [...byClear.entries()].sort().forEach(([clears, teams]) => {
      head.append(el("div", "wline", `Locked after kickoff: ${teams.sort().join(", ")} until ${fmtTime(clears)}`));
    });
  } else if (!data.week_closed) {
    head.append(el("div", "wline", "No team is locked right now."));
  }

  const card = clear($("#waivers-card"));
  if (!data.players.length) {
    card.append(el("div", "skeleton", waivers.query
      ? `No player matches “${waivers.query}”.`
      : "Nobody to show."));
    return;
  }
  const rows = el("div", "rows");
  data.players.forEach((player) => {
    const row = el("div", "row");
    const main = el("div", "main");
    main.append(el("div", "name", player.name));
    const line = el("div", "line");
    line.append(el("span", null, `${player.position} ${player.team || "FA"}`));
    main.append(line);

    const why = [];
    if (player.kicked_off) why.push(`game kicked off ${fmtTime(player.kicked_off)}`);
    if (player.dropped_at) why.push(`dropped ${fmtTime(player.dropped_at)}`);
    if (player.week_closed) why.push(`week's last game kicked off ${fmtTime(player.week_closed)}`);
    if (player.clears_at) {
      const when = el("div", "wline");
      when.append(el("span", "when", `clears ${fmtTime(player.clears_at)}`), ` ${fmtIn(player.clears_at, data.now)}`);
      main.append(when);
    }
    if (why.length) main.append(el("div", "wline", why.join("; ")));

    const right = el("div", "right");
    const pill = player.status === "free_agent" ? el("span", "pill free", "free agent")
      : player.status === "waivers" ? el("span", "pill waivers", "waivers")
      : el("span", "pill rostered", player.rostered_by || "rostered");
    right.append(pill);
    row.append(main, right);
    rows.append(row);
  });
  card.append(rows);

  if (!waivers.query && data.players.length >= waivers.limit) {
    const more = el("button", "button", "Show more");
    more.type = "button";
    more.addEventListener("click", () => { waivers.limit += 50; loadWaivers(); });
    const actions = el("div", "actions");
    actions.append(more);
    card.append(actions);
  }
}

$("#waivers-query").addEventListener("input", (event) => {
  waivers.query = event.target.value.trim();
  window.clearTimeout(waivers.debounce);
  waivers.debounce = window.setTimeout(loadWaivers, 350);
});

$("#waivers-positions").addEventListener("click", (event) => {
  const chip = event.target.closest(".chip");
  if (!chip) return;
  const position = chip.dataset.pos;
  if (waivers.positions.has(position)) waivers.positions.delete(position);
  else waivers.positions.add(position);
  chip.classList.toggle("on");
  loadWaivers();
});


$("#waivers-refresh").addEventListener("click", () => loadWaivers());

// -- leagues --------------------------------------------------------------------------

// One league for the whole app: the lineup and waiver tabs are always looking at the same
// one, so the control belongs in the header rather than twice in the panels.
const leagues = { list: [], chosen: stored("ffa-league") || "" };

async function loadLeagues() {
  try {
    const data = await api("/api/leagues");
    leagues.list = data.leagues || [];
  } catch (error) {
    leagues.list = [];
  }
  const known = leagues.list.some((l) => l.league_id === leagues.chosen);
  if (leagues.chosen && !known) leagues.chosen = "";  // a league that is no longer configured
  renderLeagues();
}

function renderLeagues() {
  const bar = $("#league-chips");
  // With one league there is nothing to choose, so the bar stays out of the way.
  show(bar, leagues.list.length > 1 && state.tab !== "draft");
  clear(bar);
  const active = leagues.chosen || (leagues.list.find((l) => l.default) || {}).league_id || "";
  leagues.list.forEach((league) => {
    const chip = el("button", "chip", league.name);
    chip.type = "button";
    chip.dataset.league = league.league_id;
    if (league.league_id === active) chip.classList.add("on");
    if (!league.available) chip.classList.add("off");
    bar.append(chip);
  });
}

function chooseLeague(leagueId) {
  if (leagues.chosen === leagueId) return;
  leagues.chosen = leagueId;
  store("ffa-league", leagueId);
  lineup.data = null;
  waivers.data = null;
  state.team = null;  // roster ids are per league; keeping one across a switch picks a stranger
  renderLeagues();
  if (state.tab === "lineup" || state.tab === "league") loadLineup();
  else if (state.tab === "waivers") loadWaivers();
}

$("#league-chips").addEventListener("click", (event) => {
  const chip = event.target.closest(".chip");
  if (chip) chooseLeague(chip.dataset.league);
});

// -- lineup ---------------------------------------------------------------------------

const lineup = { data: null, error: null, fetchedAt: 0, timer: null };

async function loadLineup() {
  const summary = $("#lineup-summary-card");
  if (!lineup.data) summary.replaceChildren(el("div", "skeleton", "Working out your week…"));
  try {
    const data = await api("/api/lineup", { league: leagues.chosen });
    lineup.data = data;
    lineup.error = null;
    lineup.fetchedAt = Date.now();
    banner($("#lineup-banner"), null);
    renderLineup(data);
    renderLeague(data);
  } catch (error) {
    lineup.error = error.message;
    banner($("#lineup-banner"), error.message, "error");
    if (!lineup.data) summary.replaceChildren(el("div", "skeleton", "No lineup advice yet."));
  }
  tickStatus();
}

function pointsCell(value) {
  const cell = el("span", "proj", Number(value).toFixed(1));
  if (value <= 0) cell.classList.add("zero");
  return cell;
}

function nameBlock(player, extra) {
  const main = el("div", "lmain");
  main.append(el("span", "name", player.name));
  main.append(el("span", "tag", `${player.position}${player.team ? " · " + player.team : ""}`));
  if (player.locked) main.append(el("span", "tag locked", "locked"));
  if (player.on_bye) main.append(el("span", "tag warn", "bye"));
  if (player.disputed) main.append(el("span", "tag warn", `±${player.spread.toFixed(1)}`));
  if (extra) main.append(extra);
  return main;
}

// The rest of the season and the playoff window answer different questions, so they are
// two tags rather than one number — and they belong on anyone worth claiming, not just
// the bench. Both come from the payload already blended; `keep` is the drop floor and is
// deliberately not drawn, because a stale preseason number beside two fresh ones reads as
// though it were one of them.
function horizonTags(player) {
  const tags = [];
  if (player.rest !== null && player.rest !== undefined) {
    tags.push(el("span", "tag", `${player.rest.toFixed(1)}/g rest`));
  }
  if (player.playoff !== null && player.playoff !== undefined) {
    tags.push(el("span", "tag playoff", `${player.playoff.toFixed(1)}/g playoffs`));
  }
  return tags;
}

function perGame(value, suffix) {
  return value === null || value === undefined ? "" : `${value.toFixed(1)}/g ${suffix}`;
}

function why(row, player) {
  if (player.reasons && player.reasons.length) {
    row.append(el("div", "lwhy", player.reasons.join("; ")));
  }
}

function playerRow(player, extra) {
  const row = el("div", "lrow");
  row.append(nameBlock(player, extra));
  row.append(pointsCell(player.points));
  why(row, player);
  return row;
}

function seatRow(seat) {
  const row = el("div", "lrow seat");
  row.append(el("span", "slot", seat.slot));
  if (!seat.player) {
    row.append(el("div", "lmain", "empty"));
    row.append(el("span", "proj zero", "—"));
    return row;
  }
  const swapTo = seat.swap_to ? el("span", "tag swapto", `→ ${seat.swap_to.name}`) : null;
  row.append(nameBlock(seat.player, swapTo));
  row.querySelector(".lmain").append(...horizonTags(seat.player));
  row.append(pointsCell(seat.player.points));
  why(row, seat.player);
  return row;
}

function renderLineup(data) {
  // Summary.
  const summary = clear($("#lineup-summary-card"));
  summary.append(el("div", "name", `${data.league} · NFL week ${data.week}`));
  if (data.settled) {
    summary.append(el("div", "notice good", "This is already the best lineup this roster can field."));
  } else {
    summary.append(el("div", "notice warn",
      `Change your lineup for +${data.gain.toFixed(1)} points: ` +
      `${data.current_total.toFixed(1)} → ${data.optimal_total.toFixed(1)}.`));
  }
  if (data.first_kickoff) {
    summary.append(el("div", "wline",
      `First game ${fmtTime(data.first_kickoff)}. Each player locks at his own kickoff.`));
  }
  const sources = data.sources || {};
  summary.append(el("div", "wline dim",
    `Projections: Sleeper${sources.espn ? " blended with ESPN" : " only"}, ` +
    `re-scored with this league's settings.` + (sources.espn_note ? ` ESPN: ${sources.espn_note}` : "")));
  const span = (weeks) => weeks.length === 1
    ? `week ${weeks[0]}` : `weeks ${weeks[0]}-${weeks[weeks.length - 1]}`;
  const spans = [["Rest of season", sources.rest_weeks || []],
                 ["playoffs", sources.playoff_weeks || []]]
    .filter(([, weeks]) => weeks.length)
    .map(([label, weeks]) => `${label} is ${span(weeks)}`);
  if (spans.length) {
    summary.append(el("div", "wline dim",
      `${spans.join(", ")} — both projected game by game and re-scored here, not ` +
      `Sleeper's preseason season row. That row is still the floor under a drop.`));
  }
  (data.notes || []).forEach((note) => summary.append(el("div", "wline dim", note)));

  // The moves, which is the whole point of the tab.
  const movesCard = $("#lineup-moves-card");
  show(movesCard, (data.swaps || []).length > 0);
  const moves = clear(movesCard.querySelector(".body"));
  (data.swaps || []).forEach((swap) => {
    const row = el("div", "lrow cand swap");
    row.append(el("span", "gain", `+${swap.gain.toFixed(1)}`));
    const main = el("div", "lmain");
    main.append(el("span", "name", swap.in.name));
    main.append(el("span", "tag", `into ${swap.slot}`));
    if (swap.out) main.append(el("span", "tag out", `for ${swap.out.name}`));
    row.append(main);
    moves.append(row);
  });

  // The lineup as it stands.
  const card = $("#lineup-card");
  show(card, true);
  $("#lineup-sub").textContent = `${data.current_total.toFixed(1)} pts as set`;
  const body = clear(card.querySelector(".body"));
  (data.lineup || []).forEach((seat) => body.append(seatRow(seat)));

  // Bench, adds, watchlist.
  const benchCard = $("#lineup-bench-card");
  show(benchCard, (data.bench || []).length > 0);
  const bench = clear(benchCard.querySelector(".body"));
  (data.bench || []).forEach((player) => {
    const row = playerRow(player);
    row.querySelector(".lmain").append(...horizonTags(player));
    bench.append(row);
  });

  renderCandidates($("#lineup-adds-card"), data.adds || []);
  renderCandidates($("#lineup-watch-card"), data.watch || []);
  $("#lineup-age").textContent = `as of ${fmtTime(data.now)}`;
}

function renderCandidates(card, candidates) {
  show(card, candidates.length > 0);
  if (!candidates.length) return;
  const body = clear(card.querySelector(".body"));
  candidates.forEach((candidate) => {
    const row = el("div", "lrow cand");
    row.append(el("span", "gain", `+${candidate.gain.toFixed(1)}`));
    const status = candidate.available_now
      ? el("span", "tag good", "free agent")
      : candidate.clears_at
        ? el("span", "tag warn", `waivers → ${fmtTime(candidate.clears_at)}`)
        : null;
    row.append(nameBlock(candidate.player, status));
    // A claim is a roster decision, so both horizons belong on it, not just this week's gain.
    row.querySelector(".lmain").append(...horizonTags(candidate.player));
    if (candidate.drop) {
      const drop = candidate.drop;
      const worth = [perGame(drop.rest, "rest of season"), perGame(drop.playoff, "in the playoffs")]
        .filter(Boolean).join(", ");
      row.append(el("div", "lwhy",
        `Drop ${drop.name}${worth ? ` (${worth})` : ""}`));
    }
    if (candidate.player.reasons && candidate.player.reasons.length) {
      row.append(el("div", "lwhy", candidate.player.reasons.join("; ")));
    }
    body.append(row);
  });
}

$("#lineup-refresh").addEventListener("click", () => { lineup.data = null; loadLineup(); });

// -- the league: every other manager, off the same payload ----------------------------

function teamsOf(data) {
  return (data && data.teams) || [];
}

function chosenTeam(data) {
  const teams = teamsOf(data);
  if (!teams.length) return null;
  const picked = teams.find((t) => t.roster_id === state.team);
  // Nobody has picked yet, so open on the matchup: that is the one everybody wants.
  return picked || teams.find((t) => t.opponent) || teams.find((t) => !t.mine) || teams[0];
}

function renderLeague(data) {
  const teams = teamsOf(data);
  const bar = $("#team-chips");
  show(bar, teams.length > 1);
  clear(bar);
  const team = chosenTeam(data);
  teams.forEach((entry) => {
    const suffix = entry.mine ? " (you)" : entry.opponent ? " · this week" : "";
    const chip = el("button", "chip", entry.name + suffix);
    chip.type = "button";
    chip.dataset.roster = String(entry.roster_id);
    if (team && entry.roster_id === team.roster_id) chip.classList.add("on");
    bar.append(chip);
  });

  const card = clear($("#league-team-card"));
  if (!team) {
    card.append(el("div", "skeleton", "No other rosters in this league."));
    show($("#league-bench-card"), false);
    return;
  }
  card.append(el("div", "name", team.name + (team.manager && team.manager !== team.name
    ? ` · ${team.manager}` : "")));
  const totals = [`${team.total.toFixed(1)} pts as set`];
  if (team.left_on_bench >= 0.1) totals.push(`${team.left_on_bench.toFixed(1)} left on his bench`);
  card.append(el("div", "wline", totals.join(" · ")));
  (team.notes || []).forEach((note) => card.append(el("div", "wline warn-text", note)));
  const body = el("div", "body");
  (team.lineup || []).forEach((seat) => body.append(seatRow(seat)));
  card.append(body);

  const benchCard = $("#league-bench-card");
  show(benchCard, (team.bench || []).length > 0);
  $("#league-bench-sub").textContent = `${(team.bench || []).length} players`;
  const bench = clear(benchCard.querySelector(".body"));
  (team.bench || []).forEach((player) => {
    const row = playerRow(player);
    row.querySelector(".lmain").append(...horizonTags(player));
    bench.append(row);
  });
}

function chooseTeam(rosterId) {
  state.team = rosterId;
  store("ffa-team", String(rosterId));
  renderLeague(lineup.data);
}

$("#team-chips").addEventListener("click", (event) => {
  const chip = event.target.closest(".chip");
  if (chip) chooseTeam(Number(chip.dataset.roster));
});

// -- tabs, freshness, visibility ------------------------------------------------------

const TABS = ["lineup", "league", "waivers", "draft"];
const state = { tab: "lineup", team: Number(stored("ffa-team")) || null };

function tickStatus() {
  if (state.tab === "draft") {
    if (draft.error && !draft.data) return setStatus("down", draft.error);
    if (!draft.data) return setStatus("", "connecting…");
    const age = Math.round((Date.now() - draft.fetchedAt) / 1000);
    const kind = draft.error ? "down" : age > 20 ? "stale" : "live";
    const research = draft.data.research.status;
    const suffix = research === "researching" ? " · researching" : research === "ready" ? " · research ready" : "";
    return setStatus(kind, `${draft.data.board.status} · updated ${age}s ago${suffix}`);
  }
  if (state.tab === "lineup" || state.tab === "league") {
    if (lineup.error && !lineup.data) return setStatus("down", lineup.error);
    if (!lineup.data) return setStatus("", "working it out…");
    const data = lineup.data;
    const text = data.settled
      ? `week ${data.week} · lineup is optimal`
      : `week ${data.week} · +${data.gain.toFixed(1)} available`;
    return setStatus(data.settled ? "live" : "stale", text);
  }
  if (!waivers.data) return setStatus("", "loading…");
  setStatus("live", `week ${waivers.data.week} · as of ${fmtTime(waivers.data.now)}`);
}

function selectTab(name) {
  state.tab = name;
  store("ffa-tab", name);
  document.querySelectorAll(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.tab === name));
  TABS.forEach((tab) => $(`#panel-${tab}`).classList.toggle("hidden", tab !== name));
  if (window.location.hash !== `#${name}`) window.history.replaceState({}, "", `#${name}`);
  // Only the visible tab polls: nothing is fetched for a screen nobody is looking at.
  window.clearTimeout(draft.timer);
  window.clearTimeout(waivers.timer);
  renderLeagues();
  if (name === "draft") pollDraft();
  else if (name === "waivers") loadWaivers();
  else if (!lineup.data) loadLineup();          // one build serves the lineup and the league
  else if (name === "league") renderLeague(lineup.data);
  tickStatus();
}

document.querySelector(".tabs").addEventListener("click", (event) => {
  const tab = event.target.closest(".tab");
  if (tab) selectTab(tab.dataset.tab);
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    window.clearTimeout(draft.timer);
    window.clearTimeout(waivers.timer);
  } else {
    selectTab(state.tab);
  }
});

window.setInterval(() => {
  renderClock();
  tickStatus();
}, 1000);

// Lineup is the default: a draft happens on a handful of days a year, a lineup every week.
loadLeagues();
const requested = (window.location.hash || "").slice(1);
selectTab(TABS.includes(requested) ? requested : TABS.includes(stored("ffa-tab")) ? stored("ffa-tab") : "lineup");
