# ETL Production Support Triage Agent

**[▶ Live Demo](https://hpk369.github.io/ai-qa-agent/)** — interactive pipeline simulator, no setup required. Walks through severity classification, an incident record, and a Slack Block Kit preview with working Approve/Reject/Escalate buttons — see [demo pages](#demo-pages) below for exactly what is and isn't live in it.

When a production ETL job breaks, the question that matters isn't "did the pipeline pass or fail" — it's "what's the severity, who's affected, and what do I do in the first fifteen minutes." This project is an AI-assisted triage agent, orchestrated via **n8n**, that watches a Big Data pipeline, classifies the severity of what it finds using a deterministic, config-driven ruleset, and opens a structured incident record rather than a pass/fail verdict. Claude (in tool-use mode) reports the evidence; a plain Python module decides how serious it is, so the same evidence always yields the same call. The result routes to the appropriate validation framework — Robot Framework to confirm restoration, pytest to isolate a root cause — the same way an on-call analyst would triage, escalate, and verify a fix.

This repository is mid-migration from an earlier "AI QA pipeline" demo into this triage-focused, Hadoop-stack-aligned system. See [`expansion-plan.md`](expansion-plan.md) for the rationale and [`IMPLEMENTATION.md`](IMPLEMENTATION.md) for the task-by-task build spec being executed against this codebase; [`docs/INVENTORY.md`](docs/INVENTORY.md) is a from-source inventory of the codebase as Phase 0 began.

## Phase status

**Phase 0 (triage reframe) and Phase 1 (Slack incident channel, runbooks, evidence, MTTA/MTTR) are both complete.** The pipeline still runs against the original Postgres/Kafka mock stack described below — the Hadoop stack (HDFS, YARN, Hive, Spark-on-YARN) in `expansion-plan.md` Track B has **not been built yet**; nothing in this README should be read as claiming it has.

Phase 0: the agent reports signals rather than a verdict; `agent/severity.py` + `config/severity.yml` classify severity deterministically; every non-clean run opens a structured **incident record** (`agent/incident.py`), the system of record; `/agent/run` returns `{run_id, incident, clean, checks_performed, duration_ms}` (see [Agent Response Contract](#agent-response-contract)); terminology swept throughout (`docs/app.py`'s demo server is the one deliberate exception — see [demo pages](#demo-pages) below).

Phase 1: `agent/slack_client.py` (bot-token Slack Web API client), `agent/slack_blocks.py` (Block Kit incident messages, golden-file tested), `agent/slack_verify.py` (HMAC request verification) — the agent server posts every incident to Slack itself and exposes `/slack/action` for button interactivity, not n8n (see [`docs/workflow-map.md`](docs/workflow-map.md) for why). `agent/incident.py::record_approval_decision` is the full Approve/Reject/Escalate gate (a second decision on an already-decided incident is rejected and reported in-thread, never silently ignored). `agent/runbooks.py` deterministically links every incident to one of five runbooks (`docs/runbooks/`), each with real, runnable diagnostic commands. `agent/evidence.py` collects an evidence bundle on every incident open, and `scripts/first-15-minutes.sh` is a standalone (no Python) equivalent for an on-call engineer working directly on a broken machine — actually run against this repo's own sandbox to confirm it degrades gracefully with no Docker/Kafka/tool-server running. `agent/incident.py::resolve_incident` syncs real Slack thread replies/reactions for MTTA, computes MTTR, and updates the Slack parent message to show RESOLVED with both figures; `scripts/incident_metrics.py` reports count-by-severity, median/p90 MTTA/MTTR, and the false-positive rate from `reports/incidents/*.json`.

**None of this has run against a live Slack workspace** — this environment has none of Phase 1's human prerequisites (a dedicated Slack workspace/app/bot token/channels/tunnel). `SLACK_MODE=stub` (the default) is exercised throughout instead, writing every payload Slack would have received to `reports/slack/` with no network call, including a full open→acknowledged→approved→remediating→verifying→resolved lifecycle demonstration run end-to-end against the real code. Set `SLACK_MODE=live` and populate `.env` once those prerequisites exist — see [`docs/PHASE1_SETUP.md`](docs/PHASE1_SETUP.md) for exactly how to get each one.

<a id="demo-pages"></a>**Demo pages.** `docs/index.html` — what actually renders at the Live Demo link above — was rewritten ahead of `IMPLEMENTATION.md`'s Phase 5 to reflect Phases 0–1 for real: it's a client-side simulation of `agent/severity.py`'s and `agent/slack_blocks.py`'s actual output for each failure mode (the exact values `tests/pytest/test_agent_response.py` pins), including a working Approve/Reject/Escalate flow against a mocked Slack thread. It needs no backend, so it works unmodified on GitHub Pages; the Claude tool-use call itself is not live in it. `docs/index_v2.html` (an older, unlinked visual redesign of the pre-triage demo) was deleted rather than carried forward. `docs/app.py` — a local-only SSE demo server, not what GitHub Pages serves — is the one piece still on the pre-T0.4 `verdict` contract; see the note at the top of that file.

## Architecture

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

`POST /agent/run` now returns:

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

**As of Phase 1 (T1.4), n8n no longer talks to Slack at all.** The Python
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
Robot Framework's keyword-driven syntax maps naturally to business-level pipeline contracts. `Logs Should Contain No Errors` and `Row Drop Should Be Under Threshold` are readable specifications, not code — and re-running the same suite after remediation is what actually confirms an incident is resolved, not just that a human believes it is. pytest provides fast, targeted unit-level feedback when something breaks: the diagnostic deep-dive that tells you exactly which transformation step failed, not just that the pipeline didn't pass an end-to-end check. This is the same split the project started with; Phase 0 sharpened *why* each side runs rather than changing which side runs when.

**Why `responseMode: responseNode`?**
Holding the webhook connection open means any caller — a Kafka consumer, a CI step, a `curl` command — receives the full incident record synchronously in a single HTTP call. No polling, no callback URL, no second request.

**Why did Slack posting move out of n8n and into the Python agent?**
T1.1–T1.3 built a fully unit-tested Slack Web API client, Block Kit builder, and HMAC signature verifier in Python, and `post_incident()` needs to mutate and re-persist the same `Incident` object it posts — something only the process that owns `agent/incident.py::persist` can do cleanly. Re-implementing Block Kit rendering and signature verification a second time in n8n's JS Code nodes would mean two implementations of the same logic, one of them untested in an environment with no running n8n instance to check JS against. See `docs/workflow-map.md` for the full reasoning and the resulting endpoint topology.

**Why does severity classification live outside the model?**
An LLM asked to both observe evidence and assign a severity label will occasionally assign different severities to identical evidence across runs, and there is no way to audit *why* short of re-reading its reasoning trace. `agent/severity.py` reads the same evidence and applies the same YAML-configured thresholds every time — the same input always produces the same severity, and `matched_conditions` names exactly why. The model's only job is to report what it actually observed accurately.

---

## Quick Start

```bash
cp .env.example .env
# Edit .env and add your ANTHROPIC_API_KEY

docker compose --profile lite up
```

Then open n8n at http://localhost:5678 (admin/password) and import `n8n_workflows/qa_agent_workflow.json`.

**`--profile lite` is required** as of the Phase 2 compose split — `docker-compose.yml` now also defines a `hadoop` profile (HDFS/YARN/Hive, landing in `T2.2`–`T2.4`; not a complete stack yet, see [`docs/hadoop-stack.md`](docs/hadoop-stack.md) once it exists), and every service belongs to one or both profiles. A bare `docker compose up` with no `--profile` flag now starts nothing.

## Demo: Trigger Failure Modes

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

Severities above reflect `config/severity.yml`'s current thresholds against each mode's characteristic signal — see `tests/pytest/test_agent_response.py` for the exact signal-to-severity mapping tested for each mode, and `expansion-plan.md` §4.A1 for the rationale behind the thresholds themselves.

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

# pytest suite (no services required — uses mock data)
pytest tests/pytest/ -v

# Robot Framework (requires tool server running)
TOOL_SERVER_HOST=localhost python agent_tools/tool_server.py &
robot --outputdir reports/robot tests/robot/acceptance.robot
```

## Project Structure

```
ai-qa-agent/
├── expansion-plan.md       # Strategy: why this project is being repositioned, and how
├── IMPLEMENTATION.md       # Task-by-task build spec (source of truth for what's built and in what order)
├── mock_pipeline/          # Simulated Big Data pipeline + failure injection
├── agent_tools/            # SQL validator, log analyser, schema comparator + FastAPI server
├── agent/                  # Claude tool-use loop, severity classifier, incident records, Slack client/blocks/verify, runbook selection, evidence bundle
├── config/                 # severity.yml — thresholds live here, never in code
├── schemas/                # incident.schema.json — the incident record's JSON Schema
├── scripts/                # first-15-minutes.sh, incident_metrics.py, demo_incident.py, demo_all.sh
├── tests/
│   ├── pytest/             # Unit/regression tests for every module above
│   ├── fixtures/blocks/    # Golden-file Block Kit fixtures (agent/slack_blocks.py)
│   └── robot/              # Keyword-driven E2E validation checks
├── n8n_workflows/          # Importable n8n workflow JSON (Slack posting lives in agent/, not here — see docs/workflow-map.md)
├── docs/                   # INVENTORY.md, workflow-map.md, runbooks/ + the live demo (pre-triage contract; see Phase status)
├── reports/                # Test output, persisted incidents, evidence bundles, stub Slack payloads
├── docker-compose.yml
└── .env.example
```

## Failure Modes

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
| Workflow orchestration | n8n (self-hosted via Docker) |
| LLM agent | Claude API, tool-use mode — reports signals, not severity |
| Severity classification | Deterministic Python, config-driven (`agent/severity.py` + `config/severity.yml`) |
| Tool API server | Python 3.11 + FastAPI |
| Acceptance / restoration validation | Robot Framework 7.x |
| Diagnostic / unit testing | pytest 8.x |
| Mock pipeline | Python + kafka-python |
| Database | PostgreSQL 15 — provisioned in `docker-compose.yml`; see `docs/INVENTORY.md` §8 for the current gap between that and what the tools actually query in mock mode |
| Containerisation | Docker + Docker Compose |
| CI integration | Jenkins webhook |
| Notifications | Slack bot-token app (`agent/slack_client.py`), posted directly by the Python agent — threaded, editable in place. `SLACK_MODE=stub` (default) writes payloads to `reports/slack/` with no live workspace required; see [Phase status](#phase-status). |

A Hadoop-aligned stack (HDFS, YARN, Hive, Spark-on-YARN, and an honestly-scoped Oozie/Impala substitution) is planned in `expansion-plan.md` Track B and `IMPLEMENTATION.md` Phase 2 onward. Nothing above should be read as claiming that stack exists in this repository yet.
