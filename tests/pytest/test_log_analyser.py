import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import pytest
from mock_pipeline.failures import FailureMode
from agent_tools.log_analyser import LogAnalyser


class TestLogAnalyserCleanLog:
    def test_status_is_pass(self, clean_log):
        result = LogAnalyser().analyse(clean_log)
        assert result["status"] == "PASS"

    def test_no_errors(self, clean_log):
        result = LogAnalyser().analyse(clean_log)
        assert result["error_count"] == 0

    def test_no_kafka_lag(self, clean_log):
        result = LogAnalyser().analyse(clean_log)
        assert result["kafka_lag"] == 0


class TestLogAnalyserErrorLog:
    def test_errors_detected(self, error_log):
        result = LogAnalyser().analyse(error_log)
        assert result["error_count"] >= 2

    def test_status_is_fail(self, error_log):
        result = LogAnalyser().analyse(error_log)
        assert result["status"] == "FAIL"

    def test_error_message_preserved(self, error_log):
        result = LogAnalyser().analyse(error_log)
        assert any("NullPointerException" in e for e in result["errors"])


class TestLogAnalyserLagLog:
    def test_kafka_lag_detected(self, lag_log):
        result = LogAnalyser().analyse(lag_log)
        assert result["kafka_lag"] > 10_000

    def test_status_is_fail_on_lag(self, lag_log):
        result = LogAnalyser().analyse(lag_log)
        assert result["status"] == "FAIL"


class TestLogAnalyserMissingFile:
    def test_missing_log_reports_error(self):
        result = LogAnalyser().analyse("/nonexistent/path/spark.log")
        assert result["error_count"] > 0
        assert result["status"] == "FAIL"


class TestLogAnalyserMockMode:
    def test_mock_clean_passes(self):
        result = LogAnalyser(failure_mode=FailureMode.NONE).analyse("/mock/path.log")
        assert result["status"] == "PASS"

    def test_mock_null_spike_fails(self):
        result = LogAnalyser(failure_mode=FailureMode.NULL_SPIKE).analyse("/mock/path.log")
        assert result["status"] == "FAIL"
        assert result["error_count"] > 0

    def test_mock_latency_fails(self):
        result = LogAnalyser(failure_mode=FailureMode.LATENCY).analyse("/mock/path.log")
        assert result["status"] == "FAIL"
        assert result["kafka_lag"] > 10_000
