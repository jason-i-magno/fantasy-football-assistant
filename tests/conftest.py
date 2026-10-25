import json
from pathlib import Path

import pytest

from ffa.draft.session import load_recording

FIXTURES = Path(__file__).parent / "fixtures"
REAL_DRAFT = FIXTURES / "sleeper_friends_2018"


def read_fixture(*parts: str):
    return json.loads(FIXTURES.joinpath(*parts).read_text())


@pytest.fixture(scope="session")
def players_fixture() -> dict:
    """Trimmed real /players/nfl payload, in on-disk cache shape."""
    return read_fixture("players_nfl.json")


@pytest.fixture(scope="session")
def recording():
    return load_recording(FIXTURES / "replay_mock_12team.jsonl")


@pytest.fixture(scope="session")
def mock_session(recording):
    return recording.session()


@pytest.fixture(scope="session")
def real_draft() -> dict:
    """A real, completed 12-team Sleeper draft recorded from the live API."""
    return {
        name: json.loads((REAL_DRAFT / f"{name}.json").read_text())
        for name in ("league", "league_users", "league_drafts", "draft", "picks", "traded_picks")
    }
