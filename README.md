# AI Agent QA Pipeline — n8n + Robot Framework + pytest

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
