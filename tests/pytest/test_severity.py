"""
Tests for agent.severity — deterministic severity classification driven by
config/severity.yml. One case per severity, boundary cases on every numeric
threshold, a clean-run case, a multi-match case, and an unknown-column case.
"""

import copy
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import pytest

from agent.severity import classify, load_config


@pytest.fixture(scope="module")
def config():
    return load_config()


class TestP1Critical:
    def test_target_unavailable(self, config):
        result = classify({"target_unavailable": True}, config)
        assert result.severity == "P1"
        assert "target_unavailable" in result.matched_conditions

    def test_control_total_mismatch_any_amount(self, config):
        # Any monetary variance at all is P1 — there is no threshold to tune.
        result = classify({"control_total_mismatch": True}, config)
        assert result.severity == "P1"

    def test_job_failed_no_path_to_sla(self, config):
        result = classify({"job_failed_no_path_to_sla": True}, config)
        assert result.severity == "P1"


class TestP2High:
    def test_row_variance_at_threshold(self, config):
        result = classify({"row_variance_pct": 5.0}, config)
        assert result.severity == "P2"

    def test_sla_breach_projected(self, config):
        result = classify({"sla_breach_projected": True}, config)
        assert result.severity == "P2"

    def test_downstream_jobs_blocked(self, config):
        result = classify({"downstream_jobs_blocked": 2}, config)
        assert result.severity == "P2"
        assert result.matched_conditions


class TestP3Moderate:
    def test_row_variance_in_band(self, config):
        result = classify({"row_variance_pct": 2.5}, config)
        assert result.severity == "P3"

    def test_null_rate_increase_on_critical_column(self, config):
        result = classify(
            {"null_rate_increase_pct": {"customer_id": 12.0}}, config
        )
        assert result.severity == "P3"

    def test_job_duration_over_baseline(self, config):
        result = classify({"job_duration_vs_baseline_pct": 250.0}, config)
        assert result.severity == "P3"


class TestP4Low:
    def test_null_rate_increase_on_non_critical_column(self, config):
        result = classify(
            {"null_rate_increase_pct": {"region_code": 15.0}}, config
        )
        assert result.severity == "P4"

    def test_log_anomaly_no_data_impact(self, config):
        result = classify({"log_anomaly_no_data_impact": True}, config)
        assert result.severity == "P4"


class TestCleanRun:
    def test_no_signals_returns_none(self, config):
        result = classify({}, config)
        assert result.severity is None
        assert result.matched_conditions == []

    def test_signals_below_every_threshold_returns_none(self, config):
        result = classify({"row_variance_pct": 0.2}, config)
        assert result.severity is None


class TestBoundaries:
    def test_row_variance_just_under_p2_is_p3(self, config):
        result = classify({"row_variance_pct": 4.99}, config)
        assert result.severity == "P3"

    def test_row_variance_at_p2_threshold_is_p2(self, config):
        result = classify({"row_variance_pct": 5.0}, config)
        assert result.severity == "P2"

    def test_row_variance_just_under_p3_floor_is_clean(self, config):
        result = classify({"row_variance_pct": 0.99}, config)
        assert result.severity is None

    def test_row_variance_at_p3_floor_is_p3(self, config):
        result = classify({"row_variance_pct": 1.0}, config)
        assert result.severity == "P3"

    def test_null_rate_increase_just_under_threshold_no_match(self, config):
        result = classify(
            {"null_rate_increase_pct": {"customer_id": 9.99}}, config
        )
        assert result.severity is None

    def test_null_rate_increase_at_threshold_matches(self, config):
        result = classify(
            {"null_rate_increase_pct": {"customer_id": 10.0}}, config
        )
        assert result.severity == "P3"


class TestMultiMatchMostSevereWins:
    def test_p1_and_p3_signals_together_return_p1(self, config):
        result = classify(
            {"target_unavailable": True, "row_variance_pct": 2.5}, config
        )
        assert result.severity == "P1"

    def test_p2_and_p4_signals_together_return_p2(self, config):
        result = classify(
            {
                "row_variance_pct": 7.0,
                "log_anomaly_no_data_impact": True,
            },
            config,
        )
        assert result.severity == "P2"

    def test_all_conditions_matched_within_a_severity_are_reported(self, config):
        result = classify(
            {"row_variance_pct": 6.0, "sla_breach_projected": True}, config
        )
        assert result.severity == "P2"
        assert len(result.matched_conditions) == 2


class TestUnknownColumnDefaultsNonCritical:
    def test_unknown_column_does_not_crash(self, config):
        result = classify(
            {"null_rate_increase_pct": {"totally_unknown_column": 12.0}},
            config,
        )
        assert result.severity == "P4"

    def test_unknown_column_not_treated_as_critical(self, config):
        result = classify(
            {"null_rate_increase_pct": {"totally_unknown_column": 12.0}},
            config,
        )
        assert result.severity != "P3"


class TestRationaleAndResponseExpectation:
    def test_rationale_names_matched_condition(self, config):
        result = classify({"target_unavailable": True}, config)
        assert "target_unavailable" in result.rationale

    def test_response_expectation_present_for_incident(self, config):
        result = classify({"target_unavailable": True}, config)
        assert result.response_expectation == config["severities"]["P1"]["response"]

    def test_response_expectation_empty_on_clean_run(self, config):
        result = classify({}, config)
        assert result.response_expectation == ""


class TestConfigDriven:
    def test_changing_threshold_changes_classification_with_no_code_edit(self, config):
        # 3.0% would normally be P3 (>=1.0, <5.0). Lower the P2 floor to 3.0
        # in a copy of the config and confirm the same signal now hits P2 —
        # proving the module has no hard-coded thresholds.
        mutated = copy.deepcopy(config)
        mutated["severities"]["P2"]["conditions"][0]["row_variance_pct"]["gte"] = 3.0

        before = classify({"row_variance_pct": 3.0}, config)
        after = classify({"row_variance_pct": 3.0}, mutated)

        assert before.severity == "P3"
        assert after.severity == "P2"
