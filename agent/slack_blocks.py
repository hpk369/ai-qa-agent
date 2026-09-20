"""
Block Kit builders for the incident channel. Moves the parent message off
mrkdwn strings (the old n8n Code node's approach) onto structured Block
Kit, so a P1 and a P4 are visually distinguishable without reading, and so
the message can be edited in place via chat.update.

Every builder here is pure — no network calls, no Incident mutation — and
every payload is validated against Slack's own block limits before it is
returned, so a limit violation raises at build time, not when posting.
"""

from __future__ import annotations

from typing import Any

from agent.incident import Incident

SEVERITY_EMOJI = {"P1": "🔴", "P2": "🟠", "P3": "🟡", "P4": "⚪"}
RESOLVED_EMOJI = "✅"

MAX_BLOCKS = 50
MAX_TEXT_CHARS = 3000
MAX_FIELDS_PER_SECTION = 10
EVIDENCE_DISPLAY_LIMIT = 5


class SlackBlockLimitError(ValueError):
    """Raised at build time when a payload would exceed a Slack Block Kit
    limit — never let this fail at post time instead."""


def _mrkdwn(text: str) -> dict[str, str]:
    return {"type": "mrkdwn", "text": text}


def _plain(text: str) -> dict[str, str]:
    return {"type": "plain_text", "text": text, "emoji": True}


def severity_emoji(incident: Incident) -> str:
    if incident.status == "resolved":
        return RESOLVED_EMOJI
    return SEVERITY_EMOJI.get(incident.severity, "❗")


def _iter_text_objects(block: dict[str, Any]):
    text = block.get("text")
    if isinstance(text, dict):
        yield text
    for field in block.get("fields", []) or []:
        yield field
    for element in block.get("elements", []) or []:
        if isinstance(element, dict):
            inner = element.get("text")
            if isinstance(inner, dict):
                yield inner


def validate_blocks(blocks: list[dict[str, Any]]) -> None:
    """Raise SlackBlockLimitError if `blocks` would violate a Slack Block
    Kit limit: 50 blocks max, 3000 chars per text object, 10 fields max
    per section."""
    if len(blocks) > MAX_BLOCKS:
        raise SlackBlockLimitError(f"{len(blocks)} blocks exceeds Slack's {MAX_BLOCKS}-block limit")

    for block in blocks:
        fields = block.get("fields")
        if fields is not None and len(fields) > MAX_FIELDS_PER_SECTION:
            raise SlackBlockLimitError(
                f"section has {len(fields)} fields, exceeds Slack's {MAX_FIELDS_PER_SECTION}-field limit"
            )
        for text_obj in _iter_text_objects(block):
            length = len(text_obj.get("text", ""))
            if length > MAX_TEXT_CHARS:
                raise SlackBlockLimitError(
                    f"text object has {length} chars, exceeds Slack's {MAX_TEXT_CHARS}-char limit: "
                    f"{text_obj['text'][:60]!r}..."
                )


def _header_block(incident: Incident) -> dict[str, Any]:
    status_label = "RESOLVED" if incident.status == "resolved" else incident.severity
    return {
        "type": "header",
        "text": _plain(f"{severity_emoji(incident)} {status_label} — {incident.incident_id}"),
    }


def _fields_block(incident: Incident) -> dict[str, Any]:
    rows_expected = incident.rows_expected if incident.rows_expected is not None else "n/a"
    rows_loaded = incident.rows_loaded if incident.rows_loaded is not None else "n/a"
    return {
        "type": "section",
        "fields": [
            _mrkdwn(f"*Affected job:*\n{incident.affected_job}"),
            _mrkdwn(f"*Affected objects:*\n{', '.join(incident.affected_objects) or 'none'}"),
            _mrkdwn(f"*Rows loaded / expected:*\n{rows_loaded} / {rows_expected}"),
            _mrkdwn(f"*Detected by:*\n{incident.detected_by}"),
            _mrkdwn(f"*Confidence:*\n{incident.confidence:.0%}"),
            _mrkdwn(f"*Status:*\n{incident.status}"),
        ],
    }


def _context_block(incident: Incident, run_id: str | None) -> dict[str, Any]:
    elements = [_mrkdwn(f"Opened: {incident.opened_at}")]
    if run_id:
        elements.append(_mrkdwn(f"Run ID: {run_id}"))
    if incident.mtta_seconds is not None:
        elements.append(_mrkdwn(f"MTTA: {incident.mtta_seconds}s"))
    if incident.mttr_seconds is not None:
        elements.append(_mrkdwn(f"MTTR: {incident.mttr_seconds}s"))
    return {"type": "context", "elements": elements}


def _actions_block(incident: Incident) -> dict[str, Any]:
    return {
        "type": "actions",
        "block_id": "incident_approval_actions",
        "elements": [
            {
                "type": "button",
                "text": _plain("Approve"),
                "style": "primary",
                "action_id": "incident_approve",
                "value": incident.incident_id,
            },
            {
                "type": "button",
                "text": _plain("Reject"),
                "style": "danger",
                "action_id": "incident_reject",
                "value": incident.incident_id,
            },
            {
                "type": "button",
                "text": _plain("Escalate"),
                "action_id": "incident_escalate",
                "value": incident.incident_id,
            },
        ],
    }


def build_evidence_section(evidence: list[str]) -> dict[str, Any]:
    """Truncate to at most EVIDENCE_DISPLAY_LIMIT paths — never paste a log
    into the channel; link out to the rest via the evidence bundle."""
    if not evidence:
        return {"type": "section", "text": _mrkdwn("*Evidence:* none collected")}

    shown = evidence[:EVIDENCE_DISPLAY_LIMIT]
    lines = [f"• `{path}`" for path in shown]
    remaining = len(evidence) - len(shown)
    text = "*Evidence:*\n" + "\n".join(lines)
    if remaining > 0:
        text += f"\n_and {remaining} more file(s) in the evidence bundle_"
    return {"type": "section", "text": _mrkdwn(text)}


def build_parent_message(incident: Incident, run_id: str | None = None) -> tuple[list[dict[str, Any]], str]:
    """
    Build the Block Kit parent message for `incident`.

    Returns (blocks, fallback_text) — `fallback_text` is Slack's required
    plain-text summary for notifications and accessibility. Raises
    SlackBlockLimitError if the assembled payload would exceed a Slack
    limit; never let that surface as a failed post instead.
    """
    blocks: list[dict[str, Any]] = [_header_block(incident)]
    blocks.append(
        {"type": "section", "text": _mrkdwn(incident.impact_summary or "_no impact summary recorded_")}
    )
    blocks.append(_fields_block(incident))

    if incident.root_cause:
        blocks.append({"type": "section", "text": _mrkdwn(f"*Root cause:*\n{incident.root_cause}")})

    action_lines = []
    if incident.recommended_action:
        action_lines.append(f"*Recommended action:*\n{incident.recommended_action}")
    if incident.runbook:
        action_lines.append(f"*Runbook:* `{incident.runbook}`")
    if action_lines:
        blocks.append({"type": "section", "text": _mrkdwn("\n".join(action_lines))})

    if incident.evidence:
        blocks.append(build_evidence_section(incident.evidence))

    blocks.append(_context_block(incident, run_id))

    if incident.requires_approval and incident.status == "open":
        blocks.append(_actions_block(incident))

    fallback_text = (
        f"{severity_emoji(incident)} {incident.severity} {incident.incident_id} "
        f"— {incident.impact_summary or 'no impact summary recorded'}"
    )

    validate_blocks(blocks)
    return blocks, fallback_text


def build_thread_reply(text: str) -> tuple[list[dict[str, Any]], str]:
    """A simple single-section thread reply for events that don't need the
    full parent-message layout (acknowledgement, approval decisions,
    remediation status, resolution notes)."""
    blocks = [{"type": "section", "text": _mrkdwn(text)}]
    validate_blocks(blocks)
    return blocks, text


def build_logset_reply(summary: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """
    The thread reply that accompanies a log-set incident: which files were
    read, what was recognised in them, and where to download the exact set
    that produced the alert.

    Takes the plain summary dict from logsets.triage.logset_summary rather
    than an Incident — the log set is what this message is about, and
    keeping it a dict keeps this module free of a dependency on logsets/.
    """
    files = summary.get("files", [])
    findings = summary.get("findings", [])

    file_lines = [
        f"• `{entry['file']}` — {entry['line_count']} lines, "
        f"{entry['error_count']} error / {entry['warn_count']} warn "
        f"({entry['provider']} background)"
        for entry in files[:EVIDENCE_DISPLAY_LIMIT]
    ]
    remaining = len(files) - len(file_lines)
    if remaining > 0:
        file_lines.append(f"_and {remaining} more file(s) in the bundle_")

    header = (
        f"*Log set* `{summary.get('session_id', 'unknown')}` "
        f"(seed `{summary.get('seed')}`) — {len(files)} file(s), "
        f"{summary.get('lines_scanned', 0)} lines scanned, "
        f"{summary.get('error_count', 0)} error / {summary.get('warn_count', 0)} warn lines."
    )
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": _mrkdwn(header)},
        {"type": "section", "text": _mrkdwn("*Files read:*\n" + ("\n".join(file_lines) or "_none_"))},
    ]

    if findings:
        finding_lines = [
            f"• *{finding['title']}* (`{finding['signature_id']}`) — "
            f"`{finding['file']}` line {finding['line']}"
            for finding in findings[:EVIDENCE_DISPLAY_LIMIT]
        ]
        extra = len(findings) - len(finding_lines)
        if extra > 0:
            finding_lines.append(f"_and {extra} more match(es)_")
        recognised = "*Recognised signatures:*\n" + "\n".join(finding_lines)
    else:
        recognised = "*Recognised signatures:* none — no catalogued failure mode matched"

    unrecognised = summary.get("unrecognised_error_count", 0)
    if unrecognised:
        recognised += f"\n_{unrecognised} unrecognised error line(s) in the set_"
    blocks.append({"type": "section", "text": _mrkdwn(recognised)})

    download = summary.get("download", {}) or {}
    url, archive = download.get("url", ""), download.get("archive", "")
    if url:
        blocks.append({
            "type": "section",
            "text": _mrkdwn("*Download the logs behind this alert*"),
            "accessory": {
                "type": "button",
                "text": _plain("⬇ Download log set"),
                "url": url,
                "action_id": "logset_download",
            },
        })
    elif archive:
        blocks.append({"type": "section", "text": _mrkdwn(f"*Log set bundle:* `{archive}`")})

    fallback_text = (
        f"Log set {summary.get('session_id', 'unknown')}: {len(findings)} recognised "
        f"signature(s), {summary.get('error_count', 0)} error lines"
    )
    validate_blocks(blocks)
    return blocks, fallback_text
