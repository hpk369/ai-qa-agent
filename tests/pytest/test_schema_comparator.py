import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import pytest
from mock_pipeline.failures import FailureMode
from agent_tools.schema_comparator import SchemaComparator


class TestSchemaComparatorClean:
    def test_status_is_pass(self, clean_schema):
        result = SchemaComparator(db_conn=clean_schema).compare(
            "src_transactions", "tgt_transactions"
        )
        assert result["status"] == "PASS"

    def test_no_removed_columns(self, clean_schema):
        result = SchemaComparator(db_conn=clean_schema).compare(
            "src_transactions", "tgt_transactions"
        )
        assert result["columns_removed"] == []

    def test_no_renamed_columns(self, clean_schema):
        result = SchemaComparator(db_conn=clean_schema).compare(
            "src_transactions", "tgt_transactions"
        )
        assert result["columns_renamed"] == []

    def test_no_type_changes(self, clean_schema):
        result = SchemaComparator(db_conn=clean_schema).compare(
            "src_transactions", "tgt_transactions"
        )
        assert result["type_changes"] == []


class TestSchemaComparatorRename:
    def test_renamed_column_detected(self, drifted_schema):
        result = SchemaComparator(db_conn=drifted_schema).compare(
            "src_transactions", "tgt_transactions"
        )
        assert len(result["columns_renamed"]) > 0

    def test_rename_identifies_correct_columns(self, drifted_schema):
        result = SchemaComparator(db_conn=drifted_schema).compare(
            "src_transactions", "tgt_transactions"
        )
        rename = result["columns_renamed"][0]
        assert rename["from"] == "account_balance"
        assert rename["to"] == "bal"

    def test_status_is_fail_on_rename(self, drifted_schema):
        result = SchemaComparator(db_conn=drifted_schema).compare(
            "src_transactions", "tgt_transactions"
        )
        assert result["status"] == "FAIL"


class TestSchemaComparatorRemoval:
    def test_removed_column_detected(self, schema_with_removal):
        result = SchemaComparator(db_conn=schema_with_removal).compare(
            "src_transactions", "tgt_transactions"
        )
        assert len(result["columns_removed"]) > 0

    def test_status_is_fail_on_removal(self, schema_with_removal):
        result = SchemaComparator(db_conn=schema_with_removal).compare(
            "src_transactions", "tgt_transactions"
        )
        assert result["status"] == "FAIL"

    def test_correct_column_identified(self, schema_with_removal):
        result = SchemaComparator(db_conn=schema_with_removal).compare(
            "src_transactions", "tgt_transactions"
        )
        assert "account_balance" in result["columns_removed"]


class TestSchemaComparatorMockMode:
    def test_mock_clean_passes(self):
        result = SchemaComparator(failure_mode=FailureMode.NONE).compare(
            "src.transactions", "tgt.transactions"
        )
        assert result["status"] == "PASS"

    def test_mock_schema_drift_fails(self):
        result = SchemaComparator(failure_mode=FailureMode.SCHEMA_DRIFT).compare(
            "src.transactions", "tgt.transactions"
        )
        assert result["status"] == "FAIL"
        assert len(result["columns_renamed"]) > 0

    def test_mock_schema_drift_correct_rename(self):
        result = SchemaComparator(failure_mode=FailureMode.SCHEMA_DRIFT).compare(
            "src.transactions", "tgt.transactions"
        )
        rename = result["columns_renamed"][0]
        assert rename["from"] == "account_balance"
        assert rename["to"] == "bal"
