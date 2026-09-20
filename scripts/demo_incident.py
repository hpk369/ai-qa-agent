#!/usr/bin/env python3
"""
demo_incident.py — trigger a realistic incident and (optionally) walk it
through its whole lifecycle, for demos and for verifying a real Slack
setup, without needing a live Claude API call.

Reuses the exact same code the real agent loop uses (agent.agent.build_response,
agent.agent.notify_slack, agent.incident.record_approval_decision/resolve_incident)
against a synthetic `agent_output` shaped like what a compliant model call
would produce for each mode — the same fixtures tests/pytest/test_agent_response.py
asserts against, so a demo run and a test run are checking the same thing.

Respects the same SLACK_MODE/SLACK_BOT_TOKEN/... environment variables as
the real agent server: run with SLACK_MODE=stub (the default) to preview
locally with zero setup, or point a real .env at a live workspace and this
posts for real — see docs/SLACK_SETUP.md.

Usage:
    scripts/demo_incident.py <mode> [--lifecycle] [--actor USER_ID] [--json]

    <mode>        one of: none, row_drop, schema_drift, null_spike, latency
    --lifecycle   walk the opened incident through
                  acknowledged -> approved -> remediating -> verifying -> resolved
                  (no-op with a warning for `none`, which opens no incident)
    --actor       Slack user ID to attribute lifecycle actions to (default: U_DEMO)
    --json        print the final incident record as JSON instead of a narration
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.agent import build_response, notify_slack
from agent.incident import load, record_approval_decision, resolve_incident, set_status
from agent.severity import load_config

ALL_TOOLS = {"sql_validator", "log_analyser", "schema_comparator"}


def _clean_signals() -> dict:
    return {
        "target_unavailable": False,
        "control_total_mismatch": False,
        "job_failed_no_path_to_sla": False,
        "row_variance_pct": 0.0,
        "sla_breach_projected": False,
        "downstream_jobs_blocked": 0,
        "null_rate_increase_pct": {},
        "job_duration_vs_baseline_pct": 100.0,
        "log_anomaly_no_data_impact": False,
    }


# Each scenario mirrors the exact signal shape tests/pytest/test_agent_response.py
# exercises for that failure mode, and mock_pipeline/failures.py's injection.
SCENARIOS = {
    "none": dict(
        detected_by="none",
        signals=_clean_signals(),
        impact_summary="No issues detected.",
        confidence=0.98,
    ),
    "row_drop": dict(
        detected_by="sql_validator",
        signals={**_clean_signals(), "row_variance_pct": 40.0},
        affected_job="cx_customer_load",
        affected_objects=["target.customer_dim"],
        rows_expected=100_000,
        rows_loaded=60_000,
        impact_summary="Target table is missing roughly 40% of expected records; downstream reporting on customer transactions will undercount.",
        root_cause="40,000 records dropped during deduplication in CustomerTransformStep; log shows partition skew and executor memory pressure at 85%.",
        recommended_action="Inspect the deduplication step for partition skew; consider repartitioning on transaction_id before the dedup stage.",
        confidence=0.95,
    ),
    "schema_drift": dict(
        detected_by="schema_comparator",
        signals={**_clean_signals(), "job_failed_no_path_to_sla": True},
        affected_job="cx_customer_load",
        affected_objects=["target.customer_dim"],
        impact_summary="account_balance is entirely absent from the target load; any downstream job reading that column will fail or silently treat balances as unknown.",
        root_cause="Column 'account_balance' renamed to 'bal' in the target schema, likely a migration applied to the wrong environment.",
        recommended_action="Revert the target schema migration or update the transformation to align with the new column name.",
        confidence=0.97,
    ),
    "null_spike": dict(
        detected_by="sql_validator",
        signals={**_clean_signals(), "null_rate_increase_pct": {"customer_id": 35.0}},
        affected_job="cx_customer_load",
        affected_objects=["target.customer_dim"],
        impact_summary="35% of loaded records have no customer_id and cannot be joined back to a customer record.",
        root_cause="NullPointerException in CustomerTransformStep broke the null-safe join on customer_id.",
        recommended_action="Fix the null-safe join in CustomerTransformStep; add a NOT NULL constraint to customer_id in the target DDL.",
        confidence=0.96,
    ),
    "latency": dict(
        detected_by="log_analyser",
        signals={**_clean_signals(), "sla_breach_projected": True},
        affected_job="cx_customer_load",
        affected_objects=["target.customer_dim"],
        impact_summary="Downstream consumers are 15,000 messages behind Kafka and falling further back; near-real-time dashboards will show stale data.",
        root_cause="Kafka consumer lag of 15,000 messages exceeds the 10,000-message threshold.",
        recommended_action="Scale out the Kafka consumer group or investigate back-pressure in the downstream streaming job.",
        confidence=0.93,
    ),
}


def _say(message: str) -> None:
    print(f"[demo_incident] {message}")


def run_lifecycle(incident_id: str, actor: str) -> None:
    incident = load(incident_id)

    set_status(incident, "acknowledged", actor=actor)
    _say(f"ACKNOWLEDGED by {actor}")

    record_approval_decision(incident, "approved", actor)
    _say(f"APPROVED by {actor} -> status={incident.status}")

    set_status(incident, "verifying", actor=actor)
    _say("VERIFYING")

    resolve_incident(incident, actor=actor)
    _say(f"RESOLVED -> MTTA={incident.mtta_seconds}s  MTTR={incident.mttr_seconds}s")
    _say(
        "(MTTA/MTTR will read ~0s here since this walked the lifecycle in "
        "milliseconds -- a real incident's figures reflect actual human "
        "response time.)"
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=sorted(SCENARIOS.keys()))
    parser.add_argument("--lifecycle", action="store_true", help="walk the incident to resolved")
    parser.add_argument("--actor", default="U_DEMO", help="Slack user ID attributed to lifecycle actions")
    parser.add_argument("--json", action="store_true", help="print the final incident as JSON")
    args = parser.parse_args(argv)

    slack_mode = os.getenv("SLACK_MODE", "stub")
    _say(f"SLACK_MODE={slack_mode} (set SLACK_MODE=live with a populated .env to post for real)")

    config = load_config()
    scenario = dict(SCENARIOS[args.mode])
    run_id = f"demo-{args.mode}-{int(time.time())}"

    start = time.monotonic()
    response = build_response(
        {"run_id": run_id, "pipeline": scenario.get("affected_job", "customer_transactions")},
        scenario,
        ALL_TOOLS,
        int((time.monotonic() - start) * 1000),
        config,
    )
    notify_slack(response)

    if response["clean"]:
        _say(f"CLEAN RUN — run_id={run_id}, no incident opened, checks_performed={response['checks_performed']}")
        if args.lifecycle:
            _say("--lifecycle has nothing to walk through on a clean run; ignoring.")
        return 0

    incident_id = response["incident"]["incident_id"]
    severity = response["incident"]["severity"]
    _say(
        f"OPENED {incident_id}  severity={severity}  "
        f"requires_approval={response['incident']['requires_approval']}  "
        f"runbook={response['incident']['runbook']}"
    )
    _say(f"slack_channel={response['incident']['slack_channel']}  slack_ts={response['incident']['slack_ts']}")

    if args.lifecycle:
        run_lifecycle(incident_id, args.actor)

    if args.json:
        print(json.dumps(load(incident_id).to_dict(), indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
