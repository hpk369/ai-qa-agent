"""
Deterministic runbook selection. Like severity, which runbook an incident
links to is decided by code reading the reported signals, not by the
model choosing a path string — the same evidence must always point at the
same runbook.

Checked in order; the first matching signal wins. Order matters where
signals could co-occur (e.g. a schema-drift-shaped failure could
incidentally show some row variance too — job_failed_no_path_to_sla is
checked first because it's the more specific, more severe signal).
"""

from __future__ import annotations

from typing import Any

RUNBOOK_DIR = "docs/runbooks"

RB_ROW_SHORTFALL = f"{RUNBOOK_DIR}/RB-001-row-shortfall.md"
RB_SCHEMA_DRIFT = f"{RUNBOOK_DIR}/RB-002-schema-drift.md"
RB_NULL_SPIKE = f"{RUNBOOK_DIR}/RB-003-null-spike.md"
RB_CONSUMER_LAG = f"{RUNBOOK_DIR}/RB-004-consumer-lag.md"
RB_JOB_FAILURE = f"{RUNBOOK_DIR}/RB-005-job-failure.md"


def select_runbook(signals: dict[str, Any]) -> str | None:
    """Return the runbook path for the first matching condition in
    `signals`, or None if nothing in the current runbook set covers it
    (e.g. a clean run, or a condition none of the five current runbooks
    address yet)."""
    if signals.get("job_failed_no_path_to_sla"):
        return RB_SCHEMA_DRIFT

    if signals.get("target_unavailable") or signals.get("control_total_mismatch"):
        return RB_JOB_FAILURE

    if signals.get("null_rate_increase_pct"):
        return RB_NULL_SPIKE

    if signals.get("sla_breach_projected"):
        return RB_CONSUMER_LAG

    row_variance = signals.get("row_variance_pct") or 0
    if row_variance > 0:
        return RB_ROW_SHORTFALL

    if signals.get("downstream_jobs_blocked") or signals.get("job_duration_vs_baseline_pct", 0) >= 200.0:
        return RB_JOB_FAILURE

    return None
