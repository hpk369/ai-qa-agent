import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import pytest
from mock_pipeline.failures import FailureMode
from agent_tools.sql_validator import SQLValidator


class TestSQLValidatorCleanRun:
    def test_status_is_pass(self, db_conn):
        result = SQLValidator(db_conn).validate("src_transactions", "tgt_transactions")
        assert result["status"] == "PASS"

    def test_row_drop_under_threshold(self, db_conn):
        result = SQLValidator(db_conn).validate("src_transactions", "tgt_transactions")
        assert result["row_drop_pct"] < 5.0

    def test_no_issues_reported(self, db_conn):
        result = SQLValidator(db_conn).validate("src_transactions", "tgt_transactions")
        assert result["issues"] == []

    def test_counts_match(self, db_conn):
        result = SQLValidator(db_conn).validate("src_transactions", "tgt_transactions")
        assert result["source_count"] == result["target_count"]


class TestSQLValidatorRowDrop:
    def test_status_is_fail(self, db_conn_with_row_drop):
        result = SQLValidator(db_conn_with_row_drop).validate("src_transactions", "tgt_transactions")
        assert result["status"] == "FAIL"

    def test_row_drop_detected(self, db_conn_with_row_drop):
        result = SQLValidator(db_conn_with_row_drop).validate("src_transactions", "tgt_transactions")
        assert result["row_drop_pct"] > 5.0

    def test_issue_message_contains_row_drop(self, db_conn_with_row_drop):
        result = SQLValidator(db_conn_with_row_drop).validate("src_transactions", "tgt_transactions")
        assert any("Row drop" in issue for issue in result["issues"])

    def test_approximately_40_pct_drop(self, db_conn_with_row_drop):
        result = SQLValidator(db_conn_with_row_drop).validate("src_transactions", "tgt_transactions")
        assert 35.0 < result["row_drop_pct"] < 45.0


class TestSQLValidatorNullSpike:
    def test_null_spike_detected(self, db_conn_with_nulls):
        result = SQLValidator(db_conn_with_nulls).validate("src_transactions", "tgt_transactions")
        assert result["null_rates"]["customer_id"] > 0.30

    def test_status_is_fail(self, db_conn_with_nulls):
        result = SQLValidator(db_conn_with_nulls).validate("src_transactions", "tgt_transactions")
        assert result["status"] == "FAIL"


class TestSQLValidatorDuplicates:
    def test_duplicate_detection(self, db_conn_with_dupes):
        result = SQLValidator(db_conn_with_dupes).validate("src_transactions", "tgt_transactions")
        assert result["duplicate_count"] > 0

    def test_status_is_fail_with_dupes(self, db_conn_with_dupes):
        result = SQLValidator(db_conn_with_dupes).validate("src_transactions", "tgt_transactions")
        assert result["status"] == "FAIL"


class TestSQLValidatorMockMode:
    """Tests using mock pipeline data (no DB connection)."""

    def test_mock_clean_passes(self):
        result = SQLValidator(failure_mode=FailureMode.NONE).validate(
            "src.transactions", "tgt.transactions"
        )
        assert result["status"] == "PASS"

    def test_mock_row_drop_fails(self):
        result = SQLValidator(failure_mode=FailureMode.ROW_DROP).validate(
            "src.transactions", "tgt.transactions"
        )
        assert result["status"] == "FAIL"
        assert result["row_drop_pct"] > 5.0

    def test_mock_null_spike_fails(self):
        result = SQLValidator(failure_mode=FailureMode.NULL_SPIKE).validate(
            "src.transactions", "tgt.transactions"
        )
        assert result["status"] == "FAIL"
        assert result["null_rates"]["customer_id"] > 0.30
