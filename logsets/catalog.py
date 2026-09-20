"""
What the agent reads (log sources) and what it knows how to recognise
(error signatures).

Two tables live here, and nothing else does:

* ``SOURCES`` — the log sources a session's set can be mixed from. Each
  names the public corpus file its background lines come from (see
  logsets/corpus.py) and the line format its own daemon writes, so an
  injected error line is indistinguishable in shape from the real lines
  around it.

* ``SIGNATURES`` — the error signatures the agent recognises, each with
  the regex that finds it and a function that turns the *matched text*
  into severity signals. Signals are derived from the log text alone,
  never from what the mixer knows it injected: the agent sees exactly
  what an on-call analyst tailing the file would see, and the manifest's
  ground truth is only ever used to score it afterwards.

Signal names are the ones config/severity.yml already classifies — this
module feeds agent/severity.py, it does not second-guess it.
"""

from __future__ import annotations

import os
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

# A consumer group this far behind is an SLA risk, not a blip.
KAFKA_LAG_THRESHOLD = int(os.getenv("KAFKA_LAG_THRESHOLD", "10000"))


# ---------- Log sources ----------

@dataclass(frozen=True)
class Source:
    """One log source a session's set can include.

    ``corpus_path`` is the file inside the fetched corpus whose lines are
    used as background; ``None`` means this source has no public corpus
    (nobody publishes a bank's Kafka or Airflow logs) and its background
    is generated instead. ``injectable`` marks the ETL-side sources an
    error signature can be injected into — host and infrastructure logs
    are carried as realistic noise, not as the thing under triage.
    """

    name: str
    family: str
    description: str
    corpus_path: str | None
    formatter: str
    injectable: bool = False


SOURCES: tuple[Source, ...] = (
    # ETL-side sources: triaged, and error signatures get injected here.
    Source("spark-executor", "spark", "Spark executor / driver log",
           "Spark/Spark_2k.log", "spark", injectable=True),
    Source("yarn-appmaster", "hadoop", "YARN MRAppMaster / ResourceManager log",
           "Hadoop/Hadoop_2k.log", "hadoop", injectable=True),
    Source("hdfs-datanode", "hdfs", "HDFS NameNode / DataNode log",
           "HDFS/HDFS_2k.log", "hdfs", injectable=True),
    Source("kafka-consumer", "kafka", "Kafka broker / consumer-group log",
           None, "kafka", injectable=True),
    Source("hive-metastore", "hive", "Hive metastore / HiveServer2 log",
           None, "hive", injectable=True),
    Source("airflow-scheduler", "airflow", "Airflow scheduler / task-instance log",
           None, "airflow", injectable=True),
    # Infrastructure noise: real logs from the machines the pipeline runs on.
    Source("zookeeper", "zookeeper", "ZooKeeper ensemble log",
           "Zookeeper/Zookeeper_2k.log", "zookeeper", injectable=True),
    Source("os-syslog", "linux", "Host syslog", "Linux/Linux_2k.log", "linux"),
    Source("openstack-nova", "openstack", "OpenStack Nova compute log",
           "OpenStack/OpenStack_2k.log", "openstack"),
    Source("edge-sshd", "openssh", "Edge-node sshd auth log",
           "OpenSSH/OpenSSH_2k.log", "openssh"),
    Source("edge-httpd", "apache", "Edge-node Apache error log",
           "Apache/Apache_2k.log", "apache"),
    Source("bluegene-ras", "bgl", "BlueGene/L RAS supercomputer log",
           "BGL/BGL_2k.log", "bgl"),
    Source("hpc-cluster", "hpc", "HPC cluster controller log",
           "HPC/HPC_2k.log", "hpc"),
    Source("thunderbird-cluster", "thunderbird", "Thunderbird supercomputer syslog",
           "Thunderbird/Thunderbird_2k.log", "thunderbird"),
    Source("proxifier", "proxifier", "Proxy client log",
           "Proxifier/Proxifier_2k.log", "proxifier"),
)

SOURCES_BY_NAME = {source.name: source for source in SOURCES}
INJECTABLE_SOURCES = tuple(s for s in SOURCES if s.injectable)


def source_by_name(name: str) -> Source:
    try:
        return SOURCES_BY_NAME[name]
    except KeyError:
        raise ValueError(
            f"unknown log source {name!r}; known sources: "
            f"{', '.join(sorted(SOURCES_BY_NAME))}"
        ) from None


# ---------- Line formatting ----------

_LOGGERS = {
    "spark": ["executor.Executor", "scheduler.TaskSetManager", "storage.BlockManager",
              "datasources.FileScanRDD", "sql.execution.WriteFilesExec"],
    "hadoop": ["org.apache.hadoop.mapreduce.v2.app.MRAppMaster",
               "org.apache.hadoop.mapreduce.v2.app.rm.RMContainerAllocator",
               "org.apache.hadoop.yarn.client.api.impl.ContainerManagementProtocolProxy"],
    "hdfs": ["dfs.DataNode$PacketResponder", "dfs.FSNamesystem", "dfs.DataNode$DataXceiver"],
    "kafka": ["kafka.coordinator.group.GroupCoordinator", "kafka.server.ReplicaManager",
              "kafka.consumer.ConsumerFetcherThread"],
    "hive": ["org.apache.hadoop.hive.metastore.HiveMetaStore",
             "org.apache.hive.service.cli.operation.SQLOperation"],
    "airflow": ["taskinstance.py:1150", "scheduler_job.py:1345", "dagrun.py:586"],
    "zookeeper": ["QuorumPeer", "SessionTrackerImpl", "NIOServerCnxn"],
}


def format_line(formatter: str, level: str, message: str, when: datetime,
                rng: random.Random) -> str:
    """Render one log line in the shape the named daemon actually writes."""
    logger = rng.choice(_LOGGERS.get(formatter, ["etl"]))
    millis = f"{when.microsecond // 1000:03d}"

    if formatter == "spark":
        return f"{when.strftime('%y/%m/%d %H:%M:%S')} {level} {logger}: {message}"
    if formatter == "hadoop":
        thread = rng.choice(["main", "IPC Server handler 4 on 8030", "RMCommunicator Allocator"])
        return (f"{when.strftime('%Y-%m-%d %H:%M:%S')},{millis} {level} "
                f"[{thread}] {logger}: {message}")
    if formatter == "hdfs":
        return (f"{when.strftime('%y%m%d %H%M%S')} {rng.randint(100, 999)} "
                f"{level} {logger}: {message}")
    if formatter == "kafka":
        return f"[{when.strftime('%Y-%m-%d %H:%M:%S')},{millis}] {level} {message} ({logger})"
    if formatter == "hive":
        return (f"{when.strftime('%Y-%m-%d %H:%M:%S')},{millis} {level} "
                f"[HiveServer2-Handler-Pool: Thread-{rng.randint(30, 99)}] {logger}: {message}")
    if formatter == "airflow":
        return f"[{when.strftime('%Y-%m-%d %H:%M:%S')},{millis}] {{{logger}}} {level} - {message}"
    if formatter == "zookeeper":
        return (f"{when.strftime('%Y-%m-%d %H:%M:%S')},{millis} - {level} "
                f"[main:{logger}@{rng.randint(100, 900)}] - {message}")
    # Host / infrastructure formatters only ever carry background lines.
    return f"{when.strftime('%b %d %H:%M:%S')} etl-edge-01 {level.lower()}: {message}"


# ---------- Error signatures ----------

@dataclass(frozen=True)
class Signature:
    """One recognisable failure, its regex, and how it becomes signals."""

    id: str
    title: str
    pattern: re.Pattern
    to_signals: Callable[[re.Match], dict[str, Any]]
    render: Callable[[random.Random], list[tuple[str, str]]]
    families: tuple[str, ...] = ()
    detail: Callable[[re.Match], str] = field(default=lambda m: m.group(0))


def _pct(value: float) -> float:
    return round(float(value), 2)


# Each render() returns [(level, message), ...] — a signature can emit a
# short burst (a WARN that precedes the ERROR, a stack frame after it)
# rather than a single line, because that is how these actually appear.

def _render_row_shortfall(rng: random.Random) -> list[tuple[str, str]]:
    expected = rng.choice([100_000, 250_000, 480_000, 1_200_000])
    variance = rng.choice([1.8, 3.4, 6.2, 12.5, 40.0])
    loaded = int(expected * (1 - variance / 100))
    return [
        ("WARN", "Partition skew detected in CustomerTransformStep; 3 partitions "
                 "hold 82% of input rows"),
        ("ERROR", f"Row count reconciliation failed for tgt.transactions: expected "
                  f"{expected} rows, loaded {loaded} (variance {variance:.2f}%)"),
    ]


def _render_schema_drift(rng: random.Random) -> list[tuple[str, str]]:
    column = rng.choice(["account_balance", "settlement_date", "account_number", "branch_code"])
    return [
        ("ERROR", f"Column '{column}' not found in target schema tgt.transactions"),
        ("ERROR", "org.apache.spark.sql.AnalysisException: cannot resolve column; "
                  "aborting write stage"),
    ]


def _render_null_spike(rng: random.Random) -> list[tuple[str, str]]:
    column = rng.choice(["customer_id", "account_balance", "region_code", "last_updated_source"])
    rate = rng.choice([12.4, 18.0, 35.5, 47.2])
    baseline = rng.choice([0.1, 0.4, 1.0])
    return [
        ("WARN", f"Null values exceeding threshold on join key {column}"),
        ("ERROR", f"Null rate for column {column} rose to {rate:.1f}% "
                  f"(baseline {baseline:.1f}%) after CustomerTransformStep"),
    ]


def _render_consumer_lag(rng: random.Random) -> list[tuple[str, str]]:
    lag = rng.choice([11_240, 15_342, 48_900, 220_450])
    group = rng.choice(["etl-ingest", "settlement-consumer", "cdc-replicator"])
    return [
        ("WARN", f"Consumer group {group} lag growing across 6 partitions"),
        ("ERROR", f"Consumer group {group} lag exceeded threshold: {lag} messages behind"),
    ]


def _render_container_oom(rng: random.Random) -> list[tuple[str, str]]:
    container = f"container_{rng.randint(1_400_000_000_000, 1_500_000_000_000)}_{rng.randint(1, 99):04d}_01_{rng.randint(1, 40):06d}"
    used = rng.choice([4.5, 8.7, 17.2])
    limit = int(used)
    return [
        ("ERROR", f"Container {container} is running beyond physical memory limits. "
                  f"Current usage: {used} GB of {limit} GB physical memory used; "
                  f"killing container"),
        ("ERROR", "Container killed by YARN for exceeding memory limits; job aborted"),
    ]


def _render_disk_full(rng: random.Random) -> list[tuple[str, str]]:
    path = rng.choice(["/data/1/dfs/dn", "/data/2/dfs/dn", "/var/lib/hadoop/tmp"])
    return [
        ("WARN", f"Volume {path} is at 99% capacity"),
        ("ERROR", f"java.io.IOException: No space left on device while writing to {path}"),
    ]


def _render_missing_block(rng: random.Random) -> list[tuple[str, str]]:
    blk = f"blk_-{rng.randint(1_000_000_000_000_000, 9_999_999_999_999_999)}"
    return [
        ("ERROR", f"Could not obtain block {blk} from any node: target replica is missing"),
        ("ERROR", "org.apache.hadoop.hdfs.BlockMissingException: could not read from any datanode"),
    ]


def _render_connection_refused(rng: random.Random) -> list[tuple[str, str]]:
    host = rng.choice(["namenode-01:8020", "hive-metastore:9083", "kafka-broker-02:9092"])
    return [
        ("ERROR", f"Call From etl-edge-01 to {host} failed on connection exception: "
                  f"java.net.ConnectException: Connection refused"),
    ]


def _render_job_failed(rng: random.Random) -> list[tuple[str, str]]:
    job = f"job_{rng.randint(1_400_000_000_000, 1_500_000_000_000)}_{rng.randint(1, 9999):04d}"
    return [
        ("ERROR", f"Task attempt attempt_{job[4:]}_m_000003_3 failed 4 times; failing the task"),
        ("ERROR", f"Job {job} failed as tasks failed. failedMaps:1 failedReduces:0"),
    ]


def _render_control_total(rng: random.Random) -> list[tuple[str, str]]:
    source_total = rng.choice([12_345_678.90, 98_004_221.15, 4_410_009.44])
    delta = rng.choice([5678.90, 12.05, 214_887.30])
    return [
        ("ERROR", f"Control total mismatch on settlement batch: source SUM(amount)="
                  f"{source_total:.2f} target SUM(amount)={source_total - delta:.2f} "
                  f"delta={delta:.2f}"),
    ]


def _render_slow_stage(rng: random.Random) -> list[tuple[str, str]]:
    baseline = rng.choice([600, 1200, 1800])
    ratio = rng.choice([1.4, 2.6, 4.5, 7.1])
    actual = int(baseline * ratio)
    return [
        ("WARN", f"Stage 7 (writeParquet) still running after {actual // 2}s"),
        ("ERROR", f"Stage 7 (writeParquet) finished in {actual}s against a baseline of "
                  f"{baseline}s ({ratio * 100:.0f}% of baseline)"),
    ]


def _render_downstream_blocked(rng: random.Random) -> list[tuple[str, str]]:
    blocked = rng.randint(1, 6)
    return [
        ("ERROR", f"Downstream dependency blocked: {blocked} jobs waiting on "
                  f"tgt.transactions past their scheduled start"),
    ]


def _render_session_expired(rng: random.Random) -> list[tuple[str, str]]:
    ms = rng.choice([30_012, 40_002, 61_440])
    sid = f"0x{rng.randint(0x1000000000000000, 0xffffffffffffffff):x}"
    return [
        ("WARN", f"Client session timed out, have not heard from server in {ms}ms "
                 f"for sessionid {sid}"),
        ("ERROR", f"Unable to reconnect to ZooKeeper service, session {sid} has expired"),
    ]


def _render_kerberos(rng: random.Random) -> list[tuple[str, str]]:
    principal = rng.choice(["etl_svc@BANK.LOCAL", "hive/_HOST@BANK.LOCAL"])
    return [
        ("ERROR", f"GSS initiate failed for {principal}: ticket expired, "
                  f"falling back to cached credentials"),
    ]


SIGNATURES: tuple[Signature, ...] = (
    Signature(
        id="SIG-001-ROW-SHORTFALL",
        title="Row count reconciliation shortfall",
        pattern=re.compile(r"Row count reconciliation failed.*?variance\s+([\d.]+)%", re.I),
        to_signals=lambda m: {"row_variance_pct": _pct(m.group(1))},
        render=_render_row_shortfall,
        families=("spark", "hadoop", "hive", "airflow"),
    ),
    Signature(
        id="SIG-002-SCHEMA-DRIFT",
        title="Target schema drift aborted the write",
        pattern=re.compile(r"Column '([\w.]+)' not found in target schema", re.I),
        to_signals=lambda m: {"job_failed_no_path_to_sla": True},
        render=_render_schema_drift,
        families=("spark", "hive", "hadoop"),
    ),
    Signature(
        id="SIG-003-NULL-SPIKE",
        title="Null rate spike on a loaded column",
        pattern=re.compile(
            r"Null rate for column (\w+) rose to ([\d.]+)%\s+\(baseline ([\d.]+)%\)", re.I),
        to_signals=lambda m: {
            "null_rate_increase_pct": {m.group(1): _pct(float(m.group(2)) - float(m.group(3)))}
        },
        render=_render_null_spike,
        families=("spark", "hive", "airflow"),
    ),
    Signature(
        id="SIG-004-CONSUMER-LAG",
        title="Kafka consumer group lag past threshold",
        pattern=re.compile(r"lag exceeded threshold:\s*(\d+)\s+messages behind", re.I),
        to_signals=lambda m: (
            {"sla_breach_projected": True} if int(m.group(1)) >= KAFKA_LAG_THRESHOLD
            else {"log_anomaly_no_data_impact": True}
        ),
        render=_render_consumer_lag,
        families=("kafka",),
    ),
    Signature(
        id="SIG-005-CONTAINER-OOM",
        title="Container killed for exceeding memory limits",
        pattern=re.compile(r"running beyond physical memory limits", re.I),
        to_signals=lambda m: {"job_failed_no_path_to_sla": True},
        render=_render_container_oom,
        families=("hadoop", "spark"),
    ),
    Signature(
        id="SIG-006-DISK-FULL",
        title="Disk full on a data volume",
        pattern=re.compile(r"No space left on device", re.I),
        to_signals=lambda m: {"target_unavailable": True},
        render=_render_disk_full,
        families=("hdfs", "hadoop", "spark"),
    ),
    Signature(
        id="SIG-007-MISSING-BLOCK",
        title="HDFS block missing from every replica",
        pattern=re.compile(r"Could not obtain block (blk_[-\d]+)", re.I),
        to_signals=lambda m: {"target_unavailable": True},
        render=_render_missing_block,
        families=("hdfs", "spark"),
    ),
    Signature(
        id="SIG-008-CONNECTION-REFUSED",
        title="Dependency unreachable (connection refused)",
        pattern=re.compile(r"failed on connection exception.*Connection refused", re.I),
        to_signals=lambda m: {"target_unavailable": True},
        render=_render_connection_refused,
        families=("hadoop", "hive", "kafka", "spark", "airflow"),
    ),
    Signature(
        id="SIG-009-JOB-FAILED",
        title="Job failed after repeated task failures",
        pattern=re.compile(r"Job (job_[\d_]+) failed as tasks failed", re.I),
        to_signals=lambda m: {"job_failed_no_path_to_sla": True},
        render=_render_job_failed,
        families=("hadoop", "airflow"),
    ),
    Signature(
        id="SIG-010-CONTROL-TOTAL",
        title="Control total mismatch between source and target",
        pattern=re.compile(r"Control total mismatch.*delta=([\d.]+)", re.I),
        to_signals=lambda m: {"control_total_mismatch": True},
        render=_render_control_total,
        families=("spark", "hive", "airflow"),
    ),
    Signature(
        id="SIG-011-SLOW-STAGE",
        title="Stage runtime far past baseline",
        pattern=re.compile(r"against a baseline of \d+s\s+\((\d+)% of baseline\)", re.I),
        to_signals=lambda m: {"job_duration_vs_baseline_pct": _pct(m.group(1))},
        render=_render_slow_stage,
        families=("spark", "hadoop"),
    ),
    Signature(
        id="SIG-012-DOWNSTREAM-BLOCKED",
        title="Downstream jobs blocked behind this load",
        pattern=re.compile(r"Downstream dependency blocked:\s*(\d+)\s+jobs", re.I),
        to_signals=lambda m: {"downstream_jobs_blocked": int(m.group(1))},
        render=_render_downstream_blocked,
        families=("airflow", "hive"),
    ),
    Signature(
        id="SIG-013-ZK-SESSION-EXPIRED",
        title="ZooKeeper session expired",
        pattern=re.compile(r"session (0x[0-9a-f]+) has expired", re.I),
        to_signals=lambda m: {"log_anomaly_no_data_impact": True},
        render=_render_session_expired,
        families=("zookeeper", "kafka"),
    ),
    Signature(
        id="SIG-014-KERBEROS",
        title="Kerberos ticket expired",
        pattern=re.compile(r"GSS initiate failed for ([\w/@._-]+)", re.I),
        to_signals=lambda m: {"log_anomaly_no_data_impact": True},
        render=_render_kerberos,
        families=("hadoop", "hive", "hdfs"),
    ),
)

SIGNATURES_BY_ID = {sig.id: sig for sig in SIGNATURES}


def signatures_for_family(family: str) -> list[Signature]:
    return [sig for sig in SIGNATURES if family in sig.families]


def signature_by_id(sig_id: str) -> Signature:
    try:
        return SIGNATURES_BY_ID[sig_id]
    except KeyError:
        raise ValueError(
            f"unknown signature {sig_id!r}; known signatures: "
            f"{', '.join(sorted(SIGNATURES_BY_ID))}"
        ) from None


def advance(when: datetime, rng: random.Random) -> datetime:
    """Next line's timestamp — log lines are close together but never
    perfectly evenly spaced."""
    return when + timedelta(milliseconds=rng.randint(4, 2500))
