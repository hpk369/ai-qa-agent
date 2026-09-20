# ETL Production Support Triage Agent

[![tests](https://github.com/hpk369/ai-qa-agent/actions/workflows/tests.yml/badge.svg)](https://github.com/hpk369/ai-qa-agent/actions/workflows/tests.yml)

**[▶ Live Demo](https://demo.inkandinfra.com/)** — interactive pipeline simulator, no setup required. Walks through severity classification, an incident record, and a Slack Block Kit preview with working Approve/Reject/Escalate buttons.

When a production ETL job breaks, the question that matters isn't "did the pipeline pass or fail" — it's "what's the severity, who's affected, and what do I do in the first fifteen minutes." This project answers that from the one artifact an on-call analyst always has: **the logs**.

Each session gets its own log set — a few log files mixed from a corpus of real, public production logs (Spark, YARN, HDFS, ZooKeeper, OpenStack, syslog, sshd, and more) with ETL failure signatures injected into them. The agent reads the log text, derives severity signals from it, classifies them with a deterministic config-driven ruleset, opens a structured incident record, and **posts a Slack alert**. The thread reply carries a link to download the exact log set that produced the alert, so the call can be checked against the evidence.

It runs two ways. **Batch** (`scripts/logset.py`) mixes one finished log set and triages it. **Streaming** (`scripts/stream.py`) emits logs continuously, alerts at the line rather than at the end — and when a failure is serious enough to block a real pipeline, it stops the stream and waits for a human to confirm in Slack that the problem is fixed before it resumes.

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
| Live stream, backpressure, the resolution gate | `logsets/stream.py` |
| Alert narration and resolution judgement | `agent/llm.py` |
| Model providers: Claude, Ollama, any OpenAI-compatible endpoint | `agent/providers.py` |
| Entry points | `scripts/logset.py`, `scripts/stream.py`, `scripts/slack_reply.py`, `POST /logset/run`, `GET /logset/{id}/download` |

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

## Live stream with backpressure

```bash
python scripts/stream.py                    # 3 minutes of live logs at 6 lines/s
python scripts/stream.py --rate 12 --duration 600
python scripts/stream.py --sources kafka-consumer,spark-executor --seed 42
python scripts/stream.py --auto-resolve 20  # unattended demo, nobody watching Slack
```

Lines arrive continuously and the agent tails them, so an alert fires **at the failure**, while the rest of the stream is still running. Each incident is judged only on the lines that arrived since the last one — the window a tail actually sees — so a second failure is never re-derived from the first one's evidence.

**A blocking failure stops the pipeline.** P1 and P2 (configurable) are the failures a real pipeline cannot carry on through: the target is unavailable, the job aborted, the control totals disagree. The stream stops advancing and drops to a trickle of backpressure lines — retries, growing queue depth, DagRuns held on an upstream incident — which is what a stalled pipeline actually writes. It resumes only when a human confirms the problem is fixed.

```
[   48.1s] 🚨 INC-20260920-0100-2 P2
            Row count reconciliation failed for tgt.transactions: 12.5% of the
            settlement batch is missing downstream of CustomerTransformStep.
            signatures: SIG-001-ROW-SHORTFALL   runbook: docs/runbooks/RB-001-row-shortfall.md
[   48.1s] ⏸ INC-20260920-0100-2 is blocking the stream
            the pipeline is held here until someone confirms it is fixed:
            python scripts/slack_reply.py INC-20260920-0100-2 "<what you did to fix it>"
[   50.1s] 💬 U_ONCALL: taking a look, paging the platform team
[   50.1s] 🤖 not resolved (0.00): no reply states outright that the issue is fixed
[   52.1s] 💬 U_ONCALL: reran the load after the dedup fix — counts match, all clear
[   52.1s] 🤖 resolved (0.95): "counts match" states recovery
[   52.1s] ▶️ a human confirmed it in Slack — backpressure released
[   60.0s] ■ completed — 1408 lines, 2 incident(s)
```

(The alert paragraph and the two 🤖 lines above come from whichever model is configured — see [the model layer](#the-model-layer). With none configured they come from the deterministic fallbacks, which is what the test suite exercises.)

Three things can release the gate, and a model being reachable is only one of them:

1. **A thread reply Claude judges to be a confirmation.** "restarted the consumer, lag is draining" releases it; "looking into it" does not. Ambiguity keeps it shut — resuming a broken pipeline is worse than waiting another minute.
2. **A ✅ reaction** on the incident message (`--react white_check_mark`).
3. **The incident being resolved in the record** — a Slack Approve/Reject button, or anything else that calls `resolve_incident`.

If nobody confirms within `--max-block-wait` (default 10 minutes), the stream **stops** rather than resuming on a pipeline nobody has fixed, and says so.

With `SLACK_MODE=stub` there is no workspace to type into, so replies come from a local inbox that the stub Slack client reads:

```bash
python scripts/slack_reply.py INC-... "restarted the consumer, lag is draining"
python scripts/slack_reply.py INC-... --react white_check_mark
python scripts/slack_reply.py INC-... --list
```

Everything downstream treats those exactly as real thread activity: the gate polls them, and MTTA counts the first one as the human response. In `SLACK_MODE=live` a person replies in Slack and this script is unnecessary.

A stream writes into the same layout as a batch log set, so `--show`, `GET /logset/{id}` and the download endpoint all work on it unchanged.

## The model layer

Two calls, both in [`agent/llm.py`](agent/llm.py), both chosen because the judgement is genuinely linguistic:

**1. Narrating the alert.** Log lines are precise and unreadable. The model turns the matched lines and derived signals into the paragraph a woken-up engineer reads first: business impact, likely root cause, next action. It is told the severity and told not to revisit it.

**2. Judging whether a Slack reply confirms resolution.** This is what releases a blocked stream, and no regex settles it — "should be fine after the next run" is not a confirmation, "namenode is back up, writes are succeeding" is.

What the model is deliberately **not** asked: severity, runbook selection, or whether an incident opens at all. Those stay in `agent/severity.py` and `agent/runbooks.py`, where the same evidence always produces the same answer and `matched_conditions` says exactly why. A model that occasionally calls the same evidence P2 and P3 is not something you can run an on-call rota against.

### Any model, including free ones

Neither call needs a frontier model — they are a short paragraph and a yes/no. [`agent/providers.py`](agent/providers.py) resolves whatever is configured:

| Option | Cost | Setup |
|---|---|---|
| **Ollama** (or llama.cpp, LM Studio, vLLM) on your own machine | free | `ollama serve && ollama pull llama3.2` — auto-detected on `localhost:11434`, nothing to configure |
| **Groq**, **OpenRouter**, **Together**, **Fireworks**, **DeepSeek**, **Gemini** (OpenAI-compatible endpoint) | free tiers available | set `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` |
| **Claude API** | paid — no free model; the default here is Haiku 4.5 at ~$0.004 an incident | federation, an `ant auth login` profile, or `ANTHROPIC_API_KEY` — see [Credentials](#credentials) |
| **Nothing** | free | deterministic fallbacks, and the output says so |

```bash
# A local model — free, no key, no account
ollama pull llama3.2
python scripts/stream.py                       # prints: Model: ollama (llama3.2)

# Any hosted OpenAI-compatible endpoint (Groq shown; OpenRouter, Together, … identical)
export LLM_BASE_URL=https://api.groq.com/openai/v1
export LLM_MODEL=llama-3.3-70b-versatile
export LLM_API_KEY=gsk_...

# Claude — defaults to Haiku 4.5, the cheapest current model.
# Authenticate with federation, an `ant auth login` profile, or a key:
export ANTHROPIC_API_KEY=sk-ant-...
export AGENT_MODEL=claude-opus-5      # only if you want to pay for more; see below

LLM_PROVIDER=off python scripts/stream.py      # force the deterministic path
```

`LLM_PROVIDER=auto` (the default) takes the first of: a configured `LLM_BASE_URL`, an `ANTHROPIC_API_KEY`, a local Ollama that answers. Pin it with `anthropic`, `openai`, `ollama` or `off`.

### Credentials

Three ways in, and the code path is the same for all of them — the SDK is constructed with no arguments and resolves the credential itself. The banner tells you which one won:

```
Model: anthropic (claude-haiku-4-5, via workload identity federation)
```

**1. Workload Identity Federation — no static secret at all.** Where this runs on a platform with an OIDC identity (GitHub Actions, Kubernetes, AWS, GCP, Azure, Okta), the workload presents a JWT its platform already issues, Anthropic exchanges it for a token that expires in minutes, and the SDK refreshes it before it does. Nothing to rotate, nothing to leak. Set up the issuer, service account and rule once in the Console (**Settings → Workload identity → Connect workload**), then:

```bash
export ANTHROPIC_FEDERATION_RULE_ID=fdrl_...
export ANTHROPIC_ORGANIZATION_ID=<org-uuid>
export ANTHROPIC_SERVICE_ACCOUNT_ID=svac_...
export ANTHROPIC_IDENTITY_TOKEN_FILE=/var/run/secrets/anthropic.com/token   # or ANTHROPIC_IDENTITY_TOKEN
# ANTHROPIC_WORKSPACE_ID only when the rule covers more than one workspace
```

[`.github/workflows/triage-stream.yml`](.github/workflows/triage-stream.yml) does exactly this for GitHub Actions — see [Continuous integration](#continuous-integration) for the variables it reads. On Kubernetes it is a projected service-account token and the path above is already right.

**2. An `ant auth login` profile — keyless on a developer machine.** A laptop running `python scripts/stream.py` by hand has no workload identity to federate, so this is the keyless option there: an interactive login stores a short-lived token under `~/.config/anthropic/` (mode `0600`), outside the repo, and a zero-arg client picks it up.

```bash
ant auth login          # browser login; ant auth status shows which source won
python scripts/stream.py
```

**3. An API key — simplest, and a long-lived secret.** `cp .env.example .env`, paste it there (`.env` is gitignored and loaded by the CLIs), or export it for one shell with a leading space so it stays out of `~/.bash_history`:

```bash
 export ANTHROPIC_API_KEY=sk-ant-...      # note the leading space
```

Whichever you use, put a **spend limit** on it in the Console. On a few-dollars-a-year budget that is the control that actually protects you — against a leaked credential, and equally against a stream left running overnight.

#### The trap worth knowing

Credentials resolve in a fixed order: `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` → `ANTHROPIC_PROFILE` → federation variables → the active profile on disk. **A variable set to an empty string still wins its slot.** An exported `ANTHROPIC_API_KEY=""` — the shape a blank placeholder in `.env` produces — makes the SDK authenticate with an empty key instead of falling through to federation, and the failure reads like a broken federation setup rather than a stray variable.

Two things here guard against that: `agent/env.py` never exports a blank value from `.env`, and `agent/providers.py` clears an empty `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` when federation or a profile is configured. Both are pinned by tests. `ant auth status` is the authoritative answer to "which credential is this actually using".

#### What the repo does with it

- `.env` is gitignored, and `.env.example` ships with the credential lines commented out.
- `agent/env.py` loads `.env` at the **entry points only** — a library import never reads it, so running the tests cannot pull your credentials into the process.
- Exception text is redacted before it reaches a log line or a Slack message (`agent/llm.py::_describe_exception`, `agent/slack_client.py::_redact`), so a 401 quoting a credential does not end up in `reports/`.
- Nothing writes a credential into an incident record, a manifest, or a downloadable bundle, and no credential is ever put in a prompt.
- Before pushing: `git grep -iE "sk-ant-|gsk_|xoxb-"`. If one ever does reach a commit, rotate first — assume it is public the moment it is pushed — then clean the history.

### What it costs

One incident is one narration call plus a judge call per batch of Slack replies — measured at about **2,700 input and 290 output tokens**, most of it the 40 log lines of context (`MAX_CONTEXT_LINES` in `agent/llm.py`).

| Model | per incident | incidents per $1 | an 8-hour stream at default pacing |
|---|---|---|---|
| **Haiku 4.5** (the default) | $0.004 | ~240 | ~$4 |
| Sonnet 5 | $0.018 | ~56 | ~$17 |
| Opus 5 | $0.057 | ~18 | ~$54 |
| Local Ollama, or a free hosted tier | $0 | — | $0 |

Haiku 4.5 is the default because this workload is a short paragraph and a yes/no. Sonnet 5 and Opus 5 think before answering and thinking bills as output, which is why they cost 4-14× Haiku here rather than the 2-5× their headline prices suggest. (Haiku 4.5 and Sonnet 4.5 also reject `output_config.effort`, so the adapter omits it for them.)

Two things keep the bill flat: the test suite never calls a model (it runs the fallbacks), and a blocked stream only calls the judge when a *new* reply arrives — waiting costs nothing. The thing that is not flat is a stream left running: at the default 15-45s gap that is roughly 120 incidents an hour.

**Structured output is negotiated, not assumed.** The OpenAI-compatible adapter asks for a JSON schema first; when an endpoint rejects that (many local servers do) it retries in plain JSON mode with the schema inlined in the prompt, strips the code fences and chat that small models wrap answers in, and validates against the schema itself. Anything that still doesn't validate counts as a failed call — never a half-parsed object.

**Every call degrades instead of failing.** No provider, a rate limit, a timeout, a malformed response — each returns the deterministic text the code had before, tagged with the provider that produced it (`narrated by ollama`, `narrated by fallback`) so the output tells you which you are reading. An alert that reads flatter is fine; an incident that fails to open because an inference call failed is not. The resolution judge fails *closed*: an unreachable model leaves the stream paused, never resumes it.

## Quick Start

```bash
pip install -r requirements.txt
python scripts/fetch_logs.py     # optional — real background logs
python scripts/logset.py         # batch: mix, triage, alert, print the download path
python scripts/stream.py         # live: stream logs, alert as they arrive, block on P1/P2
```

No API key, no containers, no Slack workspace needed for either. The alert lands in `reports/slack/`, the log set and its zip in `reports/logsets/`. Point it at a model — [a local Ollama, a free hosted endpoint, or Claude](#any-model-including-free-ones) — and it writes the alerts and judges the Slack replies too.

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
pytest tests/pytest/ -v          # 262 tests, nothing touches the network
pytest tests/pytest/test_logsets.py -v
pytest tests/pytest/test_stream.py tests/pytest/test_llm.py tests/pytest/test_providers.py -v
```

## Continuous integration

Two workflows, in [`.github/workflows/`](.github/workflows):

**`tests.yml`** — runs the suite and `pyflakes` on every push and pull request. It needs no credentials, no network and no services: the tests exercise the deterministic fallbacks and a local HTTP server for the provider adapter. If it ever starts needing a secret, something has regressed.

**`triage-stream.yml`** — runs the live stream against the real model on a weekly schedule (or on demand), authenticating with **Workload Identity Federation**. GitHub mints a short-lived OIDC token for the job, Anthropic exchanges it for a token that expires in minutes, and no API key exists in the repository or its secrets. The run uploads its log sets, incident records and Slack payloads as an artifact.

Set these under **Settings → Secrets and variables → Actions → Variables** — they are identifiers rather than secrets, so they stay auditable in the run log:

| Variable | |
|---|---|
| `ANTHROPIC_FEDERATION_RULE_ID` | `fdrl_...` from the Console's **Connect workload** wizard |
| `ANTHROPIC_ORGANIZATION_ID` | the organization UUID |
| `ANTHROPIC_SERVICE_ACCOUNT_ID` | `svac_...`, the rule's target service account |
| `ANTHROPIC_WORKSPACE_ID` | only when the rule covers more than one workspace — with a single-workspace rule, leave it unset and the server picks that one |
| `ANTHROPIC_OIDC_AUDIENCE` | optional — defaults to `https://api.anthropic.com`, which is what the Console's GitHub Actions wizard writes into the rule |
| `AGENT_MODEL` | optional; defaults to `claude-haiku-4-5` |

The workflow requests the token with audience `https://api.anthropic.com` (GitHub's own default is the repository owner URL, which would not match a rule built by the wizard), declares `permissions: id-token: write`, writes the JWT to a `0600` file that is never echoed, and **fails if the resolved credential is anything other than federation** — otherwise a misconfigured run would quietly fall back to deterministic narration and look like a quiet model rather than a broken setup. Until `ANTHROPIC_FEDERATION_RULE_ID` is set the job is skipped rather than failed, so a fork does not go red.

A scheduled run passes `--auto-resolve 30`: nobody is watching Slack at 07:17 on a Monday, so a blocking incident releases itself instead of holding the runner. Drop that flag to watch the gate hold for real. At `--duration 120` the run costs about a cent on Haiku 4.5.

## Project Structure

```
ai-qa-agent/
├── logsets/                # Log sources + error signatures (catalog.py), corpus
│                           #   fetch/fallback (corpus.py), per-session mixing
│                           #   (session.py), analysis → severity → incident →
│                           #   Slack (triage.py), live stream + blocking gate
│                           #   (stream.py)
├── agent/                  # Severity classifier, incident record + lifecycle,
│                           #   Slack client/blocks/verify, runbook selection,
│                           #   the model layer (llm.py + providers.py), and the
│                           #   HTTP server
├── config/                 # severity.yml — thresholds live here, never in code
├── schemas/                # incident.schema.json — the incident record's contract
├── scripts/                # logset.py, stream.py, slack_reply.py, fetch_logs.py,
│                           #   incident_metrics.py
├── tests/
│   ├── pytest/             # Unit/regression tests for every module above
│   └── fixtures/blocks/    # Golden-file Block Kit fixtures (agent/slack_blocks.py)
├── .github/workflows/      # tests on every push; a weekly federated stream run
├── docs/                   # SLACK_SETUP.md, runbooks/, and the live demo page
├── reports/                # Session log sets + zips, persisted incidents,
│                           #   stub Slack payloads
└── .env.example
```

## Design Decisions

**Why is severity decided by code rather than inferred?**
Anything that both observes evidence and judges its severity will eventually call identical evidence two different ways, and there is no way to audit *why* after the fact. `agent/severity.py` reads the signals and applies the same YAML-configured thresholds every time — the same log set always produces the same severity, and `matched_conditions` names exactly which rule fired. The thresholds live in `config/severity.yml` so a disagreement about where P2 starts is a config review, not a code change. The same argument applies to runbook selection (`agent/runbooks.py`) and to the approval gate: all three are code reading signals, not judgement calls.

**Why derive signals from log text rather than from the mixer?**
Because the mixer knows the answer and the agent must not. Everything the triage path concludes comes from regex matches against the log lines — the same lines an analyst tailing the file would see. The manifest's ground truth exists only to score detection afterwards, and a test asserts the analysis is identical when that ground truth is deleted.

**Why is Slack a view rather than the source of truth?**
The incident record on disk (`schemas/incident.schema.json`) is the system of record. Slack is where humans see it and act on it, so every Slack failure is logged loudly and never raised: a run that correctly opened an incident must not fail because Slack was unreachable. `post_incident()` writes the returned `ts`/`channel` back onto the incident and re-persists in the same call, so there is never a posted message with no record behind it.

**Why is the model layer provider-neutral?**
The two calls are a short paragraph and a yes/no — they do not need a frontier model, and tying them to one would mean anyone running this needs a paid account before they see it work. One adapter speaks the OpenAI protocol, which covers a local Ollama, a llama.cpp server, vLLM, and every hosted free tier; a second adapter covers Claude. Swapping providers is three environment variables, and the deterministic fallbacks mean no provider at all is a supported configuration rather than a broken one.

**Why is the corpus fetched rather than vendored?**
The LogHub datasets are third-party research data. Fetching them at setup keeps the repository small and the provenance honest — and the generated fallback means a clone with no network still produces complete log sets, with each file's origin recorded in the manifest.

## Tech Stack

| Layer | Technology |
|---|---|
| Log corpus | [LogHub](https://github.com/logpai/loghub) public system logs, fetched at setup (`scripts/fetch_logs.py`), with a generated fallback |
| Log-set mixing & analysis | Python 3.11 (`logsets/`) — seeded mixing, regex signature catalogue |
| Live streaming | Python 3.11 (`logsets/stream.py`) — rate-paced emission, windowed tailing, backpressure gate |
| Severity classification | Deterministic Python, config-driven (`agent/severity.py` + `config/severity.yml`) |
| Incident record | JSON Schema-validated records on disk, full lifecycle + approval gate (`agent/incident.py`) |
| HTTP server | FastAPI — log-set endpoints and Slack's interactivity endpoint |
| Notifications | Slack bot-token app (`agent/slack_client.py`) — threaded, editable in place, Block Kit. `SLACK_MODE=stub` (default) writes payloads to `reports/slack/` with no live workspace required |
| Narration & resolution judgement | Any model — local Ollama/llama.cpp, a free hosted OpenAI-compatible endpoint, or Claude — with schema-validated output and deterministic fallbacks |
| Testing | pytest 8.x — 262 tests, no network, no services |
