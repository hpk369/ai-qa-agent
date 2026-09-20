# CLAUDE.md — infra/bankdemo (Track B)

> **DROPPED — 2026-09-20.** The real Hadoop stack this document specifies is not being
> built. See [`ROADMAP.md`](../../ROADMAP.md) §0 for the reasoning. The project's scope is
> now one loop: a set of logs in, a Slack alert out, and the logs behind it downloadable
> (`logsets/`, `scripts/logset.py`). This file is kept as a record of the design decisions,
> not as work to do.

Scoped rules for everything under `infra/bankdemo/`. The repo-wide rules in
`/IMPLEMENTATION.md` §0 ("Ground rules") still apply; where the two disagree, this
file wins **inside this directory only**.

Read this file fully before every task. The full specification lives in
`IMPLEMENTATION_GUIDE.md`; progress lives in `docs/PROGRESS.md`; the phase order and
its relationship to the rest of the repo live in `/ROADMAP.md`.

## What this is

The Hadoop substrate for Track B of `expansion-plan.md`. A single-VM,
pseudo-distributed banking stack (HDFS, YARN, Spark, Kafka, PostgreSQL) on an Oracle
Cloud Always Free Ampere A1 VM. On demand it runs a 10–15 minute simulated "business
day" (card transactions, EOD batch chain, fraud stream), injects 1–3 seeded, randomly
chosen production incidents, records all logs and diagnostics into a bundle, then
resets.

The bundle has **two consumers**, and both matter:

1. **A human** (the owner) practising L2 application-support triage against it, graded
   against a hidden answer key.
2. **The triage agent** already built in this repo (`agent/`), which ingests the same
   bundle and produces an incident record. The answer key is ground truth for both, so
   it is what makes `docs/scorecard.md` (roadmap Phase B5) a real measurement rather
   than a self-assessment.

This is why the bundle format is a contract, not an implementation detail. See
`IMPLEMENTATION_GUIDE.md` §9.2 and §9.5.

## Hard constraints (do not violate)

- Target host: Oracle Linux 9, **aarch64**, **2 OCPU / 12 GB RAM**, hostname `bankdemo`.
  Every memory setting must fit the budget in `IMPLEMENTATION_GUIDE.md` §2.3, which
  budgets **resident set size, not heap**. A new JVM costs its heap plus ~300 MB.
- A normal run (exit 0, boot → faults → collect → stop) must finish within **15:00**
  wall clock, and the phase budgets in §8.2 must sum to **≤ 13:00**. A baseline run
  with no faults must finish within **12:00**. The aborted path (exit 3) may run to
  ~16:30 before SIGKILL; that is accepted and documented, and is not a "normal run".
- Java **11** only. Kafka is pinned to **3.9.x** (KRaft-native, Java 11-clean)
  precisely so that a second JDK is never needed. Do not introduce Java 17. Do not
  bump Kafka to 4.x without re-reading §6.4 — 4.x drops broker-side support for
  pre-2.1 clients, which breaks `kafka-python`, which the parent repo already depends on.
- Core daemons use `Restart=no`. Auto-restart would silently "heal" injected faults.
- Only TCP 22 is reachable from outside. Web UIs are accessed by SSH tunnel only.
- Every injected fault must have an idempotent `revert.sh`. Reset must call every
  fault's revert, not only the ones that ran — so **every revert must detect current
  state first and be a fast no-op when the system is already clean** (§10.1).
- The answer key is **never** written into the run bundle or uploaded anywhere, and
  **the seed alone must not reveal it**. Fault selection is seeded from
  `HMAC(BANKDEMO_FAULT_SALT, seed)`, with the salt in `/etc/bankdemo/secrets.env`
  (§10.2). Generator randomness stays on the bare seed so same-seed data stays
  reproducible.
- The bundle is assembled in `/data/runs/<run_id>.bundle/` and **never** by tarring
  `RUN_DIR`. `RUN_DIR/state/` holds the feed profile and fault action logs; tarring it
  would publish the answers (§9.2).

## Safety rules for Claude Code

- **Never run install, fault, reset, or bankdemo commands on the local machine.**
  This subtree is developed locally and executed on the VM via `make -C infra/bankdemo deploy` / `ssh`.
- All host-mutating scripts must start with `require_bankdemo_host` from
  `install/lib/common.sh`, which exits unless `/etc/bankdemo/host-marker` exists.
  `00-preflight.sh` is the sole exemption: it creates the marker, and must therefore
  do its own arch/OS/RAM checks before writing anything.
- Never create, delete, or resize OCI resources. Provisioning is a human step.
- Never commit secrets. `.env`, `*.pem`, `secrets.env` are gitignored.
- Do not open firewall ports, disable SELinux, or disable firewalld to make
  something work. Find the real fix and document it in `docs/RUNBOOK.md`.
- Stop and ask the human at every step marked **[HUMAN]** in the guide.
- Never `mkfs` a device identified only by kernel name (`/dev/sdb`). Use the OCI
  consistent device path and confirm size and empty state first (§4.2).

## Workflow

1. Work one phase at a time, in order. Start each phase in plan mode: restate the
   phase goal, list files to create/change, list acceptance checks.
2. Implement, then deploy to the VM and run the phase's acceptance checks.
3. Paste the acceptance-check output into `docs/PROGRESS.md` under the phase.
4. Commit with `feat(bankdemo): <summary>` or `phase-N: <summary>`; match the parent
   repo's conventional-commit style. Do not start the next phase until all checks for
   the current one pass.
5. When you discover a gotcha, add it to `docs/RUNBOOK.md` (symptom → cause → fix).

## Code conventions

- Bash: `#!/usr/bin/env bash` + `set -Eeuo pipefail`, source `install/lib/common.sh`,
  use its `log_info/log_warn/log_error` helpers, pass `shellcheck` with no warnings.
- Every installer step is idempotent: re-running `install.sh` on a finished host is a no-op.
- Python 3.11, package `bankdemo/`, formatted and linted with `ruff`, type hints on
  public functions, tests in `tests/` with `pytest`. Stdlib first; allowed deps are
  listed in `requirements.txt` (pyyaml, requests, kafka-python, psycopg[binary]).
  `pyspark` is a **dev-only** dependency for linting and IDE resolution — never install
  it into the runtime venv, or it will shadow `/opt/spark/python` and skew versions.
- All randomness flows from explicitly passed `random.Random` instances. No module-level
  `random` calls. Two independent streams: the **data** stream seeded from the bare seed
  (so same seed ⇒ same EOD detail rows) and the **fault** stream seeded from
  `HMAC(salt, seed)` (so a published seed reveals nothing). Same seed + same fault list
  ⇒ identical schedule and feed profile.
- Log line format everywhere we control: `ISO8601_with_offset|LEVEL|component|message`.
  This matches nothing the parent repo emits by design — the bundle is a *source* for
  the agent, not an extension of its log format.
- Config templates use `envsubst` with an explicit variable list; never bare `envsubst`.
  Rendered configs must contain no timestamps or other per-render values, because §8.5
  asserts a live config re-renders byte-identically.
- `render_template` returns **0 = unchanged, 10 = content changed, 1 = error**. Never
  overload 0 to mean "changed"; under `set -e` that turns an unchanged file into an abort.
- Versions are pinned only in `config/versions.env`; downloads verify SHA-512 against
  the published checksum, and the guide records that this is an integrity check, not an
  authenticity one (same host serves both).
- Parse untrusted YAML with `yaml.safe_load`, never `yaml.load`. `bankdemo grade` runs
  as root against a user-supplied file.

## Useful commands (run on VM unless noted)

```
make lint                  # local: shellcheck + ruff + pytest
make deploy                # local: rsync subtree to VM and run install.sh
bankdemo health            # service + HDFS + YARN + Kafka + Postgres checks
bankdemo run [--seed N] [--faults F01,F06] [--no-faults]
bankdemo test-fault F04    # single-fault run that asserts evidence signatures
bankdemo reset [--deep]
bankdemo reveal <run_id>   # prints answer key (interactive use only)
bankdemo grade <run_id> <rca.yaml>
```
