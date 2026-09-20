"""
Triage one log set: read the logs, derive signals, classify, alert Slack.

This is the whole scope of the project in one function —
``triage_logset()``:

    log set  →  signature matches  →  signals  →  severity (agent/severity.py)
             →  incident record (agent/incident.py)  →  Slack alert
             →  a zip of exactly the logs that produced it

Everything the agent concludes comes from the log text. It never reads
the session manifest's ground truth; ``score()`` does, afterwards, to say
how much of what was injected was actually found.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from agent.incident import Incident, persist
from agent.runbooks import select_runbook
from agent.severity import classify, load_config
from agent.slack_blocks import build_logset_reply, build_parent_message
from agent.slack_client import SlackClient
from logsets.catalog import SIGNATURES
from logsets.session import LogSet, build_session, bundle, load_session

_ERROR_LEVEL = re.compile(r"\b(ERROR|FATAL|SEVERE)\b")
_WARN_LEVEL = re.compile(r"\b(WARN|WARNING)\b")

# Ordered worst-first, so the reported root cause is the worst thing in
# the set rather than whichever file happened to be read first.
_SEVERITY_RANK = {"P1": 0, "P2": 1, "P3": 2, "P4": 3, None: 4}


# ---------- Reading the logs ----------

def _merge_signals(into: dict[str, Any], new: dict[str, Any]) -> None:
    """Combine one finding's signals into the set-wide signals. Two hits
    of the same signature are not twice as bad, they are as bad as the
    worse one — except blocked downstream jobs, which add up."""
    for key, value in new.items():
        if key == "null_rate_increase_pct":
            bucket = into.setdefault(key, {})
            for column, pct in value.items():
                bucket[column] = max(bucket.get(column, 0.0), pct)
        elif key == "downstream_jobs_blocked":
            into[key] = into.get(key, 0) + value
        elif isinstance(value, bool):
            into[key] = into.get(key, False) or value
        else:
            into[key] = max(into.get(key, 0), value)


def analyse_file(path: Path) -> dict[str, Any]:
    """Scan one log file: level counts, signature hits, unrecognised errors."""
    findings: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    error_count = warn_count = 0

    for number, raw in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        line = raw.rstrip()
        is_error = bool(_ERROR_LEVEL.search(line))
        if is_error:
            error_count += 1
        elif _WARN_LEVEL.search(line):
            warn_count += 1

        matched = False
        for signature in SIGNATURES:
            match = signature.pattern.search(line)
            if not match:
                continue
            matched = True
            findings.append({
                "signature_id": signature.id,
                "title": signature.title,
                "file": path.name,
                "line": number,
                "line_text": line[:400],
                "signals": signature.to_signals(match),
            })
        if is_error and not matched:
            unmatched.append({"file": path.name, "line": number, "line_text": line[:400]})

    return {
        "file": path.name,
        "error_count": error_count,
        "warn_count": warn_count,
        "findings": findings,
        "unmatched_errors": unmatched,
    }


def analyse_logset(logset: LogSet) -> dict[str, Any]:
    """Read every file in the set and reduce it to signals for
    agent/severity.py. Pure: no incident, no Slack, no disk writes."""
    per_file = []
    findings: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    signals: dict[str, Any] = {}
    error_count = warn_count = 0

    for entry in logset.files:
        path = logset.directory / entry["file"]
        if not path.exists():
            continue
        result = analyse_file(path)
        result["source"] = entry["source"]
        result["provider"] = entry["provider"]
        result["line_count"] = entry["line_count"]
        per_file.append(result)
        findings.extend(result["findings"])
        unmatched.extend(result["unmatched_errors"])
        error_count += result["error_count"]
        warn_count += result["warn_count"]

    for finding in findings:
        _merge_signals(signals, finding["signals"])

    # An ERROR the catalogue doesn't recognise is still an error. It is
    # reported as exactly what it is — a log anomaly with no established
    # data impact — rather than being guessed at or quietly dropped.
    if unmatched:
        signals["log_anomaly_no_data_impact"] = True

    return {
        "session_id": logset.session_id,
        "files": per_file,
        "findings": findings,
        "unmatched_errors": unmatched,
        "signals": signals,
        "error_count": error_count,
        "warn_count": warn_count,
        "lines_scanned": sum(f["line_count"] for f in per_file),
    }


def score(logset: LogSet, analysis: dict[str, Any]) -> dict[str, Any]:
    """Compare findings against the manifest's ground truth. Used for
    reporting only — never by the triage path itself."""
    injected = {item["signature_id"] for item in logset.injected}
    detected = {finding["signature_id"] for finding in analysis["findings"]}
    return {
        "injected": sorted(injected),
        "detected": sorted(detected),
        "missed": sorted(injected - detected),
        "extra": sorted(detected - injected),
        "recall": round(len(injected & detected) / len(injected), 3) if injected else None,
    }


# ---------- Turning the analysis into an incident ----------

def _download_url(session_id: str) -> str:
    base = os.getenv("AGENT_PUBLIC_URL", "").rstrip("/")
    return f"{base}/logset/{session_id}/download" if base else ""


def _impact_summary(logset: LogSet, analysis: dict[str, Any]) -> str:
    titles = []
    for finding in analysis["findings"]:
        if finding["title"] not in titles:
            titles.append(finding["title"])
    files = len(analysis["files"])
    head = (f"Log set `{logset.session_id}` — {analysis['error_count']} error and "
            f"{analysis['warn_count']} warning lines across {files} log file(s) "
            f"({analysis['lines_scanned']} lines scanned).")
    if titles:
        return head + " Recognised: " + "; ".join(titles[:4]) + "."
    return head + f" No recognised failure signature; {len(analysis['unmatched_errors'])} unrecognised error line(s)."


def _root_cause(analysis: dict[str, Any], config: dict[str, Any]) -> str | None:
    """The worst recognised finding, by the severity its own signals
    classify to — not the first one read."""
    if not analysis["findings"]:
        if analysis["unmatched_errors"]:
            first = analysis["unmatched_errors"][0]
            return (f"Unrecognised error in `{first['file']}` line {first['line']}: "
                    f"{first['line_text'][:200]}")
        return None

    worst = min(
        analysis["findings"],
        key=lambda f: _SEVERITY_RANK[classify(f["signals"], config).severity],
    )
    return (f"{worst['title']} ({worst['signature_id']}) — `{worst['file']}` "
            f"line {worst['line']}: {worst['line_text'][:200]}")


def _recommended_action(runbook: str | None, download: str) -> str:
    action = f"Work the runbook `{runbook}`." if runbook else \
        "No runbook covers this signature set — triage from the logs."
    return f"{action} The exact log set is attached for download: {download}"


def build_incident(logset: LogSet, analysis: dict[str, Any], archive: Path,
                   config: dict[str, Any] | None = None) -> Incident | None:
    """Classify the analysis and open an incident, or return None for a
    clean set. Severity stays where it has always been — deterministic,
    in agent/severity.py against config/severity.yml."""
    config = config or load_config()
    result = classify(analysis["signals"], config)
    if not result.severity:
        return None

    from agent.incident import open_incident  # local: keeps import cost off clean runs

    url = _download_url(logset.session_id)
    download = url or f"`{archive}`"
    evidence = [str(archive)] + [
        str(logset.directory / entry["file"]) for entry in analysis["files"]
    ]

    incident = open_incident(analysis["signals"], result, {
        "detected_by": "logset-triage",
        "affected_job": f"log-set {logset.session_id}",
        "affected_objects": [entry["file"] for entry in analysis["files"]],
        "impact_summary": _impact_summary(logset, analysis),
        "evidence": evidence,
        "root_cause": _root_cause(analysis, config),
        # Deterministic, like severity: a recognised signature is a known
        # failure mode read straight off the log line; an unrecognised
        # error is a real error whose meaning has not been established.
        "confidence": 0.9 if analysis["findings"] else 0.4,
        "runbook": select_runbook(analysis["signals"]),
        "recommended_action": _recommended_action(
            select_runbook(analysis["signals"]), download),
        "requires_approval": result.severity in {"P1", "P2"},
    })
    persist(incident)
    return incident


def alert_slack(incident: Incident, logset: LogSet, analysis: dict[str, Any],
                archive: Path, client: SlackClient | None = None) -> None:
    """Post the incident and a thread reply carrying the log-set
    breakdown and the download link. Slack is a view onto the incident
    record, never the source of truth, so a Slack failure is logged and
    never raised."""
    try:
        client = client or SlackClient()
        blocks, text = build_parent_message(incident, run_id=logset.session_id)
        client.post_incident(incident, blocks, text)
        if incident.severity == "P1":
            client.mirror_p1(incident, blocks, text)

        detail_blocks, detail_text = build_logset_reply(
            logset_summary(logset, analysis, archive))
        client.reply_thread(incident, detail_blocks, detail_text)
    except Exception as exc:  # noqa: BLE001 - an alert failure must not lose the incident
        print(f"[logsets] WARNING: Slack alert failed for {incident.incident_id}: {exc}")


def logset_summary(logset: LogSet, analysis: dict[str, Any],
                   archive: Path) -> dict[str, Any]:
    """The compact view of a triaged log set that Slack, the API and the
    CLI all render."""
    return {
        "session_id": logset.session_id,
        "seed": logset.seed,
        "created_at": logset.created_at,
        "files": [
            {
                "file": entry["file"],
                "source": entry["source"],
                "provider": entry["provider"],
                "line_count": entry["line_count"],
                "error_count": entry["error_count"],
                "warn_count": entry["warn_count"],
            }
            for entry in analysis["files"]
        ],
        "findings": [
            {k: v for k, v in finding.items() if k != "signals"}
            for finding in analysis["findings"]
        ],
        "unrecognised_error_count": len(analysis["unmatched_errors"]),
        "error_count": analysis["error_count"],
        "warn_count": analysis["warn_count"],
        "lines_scanned": analysis["lines_scanned"],
        "download": {"archive": str(archive), "url": _download_url(logset.session_id)},
    }


# ---------- The whole thing ----------

def triage_logset(
    seed: int | None = None,
    session_id: str | None = None,
    sources: list[str] | None = None,
    source_count: int | None = None,
    injections: int | None = None,
    clean: bool = False,
    notify: bool = True,
    root: Path | None = None,
    corpus_dir: Path | None = None,
    started_at: datetime | None = None,
    slack_client: SlackClient | None = None,
) -> dict[str, Any]:
    """Build (or reload) a session's log set, triage it, alert Slack, and
    return the response — incident included, download link included."""
    if session_id and _session_exists(session_id, root):
        logset = load_session(session_id, root)
    else:
        logset = build_session(seed=seed, sources=sources, source_count=source_count,
                               injections=injections, clean=clean,
                               session_id=session_id, root=root, corpus_dir=corpus_dir,
                               started_at=started_at)

    analysis = analyse_logset(logset)
    archive = bundle(logset, force=True)
    incident = build_incident(logset, analysis, archive)

    if incident and notify:
        alert_slack(incident, logset, analysis, archive, client=slack_client)

    return {
        "session_id": logset.session_id,
        "seed": logset.seed,
        "logset": logset_summary(logset, analysis, archive),
        "signals": analysis["signals"],
        "incident": incident.to_dict() if incident else None,
        "clean": incident is None,
        "score": score(logset, analysis),
    }


def _session_exists(session_id: str, root: Path | None) -> bool:
    try:
        load_session(session_id, root)
        return True
    except FileNotFoundError:
        return False
