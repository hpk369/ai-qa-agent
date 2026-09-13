"""
Slack Web API client — bot token only. The old SLACK_WEBHOOK_URL incoming
webhook (still used by n8n_workflows/qa_agent_workflow.json until T1.4)
cannot return a message ts, cannot update a message, and cannot carry
interactivity, so it cannot thread an incident. This client can.

SLACK_MODE=stub (the default when unset) writes every payload Slack would
have received to reports/slack/<incident_id>-<seq>.json and returns a
synthetic ts, making no network call at all — this is what keeps the repo
fully runnable and testable without a live Slack workspace. Set
SLACK_MODE=live once a real bot token and channels exist.

Block Kit content comes from agent.slack_blocks (T1.2), not from this
module — post_incident/update_parent/mirror_p1 all take (incident, blocks,
text) exactly like reply_thread does, rather than building content
internally. This is a deliberate, small departure from
IMPLEMENTATION.md's literal `post_incident(incident) -> str` signature
list: building blocks internally here would make this module import
agent.slack_blocks, which doesn't exist until the very next task, and
"tests pass before every commit" means T1.1 cannot depend on T1.2's module
existing yet. Applying reply_thread's own (incident, blocks, text) shape
uniformly avoids that forward dependency and keeps transport (this module)
cleanly separate from content-building (slack_blocks) — the caller (the
agent loop, or a test) is responsible for building blocks via
agent.slack_blocks and passing them in. post_change_log is the one
exception: its message is a single plain-text audit line, not a rendered
incident, so it still builds its own tiny block list.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

from agent.incident import Incident, persist

SLACK_API_BASE = "https://slack.com/api"
STUB_DIR = Path(os.path.join(os.path.dirname(__file__), "..", "reports", "slack"))

MAX_ATTEMPTS = 3
DEFAULT_RETRY_AFTER_SECONDS = 1


class SlackAPIError(RuntimeError):
    """Raised when a Slack API call fails after retrying, or Slack itself
    reports ok: false. Never carries the bot token in its message."""


def _redact(text: str, token: str | None) -> str:
    if token:
        text = text.replace(token, "***REDACTED***")
    return text


class SlackClient:
    def __init__(
        self,
        bot_token: str | None = None,
        mode: str | None = None,
        channel_alerts: str | None = None,
        channel_p1: str | None = None,
        channel_changes: str | None = None,
        channel_daily: str | None = None,
    ):
        self.bot_token = bot_token if bot_token is not None else os.getenv("SLACK_BOT_TOKEN", "")
        self.mode = (mode or os.getenv("SLACK_MODE", "stub")).lower()
        self.channel_alerts = channel_alerts or os.getenv("SLACK_CHANNEL_ALERTS", "")
        self.channel_p1 = channel_p1 or os.getenv("SLACK_CHANNEL_P1", "")
        self.channel_changes = channel_changes or os.getenv("SLACK_CHANNEL_CHANGES", "")
        self.channel_daily = channel_daily or os.getenv("SLACK_CHANNEL_DAILY", "")

    # ---------- low-level transport ----------

    def _stub_call(self, method: str, payload: dict, incident_id: str) -> dict:
        """
        Sequence number is derived from what's already on disk, not an
        in-memory counter — a fresh SlackClient() is created on most calls
        (notify_slack, record_approval_decision), so per-instance state
        would silently reset and overwrite earlier stub files for the
        same incident.
        """
        STUB_DIR.mkdir(parents=True, exist_ok=True)
        existing = list(STUB_DIR.glob(f"{incident_id}-*.json"))
        seq = len(existing) + 1

        record = {"method": method, "payload": payload}
        path = STUB_DIR / f"{incident_id}-{seq}.json"
        path.write_text(json.dumps(record, indent=2) + "\n")

        synthetic_ts = f"{time.time():.6f}"
        return {"ok": True, "ts": synthetic_ts, "channel": payload.get("channel", "")}

    def _live_call(self, method: str, payload: dict) -> dict:
        headers = {
            "Authorization": f"Bearer {self.bot_token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        last_error: str | None = None

        for attempt in range(MAX_ATTEMPTS):
            try:
                resp = httpx.post(
                    f"{SLACK_API_BASE}/{method}", json=payload, headers=headers, timeout=10.0
                )
            except httpx.RequestError as exc:
                last_error = _redact(str(exc), self.bot_token)
                time.sleep(2**attempt)
                continue

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", DEFAULT_RETRY_AFTER_SECONDS))
                time.sleep(retry_after)
                last_error = f"rate limited (429), retry-after {retry_after}s"
                continue

            if resp.status_code >= 500:
                last_error = f"Slack returned {resp.status_code}"
                time.sleep(2**attempt)
                continue

            data = resp.json()
            if not data.get("ok", False):
                raise SlackAPIError(
                    _redact(f"Slack API {method} failed: {data.get('error', 'unknown_error')}", self.bot_token)
                )
            return data

        raise SlackAPIError(
            _redact(f"Slack API {method} failed after {MAX_ATTEMPTS} attempts: {last_error}", self.bot_token)
        )

    def _call(self, method: str, payload: dict, incident_id: str) -> dict:
        if self.mode == "stub":
            return self._stub_call(method, payload, incident_id)
        return self._live_call(method, payload)

    # ---------- public API ----------

    def post_incident(self, incident: Incident, blocks: list[dict], text: str) -> str:
        """chat.postMessage the parent incident message. Writes the
        returned ts and channel back onto the incident and persists it
        before returning — an orphaned Slack message with no record is
        the failure mode to avoid, so persistence happens here, not left
        to the caller."""
        channel = self.channel_alerts  # every incident's parent message lives in #etl-prod-alerts
        payload = {"channel": channel, "blocks": blocks, "text": text}
        data = self._call("chat.postMessage", payload, incident.incident_id)

        incident.slack_channel = data.get("channel") or channel
        incident.slack_ts = data["ts"]
        try:
            persist(incident)
        except Exception as exc:  # noqa: BLE001 - deliberately broad: never lose the ts
            print(
                f"[slack_client] CRITICAL: posted {incident.incident_id} to Slack "
                f"(ts={incident.slack_ts}) but failed to persist the incident record: {exc}"
            )
        return incident.slack_ts

    def reply_thread(self, incident: Incident, blocks: list[dict], text: str) -> str:
        """chat.postMessage with thread_ts set to the parent message."""
        payload = {
            "channel": incident.slack_channel,
            "thread_ts": incident.slack_ts,
            "blocks": blocks,
            "text": text,
        }
        data = self._call("chat.postMessage", payload, incident.incident_id)
        return data["ts"]

    def update_parent(self, incident: Incident, blocks: list[dict], text: str) -> None:
        """chat.update on the incident's parent message."""
        payload = {
            "channel": incident.slack_channel,
            "ts": incident.slack_ts,
            "blocks": blocks,
            "text": text,
        }
        self._call("chat.update", payload, incident.incident_id)

    def mirror_to_p1(self, incident: Incident, blocks: list[dict], text: str) -> str:
        """Unconditionally post to #etl-prod-p1 with <!here>. mirror_p1
        gates this on severity == P1; an escalate decision (T1.5) calls
        this directly to force P1-channel visibility regardless of the
        incident's actual severity."""
        payload = {
            "channel": self.channel_p1,
            "blocks": blocks,
            "text": f"<!here> {text}",
        }
        data = self._call("chat.postMessage", payload, incident.incident_id)
        return data["ts"]

    def mirror_p1(self, incident: Incident, blocks: list[dict], text: str) -> str | None:
        """P1 only — mirror to #etl-prod-p1 with <!here>. Returns None for
        any other severity rather than posting nothing silently wrong."""
        if incident.severity != "P1":
            return None
        return self.mirror_to_p1(incident, blocks, text)

    def post_change_log(self, incident: Incident, action: str, approver: str) -> str:
        """Post a decision (approved/rejected/escalated/executed/...) to
        #etl-changes — the audit trail for every remediation action."""
        text = (
            f"*{incident.incident_id}* ({incident.severity}) — *{action}* by <@{approver}>"
        )
        payload = {
            "channel": self.channel_changes,
            "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
            "text": text,
        }
        data = self._call("chat.postMessage", payload, incident.incident_id)
        return data["ts"]
