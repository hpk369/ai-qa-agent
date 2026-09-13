"""Tests for agent.runbooks.select_runbook — deterministic runbook selection."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from agent.runbooks import (
    RB_CONSUMER_LAG,
    RB_JOB_FAILURE,
    RB_NULL_SPIKE,
    RB_ROW_SHORTFALL,
    RB_SCHEMA_DRIFT,
    select_runbook,
)


def _clean_signals() -> dict:
    return {
        "target_unavailable": False, "control_total_mismatch": False,
        "job_failed_no_path_to_sla": False, "row_variance_pct": 0.0,
        "sla_breach_projected": False, "downstream_jobs_blocked": 0,
        "null_rate_increase_pct": {}, "job_duration_vs_baseline_pct": 100.0,
        "log_anomaly_no_data_impact": False,
    }


class TestPerFailureMode:
    def test_row_drop_selects_row_shortfall(self):
        signals = {**_clean_signals(), "row_variance_pct": 40.0}
        assert select_runbook(signals) == RB_ROW_SHORTFALL

    def test_schema_drift_selects_schema_drift(self):
        signals = {**_clean_signals(), "job_failed_no_path_to_sla": True}
        assert select_runbook(signals) == RB_SCHEMA_DRIFT

    def test_null_spike_selects_null_spike(self):
        signals = {**_clean_signals(), "null_rate_increase_pct": {"customer_id": 35.0}}
        assert select_runbook(signals) == RB_NULL_SPIKE

    def test_latency_selects_consumer_lag(self):
        signals = {**_clean_signals(), "sla_breach_projected": True}
        assert select_runbook(signals) == RB_CONSUMER_LAG


class TestCleanRun:
    def test_no_signals_selects_no_runbook(self):
        assert select_runbook(_clean_signals()) is None


class TestFallbackToJobFailure:
    def test_target_unavailable_selects_job_failure(self):
        signals = {**_clean_signals(), "target_unavailable": True}
        assert select_runbook(signals) == RB_JOB_FAILURE

    def test_control_total_mismatch_selects_job_failure(self):
        signals = {**_clean_signals(), "control_total_mismatch": True}
        assert select_runbook(signals) == RB_JOB_FAILURE

    def test_downstream_jobs_blocked_selects_job_failure(self):
        signals = {**_clean_signals(), "downstream_jobs_blocked": 2}
        assert select_runbook(signals) == RB_JOB_FAILURE

    def test_long_duration_selects_job_failure(self):
        signals = {**_clean_signals(), "job_duration_vs_baseline_pct": 250.0}
        assert select_runbook(signals) == RB_JOB_FAILURE


class TestPrecedence:
    def test_schema_drift_signal_wins_over_row_variance(self):
        # A schema-drift-shaped failure could incidentally show some row
        # variance too -- the more specific signal must win.
        signals = {**_clean_signals(), "job_failed_no_path_to_sla": True, "row_variance_pct": 12.0}
        assert select_runbook(signals) == RB_SCHEMA_DRIFT
