# ETL Production Support Triage Agent

**[▶ Live Demo](https://demo.inkandinfra.com/)** — interactive pipeline simulator, no setup required. Walks through severity classification, an incident record, and a Slack Block Kit preview with working Approve/Reject/Escalate buttons.

When a production ETL job breaks, the question that matters isn't "did the pipeline pass or fail" — it's "what's the severity, who's affected, and what do I do in the first fifteen minutes." This project answers that from the one artifact an on-call analyst always has: **the logs**.

Each session gets its own log set — a few log files mixed from a corpus of real, public production logs (Spark, YARN, HDFS, ZooKeeper, OpenStack, syslog, sshd, and more) with ETL failure signatures injected into them. The agent reads the log text, derives severity signals from it, classifies them with a deterministic config-driven ruleset, opens a structured incident record, and **posts a Slack alert**. The thread reply carries a link to download the exact log set that produced the alert, so the call can be checked against the evidence.

## What this is

**One loop: a set of logs in, a Slack alert out, and the logs behind it downloadable.** It runs with no API key, no cluster and no services — `python scripts/logset.py` does the whole thing on a fresh clone.

The pieces behind that loop:

| Piece | Where |
|---|---|
| 15 log sources and 14 ETL error signatures | `logsets/catalog.py` |
| Public log corpus, fetched at setup, with a generated fallback | `logsets/corpus.py`, `scripts/fetch_logs.py` |
| Per-session mixing, ground-truth manifest, zip bundle | `logsets/session.py` |
| Analysis → signals → severity → incident → Slack alert | `logsets/triage.py` |
| Deterministic severity, thresholds in config rather than code | `agent/severity.py`, `config/severity.yml` |
| Incident record, approval gate, MTTA/MTTR | `agent/incident.py`, `scripts/incident_metrics.py` |
| Slack Web API client, Block Kit messages, HMAC verification | `agent/slack_client.py`, `agent/slack_blocks.py`, `agent/slack_verify.py` |
| Five runbooks, deterministically selected | `agent/runbooks.py`, `docs/runbooks/` |
| Entry points | `scripts/logset.py`, `POST /logset/run`, `GET /logset/{id}/download` |

**Slack runs in stub mode by default.** `SLACK_MODE=stub` writes every payload Slack would have received to `reports/slack/` and makes no network call, so the full alert path — parent message, thread reply, P1 mirror, button interactions — runs end to end with nothing to set up. Set `SLACK_MODE=live` with a bot token in `.env` to post into a real workspace; [`docs/SLACK_SETUP.md`](docs/SLACK_SETUP.md) walks through obtaining each value.

<a id="demo-pages"></a>**Demo page.** [`docs/index.html`](https://demo.inkandinfra.com/) is a client-side simulation of `agent/severity.py`'s and `agent/slack_blocks.py`'s output for each mock-pipeline failure mode, including a working Approve/Reject/Escalate flow against a mocked Slack thread. It needs no backend, so it works unmodified on GitHub Pages.

## Log-set triage

```bash
# Optional: fetch the public log corpus (~3 MB, gitignored, not vendored).
# Skip it and background lines are generated instead — the manifest says which.
python scripts/fetch_logs.py

# Mix a log set for this session, triage it, alert Slack, print the download path
python scripts/logset.py

python scripts/logset.py --seed 2026            # reproduce a set exactly
python scripts/logset.py --sources spark-executor,kafka-consumer
python scripts/logset.py --injections 3 --count 5
python scripts/logset.py --clean --no-slack     # background only, nothing injected
python scripts/logset.py --list-sources
python scripts/logset.py --show LS-...          # re-read a set without alerting
```

```
Log set   LS-20260920-001007-07ea  (seed 2026)
Scanned   1207 lines across 3 file(s) — 2 error / 390 warn

  FILE                         BACKGROUND   LINES   ERR  WARN
  openstack-nova.log           real           413     0     6
  zookeeper.log                real           373     1   285
  hive-metastore.log           generated      421     1    99

Recognised signatures:
  SIG-013-ZK-SESSION-EXPIRED   zookeeper.log:65  ZooKeeper session expired
  SIG-010-CONTROL-TOTAL        hive-metastore.log:120  Control total mismatch between source and target

Incident  INC-20260920-0010  P1  — approval required
Rationale control_total_mismatch
Runbook   docs/runbooks/RB-005-job-failure.md
Slack     posted to C_ALERTS (ts 1789863007.824481)
Detection 2/2 injected signature(s) found (recall 1.0)

Download  reports/logsets/LS-20260920-001007-07ea.zip
```

Over HTTP, the same thing (`python agent/agent.py`):

| Endpoint | What it does |
|---|---|
| `POST /logset/run` | Mix this session's set, triage it, alert Slack, return the incident and the download link. Body: `{seed, sources, source_count, injections, clean, notify, session_id}` — all optional |
| `GET /logset` | Sessions built so far |
| `GET /logset/{session_id}` | Re-read a set: files, findings, signals, detection score. Read-only — opens no incident, posts nothing |
| `GET /logset/{session_id}/download` | The zip: every log file, the manifest, a README |

Set `AGENT_PUBLIC_URL` and the Slack thread reply renders a **⬇ Download log set** button pointing at that endpoint instead of a local path.

### How a session's set is mixed

```
          logsets/corpus/            logsets/catalog.py
   real public logs (LogHub)     14 ETL error signatures
   or generated background                │
                │                         │
                └──────────┬──────────────┘
                           ▼
              logsets/session.py  — 3-5 sources, 120-420 lines each,
              1-4 signatures injected at random offsets, seeded
                           │
                   ┌───────┴────────┐
                   ▼                ▼
         reports/logsets/<id>/   manifest.json  (ground truth:
         *.log                    what went in, and where)
                   │
                   ▼
              logsets/triage.py  — regex match → signals
                   │
                   ▼
         agent/severity.py + config/severity.yml   (deterministic)
                   │
              ┌────┴─────┐
         clean │          │ incident (P1-P4)
          no   │          ▼
         alert │   agent/incident.py  →  Slack parent message
               │                      →  thread reply + download link
               │                      →  reports/logsets/<id>.zip
```

The seed fixes the content of a set — same seed, same sources, same signatures, same places. Timestamps re-anchor to the build time, so a rebuild is the same set dated today; pass a fixed anchor (`build_session(started_at=...)`) for a byte-identical rebuild.

**The manifest's ground truth is never read by the triage path.** The agent works from the log text alone; `logsets.triage.score()` compares its findings against the manifest afterwards and reports recall, which is what makes "it found the thing" a measurement rather than a claim. A test asserts the analysis is unchanged when the ground truth is deleted.

**Unrecognised errors are reported as exactly that.** An `ERROR` line no signature matches raises `log_anomaly_no_data_impact` — a P4, "something is wrong and its data impact is not established" — rather than being guessed at or dropped. Real logs contain real errors, so a set mixed from the real corpus is rarely perfectly clean, and that is the honest answer.

### Log sources

`python scripts/logset.py --list-sources` prints these. ETL-side sources are what signatures get injected into; the rest are carried as the infrastructure noise a real analyst has to read past.

| Source | Role | Background |
|---|---|---|
| `spark-executor`, `yarn-appmaster`, `hdfs-datanode`, `zookeeper` | ETL (triaged) | Real — LogHub Spark / Hadoop / HDFS / ZooKeeper |
| `kafka-consumer`, `hive-metastore`, `airflow-scheduler` | ETL (triaged) | Generated — nobody publishes these |
| `os-syslog`, `openstack-nova`, `edge-sshd`, `edge-httpd`, `bluegene-ras`, `hpc-cluster`, `thunderbird-cluster`, `proxifier` | Infrastructure noise | Real — LogHub |

Background lines come from the [LogHub](https://github.com/logpai/loghub) collection of real system logs, fetched by `scripts/fetch_logs.py` into `logsets/corpus/` (gitignored — third-party research datasets are fetched, not vendored). Every log file in a session's manifest records whether its background was `real` or `generated`.

### Error signatures

`logsets/catalog.py`. Each is a regex plus a function turning the matched text into the signals `config/severity.yml` already classifies — the severity of a signature is not written next to it, it is whatever the classifier makes of its signals.

| Signature | Signal derived | Typical severity |
|---|---|---|
| `SIG-001-ROW-SHORTFALL` | `row_variance_pct` (read off the line) | P2 / P3 |
| `SIG-002-SCHEMA-DRIFT` | `job_failed_no_path_to_sla` | P1 |
| `SIG-003-NULL-SPIKE` | `null_rate_increase_pct` per column | P3 / P4 |
| `SIG-004-CONSUMER-LAG` | `sla_breach_projected` past the lag threshold | P2 |
| `SIG-005-CONTAINER-OOM` | `job_failed_no_path_to_sla` | P1 |
| `SIG-006-DISK-FULL` | `target_unavailable` | P1 |
| `SIG-007-MISSING-BLOCK` | `target_unavailable` | P1 |
| `SIG-008-CONNECTION-REFUSED` | `target_unavailable` | P1 |
| `SIG-009-JOB-FAILED` | `job_failed_no_path_to_sla` | P1 |
| `SIG-010-CONTROL-TOTAL` | `control_total_mismatch` | P1 |
| `SIG-011-SLOW-STAGE` | `job_duration_vs_baseline_pct` | P3 |
| `SIG-012-DOWNSTREAM-BLOCKED` | `downstream_jobs_blocked` (summed across hits) | P2 |
| `SIG-013-ZK-SESSION-EXPIRED` | `log_anomaly_no_data_impact` | P4 |
| `SIG-014-KERBEROS` | `log_anomaly_no_data_impact` | P4 |
| *(none matched)* | `log_anomaly_no_data_impact` | P4 |

Two hits of the same signature are not twice as bad — signals merge by taking the worse value, except blocked downstream jobs, which add up. A test renders every signature 25 times and asserts the analyser finds it and that its signals classify to a real severity: a failure mode the mixer can inject but the analyser cannot find would be the worst kind of bug here.

## Quick Start

```bash
pip install -r requirements.txt
python scripts/fetch_logs.py     # optional — real background logs
python scripts/logset.py         # mix, triage, alert, print the download path
```

No API key, no containers, no Slack workspace. The alert lands in `reports/slack/`, the log set and its zip in `reports/logsets/`.

## Demo: the incident lifecycle

`--lifecycle` walks the opened incident through the states a real one goes through, using the same functions the Slack buttons drive — acknowledged → approved (the gate every P1/P2 requires) → verifying → resolved, which is what produces MTTA and MTTR:

```bash
python scripts/logset.py --lifecycle
```

```
Incident  INC-20260920-0034  P1  — approval required
...
Lifecycle
          ACKNOWLEDGED by U_DEMO
          APPROVED by U_DEMO -> status=remediating
          VERIFYING
          RESOLVED -> MTTA=0s MTTR=0s
```

A batch of sessions, then the metrics across all of them:

```bash
python scripts/logset.py --sessions 5 --lifecycle
python scripts/incident_metrics.py
```

`incident_metrics.py` reports count by severity, median and p90 MTTA/MTTR, and the false-positive rate, read from `reports/incidents/*.json`. MTTA/MTTR read ~0s on a scripted walkthrough, for the obvious reason; a real incident's figures come from real Slack thread timestamps (`agent/incident.py::sync_slack_engagement`).

## Running Tests Locally

```bash
pip install -r requirements.txt
pytest tests/pytest/ -v          # 206 tests, nothing touches the network
pytest tests/pytest/test_logsets.py -v
```

## Project Structure

```
ai-qa-agent/
├── logsets/                # Log sources + error signatures (catalog.py), corpus
│                           #   fetch/fallback (corpus.py), per-session mixing
│                           #   (session.py), analysis → severity → incident →
│                           #   Slack (triage.py)
├── agent/                  # Severity classifier, incident record + lifecycle,
│                           #   Slack client/blocks/verify, runbook selection,
│                           #   and the HTTP server (agent.py)
├── config/                 # severity.yml — thresholds live here, never in code
├── schemas/                # incident.schema.json — the incident record's contract
├── scripts/                # logset.py (the CLI), fetch_logs.py, incident_metrics.py
├── tests/
│   ├── pytest/             # Unit/regression tests for every module above
│   └── fixtures/blocks/    # Golden-file Block Kit fixtures (agent/slack_blocks.py)
├── docs/                   # SLACK_SETUP.md, runbooks/, and the live demo page
├── reports/                # Session log sets + zips, persisted incidents,
│                           #   stub Slack payloads
└── .env.example
```

## Design Decisions

**Why does severity classification live outside a model?**
An LLM asked to both observe evidence and assign a severity label will occasionally assign different severities to identical evidence across runs, and there is no way to audit *why* short of re-reading its reasoning trace. `agent/severity.py` reads the same signals and applies the same YAML-configured thresholds every time — the same log set always produces the same severity, and `matched_conditions` names exactly which rule fired. The same argument applies to runbook selection (`agent/runbooks.py`) and to the approval gate: all three are code reading signals, not judgement calls.

**Why derive signals from log text rather than from the mixer?**
Because the mixer knows the answer and the agent must not. Everything the triage path concludes comes from regex matches against the log lines — the same lines an analyst tailing the file would see. The manifest's ground truth exists only to score detection afterwards, and a test asserts the analysis is identical when that ground truth is deleted.

**Why is Slack a view rather than the source of truth?**
The incident record on disk (`schemas/incident.schema.json`) is the system of record. Slack is where humans see it and act on it, so every Slack failure is logged loudly and never raised: a run that correctly opened an incident must not fail because Slack was unreachable. `post_incident()` writes the returned `ts`/`channel` back onto the incident and re-persists in the same call, so there is never a posted message with no record behind it.

**Why is the corpus fetched rather than vendored?**
The LogHub datasets are third-party research data. Fetching them at setup keeps the repository small and the provenance honest — and the generated fallback means a clone with no network still produces complete log sets, with each file's origin recorded in the manifest.

## Tech Stack

| Layer | Technology |
|---|---|
| Log corpus | [LogHub](https://github.com/logpai/loghub) public system logs, fetched at setup (`scripts/fetch_logs.py`), with a generated fallback |
| Log-set mixing & analysis | Python 3.11 (`logsets/`) — seeded mixing, regex signature catalogue |
| Severity classification | Deterministic Python, config-driven (`agent/severity.py` + `config/severity.yml`) |
| Incident record | JSON Schema-validated records on disk, full lifecycle + approval gate (`agent/incident.py`) |
| HTTP server | FastAPI — log-set endpoints and Slack's interactivity endpoint |
| Notifications | Slack bot-token app (`agent/slack_client.py`) — threaded, editable in place, Block Kit. `SLACK_MODE=stub` (default) writes payloads to `reports/slack/` with no live workspace required |
| Testing | pytest 8.x — 206 tests, no network, no services |
