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

A second path is also in the tree and tested: a Claude tool-use loop (`POST /agent/run`) over a mock Postgres/Kafka pipeline, orchestrated by n8n. It shares the same severity classifier, incident record and Slack layer.

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

Over HTTP, the same thing (`python agent/agent.py`, or `docker compose up agent_server`):

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

## The tool-use path

A Claude tool-use loop over the mock Postgres/Kafka pipeline, orchestrated by n8n — the second path through the same triage layer.

```
Kafka event / cron / webhook
         │
    [n8n Trigger]
         │
    [Triage Agent Node]  ◄── Claude API (tool-use mode) — reports signals, not severity
         │
    ┌────┴────────────────┐
    │                     │                      │
[SQL Validator]   [Log Analyser]   [Schema Comparator]
    │                     │                      │
    └────────────┬──────────────────────────────┘
                 │
       [Deterministic Severity Classifier]
       agent/severity.py + config/severity.yml
                 │
          ┌──────┴──────┐
      no incident    incident (P1–P4)
          │              │
  [Robot Framework]   [pytest]
  SLA / contract      Diagnostic deep-dive to
  verification;       isolate the failing
  re-run after         component
  remediation to
  confirm restoration
          │              │
          └──────┬───────┘
                 │
      [n8n Incident Record Builder]
                 │
     [Slack alert + Jenkins webhook]
```

## Agent Response Contract

`POST /agent/run` returns:

```json
{
  "run_id": "run-001",
  "incident": { "incident_id": "INC-20260913-0214", "severity": "P2", "...": "..." } | null,
  "clean": true,
  "checks_performed": ["recon", "logs", "schema"],
  "duration_ms": 842
}
```

`incident` is `null` and `clean` is `true` on a healthy run. Otherwise `incident` is a full record matching `schemas/incident.schema.json` — severity, a rationale naming every matched condition, business-terms impact summary, technical root cause, confidence, and whether it requires human approval before remediation (deterministic: any `P1`/`P2` does). See `agent/prompts.py` for exactly what the model is asked to report, and `agent/severity.py` for how that gets turned into a severity.

## n8n Workflow

The complete workflow lives in `n8n_workflows/qa_agent_workflow.json` — import it via **Settings → Import from file** in the n8n UI. (The filename is unchanged for now to avoid an unrelated rename churning the diff; nodes inside it are named per the current terminology.)

### Workflow Map

**n8n does not talk to Slack at all.** The Python
agent server posts incidents to Slack itself, right inside `/agent/run`,
and Slack's own interactivity Request URL points at a new endpoint on
that same server (`/slack/action`), not at n8n. Full rationale and the
detailed sequence diagrams live in [`docs/workflow-map.md`](docs/workflow-map.md); the summary:

```
① Pipeline Trigger      (Webhook — POST /pipeline-trigger)
         │
② Call Triage Agent     (HTTP Request → agent_server:8001/agent/run, 60s timeout)
         │              Claude tool-use loop + severity classification +
         │              posting the incident to Slack all happen here
         │
③ Check Incident Status (IF node — clean == true)
         │
    TRUE ┤                              FALSE
   clean │                            incident
         ▼                                 ▼
④ Run Robot Framework           ⑤ Run pytest
         │                                 │
⑥ Read RF Report                ⑦ Read pytest Report
         │                                 │
         └──────────────┬──────────────────┘
                        ▼
               ⑧ Merge Reports
                        │
          ⑨ Build Incident Record   (Code node — JS)
             Shapes the summary object for Jenkins/the caller
                        │
              ⑩ Jenkins Webhook
                        │
            ⑪ Respond to Webhook
```

Node-by-node detail (including the endpoint Slack's Interactivity Request
URL actually needs to point at) is in [`docs/workflow-map.md`](docs/workflow-map.md).

### Design Decisions

**Why clean → Robot Framework and incident → pytest?**
Robot Framework's keyword-driven syntax maps naturally to business-level pipeline contracts. `Logs Should Contain No Errors` and `Row Drop Should Be Under Threshold` are readable specifications, not code — and re-running the same suite after remediation is what actually confirms an incident is resolved, not just that a human believes it is. pytest provides fast, targeted unit-level feedback when something breaks: the diagnostic deep-dive that tells you exactly which transformation step failed, not just that the pipeline didn't pass an end-to-end check.

**Why `responseMode: responseNode`?**
Holding the webhook connection open means any caller — a Kafka consumer, a CI step, a `curl` command — receives the full incident record synchronously in a single HTTP call. No polling, no callback URL, no second request.

**Why does the Python agent post to Slack rather than n8n?**
The Slack Web API client, Block Kit builder and HMAC signature verifier are fully unit-tested Python, and `post_incident()` needs to mutate and re-persist the same `Incident` object it posts — something only the process that owns `agent/incident.py::persist` can do cleanly. Re-implementing Block Kit rendering and signature verification a second time in n8n's JS Code nodes would mean two implementations of the same logic, one of them untested in an environment with no running n8n instance to check JS against. See `docs/workflow-map.md` for the full reasoning and the resulting endpoint topology.

**Why does severity classification live outside the model?**
An LLM asked to both observe evidence and assign a severity label will occasionally assign different severities to identical evidence across runs, and there is no way to audit *why* short of re-reading its reasoning trace. `agent/severity.py` reads the same evidence and applies the same YAML-configured thresholds every time — the same input always produces the same severity, and `matched_conditions` names exactly why. The model's only job is to report what it actually observed accurately.

---

## Quick Start

```bash
pip install -r requirements.txt
python scripts/fetch_logs.py     # optional — real background logs
python scripts/logset.py         # mix, triage, alert, print the download path
```

That is the whole main path: no API key, no Docker, no Slack workspace. The alert lands in `reports/slack/` and the log set in `reports/logsets/`.

For the tool-use path (Claude + n8n + the mock pipeline) you need the stack:

```bash
cp .env.example .env
# Edit .env and add your ANTHROPIC_API_KEY

docker compose up
```

Then open n8n at http://localhost:5678 (admin/password) and import `n8n_workflows/qa_agent_workflow.json`.

## Demo: Trigger Failure Modes (mock pipeline)

```bash
# Clean run → no incident → Robot Framework
INJECT_FAILURE=none python mock_pipeline/producer.py

# Schema drift → incident (P1: load cannot complete without the column) → pytest
INJECT_FAILURE=schema_drift python mock_pipeline/producer.py

# Row drop → incident (P2: row variance over threshold) → pytest
INJECT_FAILURE=row_drop python mock_pipeline/producer.py

# Null spike → incident (P3: null-rate increase on a critical column) → pytest
INJECT_FAILURE=null_spike python mock_pipeline/producer.py

# Kafka latency → incident (P2: projected SLA breach) → pytest
INJECT_FAILURE=latency python mock_pipeline/producer.py
```

Severities above reflect `config/severity.yml`'s thresholds against each mode's characteristic signal — see `tests/pytest/test_agent_response.py` for the exact signal-to-severity mapping tested for each mode.

The commands above go through the full pipeline (Kafka → n8n → agent, requiring `ANTHROPIC_API_KEY` and the whole stack running). For a live demo or to verify a real Slack setup without any of that, use the demo scripts instead:

```bash
# One incident, opened and posted to Slack (or reports/slack/ if SLACK_MODE=stub)
scripts/demo_incident.py row_drop

# ...and walked through its full lifecycle: acknowledged -> approved ->
# remediating -> verifying -> resolved, updating the Slack parent message
# in place at each step
scripts/demo_incident.py schema_drift --lifecycle --actor U_ONCALL

# Every failure mode, full lifecycle, plus the resulting metrics report --
# a one-command walkthrough for a live demo
scripts/demo_all.sh
```

These call exactly the same code the real agent uses (`agent.agent.build_response`/`notify_slack`, `agent.incident.record_approval_decision`/`resolve_incident`) against a synthetic model output shaped like what a compliant Claude call would produce — no `ANTHROPIC_API_KEY`, no tool server, no n8n required. `SLACK_MODE` (stub by default) works exactly as it does everywhere else in this repo: unset/`stub` previews locally with zero setup, `live` with a populated `.env` narrates into a real workspace.

## Running Tests Locally

```bash
pip install -r requirements.txt

# pytest suite (no services required — 297 tests, nothing touches the network)
pytest tests/pytest/ -v

# just the log-set path
pytest tests/pytest/test_logsets.py -v

# Robot Framework (requires tool server running)
TOOL_SERVER_HOST=localhost python agent_tools/tool_server.py &
robot --outputdir reports/robot tests/robot/acceptance.robot
```

## Project Structure

```
ai-qa-agent/
├── logsets/                # THE MAIN PATH — log sources + error signatures (catalog.py),
│                           #   corpus fetch/fallback (corpus.py), per-session mixing
│                           #   (session.py), analysis → severity → incident → Slack (triage.py)
├── mock_pipeline/          # Simulated pipeline + failure injection (tool-use path)
├── agent_tools/            # SQL validator, log analyser, schema comparator + FastAPI server
├── agent/                  # Claude tool-use loop, severity classifier, incident records, Slack client/blocks/verify, runbook selection, evidence bundle
├── config/                 # severity.yml — thresholds live here, never in code
├── schemas/                # incident.schema.json — the incident record's JSON Schema
├── scripts/                # logset.py, fetch_logs.py, first-15-minutes.sh,
│                           #   incident_metrics.py, demo_incident.py, demo_all.sh
├── tests/
│   ├── pytest/             # Unit/regression tests for every module above
│   ├── fixtures/blocks/    # Golden-file Block Kit fixtures (agent/slack_blocks.py)
│   └── robot/              # Keyword-driven E2E validation checks
├── n8n_workflows/          # Importable n8n workflow JSON (Slack posting lives in agent/, not here — see docs/workflow-map.md)
├── docs/                   # workflow-map.md, SLACK_SETUP.md, runbooks/ + the live demo page
├── reports/                # Session log sets + their zips, persisted incidents, evidence
│                           #   bundles, stub Slack payloads, test output
├── docker-compose.yml
└── .env.example
```

## Failure Modes (mock pipeline)

| Mode | Description | Failing Tool(s) | Signal reported | Resulting severity |
|---|---|---|---|---|
| `none` | Clean run | — | all clean | no incident |
| `row_drop` | Target has 40% fewer rows | SQL Validator | `row_variance_pct: 40.0` | P2 |
| `schema_drift` | `account_balance` renamed to `bal` | Schema Comparator | `job_failed_no_path_to_sla: true` | P1 |
| `null_spike` | `customer_id` null rate → 35% | SQL Validator + Log Analyser | `null_rate_increase_pct: {customer_id: 35.0}` | P3 |
| `latency` | Kafka consumer lag > 10,000 msgs | Log Analyser | `sla_breach_projected: true` | P2 |

## Tech Stack

| Layer | Technology |
|---|---|
| Log corpus | [LogHub](https://github.com/logpai/loghub) public system logs, fetched at setup (`scripts/fetch_logs.py`), with a generated fallback |
| Log-set mixing & analysis | Python 3.11 (`logsets/`) — seeded mixing, regex signature catalogue, no model in the loop |
| Workflow orchestration | n8n (self-hosted via Docker) — tool-use path |
| LLM agent | Claude API, tool-use mode — reports signals, not severity |
| Severity classification | Deterministic Python, config-driven (`agent/severity.py` + `config/severity.yml`) |
| Tool API server | Python 3.11 + FastAPI |
| Acceptance / restoration validation | Robot Framework 7.x |
| Diagnostic / unit testing | pytest 8.x |
| Mock pipeline | Python + kafka-python |
| Database | PostgreSQL 15 — provisioned in `docker-compose.yml` for the tool-use path |
| Containerisation | Docker + Docker Compose |
| CI integration | Jenkins webhook |
| Notifications | Slack bot-token app (`agent/slack_client.py`), posted directly by the Python agent — threaded, editable in place. `SLACK_MODE=stub` (default) writes payloads to `reports/slack/` with no live workspace required. |
