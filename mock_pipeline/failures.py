"""
Failure injection modes for the mock pipeline.
Controlled via the INJECT_FAILURE environment variable.
"""

import os
from enum import Enum


class FailureMode(str, Enum):
    NONE = "none"
    ROW_DROP = "row_drop"
    SCHEMA_DRIFT = "schema_drift"
    NULL_SPIKE = "null_spike"
    LATENCY = "latency"


def get_failure_mode() -> FailureMode:
    raw = os.getenv("INJECT_FAILURE", "none").lower()
    try:
        return FailureMode(raw)
    except ValueError:
        return FailureMode.NONE


# Base schema for source table
BASE_SCHEMA = {
    "transaction_id": "UUID",
    "customer_id": "VARCHAR",
    "account_balance": "NUMERIC",
    "amount": "NUMERIC",
    "transaction_date": "TIMESTAMP",
    "status": "VARCHAR",
}

BASE_ROW_COUNT = 100_000


def get_source_data(failure_mode: FailureMode = None) -> dict:
    """Return simulated source table metadata."""
    if failure_mode is None:
        failure_mode = get_failure_mode()
    return {
        "schema": BASE_SCHEMA,
        "row_count": BASE_ROW_COUNT,
        "null_rates": {
            "customer_id": 0.0,
            "amount": 0.0,
            "account_balance": 0.0,
        },
    }


def get_target_data(failure_mode: FailureMode = None) -> dict:
    """Return simulated target table metadata with optional injected failures."""
    if failure_mode is None:
        failure_mode = get_failure_mode()

    schema = dict(BASE_SCHEMA)
    row_count = BASE_ROW_COUNT
    null_rates = {"customer_id": 0.0, "amount": 0.0, "account_balance": 0.0}
    kafka_lag = 0

    if failure_mode == FailureMode.ROW_DROP:
        row_count = int(BASE_ROW_COUNT * 0.60)  # 40% drop

    elif failure_mode == FailureMode.SCHEMA_DRIFT:
        del schema["account_balance"]
        schema["bal"] = "NUMERIC"
        null_rates.pop("account_balance")
        null_rates["bal"] = 0.0

    elif failure_mode == FailureMode.NULL_SPIKE:
        null_rates["customer_id"] = 0.35  # 35% nulls

    elif failure_mode == FailureMode.LATENCY:
        kafka_lag = 15_000  # messages behind

    return {
        "schema": schema,
        "row_count": row_count,
        "null_rates": null_rates,
        "kafka_lag": kafka_lag,
    }


def get_log_data(failure_mode: FailureMode = None) -> dict:
    """Return simulated Spark log content based on failure mode."""
    if failure_mode is None:
        failure_mode = get_failure_mode()

    errors = []
    warnings = []

    if failure_mode == FailureMode.ROW_DROP:
        errors.append("WARN: Partition skew detected in CustomerTransformStep")
        errors.append("ERROR: 40000 records dropped during deduplication stage")
        warnings = ["WARN: Executor memory pressure at 85%"] * 5

    elif failure_mode == FailureMode.SCHEMA_DRIFT:
        errors.append("ERROR: Column 'account_balance' not found in target schema")
        warnings = ["WARN: Schema validation skipped for 3 partitions"] * 3

    elif failure_mode == FailureMode.NULL_SPIKE:
        errors.append(
            "ERROR: NullPointerException in CustomerTransformStep at line 84"
        )
        errors.append("ERROR: Null propagation detected in customer_id join key")
        warnings = ["WARN: Null values exceeding threshold"] * 12

    elif failure_mode == FailureMode.LATENCY:
        errors.append("ERROR: Kafka consumer lag exceeded 10000 messages")
        warnings = ["WARN: Consumer group lag growing"] * 8

    return {
        "errors": errors,
        "warnings": warnings,
        "error_count": len(errors),
        "warn_count": len(warnings),
    }
