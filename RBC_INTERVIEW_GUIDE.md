# AI QA Agent — Technical Interview Guide
### Big Data QA Role · RBC

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Architecture Deep Dive](#2-architecture-deep-dive)
3. [Mock Pipeline & Failure Injection](#3-mock-pipeline--failure-injection)
4. [QA Tools — Core Logic](#4-qa-tools--core-logic)
   - 4.1 SQL Validator
   - 4.2 Log Analyser
   - 4.3 Schema Comparator
5. [FastAPI Tool Server](#5-fastapi-tool-server)
6. [Claude AI Agent — Tool-Use Loop](#6-claude-ai-agent--tool-use-loop)
7. [n8n Workflow Orchestration](#7-n8n-workflow-orchestration)
8. [Test Strategy](#8-test-strategy)
   - 8.1 pytest Suite
   - 8.2 Robot Framework Acceptance Tests
9. [Infrastructure — Docker Compose](#9-infrastructure--docker-compose)
10. [Key Engineering Decisions](#10-key-engineering-decisions)
11. [Failure Mode Reference](#11-failure-mode-reference)
12. [Interview Q&A Prep](#12-interview-qa-prep)

---

## 1. Project Overview

This project is an **AI-powered QA pipeline** for a Big Data transaction processing system. The pipeline monitors a simulated Spark/Kafka ETL job that moves customer transaction data from a source schema (`src.transactions`) to a target schema (`tgt.transactions`). An AI agent (Claude) reasons over three diagnostic tool outputs and routes to the appropriate test framework based on its verdict.

**The problem it solves:** In Big Data pipelines, failures are not always binary. A row drop of 40%, a column renamed silently, or a Kafka consumer lagging 15,000 messages all indicate different root causes requiring different remediation. A human-readable verdict with a root cause and recommended action — generated automatically — saves hours of manual log triage.

**The live demo** is at: `https://hpk369.github.io/ai-qa-agent/`

---

## 2. Architecture Deep Dive

```
Kafka event / cron / webhook
         │
    [n8n Trigger]           ← Webhook POST /pipeline-trigger
         │                     responseMode: responseNode (holds TCP open)
    [AI Agent Node]  ◄── Claude API (tool-use mode, multi-turn)
         │
    ┌────┴────────────────────────┐
    │             │               │
[SQL Validator] [Log Analyser] [Schema Comparator]
  FastAPI :8000   FastAPI :8000   FastAPI :8000
    │             │               │
    └─────────────┴───────────────┘
                  │
         [LLM Synthesis]  ← Claude synthesises all 3 tool results
         Root cause + verdict JSON
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

**Data flow step by step:**

1. A Kafka producer emits a JSON event when a Spark job completes (or a cron/webhook triggers it).
2. n8n receives the event via its Webhook node and holds the HTTP connection open (`responseMode: responseNode`).
3. n8n forwards the event to the Claude AI agent server (`POST :8001/agent/run`, 60 s timeout).
4. The agent calls all three QA tools (in any order Claude decides) via the tool server (`POST :8000/tools/*`).
5. Claude synthesises the three tool results into a JSON verdict: `{verdict, root_cause, details, recommended_action, confidence}`.
6. n8n's IF node routes: `verdict == "PASS"` → Robot Framework E2E tests; anything else → pytest regression tests.
7. n8n reads the XML report, builds a Slack-formatted summary, fires a Jenkins webhook, and responds to the original caller.

---

## 3. Mock Pipeline & Failure Injection

**File:** `mock_pipeline/failures.py`

The entire system is testable without a real Kafka cluster or Postgres instance because all tools can draw from injected mock data controlled by a single environment variable.

### FailureMode Enum

```python
class FailureMode(str, Enum):
    NONE        = "none"         # Clean run — all checks pass
    ROW_DROP    = "row_drop"     # Target loses 40% of rows
    SCHEMA_DRIFT= "schema_drift" # account_balance renamed to bal
    NULL_SPIKE  = "null_spike"   # customer_id null rate → 35%
    LATENCY     = "latency"      # Kafka consumer lag → 15,000 msgs
```

`FailureMode` inherits from `str` so it serialises cleanly in JSON and can be compared directly to string values without `.value` unpacking.

### How injection works

```python
def get_target_data(failure_mode: FailureMode = None) -> dict:
    schema    = dict(BASE_SCHEMA)       # mutable copy
    row_count = BASE_ROW_COUNT          # 100,000 rows
    null_rates = {"customer_id": 0.0, "amount": 0.0, "account_balance": 0.0}
    kafka_lag  = 0

    if failure_mode == FailureMode.ROW_DROP:
        row_count = int(BASE_ROW_COUNT * 0.60)   # 40% dropped

    elif failure_mode == FailureMode.SCHEMA_DRIFT:
        del schema["account_balance"]
        schema["bal"] = "NUMERIC"                 # renamed

    elif failure_mode == FailureMode.NULL_SPIKE:
        null_rates["customer_id"] = 0.35          # 35% nulls

    elif failure_mode == FailureMode.LATENCY:
        kafka_lag = 15_000                        # msgs behind
    ...
```

Each failure mode touches a different subsystem — this is why it is important to call all three tools regardless of early findings. A latency failure has `kafka_lag = 15_000` but leaves row counts and schema intact, so the SQL Validator and Schema Comparator will still pass.

### Kafka Producer (`mock_pipeline/producer.py`)

```python
def build_event(run_id: str) -> dict:
    return {
        "run_id":       run_id,
        "pipeline":     "customer_transactions",
        "timestamp":    datetime.now(timezone.utc).isoformat(),
        "source_table": "src.transactions",
        "target_table": "tgt.transactions",
        "log_path":     f"/logs/spark_run_{run_id}.log",
        "failure_mode": get_failure_mode().value,
    }
```

If `KAFKA_BOOTSTRAP_SERVERS` is unset, the event is printed to stdout instead of sent to Kafka — the system degrades gracefully without the full stack.

**Usage:**
```bash
INJECT_FAILURE=schema_drift python mock_pipeline/producer.py
```

---

## 4. QA Tools — Core Logic

All three tools share the same pattern: accept either a real database connection or fall back to mock data from `failures.py`. This makes them independently unit-testable with zero infrastructure.

### 4.1 SQL Validator

**File:** `agent_tools/sql_validator.py`

Validates data quality between source and target tables. Checks:
- **Row drop percentage** — `(source_count - target_count) / source_count * 100`
- **Null rates per column** — `AVG(CASE WHEN col IS NULL THEN 1.0 ELSE 0.0 END)`
- **Duplicate count** — `COUNT(*) - COUNT(DISTINCT transaction_id)`

```python
class SQLValidator:
    def validate(self, source_table: str, target_table: str, run_id: str = "") -> dict:
        source_count  = self._get_row_count(source_table)
        target_count  = self._get_row_count(target_table)
        row_drop_pct  = (source_count - target_count) / source_count * 100.0

        null_rates     = self._get_null_rates(target_table, ["customer_id", "amount"])
        duplicate_count = self._get_duplicate_count(target_table)

        issues = []
        if row_drop_pct > ROW_DROP_THRESHOLD:           # default 5.0%
            issues.append(f"Row drop {row_drop_pct:.1f}%")
        for col, rate in null_rates.items():
            if rate > NULL_RATE_THRESHOLD:              # default 0.05 (5%)
                issues.append(f"{col} null rate {rate * 100:.0f}%")
        if duplicate_count > 0:
            issues.append(f"Duplicate rows: {duplicate_count}")

        return {
            "source_count":    source_count,
            "target_count":    target_count,
            "row_drop_pct":    round(row_drop_pct, 2),
            "null_rates":      null_rates,
            "duplicate_count": duplicate_count,
            "status":          "FAIL" if issues else "PASS",
            "issues":          issues,
        }
```

**Threshold configuration:** Both thresholds are environment-variable controlled (`NULL_RATE_THRESHOLD`, `ROW_DROP_THRESHOLD`), which allows different values in staging vs production without code changes.

**The null-rate query pattern** (`AVG(CASE WHEN col IS NULL THEN 1.0 ELSE 0.0 END)`) works identically on both SQLite and PostgreSQL, making the same query valid in unit tests and production.

### 4.2 Log Analyser

**File:** `agent_tools/log_analyser.py`

Parses Spark and Kafka log files for ERROR/WARN lines and consumer lag.

```python
def _parse_log_file(log_path: str) -> dict:
    errors, warnings = [], []
    kafka_lag = 0

    try:
        with open(log_path) as f:
            for line in f:
                line = line.rstrip()
                if _ERROR_PATTERN.search(line):
                    errors.append(line)
                elif _WARN_PATTERN.search(line):
                    warnings.append(line)
                # Extract all lag numbers from one line and take the max
                lag_numbers = [
                    int(m) for m in re.findall(
                        r"(?:lag\s*(?:exceeded|:)?\s*)(\d+)", line, re.IGNORECASE
                    )
                ]
                if lag_numbers:
                    kafka_lag = max(kafka_lag, max(lag_numbers))
    except FileNotFoundError:
        errors.append(f"Log file not found: {log_path}")
    ...
```

**Key design decision — `re.findall` over `re.search` for lag extraction:**
A single log line can contain multiple lag numbers, e.g.:
```
ERROR: Kafka consumer lag exceeded 10000 messages — current lag: 15000
```
`re.search` returns only the first match (10000). `re.findall` returns all matches and `max()` picks the highest (15000). This is what makes `test_kafka_lag_detected` pass correctly — the assertion requires `kafka_lag > 10_000`.

**Path routing logic:**
```python
def analyse(self, log_path: str, run_id: str = "") -> dict:
    if not log_path:
        # Empty path → intentional mock mode (agent called without a log path)
        log_data = get_log_data(self.failure_mode)
    elif log_path.startswith("/mock/"):
        # Explicit mock path → mock mode (useful in tests)
        log_data = get_log_data(self.failure_mode)
    else:
        # Real path → parse file (reports error if not found)
        parsed = _parse_log_file(log_path)
```

The three-branch routing ensures that a missing file is a real failure (not silently swallowed), while still allowing tests to use `/mock/` paths without filesystem setup.

### 4.3 Schema Comparator

**File:** `agent_tools/schema_comparator.py`

Detects schema drift: added, removed, renamed columns, and type changes.

**SQLite vs PostgreSQL detection:**
```python
def _get_schema(self, table: str) -> dict[str, str]:
    if self.db_conn is not None:
        import sqlite3 as _sqlite3
        cur = self.db_conn.cursor()
        if isinstance(self.db_conn, _sqlite3.Connection):
            # SQLite: PRAGMA table_info — no schema prefix, no parameterised query
            bare = table.split(".")[-1]
            cur.execute(f"PRAGMA table_info({bare})")
            return {row[1]: row[2] for row in cur.fetchall()}
        else:
            # PostgreSQL: information_schema.columns with %s params
            schema_name, table_name = table.split(".", 1)
            cur.execute("""
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                ORDER BY ordinal_position
            """, (schema_name, table_name))
            return {row[0]: row[1] for row in cur.fetchall()}
```

`information_schema` and `%s` parametrisation do not exist in SQLite, so detecting the connection type at runtime is necessary to make the same class work in unit tests (SQLite in-memory) and production (PostgreSQL 15).

**Rename detection algorithm:**
```python
# A column is classified as "renamed" if a removed column and an added column
# share the same data type
for removed in list(unmatched_removed):
    removed_type = source_schema[removed]
    for added in list(unmatched_added):
        if target_schema[added] == removed_type:
            columns_renamed.append({"from": removed, "to": added})
            unmatched_removed.remove(removed)
            unmatched_added.remove(added)
            break
```

This heuristic correctly identifies `account_balance → bal` as a rename (both `NUMERIC`) rather than an unrelated removal + addition.

---

## 5. FastAPI Tool Server

**File:** `agent_tools/tool_server.py`

The tool server is the HTTP boundary between the AI agent (or n8n) and the three Python QA tools. Each tool gets its own typed Pydantic request model.

```python
class SQLValidatorRequest(BaseModel):
    source_table: str
    target_table: str
    run_id: str = ""       # optional — for traceability in logs

@app.post("/tools/sql_validator")
def sql_validator(req: SQLValidatorRequest):
    return SQLValidator().validate(req.source_table, req.target_table, req.run_id)
```

**Why separate the tool server from the agent server?**
- The tool server (`port 8000`) can be tested independently — Robot Framework tests call it directly via HTTP without involving the AI agent.
- n8n could call the tools directly if needed (bypassing the agent for debugging).
- The agent server (`port 8001`) is the only component that requires an `ANTHROPIC_API_KEY`.
- In the Docker Compose dependency chain, `agent_server` depends on `tool_server` being healthy, but `tool_server` can run standalone for acceptance testing.

**Endpoints:**
| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/tools/sql_validator` | Row count, null rate, duplicate checks |
| `POST` | `/tools/log_analyser` | Spark/Kafka log parsing |
| `POST` | `/tools/schema_comparator` | Schema drift detection |
| `GET`  | `/health` | Liveness probe (`{"status": "ok"}`) |

---

## 6. Claude AI Agent — Tool-Use Loop

**File:** `agent/agent.py`

This is the brain of the system. It implements a multi-turn conversation with Claude where the model drives which tools to call and when.

### How Claude Tool Use Works

The Anthropic API supports a `tool_use` stop reason. When Claude wants to call a tool, it returns a structured `tool_use` content block instead of plain text. The caller executes the tool and sends back a `tool_result` message. This loop continues until Claude returns `stop_reason: "end_turn"` with the final answer.

```
User: "Analyse this pipeline event"
Claude: [tool_use: sql_validator({source_table: "src.transactions", ...})]
System: [tool_result: {"status": "PASS", "row_drop_pct": 0.0, ...}]
Claude: [tool_use: log_analyser({log_path: "/logs/spark_run_abc.log"})]
System: [tool_result: {"status": "FAIL", "kafka_lag": 15000, ...}]
Claude: [tool_use: schema_comparator({source_table: "src.transactions", ...})]
System: [tool_result: {"status": "PASS", "columns_renamed": [], ...}]
User: "All three tools called. Provide your final QA verdict as JSON only."
Claude: {"verdict": "FAIL", "root_cause": "Kafka consumer lag: 15000 messages", ...}
```

### Agent Loop Implementation

```python
def run_agent(pipeline_event: dict) -> dict:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    messages = [{"role": "user", "content": f"Analyse this event...\n{json.dumps(pipeline_event)}"}]
    tool_results_collected = []

    for _ in range(10):           # safety cap — prevents infinite loops
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            # Final response — extract text block and parse as JSON
            for block in response.content:
                if hasattr(block, "text"):
                    text = block.text.strip()
                    if text.startswith("```"):        # strip markdown fences
                        text = text.split("```")[1]
                        if text.startswith("json"):
                            text = text[4:]
                    return json.loads(text.strip())

        if response.stop_reason == "tool_use":
            tool_result_blocks = []
            for block in response.content:
                if block.type == "tool_use":
                    result = _call_tool(block.name, block.input)   # HTTP to tool server
                    tool_results_collected.append({"tool": block.name, "result": result})
                    tool_result_blocks.append({
                        "type":        "tool_result",
                        "tool_use_id": block.id,
                        "content":     json.dumps(result),
                    })
            messages.append({"role": "user", "content": tool_result_blocks})

            # Once all three tools have been called, inject the synthesis prompt
            called_tools = {r["tool"] for r in tool_results_collected}
            if called_tools >= {"sql_validator", "log_analyser", "schema_comparator"}:
                messages.append({"role": "user", "content": SYNTHESIS_PROMPT})
```

**Critical details:**

- `tool_use_id` in the `tool_result` must match the `id` in the corresponding `tool_use` block. The Anthropic API validates this — mismatched IDs cause an error.
- The synthesis prompt is injected **after** all three tools have been called (checked via set membership: `called_tools >= {required_tools}`). This tells Claude it has all the information it needs and should stop calling tools and produce the final JSON.
- Markdown fence stripping handles the case where Claude wraps JSON in ` ```json ` fences despite the system prompt instructing plain JSON — defensive parsing.
- The 10-turn safety cap prevents runaway loops if Claude keeps requesting tools unexpectedly.

### System Prompt

```python
SYSTEM_PROMPT = """You are a Big Data QA agent. For each pipeline run you receive,
call ALL THREE tools (sql_validator, log_analyser, schema_comparator)
before forming your verdict.

Your final response must be valid JSON with no additional text:
{
  "verdict": "PASS" | "FAIL",
  "root_cause": "one sentence",
  "details": ["issue 1", "issue 2"],
  "recommended_action": "one sentence",
  "confidence": 0.0-1.0
}

Rules:
- Call all three tools regardless of early findings.
- If any tool returns status FAIL, the overall verdict must be FAIL.
- confidence should reflect how certain you are based on the evidence.
"""
```

Requiring all three tools regardless of early findings is a deliberate design choice. A latency failure (Log Analyser FAIL) does not mean the schema is clean — the agent needs the complete picture to give an accurate root cause and confidence score.

### Tool Definitions (`agent/tools_manifest.py`)

Claude knows what tools are available via JSON schemas passed in the `tools` parameter:

```python
{
    "name": "sql_validator",
    "description": "Validates data quality between source and target tables...",
    "input_schema": {
        "type": "object",
        "properties": {
            "source_table": {"type": "string", "description": "e.g. src.transactions"},
            "target_table": {"type": "string", "description": "e.g. tgt.transactions"},
            "run_id":       {"type": "string", "description": "Pipeline run UUID"},
        },
        "required": ["source_table", "target_table"],
    },
}
```

The `input_schema` is a standard JSON Schema object. Claude uses the `description` fields to understand what values to fill in — it maps `source_table` and `target_table` from the pipeline event JSON it received as the user message.

---

## 7. n8n Workflow Orchestration

**File:** `n8n_workflows/qa_agent_workflow.json`

n8n is a self-hosted workflow automation tool (like Zapier but open source and developer-friendly). The workflow has 12 nodes.

### Workflow Map

```
① Pipeline Trigger   (Webhook POST /pipeline-trigger — holds connection open)
         │
② Call QA Agent      (HTTP POST :8001/agent/run — 60s timeout for full tool loop)
         │
③ Check Verdict      (IF: $json.verdict == "PASS")
    TRUE ┤                              FALSE
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
          ┌─────────────┴──────────────┐
          ▼                            ▼
  ⑩ Slack Alert              ⑪ Jenkins Webhook
          └─────────────┬──────────────┘
                        ▼
            ⑫ Respond to Webhook   (returns full summary JSON)
```

### Node Reference

| # | Node | Type | Key Config |
|---|------|------|-----------|
| 1 | Pipeline Trigger | Webhook | `POST /pipeline-trigger`, `responseMode: responseNode` |
| 2 | Call QA Agent | HTTP Request | `POST :8001/agent/run`, timeout 60 s |
| 3 | Check Verdict | IF | `$json.verdict == "PASS"` |
| 4 | Run Robot Framework | Execute Command | `robot --outputdir reports/robot tests/robot/acceptance.robot` |
| 5 | Run pytest | Execute Command | `pytest tests/pytest/ --junitxml=reports/pytest/results.xml -v` |
| 6 | Read RF Report | Read Binary File | `/qa/reports/robot/output.xml` |
| 7 | Read pytest Report | Read Binary File | `/qa/reports/pytest/results.xml` |
| 8 | Merge Reports | Merge | `mergeByPosition` |
| 9 | Build QA Summary | Code (JS) | Cross-references `$('Call QA Agent')` and `$('Pipeline Trigger')` |
| 10 | Slack Alert | Slack | Posts to `#qa-alerts`, credential in `$env.SLACK_WEBHOOK_URL` |
| 11 | Jenkins Webhook | HTTP Request | `POST $env.JENKINS_WEBHOOK_URL` with `$env.JENKINS_TOKEN` |
| 12 | Respond to Webhook | Respond to Webhook | `respondWith: json` — closes connection opened by node ① |

### Build QA Summary — Code Node (JS)

The only custom JavaScript in the workflow. Uses n8n's cross-reference syntax to reach any earlier node by name:

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

**Why `$('Call QA Agent').first().json` and not `$json`?** After the Merge node, `$json` holds the report XML (from the last active branch). The agent result is several nodes back. n8n's node-name cross-reference lets you reach back to any named node's output regardless of branching.

### Why `responseMode: responseNode`?

Standard webhook nodes in n8n respond immediately with a 200 OK and process asynchronously. Setting `responseMode: responseNode` holds the HTTP connection open until node ⑫ explicitly fires. This means any caller — a Kafka consumer, a CI script, a `curl` — receives the full verdict JSON synchronously in a single HTTP round trip. No polling. No callback URL. No second request.

---

## 8. Test Strategy

The project uses two complementary test frameworks for different purposes — this is the central architectural insight.

### 8.1 pytest Suite

**Location:** `tests/pytest/`
**Purpose:** Fast, precise unit and regression tests. Run on FAIL verdict — you need to know exactly which component broke.

**Test count:** 50 tests across 4 files.

#### conftest.py — SQLite In-Memory Fixtures

```python
def _make_sqlite_conn(source_rows, target_rows, source_cols=None, target_cols=None):
    conn = sqlite3.connect(":memory:")          # no disk I/O, no cleanup
    cur  = conn.cursor()
    cur.execute(f"CREATE TABLE src_transactions ({src_col_defs})")
    cur.execute(f"CREATE TABLE tgt_transactions ({tgt_col_defs})")
    cur.executemany(f"INSERT INTO src_transactions VALUES ({placeholders})", source_rows)
    cur.executemany(f"INSERT INTO tgt_transactions VALUES ({placeholders})", target_rows)
    conn.commit()
    return conn
```

Using SQLite in-memory means:
- Zero dependencies — no Postgres container required to run the test suite
- Tests run in milliseconds (sub-100ms per fixture)
- Each fixture is fully isolated — `yield conn` then `conn.close()` guarantees teardown
- The same `SQLValidator` and `SchemaComparator` classes work with SQLite and PostgreSQL because the database-type detection is in the tool, not the test

**Fixture catalogue:**

| Fixture | Scenario | What it creates |
|---------|----------|-----------------|
| `db_conn` | Clean run | 1000 rows source = 1000 rows target |
| `db_conn_with_row_drop` | 40% row loss | 1000 source, 600 target |
| `db_conn_with_nulls` | Null spike | 35% null `customer_id` in target |
| `db_conn_with_dupes` | Duplicates | 20 duplicate `transaction_id` rows |
| `clean_schema` | No drift | Identical schemas |
| `drifted_schema` | Column rename | `account_balance` → `bal` |
| `schema_with_removal` | Column drop | `account_balance` absent in target |
| `clean_log` | No errors | INFO lines only |
| `error_log` | NullPointerException | 2 ERRORs, 2 WARNs |
| `lag_log` | Kafka lag | Lag exceeded 10000, current lag 15000 |

#### Test File Overview

**`test_sql_validator.py`** — 11 tests grouped into 5 classes:
- `TestSQLValidatorCleanRun` — status PASS, drop < 5%, counts match, no issues
- `TestSQLValidatorRowDrop` — status FAIL, drop > 5%, ~40% drop, issue message present
- `TestSQLValidatorNullSpike` — null rate > 30%, status FAIL
- `TestSQLValidatorDuplicates` — duplicate_count > 0, status FAIL
- `TestSQLValidatorMockMode` — no DB connection, uses mock pipeline data

**`test_log_analyser.py`** — 10 tests:
- `TestLogAnalyserCleanLog` — 0 errors, 0 lag, PASS
- `TestLogAnalyserErrorLog` — ≥2 errors, FAIL, NullPointerException preserved
- `TestLogAnalyserLagLog` — kafka_lag > 10,000, FAIL
- `TestLogAnalyserMissingFile` — missing file reports error, FAIL
- `TestLogAnalyserMockMode` — mock clean/null-spike/latency scenarios

**`test_schema_comparator.py`** — 12 tests:
- Clean schema — no removes, no renames, no type changes, PASS
- Rename detection — `{"from": "account_balance", "to": "bal"}` identified
- Removal detection — `account_balance` in `columns_removed`
- Mock mode — NONE passes, SCHEMA_DRIFT fails with correct rename

**`test_agent_tools.py`** — 10 integration-style tests that run all three tools together:
```python
def _run_all_tools(failure_mode: FailureMode, log_path: str = "/mock/path.log") -> dict:
    sql    = SQLValidator(failure_mode=failure_mode).validate(...)
    log    = LogAnalyser(failure_mode=failure_mode).analyse(log_path)
    schema = SchemaComparator(failure_mode=failure_mode).compare(...)
    return {"sql": sql, "log": log, "schema": schema}
```
Key cross-tool assertions:
- `ROW_DROP` → only `sql` fails (schema and log are unaffected)
- `SCHEMA_DRIFT` → only `schema` fails (row counts unaffected)
- `NULL_SPIKE` → both `sql` and `log` fail (SQL sees null rate; log has NullPointerException)
- `LATENCY` → only `log` fails (SQL and schema are unaffected)

### 8.2 Robot Framework Acceptance Tests

**Location:** `tests/robot/`
**Purpose:** Business-readable E2E acceptance tests. Run on PASS verdict — verify business contracts against the live tool server.

```
tests/robot/
├── acceptance.robot             # 6 test cases
├── keywords/
│   └── pipeline_keywords.robot  # reusable keyword library
└── resources/
    └── variables.robot          # ${TOOL_SERVER_URL}, thresholds
```

#### acceptance.robot

```robotframework
*** Settings ***
Suite Setup   Tool Server Should Be Healthy

*** Test Cases ***
Pipeline Run Completes Successfully
    [Tags]    smoke    acceptance
    ${result}=    Run SQL Validator    ${SOURCE_TABLE}    ${TARGET_TABLE}
    Status Should Be Pass        ${result}
    Row Drop Should Be Under Threshold    ${result}    threshold=5.0
    Null Rate Should Be Acceptable        ${result}    column=customer_id    threshold=0.05

Schema Should Match Source
    [Tags]    smoke    acceptance
    ${result}=    Run Schema Comparator    ${SOURCE_TABLE}    ${TARGET_TABLE}
    Status Should Be Pass      ${result}
    No Columns Should Be Removed    ${result}
    No Columns Should Be Renamed    ${result}

Logs Should Contain No Errors
    [Tags]    smoke    acceptance
    ${result}=    Run Log Analyser    ${LOG_PATH}
    Status Should Be Pass       ${result}
    Error Count Should Be Zero  ${result}
```

**Why Robot Framework for the PASS branch?**
- Keywords like `Row Drop Should Be Under Threshold` and `Schema Should Match Source` map directly to business-level pipeline contracts — readable by a business analyst, not just a developer.
- The `Suite Setup: Tool Server Should Be Healthy` guard means the suite fails immediately with a clear message if infrastructure is unavailable, rather than showing confusing HTTP errors in every test.
- Tags (`smoke`, `acceptance`) allow selective execution in CI: `robot --include smoke` for a fast sanity check, full suite for a release gate.

#### Keywords (`pipeline_keywords.robot`)

```robotframework
Run SQL Validator
    [Arguments]    ${source_table}    ${target_table}    ${run_id}=test-run-001
    ${payload}=    Create Dictionary
    ...    source_table=${source_table}
    ...    target_table=${target_table}
    ...    run_id=${run_id}
    ${response}=   POST    ${TOOL_SERVER_URL}/tools/sql_validator
    ...            json=${payload}    expected_status=200
    RETURN         ${response.json()}

Row Drop Should Be Under Threshold
    [Arguments]    ${result}    ${threshold}=5.0
    ${drop}=    Convert To Number    ${result}[row_drop_pct]
    Should Be True    ${drop} < ${threshold}
    ...    Row drop ${drop}% exceeds threshold ${threshold}%
```

Keyword-driven tests separate the test logic (what to check) from the implementation (how to check it). Changing the tool server URL requires editing only `variables.robot`, not every test case.

---

## 9. Infrastructure — Docker Compose

**File:** `docker-compose.yml`

Six services with proper dependency ordering using `condition: service_healthy`.

```
postgres ──healthcheck──► tool_server ──healthcheck──► agent_server ──healthcheck──► n8n
zookeeper ──────────────► kafka
```

### Service Summary

| Service | Image / Build | Port | Purpose |
|---------|--------------|------|---------|
| `postgres` | `postgres:15` | 5432 | Transaction data store (src/tgt schemas) |
| `zookeeper` | `confluentinc/cp-zookeeper:7.5.0` | 2181 | Kafka coordinator |
| `kafka` | `confluentinc/cp-kafka:7.5.0` | 9092 | Pipeline event bus |
| `tool_server` | `./agent_tools/Dockerfile` | 8000 | QA tool HTTP endpoints |
| `agent_server` | `./agent/Dockerfile` | 8001 | Claude agent endpoint |
| `n8n` | `n8nio/n8n:latest` | 5678 | Workflow orchestration UI |

### Healthcheck Pattern

```yaml
tool_server:
  healthcheck:
    test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
    interval: 10s
    timeout: 5s
    retries: 5

agent_server:
  depends_on:
    tool_server:
      condition: service_healthy   # waits for tool_server healthcheck to pass
```

Using `condition: service_healthy` instead of just `condition: service_started` prevents the agent server from starting before the tool server is actually ready to accept connections. This is the difference between a reliable `docker compose up` and a race condition.

### Volume Mounts (n8n)

```yaml
n8n:
  volumes:
    - n8n_data:/home/node/.n8n         # workflow persistence
    - ./n8n_workflows:/workflows        # import from local files
    - ./reports:/qa/reports             # test output accessible to n8n's Read Binary File nodes
    - ./tests:/qa/tests                 # Robot Framework and pytest test files
```

The `reports` and `tests` mounts are required because n8n's Execute Command nodes run `robot` and `pytest` inside the n8n container, and the Read Binary File nodes need to read the output XML files that those commands produce.

### Environment Variables

```bash
# Required
ANTHROPIC_API_KEY=sk-ant-...

# Optional — defaults work for local dev
INJECT_FAILURE=none         # none | row_drop | schema_drift | null_spike | latency
SLACK_WEBHOOK_URL=...        # Slack Incoming Webhook URL
JENKINS_WEBHOOK_URL=...      # Jenkins build trigger URL
JENKINS_TOKEN=...            # Jenkins API token
```

---

## 10. Key Engineering Decisions

### 1. Why AI agent + tools instead of hardcoded rules?

Hardcoded rules (e.g., `if row_drop > 5% then FAIL`) require a developer to anticipate every failure combination. The AI agent can reason across multiple tool outputs simultaneously — for example, correlating a null spike in SQL with a NullPointerException in logs and assigning a single root cause. It also generates a recommended action and a confidence score, which hardcoded rules cannot.

### 2. Why PASS → Robot Framework and FAIL → pytest?

| Framework | When used | Why |
|-----------|-----------|-----|
| Robot Framework | PASS (clean pipeline) | Business-readable keyword syntax maps to acceptance contracts. `Row Drop Should Be Under Threshold` communicates what the business agreed to, not what the code does. |
| pytest | FAIL (broken pipeline) | Fast, precise, pytest's class-based test organisation pinpoints exactly which check failed and why. JUnit XML integrates natively with Jenkins and GitHub Actions. |

### 3. Why SQLite for unit tests instead of a Postgres Docker container?

- `pytest` runs in CI without Docker — using SQLite means the full test suite runs with `pip install -r requirements.txt && pytest` and nothing else.
- In-memory SQLite databases are created and destroyed in microseconds — 50 tests complete in under 3 seconds.
- The tools detect the connection type at runtime so the exact same code paths execute in tests and production.

### 4. Why `responseMode: responseNode` in n8n?

Asynchronous webhooks require the caller to implement polling or a callback URL. With `responseMode: responseNode`, the webhook keeps the HTTP connection open until the workflow completes. The caller gets the verdict synchronously in one request — simpler CI integration, no polling loop, no extra endpoint.

### 5. Why a single JS Code node instead of multiple Set/Function nodes for the Slack message?

The Slack message requires conditional formatting (choose emoji, filter empty detail arrays, join bullet points). That logic expressed in n8n expression syntax would require five or six chained nodes. Thirty lines of readable JavaScript is objectively clearer, easier to test in isolation, and easier for a future engineer to modify.

### 6. Why `re.findall` over `re.search` for Kafka lag?

A Spark log line can reference two lag values: `"lag exceeded 10000 messages — current lag: 15000"`. `re.search` finds the first match (10000). `re.findall` finds all matches; `max()` gives the correct current value (15000). The test assertion `kafka_lag > 10_000` requires exactly this: `15000 > 10000` passes; `10000 > 10000` fails.

---

## 11. Failure Mode Reference

| Mode | `INJECT_FAILURE` | Tools that FAIL | Root cause text |
|------|-----------------|-----------------|-----------------|
| Clean run | `none` | — | "All QA checks passed" |
| Row drop | `row_drop` | SQL Validator | "Row drop 40.0% — 40000 records dropped during deduplication" |
| Schema drift | `schema_drift` | Schema Comparator | "Column account_balance renamed to bal in target schema" |
| Null spike | `null_spike` | SQL Validator + Log Analyser | "35% null rate on customer_id; NullPointerException in CustomerTransformStep" |
| Kafka latency | `latency` | Log Analyser | "Kafka consumer lag: 15000 messages — exceeded threshold of 10000" |

**Triggering failure modes:**
```bash
# Clean run
INJECT_FAILURE=none python mock_pipeline/producer.py

# Schema drift
INJECT_FAILURE=schema_drift python mock_pipeline/producer.py

# Or via the n8n webhook directly
curl -X POST http://localhost:5678/webhook/pipeline-trigger \
  -H "Content-Type: application/json" \
  -d '{"run_id": "test-001", "failure_mode": "row_drop", ...}'
```

---

## 12. Interview Q&A Prep

The questions below are likely topics at RBC for a Big Data QA role. Each answer draws directly on this project.

---

**Q: How would you automate quality checks for a high-volume transaction pipeline at RBC?**

This project does exactly that. I built three automated checks: a SQL Validator that computes row drop percentage and null rates using `COUNT(*)` and `AVG(CASE WHEN col IS NULL THEN 1.0 ELSE 0.0 END)`, a Log Analyser that parses Spark and Kafka logs with regex, and a Schema Comparator that detects column renames/removals/type changes. All three are exposed as HTTP endpoints and called by a Claude AI agent, which synthesises the results into a verdict with root cause and recommended action. The whole pipeline is triggered by Kafka events and orchestrated by n8n.

---

**Q: What is schema drift and how do you detect it?**

Schema drift is when the target table's column structure diverges from the source — columns get renamed, removed, or have their data types changed, usually due to an upstream schema change that wasn't communicated to downstream consumers.

My `SchemaComparator` reads the column list from both tables (via PostgreSQL's `information_schema.columns` or SQLite's `PRAGMA table_info`) and computes:
- `columns_removed = source_cols - target_cols`
- `columns_added = target_cols - source_cols`
- A rename is inferred when a removed column and an added column share the same data type
- Type changes are detected for columns present in both

The example failure is `account_balance` (NUMERIC) renamed to `bal` — detected as `{"from": "account_balance", "to": "bal"}`.

---

**Q: How do you test a Big Data pipeline without a full infrastructure setup?**

I use two strategies. First, failure injection: all tools accept a `FailureMode` enum that simulates broken data (row drops, schema drift, null spikes, Kafka lag) as mock data, controlled by an environment variable. No Kafka or Postgres needed. Second, SQLite in-memory for unit tests: the same `SQLValidator` and `SchemaComparator` classes detect whether they have a SQLite or PostgreSQL connection and switch query strategies accordingly. All 50 pytest tests run with `pip install -r requirements.txt && pytest` in under 3 seconds, no Docker required.

---

**Q: What is the difference between acceptance testing and regression testing, and how did you implement both?**

Acceptance testing verifies that the system meets its business contract — the pipeline delivered the right data. I used Robot Framework for this because its keyword-driven syntax (`Row Drop Should Be Under Threshold`, `No Columns Should Be Renamed`) is readable by business stakeholders, not just developers. These tests run when the AI agent gives a PASS verdict.

Regression testing verifies that individual components still behave correctly after a change. I used pytest for this because it's fast, precise, and its class-based organisation (`TestSQLValidatorRowDrop`, `TestLogAnalyserLagLog`) tells you exactly which check failed. These tests run on FAIL verdicts, where you need granular debugging information, not a high-level business narrative.

---

**Q: How do you handle a situation where Kafka consumer lag grows during a pipeline run?**

The Log Analyser detects this by scanning each log line with `re.findall(r"(?:lag\s*(?:exceeded|:)?\s*)(\d+)", line)` and taking the maximum lag value found. If lag exceeds the threshold (default 10,000 messages), the tool returns `status: FAIL` and includes the lag value in `issues`. The AI agent picks this up, identifies it as the only failing tool (SQL and Schema are unaffected by lag), and returns a verdict like: `"verdict": "FAIL", "root_cause": "Kafka consumer lag: 15000 messages", "recommended_action": "Scale Kafka consumer group or investigate producer throughput spike"`.

The `KAFKA_LAG_THRESHOLD` is environment-variable controlled so you can set different thresholds for dev (permissive) and production (strict) without code changes.

---

**Q: How do you integrate automated QA with CI/CD at RBC?**

The n8n workflow fires a Jenkins webhook (node ⑪) after every pipeline run with the full QA summary as JSON payload. Jenkins can gate deployments on the verdict — if `verdict == "FAIL"`, the Jenkins job fails and the deployment is blocked. The webhook fires synchronously (the HTTP connection is held open until n8n completes), so Jenkins doesn't need to poll for results. Both pytest and Robot Framework produce JUnit XML reports (`--junitxml=reports/pytest/results.xml`, Robot's `output.xml`), which Jenkins parses natively for test trend reports.

---

**Q: How would you scale this system to handle RBC's transaction volume?**

Several ways:
- **Tool server:** Stateless FastAPI — deploy multiple replicas behind a load balancer. Each request is independent.
- **Agent server:** The Claude API call is the bottleneck; run multiple agent_server replicas for parallel pipeline events.
- **Kafka partitioning:** The existing Kafka setup (confluentinc images) supports multiple partitions — parallel consumers can process multiple pipeline events simultaneously.
- **Thresholds as config:** All quality thresholds are environment variables, so you can tighten them as data volume increases without redeploying code.
- **Database:** PostgreSQL 15 with proper indexing on `transaction_id` for the duplicate check — `COUNT(DISTINCT transaction_id)` is fast with a B-tree index.

---

**Q: Walk me through a null spike failure end to end.**

1. `INJECT_FAILURE=null_spike python mock_pipeline/producer.py` emits a Kafka event.
2. n8n receives it via the Webhook node and calls `POST :8001/agent/run`.
3. The Claude agent calls `sql_validator` → returns `status: FAIL`, `null_rates: {customer_id: 0.35}`, issue: `"customer_id null rate 35%"`.
4. The agent calls `log_analyser` → returns `status: FAIL`, errors: `["ERROR: NullPointerException in CustomerTransformStep at line 84", "ERROR: Null propagation detected in customer_id join key"]`.
5. The agent calls `schema_comparator` → returns `status: PASS` (no schema changes).
6. Synthesis prompt injected: Claude cross-references both FAIL results and returns:
   ```json
   {
     "verdict": "FAIL",
     "root_cause": "customer_id null spike (35%) with NullPointerException in CustomerTransformStep",
     "details": ["customer_id null rate 35%", "NullPointerException at CustomerTransformStep line 84"],
     "recommended_action": "Investigate CustomerTransformStep null handling in join key logic",
     "confidence": 0.97
   }
   ```
7. n8n IF node routes to pytest. Tests run targeting null rate and error-log checks.
8. Slack alert posted to `#qa-alerts` with ❌ and the root cause. Jenkins webhook fires with the full JSON.

---

**Q: How do you ensure your tests don't give false positives?**

Three practices in this project:
1. **Independent failure modes:** Each `FailureMode` only affects the tools it should. `LATENCY` sets `kafka_lag = 15_000` but leaves row counts and schema identical — `test_sql_unaffected_by_latency` in `test_agent_tools.py` explicitly asserts `sql["status"] == "PASS"` for this mode. This verifies the tools don't leak failures across dimensions.
2. **Boundary assertions:** Tests assert specific ranges, not just pass/fail. `test_approximately_40_pct_drop` asserts `35.0 < row_drop_pct < 45.0` — it would catch if the mock data formula changed unexpectedly.
3. **Isolated fixtures:** Each pytest fixture creates a fresh in-memory SQLite database. There is no shared state between tests — a failure in one test cannot affect another.

---

*This guide covers every file, design decision, and code path in the project. Good luck at RBC.*
