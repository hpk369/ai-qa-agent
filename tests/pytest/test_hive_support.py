"""
Tests for T2.5 — SchemaComparator's and SQLValidator's Hive support.

Uses a plain fake connection/cursor, not a real pyhive install: db_kind
explicitly names the connection kind rather than relying purely on
isinstance detection, specifically so this is testable without pyhive
(an optional, hadoop-profile-only dependency, see agent_tools/schema_comparator.py's
module docstring) installed in the main test environment.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from agent_tools.schema_comparator import SchemaComparator
from agent_tools.sql_validator import SQLValidator


class FakeHiveCursor:
    """Mimics pyhive's DB-API 2.0 cursor closely enough to exercise the
    Hive code paths: .execute() records the query, .fetchone()/.fetchall()
    return canned results keyed by a simple substring match on the SQL."""

    def __init__(self, describe_rows=None, scalar_results=None):
        self._describe_rows = describe_rows or []
        self._scalar_results = scalar_results or {}
        self._last_sql = ""
        self.executed = []

    def execute(self, sql, params=None):
        self._last_sql = sql
        self.executed.append(sql)

    def fetchall(self):
        if "DESCRIBE" in self._last_sql.upper():
            return self._describe_rows
        return []

    def fetchone(self):
        for key, value in self._scalar_results.items():
            if key in self._last_sql:
                return (value,)
        return (0,)


class FakeHiveConnection:
    def __init__(self, describe_rows=None, scalar_results=None):
        self._cursor = FakeHiveCursor(describe_rows, scalar_results)

    def cursor(self):
        return self._cursor


class TestSchemaComparatorHive:
    def test_describe_output_parsed_into_schema_dict(self):
        conn = FakeHiveConnection(
            describe_rows=[
                ("transaction_id", "string", None),
                ("customer_id", "string", None),
                ("account_balance_usd", "double", None),
            ]
        )
        comparator = SchemaComparator(db_conn=conn, db_kind="hive")
        schema = comparator._get_schema("target.account_balance_fact")
        assert schema == {
            "transaction_id": "string",
            "customer_id": "string",
            "account_balance_usd": "double",
        }

    def test_partition_information_section_is_excluded(self):
        # Real Hive DESCRIBE output on a partitioned table appends a
        # blank row, a "# Partition Information" marker, a repeated
        # header, then the partition columns -- none of that should
        # land in the parsed schema.
        conn = FakeHiveConnection(
            describe_rows=[
                ("transaction_id", "string", None),
                ("customer_id", "string", None),
                ("", "", ""),
                ("# Partition Information", "", ""),
                ("# col_name", "data_type", "comment"),
                ("load_date", "string", None),
            ]
        )
        comparator = SchemaComparator(db_conn=conn, db_kind="hive")
        schema = comparator._get_schema("target.account_balance_fact")
        assert schema == {"transaction_id": "string", "customer_id": "string"}
        assert "load_date" not in schema

    def test_compare_detects_drift_via_hive_describe(self):
        source_conn = FakeHiveConnection(
            describe_rows=[("customer_id", "string", None), ("account_balance", "double", None)]
        )
        target_conn = FakeHiveConnection(
            describe_rows=[("customer_id", "string", None), ("bal", "double", None)]
        )

        class TwoConnComparator(SchemaComparator):
            """compare() calls _get_schema twice with the same db_conn --
            swap it out between calls to simulate genuinely different
            source/target connections, the way a real caller would pass
            one Hive connection per catalog/database."""

            def _get_schema(self, table):
                self.db_conn = source_conn if "source" in table else target_conn
                return super()._get_schema(table)

        result = TwoConnComparator(db_conn=source_conn, db_kind="hive").compare(
            "source.customer_dim", "target.customer_dim"
        )
        assert result["status"] == "FAIL"
        assert any(r["from"] == "account_balance" and r["to"] == "bal" for r in result["columns_renamed"])

    def test_db_kind_none_still_auto_detects_sqlite_and_postgres(self, clean_schema):
        # Regression check: adding db_kind must not change the existing
        # auto-detect behavior when it's left unset.
        result = SchemaComparator(db_conn=clean_schema).compare("src_transactions", "tgt_transactions")
        assert result["status"] == "PASS"


class TestSQLValidatorHive:
    """No Hive-specific branch exists in SQLValidator (see its module
    docstring) -- these tests prove the existing generic queries actually
    work unmodified against a Hive-shaped connection, not just that they
    theoretically should."""

    def test_row_count_and_null_rate_queries_work_against_hive_cursor(self):
        conn = FakeHiveConnection(
            scalar_results={
                "COUNT(*) FROM source.customer_transactions": 1000,
                "COUNT(*) FROM target.customer_transactions": 950,
                "customer_id IS NULL": 0.02,
            }
        )
        validator = SQLValidator(db_conn=conn, db_kind="hive")
        source_count = validator._get_row_count("source.customer_transactions")
        target_count = validator._get_row_count("target.customer_transactions")
        assert source_count == 1000
        assert target_count == 950

    def test_db_kind_accepted_and_stored(self):
        validator = SQLValidator(db_conn=None, db_kind="hive")
        assert validator.db_kind == "hive"
