"""Config loading and validation: a bad config must fail loudly, before draft day."""

from pathlib import Path

import pytest

from draftbot.config import DEFAULT_MODEL, ConfigError, load_config, parse_env_file

GOOD = {"SLEEPER_USERNAME": "jason", "SLEEPER_LEAGUE_ID": "123456789"}
ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"


def load(**overrides):
    return load_config({**GOOD, **overrides}, env_file=None)


def test_defaults():
    config = load()
    assert config.model == DEFAULT_MODEL
    assert config.poll_interval_seconds == 3.0
    assert config.research_concurrency == 4
    assert config.research_budget == 60
    assert config.anthropic_api_key is None


def test_draft_id_alone_is_enough():
    config = load_config({"SLEEPER_USERNAME": "jason", "DRAFT_ID": "987"}, env_file=None)
    assert config.league_id is None and config.draft_id == "987"


def test_every_problem_is_reported_at_once():
    with pytest.raises(ConfigError) as err:
        load_config({"POLL_INTERVAL_SECONDS": "0.1", "RESEARCH_CONCURRENCY": "nope"}, env_file=None)
    problems = err.value.problems
    assert len(problems) == 4  # username, league/draft id, poll interval, concurrency
    assert any("SLEEPER_USERNAME" in p for p in problems)
    assert any("SLEEPER_LEAGUE_ID" in p for p in problems)
    assert any("POLL_INTERVAL_SECONDS" in p for p in problems)
    assert any("RESEARCH_CONCURRENCY" in p for p in problems)


@pytest.mark.parametrize("value", ["0.5", "120", "nan", "abc"])
def test_bad_poll_intervals_are_rejected(value):
    with pytest.raises(ConfigError, match="POLL_INTERVAL_SECONDS"):
        load(POLL_INTERVAL_SECONDS=value)


def test_non_numeric_ids_are_rejected():
    with pytest.raises(ConfigError, match="SLEEPER_LEAGUE_ID"):
        load(SLEEPER_LEAGUE_ID="https://sleeper.com/leagues/123")


def test_empty_values_count_as_unset():
    with pytest.raises(ConfigError, match="SLEEPER_USERNAME"):
        load(SLEEPER_USERNAME="   ")


def test_require_anthropic_key():
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        load().require_anthropic_key()
    assert load(ANTHROPIC_API_KEY="sk-test").require_anthropic_key() == "sk-test"


def test_env_file_is_parsed_and_overridden_by_real_environment(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# comment\n"
        "SLEEPER_USERNAME=fromfile    # trailing comment\n"
        "SLEEPER_LEAGUE_ID='123'\n"
        "export DRAFT_ID=456\n"
        "DRAFTBOT_MODEL=\"claude-test\"\n"
        "\n"
    )
    values = parse_env_file(env)
    assert values["SLEEPER_USERNAME"] == "fromfile"
    assert values["SLEEPER_LEAGUE_ID"] == "123"
    assert values["DRAFT_ID"] == "456"
    assert values["DRAFTBOT_MODEL"] == "claude-test"

    config = load_config({"SLEEPER_USERNAME": "fromenv"}, env_file=env)
    assert config.sleeper_username == "fromenv"  # real environment wins
    assert config.model == "claude-test"


def test_env_example_ships_with_every_value_unset(tmp_path):
    """Copying .env.example verbatim must read as 'nothing set', not as comment text."""
    values = parse_env_file(ENV_EXAMPLE)
    assert values["SLEEPER_USERNAME"] == ""
    assert values["DRAFT_ID"] == ""
    assert values["POLL_INTERVAL_SECONDS"] == "3"
    with pytest.raises(ConfigError) as err:
        load_config({}, env_file=ENV_EXAMPLE)
    assert any("SLEEPER_USERNAME is required" in p for p in err.value.problems)


def test_quoted_values_keep_hashes(tmp_path):
    env = tmp_path / ".env"
    env.write_text('ANTHROPIC_API_KEY="sk-a#b"\n')
    assert parse_env_file(env)["ANTHROPIC_API_KEY"] == "sk-a#b"


def test_malformed_env_line_is_rejected(tmp_path):
    env = tmp_path / ".env"
    env.write_text("SLEEPER_USERNAME\n")
    with pytest.raises(ConfigError, match="KEY=VALUE"):
        parse_env_file(env)
