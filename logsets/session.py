"""
One session, one log set.

``build_session()`` mixes a fresh set of log files: a handful of sources
drawn from logsets.catalog, background lines from logsets.corpus (real
public logs where they exist, generated otherwise), and a few ETL error
signatures injected into the ETL-side files at random offsets. Every
session gets a different mix; passing the same ``seed`` reproduces one
exactly, which is what makes a Slack alert re-checkable after the fact.

The manifest written beside the log files records the ground truth — which
signature went into which file at which line. Nothing in the triage path
reads it (logsets/triage.py works from the log text alone); it exists so a
downloaded bundle can be checked against the alert it produced, and so
detection can be scored.
"""

from __future__ import annotations

import json
import random
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from logsets.catalog import (
    INJECTABLE_SOURCES,
    SOURCES,
    Signature,
    Source,
    advance,
    format_line,
    signatures_for_family,
    source_by_name,
)
from logsets.corpus import LOGHUB_ATTRIBUTION, background_lines

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROOT = REPO_ROOT / "reports" / "logsets"

MIN_LINES_PER_FILE = 120
MAX_LINES_PER_FILE = 420

MANIFEST_NAME = "manifest.json"
README_NAME = "README.md"


@dataclass
class LogSet:
    """A built log set: the files on disk plus what went into them."""

    session_id: str
    seed: int
    created_at: str
    directory: Path
    files: list[dict[str, Any]] = field(default_factory=list)
    injected: list[dict[str, Any]] = field(default_factory=list)
    corpus_note: str = LOGHUB_ATTRIBUTION

    @property
    def log_paths(self) -> list[Path]:
        return [self.directory / entry["file"] for entry in self.files]

    @property
    def total_lines(self) -> int:
        return sum(entry["line_count"] for entry in self.files)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "seed": self.seed,
            "created_at": self.created_at,
            "files": self.files,
            "total_lines": self.total_lines,
            # Ground truth. The triage path never reads this — see module docstring.
            "injected": self.injected,
            "corpus_note": self.corpus_note,
        }

    @classmethod
    def from_manifest(cls, manifest: dict[str, Any], directory: Path) -> "LogSet":
        return cls(
            session_id=manifest["session_id"],
            seed=manifest["seed"],
            created_at=manifest["created_at"],
            directory=directory,
            files=manifest.get("files", []),
            injected=manifest.get("injected", []),
            corpus_note=manifest.get("corpus_note", LOGHUB_ATTRIBUTION),
        )


# ---------- Building ----------

def _pick_sources(rng: random.Random, source_count: int | None) -> list[Source]:
    """At least two ETL-side sources (there has to be something to triage),
    the rest drawn from the infrastructure logs around them."""
    noise = [s for s in SOURCES if not s.injectable]
    count = source_count if source_count is not None else rng.randint(3, 5)
    count = max(2, min(count, len(SOURCES)))

    etl_count = min(len(INJECTABLE_SOURCES), max(2, count - rng.randint(0, 2)))
    chosen = rng.sample(list(INJECTABLE_SOURCES), etl_count)
    remaining = count - etl_count
    if remaining > 0:
        chosen += rng.sample(noise, min(remaining, len(noise)))
    rng.shuffle(chosen)
    return chosen


def _eligible_signatures(sources: list[Source]) -> list[tuple[Source, Signature]]:
    pairs = []
    for source in sources:
        if not source.injectable:
            continue
        for signature in signatures_for_family(source.family):
            pairs.append((source, signature))
    return pairs


def build_session(
    seed: int | None = None,
    sources: list[str] | None = None,
    source_count: int | None = None,
    injections: int | None = None,
    clean: bool = False,
    session_id: str | None = None,
    root: Path | None = None,
    corpus_dir: Path | None = None,
    started_at: datetime | None = None,
) -> LogSet:
    """Mix and write one session's log set. Returns the built LogSet.

    The seed fixes everything about the content — which sources, which
    lines, which signatures, where they land. Timestamps are anchored to
    the build time, so a rebuild of the same seed tomorrow produces the
    same log set dated tomorrow; pass ``started_at`` to anchor those too
    and get a byte-identical rebuild.
    """
    if seed is None:
        seed = random.SystemRandom().randrange(2**31)
    rng = random.Random(seed)

    created = started_at or datetime.now(timezone.utc)
    session_id = session_id or f"LS-{created.strftime('%Y%m%d-%H%M%S')}-{seed % 0x10000:04x}"
    directory = (root or DEFAULT_ROOT) / session_id
    directory.mkdir(parents=True, exist_ok=True)

    chosen = ([source_by_name(name) for name in sources] if sources
              else _pick_sources(rng, source_count))

    # Background first: every file gets its lines before anything is injected,
    # so an injection lands in the middle of a running log rather than at a seam.
    window_start = created - timedelta(minutes=rng.randint(20, 240))
    staged: list[dict[str, Any]] = []
    for source in chosen:
        count = rng.randint(MIN_LINES_PER_FILE, MAX_LINES_PER_FILE)
        lines, provider = background_lines(source, rng, count, window_start, corpus_dir)
        staged.append({
            "source": source,
            "lines": lines,
            "provider": provider,
            "injected": [],
        })

    # Then the signatures.
    injected: list[dict[str, Any]] = []
    if not clean:
        pairs = _eligible_signatures(chosen)
        wanted = injections if injections is not None else rng.choice([1, 1, 2, 2, 3, 4])
        for _ in range(wanted):
            if not pairs:
                break
            source, signature = rng.choice(pairs)
            entry = next(item for item in staged if item["source"].name == source.name)
            when = window_start + timedelta(seconds=rng.randint(0, 3600))
            rendered = []
            for level, message in signature.render(rng):
                rendered.append(format_line(source.formatter, level, message, when, rng))
                when = advance(when, rng)
            position = rng.randint(1, max(1, len(entry["lines"]) - 1))
            entry["lines"][position:position] = rendered
            entry["injected"].append({
                "signature_id": signature.id,
                "title": signature.title,
                # Resolved to a final line number at write time — a later
                # injection into the same file shifts everything under it.
                "anchor": rendered[0],
                "lines_written": len(rendered),
            })

    # Write.
    files: list[dict[str, Any]] = []
    for entry in staged:
        source: Source = entry["source"]
        name = f"{source.name}.log"
        (directory / name).write_text("\n".join(entry["lines"]) + "\n", encoding="utf-8")
        for item in entry["injected"]:
            item["line"] = entry["lines"].index(item.pop("anchor")) + 1  # 1-indexed
        entry["injected"].sort(key=lambda item: item["line"])
        files.append({
            "file": name,
            "source": source.name,
            "family": source.family,
            "description": source.description,
            "provider": entry["provider"],
            "line_count": len(entry["lines"]),
            "injected": entry["injected"],
        })
        for item in entry["injected"]:
            injected.append({**item, "file": name, "source": source.name})

    logset = LogSet(
        session_id=session_id,
        seed=seed,
        created_at=created.isoformat(timespec="seconds"),
        directory=directory,
        files=files,
        injected=injected,
    )
    (directory / MANIFEST_NAME).write_text(
        json.dumps(logset.to_dict(), indent=2) + "\n", encoding="utf-8")
    (directory / README_NAME).write_text(_bundle_readme(logset), encoding="utf-8")
    return logset


def _bundle_readme(logset: LogSet) -> str:
    real = sum(1 for f in logset.files if f["provider"] == "real")
    lines = [
        f"# Log set {logset.session_id}",
        "",
        f"Built {logset.created_at} · seed `{logset.seed}` · "
        f"{len(logset.files)} files · {logset.total_lines} lines "
        f"({real} of {len(logset.files)} files from real public logs).",
        "",
        "Rebuild this set (content is fixed by the seed; timestamps re-anchor "
        "to the rebuild):",
        "",
        "```bash",
        f"python scripts/logset.py --seed {logset.seed}",
        "```",
        "",
        "## Files",
        "",
        "| File | Source | Lines | Background |",
        "|---|---|---|---|",
    ]
    for entry in logset.files:
        lines.append(f"| `{entry['file']}` | {entry['description']} | "
                     f"{entry['line_count']} | {entry['provider']} |")
    lines += [
        "",
        "## Ground truth",
        "",
        "What the mixer injected, for checking the alert against the logs. "
        "The agent never reads this — it works from the log text alone.",
        "",
    ]
    if logset.injected:
        lines += ["| Signature | File | Line |", "|---|---|---|"]
        for item in logset.injected:
            lines.append(f"| {item['signature_id']} — {item['title']} | "
                         f"`{item['file']}` | {item['line']} |")
    else:
        lines.append("Nothing injected — this set is a clean run.")
    lines += ["", "## Background lines", "", logset.corpus_note, ""]
    return "\n".join(lines)


# ---------- Loading, listing, bundling ----------

def load_session(session_id: str, root: Path | None = None) -> LogSet:
    directory = (root or DEFAULT_ROOT) / session_id
    manifest_path = directory / MANIFEST_NAME
    if not manifest_path.exists():
        raise FileNotFoundError(f"no log set {session_id!r} under {directory.parent}")
    return LogSet.from_manifest(json.loads(manifest_path.read_text()), directory)


def list_sessions(root: Path | None = None) -> list[str]:
    base = root or DEFAULT_ROOT
    if not base.exists():
        return []
    return sorted(
        (path.name for path in base.iterdir() if (path / MANIFEST_NAME).exists()),
        reverse=True,
    )


def bundle(logset: LogSet, force: bool = False) -> Path:
    """Zip the log set for download: every log file, the manifest, and the
    README. Written next to the session directory, rebuilt on demand."""
    archive = logset.directory.parent / f"{logset.session_id}.zip"
    if archive.exists() and not force:
        return archive

    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for entry in logset.files:
            path = logset.directory / entry["file"]
            if path.exists():
                zf.write(path, f"{logset.session_id}/{entry['file']}")
        for extra in (MANIFEST_NAME, README_NAME):
            path = logset.directory / extra
            if path.exists():
                zf.write(path, f"{logset.session_id}/{extra}")
    return archive
