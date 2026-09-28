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
* ``/incidents`` and ``/incident/*`` — the approval console: a triage run
  pushes its incident here, and the Slack alert links to a page that
  records the decision. This is the path that works when the alert was
  posted by CI, where the runner holding the incident is long gone by the
  time anyone clicks. See agent/console.py.

Slack ownership sits here rather than in an external workflow tool
because agent/slack_client.py's post_incident() mutates and re-persists
the Incident in the same call, which only makes sense from the process
that owns persist().
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import urllib.parse
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.console import (
    DECISIONS,
    DECISION_LABELS,
    already_decided,
    approver_from_request,
    load_for_console,
    store_pushed_incident,
    token_accepted,
)
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


# ---------- the approval console ----------

def _console_page(title: str, body: str, status_line: str = "") -> HTMLResponse:
    """One self-contained page. No CDN, no build step — this is served from
    the same process that triages, and an approval screen that cannot render
    because a stylesheet host is unreachable is worse than a plain one."""
    return HTMLResponse(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
  :root {{ color-scheme: light dark; --fg:#1a1a1a; --bg:#fbfbfa; --muted:#666;
           --line:#e3e3e0; --card:#fff; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --fg:#e8e8e6; --bg:#191918; --muted:#a0a09a; --line:#33332f; --card:#222221; }}
  }}
  body {{ margin:0; padding:24px 16px; background:var(--bg); color:var(--fg);
          font:16px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif; }}
  main {{ max-width:680px; margin:0 auto; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
           padding:20px; margin-bottom:16px; }}
  h1 {{ font-size:20px; margin:0 0 4px; }}
  .muted {{ color:var(--muted); font-size:14px; }}
  dl {{ display:grid; grid-template-columns:auto 1fr; gap:6px 16px; margin:16px 0 0; font-size:14px; }}
  dt {{ color:var(--muted); }} dd {{ margin:0; }}
  pre {{ white-space:pre-wrap; word-break:break-word; font-size:13px; margin:8px 0 0; }}
  form {{ display:flex; gap:10px; flex-wrap:wrap; margin:0; }}
  button {{ font:inherit; font-weight:600; padding:10px 18px; border-radius:8px;
            border:1px solid var(--line); cursor:pointer; background:var(--card); color:var(--fg); }}
  button.approve {{ background:#1f7a43; border-color:#1f7a43; color:#fff; }}
  button.reject  {{ background:#a02b2b; border-color:#a02b2b; color:#fff; }}
  .banner {{ padding:12px 16px; border:1px solid var(--line); border-radius:8px;
             margin-bottom:16px; font-size:14px; }}
</style></head><body><main>
{status_line}
{body}
</main></body></html>""")


@app.post("/incidents")
async def ingest_incident(request: Request):
    """Accept an incident pushed by a triage run.

    This is what makes the console usable at all from CI. The runner that
    opens an incident writes it to its own ephemeral disk and is destroyed;
    pushing the record here gives the console something to act on.
    """
    if not token_accepted(request.headers.get("Authorization", "").removeprefix("Bearer ").strip()):
        raise HTTPException(status_code=401, detail="invalid or missing ingest token")
    try:
        incident = store_pushed_incident(await request.json())
    except Exception as exc:  # noqa: BLE001 - a malformed push is the caller's bug to see
        raise HTTPException(status_code=400, detail=f"could not store incident: {exc}") from exc
    return {"ok": True, "incident_id": incident.incident_id}


@app.get("/incident/{incident_id}", response_class=HTMLResponse)
def console_incident(incident_id: str, request: Request, intent: str | None = None):
    """The page a Slack button opens.

    `intent` only preselects which action the reader arrived for; it never
    decides anything. Opening a link must stay safe — Slack unfurls URLs,
    and chat clients prefetch them.
    """
    approver = approver_from_request(request.headers)
    if approver is None:
        return _console_page(
            "Not authenticated",
            '<div class="card"><h1>Not authenticated</h1>'
            '<p class="muted">This request did not arrive through Cloudflare Access, so there is '
            'no verified identity to record against a decision. Reach this console through its '
            'public hostname rather than the origin directly.</p></div>')

    incident = load_for_console(incident_id)
    if incident is None:
        return _console_page(
            "Unknown incident",
            f'<div class="card"><h1>Unknown incident</h1><p class="muted">'
            f'<code>{html.escape(incident_id)}</code> was never pushed to this console. '
            f'The triage run that opened it may have had no <code>AGENT_INGEST_URL</code> '
            f'configured.</p></div>')

    decided = already_decided(incident)
    banner = (f'<div class="banner">Signed in as <strong>{html.escape(approver)}</strong></div>')

    details = f"""<div class="card">
  <h1>{html.escape(incident.incident_id)} &middot; {html.escape(incident.severity)}</h1>
  <div class="muted">{html.escape(incident.status)}</div>
  <pre>{html.escape(incident.impact_summary or '')}</pre>
  <dl>
    <dt>Job</dt><dd>{html.escape(incident.affected_job or '—')}</dd>
    <dt>Root cause</dt><dd>{html.escape(incident.root_cause or '—')}</dd>
    <dt>Runbook</dt><dd>{html.escape(incident.runbook or '—')}</dd>
    <dt>Opened</dt><dd>{html.escape(incident.opened_at or '—')}</dd>
  </dl>
</div>"""

    if decided:
        actions = (f'<div class="card"><h1>Already decided</h1>'
                   f'<p class="muted">{html.escape(decided)}. A second decision is refused, so '
                   f'there is nothing to act on here.</p></div>')
    else:
        preselect = intent if intent in DECISIONS else ""
        actions = f"""<div class="card">
  <p class="muted">Recording a decision updates the incident, replies in its Slack thread and
  posts to the change log.{' You arrived via <strong>' + html.escape(DECISION_LABELS[preselect]) + '</strong>.' if preselect else ''}</p>
  <form method="post" action="/incident/{html.escape(incident_id)}/decision">
    <button class="approve" name="decision" value="approved">Approve</button>
    <button class="reject" name="decision" value="rejected">Reject</button>
    <button name="decision" value="escalated">Escalate</button>
  </form>
</div>"""

    return _console_page(f"{incident.incident_id} — approval", details + actions, banner)


@app.post("/incident/{incident_id}/decision", response_class=HTMLResponse)
async def console_decision(incident_id: str, request: Request):
    """Record the decision. The work itself is agent.incident's, unchanged
    from the Slack-button path — this only supplies a verified approver."""
    approver = approver_from_request(request.headers)
    if approver is None:
        raise HTTPException(status_code=401, detail="no Cloudflare Access identity on this request")

    # Parsed by hand rather than via request.form(), which would pull in
    # python-multipart for one urlencoded field. /slack/action already reads
    # its body the same way.
    body = urllib.parse.parse_qs((await request.body()).decode("utf-8"))
    decision = (body.get("decision") or [""])[0]
    if decision not in DECISIONS:
        raise HTTPException(status_code=400, detail=f"unknown decision: {decision!r}")

    incident = load_for_console(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail=f"unknown incident: {incident_id}")

    from agent.incident import record_approval_decision

    record_approval_decision(incident, decision, approver)
    return _console_page(
        f"{incident_id} — {decision}",
        f'<div class="card"><h1>Recorded</h1><p class="muted">'
        f'<strong>{html.escape(decision)}</strong> by {html.escape(approver)}. '
        f'The Slack thread and the change log have been updated. You can close this tab.</p></div>')


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    host = os.getenv("AGENT_SERVER_HOST", "0.0.0.0")
    port = int(os.getenv("AGENT_SERVER_PORT", "8001"))
    uvicorn.run(app, host=host, port=port)
