"""
Schema Comparator — detects schema drift between source and target tables.
Accepts a real psycopg2 connection or falls back to mock data.
"""

from __future__ import annotations

import os
import sys
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mock_pipeline"))

from failures import FailureMode, get_failure_mode, get_source_data, get_target_data


class SchemaComparator:
    def __init__(self, db_conn=None, failure_mode: FailureMode | None = None):
        self.db_conn = db_conn
        self.failure_mode = failure_mode or get_failure_mode()

    def _get_schema(self, table: str) -> dict[str, str]:
        """Return {column_name: data_type} for a table."""
        if self.db_conn is not None:
            import sqlite3 as _sqlite3
            cur = self.db_conn.cursor()
            if isinstance(self.db_conn, _sqlite3.Connection):
                # SQLite: use PRAGMA — table name has no schema prefix
                bare = table.split(".")[-1]
                cur.execute(f"PRAGMA table_info({bare})")
                return {row[1]: row[2] for row in cur.fetchall()}
            else:
                # PostgreSQL (psycopg2)
                parts = table.split(".", 1)
                schema_name = parts[0] if len(parts) == 2 else "public"
                table_name = parts[1] if len(parts) == 2 else parts[0]
                cur.execute(
                    """
                    SELECT column_name, data_type
                    FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = %s
                    ORDER BY ordinal_position
                    """,
                    (schema_name, table_name),
                )
                return {row[0]: row[1] for row in cur.fetchall()}

        if "src" in table:
            return get_source_data(self.failure_mode)["schema"]
        return get_target_data(self.failure_mode)["schema"]

    def compare(self, source_table: str, target_table: str) -> dict[str, Any]:
        source_schema = self._get_schema(source_table)
        target_schema = self._get_schema(target_table)

        source_cols = set(source_schema.keys())
        target_cols = set(target_schema.keys())

        columns_added = sorted(target_cols - source_cols)
        columns_removed = sorted(source_cols - target_cols)

        # Detect renames: a remove + add with matching type is likely a rename
        columns_renamed = []
        unmatched_removed = list(columns_removed)
        unmatched_added = list(columns_added)

        for removed in list(unmatched_removed):
            removed_type = source_schema[removed]
            for added in list(unmatched_added):
                if target_schema[added] == removed_type:
                    columns_renamed.append({"from": removed, "to": added})
                    unmatched_removed.remove(removed)
                    unmatched_added.remove(added)
                    break

        # Type changes for columns present in both
        type_changes = []
        for col in source_cols & target_cols:
            if source_schema[col] != target_schema[col]:
                type_changes.append(
                    {
                        "column": col,
                        "source_type": source_schema[col],
                        "target_type": target_schema[col],
                    }
                )

        issues = []
        for rename in columns_renamed:
            issues.append(f"Column renamed: {rename['from']} → {rename['to']}")
        for col in unmatched_removed:
            issues.append(f"Column removed: {col}")
        for col in unmatched_added:
            issues.append(f"Column added: {col}")
        for tc in type_changes:
            issues.append(
                f"Type change on {tc['column']}: {tc['source_type']} → {tc['target_type']}"
            )

        return {
            "columns_added": columns_added,
            "columns_removed": columns_removed,
            "columns_renamed": columns_renamed,
            "type_changes": type_changes,
            "status": "FAIL" if issues else "PASS",
            "issues": issues,
        }
