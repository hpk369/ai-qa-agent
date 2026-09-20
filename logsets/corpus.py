"""
Background log lines — the bulk of every session's set.

Two providers, in this order:

1. **Real public logs.** ``scripts/fetch_logs.py`` downloads the LogHub
   sample logs (https://github.com/logpai/loghub) into ``logsets/corpus/``,
   which is gitignored: these are third-party research datasets, so the
   repo fetches them rather than vendoring them. Once fetched, a session's
   background is a real contiguous window of a real production log —
   Spark, YARN, HDFS, ZooKeeper, OpenStack, syslog, sshd, Apache, and
   several supercomputer RAS logs.

2. **Generated lines.** Sources nobody publishes (Kafka, Hive, Airflow),
   and every source when the corpus has not been fetched, get generated
   background in the same line format. This is what keeps the repo
   runnable with no network at all — a clone with no corpus still
   produces a full log set, it just says so in the manifest.

Which provider each source used is recorded per file in the session
manifest, so a downloaded bundle never leaves you guessing whether a line
came from a real cluster or from here.
"""

from __future__ import annotations

import json
import random
import urllib.request
from datetime import datetime
from pathlib import Path

from logsets.catalog import SOURCES, Source, advance, format_line

CORPUS_DIR = Path(__file__).resolve().parent / "corpus"
PROVENANCE_FILE = CORPUS_DIR / "PROVENANCE.json"

LOGHUB_BASE = "https://raw.githubusercontent.com/logpai/loghub/master"
LOGHUB_ATTRIBUTION = (
    "Background lines come from the LogHub log collection "
    "(https://github.com/logpai/loghub), sampled 2k-line excerpts of real "
    "system logs. They are used here as realistic background only; the ETL "
    "error signatures this agent triages are generated and injected by "
    "logsets/session.py."
)

REAL = "real"
GENERATED = "generated"


# ---------- Fetching ----------

def fetch_corpus(force: bool = False, timeout: int = 60,
                 dest: Path | None = None) -> dict[str, str]:
    """Download every source's public corpus file. Returns
    {source_name: "downloaded" | "cached" | "unavailable: <reason>"}."""
    dest = dest or CORPUS_DIR
    dest.mkdir(parents=True, exist_ok=True)
    results: dict[str, str] = {}
    fetched: dict[str, str] = {}

    for source in SOURCES:
        if not source.corpus_path:
            results[source.name] = "no public corpus — generated background"
            continue

        target = dest / source.corpus_path
        url = f"{LOGHUB_BASE}/{source.corpus_path}"
        if target.exists() and not force:
            results[source.name] = "cached"
            fetched[source.name] = url
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                payload = response.read()
            target.write_bytes(payload)
            results[source.name] = f"downloaded ({len(payload):,} bytes)"
            fetched[source.name] = url
        except Exception as exc:  # noqa: BLE001 - a missing corpus degrades, never fails
            results[source.name] = f"unavailable: {exc}"

    PROVENANCE_FILE.write_text(json.dumps({
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "attribution": LOGHUB_ATTRIBUTION,
        "sources": fetched,
    }, indent=2) + "\n")
    return results


def corpus_path(source: Source, dest: Path | None = None) -> Path | None:
    """Absolute path to this source's fetched corpus file, or None."""
    if not source.corpus_path:
        return None
    path = (dest or CORPUS_DIR) / source.corpus_path
    return path if path.exists() else None


def corpus_status(dest: Path | None = None) -> dict[str, bool]:
    """{source_name: True if real lines are available locally}."""
    return {source.name: corpus_path(source, dest) is not None for source in SOURCES}


# ---------- Generated background ----------

_BACKGROUND = {
    "spark": [
        ("INFO", "Starting task 42.0 in stage 7.0 (TID 1842, etl-worker-03, executor 4)"),
        ("INFO", "Finished task 41.0 in stage 7.0 (TID 1841) in 4821 ms on etl-worker-02"),
        ("INFO", "Block broadcast_18_piece0 stored as bytes in memory (estimated size 8.1 KB)"),
        ("INFO", "Reading 118 files, totalling 2.4 GB from hdfs://namenode-01:8020/warehouse/stg/transactions"),
        ("INFO", "Code generated in 184.2 ms"),
        ("WARN", "Truncated the string representation of a plan since it was too large"),
        ("INFO", "Committed partition 000041 to hdfs://namenode-01:8020/warehouse/tgt/transactions"),
        ("INFO", "Registered executor NettyRpcEndpointRef(spark-client://Executor) with ID 4"),
    ],
    "hadoop": [
        ("INFO", "Assigned container container_1445144423722_0020_01_000007 to attempt_m_000006_0"),
        ("INFO", "Progress of TaskAttempt attempt_1445144423722_0020_m_000003_0 is : 0.71"),
        ("INFO", "Received completed container container_1445144423722_0020_01_000004"),
        ("INFO", "After Scheduling: PendingReds:0 ScheduledMaps:4 ScheduledReds:0 AssignedMaps:6"),
        ("WARN", "Event Writer setup for JobId: job_1445144423722_0020 took 41 ms"),
        ("INFO", "Job jar is not present. Not adding any jar to the list of resources."),
    ],
    "hdfs": [
        ("INFO", "Receiving block blk_-1608999687919862906 src: /10.251.31.5:55886 dest: /10.251.31.5:50010"),
        ("INFO", "PacketResponder 1 for block blk_38865049064139660 terminating"),
        ("INFO", "BLOCK* NameSystem.allocateBlock: /warehouse/stg/transactions/part-00041"),
        ("INFO", "Verification succeeded for blk_-4980916519894289629"),
        ("WARN", "Slow BlockReceiver write packet to mirror took 412ms"),
    ],
    "kafka": [
        ("INFO", "[GroupCoordinator 2]: Stabilized group etl-ingest generation 84"),
        ("INFO", "Partition transactions-7 on broker 2: Expanding ISR from 2,3 to 2,3,1"),
        ("INFO", "Deleting segment 0 from log transactions-3 due to retention time 604800000ms breach"),
        ("INFO", "[ReplicaFetcher replicaId=2, leaderId=1, fetcherId=0] Truncating to offset 184122"),
        ("WARN", "Attempting to send response via channel for which there is no open connection"),
        ("INFO", "[GroupCoordinator 2]: Preparing to rebalance group settlement-consumer"),
    ],
    "hive": [
        ("INFO", "get_table : db=stg tbl=transactions"),
        ("INFO", "Completed compiling command queryId=hive_20260920110213_8f2a; Time taken: 0.412 seconds"),
        ("INFO", "Partition stg.transactions{dt=2026-09-20} stats: [numFiles=118, numRows=1204881]"),
        ("INFO", "Opened a connection to metastore, current connections: 7"),
        ("WARN", "Shutting down the object store; 1 connection still open"),
    ],
    "airflow": [
        ("INFO", "Dependencies all met for <TaskInstance: customer_transactions.load_target scheduled__2026-09-20T09:00:00+00:00 [queued]>"),
        ("INFO", "Executing <Task(SparkSubmitOperator): load_target> on 2026-09-20T09:00:00+00:00"),
        ("INFO", "Marking task as SUCCESS. dag_id=customer_transactions, task_id=reconcile_counts"),
        ("INFO", "DagRun Finished: dag_id=customer_transactions, run_id=scheduled__2026-09-20T08:00:00+00:00, state=success"),
        ("WARN", "Failing back to the default pool; pool 'etl_heavy' has no slots free"),
    ],
    "zookeeper": [
        ("INFO", "Accepted socket connection from /10.10.34.12:44712"),
        ("INFO", "Established session 0x14ed9d0f0c10002 with negotiated timeout 40000 for client /10.10.34.12:44712"),
        ("INFO", "Processed session termination for sessionid: 0x14ed9d0f0c10002"),
        ("WARN", "fsync-ing the write ahead log in SyncThread:1 took 1834ms"),
    ],
}

_BACKGROUND_DEFAULT = [
    ("INFO", "session opened for user etl_svc by (uid=0)"),
    ("INFO", "cron[18422]: (etl_svc) CMD (/opt/etl/bin/heartbeat.sh)"),
    ("INFO", "kernel: EXT4-fs (sdb1): mounted filesystem with ordered data mode"),
    ("WARN", "systemd: Starting Cleanup of Temporary Directories..."),
]


def _generated_lines(source: Source, rng: random.Random, count: int,
                     start: datetime) -> list[str]:
    pool = _BACKGROUND.get(source.family, _BACKGROUND_DEFAULT)
    lines = []
    when = start
    for _ in range(count):
        level, message = rng.choice(pool)
        lines.append(format_line(source.formatter, level, message, when, rng))
        when = advance(when, rng)
    return lines


def _real_lines(path: Path, rng: random.Random, count: int) -> list[str]:
    """A contiguous window of a real log, so the background still reads
    like one machine doing one thing rather than shuffled confetti."""
    all_lines = [line.rstrip("\n") for line in path.read_text(
        encoding="utf-8", errors="replace").splitlines() if line.strip()]
    if not all_lines:
        return []
    if count >= len(all_lines):
        return all_lines
    start = rng.randrange(0, len(all_lines) - count)
    return all_lines[start:start + count]


def background_lines(source: Source, rng: random.Random, count: int,
                     start: datetime, dest: Path | None = None) -> tuple[list[str], str]:
    """Return (lines, provider) for one source, provider being
    ``real`` or ``generated``."""
    path = corpus_path(source, dest)
    if path:
        lines = _real_lines(path, rng, count)
        if lines:
            return lines, REAL
    return _generated_lines(source, rng, count, start), GENERATED
