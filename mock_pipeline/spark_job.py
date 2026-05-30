"""
Mock Spark transformation job.
Reads from source, applies transformations, writes to target.
Failure injection is controlled via INJECT_FAILURE env var.
"""

import os
import sys

# Allow running from repo root or mock_pipeline dir
sys.path.insert(0, os.path.dirname(__file__))

from failures import FailureMode, get_failure_mode, get_log_data, get_source_data, get_target_data


def run_job(run_id: str) -> dict:
    failure_mode = get_failure_mode()
    print(f"[SparkJob] run_id={run_id} failure_mode={failure_mode.value}")

    source = get_source_data(failure_mode)
    target = get_target_data(failure_mode)
    logs = get_log_data(failure_mode)

    for err in logs["errors"]:
        print(f"[SparkJob] {err}")
    for warn in logs["warnings"][:3]:  # truncate noisy warnings
        print(f"[SparkJob] {warn}")

    result = {
        "run_id": run_id,
        "failure_mode": failure_mode.value,
        "source_row_count": source["row_count"],
        "target_row_count": target["row_count"],
        "error_count": logs["error_count"],
        "warn_count": logs["warn_count"],
        "status": "FAIL" if failure_mode != FailureMode.NONE else "PASS",
    }
    print(f"[SparkJob] Completed: {result}")
    return result


if __name__ == "__main__":
    import uuid
    run_job(str(uuid.uuid4()))
