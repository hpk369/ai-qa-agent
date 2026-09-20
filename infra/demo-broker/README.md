# demo-broker — visitor-triggered runs without exposing the VM

> **DROPPED — 2026-09-20.** This component depended on the Hadoop stack in
> `infra/bankdemo/`, which is not being built. See [`ROADMAP.md`](../../ROADMAP.md) §0.
> Kept as a record of the design, not as work to do.

**Status: specification only. Not built.** Depends on roadmap B3 (the run lifecycle) existing
first. Tier 1 — the live demo feed in `infra/bankdemo/IMPLEMENTATION_GUIDE.md` §9.6 — is the
prerequisite and the fallback; this component is strictly additive and the site must keep
working with it removed or down.

## What it is

A small Cloudflare Worker that lets a visitor on the GitHub Pages demo ask for a **fresh run of
the real stack**, and watch it happen, without the VM accepting a single inbound connection.

The VM stays outbound-only, exactly as `../bankdemo/docs/VM_SETUP.md` §4 established. It
*polls* the broker; the broker never reaches the VM. The public attack surface is a stateless
Worker that holds no credentials for anything except itself — a far better place for it than a
host running Hadoop as root.

## The problem this has to solve

`bankdemo run` takes **~13 minutes**. A naive "click → wait → here's your bundle" flow has
terrible conversion: nobody watches a spinner for 13 minutes, and most requests get abandoned
before the run finishes, having consumed the compute anyway.

**So don't hide the 13 minutes — make them the demo.** The orchestrator already emits phase
transitions to `timeline.log` (§8.2), and the monitor already writes alerts to `alerts.log` as
conditions fire (§7.6). Stream both to the broker as they happen, and the visitor watches a
real Hadoop stack boot, ingest, run a batch chain, break, and get collected:

```
  0:12  PREPARE   run R20260919-141203 created
  0:34  BOOT      namenode up · datanode 1/2 · datanode 2/2 · RM up · NM registered
  3:01  FEED      producer started · 118 txn/s · fraud consumer group joined
  3:38  BATCH     FW_EOD_FILE  SUCCESS
  4:52  BATCH     LOAD_EOD     SUCCESS   (312k rows)
  6:20  BATCH     SPK_INGEST   RUNNING   application_1758...
  7:04  ⚠ CRITICAL  HDFS  UNDER_REPLICATED_BLOCKS  value=37 threshold=0
  7:41  BATCH     SPK_INGEST   SUCCESS
  9:02  BATCH     SPK_SETTLE   FAILURE   ← something went wrong
 11:30  COLLECT   evidence bundle assembled
 12:58  READY     open the incident →
```

That is the most convincing thing this project could show a visitor, and it only exists because
the run is genuinely slow and genuinely real. The wait stops being a cost and becomes the
product demo.

## Architecture

```
  visitor ──POST /request────────────────►┐
  (Turnstile token)                       │
                                    ┌─────┴──────────────────┐
  visitor ──GET /status/<ticket>───►│  Cloudflare Worker     │
  (poll 5s, or SSE)                 │  + KV: queue, tickets  │
                                    └─────┬──────────────────┘
                                          │  all VM calls are OUTBOUND
                     ┌────────────────────┘
                     │  GET  /next        (every 2 min, poll token)
                     │  POST /progress    (on each phase transition + alert)
                     │  POST /complete    (HMAC-signed preview JSON)
                     ▼
              bankdemo VM  ── no inbound ports, no public IP exposure
```

Nothing here changes the VM's network posture. `/32` on TCP 22 for you, Tailscale for CI, and
three outbound HTTPS calls for this.

## Hosting

Mount the Worker on a **custom domain** from the Cloudflare zone —
`api.inkandinfra.com/demo/*` — rather than on `*.workers.dev`. Two reasons, and the second is
the one that matters for this project:

- A stable hostname survives account and subdomain changes.
- `workers.dev` is filtered on some corporate networks. This site exists to be opened by
  hiring managers, plausibly from inside a bank, and a demo that silently fails to load there
  is worse than no demo.

Turnstile is configured against the same zone. The demo page stays on GitHub Pages — moving it
to Cloudflare Pages would buy same-origin requests, but a Worker sets its own CORS headers, so
that gain is small and not worth migrating a site that already works and is already linked from
the README.

## Endpoints

| Method | Path | Caller | Notes |
|---|---|---|---|
| `POST` | `/request` | visitor | Body carries **only** a Turnstile token. Returns `{ticket}` |
| `GET` | `/status/<ticket>` | visitor | Returns `{state, queue_position, elapsed, events[]}` |
| `GET` | `/next` | VM | Auth: `Authorization: Bearer $BANKDEMO_POLL_TOKEN`. Returns one ticket or `204` |
| `POST` | `/progress` | VM | `{ticket, elapsed, phase, line}` — appended to the ticket's event list |
| `POST` | `/complete` | VM | `{ticket, outcome, preview}` + `X-Signature: HMAC-SHA256`. Stores the preview |

`state` is one of `queued`, `running`, `ready`, `failed`, `expired`.

### The request body is empty on purpose

`/request` accepts **no parameters**. No seed, no fault selection, no run mode. A visitor who
could request specific faults could map the catalog against the published answers, and a
visitor who could pass a seed could replay one. The broker generates the ticket; the VM chooses
the seed; selection stays salted (§10.2, demo salt). The CLI's own argument validation (§8.1)
is the second layer, but the first layer is simply never accepting the input.

## Abuse control

A public endpoint that causes 13 minutes of compute on a free-tier box needs real limits, in
this order:

| Control | Value | Why |
|---|---|---|
| **Turnstile** | required on `/request` | Cloudflare's captcha, free, native to Workers. The primary defence against scripted abuse |
| **Per-IP rate limit** | 1 request / IP / hour | KV key on a hashed IP with a TTL. Stops a single bored visitor |
| **Queue depth cap** | 3 pending | Beyond that, `/request` returns `queue_full` and the page falls back to Tier 1 |
| **Global daily cap** | 8 visitor runs / day | With cron's 6, that is 14 runs ≈ 3 hours of compute. Enforced in the Worker **and independently on the VM** — never trust the broker alone |
| **Ticket expiry** | 20 min | A run that dies mid-way must not leave the page spinning forever |

The daily cap is about predictability, not cost: extra runs actually *help* against idle
reclamation (§11.2), and bundle egress is trivial against Always Free's allowance.

## VM side

A systemd timer, not a daemon — nothing to supervise, nothing to leak memory.

```
bankdemo-poll.timer     OnUnitActiveSec=2min
bankdemo-poll.service   ExecStart=/opt/bankdemo/bin/bankdemo poll-requests
```

`bankdemo poll-requests` must:

1. **Test the lock without taking it.** `flock -n` on `/run/lock/bankdemo.lock` — if a cron run
   or one of your own practice runs holds it, exit 0 immediately and try again in two minutes.
   Visitor runs are the lowest-priority caller on this box and must never contend.
2. **Check the local daily cap** from `/var/lib/bankdemo/state/demo-runs-<date>` before calling
   out. The Worker's cap can be wrong or bypassed; this one cannot.
3. `GET /next`. On `204`, exit.
4. Run `bankdemo run --source demo` with a random seed, no other arguments.
5. **Stream progress.** A `--progress-url` flag makes the orchestrator POST each `timeline.log`
   phase transition and each `alerts.log` line as it is written. Failures here are logged and
   ignored — the broker being unreachable must never fail a run.
6. `POST /complete` with the demo preview (§9.6) and the outcome, signed. On a failed or aborted
   run, post the failure honestly: an `INFRA_ERROR` run is still evidence, and pretending
   otherwise would be the one dishonest thing in this design.

`--source demo` is a demo-class run (§9.6): it publishes a preview *with* the answer, uses
`BANKDEMO_DEMO_SALT`, and appends to the same feed Tier 1 serves. A visitor-triggered run is
therefore just a feed entry that someone asked for — which means the page renders it with code
that already exists.

## Secrets

| Secret | Held by | Purpose |
|---|---|---|
| `BANKDEMO_POLL_TOKEN` | VM (`/etc/bankdemo/secrets.env`, 0640) + Worker | Bearer token for `/next` and `/progress` |
| `BANKDEMO_SUBMIT_KEY` | same | HMAC key for `/complete`, so a leaked poll token cannot forge a result |
| `TURNSTILE_SECRET` | Worker only | Captcha verification |

Two separate secrets rather than one: the poll token is used far more often and has a wider
blast radius if it leaks, but on its own it only lets an attacker *drain* the queue, not
*publish* to the feed. Publishing requires the submit key.

Both VM-held values go in the existing `secrets.env`, which §9.1 already refuses to collect
into any bundle. Add `BANKDEMO_POLL_TOKEN` and `BANKDEMO_SUBMIT_KEY` to the redaction pattern
set while you are there.

Note what is *not* in that table: no R2 access key, no GitHub token, no Cloudflare API token.
The VM holds two scoped shared secrets for one Worker and nothing else. Keep it that way — it
is the machine running Hadoop as root, and the one an attacker would most want a credential
from.

## Relationship to R2

The broker does **not** own bundle storage. Tarballs go to R2 via the hourly harvest workflow
in §9.6, which holds the R2 credentials as Actions secrets — that path belongs to Tier 1 and
must keep working with this component deleted.

One optional refinement once both exist: `/complete` can return a **presigned R2 PUT URL**, so
a visitor-triggered run uploads its own tarball immediately rather than waiting up to an hour
for the next harvest. This is worth doing only because the visitor just watched that specific
run finish and will plausibly want its evidence now. It adds no credential to the VM — the
presigned URL is minted by the Worker from its own R2 binding and is single-use — and it adds
no dependency, since the upload is a plain HTTP PUT with `requests`. If it is not built, the
harvest picks the tarball up on its normal cycle and nothing is broken.

## Storage

Cloudflare KV is sufficient and definitely free-tier: the queue is a single JSON array key
(peak depth 3), and each ticket is one key with a TTL. Write volume is ~10 requests/day plus
progress events — well inside the free allowance, and the 1-write-per-second-per-key limit is
irrelevant at this scale. KV's eventual consistency does not matter when the VM polls every
120 seconds.

Verify current free-tier limits when you build it rather than trusting this table; Cloudflare
revises them. If Durable Objects are free-tier by then, they are a better fit for the queue
(strongly consistent, single-threaded) — but they are not required.

## Graceful degradation

Every failure mode must land back on Tier 1, which needs no broker at all:

| Failure | Visitor sees |
|---|---|
| Worker down / unreachable | The feed, with the request button hidden |
| Queue full or daily cap hit | "The rig is busy — here's a run from 40 minutes ago" + the feed |
| Run fails | The real failure, published as a feed entry, with what broke |
| Ticket expires | "That run didn't finish. Here's a recent one" + the feed |

**Tier 1 must never depend on Tier 2.** If this whole component is deleted, the demo site
loses a button and nothing else.

## Build order

1. Tier 1 feed working and populated by cron (roadmap B6.1).
2. Worker with `/request` + `/status` only, returning a canned ticket. Prove Turnstile, the
   rate limits, and the page's polling loop against a stub.
3. `/next` + VM poller, still with no run — confirm the lock test, the daily cap, and that a
   busy VM never picks up work.
4. Wire the real `bankdemo run`, then `/progress` streaming last. The live phase feed is the
   valuable part but also the only part that touches the orchestrator, so it lands once
   everything around it is known-good.

## Honest notes

- **This is spec, not code.** Nothing in `infra/bankdemo/` is built yet either; this depends on
  B3 at minimum and realistically lands after B6.1.
- **Cloudflare is a dependency** on a project that otherwise has only GitHub and OCI. It is a
  smaller cost than it was — the account and domain already exist, and R2 is already carrying
  bundle storage for Tier 1 (§9.6) — but it is still a third vendor in the critical path of the
  demo. The justification is that this is the only design found that gives a visitor-triggered
  run while keeping the VM's inbound exposure at zero; a broker hosted *on* the VM would
  reintroduce exactly what `VM_SETUP.md` §4 removed.
- **The VM access path is deliberately not Cloudflare.** Tailscale keeps the SSH/CI path
  (`VM_SETUP.md` §4a) even though Cloudflare Tunnel could carry it, and the Hadoop web UIs stay
  behind an SSH tunnel as `../bankdemo/CLAUDE.md` requires. Consolidating was considered and
  declined: Tunnel plus Access would also make the UIs remotely viewable, which is a real demo
  asset, but it changes a constraint marked "do not violate" and the existing path works.
- **The live phase feed is the reason to build this at all.** Without it, Tier 2 is a slower
  way to get what Tier 1 already delivers instantly. If step 4 above proves impractical,
  reconsider whether to ship the button rather than shipping it without the stream.
