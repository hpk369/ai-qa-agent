"""
Tests for agent.env — loading a local .env without leaking it.

The important properties: a real environment variable always beats the
file, a missing file is normal, and the loader reports variable *names*
rather than values so nothing downstream can log a secret by accident.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import pytest

from agent.env import load_env, parse_env


@pytest.fixture(autouse=True)
def restore_environ():
    """load_env writes straight into os.environ, so snapshot and restore it
    exactly. (monkeypatch.delenv would *re-set* the value at teardown,
    leaking it into every test that runs after this module.)"""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


def test_parse_handles_what_people_actually_write():
    parsed = parse_env(
        "# a comment\n"
        "\n"
        "ANTHROPIC_API_KEY=sk-ant-plain\n"
        "export LLM_MODEL=llama3.2\n"
        '  SLACK_BOT_TOKEN = "xoxb-quoted"  \n'
        "LLM_BASE_URL='http://localhost:11434/v1'\n"
        "EMPTY=\n"
        "WITH_EQUALS=a=b=c\n"
        "not a variable line\n"
    )
    assert parsed == {
        "ANTHROPIC_API_KEY": "sk-ant-plain",
        "LLM_MODEL": "llama3.2",
        "SLACK_BOT_TOKEN": "xoxb-quoted",
        "LLM_BASE_URL": "http://localhost:11434/v1",
        "EMPTY": "",
        "WITH_EQUALS": "a=b=c",
    }


def test_load_sets_variables_from_the_file(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=sk-ant-from-file\n")

    applied = load_env(env_file)

    assert applied == ["ANTHROPIC_API_KEY"]
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-from-file"


def test_the_real_environment_wins(tmp_path, monkeypatch):
    """An exported key, or a CI secret, must beat a stale checked-out file."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-exported")
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=sk-ant-from-file\n")

    applied = load_env(env_file)

    assert applied == []
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-exported"


def test_override_is_available_but_not_the_default(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "exported")
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_MODEL=from-file\n")

    load_env(env_file, override=True)
    assert os.environ["LLM_MODEL"] == "from-file"


def test_blank_values_are_skipped_not_exported(tmp_path, monkeypatch):
    """A blank placeholder is "unset", not "set to empty" — an exported
    ANTHROPIC_API_KEY="" would shadow workload identity federation and
    authenticate with an empty key."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=\nLLM_MODEL=llama3.2\nAGENT_PUBLIC_URL=   \n")

    applied = load_env(env_file)

    assert applied == ["LLM_MODEL"]
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert "AGENT_PUBLIC_URL" not in os.environ


def test_a_missing_file_is_not_an_error(tmp_path):
    assert load_env(tmp_path / "nope.env") == []


def test_a_directory_in_place_of_the_file_is_not_an_error(tmp_path):
    (tmp_path / "adir").mkdir()
    assert load_env(tmp_path / "adir" / ".env") == []


def test_it_reports_names_never_values(tmp_path, monkeypatch):
    """Callers may print the return value; it must not carry a secret."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=sk-ant-supersecret\n")

    applied = load_env(env_file)

    assert "sk-ant-supersecret" not in " ".join(applied)


@pytest.mark.parametrize("module", [
    "agent/llm.py", "agent/providers.py", "agent/incident.py",
    "agent/slack_client.py", "logsets/triage.py", "logsets/stream.py",
])
def test_library_modules_never_load_the_env_file(module):
    """Only entry points call load_env. Importing a library module in a
    test must not pull a developer's real credentials into the process."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / module).read_text()
    assert "load_env()" not in source, f"{module} loads .env at import time"


@pytest.mark.parametrize("script", [
    "scripts/logset.py", "scripts/stream.py", "scripts/slack_reply.py", "scripts/fetch_logs.py",
])
def test_every_entry_point_loads_the_file(script):
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / script).read_text()
    assert "load_env()" in source, f"{script} would ignore .env"
