"""The leagues a run knows about, by id and by name.

Managers are usually in more than one league, and a Sleeper league id is nineteen digits.
Nobody is typing that into a phone, so `SLEEPER_LEAGUE_IDS` lists them once and everything
else refers to them the way a person does -- `--league dynasty`, or a tap on a chip.

Names come from Sleeper rather than from configuration: the league already has a name, and
a second place to keep it is a second place for it to be wrong.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .config import Config
from .sleeper import SleeperError

log = logging.getLogger(__name__)


class UnknownLeague(Exception):
    """A `--league` value that is neither an id nor the name of a configured league."""


class LeagueSource(Protocol):
    def league(self, league_id: str) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class LeagueRef:
    league_id: str
    name: str
    is_default: bool = False
    available: bool = True  # False when Sleeper wouldn't tell us about it

    @property
    def label(self) -> str:
        return self.name or self.league_id


def directory(client: LeagueSource, config: Config) -> list[LeagueRef]:
    """Every configured league, default first, each with the name Sleeper gives it.

    One unreachable league must not cost you the others, so a failure becomes a ref with no
    name rather than an exception: the picker still shows it, and asking for it reports the
    real error then.
    """
    refs: list[LeagueRef] = []
    for league_id in config.league_ids or ([config.league_id] if config.league_id else []):
        try:
            name = (client.league(league_id) or {}).get("name") or ""
            refs.append(LeagueRef(league_id, name, league_id == config.league_id))
        except SleeperError as exc:
            log.warning("Could not read league %s: %s", league_id, exc)
            refs.append(LeagueRef(league_id, "", league_id == config.league_id, available=False))
    return refs


def resolve(client: LeagueSource, config: Config, requested: str | None) -> str:
    """Turn `--league` into a league id: an id passes through, a name is looked up.

    Matching is case-insensitive and tries exact, then prefix, then substring, so a short
    nickname and a fragment of the full name both work, while an abbreviation that fits two
    leagues is refused rather than guessed at.
    """
    wanted = (requested or "").strip()
    if not wanted:
        if config.league_id is None:
            raise UnknownLeague("No league configured. Pass --league or set SLEEPER_LEAGUE_ID.")
        return config.league_id
    if wanted.isdigit():
        return wanted
    refs = [ref for ref in directory(client, config) if ref.name]
    folded = wanted.casefold()
    for match in (
        [r for r in refs if r.name.casefold() == folded],
        [r for r in refs if r.name.casefold().startswith(folded)],
        [r for r in refs if folded in r.name.casefold()],
    ):
        if len(match) == 1:
            return match[0].league_id
        if len(match) > 1:
            raise UnknownLeague(
                f"{wanted!r} matches more than one league: {_names(match)}. Use the full name."
            )
    known = f" Configured: {_names(refs)}." if refs else ""
    raise UnknownLeague(f"No configured league is called {wanted!r}.{known}")


def _names(refs: Sequence[LeagueRef]) -> str:
    return ", ".join(repr(ref.label) for ref in refs)


def directory_json(refs: Sequence[LeagueRef]) -> list[dict[str, Any]]:
    return [
        {"league_id": ref.league_id, "name": ref.label,
         "default": ref.is_default, "available": ref.available}
        for ref in refs
    ]
