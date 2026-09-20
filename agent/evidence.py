"""
Evidence bundle — collects what an L2 analyst would gather before
escalating, into reports/evidence/<incident_id>/, with a manifest.json
listing each artifact's size and collection timestamp.

Every collection step is wrapped so a missing or failing source produces
a "missing" entry in the manifest rather than crashing the whole bundle —
the incident is already in trouble; evidence collection failing too
should never compound that. scripts/first-15-minutes.sh is a deliberately
separate, standalone (no Python) implementation of the same idea for an
on-call engineer working directly on a broken machine; this module is
what agent/agent.py calls automatically at incident open.

Current stack caveat: this repo's tools run in mock mode by default —
DDL/schema and row counts below reflect that mock data unless a real
source/target connection is wired in. Said plainly in each artifact
rather than silently presented as live data.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent_tools.log_analyser import LogAnalyser
from agent_tools.schema_comparator import SchemaComparator
from agent_tools.sql_validator import SQLValidator
from mock_pipeline.failures import get_failure_mode, get_target_data

REPO_ROOT = Path(os.path.join(os.path.dirname(__file__), ".."))
EVIDENCE_DIR = REPO_ROOT / "reports" / "evidence"

MAX_BUNDLE_BYTES = 50 * 1024 * 1024  # 50 MB
STDOUT_TAIL_LINES = 200
TRUNCATED_ARTIFACT_CAP_BYTES = 64 * 1024  # per-artifact cap once truncating


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass
class Artifact:
    name: str
    content: str | None
    status: str  # "ok" | "missing" | "truncated"
    note: str | None = None
    collected_at: str = field(default_factory=_now_iso)

    @property
    def size_bytes(self) -> int:
        return len(self.content.encode("utf-8")) if self.content is not None else 0


def _ok(name: str, content: str) -> Artifact:
    return Artifact(name=name, content=content, status="ok")


def _missing(name: str, reason: str) -> Artifact:
    return Artifact(name=name, content=None, status="missing", note=reason)


def _collect_job_log(log_path: str) -> Artifact:
    try:
        result = LogAnalyser().analyse(log_path)
        return _ok("job_log.json", json.dumps(result, indent=2))
    except Exception as exc:  # noqa: BLE001 - degrade gracefully, never crash the bundle
        return _missing("job_log.json", f"log analysis failed: {exc}")


def _collect_stdout_tail(stdout_path: str | None) -> Artifact:
    if not stdout_path:
        return _missing("driver_stdout_tail.txt", "no stdout path provided for this run")
    try:
        with open(stdout_path) as f:
            lines = f.readlines()
        tail = "".join(lines[-STDOUT_TAIL_LINES:])
        return _ok("driver_stdout_tail.txt", tail)
    except FileNotFoundError:
        return _missing("driver_stdout_tail.txt", f"stdout file not found: {stdout_path}")
    except Exception as exc:  # noqa: BLE001
        return _missing("driver_stdout_tail.txt", f"failed to read stdout: {exc}")


def _collect_row_counts(source_table: str, target_table: str) -> Artifact:
    try:
        result = SQLValidator().validate(source_table, target_table)
        content = {
            "source_table": source_table,
            "target_table": target_table,
            "source_count": result["source_count"],
            "target_count": result["target_count"],
            "row_drop_pct": result["row_drop_pct"],
        }
        return _ok("row_counts.json", json.dumps(content, indent=2))
    except Exception as exc:  # noqa: BLE001
        return _missing("row_counts.json", f"row count collection failed: {exc}")


def _collect_schema(source_table: str, target_table: str) -> Artifact:
    try:
        diff = SchemaComparator().compare(source_table, target_table)
        target_schema = get_target_data(get_failure_mode())["schema"]
        content = {
            "note": "mock-mode schema — see agent/evidence.py module docstring",
            "target_schema": target_schema,
            "diff_vs_source": diff,
        }
        return _ok("target_schema.json", json.dumps(content, indent=2))
    except Exception as exc:  # noqa: BLE001
        return _missing("target_schema.json", f"schema collection failed: {exc}")


def _collect_kafka_lag(log_path: str) -> Artifact:
    try:
        result = LogAnalyser().analyse(log_path)
        content = {
            "kafka_lag": result["kafka_lag"],
            "note": (
                "live consumer-group offset query requires a running Kafka broker; "
                "this is the lag figure the Log Analyser captured instead"
            ),
        }
        return _ok("kafka_lag.json", json.dumps(content, indent=2))
    except Exception as exc:  # noqa: BLE001
        return _missing("kafka_lag.json", f"kafka lag collection failed: {exc}")


def _collect_disk_snapshot() -> Artifact:
    try:
        import shutil

        usage = shutil.disk_usage(str(REPO_ROOT))
        content: dict[str, Any] = {
            "disk_total_bytes": usage.total,
            "disk_used_bytes": usage.used,
            "disk_free_bytes": usage.free,
        }
        try:
            stats = os.statvfs(str(REPO_ROOT))
            content["inodes_total"] = stats.f_files
            content["inodes_free"] = stats.f_ffree
        except (AttributeError, OSError):
            content["inodes_note"] = "inode stats unavailable on this platform"
        return _ok("disk_and_inode_snapshot.json", json.dumps(content, indent=2))
    except Exception as exc:  # noqa: BLE001
        return _missing("disk_and_inode_snapshot.json", f"disk snapshot failed: {exc}")


def _apply_size_cap(artifacts: list[Artifact]) -> None:
    """
    Cap total bundle size at MAX_BUNDLE_BYTES, truncating the largest
    artifacts first, in place — mutates status/content/note on any
    artifact it truncates.
    """
    total = sum(a.size_bytes for a in artifacts)
    if total <= MAX_BUNDLE_BYTES:
        return

    for artifact in sorted(artifacts, key=lambda a: a.size_bytes, reverse=True):
        if total <= MAX_BUNDLE_BYTES:
            break
        if artifact.content is None or artifact.size_bytes <= TRUNCATED_ARTIFACT_CAP_BYTES:
            continue
        original_size = artifact.size_bytes
        truncated_content = artifact.content.encode("utf-8")[:TRUNCATED_ARTIFACT_CAP_BYTES].decode(
            "utf-8", errors="ignore"
        )
        total -= original_size - len(truncated_content.encode("utf-8"))
        artifact.content = truncated_content
        artifact.status = "truncated"
        artifact.note = f"truncated from {original_size} bytes to fit the {MAX_BUNDLE_BYTES}-byte bundle cap"


def collect_evidence(
    incident_id: str,
    *,
    source_table: str = "src.transactions",
    target_table: str = "tgt.transactions",
    log_path: str = "",
    stdout_path: str | None = None,
) -> Path:
    """
    Collect the evidence bundle for `incident_id` into
    reports/evidence/<incident_id>/ and write manifest.json. Returns the
    bundle directory.
    """
    artifacts = [
        _collect_job_log(log_path),
        _collect_stdout_tail(stdout_path),
        _collect_row_counts(source_table, target_table),
        _collect_schema(source_table, target_table),
        _collect_kafka_lag(log_path),
        _collect_disk_snapshot(),
    ]

    _apply_size_cap(artifacts)

    bundle_dir = EVIDENCE_DIR / incident_id
    bundle_dir.mkdir(parents=True, exist_ok=True)

    manifest_entries = []
    for artifact in artifacts:
        entry: dict[str, Any] = {
            "name": artifact.name,
            "status": artifact.status,
            "collected_at": artifact.collected_at,
            "size_bytes": artifact.size_bytes,
        }
        if artifact.note:
            entry["note"] = artifact.note
        if artifact.content is not None:
            (bundle_dir / artifact.name).write_text(artifact.content)
            entry["path"] = f"reports/evidence/{incident_id}/{artifact.name}"
        manifest_entries.append(entry)

    manifest = {
        "incident_id": incident_id,
        "collected_at": _now_iso(),
        "total_size_bytes": sum(a.size_bytes for a in artifacts),
        "artifacts": manifest_entries,
    }
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    return bundle_dir
