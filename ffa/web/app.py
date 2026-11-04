"""A small WSGI app: JSON for both tools, plus the static phone UI.

No web framework and no new dependency — the stdlib serves it and httpx's WSGI transport
tests it, so the server is exercised offline like the rest of this project. It is read-only
in both directions: the Sleeper API cannot be written to, and this app has no route that
changes anything, so the worst a stranger on your network can do is read your league.

`ffa serve --token` turns on a shared secret for the few people who don't trust their LAN;
the browser picks it up from `?token=` once and sends it as a header afterwards.
"""

import json
import logging
import mimetypes
import socket
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from .. import __version__, espn
from ..config import ConfigError
from ..draft import board
from ..draft.session import SessionError, now_ms
from ..draft.shortlist import context_block
from ..leagues import directory_json as leagues_json
from ..lineup import ui as lineup_ui
from ..lineup.advice import NoRosterError
from ..lineup.source import LineupUnavailable
from ..sleeper import SleeperError
from ..waivers import ui as waivers_ui
from ..waivers.status import UnsupportedWaivers, select_statuses
from . import payload
from .state import AppState, FeedUnavailable

log = logging.getLogger(__name__)

DEFAULT_HOST = "0.0.0.0"  # the point of the exercise: reachable from the phone
DEFAULT_PORT = 8765
WAIVERS_SHOWN = 25
WAIVERS_MAX = 200
TOKEN_HEADER = "HTTP_X_FFA_TOKEN"
# Every response is per-request state, and the UI is edited while the server runs.
NO_STORE = ("Cache-Control", "no-store")
# These are expected, explainable failures: Sleeper down, ESPN down, no draft yet, a league
# whose settings aren't modeled. The phone shows the message instead of a blank screen.
EXPECTED_ERRORS = (
    FeedUnavailable,
    SleeperError,
    espn.ESPNError,
    UnsupportedWaivers,
    LineupUnavailable,
    NoRosterError,
    SessionError,
    ConfigError,
    board.UnsupportedDraftError,
    board.MalformedDraftError,
)


def create_app(state: AppState):
    """Build the WSGI callable for this state."""

    def app(environ, start_response):
        method = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO") or "/"
        query = parse_qs(environ.get("QUERY_STRING", ""))
        if method not in ("GET", "HEAD"):
            return _error(start_response, "405 Method Not Allowed", f"{method} is not supported; ffa only reads.")
        if not _authorized(state, environ, query):
            return _error(start_response, "401 Unauthorized", "This server needs a token: add ?token=… to the URL.")
        try:
            route = ROUTES.get(path)
            body = route(state, query, start_response) if route else _static(state, path, start_response)
            return [] if method == "HEAD" else body
        except EXPECTED_ERRORS as exc:
            return _error(start_response, "503 Service Unavailable", str(exc))
        except Exception:  # noqa: BLE001 - one bad request must not stop the server
            log.exception("Unhandled error serving %s", path)
            return _error(start_response, "500 Internal Server Error",
                          "Internal error. See cache/ffa.log for the traceback.")

    return app


# -- routes ---------------------------------------------------------------------------


def _health(state: AppState, query, start_response):
    return _json(start_response, {
        "ok": True,
        "version": __version__,
        "user": state.config.sleeper_username,
        "league_configured": state.config.league_id is not None,
        "poll_interval": state.draft.interval,
        "token_required": state.token is not None,
    })


def _draft(state: AppState, query, start_response):
    frame = state.draft.frame()
    return _json(start_response, payload.draft_json(
        frame.view,
        now_ms=now_ms(),
        note=frame.note,
        research=frame.research,
        research_status=frame.research_status,
        next_poll_in=frame.next_poll_in,
        budget_left=frame.budget_left,
    ))


def _draft_context(state: AppState, query, start_response):
    """The pasteable markdown block, for a second opinion in the Claude app."""
    view = state.draft.frame().view
    text = context_block(
        view.state,
        view.fill,
        view.shortlist,
        scoring_summary=view.session.scoring_summary,
        players=view.session.players,
        recent=view.recent,
        runs=view.runs,
    )
    return _text(start_response, text, "text/markdown; charset=utf-8")


def _waivers(state: AppState, query, start_response):
    report = state.waivers.report(_one(query, "league"))
    name = _one(query, "q")
    shown = select_statuses(
        report.statuses,
        positions=[p.upper() for p in query.get("pos", []) if p.strip()],
        names=[name] if name else (),
        limit=None if name else _limit(query),
    )
    return _json(start_response, {"ok": True, **waivers_ui.report_json(report, shown)})


def _leagues(state: AppState, query, start_response):
    """The leagues this server can show, so the phone offers names instead of ids."""
    refs = state.leagues.refs() if state.leagues is not None else []
    return _json(start_response, {"ok": True, "leagues": leagues_json(refs)})


def _lineup(state: AppState, query, start_response):
    advice, sources = state.lineup.advice(_one(query, "league"))
    return _json(start_response, {"ok": True, **lineup_ui.advice_json(advice, sources)})


ROUTES = {
    "/api/health": _health,
    "/api/draft": _draft,
    "/api/draft/context": _draft_context,
    "/api/waivers": _waivers,
    "/api/lineup": _lineup,
    "/api/leagues": _leagues,
}


# -- plumbing -------------------------------------------------------------------------


def _one(query: dict, name: str) -> str | None:
    values = [v.strip() for v in query.get(name, []) if v.strip()]
    return values[0] if values else None


def _limit(query: dict) -> int:
    raw = _one(query, "limit")
    try:
        return max(1, min(WAIVERS_MAX, int(raw))) if raw else WAIVERS_SHOWN
    except ValueError:
        return WAIVERS_SHOWN


def _authorized(state: AppState, environ, query) -> bool:
    if state.token is None:
        return True
    return environ.get(TOKEN_HEADER) == state.token or _one(query, "token") == state.token


def _json(start_response, body: dict, status: str = "200 OK"):
    raw = json.dumps(body).encode()
    start_response(status, [("Content-Type", "application/json; charset=utf-8"),
                            ("Content-Length", str(len(raw))), NO_STORE])
    return [raw]


def _text(start_response, text: str, content_type: str):
    raw = text.encode()
    start_response("200 OK", [("Content-Type", content_type),
                              ("Content-Length", str(len(raw))), NO_STORE])
    return [raw]


def _error(start_response, status: str, message: str):
    return _json(start_response, {"ok": False, "error": message}, status)


def _static(state: AppState, path: str, start_response):
    """Serve the UI out of `ffa/web/static`, and nothing else."""
    root = Path(state.static_dir).resolve()
    target = (root / (path.lstrip("/") or "index.html")).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        if path.startswith("/api/"):
            return _error(start_response, "404 Not Found", f"No such endpoint: {path}")
        return _error(start_response, "404 Not Found", f"Not found: {path}")
    content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    if content_type.startswith("text/") or content_type.endswith(("javascript", "json")):
        content_type += "; charset=utf-8"
    raw = target.read_bytes()
    start_response("200 OK", [("Content-Type", content_type),
                              ("Content-Length", str(len(raw))), NO_STORE])
    return [raw]


# -- the server itself ----------------------------------------------------------------


class _ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    """One thread per request: a Sleeper fetch must not block the other screens."""

    daemon_threads = True
    allow_reuse_address = True


class _QuietHandler(WSGIRequestHandler):
    """Access logs go to the log file, not into the terminal you're watching."""

    def log_message(self, format, *args):  # noqa: A002 - signature set by the stdlib
        log.info("%s %s", self.address_string(), format % args)


def local_ip() -> str | None:
    """The address a phone on the same network should use. Sends no packets."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def urls(host: str, port: int, token: str | None = None) -> list[str]:
    """The URLs to hand the user: localhost, plus the LAN address for the phone."""
    suffix = f"/?token={token}" if token else "/"
    hosts = ["localhost"] if host in ("0.0.0.0", "::", "") else [host]
    if host in ("0.0.0.0", "::", "") and (ip := local_ip()):
        hosts.append(ip)
    return [f"http://{h}:{port}{suffix}" for h in hosts]


def make_http_server(state: AppState, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
    """A threaded WSGI server for this state. Caller runs `serve_forever`."""
    return make_server(host, port, create_app(state),
                       server_class=_ThreadingWSGIServer, handler_class=_QuietHandler)
