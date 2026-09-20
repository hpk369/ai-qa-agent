"""
Claude API agent with tool-use loop.
Calls all three triage tools, then reports observed signals — severity is
never decided by the model; agent.severity.classify() derives it
deterministically from those signals. Also exposed as a FastAPI endpoint
for n8n to call.

Slack ownership: this process posts incidents to Slack itself (see
_notify_slack) rather than n8n's own Slack node doing it, and this
process's /slack/action endpoint — not an n8n webhook — is what Slack's
interactivity Request URL should point at. agent/slack_client.py's
post_incident() mutates and re-persists the Incident object in the same
call, which only makes sense from the process that owns persist(); moving
that logic into n8n would mean a second, untested implementation of Block
Kit rendering and HMAC verification in n8n's JS Code nodes. See
docs/workflow-map.md for the resulting (simplified) n8n topology.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse
from typing import Any

import anthropic
import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.evidence import collect_evidence
from agent.incident import Incident, load, open_incident, persist
from agent.prompts import SYNTHESIS_PROMPT, SYSTEM_PROMPT
from agent.runbooks import select_runbook
from agent.severity import classify, load_config
from agent.slack_blocks import build_parent_message
from agent.slack_client import SlackClient
from agent.slack_verify import verify_slack_request
from agent.tools_manifest import TOOLS
from logsets.session import DEFAULT_ROOT, bundle, list_sessions, load_session
from logsets.triage import analyse_logset, logset_summary, score, triage_logset

MODEL = os.getenv("AGENT_MODEL", "claude-sonnet-5")
TOOL_SERVER_BASE = (
    f"http://{os.getenv('TOOL_SERVER_HOST', 'localhost')}"
    f":{os.getenv('TOOL_SERVER_PORT', '8000')}"
)
REQUIRED_TOOLS = {"sql_validator", "log_analyser", "schema_comparator"}

# Canonical order for the response's checks_performed list. Filtered
# down to whichever tools were
# actually called on a given run.
TOOL_SHORT_NAMES = [
    ("sql_validator", "recon"),
    ("log_analyser", "logs"),
    ("schema_comparator", "schema"),
]

app = FastAPI(title="ETL Production Support Triage Agent", version="2.0.0")


# ---------- Tool execution ----------

def _call_tool(tool_name: str, tool_input: dict) -> dict:
    """Execute a tool by calling the tool server HTTP endpoint."""
    url = f"{TOOL_SERVER_BASE}/tools/{tool_name}"
    try:
        resp = httpx.post(url, json=tool_input, timeout=30.0)
        resp.raise_for_status()
        return resp.json()
    except httpx.RequestError as exc:
        return {"status": "ERROR", "error": f"Tool server unreachable: {exc}"}
    except httpx.HTTPStatusError as exc:
        return {"status": "ERROR", "error": f"Tool server returned {exc.response.status_code}"}


def _checks_performed(called_tools: set[str]) -> list[str]:
    return [short for tool, short in TOOL_SHORT_NAMES if tool in called_tools]


def _attach_evidence(incident: Incident, pipeline_event: dict[str, Any]) -> None:
    """
    Collect the evidence bundle (agent.evidence) for a newly opened
    incident and set incident.evidence to the resulting artifact paths.
    Never raises — a run that correctly opened an incident must not fail
    just because evidence collection had trouble; a missing/failed
    collector already degrades to a "missing" manifest entry inside
    collect_evidence itself, but this is a second layer of defense around
    collect_evidence failing outright (e.g. disk full writing the bundle).
    """
    try:
        bundle_dir = collect_evidence(
            incident.incident_id,
            source_table=pipeline_event.get("source_table", "src.transactions"),
            target_table=pipeline_event.get("target_table", "tgt.transactions"),
            log_path=pipeline_event.get("log_path", ""),
        )
        manifest = json.loads((bundle_dir / "manifest.json").read_text())
        incident.evidence = [entry["path"] for entry in manifest["artifacts"] if "path" in entry]
    except Exception as exc:  # noqa: BLE001 - evidence is supplementary, never blocks the incident
        print(f"[agent] WARNING: evidence collection failed for {incident.incident_id}: {exc}")


# ---------- Response assembly (pure — no network calls, unit-testable) ----------

def build_response(
    pipeline_event: dict[str, Any],
    agent_output: dict[str, Any],
    called_tools: set[str],
    duration_ms: int,
    severity_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Turn Claude's reported signals into the agent's response contract.
    Severity, and therefore whether an incident opens at all, is decided
    here deterministically by agent.severity.classify — never by the model.
    """
    config = severity_config or load_config()
    signals = agent_output.get("signals", {})
    severity_result = classify(signals, config)

    incident_dict = None
    if severity_result.severity:
        run_context = {
            "detected_by": agent_output.get("detected_by", "agent"),
            "affected_job": agent_output.get(
                "affected_job", pipeline_event.get("pipeline", "unknown")
            ),
            "affected_objects": agent_output.get("affected_objects", []),
            "rows_expected": agent_output.get("rows_expected"),
            "rows_loaded": agent_output.get("rows_loaded"),
            "impact_summary": agent_output.get("impact_summary", ""),
            "evidence": agent_output.get("evidence", []),
            "root_cause": agent_output.get("root_cause"),
            "confidence": agent_output.get("confidence", 0.0),
            # Deterministic, like severity — see agent/runbooks.py.
            "runbook": select_runbook(signals),
            "recommended_action": agent_output.get("recommended_action"),
            # Deterministic, not model-decided: P1/P2 always require a
            # recorded human approval before remediation.
            "requires_approval": severity_result.severity in {"P1", "P2"},
        }
        incident = open_incident(signals, severity_result, run_context)
        _attach_evidence(incident, pipeline_event)
        persist(incident)
        incident_dict = incident.to_dict()

    return {
        "run_id": pipeline_event.get("run_id", ""),
        "incident": incident_dict,
        "clean": incident_dict is None,
        "checks_performed": _checks_performed(called_tools),
        "duration_ms": duration_ms,
    }


def notify_slack(response: dict[str, Any]) -> None:
    """
    Post a newly opened incident to Slack (and mirror to #etl-prod-p1 if
    it's a P1). Slack is a view onto the incident record, never the
    source of truth, so a Slack failure here is logged loudly and never
    raised — a run that opened a valid incident must not fail just
    because Slack was unreachable. No-op on a clean run.
    """
    incident_dict = response.get("incident")
    if not incident_dict:
        return

    incident = Incident.from_dict(incident_dict)
    try:
        client = SlackClient()
        blocks, text = build_parent_message(incident, run_id=response.get("run_id"))
        client.post_incident(incident, blocks, text)
        if incident.severity == "P1":
            client.mirror_p1(incident, blocks, text)
        response["incident"] = incident.to_dict()  # picks up slack_channel/slack_ts
    except Exception as exc:  # noqa: BLE001 - Slack is a view, never the source of truth
        print(f"[agent] WARNING: failed to post incident {incident.incident_id} to Slack: {exc}")


# ---------- Agent loop ----------

def run_agent(pipeline_event: dict) -> dict[str, Any]:
    """
    Multi-turn Claude tool-use loop.
    1. Send pipeline event to Claude with tool definitions.
    2. Execute each tool call Claude requests.
    3. Feed results back until Claude reports its final observed signals.
    4. Classify severity deterministically and build the response contract.
    """
    start = time.monotonic()
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    user_message = (
        f"Pipeline run received. Analyse this event and call all three "
        f"triage tools:\n\n{json.dumps(pipeline_event, indent=2)}"
    )

    messages: list[dict] = [{"role": "user", "content": user_message}]
    called_tools: set[str] = set()
    agent_output: dict[str, Any] | None = None

    for _ in range(10):  # safety cap on turns
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    text = block.text.strip()
                    if text.startswith("```"):
                        text = text.split("```")[1]
                        if text.startswith("json"):
                            text = text[4:]
                    agent_output = json.loads(text.strip())
                    break
            if agent_output is None:
                raise ValueError("No text block in final response")
            break

        if response.stop_reason == "tool_use":
            tool_result_blocks = []
            for block in response.content:
                if block.type == "tool_use":
                    result = _call_tool(block.name, block.input)
                    called_tools.add(block.name)
                    tool_result_blocks.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result),
                        }
                    )

            messages.append({"role": "user", "content": tool_result_blocks})

            if called_tools >= REQUIRED_TOOLS:
                messages.append({"role": "user", "content": SYNTHESIS_PROMPT})
    else:
        raise RuntimeError("Agent loop did not converge within iteration limit")

    duration_ms = int((time.monotonic() - start) * 1000)
    response = build_response(pipeline_event, agent_output, called_tools, duration_ms)
    notify_slack(response)
    return response


# ---------- FastAPI endpoint ----------

class PipelineEvent(BaseModel):
    run_id: str
    pipeline: str = "customer_transactions"
    timestamp: str = ""
    source_table: str = "src.transactions"
    target_table: str = "tgt.transactions"
    log_path: str = ""
    failure_mode: str = "none"


@app.post("/agent/run")
def agent_run(event: PipelineEvent):
    try:
        return run_agent(event.model_dump())
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


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
    Handle one verified Slack interactivity payload. The endpoint
    plumbing (verification, immediate ack, dispatch) lives here; the
    decision itself is recorded by
    agent.incident.record_approval_decision — this function just wires
    the two together and logs anything it can't process rather than
    raising (there's no HTTP response left to return by the time this
    runs — see the endpoint's docstring on the 3-second ack rule).
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
    agent/slack_blocks.py's actions block). Slack's own Request URL should
    point here directly, not at n8n — see this module's docstring.

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


# ---------- Log-set endpoints ----------
#
# The log-set path is the project's scope: a session's log set is mixed,
# analysed, classified by the same deterministic classifier the tool-use
# path uses, alerted to Slack, and downloadable as the exact zip of logs
# that produced the alert. It needs no Claude API key and no pipeline
# stack — only the logs.

SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


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


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    host = os.getenv("AGENT_SERVER_HOST", "0.0.0.0")
    port = int(os.getenv("AGENT_SERVER_PORT", "8001"))
    uvicorn.run(app, host=host, port=port)
