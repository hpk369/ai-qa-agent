"""
Tests for agent.agent.build_response — the pure, network-free part of the
agent loop that turns Claude's reported signals into the new response
contract (T0.4). run_agent() itself calls the live Anthropic API and is
exercised manually/in the demo, not here (see docs/INVENTORY.md).

Each existing failure mode gets a synthetic `agent_output` representing
what a compliant agent should report for that mode's tool results, so
these tests double as the "existing failure injections produce valid
incidents" acceptance check from IMPLEMENTATION.md T0.4 without requiring
a live model call.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import pytest

from agent import incident as incident_module
from agent.agent import build_response
from agent.severity import load_config


@pytest.fixture(autouse=True)
def incidents_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(incident_module, "INCIDENTS_DIR", tmp_path)
    return tmp_path


@pytest.fixture(scope="module")
def config():
    return load_config()


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


def _agent_output(**overrides) -> dict:
    base = {
        "detected_by": "none",
        "signals": _clean_signals(),
        "affected_job": "customer_transactions",
        "affected_objects": ["tgt.transactions"],
        "rows_expected": 100_000,
        "rows_loaded": 100_000,
        "impact_summary": "No issues detected.",
        "root_cause": None,
        "recommended_action": None,
        "confidence": 0.98,
    }
    base.update(overrides)
    return base


def _event(**overrides) -> dict:
    base = {
        "run_id": "run-001",
        "pipeline": "customer_transactions",
        "source_table": "src.transactions",
        "target_table": "tgt.transactions",
        "failure_mode": "none",
    }
    base.update(overrides)
    return base


ALL_TOOLS = {"sql_validator", "log_analyser", "schema_comparator"}


class TestCleanRun:
    def test_clean_run_produces_no_incident(self, config):
        response = build_response(_event(), _agent_output(), ALL_TOOLS, 1234, config)
        assert response["incident"] is None
        assert response["clean"] is True
        assert "verdict" not in response

    def test_clean_run_checks_performed_in_canonical_order(self, config):
        response = build_response(_event(), _agent_output(), ALL_TOOLS, 1234, config)
        assert response["checks_performed"] == ["recon", "logs", "schema"]

    def test_run_id_is_passed_through(self, config):
        response = build_response(_event(run_id="abc-123"), _agent_output(), ALL_TOOLS, 1, config)
        assert response["run_id"] == "abc-123"

    def test_duration_ms_passed_through(self, config):
        response = build_response(_event(), _agent_output(), ALL_TOOLS, 4321, config)
        assert response["duration_ms"] == 4321


class TestRowDropFailureMode:
    """mock_pipeline/failures.py ROW_DROP: 40% fewer target rows -> row_variance_pct 40.0."""

    def test_produces_p2_incident(self, config):
        agent_output = _agent_output(
            detected_by="sql_validator",
            signals={**_clean_signals(), "row_variance_pct": 40.0},
            rows_expected=100_000,
            rows_loaded=60_000,
            impact_summary="Target table is missing roughly 40% of expected records.",
            root_cause="40,000 records dropped during deduplication in CustomerTransformStep.",
            recommended_action="Inspect the deduplication step for partition skew.",
            confidence=0.95,
        )
        response = build_response(_event(failure_mode="row_drop"), agent_output, ALL_TOOLS, 500, config)

        assert response["clean"] is False
        assert response["incident"]["severity"] == "P2"
        assert response["incident"]["requires_approval"] is True


class TestSchemaDriftFailureMode:
    """SCHEMA_DRIFT: account_balance renamed away -> load cannot write a required
    column, i.e. no path to complete before SLA."""

    def test_produces_p1_incident(self, config):
        agent_output = _agent_output(
            detected_by="schema_comparator",
            signals={**_clean_signals(), "job_failed_no_path_to_sla": True},
            impact_summary="account_balance is missing from the target load entirely.",
            root_cause="Column 'account_balance' renamed to 'bal' in the target schema.",
            recommended_action="Revert the target schema migration.",
            confidence=0.97,
        )
        response = build_response(_event(failure_mode="schema_drift"), agent_output, ALL_TOOLS, 500, config)

        assert response["incident"]["severity"] == "P1"
        assert response["incident"]["requires_approval"] is True


class TestNullSpikeFailureMode:
    """NULL_SPIKE: customer_id null rate rises to 35% on a critical column."""

    def test_produces_p3_incident(self, config):
        agent_output = _agent_output(
            detected_by="sql_validator",
            signals={**_clean_signals(), "null_rate_increase_pct": {"customer_id": 35.0}},
            impact_summary="35% of loaded records have no customer_id and cannot be joined to a customer.",
            root_cause="NullPointerException in CustomerTransformStep broke the join key.",
            recommended_action="Fix the null-safe join in CustomerTransformStep.",
            confidence=0.96,
        )
        response = build_response(_event(failure_mode="null_spike"), agent_output, ALL_TOOLS, 500, config)

        assert response["incident"]["severity"] == "P3"
        assert response["incident"]["requires_approval"] is False


class TestLatencyFailureMode:
    """LATENCY: Kafka consumer lag of 15,000 exceeds the 10,000 threshold."""

    def test_produces_p2_incident(self, config):
        agent_output = _agent_output(
            detected_by="log_analyser",
            signals={**_clean_signals(), "sla_breach_projected": True},
            impact_summary="Downstream consumers are 15,000 messages behind and falling further back.",
            root_cause="Kafka consumer lag of 15,000 exceeds the 10,000 message threshold.",
            recommended_action="Scale out the consumer group.",
            confidence=0.93,
        )
        response = build_response(_event(failure_mode="latency"), agent_output, ALL_TOOLS, 500, config)

        assert response["incident"]["severity"] == "P2"
        assert response["incident"]["requires_approval"] is True


class TestChecksPerformed:
    def test_reflects_only_called_tools(self, config):
        response = build_response(
            _event(), _agent_output(), {"log_analyser", "sql_validator"}, 1, config
        )
        assert response["checks_performed"] == ["recon", "logs"]

    def test_empty_when_no_tools_called(self, config):
        response = build_response(_event(), _agent_output(), set(), 1, config)
        assert response["checks_performed"] == []


class TestIncidentPersisted:
    def test_incident_is_persisted_to_disk(self, config, incidents_dir):
        agent_output = _agent_output(
            detected_by="sql_validator",
            signals={**_clean_signals(), "row_variance_pct": 40.0},
        )
        response = build_response(_event(), agent_output, ALL_TOOLS, 1, config)
        incident_id = response["incident"]["incident_id"]
        assert (incidents_dir / f"{incident_id}.json").exists()
