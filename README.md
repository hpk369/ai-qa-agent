# ETL Production Support Triage Agent

**[▶ Live Demo](https://hpk369.github.io/ai-qa-agent/)** — interactive pipeline simulator, no setup required. (The demo's narrative script still speaks the pre-triage `verdict`/PASS-FAIL contract described in [Phase status](#phase-status) below — see that section before drawing conclusions about the current response shape from the demo alone.)

When a production ETL job breaks, the question that matters isn't "did the pipeline pass or fail" — it's "what's the severity, who's affected, and what do I do in the first fifteen minutes." This project is an AI-assisted triage agent, orchestrated via **n8n**, that watches a Big Data pipeline, classifies the severity of what it finds using a deterministic, config-driven ruleset, and opens a structured incident record rather than a pass/fail verdict. Claude (in tool-use mode) reports the evidence; a plain Python module decides how serious it is, so the same evidence always yields the same call. The result routes to the appropriate validation framework — Robot Framework to confirm restoration, pytest to isolate a root cause — the same way an on-call analyst would triage, escalate, and verify a fix.

This repository is mid-migration from an earlier "AI QA pipeline" demo into this triage-focused, Hadoop-stack-aligned system. See [`expansion-plan.md`](expansion-plan.md) for the rationale and [`IMPLEMENTATION.md`](IMPLEMENTATION.md) for the task-by-task build spec being executed against this codebase; [`docs/INVENTORY.md`](docs/INVENTORY.md) is a from-source inventory of the codebase as Phase 0 began.

## Phase status

**Phase 0 (triage reframe, no infrastructure changes) is complete.** The pipeline still runs against the original Postgres/Kafka mock stack described below — the Hadoop stack (HDFS, YARN, Hive, Spark-on-YARN) in `expansion-plan.md` Track B has **not been built yet**; nothing in this README should be read as claiming it has. What has changed in Phase 0:

- The agent no longer returns a `PASS`/`FAIL` verdict. It reports observed signals; `agent/severity.py` classifies those signals into a severity (`P1`–`P4`, or no incident) using thresholds in `config/severity.yml` — never hard-coded, never decided by the model itself.
- Every non-clean run opens a structured **incident record** (`agent/incident.py`, schema at `schemas/incident.schema.json`), persisted to `reports/incidents/<incident_id>.json` with a markdown sidecar. This is now the system of record; a future Slack integration (`expansion-plan.md` §4.A6, not yet built) will be a *view* onto it, not the other way around.
- `/agent/run` returns `{run_id, incident, clean, checks_performed, duration_ms}` — see [Agent Response Contract](#agent-response-contract).
- The n8n workflow, README, and code comments have been swept for the old QA-pipeline terminology (`docs/app.py`'s demo server is the one deliberate exception — see the note in that file).

The demo server (`docs/app.py`) and the static GitHub Pages front end (`docs/index.html`, `docs/index_v2.html`) still narrate the **old** `verdict` contract — rewriting them is explicitly deferred to the end of the roadmap (`IMPLEMENTATION.md` Phase 5), once the system's shape has stopped changing, rather than rewritten twice.

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

```
① Pipeline Trigger      (Webhook — POST /pipeline-trigger)
         │
② Call Triage Agent     (HTTP Request → agent_server:8001/agent/run, 60s timeout)
         │              Claude multi-turn tool-use loop runs here
         │
③ Check Incident Status (IF node — clean == true)
         │
    TRUE ┤                              FALSE
   clean │                            incident
         ▼                                 ▼
④ Run Robot Framework           ⑤ Run pytest
  tests/robot/                     tests/pytest/ --junitxml
         │                                 │
⑥ Read RF Report                ⑦ Read pytest Report
  reports/robot/output.xml         reports/pytest/results.xml
         │                                 │
         └──────────────┬──────────────────┘
                        ▼
               ⑧ Merge Reports    (converges both branches)
                        │
          ⑨ Build Incident Record   (Code node — JS)
             Formats summary object + Slack mrkdwn
                        │
          ┌─────────────┴──────────────┐
          ▼                            ▼
  ⑩ Slack Alert              ⑪ Jenkins Webhook
  POST to #etl-prod-alerts      POST summary JSON to CI
          │                            │
          └─────────────┬──────────────┘
                        ▼
            ⑫ Respond to Webhook
            Returns summary JSON to caller
```

### Node Reference

| # | Node | n8n Type | Key Config | Purpose |
|---|------|----------|------------|---------|
| 1 | **Pipeline Trigger** | Webhook | `POST /pipeline-trigger`, `responseMode: responseNode` | Entry point. Holds the HTTP connection open until node ⑫ fires — the caller receives the incident record synchronously. Accepts Kafka event JSON or a manual `curl`. |
| 2 | **Call Triage Agent** | HTTP Request | `POST agent_server:8001/agent/run`, timeout 60 s | Hands off to the Claude agent. The 60 s timeout covers the full multi-turn loop: 3 tool calls + signal reporting. Maps all Pipeline Trigger fields into the request body. |
| 3 | **Check Incident Status** | IF | `$json.clean == true` | Central routing decision. True branch (no incident) → SLA/contract verification. False branch (incident opened) → diagnostic deep-dive. Routing off a deterministic severity call, not a model-decided label, is the core architectural change from the original QA-pipeline design. |
| 4 | **Run Robot Framework** | Execute Command | `robot --outputdir reports/robot tests/robot/acceptance.robot` | Runs on a clean run, and again after remediation to confirm restoration. RF's keyword-driven syntax maps to business-level pipeline contracts ("Row Drop Should Be Under Threshold"). Readable by non-developers. |
| 5 | **Run pytest** | Execute Command | `pytest tests/pytest/ --junitxml=reports/pytest/results.xml -v` | Runs when an incident opens. Provides fast, precise unit-level feedback for exactly which component broke — the diagnostic deep-dive that isolates the failing component. JUnit XML integrates natively with Jenkins. |
| 6 | **Read RF Report** | Read Binary File | `/qa/reports/robot/output.xml` | Loads the Robot Framework XML execution tree for downstream parsing. |
| 7 | **Read pytest Report** | Read Binary File | `/qa/reports/pytest/results.xml` | Loads the JUnit XML. Standard schema parsed by Jenkins, GitHub Actions, or any CI system. |
| 8 | **Merge Reports** | Merge | `mergeByPosition` | Converges the two branches. Since only one branch runs per execution, this is a pass-through — but n8n requires explicit convergence to provide a single downstream connection. |
| 9 | **Build Incident Record** | Code (JS) | See code below | The only custom code in the workflow. Cross-references `$('Call Triage Agent')` and `$('Pipeline Trigger')` by name. Builds the `summary` object and formats the Slack `mrkdwn` message from `incident`/`clean`, not a `verdict` string. |
| 10 | **Slack Alert** | Slack | `$env.SLACK_WEBHOOK_URL`, username: `Triage Agent` | Posts `slack_message` to `#etl-prod-alerts` in mrkdwn format. **Still an incoming webhook** — `expansion-plan.md` §4.A6 calls for migrating to a bot-token Slack app so incidents can thread and update in place; that migration is Phase 1, not yet done. |
| 11 | **Jenkins Webhook** | HTTP Request | `POST $env.JENKINS_WEBHOOK_URL`, token: `$env.JENKINS_TOKEN` | Fires a downstream Jenkins job with the full `summary` JSON as payload. Decoupled from Slack — either can fail independently without blocking the other. |
| 12 | **Respond to Webhook** | Respond to Webhook | `respondWith: json` | Closes the HTTP connection opened by node ①. Returns the full `summary` JSON synchronously. CI systems can gate deployments on this response without polling. |

### Build Incident Record — Code Node

The only custom JavaScript in the entire workflow. It uses n8n's `$('node name')` cross-reference syntax to reach back to any earlier node by name:

```javascript
const agentResult = $('Call Triage Agent').first().json;

const incident = agentResult.incident;
const clean = agentResult.clean === true;

const severityEmoji = { P1: '🔴', P2: '🟠', P3: '🟡', P4: '⚪' };
const emoji = clean ? '✅' : (severityEmoji[incident && incident.severity] || '❗');

const summary = {
  run_id: $('Pipeline Trigger').first().json.run_id,
  clean,
  incident_id: incident ? incident.incident_id : null,
  severity: incident ? incident.severity : null,
  impact_summary: incident ? incident.impact_summary : 'No issues detected.',
  root_cause: incident ? incident.root_cause : null,
  recommended_action: incident ? incident.recommended_action : null,
  confidence: incident ? incident.confidence : null,
  checks_performed: agentResult.checks_performed || [],
  requires_approval: incident ? incident.requires_approval : false,
  runbook: incident ? incident.runbook : null,
  validation_framework: clean ? 'Robot Framework' : 'pytest',
  timestamp: new Date().toISOString(),
};

const slack_message = [
  `${emoji} *ETL Production Support* — ${clean ? 'No Incident' : `Incident ${summary.incident_id} (${summary.severity})`}`,
  `*Run ID:* ${summary.run_id}`,
  `*Impact:* ${summary.impact_summary}`,
  summary.root_cause ? `*Root Cause:* ${summary.root_cause}` : '',
  summary.recommended_action ? `*Recommended Action:* ${summary.recommended_action}` : '',
  summary.runbook ? `*Runbook:* ${summary.runbook}` : '',
  `*Confidence:* ${summary.confidence != null ? (summary.confidence * 100).toFixed(0) + '%' : 'n/a'}`,
  `*Validation via:* ${summary.validation_framework}`,
].filter(Boolean).join('\n');

return [{ json: { summary, slack_message, clean } }];
```

### Design Decisions

**Why clean → Robot Framework and incident → pytest?**
Robot Framework's keyword-driven syntax maps naturally to business-level pipeline contracts. `Logs Should Contain No Errors` and `Row Drop Should Be Under Threshold` are readable specifications, not code — and re-running the same suite after remediation is what actually confirms an incident is resolved, not just that a human believes it is. pytest provides fast, targeted unit-level feedback when something breaks: the diagnostic deep-dive that tells you exactly which transformation step failed, not just that the pipeline didn't pass an end-to-end check. This is the same split the project started with; Phase 0 sharpened *why* each side runs rather than changing which side runs when.

**Why `responseMode: responseNode`?**
Holding the webhook connection open means any caller — a Kafka consumer, a CI step, a `curl` command — receives the full incident record synchronously in a single HTTP call. No polling, no callback URL, no second request.

**Why a JS Code node instead of more HTTP/Set nodes?**
The Slack message requires conditional formatting: picking a severity emoji, omitting fields that don't apply to a clean run, joining bullet points. That logic in n8n expression syntax would require chaining several Function/Set nodes. Readable JavaScript is strictly better.

**Why does severity classification live outside the model?**
An LLM asked to both observe evidence and assign a severity label will occasionally assign different severities to identical evidence across runs, and there is no way to audit *why* short of re-reading its reasoning trace. `agent/severity.py` reads the same evidence and applies the same YAML-configured thresholds every time — the same input always produces the same severity, and `matched_conditions` names exactly why. The model's only job is to report what it actually observed accurately.

---

## Quick Start

```bash
cp .env.example .env
# Edit .env and add your ANTHROPIC_API_KEY

docker compose up
```

Then open n8n at http://localhost:5678 (admin/password) and import `n8n_workflows/qa_agent_workflow.json`.

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
├── agent/                  # Claude tool-use loop, deterministic severity classifier, incident records
├── config/                 # severity.yml — thresholds live here, never in code
├── schemas/                # incident.schema.json — the incident record's JSON Schema
├── tests/
│   ├── pytest/             # Unit/regression tests for tools, severity, incidents, and agent response building
│   └── robot/              # Keyword-driven E2E validation checks
├── n8n_workflows/          # Importable n8n workflow JSON
├── docs/                   # INVENTORY.md (from-source repo inventory) + the live demo (pre-triage contract; see Phase status)
├── reports/                # Test output + persisted incident records
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
| Notifications | Slack incoming webhook via n8n — bot-token migration for threaded incidents planned, not yet built (`expansion-plan.md` §4.A6) |

A Hadoop-aligned stack (HDFS, YARN, Hive, Spark-on-YARN, and an honestly-scoped Oozie/Impala substitution) is planned in `expansion-plan.md` Track B and `IMPLEMENTATION.md` Phase 2 onward. Nothing above should be read as claiming that stack exists in this repository yet.
