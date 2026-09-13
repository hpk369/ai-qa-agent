"""
Incident record — the system of record for the ETL Production Support
Triage Agent. Slack (agent/slack_client.py, Phase 1) is a view onto this
object; this module is the only thing that writes it.

Every non-clean run produces an Incident, persisted to
reports/incidents/<incident_id>.json and validated against
schemas/incident.schema.json on every write. A malformed incident is
worse than none — persist() fails loudly rather than writing one.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jsonschema

from agent.severity import SeverityResult

REPO_ROOT = Path(os.path.join(os.path.dirname(__file__), ".."))
SCHEMA_PATH = REPO_ROOT / "schemas" / "incident.schema.json"
INCIDENTS_DIR = REPO_ROOT / "reports" / "incidents"

# Actors whose timeline entries do not count as a human response for MTTA
# purposes. Extended in Phase 1 to exclude the Slack bot's own user ID.
NON_HUMAN_ACTORS = {"system", "agent", "bot"}

TERMINAL_STATUSES = {"resolved", "false_positive"}
VALID_STATUSES = {
    "open",
    "acknowledged",
    "remediating",
    "verifying",
    "resolved",
    "false_positive",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse_iso(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc) \
        if "." in ts \
        else datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


@dataclass
class Incident:
    incident_id: str
    opened_at: str
    detected_by: str
    severity: str
    severity_rationale: str
    affected_job: str
    affected_objects: list[str]
    rows_expected: int | None
    rows_loaded: int | None
    impact_summary: str
    evidence: list[str]
    root_cause: str | None
    confidence: float
    runbook: str | None
    recommended_action: str | None
    requires_approval: bool
    slack_channel: str | None
    slack_ts: str | None
    status: str
    timeline: list[dict[str, str]] = field(default_factory=list)
    mtta_seconds: int | None = None
    mttr_seconds: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Incident":
        return cls(**data)


def _load_schema() -> dict:
    with open(SCHEMA_PATH) as f:
        return json.load(f)


def _next_incident_id(base_id: str) -> str:
    """base_id already collision-free unless a file for it exists on disk."""
    candidate = base_id
    suffix = 2
    while (INCIDENTS_DIR / f"{candidate}.json").exists():
        candidate = f"{base_id}-{suffix}"
        suffix += 1
    return candidate


def open_incident(
    signals: dict[str, Any],
    severity_result: SeverityResult,
    run_context: dict[str, Any],
) -> Incident:
    """
    Open a new incident from observed signals, a severity classification,
    and run context (affected job/objects, row counts, impact summary,
    evidence paths, and whatever root cause / recommended action / runbook
    the agent could determine). Raises if severity_result has no severity —
    a clean run never opens an incident.
    """
    if not severity_result.severity:
        raise ValueError("cannot open an incident for a clean run (severity is None)")

    opened_at = _now_iso()
    base_id = f"INC-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')}"
    incident_id = _next_incident_id(base_id)

    return Incident(
        incident_id=incident_id,
        opened_at=opened_at,
        detected_by=run_context.get("detected_by", "unknown"),
        severity=severity_result.severity,
        severity_rationale=severity_result.rationale,
        affected_job=run_context.get("affected_job", "unknown"),
        affected_objects=run_context.get("affected_objects", []),
        rows_expected=run_context.get("rows_expected"),
        rows_loaded=run_context.get("rows_loaded"),
        impact_summary=run_context.get("impact_summary", ""),
        evidence=run_context.get("evidence", []),
        root_cause=run_context.get("root_cause"),
        confidence=run_context.get("confidence", 0.0),
        runbook=run_context.get("runbook"),
        recommended_action=run_context.get("recommended_action"),
        requires_approval=run_context.get("requires_approval", False),
        slack_channel=run_context.get("slack_channel"),
        slack_ts=run_context.get("slack_ts"),
        status="open",
        timeline=[
            {
                "at": opened_at,
                # "system", not detected_by: the open event is automated
                # detection, never a human response, and must not count
                # toward MTTA (see NON_HUMAN_ACTORS / compute_mtta).
                "actor": "system",
                "event": "opened",
                "detail": f"detected by {run_context.get('detected_by', 'unknown')}: {severity_result.rationale}",
            }
        ],
        mtta_seconds=None,
        mttr_seconds=None,
    )


def append_timeline(incident: Incident, actor: str, event: str, detail: str) -> Incident:
    incident.timeline.append(
        {"at": _now_iso(), "actor": actor, "event": event, "detail": detail}
    )
    return incident


def set_status(incident: Incident, status: str, actor: str = "system") -> Incident:
    if status not in VALID_STATUSES:
        raise ValueError(f"unknown status: {status!r} (must be one of {sorted(VALID_STATUSES)})")

    incident.status = status
    append_timeline(incident, actor=actor, event="status_change", detail=f"status set to {status}")

    if status == "resolved":
        incident.mttr_seconds = compute_mttr(incident)

    return incident


VALID_DECISIONS = {"approved", "rejected", "escalated"}

# decision -> status it moves the incident to. requires_approval means no
# remediation proceeds without one of these being recorded first.
_DECISION_STATUS = {"approved": "remediating", "rejected": "acknowledged", "escalated": "open"}


def _has_prior_decision(incident: Incident) -> bool:
    return any(entry["event"] == "approval_decision" for entry in incident.timeline)


def record_approval_decision(incident: Incident, decision: str, approver: str, slack_client=None) -> Incident:
    """
    Record a human decision (approved/rejected/escalated) — from a Slack
    button click, most likely — and persist it. `requires_approval: true`
    means no remediation proceeds without exactly one of these being
    recorded; a second decision on an already-decided incident is
    rejected and reported in the Slack thread rather than silently
    ignored, per T1.5.

    approved -> status "remediating"; rejected -> "acknowledged" (a human
    looked at it and declined the proposed action, but the incident is
    still open pending a different plan); escalated -> "open" and
    unconditionally mirrored to #etl-prod-p1 regardless of the incident's
    actual severity, since escalation is itself a request for more
    visibility.

    Local imports of agent.slack_client/agent.slack_blocks below avoid a
    circular import — slack_client.py imports Incident/persist from this
    module at module load time, so this module cannot import it back at
    module load time too.
    """
    if decision not in VALID_DECISIONS:
        raise ValueError(f"unknown decision: {decision!r} (must be one of {sorted(VALID_DECISIONS)})")

    from agent.slack_blocks import build_parent_message, build_thread_reply
    from agent.slack_client import SlackClient

    slack = slack_client or SlackClient()

    if _has_prior_decision(incident):
        append_timeline(
            incident,
            actor=approver,
            event="approval_decision_rejected",
            detail=f"duplicate {decision} by {approver} ignored — already decided",
        )
        persist(incident)
        try:
            blocks, text = build_thread_reply(
                f"⚠️ Duplicate decision ignored: <@{approver}> attempted *{decision}*, "
                f"but this incident was already decided."
            )
            slack.reply_thread(incident, blocks, text)
        except Exception as exc:  # noqa: BLE001 - Slack is a view, never the source of truth
            print(f"[incident] WARNING: failed to report duplicate decision on {incident.incident_id}: {exc}")
        return incident

    append_timeline(incident, actor=approver, event="approval_decision", detail=f"{decision} by {approver}")
    set_status(incident, _DECISION_STATUS[decision], actor=approver)
    persist(incident)

    try:
        blocks, text = build_thread_reply(f"*{decision.capitalize()}* by <@{approver}>")
        slack.reply_thread(incident, blocks, text)
        slack.post_change_log(incident, decision, approver)
        if decision == "escalated":
            parent_blocks, parent_text = build_parent_message(incident)
            slack.mirror_to_p1(incident, parent_blocks, parent_text)
            slack.update_parent(incident, parent_blocks, parent_text)
    except Exception as exc:  # noqa: BLE001 - the decision is already persisted; Slack is a view
        print(f"[incident] WARNING: Slack notification failed while recording {decision} on {incident.incident_id}: {exc}")

    return incident


def compute_mtta(incident: Incident) -> int | None:
    """
    Seconds from opened_at to the first human timeline event — a thread
    reply or reaction from a non-bot user (agent.slack_client wires the
    real Slack signal in Phase 1). Here, "human" means any timeline entry
    whose actor is not in NON_HUMAN_ACTORS. Returns None if no such event
    has been recorded yet.
    """
    opened = _parse_iso(incident.opened_at)
    for entry in incident.timeline:
        if entry["actor"] not in NON_HUMAN_ACTORS:
            return int((_parse_iso(entry["at"]) - opened).total_seconds())
    return None


def compute_mttr(incident: Incident) -> int | None:
    """Seconds from opened_at to the status=resolved timeline event."""
    if incident.status != "resolved":
        return None

    opened = _parse_iso(incident.opened_at)
    for entry in incident.timeline:
        if entry["event"] == "status_change" and entry["detail"] == "status set to resolved":
            return int((_parse_iso(entry["at"]) - opened).total_seconds())
    return None


def render_markdown(incident: Incident) -> str:
    lines = [
        f"# {incident.incident_id} — {incident.severity}",
        "",
        f"**Opened:** {incident.opened_at}  ",
        f"**Status:** {incident.status}  ",
        f"**Detected by:** {incident.detected_by}  ",
        f"**Affected job:** {incident.affected_job}  ",
        f"**Affected objects:** {', '.join(incident.affected_objects) or 'none'}  ",
        "",
        "## Impact",
        incident.impact_summary or "_not recorded_",
        "",
        "## Severity rationale",
        incident.severity_rationale,
        "",
        "## Root cause",
        incident.root_cause or "_not yet determined_",
        "",
        "## Recommended action",
        incident.recommended_action or "_none yet_",
    ]
    if incident.runbook:
        lines.append(f"Runbook: `{incident.runbook}`")
    lines += [
        "",
        f"**Confidence:** {incident.confidence:.2f}  ",
        f"**Requires approval:** {incident.requires_approval}  ",
    ]
    if incident.evidence:
        lines += ["", "## Evidence", *[f"- `{path}`" for path in incident.evidence]]
    lines += ["", "## Timeline"]
    for entry in incident.timeline:
        lines.append(f"- `{entry['at']}` **{entry['actor']}** — {entry['event']}: {entry['detail']}")
    if incident.mtta_seconds is not None:
        lines += ["", f"**MTTA:** {incident.mtta_seconds}s"]
    if incident.mttr_seconds is not None:
        lines += [f"**MTTR:** {incident.mttr_seconds}s"]
    return "\n".join(lines) + "\n"


def persist(incident: Incident) -> Path:
    """
    Validate against schemas/incident.schema.json and write
    reports/incidents/<incident_id>.json (plus a rendered .md sidecar).
    Raises jsonschema.ValidationError on a malformed incident rather than
    writing it — a malformed record is worse than none.
    """
    data = incident.to_dict()
    jsonschema.validate(instance=data, schema=_load_schema())

    INCIDENTS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = INCIDENTS_DIR / f"{incident.incident_id}.json"
    json_path.write_text(json.dumps(data, indent=2) + "\n")

    md_path = INCIDENTS_DIR / f"{incident.incident_id}.md"
    md_path.write_text(render_markdown(incident))

    return json_path


def load(incident_id: str) -> Incident:
    json_path = INCIDENTS_DIR / f"{incident_id}.json"
    with open(json_path) as f:
        data = json.load(f)
    return Incident.from_dict(data)
