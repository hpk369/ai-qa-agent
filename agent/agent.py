"""
The agent server: HTTP entry points for log-set triage and for Slack's
button interactions.

Triage itself lives in logsets/triage.py — this module is transport. Two
surfaces:

* ``/logset/*`` — mix a session's log set, triage it, alert Slack, and
  serve the exact logs that produced the alert as a downloadable zip.
* ``/slack/action`` — Slack's interactivity Request URL. Approve / Reject
  / Escalate buttons post here, not to any orchestrator; the decision is
  recorded on the incident record by agent.incident.

Slack ownership sits here rather than in an external workflow tool
because agent/slack_client.py's post_incident() mutates and re-persists
the Incident in the same call, which only makes sense from the process
that owns persist().
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.incident import load
from agent.slack_verify import verify_slack_request
from logsets.session import DEFAULT_ROOT, bundle, list_sessions, load_session
from logsets.triage import analyse_logset, logset_summary, score, triage_logset

app = FastAPI(title="ETL Production Support Triage Agent", version="3.0.0")

SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


# ---------- Log sets ----------

class LogSetRequest(BaseModel):
    seed: int | None = None
    session_id: str | None = None
    sources: list[str] | None = None
    source_count: int | None = None
    injections: int | None = None
    clean: bool = False
    notify: bool = True


def _validate_session_id(session_id: str) -> str:
    """Path-segment safety: session ids reach the filesystem, so anything
    that is not one of our own generated ids is rejected outright rather
    than normalised."""
    if not SESSION_ID_PATTERN.match(session_id) or session_id in {".", ".."}:
        raise HTTPException(status_code=400, detail="invalid session id")
    return session_id


@app.post("/logset/run")
def logset_run(request: LogSetRequest):
    """Mix a log set for this session, triage it, alert Slack, and return
    the incident plus the download link."""
    if request.session_id:
        _validate_session_id(request.session_id)
    try:
        return triage_logset(**request.model_dump())
    except ValueError as exc:  # unknown source name
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/logset")
def logset_list():
    return {"sessions": list_sessions(), "root": str(DEFAULT_ROOT)}


@app.get("/logset/{session_id}")
def logset_detail(session_id: str):
    """Re-read a previously built log set. Read-only: no incident opens
    and nothing is posted to Slack — that happened when it was built."""
    _validate_session_id(session_id)
    try:
        logset = load_session(session_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    analysis = analyse_logset(logset)
    archive = bundle(logset)
    return {
        "logset": logset_summary(logset, analysis, archive),
        "signals": analysis["signals"],
        "score": score(logset, analysis),
    }


@app.get("/logset/{session_id}/download")
def logset_download(session_id: str):
    """Download this session's log set — every log file, the manifest with
    the mixer's ground truth, and a README."""
    _validate_session_id(session_id)
    try:
        logset = load_session(session_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    archive = bundle(logset, force=True)
    return FileResponse(
        path=archive,
        media_type="application/zip",
        filename=f"{session_id}.zip",
    )


# ---------- Slack interactivity ----------

def _extract_action(action_payload: dict[str, Any]) -> tuple[str, str, str] | None:
    """Pull (action_id, incident_id, user_id) out of a Slack block_actions
    interaction payload. Returns None if the payload doesn't look like one
    of our incident buttons (e.g. Slack's own url_verification/other
    payload shapes)."""
    actions = action_payload.get("actions") or []
    if not actions:
        return None
    action = actions[0]
    user = action_payload.get("user", {})
    return action.get("action_id", ""), action.get("value", ""), user.get("id", "")


def process_slack_action(action_payload: dict[str, Any]) -> None:
    """
    Handle one verified Slack interactivity payload: resolve the incident
    it refers to and record the decision. Logs anything it can't process
    rather than raising — there's no HTTP response left to return by the
    time this runs (see the endpoint's docstring on the 3-second ack rule).
    """
    extracted = _extract_action(action_payload)
    if extracted is None:
        print(f"[agent] WARNING: /slack/action received an unrecognised payload: {action_payload!r}")
        return

    action_id, incident_id, approver = extracted
    try:
        incident = load(incident_id)
    except FileNotFoundError:
        print(f"[agent] WARNING: /slack/action referenced unknown incident {incident_id!r}")
        return

    from agent.incident import record_approval_decision  # local import: avoids a cycle

    decision = {
        "incident_approve": "approved",
        "incident_reject": "rejected",
        "incident_escalate": "escalated",
    }.get(action_id)
    if decision is None:
        print(f"[agent] WARNING: /slack/action received unknown action_id {action_id!r}")
        return

    try:
        record_approval_decision(incident, decision, approver)
    except Exception as exc:  # noqa: BLE001 - nowhere left to report this but the log
        print(f"[agent] WARNING: failed to process {decision} on {incident_id}: {exc}")


@app.post("/slack/action")
async def slack_action(request: Request, background_tasks: BackgroundTasks):
    """
    Slack interactivity endpoint (Approve/Reject/Escalate buttons —
    agent/slack_blocks.py's actions block). Point the Slack app's
    Request URL here.

    Verification happens synchronously (it's a local HMAC computation, not
    a network call, so it costs microseconds) and an invalid signature is
    rejected with 401 before anything else touches the payload. Once
    verified, Slack's 3-second response deadline is non-negotiable: this
    handler acknowledges immediately and hands the actual processing
    (which does make further Slack API calls) to a background task rather
    than doing it before responding.
    """
    raw_body = await request.body()
    headers = dict(request.headers)
    signing_secret = os.getenv("SLACK_SIGNING_SECRET", "")

    if not verify_slack_request(headers, raw_body, signing_secret):
        raise HTTPException(status_code=401, detail="invalid Slack request signature")

    form = urllib.parse.parse_qs(raw_body.decode("utf-8"))
    payload_str = form.get("payload", ["{}"])[0]
    action_payload = json.loads(payload_str)

    background_tasks.add_task(process_slack_action, action_payload)
    return {"ok": True}


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    host = os.getenv("AGENT_SERVER_HOST", "0.0.0.0")
    port = int(os.getenv("AGENT_SERVER_PORT", "8001"))
    uvicorn.run(app, host=host, port=port)
