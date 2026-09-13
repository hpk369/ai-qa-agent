"""
Schema Comparator — detects schema drift between source and target tables.
Accepts a real psycopg2 connection, a Hive/pyhive connection (T2.5), or
falls back to mock data.
"""

from __future__ import annotations

import os
import sys
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mock_pipeline"))

from failures import FailureMode, get_failure_mode, get_source_data, get_target_data

try:
    from pyhive import hive as _pyhive_hive  # optional — hadoop-profile-only
except ImportError:  # pragma: no cover - exercised via db_kind="hive" instead
    _pyhive_hive = None


class SchemaComparator:
    def __init__(self, db_conn=None, failure_mode: FailureMode | None = None, db_kind: str | None = None):
        """
        db_kind explicitly names the connection type ("sqlite" | "postgres"
        | "hive") instead of relying purely on isinstance detection —
        useful both because pyhive is an optional dependency this module
        shouldn't require just to import, and because it makes the Hive
        code path testable with a plain fake connection object, no real
        pyhive install needed. Leave it None (the default) to keep the
        original auto-detect behaviour for sqlite/postgres callers
        unchanged.
        """
        self.db_conn = db_conn
        self.failure_mode = failure_mode or get_failure_mode()
        self.db_kind = db_kind

    def _resolved_db_kind(self) -> str:
        if self.db_kind:
            return self.db_kind
        import sqlite3 as _sqlite3

        if isinstance(self.db_conn, _sqlite3.Connection):
            return "sqlite"
        if _pyhive_hive is not None and isinstance(self.db_conn, _pyhive_hive.Connection):
            return "hive"
        return "postgres"

    def _get_schema(self, table: str) -> dict[str, str]:
        """Return {column_name: data_type} for a table."""
        if self.db_conn is not None:
            cur = self.db_conn.cursor()
            kind = self._resolved_db_kind()

            if kind == "sqlite":
                # SQLite: use PRAGMA — table name has no schema prefix
                bare = table.split(".")[-1]
                cur.execute(f"PRAGMA table_info({bare})")
                return {row[1]: row[2] for row in cur.fetchall()}

            if kind == "hive":
                # Hive has no queryable information_schema in the general
                # case (unlike Postgres) — DESCRIBE is the portable way to
                # get a table's columns across Hive versions.
                cur.execute(f"DESCRIBE {table}")
                schema: dict[str, str] = {}
                for row in cur.fetchall():
                    col_name, col_type = row[0], row[1]
                    # A partitioned table's DESCRIBE output has a blank
                    # row then a "# Partition Information" section after
                    # the real columns — stop there rather than parsing
                    # partition columns as regular ones or a comment as
                    # a column name.
                    if not col_name or col_name.strip().startswith("#"):
                        break
                    schema[col_name.strip()] = col_type.strip()
                return schema

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
