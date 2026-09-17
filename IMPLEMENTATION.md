# IMPLEMENTATION.md — Execution spec for Claude Code

**Repository:** `hpk369/ai-qa-agent`
**Companion document:** `expansion-plan.md` (strategy and rationale — read it, but this file is the source of truth for what to build)
**Objective:** convert an AI QA automation demo into an ETL production support triage system running on a Hadoop stack, with Slack as the incident channel.

---

## 0. How to use this document

Work **one task at a time**, in order. Each task has an ID, a file list, a specification, and acceptance criteria. Do not batch tasks. Do not skip ahead.

Phases are separated by **STOP GATES**. At a stop gate, halt and report — do not continue into the next phase in the same session. Phases 2 and later depend on machine resources and on what Phase 0 discovers, so they are specified as objectives and acceptance criteria rather than line-level instructions.

### Ground rules

1. **Read before writing.** Task 0.1 exists because this document was written from the README, not from the source. Do not assume a function name, a field name, or a module path that you have not verified in the code.
2. **Never overclaim.** If a component is substituted or simulated (Impala, Oozie), the README must say so plainly. Do not describe simulated behaviour as real.
3. **The lite path must keep working.** A reviewer cloning this repo on a laptop must be able to run something. Never break `docker compose --profile lite up`.
4. **No secrets in tracked files.** Tokens, signing secrets, channel IDs go in `.env`. `.env.example` gets placeholder values only. Verify `.gitignore` covers `.env` before Task 1.1.
5. **One task, one commit.** Conventional commits: `feat(triage): add severity classification`. Tests pass before every commit.
6. **Stop on a wrong assumption.** If a task's premise does not match the code, do not improvise a workaround. Stop, report what differs, and propose an amendment.
7. **Do not delete the Postgres pipeline** until the Phase 3 gate explicitly authorises it.
8. **Ask before anything destructive** — force pushes, history rewrites, dropping tables, deleting directories.

### What you cannot do — human prerequisites

These require a human and must be complete before Phase 1 begins. Check for their presence; if absent, stop and list what is missing.

- [ ] Slack workspace created (new, dedicated to this project)
- [ ] Slack app created from manifest, installed, bot token issued
- [ ] Channels created: `#etl-prod-alerts`, `#etl-prod-p1`, `#etl-changes`, `#etl-daily`; bot invited to each
- [ ] Channel IDs recorded
- [ ] Tunnel running (Cloudflare Tunnel preferred) with a stable hostname pointing at n8n on 5678
- [ ] `.env` populated with `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `SLACK_CHANNEL_ALERTS`, `SLACK_CHANNEL_P1`, `SLACK_CHANNEL_CHANGES`, `SLACK_CHANNEL_DAILY`, `PUBLIC_WEBHOOK_BASE`

---

# PHASE 0 — Orientation and the triage model

No infrastructure changes. Everything here runs against the existing Postgres pipeline.

## T0.1 — Inventory the repository

**Creates:** `docs/INVENTORY.md`

Read every source file and produce an inventory. This document is a working artifact for the tasks that follow; accuracy matters more than prose.

Record:
- Every module under `agent/`, `agent_tools/`, `mock_pipeline/` — path, purpose, public functions with signatures
- The exact tool schemas passed to the Claude API (names, descriptions, input schemas)
- The exact shape of the agent's current response object — every field name, its type, where it is constructed
- Every environment variable read anywhere in the codebase
- Every node name in `n8n_workflows/qa_agent_workflow.json` and the fields each reads
- Existing test files and what they cover
- Contents of `docs/` and how the GitHub Pages demo at `hpk369.github.io/ai-qa-agent` is generated
- Anything in the code that contradicts the README

**Acceptance:** a reader can rename a field in the agent response and know every file that must change.

---

## T0.2 — Severity model

**Creates:** `config/severity.yml`, `agent/severity.py`, `tests/pytest/test_severity.py`

Thresholds live in config, never in code. The module reads config and classifies.

```yaml
# config/severity.yml
severities:
  P1:
    label: "Critical — financial or availability impact"
    conditions:
      - target_unavailable: true
      - control_total_mismatch: true          # any monetary variance at all
      - job_failed_no_path_to_sla: true
    response: "Page immediately, engage app dev, open incident bridge"
  P2:
    label: "High — data integrity or SLA at risk"
    conditions:
      - row_variance_pct: {gte: 5.0}
      - sla_breach_projected: true
      - downstream_jobs_blocked: {gte: 1}
    response: "Notify on-call, begin remediation, hourly updates"
  P3:
    label: "Moderate — complete but degraded"
    conditions:
      - row_variance_pct: {gte: 1.0, lt: 5.0}
      - null_rate_increase_pct: {gte: 10.0, columns: critical}
      - job_duration_vs_baseline_pct: {gte: 200.0}
    response: "Ticket, next business day"
  P4:
    label: "Low — informational"
    conditions:
      - null_rate_increase_pct: {gte: 10.0, columns: non_critical}
      - log_anomaly_no_data_impact: true
    response: "Log, batch into weekly review"

column_criticality:
  critical: [customer_id, account_balance, account_number]
  non_critical: [last_updated_source, region_code]

evaluation:
  # highest matching severity wins; ties resolve to the more severe
  order: [P1, P2, P3, P4]
  default: null    # no match = no incident
```

**Required API:**

```python
classify(signals: dict, config: dict) -> SeverityResult
# SeverityResult: severity ("P1".."P4" | None),
#                 matched_conditions (list[str]),
#                 rationale (str),
#                 response_expectation (str)
```

`matched_conditions` must name every condition that fired. The agent will use it to justify the call in the incident record, and an unexplained severity is worthless.

**Tests:** a case per severity, boundary cases on every numeric threshold (4.99 vs 5.0 row variance), a clean-run case returning `None`, a multi-match case confirming the most severe wins, a case with an unknown column confirming it defaults to non-critical rather than crashing.

**Acceptance:** `pytest tests/pytest/test_severity.py -v` passes; changing a threshold in YAML changes classification with no code edit.

---

## T0.3 — Incident record

**Creates:** `agent/incident.py`, `schemas/incident.schema.json`, `tests/pytest/test_incident.py`
**Creates directory:** `reports/incidents/`

The incident record is the system of record. Slack is a view onto it.

Write a JSON Schema (draft 2020-12) with these fields. Everything not marked optional is required at open time.

| Field | Type | Notes |
|---|---|---|
| `incident_id` | string | `INC-YYYYMMDD-HHMM`; append `-2` on collision |
| `opened_at` | string | ISO 8601, UTC, `Z` suffix |
| `detected_by` | string | tool name that raised it |
| `severity` | enum | `P1`–`P4` |
| `severity_rationale` | string | from `matched_conditions` |
| `affected_job` | string | |
| `affected_objects` | array[string] | schema-qualified table names |
| `rows_expected` | integer\|null | |
| `rows_loaded` | integer\|null | |
| `impact_summary` | string | **business terms, not stack terms** — see below |
| `evidence` | array[string] | repo-relative paths |
| `root_cause` | string\|null | null until determined |
| `confidence` | number | 0.0–1.0 |
| `runbook` | string\|null | path under `docs/runbooks/` |
| `recommended_action` | string\|null | |
| `requires_approval` | boolean | |
| `slack_channel` | string\|null | channel ID |
| `slack_ts` | string\|null | parent message ts — the join key |
| `status` | enum | `open`, `acknowledged`, `remediating`, `verifying`, `resolved`, `false_positive` |
| `timeline` | array[object] | `{at, actor, event, detail}` |
| `mtta_seconds` | integer\|null | |
| `mttr_seconds` | integer\|null | |

`impact_summary` constraint: written for someone who does not know the stack. "Customer dimension is missing roughly 40% of records; three downstream reporting jobs cannot start" — not "Spark stage 4 failed with ExecutorLostFailure." The technical detail belongs in `root_cause`.

**Required API:**

```python
open_incident(signals, severity_result, run_context) -> Incident
append_timeline(incident, actor, event, detail) -> Incident
set_status(incident, status) -> Incident      # appends to timeline automatically
compute_mtta(incident) -> int | None          # opened_at → first human event
compute_mttr(incident) -> int | None          # opened_at → status=resolved
persist(incident) -> Path                     # reports/incidents/<id>.json
render_markdown(incident) -> str              # reports/incidents/<id>.md
load(incident_id) -> Incident
```

Validate against the schema on every `persist`. Fail loudly on violation — a malformed incident is worse than none.

**Tests:** schema validation accept and reject cases, ID collision handling, timeline ordering, MTTA with no human event returning `None`, MTTR on a resolved incident, round-trip persist and load.

---

## T0.4 — Change the agent's output contract

**Modifies:** `agent/` tool-use loop, agent system prompt, FastAPI endpoint
**Modifies:** `tests/pytest/` for any test asserting on `verdict`

The agent currently returns `verdict: PASS|FAIL`. It now returns an incident record, or a clean result.

New response shape from `/agent/run`:

```json
{
  "run_id": "...",
  "incident": { ... } | null,
  "clean": true | false,
  "checks_performed": ["recon", "logs", "schema"],
  "duration_ms": 1234
}
```

Update the system prompt so the agent:
- Reports observed signals rather than deciding severity itself — severity comes from `classify()`, deterministically, so the same evidence always yields the same call. Say this in the prompt explicitly.
- Writes `impact_summary` in business terms and `root_cause` in technical terms, and knows the difference.
- Returns `confidence` reflecting evidence quality, and states what would raise it.
- Sets `root_cause` to null rather than guessing when evidence is insufficient.

**Backward compatibility:** keep `verdict` in the response as a derived field (`"FAIL"` when `incident` is non-null) until T0.5 updates the n8n workflow. Remove it at the end of T0.5, not before.

**Acceptance:** existing failure injections produce valid incidents; the clean run produces `incident: null, clean: true`.

---

## T0.5 — Terminology sweep

**Modifies:** `README.md`, `n8n_workflows/qa_agent_workflow.json`, `docs/`, the GitHub Pages demo source, all inline comments

Apply consistently:

| From | To |
|---|---|
| AI QA Pipeline / QA Agent | ETL Production Support Triage Agent |
| verdict PASS/FAIL | incident severity / no incident |
| QA report | incident record |
| QA Summary (n8n node) | Build Incident Record |
| Call QA Agent (n8n node) | Call Triage Agent |
| `#qa-alerts` | `#etl-prod-alerts` |
| test run | validation check |

Rewrite the README opening so the first three sentences describe production support, not testing. Recast the Robot/pytest split in the Design Decisions section: Robot Framework verifies SLA and data contracts and re-runs after remediation to confirm restoration; pytest is the diagnostic deep-dive that isolates the failing component. The original rationale still holds — extend it, do not discard it.

Renaming n8n nodes breaks `$('node name')` cross-references in the Code node. Update every reference and re-import the workflow to confirm it loads.

Remove the derived `verdict` field from T0.4 once the workflow no longer reads it.

**Acceptance:** `grep -ri "qa agent\|verdict\|qa-alerts" --include="*.py" --include="*.md" --include="*.json" .` returns only intentional historical references; the workflow imports cleanly into n8n.

---

## 🛑 STOP GATE — Phase 0

Report: inventory summary, the severity thresholds as configured, a sample incident record from each failure mode, and anything in the code that contradicted this spec. Wait for approval before Phase 1.

---

# PHASE 1 — Slack incident channel, runbooks, evidence

Verify the human prerequisites checklist before starting.

## T1.1 — Slack client

**Creates:** `agent/slack_client.py`, `tests/pytest/test_slack_client.py`
**Modifies:** `.env.example`

Wraps the Slack Web API. Bot token only — the incoming-webhook approach cannot thread or update and is being removed.

```python
post_incident(incident) -> str                  # chat.postMessage → returns ts
reply_thread(incident, blocks, text) -> str     # chat.postMessage with thread_ts
update_parent(incident) -> None                 # chat.update on incident.slack_ts
mirror_p1(incident) -> str | None               # P1 only → #etl-prod-p1 with <!here>
post_change_log(incident, action, approver) -> str   # → #etl-changes
```

Requirements:
- `post_incident` writes the returned `ts` and channel ID back onto the incident and persists it before returning. If persistence fails after posting, log loudly — an orphaned Slack message with no record is the failure mode to avoid.
- Retry on 429 honouring `Retry-After`; exponential backoff on 5xx; three attempts then raise.
- **Stub mode:** when `SLACK_MODE=stub`, write the exact payload that would have been sent to `reports/slack/<incident_id>-<seq>.json` and return a synthetic ts. No network calls. This keeps the repo runnable for anyone without a workspace.
- Never log the bot token. Redact it from exception messages.

**Tests:** mock the HTTP layer entirely — no live Slack in the test suite. Cover ts capture and persistence, thread replies carrying the right `thread_ts`, retry on 429, stub mode writing files and making no network call, token redaction in errors.

---

## T1.2 — Block Kit builders

**Creates:** `agent/slack_blocks.py`, `tests/pytest/test_slack_blocks.py`, `tests/fixtures/blocks/`

Move off mrkdwn strings. Build Block Kit structures.

**Parent message:**
- Header block: severity emoji + severity + incident ID. A P1 and a P4 must be distinguishable without reading — 🔴 P1, 🟠 P2, 🟡 P3, ⚪ P4, ✅ resolved.
- Section: `impact_summary`
- Fields (two columns): affected job, affected objects, rows expected vs loaded, detected by, confidence, status
- Section: `root_cause` — omit the block entirely when null, do not render "None"
- Section: `recommended_action` + runbook link
- Context: opened_at, run_id, and MTTA/MTTR once known
- Actions block (only when `requires_approval`): Approve / Reject / Escalate, each carrying `action_id` and a `value` containing the incident ID

**Evidence truncation:** never paste a log into the channel. Show at most five lines with a link out. A wall of stack trace is how a channel gets muted.

**Golden-file tests:** commit expected JSON per severity under `tests/fixtures/blocks/` and assert byte-equality. Validate every payload against Slack's block limits — 50 blocks max, 3000 chars per text object, 10 fields per section. Exceeding a limit must raise at build time, not fail at post time.

---

## T1.3 — Request signature verification

**Creates:** `agent/slack_verify.py`, `tests/pytest/test_slack_verify.py`

Anyone who finds the interactivity URL can forge a button click. Verify every inbound request.

```python
verify_slack_request(headers: dict, raw_body: bytes, signing_secret: str) -> bool
```

Implement per Slack's scheme: base string is `v0:{timestamp}:{raw_body}`, HMAC-SHA256 with the signing secret, compare to `X-Slack-Signature` using `hmac.compare_digest`. Reject when the timestamp is more than 300 seconds old.

Two things that are easy to get wrong and must be right: use the **raw** body bytes, not a re-serialised parse — any reordering breaks the HMAC; and use a constant-time comparison, not `==`.

**Tests:** valid signature accepted, tampered body rejected, wrong secret rejected, stale timestamp rejected, future timestamp beyond skew rejected, missing headers rejected without raising.

---

## T1.4 — n8n workflow rewiring

**Modifies:** `n8n_workflows/qa_agent_workflow.json`
**Creates:** `docs/workflow-map.md`

Changes:
1. Replace the Slack webhook node with a Slack node using an Access Token credential calling `chat.postMessage`.
2. Add a Set node after it capturing `ts` into the incident record.
3. Add a second webhook trigger at `/webhook/slack-action` for interactivity.
4. First node on that path is a Code node calling signature verification. Reject with 401 on failure. **This node runs before anything else touches the payload.**
5. Slack's 3-second response deadline is not negotiable: acknowledge immediately with a 200, then continue processing asynchronously. Do not do the work before responding.
6. On approval, post to `#etl-changes` and update the parent message.

Update `$('node name')` references for the T0.5 renames.

Export and re-import the workflow to confirm it round-trips. Document each node in `docs/workflow-map.md` in the same table format the README already uses.

---

## T1.5 — Approval gate

**Modifies:** `agent/incident.py`, workflow JSON
**Creates:** `tests/pytest/test_approval.py`

- `requires_approval` true means no remediation proceeds without a recorded decision.
- Record approver Slack user ID, decision, and timestamp in `timeline`.
- Post the decision to `#etl-changes` — that channel is the audit trail.
- Reject the second decision on an already-decided incident; report it in thread rather than silently ignoring it.
- Escalate sets status to `open`, mirrors to `#etl-prod-p1`, appends to timeline.

**Fallback:** if the tunnel proves unreliable, implement `APPROVAL_MODE=reaction` — a scheduled workflow polls `reactions.get` on the parent ts and treats ✅ from an ID in `SLACK_APPROVERS` as approval. Build the interactive path first. Whichever ships, the README states which.

---

## T1.6 — Runbooks

**Creates:** `docs/runbooks/TEMPLATE.md` plus one runbook per current failure mode

`RB-001-row-shortfall.md`, `RB-002-schema-drift.md`, `RB-003-null-spike.md`, `RB-004-consumer-lag.md`, `RB-005-job-failure.md`

Template sections, in this order:

```markdown
# RB-XXX — <Title>
**Severity guidance:** typically P_
**Owner:** Application Support
**Last reviewed:** YYYY-MM-DD

## Symptom
How it presents in monitoring and in Slack.

## Impact
Who is affected and what they cannot do. Business terms.

## Diagnostic steps
Numbered, with exact commands. Each step states what output confirms or rules out this cause.

## Remediation options
| Option | Risk | Requires approval | Notes |

## Escalation
Who, when, and what to hand over.

## Prevention
Permanent fix and its owner.
```

Every command must be real and runnable against this repo. A runbook with invented commands is worse than no runbook — it will be read as filler by anyone who checks.

Wire `runbook` on the incident record to the right file per detected condition.

---

## T1.7 — Evidence bundle

**Creates:** `agent/evidence.py`, `scripts/first-15-minutes.sh`, `tests/pytest/test_evidence.py`

Collect into `reports/evidence/<incident_id>/` on incident open: application/job log, last 200 lines of stdout, row counts both sides, target DDL, Kafka consumer group offsets and lag, disk and inode snapshot, a `manifest.json` listing each artifact with size and collection timestamp.

`first-15-minutes.sh` does the same collection standalone, with no Python dependency and no agent involvement. It must run under `set -euo pipefail`, work on a machine where the pipeline is broken, and degrade gracefully — a missing source produces a note in the manifest, not a crash. This script is a portfolio artifact in its own right; write it as if handing it to an on-call colleague.

Cap total bundle size at 50 MB; truncate the largest artifacts first and record the truncation in the manifest.

---

## T1.8 — MTTA and MTTR

**Modifies:** `agent/incident.py`, `agent/slack_client.py`
**Creates:** `scripts/incident_metrics.py`

MTTA is the interval from `opened_at` to the **first human event** — a thread reply or a reaction from a non-bot user, not the bot's own post. Poll `reactions.get` and `conversations.replies` on the parent ts, or handle `reaction_added` events if you add the subscription.

MTTR runs from `opened_at` to `status: resolved`.

On resolution, update the parent message to show resolved status and both figures.

`scripts/incident_metrics.py` reads `reports/incidents/*.json` and prints count by severity, median and p90 MTTA, median and p90 MTTR, and false-positive rate. These are the numbers that become a résumé line, so compute them honestly — including the incidents where the agent was wrong.

---

## 🛑 STOP GATE — Phase 1

Demonstrate one incident opening, being acknowledged, approved, remediated, verified and resolved inside a single Slack thread, with the parent message edited in place throughout. Report the metrics output. Wait for approval.

---

# PHASE 2 — Hadoop stack

> **⚠️ SUPERSEDED by `/ROADMAP.md` §2–§4 (2026-09-17).** The Hadoop stack moved from Docker
> Compose to a dedicated OCI VM, specified in `infra/bankdemo/IMPLEMENTATION_GUIDE.md`. T2.1–T2.5
> below are **not to be built**: the laptop cannot hold the stack alongside the tooling, a VM
> reproduces host-level failures (disk, inodes, OOM, systemd) that Compose cannot, and it can run
> on a schedule unattended. `--profile lite` is now the permanent demo path rather than a
> migration waypoint, and there is no Hive (it does not fit the 12 GB budget — Spark SQL over
> HDFS Parquet covers the same ground, and the README must not imply Hive ran).
>
> Phases 3–4 below are likewise redirected: T3.1–T3.3 become roadmap **B5.2**, built against
> bundle evidence rather than a live Compose stack; T3.4's failure modes become the F01–F17
> fault catalog; T3.5 is dropped; Oozie is replaced by bankdemo's Autosys-flavoured
> `scheduler/jobs.yaml`. Phase 5's scorecard becomes **B5.5**, and is finally buildable because
> bankdemo's answer key supplies ground truth.
>
> The original text is kept below for provenance.

**Do not begin without confirming available RAM.** Under roughly 12 GB, stop and report; Spark standalone is the honest fallback and the plan changes.

Specified as objectives. Work them as separate sessions.

**T2.1 — Compose profiles.** Split `docker-compose.yml` into `lite` (current Postgres path) and `hadoop`. `--profile lite` must keep working unchanged.

**T2.2 — HDFS and YARN single-node.** NameNode, DataNode, ResourceManager, NodeManager. Acceptance: `hdfs dfs -ls /` works, ResourceManager UI reachable, a sample job appears in the applications list.

**T2.3 — Hive.** Metastore and HiveServer2, metastore backed by the **existing Postgres 15 container** rather than a new database. Acceptance: an external table over HDFS Parquet is queryable.

**T2.4 — Spark transform.** `spark_jobs/cx_customer_load.py`, submitted with `--master yarn`. Reads source Hive schema, writes partitioned Parquet to target. Realistic CX transformations: dedup on natural key, SCD handling on the customer dimension, date and currency normalisation, PII masking on at least one column. Partition by load date.

**T2.5 — Port validation.** Existing checks run against Hive instead of Postgres. All Phase 0–1 behaviour intact on the new stack.

**Known pitfalls:** container memory defaults are too low for any real Spark job and will produce OOM kills you did not intend to inject; `/etc/hosts` and hostname resolution between containers is the usual source of NameNode connection failures; the Hive metastore schema must be initialised with `schematool` before first use.

## 🛑 STOP GATE — Phase 2

Report resource usage under load, and whether the lite profile still runs.

---

# PHASE 3 — Rewrite the tools

**T3.1 — SQL Validator → Recon Checker.** Queries Hive/Spark SQL. Adds control totals (sum of balances matching to the cent), duplicate detection on natural key, referential integrity against dimensions, re-run idempotency.

**T3.2 — Log Analyser → YARN Log Analyser.** Parses aggregated container logs and Spark event logs. Detects: `Container killed by YARN for exceeding physical memory limits`, `ExecutorLostFailure`, stage retry loops, tasks far above the stage median runtime, shuffle fetch failures.

**T3.3 — Metastore Comparator.** Compares Hive metastore schema against actual Parquet footer schema; detects partitions on HDFS missing from the metastore.

**T3.4 — New failure modes.** `container_oom`, `partition_skew`, `small_files`, `metastore_drift`, `stale_partitions`, `late_arriving`, `duplicate_on_rerun`, plus existing `consumer_lag`.

The pair that matters: `stale_partitions` and `row_shortfall` present identically — both look like missing rows. The agent must distinguish them and its `root_cause` must explain how. Build a test asserting exactly this, and make the distinguishing logic legible in the code; it is the single best demonstration in the project.

**T3.5 — Retire the Postgres pipeline.** Only after the above pass. Confirm before deleting.

---

# PHASE 4 — Oozie and SLA

Real `workflow.xml` and `coordinator.xml`: daily frequency, dataset dependency on the source `_SUCCESS` marker, SLA block with `should-end`. Run Oozie in Docker if it comes up cleanly; otherwise Airflow as executor with `docs/scheduler-mapping.md` explaining the substitution and the Oozie/Autosys equivalences.

**Honesty requirement:** whichever runs, the README says so. SLA breach becomes a first-class trigger raising a P2.

---

# PHASE 5 — Evidence layer

**T5.1** — A postmortem per failure mode in `docs/postmortems/`: timeline, impact, detection, root cause, remediation, prevention. Written from real runs, with real timestamps.

**T5.2** — `docs/scorecard.md`: across N runs, how often the agent classified correctly, with **every miss listed and analysed**. A scorecard without misses reads as fabricated and will be treated as such.

**T5.3** — Final README and demo page rewrite reflecting the finished system.

---

## Definition of done

- [ ] `docker compose --profile lite up` works on a clean clone
- [ ] `docker compose --profile hadoop up` brings up the full stack
- [ ] All eight failure modes inject and are correctly classified
- [ ] Every incident opens, threads, and resolves in Slack with the parent edited in place
- [ ] No remediation runs without a recorded approval in `#etl-changes`
- [ ] Every failure mode has a runbook with commands that actually run
- [ ] `pytest tests/pytest/ -v` green; Robot suite green
- [ ] Scorecard published with misses included
- [ ] No substituted component described as real anywhere in the repo
- [ ] No secret in any tracked file
