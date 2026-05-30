"""
Integration-style tests that exercise all three tools together
and verify the combined output matches expected failure-mode behaviour.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from mock_pipeline.failures import FailureMode
from agent_tools.log_analyser import LogAnalyser
from agent_tools.schema_comparator import SchemaComparator
from agent_tools.sql_validator import SQLValidator


def _run_all_tools(failure_mode: FailureMode, log_path: str = "/mock/path.log") -> dict:
    sql = SQLValidator(failure_mode=failure_mode).validate("src.transactions", "tgt.transactions")
    log = LogAnalyser(failure_mode=failure_mode).analyse(log_path)
    schema = SchemaComparator(failure_mode=failure_mode).compare("src.transactions", "tgt.transactions")
    return {"sql": sql, "log": log, "schema": schema}


class TestAllToolsCleanRun:
    def test_all_pass_on_none(self):
        results = _run_all_tools(FailureMode.NONE)
        assert results["sql"]["status"] == "PASS"
        assert results["log"]["status"] == "PASS"
        assert results["schema"]["status"] == "PASS"

    def test_no_issues_on_none(self):
        results = _run_all_tools(FailureMode.NONE)
        assert results["sql"]["issues"] == []
        assert results["log"]["issues"] == []
        assert results["schema"]["issues"] == []


class TestAllToolsRowDrop:
    def test_sql_detects_row_drop(self):
        results = _run_all_tools(FailureMode.ROW_DROP)
        assert results["sql"]["status"] == "FAIL"

    def test_only_sql_fails_on_row_drop(self):
        results = _run_all_tools(FailureMode.ROW_DROP)
        assert results["schema"]["status"] == "PASS"


class TestAllToolsSchemaDrift:
    def test_schema_detects_drift(self):
        results = _run_all_tools(FailureMode.SCHEMA_DRIFT)
        assert results["schema"]["status"] == "FAIL"

    def test_sql_unaffected_by_schema_drift(self):
        results = _run_all_tools(FailureMode.SCHEMA_DRIFT)
        # Row counts are not affected by schema drift
        assert results["sql"]["row_drop_pct"] < 5.0


class TestAllToolsNullSpike:
    def test_sql_detects_null_spike(self):
        results = _run_all_tools(FailureMode.NULL_SPIKE)
        assert results["sql"]["status"] == "FAIL"

    def test_log_detects_null_errors(self):
        results = _run_all_tools(FailureMode.NULL_SPIKE)
        assert results["log"]["status"] == "FAIL"


class TestAllToolsLatency:
    def test_log_detects_kafka_lag(self):
        results = _run_all_tools(FailureMode.LATENCY)
        assert results["log"]["status"] == "FAIL"
        assert results["log"]["kafka_lag"] > 10_000

    def test_sql_unaffected_by_latency(self):
        results = _run_all_tools(FailureMode.LATENCY)
        assert results["sql"]["status"] == "PASS"
