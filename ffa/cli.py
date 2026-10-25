"""Command line entry point."""

import argparse
import json
import logging
import time
from dataclasses import replace
from pathlib import Path

from rich.console import Console
from rich.json import JSON
from rich.live import Live
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table

from .draft import board, ui
from .config import (
    DEFAULT_CACHE_DIR,
    Config,
    ConfigError,
    anthropic_credentials_available,
    load_config,
)
from .draft.session import (
    DraftView,
    load_exclusions,
    LiveSource,
    Recorder,
    ReplaySource,
    Session,
    SessionError,
    compute_view,
    load_recording,
    now_ms,
    open_session,
    recording_header,
    resolve_draft_id,
)
from .draft.shortlist import context_block
from .sleeper import SleeperClient, SleeperError, SleeperRateLimited

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2

RATE_LIMIT_BACKOFF_SECONDS = 30.0
# Spec: a `recommend` that can't complete in 5s degrades to shortlist output rather than
# burn the clock. `watch` keeps research warm, so a warm result returns instantly.
RECOMMEND_DEADLINE_SECONDS = 5.0
FRAME_SECONDS = 1.0  # redraw (and tick the countdown) this often between polls


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    console = Console()
    _setup_logging(args.verbose)
    try:
        return args.func(args, console)
    except ConfigError as exc:
        console.print(f"[bold red]{exc}")
        console.print("[dim]See .env.example for the expected settings.")
        return EXIT_CONFIG
    except (board.UnsupportedDraftError, board.MalformedDraftError) as exc:
        console.print(f"[bold red]{exc}")
        return EXIT_ERROR
    except (SleeperError, SessionError) as exc:
        console.print(f"[bold red]{exc}")
        return EXIT_ERROR
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.")
        return EXIT_OK


# -- draft commands ------------------------------------------------------------------


def _exclusions(args, config: Config) -> list[str]:
    """--exclude plus anything in cache/exclude.txt."""
    return [*(getattr(args, "exclude", None) or []), *load_exclusions(config.cache_dir)]


def _config_for(args) -> Config:
    """Load config, letting --user stand in for SLEEPER_USERNAME.

    One checkout, two managers: each runs their own `watch` against the same draft.
    """
    config = load_config(env_file=args.env_file)
    user = getattr(args, "user", None)
    return replace(config, sleeper_username=user) if user else config


def cmd_doctor(args, console: Console) -> int:
    config = _config_for(args)
    problems: list[str] = []

    table = Table(box=None, show_header=False)
    table.add_column(style="bold")
    table.add_column()
    table.add_row("username", config.sleeper_username)
    table.add_row("league_id", config.league_id or "[dim](unset)")
    table.add_row("draft_id override", config.draft_id or "[dim](unset)")
    table.add_row("model", config.model)
    table.add_row("poll interval", f"{config.poll_interval_seconds:g}s")
    table.add_row("research", f"concurrency {config.research_concurrency}, budget {config.research_budget}")
    table.add_row("anthropic key", "set" if config.anthropic_api_key else "[yellow]not set")
    console.print(table)

    client = SleeperClient(config.cache_dir)
    user = client.user(config.sleeper_username)
    console.print(f"\n[green]✓[/green] user [bold]{user.get('display_name')}[/bold] -> user_id {user['user_id']}")

    draft_id = resolve_draft_id(config, client)
    session = open_session(config, client)
    raw_draft = client.draft(draft_id)
    console.print(f"[green]✓[/green] draft_id [bold]{draft_id}[/bold]")
    if session.league_name:
        console.print(f"[green]✓[/green] league [bold]{session.league_name}[/bold] ({session.league_id})")
    else:
        console.print("[yellow]•[/yellow] no league attached (mock draft): roster slots come from draft settings")

    console.print("\n[bold]Raw draft settings[/bold] [dim](eyeball this before the draft)[/dim]")
    console.print(JSON(json.dumps(raw_draft.get("settings") or {}, sort_keys=True)))

    settings = board.parse_draft(raw_draft)
    console.print(
        f"[green]✓[/green] {settings.draft_type} draft, {settings.teams} teams, {settings.rounds} rounds, "
        f"pick timer {settings.pick_timer}s"
        + (f", reversal from round {settings.reversal_round}" if settings.reversal_round else "")
    )
    for warning in board.settings_warnings(settings):
        console.print(f"[yellow]![/yellow] {warning}")

    console.print(f"[green]✓[/green] scoring: {session.scoring_summary}")
    console.print(f"[green]✓[/green] starters: {session.requirements.describe()}")
    if session.requirements.ignored:
        console.print(f"[yellow]![/yellow] roster slots not modeled: {sorted(set(session.requirements.ignored))}")

    if settings.draft_order is None:
        console.print("[yellow]![/yellow] draft_order is not set yet (commissioner hasn't randomized). `watch` will poll for it.")
    else:
        slot = settings.draft_order.get(session.user_id)
        if slot is None:
            problems.append(
                f"{config.sleeper_username} (user_id {session.user_id}) is not in this draft's draft_order. "
                "Wrong league or a co-owner account? Use --slot N to override."
            )
            console.print(f"[red]✗[/red] {problems[-1]}")
        else:
            console.print(
                f"[green]✓[/green] your draft slot: [bold]{slot}[/bold] "
                f"(roster_id {settings.slot_to_roster_id.get(slot)})"
            )

    picks = client.picks(draft_id)
    traded = client.traded_picks(draft_id, pick_count=len(picks))
    console.print(f"[green]✓[/green] {len(picks)} picks made, {len(traded)} traded picks, status [bold]{settings.status}[/bold]")

    console.print(f"[green]✓[/green] player map: {len(session.players)} fantasy-relevant players, fetched {session.players_date}")

    note = _check_anthropic(config)
    console.print(f"[yellow]![/yellow] {note}" if note else f"[green]✓[/green] Anthropic API key and model {config.model} look valid")

    if problems:
        console.print(f"\n[bold red]{len(problems)} problem(s) to fix before draft day.")
        return EXIT_ERROR
    console.print("\n[bold green]All checks passed.[/bold green] Next: run `ffa draft watch` against a mock draft.")
    return EXIT_OK


def cmd_watch(args, console: Console) -> int:
    config = _config_for(args)
    client = SleeperClient(config.cache_dir)
    session = open_session(config, client)
    _print_startup(console, session, client)

    recorder = None
    if args.record:
        recorder = Recorder(Path(args.record), recording_header(session, client))
        console.print(f"[dim]Recording raw snapshots to {args.record}")

    worker = _research_worker(config, console)
    try:
        return _run_loop(
            console,
            session,
            LiveSource(client, session.draft_id),
            interval=config.poll_interval_seconds,
            my_slot=args.slot,
            recorder=recorder,
            worker=worker,
            excluded=lambda: _exclusions(args, config),
        )
    finally:
        if worker is not None:
            worker.shutdown()


def cmd_shortlist(args, console: Console) -> int:
    config = _config_for(args)
    client = SleeperClient(config.cache_dir)
    session = open_session(config, client)
    view = compute_view(session, LiveSource(client, session.draft_id).poll(),
                        my_slot=args.slot, excluded=_exclusions(args, config))
    _print_shortlist(console, view)
    return EXIT_OK


def cmd_recommend(args, console: Console) -> int:
    """Research the shortlist and print a pick. Degrades to `shortlist` rather than stall."""
    config = _config_for(args)
    client = SleeperClient(config.cache_dir)
    session = open_session(config, client)
    view = compute_view(session, LiveSource(client, session.draft_id).poll(),
                        my_slot=args.slot, excluded=_exclusions(args, config))

    worker = _research_worker(config, console)
    research, status = None, "off"
    if worker is not None:
        try:
            wait = args.wait
            with console.status(f"[cyan]Researching {len(view.shortlist.candidates)} candidates "
                                f"(up to {wait:g}s)…"):
                research = worker.wait(view, wait)
            status = worker.status(view)
            if research is None:
                console.print(
                    f"[yellow]Research didn't finish within {wait:g}s — showing the heuristic "
                    "shortlist. `ffa draft watch` keeps research warm ahead of your pick; "
                    "`--wait 60` waits longer.[/yellow]"
                )
                view = replace(view, research_note=worker.failure(view) or "timed out")
        finally:
            worker.shutdown()
    _print_shortlist(console, view, research=research, status=status)
    return EXIT_OK


def _research_worker(config: Config, console: Console):
    """Build the prefetch worker, or explain why research is off. Never fatal."""
    if not config.anthropic_api_key and not anthropic_credentials_available():
        console.print(
            "[dim]Research off: no Anthropic credentials. Set ANTHROPIC_API_KEY in .env, or "
            "run `ant auth login`. `shortlist` still prints a block to paste into Claude."
        )
        return None
    if config.research_budget <= 0:
        console.print("[dim]Research off: RESEARCH_BUDGET is 0.")
        return None
    try:
        from .draft.research import ResearchClient, ResearchWorker

        research_client = ResearchClient(
            config.anthropic_api_key, config.model, config.research_budget
        )
        return ResearchWorker(research_client, config.research_concurrency)
    except Exception as exc:  # noqa: BLE001 - research is an enhancement, never a dependency
        log.warning("Research layer unavailable", exc_info=True)
        console.print(f"[yellow]Research unavailable ({exc}); continuing without it.")
        return None


def cmd_replay(args, console: Console) -> int:
    recording = load_recording(args.file)
    session = recording.session()
    console.print(
        f"[dim]Replaying {len(recording.snapshots)} snapshots from {args.file} "
        f"(draft {session.draft_id}, player map {session.players_date})"
    )
    if args.snapshot is not None:
        if not 0 <= args.snapshot < len(recording.snapshots):
            raise SessionError(f"--snapshot must be 0..{len(recording.snapshots) - 1}")
        view = compute_view(session, recording.snapshots[args.snapshot], my_slot=args.slot)
        _print_shortlist(console, view)
        return EXIT_OK
    return _run_loop(
        console,
        session,
        ReplaySource(recording.snapshots),
        interval=args.delay,
        my_slot=args.slot,
        use_snapshot_clock=True,
    )


# -- shared plumbing -----------------------------------------------------------------


def _print_shortlist(console: Console, view: DraftView, research=None, status: str = "off") -> None:
    context = context_block(
        view.state,
        view.fill,
        view.shortlist,
        scoring_summary=view.session.scoring_summary,
        players=view.session.players,
        recent=view.recent,
        runs=view.runs,
    )
    ui.print_one_shot(console, view, context, research=research, research_status=status)


def _print_startup(console: Console, session: Session, client: SleeperClient) -> None:
    raw = client.draft(session.draft_id)
    console.print(
        f"[bold]ffa draft[/bold] · {session.league_name or 'mock draft'} · draft {session.draft_id} · "
        f"you are {session.display_name}"
    )
    console.print("[dim]raw draft settings:[/dim]")
    console.print(JSON(json.dumps(raw.get("settings") or {}, sort_keys=True)))


def _run_loop(
    console: Console,
    session: Session,
    source,
    *,
    interval: float,
    my_slot: int | None = None,
    recorder: Recorder | None = None,
    worker=None,
    excluded=None,
    use_snapshot_clock: bool = False,
    sleep=time.sleep,
    monotonic=time.monotonic,
    max_frames: int | None = None,
) -> int:
    """Poll, rebuild, render. Polls on `interval`; redraws every second so the clock ticks."""
    view: DraftView | None = None
    note: str | None = None
    next_poll = 0.0
    errors = 0
    frames = 0

    # screen=False keeps the final board on screen after Ctrl-C instead of restoring the
    # terminal; the Layout fills the window, so there is nothing to overflow.
    with Live(console=console, auto_refresh=False, screen=False) as live:
        while True:
            now = monotonic()
            if now >= next_poll:
                try:
                    snapshot = source.poll()
                    view = compute_view(session, snapshot, my_slot=my_slot,
                                        excluded=excluded() if excluded else ())
                    if recorder is not None and recorder.add(snapshot):
                        log.debug("recorded snapshot at %s picks", len(snapshot.picks))
                    note, errors = None, 0
                    next_poll = now + interval
                except SleeperRateLimited as exc:
                    errors += 1
                    wait = max(exc.retry_after or 0.0, RATE_LIMIT_BACKOFF_SECONDS * errors)
                    note = f"rate limited by Sleeper — backing off {wait:.0f}s"
                    next_poll = now + wait
                except SleeperError as exc:
                    errors += 1
                    note = f"{exc} (retry {errors})"
                    next_poll = now + interval * min(errors, 5)
                except board.UnsupportedDraftError:
                    raise  # the pick math can't work here at all; bail out loudly
                # A MalformedDraftError is one bad payload, not a bad draft: it falls through
                # to the retry below rather than ending the session.
                except Exception as exc:  # noqa: BLE001 - a live draft must not die on us
                    errors += 1
                    log.exception("Unexpected failure while rebuilding the board")
                    note = f"internal error: {exc} (see {DEFAULT_CACHE_DIR / 'ffa.log'})"
                    next_poll = now + interval * min(errors, 5)

            frames += 1
            if view is not None:
                research, status = None, "off"
                if worker is not None:
                    # Start research for the current shortlist now, during someone else's
                    # pick, so it is already warm when the clock comes round.
                    worker.ensure(view)
                    research, status = worker.get(view), worker.status(view)
                    if status == "failed":
                        view = replace(view, research_note=worker.failure(view))
                frame_ms = view.fetched_at_ms if use_snapshot_clock else now_ms()
                live.update(
                    ui.dashboard(view, now_ms=frame_ms, note=note, width=console.width,
                                 height=console.height, research=research, research_status=status),
                    refresh=True,
                )
                if view.state.is_complete or getattr(source, "finished", False):
                    break
            else:
                # Nothing rendered yet: say why rather than showing an empty screen.
                live.update(Panel(note or "Waiting for the first poll...", style="yellow"), refresh=True)
            if max_frames is not None and frames >= max_frames:
                break
            sleep(max(0.05, min(FRAME_SECONDS, next_poll - monotonic())))

    if view is not None and view.state.is_complete:
        console.print("[dim]Draft complete.")
    return EXIT_OK


def _check_anthropic(config: Config) -> str | None:
    """Preflight the research credentials; returns a warning string or None when healthy."""
    if not config.anthropic_api_key:
        return "ANTHROPIC_API_KEY is not set — `recommend` will be unavailable, `shortlist` still works."
    try:
        import anthropic
    except ImportError:  # pragma: no cover - dependency is declared
        return "the `anthropic` package is not installed."
    client = anthropic.Anthropic(api_key=config.anthropic_api_key)
    try:
        client.models.retrieve(config.model)
    except anthropic.AuthenticationError:
        return "ANTHROPIC_API_KEY was rejected (401)."
    except anthropic.NotFoundError:
        return f"model {config.model!r} was not found — set FFA_MODEL to a model your key can use."
    except anthropic.APIError as exc:
        return f"could not reach the Anthropic API: {exc}"
    return None


def _setup_logging(verbose: bool) -> None:
    DEFAULT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(DEFAULT_CACHE_DIR / "ffa.log")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    file_handler.setLevel(logging.DEBUG)
    console_handler = RichHandler(show_path=False, show_time=False, level=logging.INFO if verbose else logging.WARNING)
    logging.basicConfig(level=logging.DEBUG, handlers=[file_handler, console_handler], force=True)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    # Shared options are accepted on either side of the subcommand; SUPPRESS keeps the
    # subparser copy from clobbering a value given before it.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--env-file", type=Path, default=argparse.SUPPRESS, help="defaults to ./.env")
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
                        help="log at INFO to the console")

    parser = argparse.ArgumentParser(
        prog="ffa", parents=[common], description="Fantasy football assistant for Sleeper leagues."
    )
    tools = parser.add_subparsers(dest="tool", required=True)

    draft = tools.add_parser("draft", parents=[common], help="live assistant for Sleeper snake drafts")
    sub = draft.add_subparsers(dest="command", required=True)

    slot_help = "view the board as this draft slot (mock drafts, replays, co-owner accounts)"
    user_help = "Sleeper username to run as, overriding SLEEPER_USERNAME"

    doctor = sub.add_parser("doctor", parents=[common],
                            help="validate config, resolve IDs, dump raw draft settings")
    doctor.add_argument("--user", help=user_help)
    doctor.set_defaults(func=cmd_doctor)

    watch = sub.add_parser("watch", parents=[common], help="live board, polled continuously")
    watch.add_argument("--slot", type=int, help=slot_help)
    watch.add_argument("--record", type=Path, help="append raw API snapshots to this file")
    watch.add_argument("--user", help=user_help)
    watch.add_argument("--exclude", action="append", metavar="NAME",
                            help="keep a player off the board (repeatable); also reads cache/exclude.txt")
    watch.set_defaults(func=cmd_watch)

    shortlist = sub.add_parser("shortlist", parents=[common],
                               help="one-shot candidates plus a pasteable context block")
    shortlist.add_argument("--slot", type=int, help=slot_help)
    shortlist.add_argument("--user", help=user_help)
    shortlist.add_argument("--exclude", action="append", metavar="NAME",
                            help="keep a player off the board (repeatable); also reads cache/exclude.txt")
    shortlist.set_defaults(func=cmd_shortlist)

    recommend = sub.add_parser("recommend", parents=[common],
                               help="one-shot researched recommendation")
    recommend.add_argument("--slot", type=int, help=slot_help)
    recommend.add_argument("--wait", type=float, default=RECOMMEND_DEADLINE_SECONDS,
                           help="seconds to wait for research before degrading to the shortlist")
    recommend.add_argument("--user", help=user_help)
    recommend.add_argument("--exclude", action="append", metavar="NAME",
                            help="keep a player off the board (repeatable); also reads cache/exclude.txt")
    recommend.set_defaults(func=cmd_recommend)

    replay = sub.add_parser("replay", parents=[common], help="replay a recording offline, no network")
    replay.add_argument("file", type=Path)
    replay.add_argument("--snapshot", type=int, help="render just this snapshot index")
    replay.add_argument("--delay", type=float, default=1.0, help="seconds between frames")
    replay.add_argument("--slot", type=int, help=slot_help)
    replay.set_defaults(func=cmd_replay)

    args = parser.parse_args(argv)
    args.env_file = getattr(args, "env_file", Path(".env"))
    args.verbose = getattr(args, "verbose", False)
    return args


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
