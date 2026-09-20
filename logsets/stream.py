"""
A live log stream with backpressure.

``build_session`` produces one finished log set. This module produces a
*running* one: log lines arrive continuously, at a rate, the way they do
off a real cluster. The agent tails what arrives, and the moment a
failure signature shows up it alerts — not at the end of a batch, at the
line.

The part that makes it more than a demo of speed is the **gate**. A
blocking failure (by default anything the classifier calls P1 or P2) is
something a real pipeline cannot simply carry on through: the target is
unavailable, the job aborted, the control totals disagree. So the stream
stops advancing and drops to a trickle of backpressure lines — retries,
growing queue depth, DagRuns waiting on an upstream incident — exactly
what a stalled pipeline actually writes. It stays there until a human
confirms in Slack that the problem is fixed, and only then does it
resume and emit recovery lines.

"A human confirms in Slack" is a judgement call, not a keyword match:
"restarted the consumer, lag is draining" releases the gate, "looking
into it" must not. agent/llm.py asks Claude to make exactly that call,
and the gate stays shut on anything ambiguous — resuming a broken
pipeline is far worse than waiting another minute. A ✅ reaction and a
recorded resolution on the incident itself also release it, so the gate
never depends solely on a model being reachable.

Severity, runbook selection and the approval gate stay where they were:
deterministic, in agent/severity.py and agent/runbooks.py.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from agent import llm
from agent.incident import Incident, load, resolve_incident
from agent.severity import load_config
from agent.slack_blocks import build_thread_reply
from agent.slack_client import SlackClient
from logsets.catalog import (
    INJECTABLE_SOURCES,
    Signature,
    Source,
    advance,
    format_line,
    signatures_for_family,
    source_by_name,
)
from logsets.corpus import GENERATED, background_lines
from logsets.session import DEFAULT_ROOT, LogSet, bundle, write_manifest
from logsets.triage import (
    _merge_signals,
    alert_slack,
    build_incident,
    logset_summary,
    narrate_incident,
    scan_lines,
)

RESUMED = "resumed"
STILL_BLOCKED = "still_blocked"


# ---------- Configuration ----------

@dataclass
class StreamConfig:
    """How the stream behaves. Every default is chosen so that a demo run
    shows the whole loop — steady state, alert, block, release — inside a
    couple of minutes."""

    rate: float = 6.0                      # background lines per second, steady state
    duration: float | None = 180.0         # seconds of streaming (None = until stopped)
    first_incident_after: float = 8.0      # seconds of clean logs before the first failure
    incident_gap: tuple[float, float] = (15.0, 45.0)   # seconds between failures
    blocking_severities: frozenset[str] = frozenset({"P1", "P2"})
    blocked_rate_factor: float = 0.12      # a stalled pipeline still logs, just slowly
    poll_interval: float = 3.0             # how often to check Slack while blocked
    resolution_confidence: float = 0.6     # below this, the gate stays shut
    auto_resolve_after: float | None = None  # unattended demos only; None = wait for a human
    max_block_wait: float = 600.0          # stop rather than wait forever
    recovery_lines: int = 4                # lines emitted on release, before normal service


@dataclass
class StreamEvent:
    """Something worth printing. The CLI renders these; tests assert on them."""

    kind: str
    at: float
    detail: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


class Clock:
    """Real time, injectable so tests can run a stream in milliseconds."""

    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


# ---------- Backpressure and recovery lines ----------

_BACKPRESSURE = {
    "spark": [("WARN", "Stage 7 (writeParquet) blocked on upstream dependency; retry 3 of 8 in 60s"),
              ("WARN", "Scheduler has 4 tasks pending with no executor able to make progress")],
    "hadoop": [("WARN", "Application attempt is unhealthy; deferring container allocation"),
               ("WARN", "RMContainerAllocator holding 6 pending map requests")],
    "hdfs": [("WARN", "Write pipeline paused; 1 datanode not accepting blocks"),
             ("WARN", "PendingReplicationBlocks backlog growing: 812 blocks")],
    "kafka": [("WARN", "Consumer group etl-ingest paused by operator; partition backlog growing"),
              ("WARN", "Fetch throttled: no committed offsets advancing for 60s")],
    "hive": [("WARN", "Query queued behind an open incident on tgt.transactions"),
             ("WARN", "Metastore lock held on tgt.transactions; waiting")],
    "airflow": [("WARN", "DagRun customer_transactions is held: upstream incident unresolved"),
                ("WARN", "Task load_target deferred; waiting on operator confirmation")],
    "zookeeper": [("WARN", "Outstanding requests queue depth 240 and rising")],
}

_RECOVERY = {
    "spark": [("INFO", "Stage 7 (writeParquet) resumed after upstream recovery"),
              ("INFO", "Committed partition 000042 to hdfs://namenode-01:8020/warehouse/tgt/transactions")],
    "hadoop": [("INFO", "Container allocation resumed; 6 pending requests satisfied"),
               ("INFO", "Job progress 0.82 and advancing")],
    "hdfs": [("INFO", "Write pipeline restored; all datanodes accepting blocks"),
             ("INFO", "PendingReplicationBlocks drained to 0")],
    "kafka": [("INFO", "Consumer group etl-ingest resumed; lag draining"),
              ("INFO", "Committed offset advancing on all 6 partitions")],
    "hive": [("INFO", "Metastore lock released on tgt.transactions"),
             ("INFO", "Queued query admitted and completed in 2.1s")],
    "airflow": [("INFO", "DagRun customer_transactions released by operator confirmation"),
                ("INFO", "Marking task as SUCCESS. dag_id=customer_transactions, task_id=load_target")],
    "zookeeper": [("INFO", "Outstanding requests queue drained")],
}

_DEFAULT_BACKPRESSURE = [("WARN", "pipeline paused pending incident resolution")]
_DEFAULT_RECOVERY = [("INFO", "pipeline resumed")]


# ---------- The stream ----------

class StreamRunner:
    """One streaming session. Writes into the same directory layout a
    built log set uses, so a stream is listable, re-readable and
    downloadable through exactly the same commands and endpoints."""

    def __init__(
        self,
        config: StreamConfig | None = None,
        sources: list[str] | None = None,
        seed: int | None = None,
        root: Path | None = None,
        corpus_dir: Path | None = None,
        real_background: bool = False,
        notify: bool = True,
        session_id: str | None = None,
        slack_client: SlackClient | None = None,
        on_event: Callable[[StreamEvent], None] | None = None,
        clock: Clock | None = None,
    ):
        self.config = config or StreamConfig()
        self.seed = seed if seed is not None else random.SystemRandom().randrange(2**31)
        self.rng = random.Random(self.seed)
        self.clock = clock or Clock()
        self.on_event = on_event or (lambda event: None)
        self.notify = notify
        self.slack_client = slack_client
        self.real_background = real_background
        self.corpus_dir = corpus_dir
        self.severity_config = load_config()

        created = datetime.now(timezone.utc)
        self.session_id = session_id or (
            f"ST-{created.strftime('%Y%m%d-%H%M%S')}-{self.seed % 0x10000:04x}")
        self.directory = (root or DEFAULT_ROOT) / self.session_id
        self.directory.mkdir(parents=True, exist_ok=True)

        chosen = ([source_by_name(name) for name in sources] if sources
                  else self.rng.sample(list(INJECTABLE_SOURCES),
                                       min(3, len(INJECTABLE_SOURCES))))
        self.sources: list[Source] = chosen
        self.logset = LogSet(
            session_id=self.session_id,
            seed=self.seed,
            created_at=created.isoformat(timespec="seconds"),
            directory=self.directory,
        )

        self._pools: dict[str, list[str]] = {}
        self._pool_cursor: dict[str, int] = {}
        self._line_counts: dict[str, int] = {}
        self._read_offsets: dict[str, int] = {}
        self._providers: dict[str, str] = {}
        self._log_time = created
        self.incidents: list[dict[str, Any]] = []
        self.started_at: float = 0.0

        for source in self.sources:
            lines, provider = background_lines(
                source, self.rng, 400, created,
                self.corpus_dir if self.real_background else Path("/nonexistent"))
            self._pools[source.name] = lines
            self._pool_cursor[source.name] = 0
            self._line_counts[source.name] = 0
            self._read_offsets[source.name] = 0
            self._providers[source.name] = provider if self.real_background else GENERATED
            (self.directory / f"{source.name}.log").write_text("", encoding="utf-8")

    # ---------- emitting ----------

    def _emit(self, source: Source, lines: list[str]) -> None:
        with (self.directory / f"{source.name}.log").open("a", encoding="utf-8") as handle:
            for line in lines:
                handle.write(line + "\n")
        self._line_counts[source.name] += len(lines)

    def _background_line(self, source: Source) -> str:
        pool = self._pools[source.name]
        cursor = self._pool_cursor[source.name]
        self._pool_cursor[source.name] = (cursor + 1) % max(1, len(pool))
        self._log_time = advance(self._log_time, self.rng)
        if self._providers[source.name] == GENERATED:
            return pool[cursor] if pool else format_line(
                source.formatter, "INFO", "heartbeat", self._log_time, self.rng)
        return pool[cursor]

    def _emit_pool(self, source: Source, pool: dict[str, list], default: list) -> None:
        level, message = self.rng.choice(pool.get(source.family, default))
        self._log_time = advance(self._log_time, self.rng)
        self._emit(source, [format_line(source.formatter, level, message,
                                        self._log_time, self.rng)])

    def _inject(self) -> tuple[Source, Signature] | None:
        """Write one failure into one source's log, as it would appear."""
        pairs = [(source, signature) for source in self.sources
                 for signature in signatures_for_family(source.family)]
        if not pairs:
            return None
        source, signature = self.rng.choice(pairs)
        rendered = []
        for level, message in signature.render(self.rng):
            self._log_time = advance(self._log_time, self.rng)
            rendered.append(format_line(source.formatter, level, message,
                                        self._log_time, self.rng))
        self._emit(source, rendered)
        self.logset.injected.append({
            "signature_id": signature.id,
            "title": signature.title,
            "file": f"{source.name}.log",
            "source": source.name,
            "line": self._line_counts[source.name] - len(rendered) + 1,
            "lines_written": len(rendered),
        })
        return source, signature

    # ---------- tailing ----------

    def _read_new(self) -> dict[str, Any]:
        """Scan only what has arrived since the last check — the same
        window a tailing agent would see, so a second failure is never
        judged on the first one's lines."""
        per_file, findings, unmatched = [], [], []
        signals: dict[str, Any] = {}
        error_count = warn_count = lines_scanned = 0
        context: list[str] = []

        for source in self.sources:
            name = f"{source.name}.log"
            path = self.directory / name
            if not path.exists():
                continue
            all_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            start = self._read_offsets[source.name]
            window = all_lines[start:]
            self._read_offsets[source.name] = len(all_lines)
            if not window:
                continue

            result = scan_lines(window, name, first_line_number=start + 1)
            result["source"] = source.name
            result["provider"] = self._providers[source.name]
            result["line_count"] = len(window)
            per_file.append(result)
            findings.extend(result["findings"])
            unmatched.extend(result["unmatched_errors"])
            error_count += result["error_count"]
            warn_count += result["warn_count"]
            lines_scanned += len(window)
            context.extend(window[-12:])

        for finding in findings:
            _merge_signals(signals, finding["signals"])
        if unmatched:
            signals["log_anomaly_no_data_impact"] = True

        return {
            "session_id": self.session_id,
            "files": per_file,
            "findings": findings,
            "unmatched_errors": unmatched,
            "signals": signals,
            "error_count": error_count,
            "warn_count": warn_count,
            "lines_scanned": lines_scanned,
            "context": context,
        }

    def _sync_manifest(self) -> None:
        self.logset.files = [
            {
                "file": f"{source.name}.log",
                "source": source.name,
                "family": source.family,
                "description": source.description,
                "provider": self._providers[source.name],
                "line_count": self._line_counts[source.name],
                "injected": [item for item in self.logset.injected
                             if item["source"] == source.name],
            }
            for source in self.sources
        ]
        write_manifest(self.logset)

    # ---------- alerting ----------

    def _open_incident(self, window: dict[str, Any]) -> Incident | None:
        self._sync_manifest()
        archive = bundle(self.logset, force=True)
        incident = build_incident(self.logset, window, archive, self.severity_config)
        if incident is None:
            return None

        narrated_by = narrate_incident(incident, window, context_lines=window["context"],
                                       source_label=f"live stream {self.session_id}")

        from agent.incident import persist

        persist(incident)
        self.incidents.append({
            "incident_id": incident.incident_id,
            "severity": incident.severity,
            "at": round(self.clock.now() - self.started_at, 2),
            "narrated_by": narrated_by,
            "signatures": [f["signature_id"] for f in window["findings"]],
        })

        if self.notify:
            alert_slack(incident, self.logset, window, archive, client=self.slack_client)

        self._event("incident", f"{incident.incident_id} {incident.severity}", {
            "incident_id": incident.incident_id,
            "severity": incident.severity,
            "impact_summary": incident.impact_summary,
            "narrated_by": narrated_by,
            "runbook": incident.runbook,
            "signatures": [f["signature_id"] for f in window["findings"]],
        })
        return incident

    # ---------- the gate ----------

    def _seen_replies(self, incident: Incident) -> list[dict[str, str]]:
        client = self.slack_client or SlackClient()
        return [message for message in client.get_thread_replies(incident)
                if message.get("text") and not message.get("bot_id")]

    def _reaction_confirms(self, incident: Incident) -> bool:
        client = self.slack_client or SlackClient()
        confirming = {"white_check_mark", "heavy_check_mark", "ballot_box_with_check"}
        return any(reaction.get("name") in confirming
                   for reaction in client.get_reactions(incident))

    def _check_release(self, incident: Incident, already_judged: set[str]) -> tuple[bool, str]:
        """Has a human confirmed this is fixed? Three ways in, and the
        model is only one of them."""
        try:
            current = load(incident.incident_id)
            if current.status == "resolved":
                return True, "the incident record was marked resolved"
        except FileNotFoundError:
            pass

        if self._reaction_confirms(incident):
            return True, "a ✅ reaction on the incident message"

        replies = self._seen_replies(incident)
        fresh = [reply for reply in replies if reply.get("ts") not in already_judged]
        if not fresh:
            return False, ""

        for reply in fresh:
            already_judged.add(reply.get("ts", ""))
            self._event("reply", f"{reply.get('user', 'someone')}: {reply.get('text', '')}",
                        {"incident_id": incident.incident_id, "user": reply.get("user"),
                         "text": reply.get("text")})

        summary = (f"{incident.incident_id} ({incident.severity}) — "
                   f"{incident.impact_summary}\nRoot cause: {incident.root_cause}")
        judgement = llm.judge_resolution(summary, replies)
        verdict = judgement.value
        self._event("judgement",
                    f"{'resolved' if verdict.resolved else 'not resolved'} "
                    f"({verdict.confidence:.2f}, {judgement.source}): {verdict.reason}",
                    {"incident_id": incident.incident_id, "resolved": verdict.resolved,
                     "confidence": verdict.confidence, "reason": verdict.reason,
                     "judged_by": judgement.source})

        if verdict.resolved and verdict.confidence >= self.config.resolution_confidence:
            return True, f"a human confirmed it in Slack: {verdict.reason}"
        return False, ""

    def _wait_for_resolution(self, incident: Incident) -> str:
        """Hold the pipeline. Emit backpressure, poll for confirmation."""
        self._event("blocked", f"{incident.incident_id} is blocking the stream", {
            "incident_id": incident.incident_id,
            "severity": incident.severity,
            "reply_with": f"python scripts/slack_reply.py {incident.incident_id} "
                          f'"<what you did to fix it>"',
        })

        blocked_at = self.clock.now()
        next_poll = blocked_at
        judged: set[str] = set()
        interval = 1.0 / max(self.config.rate * self.config.blocked_rate_factor, 0.01)

        while True:
            elapsed = self.clock.now() - blocked_at

            if self.config.auto_resolve_after is not None and elapsed >= self.config.auto_resolve_after:
                self._release(incident, "auto-resolved for an unattended run", "auto-resolver")
                return RESUMED

            if self.clock.now() >= next_poll:
                next_poll = self.clock.now() + self.config.poll_interval
                released, why = self._check_release(incident, judged)
                if released:
                    self._release(incident, why, "U_ONCALL")
                    return RESUMED

            if elapsed >= self.config.max_block_wait:
                self._event("still_blocked",
                            f"{incident.incident_id} unresolved after "
                            f"{self.config.max_block_wait:.0f}s — stopping the stream rather "
                            f"than resuming on a broken pipeline",
                            {"incident_id": incident.incident_id})
                return STILL_BLOCKED

            source = self.rng.choice(self.sources)
            self._emit_pool(source, _BACKPRESSURE, _DEFAULT_BACKPRESSURE)
            self.clock.sleep(interval)

    def _release(self, incident: Incident, why: str, actor: str) -> None:
        try:
            current = load(incident.incident_id)
            if current.status != "resolved":
                resolve_incident(current, actor=actor,
                                 slack_client=self.slack_client or SlackClient())
        except Exception as exc:  # noqa: BLE001 - the stream resuming must not hinge on Slack
            self._event("warning", f"could not mark {incident.incident_id} resolved: {exc}")

        self._event("resumed", why, {"incident_id": incident.incident_id})

        if self.notify:
            try:
                client = self.slack_client or SlackClient()
                blocks, text = build_thread_reply(
                    f"▶️ Stream `{self.session_id}` resumed — {why}. "
                    f"Backpressure released; pipeline is moving again.")
                client.reply_thread(incident, blocks, text)
            except Exception as exc:  # noqa: BLE001
                self._event("warning", f"could not post the resume note: {exc}")

        for _ in range(self.config.recovery_lines):
            self._emit_pool(self.rng.choice(self.sources), _RECOVERY, _DEFAULT_RECOVERY)
        # Recovery lines are ours, not the pipeline's failure — don't let the
        # next window re-read them as new evidence.
        self._read_new()

    # ---------- the loop ----------

    def _event(self, kind: str, detail: str = "", payload: dict[str, Any] | None = None) -> None:
        self.on_event(StreamEvent(kind=kind, at=round(self.clock.now() - self.started_at, 2),
                                  detail=detail, payload=payload or {}))

    def run(self) -> dict[str, Any]:
        """Stream until the duration runs out, or until a blocking
        incident outlasts max_block_wait."""
        self.started_at = self.clock.now()
        interval = 1.0 / max(self.config.rate, 0.01)
        next_incident = self.started_at + self.config.first_incident_after
        outcome = "completed"

        self._event("started", f"{self.session_id} — {len(self.sources)} source(s) at "
                               f"{self.config.rate:g} lines/s", {
            "session_id": self.session_id, "seed": self.seed,
            "sources": [s.name for s in self.sources],
            "llm": llm.describe(),
        })

        while True:
            now = self.clock.now()
            if self.config.duration is not None and now - self.started_at >= self.config.duration:
                break

            if now >= next_incident:
                injected = self._inject()
                window = self._read_new()
                incident = self._open_incident(window) if injected else None

                if incident and incident.severity in self.config.blocking_severities:
                    if self._wait_for_resolution(incident) == STILL_BLOCKED:
                        outcome = "blocked"
                        break
                elif incident:
                    self._event("continuing",
                                f"{incident.severity} is not blocking — the stream keeps moving",
                                {"incident_id": incident.incident_id})

                low, high = self.config.incident_gap
                next_incident = self.clock.now() + self.rng.uniform(low, high)

            source = self.rng.choice(self.sources)
            self._emit(source, [self._background_line(source)])
            self.clock.sleep(interval)

        self._read_new()
        self._sync_manifest()
        archive = bundle(self.logset, force=True)
        window = {"files": [{**entry, "error_count": 0, "warn_count": 0}
                            for entry in self.logset.files],
                  "findings": [], "unmatched_errors": [], "error_count": 0,
                  "warn_count": 0, "lines_scanned": sum(self._line_counts.values())}
        summary = logset_summary(self.logset, window, archive)

        self._event("finished", f"{outcome} — {sum(self._line_counts.values())} lines, "
                                f"{len(self.incidents)} incident(s)",
                    {"outcome": outcome, "archive": str(archive)})

        return {
            "session_id": self.session_id,
            "seed": self.seed,
            "outcome": outcome,
            "lines_emitted": sum(self._line_counts.values()),
            "incidents": self.incidents,
            "logset": summary,
            "download": {"archive": str(archive)},
        }
