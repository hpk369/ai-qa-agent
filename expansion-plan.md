# Expansion Plan — `ai-qa-agent` → Production Support Triage on a Hadoop Stack

**Repo:** https://github.com/hpk369/ai-qa-agent
**Author:** Harsh Keshruwala
**Purpose of this document:** define the two-track expansion that repositions this project from a QA automation demo into a production support artifact aligned with Application Support Analyst roles at banks (Citi C11 Apps Support Intermediate Analyst as the reference JD).

---

## 1. Why change anything

The project currently works and demonstrates real skill. The problem is positioning. As built, it answers the question *"can you automate testing of a data pipeline?"* The roles being targeted ask a different question: *"when this job breaks at 02:00 and a trader can't see their book, what do you do in the first fifteen minutes?"*

Two changes close that gap:

| Track | Change | What it buys |
|---|---|---|
| **A — Triage reframe** | Recast detect-and-route as detect, classify, evidence, recommend, verify | Interview stories: severity calls, escalation, MTTR, runbooks |
| **B — Stack migration** | Replace the Postgres/Python mock with Spark on YARN writing to Hive on HDFS | Credibility: YARN container logs, metastore drift, skew, small files |

Track A is documentation and light code. Track B is infrastructure work. Do A first — it is cheap and it changes how every later piece is described.

---

## 2. Current state

```
Kafka event → n8n → Claude agent (3 tools) → PASS/FAIL
                                               │
                          PASS → Robot Framework    FAIL → pytest
                                               │
                                   Slack + Jenkins + webhook response
```

- **Pipeline:** `mock_pipeline/producer.py`, Python + kafka-python, PostgreSQL 15 source and target
- **Tools:** SQL Validator, Log Analyser, Schema Comparator (FastAPI, `agent_tools/`)
- **Failure modes:** `none`, `row_drop`, `schema_drift`, `null_spike`, `latency`
- **Orchestration:** n8n, 12 nodes, importable JSON

## 3. Target state

```
Oozie coordinator fires Spark job (source schema → target schema, customer data)
                            │
                   Spark on YARN → Hive external tables on HDFS
                            │
          job fails / SLA breach / recon mismatch → n8n webhook
                            │
                   [Triage Agent]  ◄── Claude, tool-use
                            │
      ┌─────────────────────┼──────────────────────┐
 [Recon Checker]     [YARN Log Analyser]   [Metastore Comparator]
      │                     │                      │
      └─────────────────────┴──────────────────────┘
                            │
              Severity classification (P1–P4)
              Incident record + evidence bundle
              Root cause + runbook link + recommended action
                            │
          ┌─────────────────┴─────────────────┐
     no incident                          incident
          │                                   │
  Robot Framework                          pytest
  SLA/contract verification            diagnostic deep-dive
          │                                   │
          └─────────────────┬─────────────────┘
                            │
          Incident record → Slack #etl-prod-alerts (threaded) + CI gate
          (post-remediation: re-run RF to confirm restoration)
```

---

## 4. Track A — Triage reframe

### A1. Severity classification

Replace the binary `PASS`/`FAIL` verdict with a severity call. The agent must justify the severity in its output, not just emit a label.

| Severity | Trigger | Response expectation |
|---|---|---|
| **P1** | Target table unavailable, or control totals do not reconcile (financial impact), or job failed with no path to complete before SLA | Page immediately, engage app dev, incident bridge |
| **P2** | Row variance beyond threshold, SLA breach projected, downstream jobs blocked | Notify on-call, begin remediation, hourly updates |
| **P3** | Data complete but degraded — elevated nulls under tolerance, job ran long, retry succeeded | Ticket, next business day |
| **P4** | Cosmetic — single non-critical column, informational log anomaly | Log, batch into weekly review |

Implementation: a `severity` module in `agent/` with explicit thresholds in config, not hardcoded. Be prepared to defend every threshold in interview — why 5% row variance is P2 and 1% is P3, why null-rate tolerance differs by column criticality.

### A2. Incident record

Every non-clean run produces a structured incident object, persisted to `reports/incidents/INC-<timestamp>.json` and rendered to markdown.

```json
{
  "incident_id": "INC-20260912-0214",
  "opened_at": "2026-09-12T02:14:33Z",
  "detected_by": "recon_checker",
  "severity": "P2",
  "affected_job": "cx_customer_load",
  "affected_objects": ["target.customer_dim", "target.account_balance_fact"],
  "rows_expected": 1482930,
  "rows_loaded": 889758,
  "impact_summary": "40% row shortfall in target.customer_dim; 3 downstream jobs blocked",
  "evidence": ["reports/evidence/INC-.../yarn_app_1757.log", "..."],
  "root_cause": "...",
  "confidence": 0.82,
  "runbook": "docs/runbooks/RB-004-row-shortfall.md",
  "recommended_action": "...",
  "requires_approval": true,
  "status": "open",
  "slack_channel": "C09ETLPROD",
  "slack_ts": "1757640873.004200",
  "mtta_seconds": null,
  "mttr_seconds": null
}
```

The JSON is the system of record; **Slack is the human surface** — see A6. The `slack_ts` field is the join key between the two, and it is what lets the agent update an existing incident rather than posting a duplicate.

MTTA and MTTR fields are the point. Being able to say "median detection-to-recommendation was eleven seconds across forty simulated incidents" is a quantified résumé line.

### A3. Runbooks

Create `docs/runbooks/`, one file per failure mode, written in the format a bank actually uses:

- Symptom and how it presents in monitoring
- Immediate impact and who is affected
- Diagnostic steps in order, with exact commands
- Remediation options, each with risk level
- What requires change approval and what does not
- Escalation path and when to escalate
- Prevention / permanent fix owner

The agent's `recommended_action` should reference the runbook ID. This is the link between "AI said something" and "a human can act on it," and it is the thing that makes the project legible to a traditional support manager.

### A4. Evidence bundle

On incident open, collect and archive what an L2 analyst would gather before escalating: the YARN application log, the Spark event log, the last 200 lines of the driver stdout, row counts on both sides, the Hive table DDL, Kafka consumer group offsets and lag, and a disk/inode snapshot. Write a `scripts/first-15-minutes.sh` that does this collection standalone — useful on its own, and it demonstrates shell fluency.

### A5. Terminology and README rewrite

The language throughout the repo should change. This is not cosmetic; it is how a reviewer decides in thirty seconds what the project is.

| Current | Becomes |
|---|---|
| AI QA Pipeline | ETL Production Support Triage Agent |
| verdict PASS / FAIL | no-incident / incident with severity |
| QA report | incident record + evidence bundle |
| `#qa-alerts` | `#etl-prod-alerts` |
| test run | validation check |
| Robot Framework on PASS | SLA and data-contract verification (also re-run post-remediation to confirm restoration) |
| pytest on FAIL | diagnostic deep-dive to isolate the failing component |

Keep the Robot/pytest split — the rationale is still sound, and the restoration-verification loop makes it stronger than it was.

### A6. Slack as the incident channel

Slack is where the incident lives for humans. The JSON record is the machine-readable truth; the Slack thread is the timeline, the escalation path, and the audit trail.

**Required infrastructure change.** The workflow currently posts via `SLACK_WEBHOOK_URL`, an incoming webhook. Incoming webhooks cannot update a message, cannot reliably return a message timestamp, and cannot carry interactivity — so they cannot thread an incident. Replace with a Slack app holding a bot token and these scopes: `chat:write`, `chat:write.public`, `reactions:read`, `channels:history`. The n8n Slack node then uses `chat.postMessage` (which returns `ts`) and `chat.update`. Keep `SLACK_BOT_TOKEN` in the env, never in the workflow JSON.

**Channel layout.**

| Channel | Contents |
|---|---|
| `#etl-prod-alerts` | Every incident, all severities. One parent message per incident |
| `#etl-prod-p1` | P1 only, mirrored, `@here` on post. Exists so nobody mutes the channel that matters |
| `#etl-changes` | Append-only log of remediation actions taken and who approved them |
| `#etl-daily` | Scheduled digest: runs completed, SLA met/missed, open incidents |

**Threading model.** One parent message per incident, everything else as a thread reply. The parent carries the current state and is edited in place via `chat.update` as status changes. The thread carries the timeline, so the incident history and the conversation are the same object — which is exactly how it works on a real support desk, and it is why this is worth building rather than firing a fresh alert per event.

- Parent message, posted on incident open, formatted with Block Kit: severity badge, incident ID, affected job and objects, impact stated in business terms, root cause, confidence, runbook link, recommended action, and a status field.
- Thread reply on each subsequent event: remediation proposed, approval granted or refused, action executed, verification suite re-run, resolved.
- On resolution, edit the parent to show `RESOLVED`, swap the severity emoji, and append MTTA/MTTR.

**Message formatting.** Move from mrkdwn strings to Block Kit. A P1 and a P4 should be visually distinguishable at a glance without reading — colour attachment, emoji, and severity in the first line. Truncate evidence in the message and link out rather than dumping a container log into the channel; a wall of stack trace in `#etl-prod-alerts` is how a channel gets muted.

**MTTA from Slack.** Measure acknowledgement as the first human reaction or thread reply on the parent message, not as the bot's own post. This gives a defensible number rather than a synthetic one, and it makes a good interview answer about why you measured it that way.

**Approval gate.** The `requires_approval` field in the incident record maps to interactive buttons on the Slack message — Approve, Reject, Escalate. The button fires a webhook back into n8n, which records the approver's user ID and timestamp in the incident JSON and posts the decision to `#etl-changes`. No remediation runs without a recorded human approval. If interactivity turns into a time sink, an acceptable interim is an emoji-reaction gate polled by the workflow, but say which one you built.

**Demo consideration.** For a repo anyone can clone, a live Slack workspace cannot be a hard dependency. Provide a `SLACK_MODE=stub` that writes the exact Block Kit payloads to `reports/slack/` and screenshot a real workspace for the README.

---

## 5. Track B — Stack migration

### Target components and honest feasibility

| Component | Plan | Notes |
|---|---|---|
| **HDFS** | Single-node in Docker (`apache/hadoop`) | Worth the trouble. Gives real `hdfs dfs` CLI, NameNode UI, block/replication concepts, small-file pressure |
| **YARN** | ResourceManager + one NodeManager | **Highest value item in this plan.** Real container logs, real `yarn logs -applicationId`, real OOM kills |
| **Spark** | Spark 3.x submitted in `yarn` mode | The transform job itself: source schema → target schema |
| **Hive** | HiveServer2 + Metastore, metastore DB on the existing Postgres 15 | Reuses a container already in compose. External tables over HDFS Parquet |
| **Kafka** | Keep as-is | Already working; remains the trigger and the lag signal |
| **Impala** | **Do not run.** Substitute Spark SQL or Trino | Impala outside CDH is a genuine time sink. Instead, be fluent verbally: MPP daemons vs MapReduce/Tez, metadata caching, `INVALIDATE METADATA` vs `REFRESH`, why Impala is chosen for interactive BI. Note the substitution in the README — never claim to have run what you simulated |
| **Oozie** | Write real `workflow.xml` and `coordinator.xml`; execute via Oozie in Docker if it comes up cleanly, otherwise Airflow as executor with a mapping doc | Same honesty rule. The XML artifacts and the understanding of coordinator frequency, dataset dependencies, and SLA blocks are what get discussed |

**Resource reality check:** HDFS + YARN + Hive + Kafka + Postgres + n8n + the agent will not run comfortably in under about 12 GB of RAM. Use Docker Compose profiles (`--profile hadoop`, `--profile lite`) so the original lightweight path still works for anyone cloning the repo, including a recruiter who wants to see it run.

### B1. Port the transform to Spark

Rewrite the source→target movement as a PySpark job in `spark_jobs/cx_customer_load.py`. It should read from the source Hive schema, apply realistic CX transformations (deduplication on natural key, SCD handling on the customer dimension, currency or date normalisation, PII masking on a column or two), and write partitioned Parquet to the target schema. Partition by load date. Run it with `spark-submit --master yarn`.

### B2. Rewrite the three tools against the new stack

- **SQL Validator → Recon Checker.** Queries Hive (or Spark SQL) rather than Postgres. Expand beyond row counts to control totals — sum of balances matching to the cent between source and target — plus duplicate detection on the natural key, referential integrity against dimension tables, and re-run idempotency.
- **Log Analyser → YARN Log Analyser.** Parses actual YARN aggregated container logs and Spark event logs. Detects the signatures that matter: `Container killed by YARN for exceeding physical memory limits`, `ExecutorLostFailure`, stage retry loops, one task with a runtime far above the stage median, shuffle fetch failures.
- **Schema Comparator → Metastore Comparator.** Compares the Hive metastore schema against the actual Parquet footer schema, and detects partitions present on HDFS but absent from the metastore (the `MSCK REPAIR TABLE` case). This is a real and commonly misdiagnosed production problem — it presents as missing rows and gets escalated as a data loss incident when it is a metadata gap.

### B3. New failure modes

Retire or supplement the current injection list with failures that are recognisable to a Hadoop support analyst.

| Mode | Injection method | Presents as | Detecting tool |
|---|---|---|---|
| `container_oom` | Shrink executor memory, widen a shuffle | Job fails mid-stage, container killed | YARN Log Analyser |
| `partition_skew` | Load a key distribution where one value dominates | 199 tasks done, 1 task running for 40 minutes | YARN Log Analyser |
| `small_files` | Write with high parallelism and no coalesce | Thousands of tiny part files, next read degraded, NameNode object count climbs | Recon Checker + custom HDFS check |
| `metastore_drift` | Alter Parquet schema without updating the Hive table | Column reads as null, or read fails on type mismatch | Metastore Comparator |
| `stale_partitions` | Write partition directories to HDFS, skip `MSCK REPAIR` | Looks exactly like row shortfall | Metastore Comparator |
| `late_arriving` | Deliver records after the watermark | Counts reconcile tomorrow but not today | Recon Checker |
| `duplicate_on_rerun` | Re-run without idempotency guard | Target row count exceeds source | Recon Checker |
| `consumer_lag` | Keep existing latency injection | Lag over threshold, upstream starvation | YARN Log Analyser / Kafka check |

The pair `stale_partitions` and `row_shortfall` presenting identically is the best interview material in this list. The agent distinguishing them — and explaining *how* it distinguished them — is a genuinely strong demonstration.

### B4. Oozie coordinator and SLA

Define the job as an Oozie coordinator with a daily frequency, a dataset dependency on the source partition's `_SUCCESS` marker, and an SLA block with a `should-end` time. This makes SLA breach a first-class trigger for the triage agent rather than an afterthought, and it gives you concrete vocabulary — coordinator vs workflow vs bundle, dataset done-flags, `EL` functions — that appears directly in the JD.

---

## 6. Phasing

| Phase | Scope | Rough effort | Done when |
|---|---|---|---|
| **0** | Track A reframe: README, terminology, severity model, incident record schema | ~1 week evenings | Repo reads as a support project; incident JSON emitted on every run |
| **1** | Slack app + bot token, threaded incident model, Block Kit; runbooks for all existing failure modes; evidence bundle; `first-15-minutes.sh` | ~1.5 weeks | An incident opens, updates, and resolves inside a single Slack thread; every failure mode has a runbook the agent links to |
| **2** | HDFS + YARN + Hive in compose; Spark transform ported; existing checks pass on new stack | ~2–3 weeks | `spark-submit --master yarn` runs the load end to end |
| **3** | Three tools rewritten; new failure modes injectable | ~2 weeks | All eight modes inject cleanly and are correctly classified |
| **4** | Oozie coordinator + SLA-breach trigger | ~1 week | Coordinator fires the job; SLA miss raises a P2 |
| **5** | Evidence layer: postmortem per mode, classification scorecard | ~1 week | Published accuracy numbers, misses included |

Phases 0 and 1 are worth doing even if Track B stalls. They stand alone.

---

## 7. Scope guards

- **Do not run both stacks in parallel indefinitely.** Once the Spark path works, delete the Postgres-only pipeline rather than maintaining two.
- **Do not add Impala, Hue, Ranger, Atlas, or a second scheduler** because the JD mentions them. Depth on four components beats shallow coverage of ten.
- **Never claim to have run what was simulated.** Where Impala or Oozie is substituted, say so in the README. A hiring manager who catches an overclaim discards everything else.
- **Keep the lite profile working.** If someone cannot run it, they will not evaluate it.
- **The AI agent should not be the headline.** Lead with the triage and the stack; the Claude tool-use loop is a feature, not the product. A support hiring manager cares more that you can read a container log than that you orchestrated an LLM.

---

## 8. JD mapping

How the finished project answers specific Citi Apps Support JD bullets:

| JD requirement | Where this project answers it |
|---|---|
| Hadoop/Big Data platform: HDFS, Hive, Spark, YARN, Kafka, Oozie | Phases 2–4, running stack |
| Impala | Verbal fluency + documented substitution rationale |
| Linux, 4–6 years | `scripts/`, evidence collection, container debugging |
| SQL and RDBMS | Recon checks, Hive metastore on Postgres, control totals |
| Performance tuning and cluster optimisation | Skew and small-files modes; executor memory tuning as remediation |
| Scheduler (Autosys / Control-M) | Oozie coordinator + SLA; discuss the mapping to Autosys box jobs and JIL |
| Shell scripting / Python | `first-15-minutes.sh`, injection harness, all tooling |
| Monitoring tools (ITRS) | Threshold config, alert design rationale, alert-fatigue reasoning |
| Post-deployment validation | Robot Framework contract suite, re-run post-remediation |
| Troubleshooting and coordinating with dev teams | Runbooks, escalation paths, incident records |
| Liaison between users and tech | Impact summaries written in business terms, not stack terms |

---

## 9. Open questions to resolve before Phase 2

1. How much RAM is actually available on the build machine? This determines whether single-node YARN is viable or whether Spark standalone is the honest fallback.
2. Is the customer data model rich enough to make control-total reconciliation meaningful? If the schema has no monetary or countable measure, add one — reconciliation on row counts alone is the weak version.
3. Should the Kafka trigger remain, or should Oozie become the sole entry point? Keeping both is defensible (event-driven plus scheduled) but doubles the trigger paths to maintain.
