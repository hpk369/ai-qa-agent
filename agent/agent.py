"""
Claude API agent with tool-use loop.
Calls all three QA tools, then synthesises a verdict.
Also exposed as a FastAPI endpoint for n8n to call.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import anthropic
import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.prompts import SYNTHESIS_PROMPT, SYSTEM_PROMPT
from agent.tools_manifest import TOOLS

MODEL = "claude-sonnet-4-6"
TOOL_SERVER_BASE = (
    f"http://{os.getenv('TOOL_SERVER_HOST', 'localhost')}"
    f":{os.getenv('TOOL_SERVER_PORT', '8000')}"
)

app = FastAPI(title="QA Agent Server", version="1.0.0")


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


# ---------- Agent loop ----------

def run_agent(pipeline_event: dict) -> dict[str, Any]:
    """
    Multi-turn Claude tool-use loop.
    1. Send pipeline event to Claude with tool definitions.
    2. Execute each tool call Claude requests.
    3. Feed results back until Claude returns final JSON verdict.
    """
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    user_message = (
        f"Pipeline run received. Analyse this event and call all three QA tools:\n\n"
        f"{json.dumps(pipeline_event, indent=2)}"
    )

    messages: list[dict] = [{"role": "user", "content": user_message}]
    tool_results_collected: list[dict] = []

    for _ in range(10):  # safety cap on turns
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        # Append assistant response to conversation
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            # Extract the final text block as the verdict JSON
            for block in response.content:
                if hasattr(block, "text"):
                    text = block.text.strip()
                    # Strip markdown code fences if present
                    if text.startswith("```"):
                        text = text.split("```")[1]
                        if text.startswith("json"):
                            text = text[4:]
                    return json.loads(text.strip())
            raise ValueError("No text block in final response")

        if response.stop_reason == "tool_use":
            tool_result_blocks = []
            for block in response.content:
                if block.type == "tool_use":
                    result = _call_tool(block.name, block.input)
                    tool_results_collected.append(
                        {"tool": block.name, "result": result}
                    )
                    tool_result_blocks.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result),
                        }
                    )

            messages.append({"role": "user", "content": tool_result_blocks})

            # If all three tools have been called, prompt for synthesis
            called_tools = {r["tool"] for r in tool_results_collected}
            if called_tools >= {"sql_validator", "log_analyser", "schema_comparator"}:
                messages.append({"role": "user", "content": SYNTHESIS_PROMPT})

    raise RuntimeError("Agent loop did not converge within iteration limit")


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
        verdict = run_agent(event.model_dump())
        return verdict
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
