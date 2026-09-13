# Repository Inventory — T0.1

**Purpose:** ground truth for the expansion in `expansion-plan.md` / `IMPLEMENTATION.md`. Built by reading every source file, not the README. Anything below that contradicts the README is called out explicitly in §8.

---

## 1. Modules

### `agent/`

| File | Purpose |
|---|---|
| `agent.py` | Claude tool-use loop + FastAPI endpoint (`/agent/run`, `/health`). Calls the tool server over HTTP, loops until Claude returns a final JSON verdict. |
| `prompts.py` | `SYSTEM_PROMPT`, `SYNTHESIS_PROMPT` — the two prompt strings sent to Claude. |
| `tools_manifest.py` | `TOOLS` — the list of tool schemas passed to the Anthropic API. |
| `Dockerfile` | Container build for the agent server. |

**Public functions / signatures:**

- `agent.py`
  - `_call_tool(tool_name: str, tool_input: dict) -> dict` — POSTs to `{TOOL_SERVER_BASE}/tools/{tool_name}`; returns `{"status": "ERROR", "error": ...}` on network/HTTP failure instead of raising.
  - `run_agent(pipeline_event: dict) -> dict[str, Any]` — builds the initial user message, loops up to 10 turns calling `client.messages.create(model=MODEL, max_tokens=4096, system=SYSTEM_PROMPT, tools=TOOLS, messages=messages)`. On `stop_reason == "tool_use"`, executes every `tool_use` block via `_call_tool`, appends `tool_result` blocks, and — once all three tool names (`sql_validator`, `log_analyser`, `schema_comparator`) have been called at least once — appends `SYNTHESIS_PROMPT` as an extra user turn. On `stop_reason == "end_turn"`, extracts the first text block, strips ``` fences, and `json.loads`s it as the return value. Raises `RuntimeError` if 10 turns pass without an `end_turn`.
  - `class PipelineEvent(BaseModel)` — fields: `run_id: str`, `pipeline: str = "customer_transactions"`, `timestamp: str = ""`, `source_table: str = "src.transactions"`, `target_table: str = "tgt.transactions"`, `log_path: str = ""`, `failure_mode: str = "none"`.
  - `agent_run(event: PipelineEvent)` — FastAPI POST handler for `/agent/run`; returns `run_agent(event.model_dump())` or raises `HTTPException(500)`.
  - `health()` — GET `/health` → `{"status": "ok"}`.
  - `MODEL = "claude-sonnet-4-6"` (module constant — **note:** this is not a real/current Anthropic model ID; flagged in §8).

- `prompts.py` — no functions, two module-level string constants. `SYSTEM_PROMPT` hard-codes the response contract (`verdict`, `root_cause`, `details`, `recommended_action`, `confidence`) and the rule "call all three tools regardless of early findings" / "if any tool FAILs, overall verdict is FAIL".

- `tools_manifest.py` — `TOOLS: list[dict]`, three entries (`sql_validator`, `log_analyser`, `schema_comparator`) with `name`, `description`, `input_schema` (JSON Schema, each `required` listing its mandatory fields — see §2 for exact schemas).

### `agent_tools/`

| File | Purpose |
|---|---|
| `sql_validator.py` | `SQLValidator` — row-count drop, null rates, duplicate count between source/target. |
| `log_analyser.py` | `LogAnalyser` — parses a Spark log file (or mock data) for errors/warnings/Kafka lag. |
| `schema_comparator.py` | `SchemaComparator` — diffs source/target column sets: added, removed, renamed (heuristic: remove+add with matching type), type changes. |
| `tool_server.py` | FastAPI app exposing the three tools as `/tools/{name}` POST endpoints, plus `/health`. |
| `Dockerfile` | Container build for the tool server. |

**Public functions / signatures:**

- `SQLValidator(db_conn=None, failure_mode: FailureMode | None = None)`
  - `_get_row_count(table: str) -> int`
  - `_get_null_rates(table: str, columns: list[str]) -> dict[str, float]`
  - `_get_duplicate_count(table: str, key_col: str = "transaction_id") -> int` — **only ever non-zero when a real `db_conn` is supplied**; mock mode always returns 0 (no `duplicate_count` failure mode exists in `mock_pipeline/failures.py`).
  - `validate(source_table: str, target_table: str, run_id: str = "") -> dict[str, Any]` → `{source_count, target_count, row_drop_pct, null_rates, duplicate_count, status: PASS|FAIL, issues: list[str]}`.
  - Thresholds: `ROW_DROP_THRESHOLD` (env, default `5.0`, percent), `NULL_RATE_THRESHOLD` (env, default `0.05`, fraction). Null columns checked are hard-coded: `["customer_id", "amount"]`.
  - Table routing in mock mode is a **substring check** — `"src" in table` picks source data, else target. `_get_row_count` calls target lookup for the source table name itself in that branch when routing to `_get_row_count(target_table)`, i.e. row count of "target" is always looked up via `get_target_data`, and any table name containing "src" is treated as source regardless of exact match.

- `LogAnalyser(failure_mode: FailureMode | None = None)`
  - `_parse_log_file(log_path: str) -> dict` (module-level helper, not a method) — regex-based error/warning/lag extraction from a real file; returns `{errors, warnings, error_count, warn_count, kafka_lag}`. Missing file → single synthetic error `"Log file not found: {log_path}"`, `error_count == 1`.
  - `analyse(log_path: str, run_id: str = "") -> dict[str, Any]` → `{error_count, warn_count, errors, kafka_lag, status, issues}`. Mock-data branches trigger when `log_path == ""` **or** `log_path.startswith("/mock/")`; any other path is parsed as a real file.
  - `KAFKA_LAG_THRESHOLD` (env, default `10000`, integer message count).

- `SchemaComparator(db_conn=None, failure_mode: FailureMode | None = None)`
  - `_get_schema(table: str) -> dict[str, str]` — branches on `isinstance(self.db_conn, sqlite3.Connection)` (PRAGMA-based) vs. assumed psycopg2 (information_schema query with `schema.table` split, default schema `"public"`). Mock branch same `"src" in table` substring routing as SQLValidator.
  - `compare(source_table: str, target_table: str) -> dict[str, Any]` → `{columns_added, columns_removed, columns_renamed: [{from, to}], type_changes: [{column, source_type, target_type}], status, issues}`. Rename detection is greedy first-match on identical `data_type`, not name-similarity — a coincidental type match between an unrelated removed/added column pair would be misreported as a rename.

- `tool_server.py`
  - `SQLValidatorRequest{source_table, target_table, run_id=""}`, `LogAnalyserRequest{log_path, run_id=""}`, `SchemaComparatorRequest{source_table, target_table}` — Pydantic request models.
  - `POST /tools/sql_validator`, `POST /tools/log_analyser`, `POST /tools/schema_comparator`, `GET /health`. Each endpoint constructs a **fresh** tool instance per request (no shared `db_conn`, no injected `failure_mode` — tools default to `get_failure_mode()` reading `INJECT_FAILURE` from the environment at call time).

### `mock_pipeline/`

| File | Purpose |
|---|---|
| `failures.py` | `FailureMode` enum + `get_source_data`/`get_target_data`/`get_log_data` mock generators, keyed off `INJECT_FAILURE`. |
| `producer.py` | Kafka producer (or stdout fallback) emitting one pipeline event JSON per run. |
| `spark_job.py` | Standalone mock "Spark job" runner — prints simulated log lines and a result dict; not wired into the agent or n8n workflow anywhere found. |

**Public functions / signatures:**

- `failures.py`
  - `class FailureMode(str, Enum)`: `NONE="none"`, `ROW_DROP="row_drop"`, `SCHEMA_DRIFT="schema_drift"`, `NULL_SPIKE="null_spike"`, `LATENCY="latency"`.
  - `get_failure_mode() -> FailureMode` — reads `INJECT_FAILURE` env var, lower-cased; invalid value silently falls back to `NONE` (no error raised).
  - `BASE_SCHEMA: dict[str,str]` (6 columns), `BASE_ROW_COUNT = 100_000`.
  - `get_source_data(failure_mode=None) -> dict` → `{schema, row_count, null_rates}` — **source is never mutated by any failure mode** in the current implementation; all injected failures land only in target/log data.
  - `get_target_data(failure_mode=None) -> dict` → `{schema, row_count, null_rates, kafka_lag}`. Per-mode effects: `ROW_DROP` → `row_count = 60_000` (40% drop); `SCHEMA_DRIFT` → `account_balance` removed, `bal` added (same `NUMERIC` type, engineered to trigger the rename heuristic); `NULL_SPIKE` → `customer_id` null rate `0.35`; `LATENCY` → `kafka_lag = 15_000`.
  - `get_log_data(failure_mode=None) -> dict` → `{errors, warnings, error_count, warn_count}`, canned strings per mode (see source for exact text — these strings are asserted on in tests and in `docs/app.py`'s `VERDICTS` table, so they are load-bearing).

- `producer.py`
  - `build_event(run_id: str) -> dict` → the `PipelineEvent`-shaped payload (matches `agent.PipelineEvent` field-for-field except it always sets `log_path = f"/logs/spark_run_{run_id}.log"`, a path that does not exist on disk — real pipeline runs therefore always hit the log analyser's "file not found" branch unless `log_path` is overridden).
  - `emit_to_kafka(event: dict) -> None`, `emit_to_stdout(event: dict) -> None`, `main()`.

- `spark_job.py`
  - `run_job(run_id: str) -> dict` → `{run_id, failure_mode, source_row_count, target_row_count, error_count, warn_count, status}`. **Not imported or invoked anywhere else in the repo** (not by `producer.py`, not by n8n, not by any test) — it is a standalone demo script only.

### `docs/`

| File | Purpose |
|---|---|
| `app.py` | Self-contained SSE demo server (FastAPI) used by the GitHub Pages-style live demo; runs the three real tool classes against mock data and streams a scripted, **pre-written** verdict/test-result narrative (`VERDICTS` dict, `TOOL_DELAYS`, hard-coded pytest case names/pass-fail per failure mode) rather than a live Claude call. |
| `index.html` | Static-feeling front end for `app.py`'s `/run` SSE stream; also carries a fully offline fallback with the same verdict data inlined as JS objects (search `verdict:` blocks) so the page works with **no backend at all** when served as static GitHub Pages. |
| `index_v2.html` | A visual redesign of the same demo (different CSS/theme), same functional shape. |

No GitHub Actions workflow exists (`.github/` is absent). The live demo at `hpk369.github.io/ai-qa-agent` is therefore GitHub Pages serving the repo's `docs/` folder directly as static content — `index.html`'s inlined JS mock data is what actually renders on GitHub Pages, since Pages cannot run `docs/app.py`. `app.py` is only useful when someone runs it themselves (`python docs/app.py`, port 7860).

---

## 2. Claude API tool schemas (exact, from `agent/tools_manifest.py`)

```
sql_validator:
  input_schema.required: [source_table, target_table]
  properties: source_table (string), target_table (string), run_id (string)

log_analyser:
  input_schema.required: [log_path]
  properties: log_path (string), run_id (string)

schema_comparator:
  input_schema.required: [source_table, target_table]
  properties: source_table (string), target_table (string)
```

## 3. Agent response object (current)

Produced by Claude itself (free-form, enforced only by the system prompt, not a schema) and returned verbatim by `POST /agent/run`:

```json
{
  "verdict": "PASS" | "FAIL",
  "root_cause": "string",
  "details": ["string", ...],
  "recommended_action": "string",
  "confidence": 0.0
}
```

Constructed in: `agent/prompts.py` (`SYSTEM_PROMPT`, describes the shape), `agent/agent.py::run_agent` (parses Claude's text output as this JSON and returns it directly — no Pydantic model, no validation of the shape Claude actually returns). Consumed in: `n8n_workflows/qa_agent_workflow.json` (`Check Verdict` IF node reads `$json.verdict`; `Build QA Summary` Code node reads `verdict`, `root_cause`, `details`, `recommended_action`, `confidence`).

## 4. Every environment variable read in the codebase

| Variable | Read in | Default when unset |
|---|---|---|
| `ANTHROPIC_API_KEY` | `agent/agent.py` | none — `KeyError` if missing |
| `INJECT_FAILURE` | `mock_pipeline/failures.py` | `"none"` |
| `TOOL_SERVER_HOST` | `agent/agent.py`, `agent_tools/tool_server.py` | `localhost` (agent) / `0.0.0.0` (server bind) |
| `TOOL_SERVER_PORT` | `agent/agent.py`, `agent_tools/tool_server.py` | `8000` |
| `AGENT_SERVER_HOST` | `agent/agent.py` | `0.0.0.0` |
| `AGENT_SERVER_PORT` | `agent/agent.py` | `8001` |
| `NULL_RATE_THRESHOLD` | `agent_tools/sql_validator.py` | `0.05` |
| `ROW_DROP_THRESHOLD` | `agent_tools/sql_validator.py` | `5.0` |
| `KAFKA_LAG_THRESHOLD` | `agent_tools/log_analyser.py` | `10000` |
| `KAFKA_BOOTSTRAP_SERVERS` | `mock_pipeline/producer.py` | `localhost:9092` (also gates whether Kafka is attempted at all) |
| `KAFKA_TOPIC` | `mock_pipeline/producer.py` | `pipeline-events` |

Read only via `docker-compose.yml` (passed into containers, not read by name in Python beyond the above): `POSTGRES_HOST/PORT/DB/USER/PASSWORD`, `N8N_BASIC_AUTH_USER/PASSWORD`, `N8N_PORT`, `SLACK_WEBHOOK_URL`, `JENKINS_WEBHOOK_URL`, `JENKINS_TOKEN`. `SQLValidator`/`SchemaComparator` never actually open a Postgres connection themselves in this codebase — `db_conn` is always passed in by a caller (tests only; `tool_server.py` never passes one), so in the running Docker stack today, Postgres is provisioned but **not queried** — all three tools run in mock mode even inside `docker compose up`.

## 5. n8n workflow — `n8n_workflows/qa_agent_workflow.json`

| Node (`name`) | Type | Reads |
|---|---|---|
| `Pipeline Trigger` | `webhook` | `POST /pipeline-trigger`; downstream code reads `$json.run_id` |
| `Call QA Agent` | `httpRequest` | POSTs `run_id, pipeline, timestamp, source_table, target_table, log_path, failure_mode` from `$json` to `agent_server:8001/agent/run` |
| `Check Verdict` | `if` | `$json.verdict == "PASS"` (string equals) |
| `Run Robot Framework` | `executeCommand` | shell: `robot --outputdir reports/robot tests/robot/acceptance.robot` (true branch) |
| `Run pytest` | `executeCommand` | shell: `pytest tests/pytest/ --junitxml=reports/pytest/results.xml -v` (false branch) |
| `Read RF Report` | `readBinaryFile` | `/qa/reports/robot/output.xml` |
| `Read pytest Report` | `readBinaryFile` | `/qa/reports/pytest/results.xml` |
| `Merge Reports` | `merge` (`mergeByPosition`) | pass-through convergence of the two branches |
| `Build QA Summary` | `code` (JS) | `$('Call QA Agent').first().json` (`verdict, root_cause, details, recommended_action, confidence`), `$('Pipeline Trigger').first().json.run_id` |
| `Slack Alert` | `slack` | `$env.SLACK_WEBHOOK_URL`; posts `$json.slack_message` via **incoming webhook** — cannot return `ts`, cannot thread, cannot update |
| `Jenkins Webhook` | `httpRequest` | `$env.JENKINS_WEBHOOK_URL`, `$env.JENKINS_TOKEN`, body = `$json.summary` |
| `Respond to Webhook` | `respondToWebhook` | `$('Build QA Summary').first().json.summary` |

Every `$('node name')` reference that Task T0.5 must fix on rename: `Call QA Agent` (referenced from `Build QA Summary`), `Pipeline Trigger` (referenced from `Build QA Summary`), `Build QA Summary` (referenced from `Respond to Webhook`).

## 6. Existing tests and coverage

- `tests/pytest/conftest.py` — SQLite-backed fixtures (`db_conn`, `db_conn_with_row_drop`, `db_conn_with_nulls`, `db_conn_with_dupes`, `clean_schema`, `drifted_schema`, `schema_with_removal`, `clean_log`, `error_log`, `lag_log`).
- `tests/pytest/test_sql_validator.py` — clean/row-drop/null-spike/duplicate cases against real SQLite `db_conn`, plus a `TestSQLValidatorMockMode` class exercising mock-mode paths directly (`FailureMode.NONE/ROW_DROP/NULL_SPIKE`).
- `tests/pytest/test_log_analyser.py` — clean/error/lag log files, missing-file case, mock-mode class (`NONE/NULL_SPIKE/LATENCY`).
- `tests/pytest/test_schema_comparator.py` — clean/rename/removal against SQLite, mock-mode class (`NONE/SCHEMA_DRIFT`).
- `tests/pytest/test_agent_tools.py` — integration-style: runs all three tools together per `FailureMode` and cross-checks that *only* the expected tool(s) fail for each mode.
- `tests/robot/acceptance.robot` (+ `keywords/pipeline_keywords.robot`, `resources/variables.robot`) — black-box HTTP tests against a **running** `tool_server` (`GET /health`, `POST /tools/*`), asserting PASS-shaped results — i.e. the Robot suite as written only exercises the clean-run path and will fail if `INJECT_FAILURE` is anything but `none` when it runs. Nothing in the current repo currently sets `INJECT_FAILURE=none` explicitly before the Robot step in the n8n workflow — it inherits whatever the tool_server container's environment has (`docker-compose.yml` passes `INJECT_FAILURE: ${INJECT_FAILURE:-none}` at container start, so it is fixed for the container's lifetime, not per-run).

No test file imports or exercises `agent/agent.py`'s `run_agent`/`_call_tool` (nothing mocks `anthropic.Anthropic` or `httpx`) — the Claude-facing agent loop itself is currently **untested**.

## 7. `docker-compose.yml` services

`postgres` (15, healthchecked, provisioned but unused by app code — see §4), `zookeeper`, `kafka` (7.5.0, healthchecked), `tool_server` (builds `agent_tools/Dockerfile`, depends on postgres healthy), `agent_server` (builds `agent/Dockerfile`, depends on tool_server healthy, needs `ANTHROPIC_API_KEY`), `n8n` (depends on agent_server healthy, mounts `./n8n_workflows`, `./reports`, `./tests` into the container at `/workflows`, `/qa/reports`, `/qa/tests`). No compose `profiles` exist yet (relevant for Phase 2's `--profile lite`/`--profile hadoop` split). No HDFS/YARN/Hive/Oozie/Spark services exist yet.

## 8. Contradictions with the README / other docs

1. **Model ID.** `agent/agent.py` hard-codes `MODEL = "claude-sonnet-4-6"`. This is not a valid/current Anthropic model identifier — it appears to be a placeholder that was never updated. Flagging per the "read before writing" rule; not changed in Phase 0 since T0.1 is inventory-only, but this should be corrected in T0.4 when the output contract changes (or sooner, as a one-line fix — see Phase 0 stop-gate report).
2. **Postgres is provisioned but never queried by the running stack.** The README's Tech Stack table lists "Database: PostgreSQL 15" as if it's in the data path; in reality every tool always runs in mock mode inside `docker compose up` because `tool_server.py` never constructs a `db_conn`. Only the pytest suite exercises the real-DB code paths (via SQLite, not Postgres — no code anywhere opens a real `psycopg2` connection at all, despite `psycopg2-binary` being a dependency and `_get_schema`'s non-SQLite branch being written for it).
3. **`mock_pipeline/spark_job.py` is dead code** relative to the documented architecture diagram — it is never invoked by `producer.py`, n8n, or any test, and the README doesn't mention it.
4. **`producer.py`'s emitted `log_path` never exists on disk** (`/logs/spark_run_{run_id}.log`), so a real end-to-end Kafka-triggered run would always fall into the log analyser's file-not-found branch rather than the mock or real-parse paths the README's failure-mode demos describe — the documented `INJECT_FAILURE=... python mock_pipeline/producer.py` demos only produce the advertised behavior because the *agent's tool calls* re-derive mock data independently via `INJECT_FAILURE` env var read at tool-call time, not because the emitted event's `log_path` is honored end-to-end.
5. **`docs/app.py`'s demo verdicts are scripted, not live-agent output.** The `VERDICTS` dict and the hard-coded pytest case pass/fail lists in `_stream_run` are canned per failure mode; only the three tool calls in the SSE stream run real code. This is consistent with the demo being explanatory, but the README's "Live Demo" link doesn't disclose that the verdict/test narrative is scripted rather than a live Claude call — worth a one-line disclosure when the README terminology sweep (T0.5) happens, in keeping with the "never overclaim" ground rule.
6. **No `duplicate_count` failure mode exists in `mock_pipeline/failures.py`**, even though `SQLValidator._get_duplicate_count` and its issue message exist and are tested — duplicate detection is only exercised via the SQLite `db_conn_with_dupes` fixture, never via `INJECT_FAILURE`. This is a gap, not a contradiction, but relevant to T3.4's new-failure-modes work later.

---

**Acceptance check (per T0.1):** renaming a field in the agent response touches: `agent/prompts.py` (`SYSTEM_PROMPT`'s documented shape), `agent/agent.py` (nothing structural — it passes the dict through, but any code that destructures the field would need updating, currently none exists there), `n8n_workflows/qa_agent_workflow.json` (`Build QA Summary` Code node, `Check Verdict` IF node if the renamed field is `verdict`), `docs/app.py` (`VERDICTS` dict keys), `docs/index.html` / `docs/index_v2.html` (inlined mock `verdict` objects and the `showVerdict`/SSE handler JS), and any pytest test asserting on that field name (none currently assert on the *agent's* response shape directly — only on the underlying tool outputs — so today no `tests/pytest/*.py` file needs to change, which will no longer be true once T0.4 adds agent-response tests).
