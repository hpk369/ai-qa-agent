# ROADMAP — ai-qa-agent

**Status date:** 2026-09-17
**Scope:** what is built, what is next, and how `infra/bankdemo/` (Track B) fits into it.

This document supersedes the phase tables in `expansion-plan.md` §6 and `IMPLEMENTATION.md`
Phases 2–5 for anything involving the Hadoop stack. Those documents remain accurate about
*intent*; this one is accurate about *sequence*, because it was written against the source
rather than against the README.

---

## 1. Where the repository actually is

Verified against the working tree, not the README: `234 passed` in `tests/pytest`.

| Area | State | Evidence |
|---|---|---|
| Severity classification | **Done.** Deterministic, config-driven | `agent/severity.py`, `config/severity.yml`, `tests/pytest/test_severity.py` |
| Incident record | **Done.** Schema-validated, full lifecycle | `agent/incident.py`, `schemas/incident.schema.json` |
| Approval gate | **Done.** Double-decision rejected, not ignored | `agent/incident.py::record_approval_decision` |
| Slack incident channel | **Code done, never run live.** `SLACK_MODE=stub` throughout | `agent/slack_client.py`, `slack_blocks.py`, `slack_verify.py` |
| Runbooks | **Done.** 5 runbooks, deterministic selection | `agent/runbooks.py`, `docs/runbooks/RB-00{1..5}` |
| Evidence bundle | **Done, against the mock stack** | `agent/evidence.py`, `scripts/first-15-minutes.sh` |
| MTTA / MTTR | **Done.** Real Slack-thread derived, not synthetic | `agent/incident.py::sync_slack_engagement`, `scripts/incident_metrics.py` |
| Agent tools | **Done, against Postgres/Kafka mock** | `agent_tools/{sql_validator,log_analyser,schema_comparator}.py` |
| Pipeline under test | **Mock only.** No Hadoop anywhere | `mock_pipeline/`, 5 failure modes |
| Hadoop stack | **Not started** | — |
| Scorecard | **Not started.** No ground truth to score against yet | — |

**The honest summary:** the triage layer is real and tested; the thing it triages is a Python
mock. Every remaining phase is about replacing the mock with something that breaks for real,
and then measuring how well the triage layer handles it.

### The two human prerequisites that gate everything

Neither is a coding task, both block a phase, and both have been outstanding since Phase 1:

1. **Slack workspace + app + bot token + 4 channels + tunnel.** `docs/PHASE1_SETUP.md` lists
   every step. Until this exists, Phase 1 is "code complete, unverified" — which is an honest
   thing to say in an interview, but a weaker one than "running."
2. **OCI VM provisioned.** Walkthrough in
   [`infra/bankdemo/docs/VM_SETUP.md`](infra/bankdemo/docs/VM_SETUP.md); spec in
   `infra/bankdemo/IMPLEMENTATION_GUIDE.md` §4.2. Blocks all of Track B.

---

## 2. The decision that reshapes the plan

`expansion-plan.md` §5 put the Hadoop stack in Docker Compose on the laptop, behind a
`--profile hadoop`. `infra/bankdemo/` puts it on a free OCI VM instead. **The VM wins**, for
three reasons that are worth being able to state out loud:

- **The laptop can't hold it.** The expansion plan's own resource check says HDFS + YARN +
  Hive + Kafka + Postgres + n8n + the agent needs ~12 GB. That is the entire VM budget, with
  nothing left for a browser, an IDE, or the thing you are demoing from.
- **A VM breaks the way production breaks.** Disk fills, inodes exhaust, the OOM killer picks
  a victim, systemd units fail to start, SELinux denies a path. Those are the incidents an L2
  analyst actually sees, and most of them are hard or artificial to stage in Compose.
- **It runs on a schedule without you.** Six cron runs a day (§11.2) means bundles accumulate
  while you sleep, which is what makes a scorecard with real N possible.

The cost, stated plainly: `docker compose --profile hadoop` is now **dead** and should not be
built. `--profile lite` stays, permanently, because `IMPLEMENTATION.md` ground rule 3 says a
reviewer cloning the repo must be able to run *something* — and after this change, the lite
Postgres path is that something. It is no longer a migration waypoint; it is the demo path.

### What this deletes from the old plan

| Old plan item | Fate |
|---|---|
| T2.1 Compose profiles (`--profile hadoop`) | **Dropped.** Lite profile stays as the permanent demo path |
| T2.2 HDFS + YARN in Compose | **Replaced** by bankdemo Phases 2–3 |
| T2.3 Hive metastore on the existing Postgres | **Dropped.** bankdemo has no Hive (`CLAUDE.md` non-goals: doesn't fit in 12 GB). Spark SQL over HDFS Parquet covers the same ground; say so rather than implying Hive ran |
| T2.4 `spark_jobs/cx_customer_load.py` | **Replaced** by `infra/bankdemo/jobs/spark/{ingest,enrich,settle}.py` |
| T3.5 Retire the Postgres pipeline | **Dropped.** It becomes the lite demo path instead |
| Oozie (Phase 4) | **Replaced** by bankdemo's `scheduler/jobs.yaml`, which is Autosys-flavoured — closer to the actual JD than Oozie is |

`expansion-plan.md` §5's *analysis* is still worth keeping and still worth discussing in an
interview (especially the Impala substitution reasoning). It is the *build instruction* that
is superseded.

---

## 3. The architecture this converges on

```
  infra/bankdemo/  (OCI VM, on demand or 6× daily cron)
  ┌──────────────────────────────────────────────────────┐
  │  HDFS · YARN · Spark · Kafka · PostgreSQL            │
  │  simulated business day + 1–3 seeded faults (F01–F17)│
  │                                                      │
  │  ├─► run bundle  (logs, snapshots, ticket.json)  ────┼──┬──► a human practising triage
  │  └─► answer key  (never bundled, salted selection)   │  │    → RCA_TEMPLATE.yaml
  └──────────────────────────────────────────────────────┘  │
                                                            └──► agent/  (this repo)
                                                                 → severity.py
                                                                 → incident.py
                                                                 → runbooks.py
                                                                 → Slack thread
                            ┌───────────────────────────────────────┘
                            ▼
                    grading.py scores BOTH against the same answer key
                            │
                            ▼
                    docs/scorecard.md   ← the deliverable that makes this project
                                          measurably true rather than merely built
```

**The key insight, and the reason to fold rather than separate:** the answer key was designed
to grade a human. It grades the agent equally well. That turns `docs/scorecard.md` — currently
an unstarted Phase 5 item with no way to produce a defensible number — into a straightforward
join on `run_id`. Nothing else in either project gives you ground truth.

### What must NOT be rebuilt

`infra/bankdemo/IMPLEMENTATION_GUIDE.md` was written standalone and therefore specifies
several things this repo already has working. Build the bankdemo side of each, not a second
copy of the agent side:

| bankdemo specifies | Already exists here | Resolution |
|---|---|---|
| `ticket.json` with a severity | `agent/severity.py` + `config/severity.yml` | Ticket carries **no** severity. The agent derives it from signals; pre-labelling makes the scorecard circular (§9.5) |
| Evidence collection (`collector.py`) | `agent/evidence.py` | Both stay. bankdemo's runs **on the VM with the stack live** and captures what only exists there (JMX, `yarn logs`, journald). `agent/evidence.py` consumes the resulting bundle. Different jobs, similar names |
| `docs/FAULTS.md`, `docs/RUNBOOK.md` | `docs/runbooks/RB-00*.md` | bankdemo's are operator docs for the rig. The **triage** runbooks stay in `docs/runbooks/` and get extended with the new fault classes in B4 |
| Answer key + `grade` | — | New, and the most valuable single thing bankdemo contributes |
| `monitor.py` alerting | `agent_tools/log_analyser.py` | bankdemo's monitor writes `alerts.log` *into the bundle*; the log analyser reads it. Producer and consumer |

---

## 4. Roadmap

Phases are lettered to avoid collision with `IMPLEMENTATION.md`'s existing Phase 0–5 numbering.

### B0 — Reconcile the docs · ½ evening · no prerequisites

The repo currently contains three documents describing three different Phase 2s. Fix that
before writing code against any of them.

- [x] Fold bankdemo in at `infra/bankdemo/` with a directory-scoped `CLAUDE.md`
- [x] Apply the review fixes to the guide (Appendix C lists all 20)
- [x] Write this roadmap
- [ ] Add supersession notes to `expansion-plan.md` §5/§6 and `IMPLEMENTATION.md` Phases 2–4
- [ ] `README.md` phase status: state that Track B moved to a VM and the Compose Hadoop
      profile was dropped, with the reasoning from §2 above

**Done when:** no document in the repo instructs anyone to build Hadoop in Docker Compose.

### B1 — Close the Slack prerequisite · 1 evening · **[HUMAN]**

Not new code — `agent/slack_*.py` is written and tested. This converts "code complete" into
"has run." Follow `docs/PHASE1_SETUP.md`, then a stub-vs-live diff run.

**Done when:** one incident opens, is acknowledged, approved, and resolved in a real Slack
thread, with the parent edited in place and real MTTA/MTTR. Screenshot it for the README.

**Why first:** it is the cheapest remaining item, it is already paid for, and it unblocks
nothing else — so it will never get done if it is not done now.

### B2 — bankdemo Phases 0–3.5 · 2–4 evenings · needs the VM **[HUMAN]**

Provisioning walkthrough: **[`infra/bankdemo/docs/VM_SETUP.md`](infra/bankdemo/docs/VM_SETUP.md)**.
Its §0 settles the shape-allowance question in §6's risk table before anything is built on it.

Then follow `infra/bankdemo/IMPLEMENTATION_GUIDE.md` §4–§6 and its Appendix A prompts.

Sequence: scaffold → VM provisioning **[HUMAN]** → OS/storage → stack → **budget truth gate**.

**Do not skip §6.8.** It is the gate that proves the memory budget is real before five phases
of code depend on it. The two faults that could disprove it (F05, F16) are not built until B4.

**Done when:** `bankdemo health` is green, SparkPi completes on YARN in under 90 s, and the
measured daemon RSS is within 15% of §2.3's table. If it isn't, re-derive §2.3 and say so.

### B3 — bankdemo Phases 4–6 · 3–5 evenings

Workload, run lifecycle, evidence collection. This is the largest block and has no shortcuts.

The one piece to get right early: **`contract/bundle_v1.md` (§9.5)**. Write it before
`collector.py`, not after. It is the interface `agent/` consumes, and retrofitting a contract
onto an existing output format is how you end up with two formats.

**Done when:** three consecutive `--no-faults` runs complete in ≤ 12:00 with zero alerts, and
the bundle contains no answer-key trace (the `state/` leak test in §9's acceptance).

### B4 — bankdemo Phase 7, faults · 4–6 evenings · the long pole

17 faults, each needing 3 consecutive clean `test-fault` runs. Expect tuning.

Build order is **F01, F05, F16** first — F05 and F16 are the budget-breakers, and finding out
at fault 15 that the memory model is wrong means rewriting the workload rather than tuning a
fault. Then the rest, F08 last.

Concurrently, extend `docs/runbooks/` to cover the new fault classes. The existing five map
onto bankdemo faults only partially:

| Existing runbook | bankdemo faults it covers | Gap |
|---|---|---|
| RB-001 row shortfall | F10, F11 | — |
| RB-002 schema drift | F09 | — |
| RB-003 null spike | — | No bankdemo equivalent; keep for the lite path |
| RB-004 consumer lag | F06, F07 | — |
| RB-005 job failure | F04, F05, F12, F14 | Too broad — split |
| **needed** | F01, F02, F03 | HDFS health: dead DataNode, safe mode, volume full |
| **needed** | F13 | Permission drift / bad change |
| **needed** | F15, F16, F17 | Host-level: inodes, memory, network |

**Done when:** every enabled fault passes `test-fault` 3× consecutively, and 10 random-seed
runs finish in budget with clean invariants.

### B5 — Integration: the agent ingests real bundles · 2–3 evenings · **the payoff**

Everything before this is infrastructure. This is where the two halves meet.

- **B5.1 — Bundle adapter.** `agent/bundle.py`: read a `bundle_v1` tarball, emit the signals
  dict `agent/severity.py` already expects. No changes to `severity.py` — if its interface
  doesn't fit, that is a finding about the interface, worth recording.
- **B5.2 — Rewrite the three tools against bundle evidence.** This is `IMPLEMENTATION.md`
  T3.1–T3.3, finally buildable: SQL Validator → Recon Checker (reads `recon_result` +
  `settlement_summary` snapshots), Log Analyser → YARN Log Analyser (reads `jobs/yarn_*.log`
  for real container-kill signatures), Schema Comparator → parquet/landing schema drift (F09).
- **B5.3 — Map faults to severities** and defend each mapping in `config/severity.yml`.
- **B5.4 — Extend `grading.py` with an agent adapter** so the same scoring function grades a
  human's `rca.yaml` and the agent's incident record.
- **B5.5 — `docs/scorecard.md`** across ≥ 40 cron-generated runs: classification accuracy,
  every miss listed and analysed, false-positive rate. A scorecard without misses reads as
  fabricated — `IMPLEMENTATION.md` T5.2 is right about that.

**Done when:** a bundle lands, the agent opens a Slack incident from it unattended, and the
scorecard has a real N with real misses.

### B6 — Hardening, publication, honesty pass · 2–3 evenings

bankdemo Phases 8–9 (cron, GitHub Actions over Tailscale, fail2ban, reboot test, rebuild
drill), then a full-repo claim audit against `IMPLEMENTATION.md` ground rule 2: no simulated
component described as real, no Hive implied, the lite path's status stated plainly, and the
demo page updated to reflect what the finished system actually does.

**B6.1 — The live demo feed.** Cron runs six times a day and each picks 1–3 faults at random,
so the last 20 runs are a continuously replenishing pool of real, distinct incidents. Serve the
demo site a random recent one, instantly, with a "different incident" control
(`infra/bankdemo/IMPLEMENTATION_GUIDE.md` §9.6).

This is what "let visitors get a fresh set of logs" actually wants. A literal request-a-run
button would make the visitor wait ~13 minutes for the stack to finish, and nobody waits — but
they cannot tell whether the incident they're handed was generated on their click or ninety
minutes ago. Same artefact, no queue, no wait, and no new attack surface on a box that is now
deliberately outbound-only.

The design splits by size so nothing needs CORS: a few-KB `demo_preview.json` per run is
committed into `docs/demo-feed/` (same origin as Pages, so `fetch()` just works), while the
60 MB tarball stays on the VM with a curated few attached to a GitHub Release as plain download
links. A scheduled workflow harvests previews over the tailnet, so the VM never holds a GitHub
token or push access.

Two properties are load-bearing and must be verified before the first publish, not after:
§9.1's redaction strips credentials, and §10.2's salted selection means a published seed does
not reveal the answer key. Demo runs additionally use a **separate salt** from the practice
pool, so publishing demo answers — which is the demo's whole job — can never leak a run you
intend to practise on.

**B6.3 — Visitor-triggered runs (optional).** `infra/demo-broker/README.md` specifies a
Cloudflare Worker that lets a visitor request a fresh run and **watch it happen live** — phase
transitions and alerts streamed as they are written, so they see a real Hadoop stack boot,
ingest, break and get collected. The VM stays outbound-only: it polls the broker, the broker
never reaches it.

Build this only after B6.1, and only for the live stream. Without the stream it is a slower way
to get what the feed already delivers instantly; with it, the 13-minute run stops being a cost
and becomes the most convincing thing the site can show. It is strictly additive — if the whole
component is deleted, the demo loses a button and nothing else.

It also adds Cloudflare as a dependency to a project that currently has only GitHub and OCI.
That is a real cost, justified only because it is the one design that gives a public trigger
while keeping the VM's inbound exposure at zero.

**B6.2 — Make the rebuild drill a reviewer path, not just a test.** `infra/bankdemo/docs/VM_SETUP.md`
plus `make deploy` should take a stranger with an Always Free account from nothing to a running
stack. The Phase 9 drill already proves this; B6.2 is stating it in the README as a supported
route and fixing whatever the drill exposes.

A public trigger is offered only through B6.3's broker, and only under its constraints: no
visitor-supplied arguments of any kind, Turnstile plus per-IP and global daily caps, and a VM
poller that yields to every other caller on the box. Direct public access to `bankdemo run` is
never offered — it is root-executed, lock-serialised, argument-taking, and sits on a box holding
the answer keys. See `infra/bankdemo/docs/VM_SETUP.md` §14.

---

## 5. Sequencing at a glance

```
B0 docs ──┬─► B1 Slack [HUMAN]  ────────────────────────────────┐
          │                                                     │
          └─► B2 VM + stack [HUMAN] ─► B3 workload ─► B4 faults ─┴─► B5 integration ─► B6 hardening
                     │                                                    ▲
                     └── §6.8 budget gate ── must pass before B3          │
                                                                          │
                              the scorecard is only possible here ────────┘
```

B1 is independent of B2–B4 and can be done in any gap. Everything else is strictly ordered.

**Rough total: 12–20 focused evenings**, dominated by B4. If time runs short, the honest
partial is B0 → B1 → B2 → B3 with a reduced fault catalog (F01, F10, F12, F13 alone still
demonstrate HDFS, data, database and change-management incidents) and a small-N scorecard.
A working rig with four faults and a real scorecard beats seventeen faults and no measurement.

---

## 6. Risks, ranked

| Risk | Impact | Mitigation |
|---|---|---|
| **12 GB is genuinely too small** | Fatal to Track B | §6.8 gate at B2 catches it before B3. Fallback: drop to one DataNode (loses F01/F03 as designed), or Spark local mode (loses YARN — the single highest-value component, so prefer the former) |
| **Always Free shape assumption wrong** | Re-derives every memory number | Verify the tenancy's actual A1 allowance in B2 before provisioning. If it's 4 OCPU / 24 GB, this all gets easier |
| **F05/F08/F16 never reproduce reliably** | Lose 3 of 17 faults | `enabled: false` + RUNBOOK note is an accepted outcome, already specified. Not a project risk |
| **VM reclaimed by Oracle for idleness** | Rebuild | Six cron runs/day = 6.3% duty cycle, just over the 95th-percentile threshold. The idempotent installer is the real insurance |
| **B4 sprawls** | Project stalls at 80% | Timebox each fault. Three consecutive `test-fault` passes is the definition of done — resist tuning past it |
| **Scorecard shows the agent performing poorly** | Uncomfortable | This is a *result*, not a failure. Publishing misses with analysis is more credible than a suspiciously clean number, and the analysis is the interview material |

---

## 7. Open questions

1. **Does `agent/severity.py`'s signals interface survive contact with real bundle evidence?**
   It was designed against `mock_pipeline`'s five modes. B5.1 will answer it. Record the
   mismatch rather than quietly reshaping the bundle to fit.
2. **Do both trigger paths survive?** bankdemo is cron/SSH/Actions-triggered; the agent is
   n8n-webhook-triggered. Simplest wiring: bankdemo's CLEANUP posts the bundle path to the
   existing n8n webhook. Decide in B5.1 — and `docs/workflow-map.md` will need updating either
   way, since it currently describes only the mock path.
3. **Does n8n stay?** After T1.4 it no longer touches Slack, and bankdemo's scheduler is its
   own orchestrator. It may end up a thin bundle-arrival trigger. That is a fine outcome, but
   decide it deliberately in B5 rather than letting it decay.
