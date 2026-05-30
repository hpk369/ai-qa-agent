"""
Kafka producer — emits pipeline run events.
Run with: INJECT_FAILURE=schema_drift python mock_pipeline/producer.py
"""

import json
import os
import uuid
from datetime import datetime, timezone

from failures import get_failure_mode

try:
    from kafka import KafkaProducer
    KAFKA_AVAILABLE = True
except ImportError:
    KAFKA_AVAILABLE = False


def build_event(run_id: str) -> dict:
    return {
        "run_id": run_id,
        "pipeline": "customer_transactions",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source_table": "src.transactions",
        "target_table": "tgt.transactions",
        "log_path": f"/logs/spark_run_{run_id}.log",
        "failure_mode": get_failure_mode().value,
    }


def emit_to_kafka(event: dict) -> None:
    bootstrap = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    topic = os.getenv("KAFKA_TOPIC", "pipeline-events")

    producer = KafkaProducer(
        bootstrap_servers=bootstrap,
        value_serializer=lambda v: json.dumps(v).encode("utf-8"),
    )
    future = producer.send(topic, value=event)
    record_metadata = future.get(timeout=10)
    producer.flush()
    print(
        f"Event sent to {record_metadata.topic}[{record_metadata.partition}]"
        f" offset={record_metadata.offset}"
    )


def emit_to_stdout(event: dict) -> None:
    print(json.dumps(event, indent=2))


def main() -> None:
    run_id = str(uuid.uuid4())
    event = build_event(run_id)
    print(f"Emitting pipeline event for run_id={run_id} "
          f"failure_mode={event['failure_mode']}")

    if KAFKA_AVAILABLE and os.getenv("KAFKA_BOOTSTRAP_SERVERS"):
        emit_to_kafka(event)
    else:
        print("Kafka not available — printing event to stdout:")
        emit_to_stdout(event)


if __name__ == "__main__":
    main()
