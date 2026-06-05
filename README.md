# AI Agent QA Pipeline: n8n + Robot Framework + pytest

**[▶ Live Demo](https://hpk369.github.io/ai-qa-agent/)** — interactive pipeline simulator, no setup required.

An AI-powered quality assurance agent orchestrated via **n8n** that monitors a mock Big Data pipeline, reasons over tool outputs using Claude in tool-use mode, and routes to the appropriate test framework based on its verdict.

## Architecture

```
Kafka event / cron / webhook
         │
    [n8n Trigger]
         │
    [AI Agent Node]  ◄── Claude API (tool-use mode)
         │
    ┌────┴────────────────┐
    │                     │                      │
[SQL Validator]   [Log Analyser]   [Schema Comparator]
    │                     │                      │
    └────────────┬──────────────────────────────┘
                 │
         [LLM Synthesis]
         Root cause + verdict
                 │
          ┌──────┴──────┐
         PASS          FAIL
          │              │
  [Robot Framework]   [pytest]
  E2E acceptance      Regression + unit
          │              │
          └──────┬───────┘
                 │
      [n8n Report Aggregator]
                 │
     [Slack alert + Jenkins webhook]
```

## n8n Workflow

The complete workflow lives in `n8n_workflows/qa_agent_workflow.json` — import it via **Settings → Import from file** in the n8n UI.

### Workflow Map

```
① Pipeline Trigger  (Webhook — POST /pipeline-trigger)
         │
② Call QA Agent     (HTTP Request → agent_server:8001/agent/run, 60s timeout)
         │          Claude multi-turn tool-use loop runs here
         │
③ Check Verdict     (IF node — verdict == "PASS")
         │
    TRUE ┤                              FALSE
    PASS │                               FAIL
         ▼                                 ▼
④ Run Robot Framework           ⑤ Run pytest
  robot tests/robot/               pytest tests/pytest/ --junitxml
         │                                 │
⑥ Read RF Report                ⑦ Read pytest Report
  reports/robot/output.xml         reports/pytest/results.xml
         │                                 │
         └──────────────┬──────────────────┘
                        ▼
               ⑧ Merge Reports    (converges both branches)
                        │
             ⑨ Build QA Summary   (Code node — JS)
               Formats summary object + Slack mrkdwn
                        │
          ┌─────────────┴──────────────┐
          ▼                            ▼
  ⑩ Slack Alert              ⑪ Jenkins Webhook
  POST to #qa-alerts            POST summary JSON to CI
          │                            │
          └─────────────┬──────────────┘
                        ▼
            ⑫ Respond to Webhook
            Returns summary JSON to caller
```

### Node Reference

| # | Node | n8n Type | Key Config | Purpose |
|---|------|----------|------------|---------|
| 1 | **Pipeline Trigger** | Webhook | `POST /pipeline-trigger`, `responseMode: responseNode` | Entry point. Holds the HTTP connection open until node ⑫ fires — the caller receives the verdict synchronously. Accepts Kafka event JSON or a manual `curl`. |
| 2 | **Call QA Agent** | HTTP Request | `POST agent_server:8001/agent/run`, timeout 60 s | Hands off to the Claude agent. The 60 s timeout covers the full multi-turn loop: 3 tool calls + LLM synthesis. Maps all Pipeline Trigger fields into the request body. |
| 3 | **Check Verdict** | IF | `$json.verdict == "PASS"` | Central routing decision. True branch → acceptance tests. False branch → regression tests. Choosing the right framework based on AI verdict is the core architectural idea. |
| 4 | **Run Robot Framework** | Execute Command | `robot --outputdir reports/robot tests/robot/acceptance.robot` | Runs only on PASS. RF's keyword-driven syntax maps to business-level pipeline contracts ("Row Drop Should Be Under Threshold"). Readable by non-developers. |
| 5 | **Run pytest** | Execute Command | `pytest tests/pytest/ --junitxml=reports/pytest/results.xml -v` | Runs only on FAIL. Provides fast, precise unit-level feedback for exactly which component broke. JUnit XML integrates natively with Jenkins. |
| 6 | **Read RF Report** | Read Binary File | `/qa/reports/robot/output.xml` | Loads the Robot Framework XML execution tree for downstream parsing. Reading XML (not HTML) means the Code node can extract counts programmatically. |
| 7 | **Read pytest Report** | Read Binary File | `/qa/reports/pytest/results.xml` | Loads the JUnit XML. Standard schema parsed by Jenkins, GitHub Actions, or any CI system. |
| 8 | **Merge Reports** | Merge | `mergeByPosition` | Converges the two branches. Since only one branch runs per execution, this is a pass-through — but n8n requires explicit convergence to provide a single downstream connection. |
| 9 | **Build QA Summary** | Code (JS) | See code below | The only custom code in the workflow. Cross-references `$('Call QA Agent')` and `$('Pipeline Trigger')` by name. Builds the `summary` object and formats the Slack `mrkdwn` message. |
| 10 | **Slack Alert** | Slack | `$env.SLACK_WEBHOOK_URL`, username: `QA Agent` | Posts `slack_message` to `#qa-alerts` in mrkdwn format — bold fields, bullet points, pass/fail emoji. Credential kept in env var, not in workflow JSON. |
| 11 | **Jenkins Webhook** | HTTP Request | `POST $env.JENKINS_WEBHOOK_URL`, token: `$env.JENKINS_TOKEN` | Fires a downstream Jenkins job with the full `summary` JSON as payload. Decoupled from Slack — either can fail independently without blocking the other. |
| 12 | **Respond to Webhook** | Respond to Webhook | `respondWith: json` | Closes the HTTP connection opened by node ①. Returns the full `summary` JSON synchronously. CI systems can gate deployments on this response without polling. |

### Build QA Summary — Code Node

The only custom JavaScript in the entire workflow. It uses n8n's `$('node name')` cross-reference syntax to reach back to any earlier node by name:

```javascript
const agentResult = $('Call QA Agent').first().json;
const isPass = agentResult.verdict === 'PASS';

const summary = {
  run_id:             $('Pipeline Trigger').first().json.run_id,
  verdict:            agentResult.verdict,
  root_cause:         agentResult.root_cause,
  details:            agentResult.details || [],
  recommended_action: agentResult.recommended_action,
  confidence:         agentResult.confidence,
  test_framework:     isPass ? 'Robot Framework' : 'pytest',
  timestamp:          new Date().toISOString(),
};

const slack_message = [
  `${isPass ? '✅' : '❌'} *QA Pipeline Report* — ${agentResult.verdict}`,
  `*Run ID:* ${summary.run_id}`,
  `*Root Cause:* ${summary.root_cause}`,
  summary.details.length > 0
    ? `*Issues:*\n${summary.details.map(d => `• ${d}`).join('\n')}`
    : '',
  `*Action:* ${summary.recommended_action}`,
  `*Confidence:* ${(summary.confidence * 100).toFixed(0)}%`,
  `*Tests run via:* ${summary.test_framework}`,
].filter(Boolean).join('\n');

return [{ json: { summary, slack_message, verdict: agentResult.verdict } }];
```

### Design Decisions

**Why PASS → Robot Framework and FAIL → pytest?**
Robot Framework's keyword-driven syntax maps naturally to business-level pipeline contracts. `Logs Should Contain No Errors` and `Row Drop Should Be Under Threshold` are readable specifications, not code. pytest provides fast, targeted unit-level feedback when something breaks — you want to know exactly which transformation step failed, not just that the pipeline didn't pass an E2E check.

**Why `responseMode: responseNode`?**
Holding the webhook connection open means any caller — a Kafka consumer, a CI step, a `curl` command — receives the full verdict synchronously in a single HTTP call. No polling, no callback URL, no second request.

**Why a JS Code node instead of more HTTP/Set nodes?**
The Slack message requires conditional formatting: filtering empty detail arrays, joining bullet points, choosing emoji. That logic in n8n expression syntax would require chaining five or six Function/Set nodes. Thirty lines of readable JavaScript is strictly better.

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
# Clean run → PASS → Robot Framework
INJECT_FAILURE=none python mock_pipeline/producer.py

# Schema drift → FAIL → pytest
INJECT_FAILURE=schema_drift python mock_pipeline/producer.py

# Row drop → FAIL → pytest
INJECT_FAILURE=row_drop python mock_pipeline/producer.py

# Null spike → FAIL → pytest
INJECT_FAILURE=null_spike python mock_pipeline/producer.py

# Kafka latency → FAIL → pytest
INJECT_FAILURE=latency python mock_pipeline/producer.py
```

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
├── mock_pipeline/          # Simulated Big Data pipeline + failure injection
├── agent_tools/            # SQL validator, log analyser, schema comparator + FastAPI server
├── agent/                  # Claude API tool-use loop + FastAPI endpoint
├── tests/
│   ├── pytest/             # Unit/regression tests for all tools
│   └── robot/              # Keyword-driven E2E acceptance tests
├── n8n_workflows/          # Importable n8n workflow JSON
├── reports/                # Test output directory
├── docker-compose.yml
└── .env.example
```

## Failure Modes

| Mode | Description | Failing Tool(s) |
|---|---|---|
| `none` | Clean run | — |
| `row_drop` | Target has 40% fewer rows | SQL Validator |
| `schema_drift` | `account_balance` renamed to `bal` | Schema Comparator |
| `null_spike` | `customer_id` null rate → 35% | SQL Validator + Log Analyser |
| `latency` | Kafka consumer lag > 10,000 msgs | Log Analyser |

## Tech Stack

| Layer | Technology |
|---|---|
| Workflow orchestration | n8n (self-hosted via Docker) |
| LLM agent | Claude API, tool-use mode |
| Tool API server | Python 3.11 + FastAPI |
| Acceptance testing | Robot Framework 7.x |
| Unit / regression testing | pytest 8.x |
| Mock pipeline | Python + kafka-python |
| Database | PostgreSQL 15 |
| Containerisation | Docker + Docker Compose |
| CI integration | Jenkins webhook |
| Notifications | Slack webhook via n8n |
