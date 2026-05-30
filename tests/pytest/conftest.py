"""
pytest fixtures providing clean, row-drop, null-spike, schema-drift, and duplicate database states.
Uses SQLite in-memory for speed (no Postgres required for unit tests).
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import pytest

from mock_pipeline.failures import FailureMode


# ---------- SQLite connection helpers ----------

def _make_sqlite_conn(source_rows: list[tuple], target_rows: list[tuple],
                      source_cols: list[str] | None = None,
                      target_cols: list[str] | None = None):
    """
    Build an in-memory SQLite connection with src_transactions and tgt_transactions tables.
    Rows: list of (transaction_id, customer_id, account_balance, amount, transaction_date, status)
    """
    if source_cols is None:
        source_cols = ["transaction_id", "customer_id", "account_balance", "amount",
                       "transaction_date", "status"]
    if target_cols is None:
        target_cols = source_cols

    conn = sqlite3.connect(":memory:")
    cur = conn.cursor()

    src_col_defs = ", ".join(f"{c} TEXT" for c in source_cols)
    tgt_col_defs = ", ".join(f"{c} TEXT" for c in target_cols)

    cur.execute(f"CREATE TABLE src_transactions ({src_col_defs})")
    cur.execute(f"CREATE TABLE tgt_transactions ({tgt_col_defs})")

    if source_rows:
        placeholders = ", ".join("?" * len(source_cols))
        cur.executemany(f"INSERT INTO src_transactions VALUES ({placeholders})", source_rows)

    if target_rows:
        placeholders = ", ".join("?" * len(target_cols))
        cur.executemany(f"INSERT INTO tgt_transactions VALUES ({placeholders})", target_rows)

    conn.commit()
    return conn


def _base_rows(n: int, null_customer: bool = False) -> list[tuple]:
    rows = []
    for i in range(n):
        customer_id = None if null_customer and i < int(n * 0.35) else f"CUST_{i:06d}"
        rows.append((
            f"TXN_{i:06d}",
            customer_id,
            f"{100.0 + i:.2f}",
            f"{10.0 + (i % 50):.2f}",
            "2026-01-01T00:00:00",
            "SETTLED",
        ))
    return rows


# ---------- Fixtures for SQLValidator ----------

@pytest.fixture
def db_conn():
    """Clean run — source and target identical, 1000 rows each."""
    rows = _base_rows(1000)
    conn = _make_sqlite_conn(rows, rows)
    yield conn
    conn.close()


@pytest.fixture
def db_conn_with_row_drop():
    """Target has 40% fewer rows than source."""
    source_rows = _base_rows(1000)
    target_rows = source_rows[:600]  # 40% dropped
    conn = _make_sqlite_conn(source_rows, target_rows)
    yield conn
    conn.close()


@pytest.fixture
def db_conn_with_nulls():
    """Target has 35% null customer_id values."""
    source_rows = _base_rows(1000)
    target_rows = _base_rows(1000, null_customer=True)
    conn = _make_sqlite_conn(source_rows, target_rows)
    yield conn
    conn.close()


@pytest.fixture
def db_conn_with_dupes():
    """Target has duplicate transaction_ids."""
    rows = _base_rows(100)
    target_rows = rows + rows[:20]  # 20 duplicates
    conn = _make_sqlite_conn(rows, target_rows)
    yield conn
    conn.close()


# ---------- Fixtures for SchemaComparator ----------

@pytest.fixture
def clean_schema():
    """Source and target share identical schemas."""
    rows = _base_rows(10)
    conn = _make_sqlite_conn(rows, rows)
    yield conn
    conn.close()


@pytest.fixture
def drifted_schema():
    """Target has account_balance renamed to bal."""
    source_cols = ["transaction_id", "customer_id", "account_balance", "amount",
                   "transaction_date", "status"]
    target_cols = ["transaction_id", "customer_id", "bal", "amount",
                   "transaction_date", "status"]
    source_rows = [(f"TXN_{i}", f"C{i}", f"{i}", f"{i}", "2026-01-01", "OK") for i in range(5)]
    target_rows = source_rows  # same data, different column names
    conn = _make_sqlite_conn(source_rows, target_rows, source_cols, target_cols)
    yield conn
    conn.close()


@pytest.fixture
def schema_with_removal():
    """Target is missing the account_balance column entirely."""
    source_cols = ["transaction_id", "customer_id", "account_balance", "amount",
                   "transaction_date", "status"]
    target_cols = ["transaction_id", "customer_id", "amount", "transaction_date", "status"]
    source_rows = [(f"TXN_{i}", f"C{i}", f"{i}", f"{i}", "2026-01-01", "OK") for i in range(5)]
    target_rows = [(f"TXN_{i}", f"C{i}", f"{i}", "2026-01-01", "OK") for i in range(5)]
    conn = _make_sqlite_conn(source_rows, target_rows, source_cols, target_cols)
    yield conn
    conn.close()


# ---------- Fixtures for LogAnalyser ----------

@pytest.fixture
def clean_log(tmp_path):
    log = tmp_path / "spark_clean.log"
    log.write_text("INFO: Job started\nINFO: Processing complete\nINFO: 100000 rows written\n")
    return str(log)


@pytest.fixture
def error_log(tmp_path):
    log = tmp_path / "spark_error.log"
    log.write_text(
        "INFO: Job started\n"
        "ERROR: NullPointerException in CustomerTransformStep at line 84\n"
        "WARN: Null values exceeding threshold\n"
        "ERROR: Null propagation detected in customer_id join key\n"
    )
    return str(log)


@pytest.fixture
def lag_log(tmp_path):
    log = tmp_path / "spark_lag.log"
    log.write_text(
        "INFO: Kafka consumer started\n"
        "WARN: Consumer group lag growing\n"
        "ERROR: Kafka consumer lag exceeded 10000 messages — current lag: 15000\n"
    )
    return str(log)
