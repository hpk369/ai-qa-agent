"""
Log Analyser — parses Spark/Kafka logs for errors, warnings, and consumer lag.
Accepts a real log file path or falls back to mock data for testing.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mock_pipeline"))

from failures import FailureMode, get_failure_mode, get_log_data, get_target_data

KAFKA_LAG_THRESHOLD = int(os.getenv("KAFKA_LAG_THRESHOLD", "10000"))

_ERROR_PATTERN = re.compile(r"\bERROR\b", re.IGNORECASE)
_WARN_PATTERN = re.compile(r"\bWARN\b", re.IGNORECASE)
_LAG_PATTERN = re.compile(r"lag[^\d]*(\d+)", re.IGNORECASE)
_ALL_NUMBERS_AFTER_LAG = re.compile(r"lag(?:[^\d]*)(\d+)", re.IGNORECASE)


def _parse_log_file(log_path: str) -> dict[str, Any]:
    errors = []
    warnings = []
    kafka_lag = 0

    try:
        with open(log_path) as f:
            for line in f:
                line = line.rstrip()
                if _ERROR_PATTERN.search(line):
                    errors.append(line)
                elif _WARN_PATTERN.search(line):
                    warnings.append(line)
                lag_numbers = [int(m) for m in re.findall(r"(?:lag\s*(?:exceeded|:)?\s*)(\d+)", line, re.IGNORECASE)]
                if lag_numbers:
                    kafka_lag = max(kafka_lag, max(lag_numbers))
    except FileNotFoundError:
        errors.append(f"Log file not found: {log_path}")

    return {
        "errors": errors,
        "warnings": warnings,
        "error_count": len(errors),
        "warn_count": len(warnings),
        "kafka_lag": kafka_lag,
    }


class LogAnalyser:
    def __init__(self, failure_mode: FailureMode | None = None):
        self.failure_mode = failure_mode or get_failure_mode()

    def analyse(self, log_path: str, run_id: str = "") -> dict[str, Any]:
        if not log_path:
            # Empty path → intentional mock mode
            log_data = get_log_data(self.failure_mode)
            target_data = get_target_data(self.failure_mode)
            parsed = {
                "errors": log_data["errors"],
                "warnings": log_data["warnings"],
                "error_count": log_data["error_count"],
                "warn_count": log_data["warn_count"],
                "kafka_lag": target_data.get("kafka_lag", 0),
            }
        elif log_path.startswith("/mock/"):
            # Explicit mock path prefix → use mock data
            log_data = get_log_data(self.failure_mode)
            target_data = get_target_data(self.failure_mode)
            parsed = {
                "errors": log_data["errors"],
                "warnings": log_data["warnings"],
                "error_count": log_data["error_count"],
                "warn_count": log_data["warn_count"],
                "kafka_lag": target_data.get("kafka_lag", 0),
            }
        else:
            # Real path — parse it (reports error if file missing)
            parsed = _parse_log_file(log_path)

        issues = []
        if parsed["error_count"] > 0:
            issues.extend(parsed["errors"])
        if parsed["kafka_lag"] > KAFKA_LAG_THRESHOLD:
            issues.append(f"Kafka consumer lag: {parsed['kafka_lag']} messages")

        return {
            "error_count": parsed["error_count"],
            "warn_count": parsed["warn_count"],
            "errors": parsed["errors"],
            "kafka_lag": parsed["kafka_lag"],
            "status": "FAIL" if issues else "PASS",
            "issues": issues,
        }
