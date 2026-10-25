"""Environment loading and validation.

Configuration comes from the process environment, with an optional `.env` file filling in
anything the environment doesn't set. Every problem is collected and reported at once so a
bad config fails loudly before a draft session starts.
"""

import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MODEL = "claude-sonnet-5"
# The server-side web search tool type is versioned per model generation. Current models
# (Sonnet 5, Opus 4.6+) take the dynamic-filtering variant; older ones only the basic one.
WEB_SEARCH_TOOL_TYPE = "web_search_20260209"
WEB_SEARCH_TOOL_TYPE_LEGACY = "web_search_20250305"
WEB_SEARCH_MODERN_MODELS = ("claude-sonnet-5", "claude-opus-5", "claude-opus-4-8",
                            "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6",
                            "claude-fable-5")
WEB_SEARCH_MAX_USES = 4  # 3-5 searches is plenty per request; bounds latency and cost
RESEARCH_EFFORT = "low"  # a draft clock is 120s; depth is not the constraint here
RESEARCH_TIMEOUT_SECONDS = 90.0  # for a background prefetch; `recommend` degrades at 5s


def web_search_tool_type(model: str) -> str:
    """Pick the web search tool version this model accepts."""
    return WEB_SEARCH_TOOL_TYPE if model.startswith(WEB_SEARCH_MODERN_MODELS) else WEB_SEARCH_TOOL_TYPE_LEGACY
DEFAULT_POLL_INTERVAL_SECONDS = 3.0
MIN_POLL_INTERVAL_SECONDS = 1.0  # never poll in a tight loop
MAX_POLL_INTERVAL_SECONDS = 30.0  # slower than this misses picks on a 60s timer
DEFAULT_RESEARCH_CONCURRENCY = 4
MAX_RESEARCH_CONCURRENCY = 16
DEFAULT_RESEARCH_BUDGET = 60
DEFAULT_CACHE_DIR = Path("cache")

_SLEEPER_ID = re.compile(r"^\d+$")
_USERNAME = re.compile(r"^[^\s/?#]+$")


class ConfigError(Exception):
    """One or more configuration problems, all reported together."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("Invalid configuration:\n" + "\n".join(f"  - {p}" for p in problems))


@dataclass(frozen=True)
class Config:
    sleeper_username: str
    league_id: str | None
    draft_id: str | None
    anthropic_api_key: str | None
    model: str
    poll_interval_seconds: float
    research_concurrency: int
    research_budget: int
    cache_dir: Path = DEFAULT_CACHE_DIR

    def require_anthropic_key(self) -> str:
        if not self.anthropic_api_key:
            raise ConfigError(["ANTHROPIC_API_KEY is required for `recommend`."])
        return self.anthropic_api_key


def anthropic_credentials_available(environ: Mapping[str, str] | None = None) -> bool:
    """Is there any credential the Anthropic SDK can resolve?

    An unset ANTHROPIC_API_KEY does not mean there are no credentials: the SDK also accepts
    ANTHROPIC_AUTH_TOKEN, an OAuth profile from `ant auth login`, and workload identity
    federation. Checking here keeps `watch` from starting research it can never complete.
    """
    env = os.environ if environ is None else environ
    if env.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    if env.get("ANTHROPIC_IDENTITY_TOKEN") or env.get("ANTHROPIC_IDENTITY_TOKEN_FILE"):
        return True
    profiles = Path(env.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "anthropic"
    return profiles.is_dir() and any(profiles.iterdir())


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a minimal `.env` file: KEY=VALUE lines, `#` comments, optional quotes."""
    values: dict[str, str] = {}
    for lineno, raw_line in enumerate(path.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key:
            raise ConfigError([f"{path}:{lineno}: expected KEY=VALUE, got {raw_line!r}"])
        value = value.strip()
        closing = value.find(value[0], 1) if value[:1] in ("'", '"') else -1
        if closing != -1:
            value = value[1:closing]  # quoted values keep any '#' inside them
        else:
            value = re.split(r"(?:^|\s+)#", value, maxsplit=1)[0].strip()
        values[key] = value
    return values


def load_config(
    environ: Mapping[str, str] | None = None,
    env_file: Path | None = Path(".env"),
) -> Config:
    """Build a validated Config. Real environment variables win over the `.env` file."""
    merged: dict[str, str] = {}
    if env_file is not None and env_file.is_file():
        merged.update(parse_env_file(env_file))
    merged.update(os.environ if environ is None else environ)

    def get(name: str) -> str | None:
        value = merged.get(name, "").strip()
        return value or None

    problems: list[str] = []

    username = get("SLEEPER_USERNAME")
    if username is None:
        problems.append("SLEEPER_USERNAME is required.")
    elif not _USERNAME.match(username):
        problems.append(f"SLEEPER_USERNAME {username!r} contains whitespace or URL characters.")

    league_id = get("SLEEPER_LEAGUE_ID")
    draft_id = get("DRAFT_ID")
    if league_id is None and draft_id is None:
        problems.append("Set SLEEPER_LEAGUE_ID, or DRAFT_ID for a mock draft.")
    for name, value in (("SLEEPER_LEAGUE_ID", league_id), ("DRAFT_ID", draft_id)):
        if value is not None and not _SLEEPER_ID.match(value):
            problems.append(f"{name} must be the numeric Sleeper ID, got {value!r}.")

    def number(name, kind, default, minimum, maximum=None):
        raw = get(name)
        if raw is None:
            return default
        try:
            value = kind(raw)
        except ValueError:
            problems.append(f"{name} must be {'an integer' if kind is int else 'a number'}, got {raw!r}.")
            return default
        if not math.isfinite(value) or value < minimum or (maximum is not None and value > maximum):
            bounds = f"at least {minimum}" if maximum is None else f"between {minimum} and {maximum}"
            problems.append(f"{name} must be {bounds}, got {raw!r}.")
        return value

    poll_interval = number(
        "POLL_INTERVAL_SECONDS", float, DEFAULT_POLL_INTERVAL_SECONDS,
        MIN_POLL_INTERVAL_SECONDS, MAX_POLL_INTERVAL_SECONDS,
    )
    concurrency = number("RESEARCH_CONCURRENCY", int, DEFAULT_RESEARCH_CONCURRENCY, 1, MAX_RESEARCH_CONCURRENCY)
    budget = number("RESEARCH_BUDGET", int, DEFAULT_RESEARCH_BUDGET, 0)

    if problems:
        raise ConfigError(problems)

    return Config(
        sleeper_username=username,
        league_id=league_id,
        draft_id=draft_id,
        anthropic_api_key=get("ANTHROPIC_API_KEY"),
        model=get("DRAFTBOT_MODEL") or DEFAULT_MODEL,
        poll_interval_seconds=poll_interval,
        research_concurrency=concurrency,
        research_budget=budget,
    )
