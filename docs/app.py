"""
Demo server for the AI QA Agent pipeline.
Self-contained: runs without Docker, Postgres, or Kafka.
Streams agent reasoning step-by-step via SSE.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mock_pipeline.failures import FailureMode, get_log_data, get_source_data, get_target_data
from agent_tools.sql_validator import SQLValidator
from agent_tools.log_analyser import LogAnalyser
from agent_tools.schema_comparator import SchemaComparator

app = FastAPI(title="AI QA Agent Demo")

# ---------- Simulated agent reasoning per failure mode ----------

VERDICTS = {
    FailureMode.NONE: {
        "verdict": "PASS",
        "root_cause": "All QA checks passed — pipeline is healthy.",
        "details": [],
        "recommended_action": "No action required. Pipeline completed within all tolerance thresholds.",
        "confidence": 0.98,
    },
    FailureMode.ROW_DROP: {
        "verdict": "FAIL",
        "root_cause": "Target table has 40% fewer rows than source — likely a partition skew or silent deduplication error in the Spark job.",
        "details": [
            "Row drop 40.0% (threshold: 5%)",
            "ERROR: 40000 records dropped during deduplication stage",
            "WARN: Executor memory pressure at 85%",
        ],
        "recommended_action": "Inspect Spark deduplication step; check partition skew in CustomerTransformStep and increase executor memory.",
        "confidence": 0.95,
    },
    FailureMode.SCHEMA_DRIFT: {
        "verdict": "FAIL",
        "root_cause": "Column 'account_balance' renamed to 'bal' in target schema — likely a migration script applied to the wrong environment.",
        "details": [
            "Column renamed: account_balance → bal",
            "ERROR: Column 'account_balance' not found in target schema",
        ],
        "recommended_action": "Revert the target schema migration or update the pipeline transformation to align with the new column name.",
        "confidence": 0.97,
    },
    FailureMode.NULL_SPIKE: {
        "verdict": "FAIL",
        "root_cause": "customer_id null rate spiked to 35% — NullPointerException in CustomerTransformStep suggests a broken join key.",
        "details": [
            "customer_id null rate 35% (threshold: 5%)",
            "ERROR: NullPointerException in CustomerTransformStep at line 84",
            "ERROR: Null propagation detected in customer_id join key",
        ],
        "recommended_action": "Fix the null-safe join in CustomerTransformStep line 84; add NOT NULL constraint to customer_id in the target DDL.",
        "confidence": 0.96,
    },
    FailureMode.LATENCY: {
        "verdict": "FAIL",
        "root_cause": "Kafka consumer lag of 15,000 messages exceeds the 10,000 threshold — consumer group is falling behind.",
        "details": [
            "Kafka consumer lag: 15000 messages (threshold: 10000)",
            "ERROR: Kafka consumer lag exceeded 10000 messages",
        ],
        "recommended_action": "Scale out the Kafka consumer group or investigate back-pressure in the downstream Spark streaming job.",
        "confidence": 0.93,
    },
}

TOOL_DELAYS = {"sql_validator": 0.9, "log_analyser": 0.7, "schema_comparator": 0.8}


async def _stream_run(failure_mode: FailureMode, run_id: str) -> AsyncGenerator[str, None]:
    def event(data: dict) -> str:
        return f"data: {json.dumps(data)}\n\n"

    yield event({"type": "start", "run_id": run_id, "failure_mode": failure_mode.value})
    await asyncio.sleep(0.3)

    # --- Tool: sql_validator ---
    yield event({"type": "tool_call", "tool": "sql_validator",
                 "input": {"source_table": "src.transactions", "target_table": "tgt.transactions", "run_id": run_id}})
    await asyncio.sleep(TOOL_DELAYS["sql_validator"])
    sql_result = SQLValidator(failure_mode=failure_mode).validate("src.transactions", "tgt.transactions", run_id)
    yield event({"type": "tool_result", "tool": "sql_validator", "result": sql_result})
    await asyncio.sleep(0.2)

    # --- Tool: log_analyser ---
    yield event({"type": "tool_call", "tool": "log_analyser",
                 "input": {"log_path": f"/logs/spark_run_{run_id}.log", "run_id": run_id}})
    await asyncio.sleep(TOOL_DELAYS["log_analyser"])
    log_result = LogAnalyser(failure_mode=failure_mode).analyse(f"/mock/{run_id}.log")
    yield event({"type": "tool_result", "tool": "log_analyser", "result": log_result})
    await asyncio.sleep(0.2)

    # --- Tool: schema_comparator ---
    yield event({"type": "tool_call", "tool": "schema_comparator",
                 "input": {"source_table": "src.transactions", "target_table": "tgt.transactions"}})
    await asyncio.sleep(TOOL_DELAYS["schema_comparator"])
    schema_result = SchemaComparator(failure_mode=failure_mode).compare("src.transactions", "tgt.transactions")
    yield event({"type": "tool_result", "tool": "schema_comparator", "result": schema_result})
    await asyncio.sleep(0.4)

    # --- LLM synthesis ---
    yield event({"type": "synthesising"})
    await asyncio.sleep(1.1)

    verdict_data = VERDICTS[failure_mode]
    yield event({"type": "verdict", **verdict_data,
                 "test_framework": "Robot Framework" if verdict_data["verdict"] == "PASS" else "pytest"})

    await asyncio.sleep(0.3)
    yield event({"type": "tests_start",
                 "framework": "Robot Framework" if verdict_data["verdict"] == "PASS" else "pytest"})
    await asyncio.sleep(1.2)

    if verdict_data["verdict"] == "PASS":
        yield event({"type": "tests_done", "framework": "Robot Framework", "passed": 6, "failed": 0,
                     "cases": ["Tool Server Is Healthy", "Pipeline Run Completes Successfully",
                               "Schema Should Match Source", "Logs Should Contain No Errors",
                               "Row Count Within Tolerance", "Null Rate Within Tolerance For Amount"]})
    else:
        failed_tests = {
            FailureMode.ROW_DROP: (
                [("test_clean_run_passes", False), ("test_row_drop_detected", True),
                 ("test_null_spike_detected", True), ("test_duplicate_detection", True),
                 ("test_mock_clean_passes", False), ("test_mock_row_drop_fails", True),
                 ("test_all_pass_on_none", False), ("test_sql_detects_row_drop", True)],
                6, 2
            ),
            FailureMode.SCHEMA_DRIFT: (
                [("test_clean_schema_passes", False), ("test_renamed_column_detected", True),
                 ("test_removed_column_detected", True), ("test_mock_schema_drift_fails", True),
                 ("test_schema_detects_drift", True), ("test_mock_clean_passes", False),
                 ("test_all_pass_on_none", False), ("test_sql_unaffected_by_schema_drift", True)],
                5, 3
            ),
            FailureMode.NULL_SPIKE: (
                [("test_clean_run_passes", False), ("test_null_spike_detected", True),
                 ("test_mock_null_spike_fails", True), ("test_sql_detects_null_spike", True),
                 ("test_log_detects_null_errors", True), ("test_mock_clean_passes", False),
                 ("test_all_pass_on_none", False), ("test_missing_log_reports_error", False)],
                4, 4
            ),
            FailureMode.LATENCY: (
                [("test_mock_latency_fails", True), ("test_kafka_lag_detected", True),
                 ("test_status_is_fail_on_lag", True), ("test_log_detects_kafka_lag", True),
                 ("test_mock_clean_passes", False), ("test_all_pass_on_none", False),
                 ("test_sql_unaffected_by_latency", True), ("test_mock_null_spike_fails", False)],
                5, 3
            ),
        }.get(failure_mode, ([], 0, 0))
        cases, passed, failed = failed_tests
        yield event({"type": "tests_done", "framework": "pytest",
                     "passed": passed, "failed": failed,
                     "cases": [{"name": n, "passed": p} for n, p in cases]})

    yield event({"type": "done"})


# ---------- Routes ----------

class RunRequest(BaseModel):
    failure_mode: str = "none"


@app.get("/run")
async def run_pipeline(failure_mode: str = "none"):
    try:
        mode = FailureMode(failure_mode)
    except ValueError:
        mode = FailureMode.NONE
    run_id = str(uuid.uuid4())[:8]

    return StreamingResponse(
        _stream_run(mode, run_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = os.path.join(os.path.dirname(__file__), "index.html")
    with open(html_path) as f:
        return f.read()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=7860, reload=False)
