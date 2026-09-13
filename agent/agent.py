"""
Claude API agent with tool-use loop.
Calls all three triage tools, then reports observed signals — severity is
never decided by the model; agent.severity.classify() derives it
deterministically from those signals. Also exposed as a FastAPI endpoint
for n8n to call.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

import anthropic
import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.incident import open_incident, persist
from agent.prompts import SYNTHESIS_PROMPT, SYSTEM_PROMPT
from agent.severity import classify, load_config
from agent.tools_manifest import TOOLS

MODEL = os.getenv("AGENT_MODEL", "claude-sonnet-5")
TOOL_SERVER_BASE = (
    f"http://{os.getenv('TOOL_SERVER_HOST', 'localhost')}"
    f":{os.getenv('TOOL_SERVER_PORT', '8000')}"
)
REQUIRED_TOOLS = {"sql_validator", "log_analyser", "schema_comparator"}

# Canonical, spec-defined order for the response's checks_performed list —
# see IMPLEMENTATION.md T0.4. Filtered down to whichever tools were
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
            "runbook": agent_output.get("runbook"),
            "recommended_action": agent_output.get("recommended_action"),
            # Deterministic, not model-decided: P1/P2 always require a
            # recorded human approval before remediation (see T1.5).
            "requires_approval": severity_result.severity in {"P1", "P2"},
        }
        incident = open_incident(signals, severity_result, run_context)
        persist(incident)
        incident_dict = incident.to_dict()

    return {
        "run_id": pipeline_event.get("run_id", ""),
        "incident": incident_dict,
        "clean": incident_dict is None,
        "checks_performed": _checks_performed(called_tools),
        "duration_ms": duration_ms,
    }


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
    return build_response(pipeline_event, agent_output, called_tools, duration_ms)


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


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    host = os.getenv("AGENT_SERVER_HOST", "0.0.0.0")
    port = int(os.getenv("AGENT_SERVER_PORT", "8001"))
    uvicorn.run(app, host=host, port=port)
