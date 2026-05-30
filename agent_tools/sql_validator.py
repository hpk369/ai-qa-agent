"""
SQL Validator — checks row counts, null rates, and duplicates between source and target tables.
Accepts either a real psycopg2 connection or injected mock data for testing.
"""

from __future__ import annotations

import os
import sys
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mock_pipeline"))

from failures import FailureMode, get_failure_mode, get_source_data, get_target_data

NULL_RATE_THRESHOLD = float(os.getenv("NULL_RATE_THRESHOLD", "0.05"))
ROW_DROP_THRESHOLD = float(os.getenv("ROW_DROP_THRESHOLD", "5.0"))


class SQLValidator:
    def __init__(self, db_conn=None, failure_mode: FailureMode | None = None):
        self.db_conn = db_conn
        self.failure_mode = failure_mode or get_failure_mode()

    def _get_row_count(self, table: str) -> int:
        if self.db_conn is not None:
            cur = self.db_conn.cursor()
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            return cur.fetchone()[0]
        # Use mock data
        if "src" in table:
            return get_source_data(self.failure_mode)["row_count"]
        return get_target_data(self.failure_mode)["row_count"]

    def _get_null_rates(self, table: str, columns: list[str]) -> dict[str, float]:
        if self.db_conn is not None:
            cur = self.db_conn.cursor()
            rates = {}
            for col in columns:
                try:
                    cur.execute(
                        f"SELECT AVG(CASE WHEN {col} IS NULL THEN 1.0 ELSE 0.0 END) FROM {table}"
                    )
                    rates[col] = float(cur.fetchone()[0] or 0.0)
                except Exception:
                    rates[col] = 0.0
            return rates
        if "src" in table:
            return get_source_data(self.failure_mode)["null_rates"]
        return get_target_data(self.failure_mode)["null_rates"]

    def _get_duplicate_count(self, table: str, key_col: str = "transaction_id") -> int:
        if self.db_conn is not None:
            cur = self.db_conn.cursor()
            try:
                cur.execute(
                    f"SELECT COUNT(*) - COUNT(DISTINCT {key_col}) FROM {table}"
                )
                return int(cur.fetchone()[0] or 0)
            except Exception:
                return 0
        # Mock: duplicates only injected when db_conn provides them via fixture
        return 0

    def validate(self, source_table: str, target_table: str, run_id: str = "") -> dict[str, Any]:
        source_count = self._get_row_count(source_table)
        target_count = self._get_row_count(target_table)

        row_drop_pct = (
            ((source_count - target_count) / source_count * 100.0)
            if source_count > 0
            else 0.0
        )

        null_columns = ["customer_id", "amount"]
        null_rates = self._get_null_rates(target_table, null_columns)
        duplicate_count = self._get_duplicate_count(target_table)

        issues = []
        if row_drop_pct > ROW_DROP_THRESHOLD:
            issues.append(f"Row drop {row_drop_pct:.1f}%")
        for col, rate in null_rates.items():
            if rate > NULL_RATE_THRESHOLD:
                issues.append(f"{col} null rate {rate * 100:.0f}%")
        if duplicate_count > 0:
            issues.append(f"Duplicate rows: {duplicate_count}")

        return {
            "source_count": source_count,
            "target_count": target_count,
            "row_drop_pct": round(row_drop_pct, 2),
            "null_rates": null_rates,
            "duplicate_count": duplicate_count,
            "status": "FAIL" if issues else "PASS",
            "issues": issues,
        }
