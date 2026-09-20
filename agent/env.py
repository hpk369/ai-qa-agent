"""
Load a local .env at the entry points, so a key never has to be typed on
a command line.

Deliberately dependency-free and deliberately *not* run on library
import: only the CLIs and the server call ``load_env()``. Importing
agent.incident in a test must never pull a developer's real credentials
into the process.

Rules:
  * a variable already set in the real environment always wins — export,
    a shell profile, or a CI secret override the file rather than the
    other way round;
  * ``export FOO=bar``, quotes and ``#`` comments are all handled;
  * a missing file is normal, not an error.

The file itself is gitignored. Nothing in this repo ever writes a
credential to disk, into an incident record, or into a log-set bundle,
and the two places a key could otherwise surface — an exception message
and a Slack payload — are redacted (agent/llm.py::_describe_exception,
agent/slack_client.py::_redact).
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def parse_env(text: str) -> dict[str, str]:
    """Parse .env content. Pure, so it can be tested without a file."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if not name:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name] = value
    return values


def load_env(path: Path | None = None, override: bool = False) -> list[str]:
    """Load `path` (default: the repo's .env) into os.environ. Returns the
    names it set, never their values — callers log the names at most."""
    env_file = path or DEFAULT_ENV_FILE
    try:
        text = env_file.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return []

    applied = []
    for name, value in parse_env(text).items():
        if not override and os.environ.get(name):
            continue  # the real environment wins
        os.environ[name] = value
        applied.append(name)
    return applied
