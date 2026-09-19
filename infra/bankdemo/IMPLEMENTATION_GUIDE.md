# bankdemo — Implementation Guide (Track B substrate)

A phase-by-phase specification for building an on-demand, incident-generating
Hadoop banking stack on one Oracle Cloud Always Free VM. Written to be executed by
Claude Code, with human checkpoints marked **[HUMAN]**.

**Where this sits.** This is Track B of `expansion-plan.md`, realised on a VM rather
than in Docker Compose. It lives at `infra/bankdemo/` inside `hpk369/ai-qa-agent`. It
supplies the thing the repo does not yet have: a real Hadoop stack that breaks in real
ways. It does **not** re-implement triage. Severity classification, the incident record,
runbooks, the Slack incident channel, and MTTA/MTTR already exist and work in `agent/`;
this subtree produces the evidence they consume. See `/ROADMAP.md` for the full
sequencing and for which existing modules each phase feeds.

Two consumers read every bundle: a human practising triage, and the agent in `agent/`.
The answer key is ground truth for both, which is what turns `docs/scorecard.md` into a
measurement instead of a self-assessment. Keep §9.2 and §9.5 honest for that reason.

---

## 0. How to use this guide with Claude Code

1. Work inside `infra/bankdemo/` in the `ai-qa-agent` repo. `infra/bankdemo/CLAUDE.md`
   is the directory-scoped ruleset; the repo-wide ground rules in `/IMPLEMENTATION.md` §0
   still apply.
2. Complete Phase 1 (VM provisioning) yourself; it is entirely **[HUMAN]**.
3. For each later phase, open Claude Code in the repo and paste the matching prompt from
   **Appendix A**. Ask it to plan first, then implement.
4. Don't let a phase close until every acceptance check passes and the output is recorded
   in `docs/PROGRESS.md`.
5. Expect Phase 7 (faults) to need tuning. Some faults only reproduce reliably after
   adjusting data volume or limits. The evidence-signature tests (§10.4) decide when a fault
   is done.

Estimated effort: 2–4 focused evenings for Phases 0–4, and another 2–3 for Phases 5–9.

Phase map: 0 scaffold · 1 VM **[HUMAN]** · 2 OS/storage · 3 stack · **3.5 budget truth** · 4 workload · 5 run lifecycle & reset · 6 evidence collection & grading · 7 faults · 8 triggering (cron, GitHub Actions) · 9 hardening & docs.

**Phase 3.5 is not optional.** It is the gate that proves §2.3's memory budget is real
before five phases of code are written against it. See §6.8.

---

## 1. Goals, non-goals, constraints

### 1.1 Goals

- One command (`bankdemo run`) produces a realistic 10–15 minute banking "business day"
  on Hadoop, with 1–3 randomly selected production incidents.
- Each run leaves a self-contained **evidence bundle** (logs, snapshots, alerts, a
  ServiceNow-style ticket) that you triage without knowing the cause.
- Runs are reproducible from a seed. A separate answer key (never in the bundle) supports
  self-grading.
- Runs can be triggered by SSH, by cron, or from a GitHub Actions button.
- The VM returns to a known-good baseline after every run, and can be rebuilt from
  scratch by re-running the installer.

### 1.2 Non-goals

- No Kerberos, HA NameNode, Ranger, or Hive Server. These don't fit in 12 GB.
- Not a performance benchmark. Data volumes stay small on purpose.
- No multi-VM cluster.

### 1.3 Platform constraints (as of September 2026)

| Item | Value | Implication |
|---|---|---|
| Shape | VM.Standard.A1.Flex, **2 OCPU, 12 GB** | Strict memory budget (§2.3). Small Spark jobs. **Verify this against your own tenancy before Phase 2** — if the Always Free A1 allowance is actually 4 OCPU / 24 GB, re-derive §2.3 with the headroom and relax §8.2; most of the tightness in this guide exists only because of the 12 GB figure |
| CPU arch | aarch64 | Use the Hadoop aarch64 tarball. Python deps must have aarch64 wheels or be pure Python |
| OS | Oracle Linux 9 (RHEL-compatible) | dnf, systemd, firewalld, SELinux enforcing |
| Storage | Boot volume + one attached block volume (50 GB suggested), within the Always Free block storage allowance | All service data on `/data` |
| Idle reclamation | Always Free VMs with low 95th-percentile CPU, memory, and network over 7 days may be reclaimed | The criterion is a **95th percentile**, so you must be busy for more than 5% of the week to clear it. Three 15-minute runs a day is 3.1% — not enough on its own. §11.2 therefore schedules six runs a day; treat reclamation as a risk you mitigate, not one you have solved. The installer is fully idempotent, so rebuilds are cheap |
| Capacity | Arm capacity errors happen in busy regions | Retry a different fault domain or availability domain |

---

## 2. Target architecture

### 2.1 Logical view

```
                    ┌────────────── bankdemo VM (OL9 aarch64, 2 OCPU / 12 GB) ───────────────┐
 GitHub Actions ──ssh──►  bankdemo CLI (Python orchestrator, run lifecycle, monitor, collector)│
 cron ─────────────────►        │                                                            │
                        │   ┌────┴─────────────── simulated business day ──────────────┐     │
                        │   │ eod_file generator ─► /data/landing (ext4, few inodes)    │     │
                        │   │ txn_stream producer ─► Kafka cards.txn.auth ─► fraud_feed │     │
                        │   │ scheduler (Autosys-like): FW_EOD → LOAD → SPK_INGEST      │     │
                        │   │          → SPK_ENRICH → SPK_SETTLE → RECON                │     │
                        │   └───────────────────────────────────────────────────────────┘     │
                        │   HDFS: NameNode + DataNode1 + DataNode2 (replication 2)            │
                        │   YARN: ResourceManager + NodeManager (queues: etl, adhoc)          │
                        │   Kafka 3.9 KRaft single broker   PostgreSQL 16                     │
                        │   fault engine: faults/Fxx/{inject,revert,verify}.sh               │
                        └─────────────────────────────────────────────────────────────────────┘
```

### 2.2 Ports (all bound on host `bankdemo`; firewalld allows only 22 externally)

| Service | Port(s) |
|---|---|
| NameNode RPC / HTTP | 9000 / 9870 |
| DataNode 1 xfer / http / ipc | 9866 / 9864 / 9867 |
| DataNode 2 xfer / http / ipc | 19866 / 19864 / 19867 |
| ResourceManager scheduler / tracker / client / admin / web | 8030 / 8031 / 8032 / 8033 / 8088 |
| NodeManager address / localizer / web | 8041 / 8040 / 8042 |
| Spark History Server | 18080 — **optional, local inspection only.** §9.1 collects event logs straight out of HDFS with `hdfs dfs -get`, which needs no history server, and the stack is stopped before you would browse one. The bundle README ships a `docker run` one-liner so the event logs can be read off-VM |
| Kafka broker / controller | 9092 / 9093 |
| PostgreSQL | 5432 (listen on localhost only) |

### 2.3 Memory budget

**This table budgets resident set size, not heap.** That distinction is the single
easiest way to blow this budget: a JVM's RSS is its heap plus metaspace, code cache,
thread stacks, and direct/Netty buffers — roughly **+300 MB per daemon** here. Six JVMs
means ~1.8 GB that a heap-only table does not show. Every figure below is measured RSS,
and Phase 3.5 (§6.8) exists to verify them before anything depends on them.

Usable RAM on a 12 GB A1 is ~11.6 GiB.

| Component | Setting | RSS budget |
|---|---|---|
| NameNode | `-Xmx768m -XX:MaxMetaspaceSize=128m` | 1.1 GB |
| DataNode ×2 | `-Xmx512m -XX:MaxMetaspaceSize=96m` each | 1.5 GB |
| ResourceManager | `-Xmx768m -XX:MaxMetaspaceSize=128m` | 1.1 GB |
| NodeManager daemon | `-Xmx512m -XX:MaxMetaspaceSize=96m` | 0.8 GB |
| Kafka broker | `KAFKA_HEAP_OPTS=-Xms512m -Xmx768m` | 1.1 GB |
| PostgreSQL | `shared_buffers=128MB`, `max_connections=30` | 0.4 GB |
| Python orchestrator, producer, consumer, monitor | — | 0.5 GB |
| **Resident daemon subtotal** | | **6.5 GB** |
| **YARN containers** | `yarn.nodemanager.resource.memory-mb=3072` | 3.0 GB |
| **Committed total** | | **9.5 GB** |
| Remaining for OS, sshd, page cache | — | ~2.1 GB |

So `MemAvailable` is ~5.1 GB with the stack idle and ~2.1 GB with a Spark app running —
comfortably clear of the monitor's 1 GB WARN and 500 MB CRITICAL (§7.6). If Phase 3.5
measures materially worse, lower `yarn.nodemanager.resource.memory-mb` first; it is the
only line with slack.

Also create a **2 GB swapfile** with `vm.swappiness=10`, and give `sshd` the drop-in
`OOMScoreAdjust=-1000`. Give the NameNode, ResourceManager, and PostgreSQL units
`OOMScoreAdjust=-500` as well. Without that, F16 (§10.3) kills the NameNode — the
largest unprotected RSS on the box — instead of the YARN container it is aiming at,
which ends the run and cannot be reverted inside the time budget.

Spark container sizing (cluster deploy mode). YARN overhead is `max(384m, 0.1 × memory)`,
so with `spark.driver.memory=512m` and `spark.executor.memory=640m`:

| Container | Memory + overhead | Rounded to 256 MB |
|---|---|---|
| Driver (is the AM in cluster mode) | 512 + 384 | 896 MB |
| Executor ×2 | 640 + 384 | 1024 MB each |
| **One Spark app** | | **2944 MB** |

That fits inside 3072 MB with 128 MB spare, so **only one batch app runs at a time**.
That is intentional: it makes the queue contention in F04 real. Set
`yarn.scheduler.maximum-allocation-mb=1280` so a runaway request is rejected rather than
accepted and then starved.

### 2.4 Users, groups, directories

| User | Groups | Purpose |
|---|---|---|
| `hdfs` | `hadoop`, `hdfsadmin` | Runs NameNode and DataNodes; HDFS superuser |
| `yarn` | `hadoop` | Runs ResourceManager and NodeManager |
| `kafka` | `kafka` | Runs the broker |
| `bankops` | `hadoop`, `kafka` | Application user: jobs, producers, consumer; SSH login for automation |
| `postgres` | — | Created by the package |

`dfs.permissions.superusergroup=hdfsadmin`, so `bankops` is **not** an HDFS superuser.

```
/opt/hadoop -> /opt/hadoop-<ver>      /opt/spark -> /opt/spark-<ver>     /opt/kafka -> /opt/kafka_2.13-<ver>
/opt/bankdemo            (deployed repo; root-owned, readable by bankops/yarn)
/opt/bankdemo/venv       (Python 3.11 venv; readable by yarn for PySpark executors)
/etc/hadoop/{nn,dn1,dn2,yarn}   full rendered conf dirs per daemon role
/etc/kafka/server.properties    /etc/spark/spark-defaults.conf
/etc/bankdemo/{cluster.env,secrets.env (0600),host-marker}
/opt/spark-extra         (PostgreSQL JDBC driver; created by 55-spark.sh)
/data                    (block volume, XFS, mounted by UUID, nofail)
  /data/images/          loop-filesystem image files
  /data/hdfs/nn          loop XFS 1 GB    (lets F02 fill the NameNode volume safely)
  /data/hdfs/dn1         loop XFS 5 GB
  /data/hdfs/dn2         loop XFS 5 GB
  /data/landing          loop ext4 2 GB, created with -N 8192 (lets F15 exhaust inodes)
  /data/kafka/logs       Kafka log.dirs
  /data/runs/            RUN_DIR per run (state/, logs/, snapshots/, jobs/)
                         plus <run_id>.bundle/ staging dirs and <run_id>.tar.gz
                         NOTE: RUN_DIR is never tarred directly — see §9.2
  /data/log-archive/     service logs moved aside at each run start
/var/log/{hadoop/hdfs,hadoop/yarn,kafka,bankdemo}
/var/lib/bankdemo/{keys (0700 root), state, DIRTY marker}
```

HDFS layout (created in Phase 3):

```
/bank/{landing,raw,staged,curated,ref,quarantine}   bankops:bankops 0750
/app-logs          yarn:hadoop 1777     (YARN log aggregation)
/spark-history     bankops:hadoop 1777
/spark/jars        bankops:hadoop 0755  (spark.yarn.archive zip)
/user/bankops      bankops:bankops 0750
/tmp               hdfs:hdfsadmin 1777
```

---

## 3. Repository layout

All paths below are relative to `infra/bankdemo/` inside the `ai-qa-agent` repo.

```
infra/bankdemo/
├── CLAUDE.md
├── IMPLEMENTATION_GUIDE.md
├── README.md
├── Makefile
├── .env.example                 # VM_HOST=, VM_USER=bankops, SSH_KEY=~/.ssh/bankdemo
├── .gitignore                   # .env, *.pem, secrets.env, runs/, __pycache__, .venv
├── requirements.txt             # pyyaml, requests, kafka-python, psycopg[binary]
├── requirements-dev.txt         # ruff, pytest, pyspark (LINT/IDE ONLY — never install
│                                #   into the runtime venv; it shadows /opt/spark/python)
├── docs/
│   ├── VM_SETUP.md              # [HUMAN] OCI provisioning walkthrough (Phase 1)
│   ├── PROGRESS.md              # phase checklist + pasted acceptance output
│   ├── ARCHITECTURE.md          # generated summary of §2
│   ├── RUNBOOK.md               # symptom → cause → fix (grows over time)
│   └── FAULTS.md                # generated from faults/*/fault.yaml (no answers section)
├── config/
│   ├── versions.env
│   ├── cluster.env              # hostnames, ports, memory numbers, paths
│   └── templates/
│       ├── hadoop/{core-site,hdfs-site,yarn-site,mapred-site,capacity-scheduler}.xml.tmpl
│       ├── hadoop/{hadoop-env.sh,log4j.properties}.tmpl
│       ├── kafka/server.properties.tmpl
│       ├── spark/spark-defaults.conf.tmpl
│       ├── postgres/{postgresql.custom.conf,pg_hba.conf}.tmpl
│       ├── systemd/*.service.tmpl
│       ├── sudoers/bankops.tmpl
│       └── cron/bankdemo.tmpl
├── install/
│   ├── install.sh               # runs stages in order; --only NN, --from NN
│   ├── lib/common.sh            # logging, require_root, require_bankdemo_host, render_template, marker helpers
│   ├── 00-preflight.sh          # arch, OS, RAM, CPU, /data present, internet reachable
│   ├── 10-os-base.sh            # packages, EPEL, timezone, sysctl, swap, sshd OOM drop-in, netem module
│   ├── 20-storage.sh            # /data mount check, loop images, fstab entries
│   ├── 30-users-dirs.sh
│   ├── 40-java-python.sh        # JDK 11 + 17, python3.11, venv
│   ├── 50-hadoop.sh             # download, verify, extract, render confs
│   ├── 55-spark.sh
│   ├── 60-kafka.sh
│   ├── 65-postgres.sh
│   ├── 70-systemd.sh            # render and install units, daemon-reload (do not enable at boot)
│   ├── 80-init-cluster.sh       # NN format, KRaft format, HDFS dirs, ref data, spark archive, topics, DB schema
│   └── 90-bankdemo.sh           # CLI symlink, sudoers, cron, logrotate, firewalld
├── bankdemo/                    # Python package
│   ├── __init__.py
│   ├── cli.py                   # argparse subcommands
│   ├── config.py                # loads /etc/bankdemo/cluster.env
│   ├── lifecycle.py             # start/stop order, health gate
│   ├── orchestrator.py          # run state machine (§8)
│   ├── faults.py                # catalog loader, selection, scheduling, inject/revert/verify
│   ├── scheduler.py             # Autosys-like job chain runner (§7.4)
│   ├── monitor.py               # threshold alerts → alerts.log (§7.6)
│   ├── collector.py             # snapshots + bundle (§9)
│   ├── reset.py                 # normal and deep reset (§8.5)
│   ├── grading.py               # reveal + grade
│   ├── generators/{merchants.py,eod_file.py,txn_stream.py}
│   ├── consumers/fraud_feed.py
│   └── util/{log.py,shell.py,hdfs.py,kafka.py,pg.py,timeutil.py}
├── jobs/
│   ├── spark/{ingest.py,enrich.py,settle.py}
│   └── python/{load_eod.py,recon.py}
├── scheduler/jobs.yaml
├── faults/
│   └── F01-datanode-crash/{fault.yaml,inject.sh,revert.sh,verify.sh}   # … one dir per fault
├── contract/
│   └── bundle_v1.md             # the bundle contract agent/ consumes (§9.5)
├── bin/bankdemo                 # exec /opt/bankdemo/venv/bin/python -m bankdemo.cli "$@"
├── scripts/{deploy.sh,fetch-bundle.sh,tunnel.sh}
├── tests/                       # pytest: seed determinism, selection conflicts, generators, grading
└── .github/workflows/run-incident.yml
```

---

## 4. Phase 0 — Repo scaffold (local) and Phase 1 — VM provisioning **[HUMAN]**

### 4.1 Phase 0: scaffold (Claude Code, local only)

Create the repository layout from §3 with placeholder files, and implement:

- `Makefile` targets: `lint` (shellcheck on all `*.sh` plus `bin/bankdemo`, `ruff check`, `ruff format --check`, `pytest`), `deploy`, `ssh`, `tunnel`, `run SEED=`, `fetch RUN=`.
- `scripts/deploy.sh`: reads `.env`, runs `rsync -az --delete --exclude .git --exclude .env ./ $VM_USER@$VM_HOST:/tmp/bankdemo-src/`, then `ssh … 'sudo rsync -a --delete /tmp/bankdemo-src/ /opt/bankdemo/ && sudo /opt/bankdemo/install/install.sh'`. Accept `--no-install` to only sync code.
- `scripts/tunnel.sh`: `ssh -N -L 9870:bankdemo:9870 -L 8088:bankdemo:8088 -L 18080:bankdemo:18080 …`.
- `install/lib/common.sh` with: `log_info/log_warn/log_error` (format from CLAUDE.md), `die`, `require_root`, `require_bankdemo_host` (checks `/etc/bankdemo/host-marker`; the marker itself is created only by `00-preflight.sh` after the arch/OS/RAM checks pass), `stage_done NAME` / `mark_stage NAME` (markers in `/var/lib/bankdemo/state/install/`), `render_template SRC DST "VAR1 VAR2 …"` (envsubst with explicit list, atomic write; **exit 0 = unchanged, 10 = content changed, 1 = error** — never overload 0 to mean "changed", because under `set -Eeuo pipefail` a bare call on an unchanged file would then abort the installer; callers use `render_template … || rc=$?`), `download_verified URL SHA512_URL DEST` (verifies SHA-512; note in the code that Apache serves the checksum from the same host as the artifact, so this is an **integrity** check against a corrupt download, not an **authenticity** check against a compromised mirror — `.asc` + KEYS would be the latter, and is out of scope here).
- `config/versions.env`, with the versions resolved at implementation time:

```bash
# Resolve the latest patch release of each line from archive.apache.org / downloads.apache.org
# and record exact versions here. Do not use "latest" at install time.
HADOOP_VERSION=3.4.x          # MUST use hadoop-${HADOOP_VERSION}-aarch64.tar.gz
SPARK_VERSION=3.5.x           # spark-${SPARK_VERSION}-bin-hadoop3.tgz (Spark 3.5 supports Java 11)
KAFKA_VERSION=3.9.x           # kafka_2.13-${KAFKA_VERSION}.tgz — KRaft-native AND Java 11-clean
SCALA_BINARY=2.13
POSTGRES_MAJOR=16
PG_JDBC_VERSION=42.7.x        # from Maven Central, .sha512 verification
```

Why these lines: Hadoop 3.4 is officially supported on Java 11 at runtime, and Spark 3.5
on YARN pairs cleanly with that. Spark 4.x requires Java 17 and adds risk with no benefit here.

**Why Kafka 3.9 and not 4.x.** 3.9 is the last 3.x line: fully KRaft-native (no ZooKeeper),
and it runs on Java 11. Pinning it deletes three separate problems at once:

1. No second JDK. The whole `JAVA17_HOME` apparatus, the per-unit `JAVA_HOME` rendering
   footgun in §6.6, and the `UnsupportedClassVersionError` failure mode all disappear.
2. No client risk. Kafka 4.0 dropped broker-side support for clients older than 2.1, which
   breaks `kafka-python` — the producer, the consumer, and faults F06/F07/F08 all depend on
   it, and the parent repo already pins it in its own `requirements.txt`. On 3.9 it works.
3. One less moving part in a stack that is already at the edge of its memory budget.

If you later want 4.x for currency, the cost is a client swap to `confluent-kafka` plus
reinstating the dual-JDK handling. Do not do it mid-build.

**Acceptance (Phase 0):** `make lint` passes on the scaffold; `tests/test_smoke.py` imports `bankdemo`.

### 4.2 Phase 1: provision the VM **[HUMAN]**

> **Step-by-step walkthrough: [`docs/VM_SETUP.md`](docs/VM_SETUP.md).** It expands every step
> below with console navigation, the capacity-error workarounds, and the disk-format safety
> procedure. Start there; this section is the summary.

1. In the OCI Console, create a compartment `bankdemo`, a VCN with an internet gateway (the "VCN with Internet Connectivity" wizard is fine), and a public subnet.
2. Security list ingress: TCP 22 only. The source is your home IP/32 if you only use SSH from home. If GitHub Actions will SSH in, it must be `0.0.0.0/0`; compensate with key-only auth and fail2ban (§12).
3. Create an instance: shape VM.Standard.A1.Flex, **2 OCPU, 12 GB**, image **Oracle Linux 9** (aarch64), hostname `bankdemo`, and your SSH public key (generate a dedicated one: `ssh-keygen -t ed25519 -f ~/.ssh/bankdemo`).
4. Create a 50 GB block volume and attach it to the instance using **paravirtualized** attachment (no iSCSI commands needed).
5. SSH in as `opc`. Format and mount the volume. **Do not trust `/dev/sdb`** — kernel device
   names are not stable and the boot volume is on the same controller; formatting the wrong
   one destroys the instance. Use the consistent device path OCI creates for paravirtualised
   attachments, and confirm before writing:
   ```bash
   lsblk -o NAME,SIZE,TYPE,MOUNTPOINT          # expect the 50G device to be unmounted, no partitions
   ls -l /dev/oracleoci/                       # oraclevdb -> the attached block volume
   DEV=/dev/oracleoci/oraclevdb
   sudo blkid "$DEV" || echo "empty, safe to format"
   sudo mkfs.xfs "$DEV"
   ```
   Then `sudo mkdir /data`, add an `/etc/fstab` line **by UUID** with `defaults,nofail`, run
   `sudo mount -a`, and confirm with `df -h /data`.
6. Create the `bankops` user with your SSH key (the installer will add its groups and sudoers later): `sudo useradd -m bankops`, copy `~opc/.ssh/authorized_keys` into `~bankops/.ssh/`, fix ownership and `chmod 700/600`, then add a temporary `bankops ALL=(ALL) NOPASSWD: ALL` in `/etc/sudoers.d/99-bankops-bootstrap`. **Phase 2 replaces this with a restricted rule.**
7. Locally, copy `.env.example` to `.env` and fill in `VM_HOST` (public IP), `VM_USER=bankops`, and `SSH_KEY`.

**Acceptance (Phase 1):** `ssh bankops@VM 'uname -m; nproc; free -g; df -h /data; cat /etc/os-release | head -2'` shows `aarch64`, `2`, about 11 GB total, `/data` mounted, and Oracle Linux 9.

---

## 5. Phase 2 — OS base, storage, users (installer stages 00–40)

### 5.1 `00-preflight.sh`

Fail with a clear message unless: `uname -m` = `aarch64`; `/etc/os-release` ID = `ol` and major version 9; `nproc` ≥ 2; MemTotal ≥ 11 GiB (compare in KiB from `/proc/meminfo`, not `free -g`, which rounds); `/data` is a mountpoint with ≥ 30 GB free; `curl -sfI https://downloads.apache.org` succeeds.

On success, `mkdir -p /etc/bankdemo` (nothing else creates it yet) and write
`/etc/bankdemo/host-marker` containing hostname, date, and machine-id.

This stage is the **sole exemption** from the `require_bankdemo_host` rule in CLAUDE.md,
because it is what creates the marker. It is still host-mutating, so it must run its own
arch/OS/RAM checks to completion before it writes anything at all.

### 5.2 `10-os-base.sh`

- `hostnamectl set-hostname bankdemo`. Add `<primary private IP> bankdemo` to `/etc/hosts` (detect the IP with `ip -4 route get 1.1.1.1`). **Do not map `bankdemo` to 127.0.0.1**: DataNode registration and WebHDFS redirects need a real interface address.
- **Make both survive a reboot.** OL9 cloud images run cloud-init, whose hostname and
  `update_etc_hosts` modules rewrite exactly these two things on every boot — which
  silently reintroduces the loopback mapping and produces the WebHDFS failure in §13 days
  after you thought it was fixed. Write `/etc/cloud/cloud.cfg.d/99-bankdemo.cfg` with
  `preserve_hostname: true` and `manage_etc_hosts: false`, then re-assert the hostname and
  the hosts entry. The §12 reboot test is what proves this worked; do not skip it.
- `timedatectl set-timezone America/Toronto`; make sure chronyd is enabled.
- Packages: `dnf install -y dnf-plugins-core oracle-epel-release-el9` (`config-manager` is a
  plugin and is not present on a minimal image), then
  `dnf config-manager --enable ol9_developer_EPEL`, then
  `dnf install -y tar gzip rsync jq curl wget unzip gettext xfsprogs e2fsprogs iproute-tc sysstat lsof psmisc procps-ng net-tools bind-utils git stress-ng fail2ban logrotate cronie python3.11 python3.11-pip zip`.
- netem: `modprobe sch_netem`. If that fails, install the modules-extra package matching the running kernel (`kernel-uek-modules-extra-$(uname -r)` on UEK, or `kernel-modules-extra-$(uname -r)` on RHCK) and retry. If it still fails, write `NETEM_AVAILABLE=false` to `/etc/bankdemo/cluster.env`. Fault F17 then declares `requires: [netem]` and is auto-excluded.
- sysctl (`/etc/sysctl.d/90-bankdemo.conf`): `vm.swappiness=10`, `vm.overcommit_memory=0`, `net.core.somaxconn=1024`, `fs.file-max=500000`.
- Swap: a 2 GB `/swapfile` (`fallocate`, `chmod 600`, `mkswap`, fstab entry). Idempotent.
- sshd: drop-in `/etc/systemd/system/sshd.service.d/oom.conf` with `[Service]` and `OOMScoreAdjust=-1000`; also set `PasswordAuthentication no` and `PermitRootLogin no` in `/etc/ssh/sshd_config.d/90-bankdemo.conf`; reload sshd.
- Transparent huge pages: leave the defaults.

### 5.3 `20-storage.sh`

Create the image files under `/data/images` if missing, then format and mount them via fstab using `loop,nofail,x-systemd.requires-mounts-for=/data`:

| Image | Size | FS | Mount | mkfs options |
|---|---|---|---|---|
| nn.img | 1G | xfs | /data/hdfs/nn | default |
| dn1.img | 5G | xfs | /data/hdfs/dn1 | default |
| dn2.img | 5G | xfs | /data/hdfs/dn2 | default |
| landing.img | 2G | ext4 | /data/landing | `-N 8192 -m 0` (few inodes, on purpose) |

Also create the plain directories `/data/kafka/logs`, `/data/runs`, `/data/log-archive`, `/data/tmp/hadoop`, and `/data/landing/{eod,spool}`.

**Landing-zone retention.** `/data/landing` has only ~8192 inodes by construction, and §8.5
asserts its inode usage stays under 20% (≈1638). Nothing else reclaims it, and F11 leaves
late files behind on purpose. CLEANUP must therefore delete `/data/landing/eod/*` for all
but the newest 3 runs, and empty `/data/landing/spool/` unconditionally.
SELinux: run `restorecon -Rv /data` after creating the mounts. If any service reports AVC denials later, apply `semanage fcontext` rules for the path instead of disabling SELinux, and document them in RUNBOOK.

### 5.4 `30-users-dirs.sh`

Create the groups `hadoop`, `hdfsadmin`, and `kafka`, and the system users `hdfs`, `yarn`, and `kafka` (nologin shell, home under `/var/lib/<user>`). Put `bankops` in `hadoop` and `kafka`. Create every directory from §2.4 with its listed owner and mode. `/var/lib/bankdemo/keys` is `root:root 0700`.

### 5.5 `40-java-python.sh`

- `dnf install -y java-11-openjdk-headless`. **One JDK only** — Kafka is pinned to 3.9.x
  precisely so Java 17 is never needed (§4.1). Resolve the real path (glob
  `/usr/lib/jvm/java-11-openjdk-*`) and write `JAVA11_HOME` to `/etc/bankdemo/cluster.env`.
  **Do not change the system-wide `alternatives` default.**
- Create a venv at `/opt/bankdemo/venv` with `python3.11 -m venv`, then `pip install -r /opt/bankdemo/requirements.txt`. Make it world-readable (`chmod -R o+rX`) so the `yarn` user's PySpark executors can use it.

**Acceptance (Phase 2):** re-running `install.sh --from 00 --to 40` prints "already done" for every stage; `swapon --show` lists the 2G swapfile; `findmnt /data/hdfs/nn /data/hdfs/dn1 /data/hdfs/dn2 /data/landing` shows all four; `df -i /data/landing` shows about 8K inodes; `sudo -u yarn /opt/bankdemo/venv/bin/python -c "import yaml,requests,kafka,psycopg"` succeeds; `/etc/bankdemo/cluster.env` contains `JAVA11_HOME` and no reference to a second JDK; `lsmod | grep sch_netem` works, or NETEM_AVAILABLE=false is recorded.

---

## 6. Phase 3 — Hadoop, Spark, Kafka, PostgreSQL, systemd (stages 50–80)

### 6.1 Downloads

`download_verified` fetches from `https://downloads.apache.org/…` and falls back to `https://archive.apache.org/dist/…`. It verifies against the published `.sha512`, extracts into `/opt/<name>-<ver>`, and repoints the `/opt/<name>` symlink. It is idempotent by version.

### 6.2 Hadoop configuration (`50-hadoop.sh`)

Render **complete** conf dirs for each role: `/etc/hadoop/nn`, `/etc/hadoop/dn1`, `/etc/hadoop/dn2`, `/etc/hadoop/yarn`. They share all files except `hdfs-site.xml`, which differs in DataNode dirs and ports.

`/etc/hadoop/client` is a **separately rendered directory, not a symlink to `/etc/hadoop/yarn`**.
Render it from the same templates and export `HADOOP_CONF_DIR=/etc/hadoop/client` in
`/etc/profile.d/bankdemo.sh`. Two things depend on it being separate:

- F14 deliberately writes a malformed value into `/etc/hadoop/yarn/yarn-site.xml`. If the
  client shared that file, `spark-submit` would fail to parse its own config client-side and
  the fault would present as a confusing pile of unrelated errors instead of "the RM won't start".
- §8.5's invariant re-renders every live config and compares SHA-256. A symlink makes
  "which file did I just check" ambiguous.

**core-site.xml**

| Property | Value |
|---|---|
| fs.defaultFS | hdfs://bankdemo:9000 |
| hadoop.tmp.dir | /data/tmp/hadoop |
| hadoop.http.staticuser.user | bankops |
| io.file.buffer.size | 65536 |

**hdfs-site.xml** (common values; DataNode-specific values follow)

| Property | Value | Why |
|---|---|---|
| dfs.replication | 2 | Two DataNodes, so F01 produces under-replication |
| dfs.namenode.name.dir | file:///data/hdfs/nn/name | |
| dfs.namenode.rpc-address / http-address | bankdemo:9000 / 0.0.0.0:9870 | |
| dfs.permissions.enabled | true | F13 |
| dfs.permissions.superusergroup | hdfsadmin | bankops is not a superuser |
| dfs.namenode.resource.du.reserved | 209715200 | 200 MB. F02 fills the nn volume below this to trigger resource-low safe mode |
| dfs.heartbeat.interval | 3 | |
| dfs.namenode.heartbeat.recheck-interval | 30000 | **Critical:** a dead DataNode is detected in ~90 s (2×recheck + 10×heartbeat) instead of ~10.5 min |
| dfs.namenode.safemode.extension | 5000 | Faster boot |
| dfs.namenode.replication.min | 1 | |
| dfs.blocksize | 16777216 | 16 MB: more blocks from small data, so faults show up more visibly |
| dfs.datanode.du.reserved | 104857600 | 100 MB |
| dfs.webhdfs.enabled | true | fraud_feed writes via WebHDFS |

DataNode-specific values:

| Property | dn1 | dn2 |
|---|---|---|
| dfs.datanode.data.dir | file:///data/hdfs/dn1/data | file:///data/hdfs/dn2/data |
| dfs.datanode.address | 0.0.0.0:9866 | 0.0.0.0:19866 |
| dfs.datanode.http.address | 0.0.0.0:9864 | 0.0.0.0:19864 |
| dfs.datanode.ipc.address | 0.0.0.0:9867 | 0.0.0.0:19867 |

**yarn-site.xml**

| Property | Value |
|---|---|
| yarn.resourcemanager.hostname | bankdemo |
| yarn.nodemanager.address | bankdemo:8041 |
| yarn.nodemanager.resource.memory-mb | 3072 (§2.3) |
| yarn.nodemanager.resource.cpu-vcores | 4 — 2× oversubscribed on 2 OCPU. Harmless because CapacityScheduler's default `DefaultResourceCalculator` ignores vcores entirely and schedules on memory alone; if you ever switch to `DominantResourceCalculator`, revisit this number first |
| yarn.scheduler.minimum-allocation-mb / maximum-allocation-mb | 256 / 1280 |
| yarn.nodemanager.pmem-check-enabled | true (so OOM shows "running beyond physical memory limits") |
| yarn.nodemanager.vmem-check-enabled | false |
| yarn.nodemanager.aux-services | mapreduce_shuffle |
| yarn.log-aggregation-enable | true |
| yarn.nodemanager.remote-app-log-dir | /app-logs |
| yarn.log-aggregation.retain-seconds | 604800 |
| yarn.nodemanager.log-dirs / local-dirs | /var/log/hadoop/yarn/userlogs / /data/tmp/hadoop/nm-local |
| yarn.nodemanager.env-whitelist | JAVA_HOME,HADOOP_COMMON_HOME,HADOOP_HDFS_HOME,HADOOP_CONF_DIR,CLASSPATH_PREPEND_DISTCACHE,HADOOP_YARN_HOME,HADOOP_HOME,PATH,LANG,TZ |
| yarn.resourcemanager.scheduler.class | …capacity.CapacityScheduler |
| yarn.resourcemanager.max-completed-applications | 200 |

**capacity-scheduler.xml**

| Property | Value |
|---|---|
| yarn.scheduler.capacity.root.queues | etl,adhoc |
| root.etl.capacity / maximum-capacity | 70 / 100 |
| root.adhoc.capacity / maximum-capacity | 30 / 100 |
| yarn.scheduler.capacity.maximum-am-resource-percent | 0.5 |
| yarn.resourcemanager.scheduler.monitor.enable (preemption) | false (in yarn-site; so F04 starvation persists) |

**mapred-site.xml:** `mapreduce.framework.name=yarn`, plus the HADOOP_MAPRED_HOME env settings for AM/map/reduce. It's used only for `yarn jar` sanity checks.

**hadoop-env.sh:** `JAVA_HOME=${JAVA11_HOME}`, `HADOOP_LOG_DIR` per role, and the heap opts from §2.3.

Logging: daemons run in the foreground under systemd, and still write rolling log files. Set
`HADOOP_ROOT_LOGGER=INFO,RFA`, `HADOOP_LOG_DIR`, and a unique `HADOOP_LOGFILE` per unit.
**Verify** that the file appears in `/var/log/hadoop/...`. If the foreground launcher overrides
the root logger, pass `-Dhadoop.root.logger=INFO,RFA -Dhadoop.log.dir=… -Dhadoop.log.file=…`
through the role's `*_OPTS` instead, and record which approach worked in RUNBOOK.

### 6.3 Spark (`55-spark.sh`)

`/etc/spark/spark-defaults.conf`:

```
spark.master                              yarn
spark.submit.deployMode                   cluster
spark.yarn.queue                          etl
spark.yarn.maxAppAttempts                 1
spark.yarn.archive                        hdfs://bankdemo:9000/spark/jars/spark-libs.zip
spark.driver.memory                       512m
spark.executor.memory                     640m
spark.executor.instances                  2
spark.executor.cores                      1
spark.dynamicAllocation.enabled           false
spark.sql.shuffle.partitions              8
spark.sql.adaptive.enabled                true
spark.eventLog.enabled                    true
spark.eventLog.dir                        hdfs://bankdemo:9000/spark-history
spark.history.fs.logDirectory             hdfs://bankdemo:9000/spark-history
spark.yarn.appMasterEnv.PYSPARK_PYTHON    /opt/bankdemo/venv/bin/python
spark.executorEnv.PYSPARK_PYTHON          /opt/bankdemo/venv/bin/python
spark.yarn.appMasterEnv.JAVA_HOME         ${JAVA11_HOME}
spark.executorEnv.JAVA_HOME               ${JAVA11_HOME}
spark.jars                                /opt/spark-extra/postgresql-${PG_JDBC_VERSION}.jar
```

`55-spark.sh` creates `/opt/spark-extra` (root-owned, 0755) and downloads the JDBC driver
into it before rendering this file; nothing else creates that directory.

**Never put the database password on the spark-submit command line or in a JDBC URL.**
It would land in the `ps` snapshot, the YARN container launch command inside `yarn logs`,
and the RM REST app diagnostics — all three of which go into the bundle (§9.1), and the
bundle is published as a CI artifact (§11.3). Write it to a properties file owned by
`bankops` at 0600 and pass `--properties-file`, or read it from the environment inside the
job. See the redaction rules in §9.1.

`spark-env.sh`: `JAVA_HOME=${JAVA11_HOME}`, `HADOOP_CONF_DIR=/etc/hadoop/client`, `SPARK_CONF_DIR=/etc/spark`.
`spark.yarn.archive` removes ~20 s of jar upload per job; it's built in `80-init-cluster.sh`.
`spark.yarn.maxAppAttempts=1` keeps failures visible instead of silently retried.

### 6.4 Kafka (`60-kafka.sh`)

Kafka is **3.9.x**, running on the same Java 11 as everything else (§4.1). KRaft, combined
mode, single node. `/etc/kafka/server.properties`:

```
process.roles=broker,controller
node.id=1
controller.quorum.voters=1@bankdemo:9093
listeners=PLAINTEXT://bankdemo:9092,CONTROLLER://bankdemo:9093
advertised.listeners=PLAINTEXT://bankdemo:9092
controller.listener.names=CONTROLLER
listener.security.protocol.map=PLAINTEXT:PLAINTEXT,CONTROLLER:PLAINTEXT
inter.broker.listener.name=PLAINTEXT
log.dirs=/data/kafka/logs
num.partitions=3
offsets.topic.replication.factor=1
transaction.state.log.replication.factor=1
transaction.state.log.min.isr=1
min.insync.replicas=1
log.retention.hours=24
log.segment.bytes=16777216
auto.create.topics.enable=false
group.initial.rebalance.delay.ms=0
```

Storage format (once, in `80-init-cluster.sh`): `kafka-storage.sh random-uuid` → save to
`/etc/bankdemo/kafka-cluster-id`, then `kafka-storage.sh format -t <id> -c /etc/kafka/server.properties`.
3.9 accepts static `controller.quorum.voters` as written above. If the pinned patch release
rejects it, follow that version's KRaft quickstart (for example `--standalone` with
`controller.quorum.bootstrap.servers`) and record the choice in RUNBOOK.

Environment in the unit: `JAVA_HOME=${JAVA11_HOME}`, `KAFKA_HEAP_OPTS=-Xms512m -Xmx768m`,
`LOG_DIR=/var/log/kafka`. The unit's `LimitNOFILE=65536` is overridden by fault F08 via a
drop-in.

### 6.5 PostgreSQL (`65-postgres.sh`)

`dnf module enable -y postgresql:16 && dnf install -y postgresql-server postgresql-contrib`,
then `postgresql-setup --initdb` if not already initialized. Custom config `include_dir` file:
`listen_addresses='localhost'`, `shared_buffers=128MB`, `max_connections=30`,
`log_min_duration_statement=2000`, `log_line_prefix='%m [%p] %u@%d '`.
`pg_hba.conf`: `local all postgres peer`; `host bankdemo bankops 127.0.0.1/32 scram-sha-256`;
also `::1/128`.

Generate a random password once into `/etc/bankdemo/secrets.env` (`PG_PASSWORD=…`, mode
0640, root:bankops). Create role `bankops` and database `bankdemo`, then apply the schema:

```sql
CREATE TABLE IF NOT EXISTS job_control (
  run_id text, job_name text, status text, attempt int DEFAULT 1,
  start_ts timestamptz, end_ts timestamptz, exit_code int, sla_minutes int, message text,
  PRIMARY KEY (run_id, job_name, attempt));
CREATE TABLE IF NOT EXISTS settlement_summary (
  run_id text, business_date date, merchant_id text, txn_count bigint,
  total_amount numeric(18,2), PRIMARY KEY (run_id, merchant_id));
CREATE TABLE IF NOT EXISTS recon_result (
  run_id text PRIMARY KEY, business_date date, source_count bigint, source_amount numeric(18,2),
  target_count bigint, target_amount numeric(18,2), diff_count bigint, diff_amount numeric(18,2),
  status text, checked_at timestamptz);
CREATE TABLE IF NOT EXISTS run_history (
  run_id text PRIMARY KEY, seed bigint, started_at timestamptz, ended_at timestamptz,
  outcome text, bundle_path text);
```

PostgreSQL **is** enabled at boot (`systemctl enable postgresql`), since it isn't part of the
per-run start/stop. Fault F12 stops it, and revert starts it. Give it the drop-in
`OOMScoreAdjust=-500` so F16 cannot take it out as collateral (§2.3).

Add a `median_ticket numeric(18,2)` column to `settlement_summary` — §7.3 has `settle.py`
compute a median per merchant, and that computation is the entire mechanism behind F05, so
it needs somewhere to land.

### 6.6 systemd units (`70-systemd.sh`)

Units to render into `/etc/systemd/system/`: `hadoop-namenode.service`,
`hadoop-datanode@.service` (instances 1 and 2), `hadoop-resourcemanager.service`,
`hadoop-nodemanager.service`, `kafka.service`, `spark-history.service`, and a target
`bankdemo-stack.target` that `Wants=` all the core daemons. **None are enabled at boot.**
The orchestrator starts and stops them per run.

Template for the DataNode instance unit:

```ini
[Unit]
Description=HDFS DataNode %i
Wants=network-online.target
After=network-online.target hadoop-namenode.service
RequiresMountsFor=/data/hdfs/dn%i
PartOf=bankdemo-stack.target

[Service]
Type=simple
User=hdfs
Group=hadoop
EnvironmentFile=/etc/bankdemo/cluster.env
Environment=JAVA_HOME=/usr/lib/jvm/java-11-openjdk-<rendered literal>
Environment=HADOOP_HOME=/opt/hadoop
Environment=HADOOP_CONF_DIR=/etc/hadoop/dn%i
Environment=HADOOP_LOG_DIR=/var/log/hadoop/hdfs
Environment=HADOOP_LOGFILE=hadoop-hdfs-datanode%i.log
Environment=HADOOP_ROOT_LOGGER=INFO,RFA
Environment="HDFS_DATANODE_OPTS=-Xmx512m -XX:MaxMetaspaceSize=96m"
ExecStart=/opt/hadoop/bin/hdfs datanode
LimitNOFILE=65536
Restart=no
SuccessExitStatus=143
TimeoutStopSec=60
```

Three systemd details that are easy to get subtly wrong:

- **`Wants=network-online.target` must accompany the `After=`.** An `After=` on a target
  nothing pulls in is a no-op.
- **There is no `[Install]` section.** `WantedBy=bankdemo-stack.target` only takes effect on
  `systemctl enable`, which §6.6 forbids — so it would be dead text that implies a
  relationship that does not exist. The target file carries `Wants=` for all core daemons
  instead, which works without enabling anything. `PartOf=` on each unit is what makes
  `systemctl stop bankdemo-stack.target` stop them.
- **`JAVA_HOME` must be a rendered literal.** systemd does not expand variables read from
  `EnvironmentFile` inside other `Environment=` lines, so `${JAVA11_HOME}` would reach the
  process uninterpolated. Render the absolute path at install time.

The NameNode and ResourceManager units additionally carry `OOMScoreAdjust=-500` and their
own `RequiresMountsFor=` (`/data/hdfs/nn` and `/data/tmp/hadoop` respectively); Kafka carries
`RequiresMountsFor=/data/kafka/logs`.

Start order (implemented in `lifecycle.py`, not only in systemd ordering):
`namenode` → wait for RPC port and `hdfs dfsadmin -safemode get` = OFF (timeout 120 s) →
`datanode@1`, `datanode@2` → wait until live DataNodes = 2 (JMX `NumLiveDataNodes`, timeout 60 s) →
`resourcemanager` → `nodemanager` → wait until RM reports 1 active NM (timeout 60 s) →
`kafka` → wait for `kafka-broker-api-versions.sh --bootstrap-server bankdemo:9092` (timeout 60 s).
Stop order is the reverse, with `systemctl stop` and a 30 s timeout each (six units × 60 s
would not fit the CLEANUP budget in §8.2); `kill -9` only as a last resort, and log it.

### 6.7 Cluster init (`80-init-cluster.sh`, runs once, guarded by a marker)

1. `sudo -u hdfs hdfs --config /etc/hadoop/nn namenode -format -nonInteractive -clusterId bankdemo`.
2. Start HDFS (namenode plus both DataNodes) using the lifecycle helper.
3. Create the HDFS directories from §2.4.
4. Build `spark-libs.zip` from `/opt/spark/jars/*` (`zip -q -j`) and put it in `/spark/jars/`.
5. Generate reference data: `python -m bankdemo.generators.merchants --count 500 --seed 1` → `/bank/ref/merchants/merchants.csv` (merchant_id, name, mcc, city, country, risk_tier).
6. Format Kafka storage, start Kafka, and create topics: `cards.txn.auth` (3 partitions, RF 1) and `bank.alerts` (1 partition).
7. Apply the Postgres schema.
8. Stop the stack. Mark done.

`80-init-cluster.sh --force` wipes the HDFS and Kafka data dirs and re-runs everything; the
deep reset in §8.5 uses this.

**Acceptance (Phase 3):**
- `bankdemo health` (implement a first version now) reports all services active; live DataNodes = 2, active NodeManagers = 1, safe mode OFF; Kafka API reachable; `pg_isready` ok. It exits 0 when everything is green, 1 when any check fails, and prints one line per check in the standard log format.
- `hdfs fsck /` is HEALTHY, with zero under-replicated blocks after the ref data load.
- `free -m` with the full stack idle shows ≥ 4.5 GB available (§2.3 predicts ~5.1 GB).
- `ps -o rss= -p <each daemon pid>` summed is within 15% of the §2.3 daemon subtotal. If it is not, stop and re-derive §2.3 before Phase 4 — every later budget depends on it.
- The SparkPi example completes on YARN in the `etl` queue in < 90 s: `spark-submit --class org.apache.spark.examples.SparkPi /opt/spark/examples/jars/spark-examples_*.jar 50`.
- Log files exist in `/var/log/hadoop/hdfs/hadoop-hdfs-datanode1.log`, `/var/log/kafka/server.log`, and elsewhere.
- Stop everything, then start everything: total start time < 150 s (record it).
- `ss -tlnp` confirms Postgres listens only on localhost, and `firewall-cmd --list-all` shows only ssh.

### 6.8 Phase 3.5 — budget truth gate

**Do not start Phase 4 until this passes.** §2.3 is a projection. Five phases of code are
about to be written against it, and the two faults most likely to disprove it (F05 skew OOM,
F16 host memory pressure) are not built until Phase 7. Discovering there that the budget was
wrong means rewriting the workload, not tuning a fault.

Run, on the VM, with the full stack up:

1. Record RSS per daemon (`ps -eo pid,user,rss,args --sort=-rss`) and `MemAvailable` at idle.
2. Submit SparkPi **and** a 300 K-row shuffle concurrently, and sample `MemAvailable` every
   2 s through both. Record the trough.
3. With a Spark app running, start F16's stress profile
   (`systemd-run --unit=bankdemo-stress --property=MemoryMax=2G stress-ng --vm 2 --vm-bytes 800M --timeout 60s`)
   and sample again. Record the trough and `dmesg -T | grep -i oom`.

**Gate:** step 2's trough is ≥ 1.5 GB, step 3's trough is between 300 MB and 700 MB, and
step 3 kills a YARN container or `stress-ng` itself — **not** the NameNode, ResourceManager,
PostgreSQL, or sshd. If any of that fails, adjust `yarn.nodemanager.resource.memory-mb`, the
daemon heaps, and F16's `--vm-bytes` until it holds, and update §2.3 with the measured
numbers. Paste the measurements into `docs/PROGRESS.md`; they are the justification for
every memory number in this guide.

---

## 7. Phase 4 — The simulated business day (workload, scheduler, monitor)

Goal: a **fault-free** run produces a correct settlement, a clean recon, a steady fraud
stream, and no CRITICAL alerts, all within 12 minutes end to end (§8.2 budgets ~11:30 for
the baseline path).

### 7.1 Identifiers and time

- `run_id` = `R<YYYYMMDD>-<HHMMSS>-<seed>`, e.g. `R20260917-143205-8812`.
- `business_date` = run start date in America/Toronto.
- HDFS partitions use `dt=<business_date>/run=<run_id>` so several runs per day never collide.
- Each run has a `RUN_DIR=/data/runs/<run_id>` with subdirs `state/`, `logs/`, `snapshots/`, `jobs/`.

### 7.2 Generators

**Feed profile.** Before generating anything, the orchestrator writes
`$RUN_DIR/state/feed_profile.json`. Pre-feed faults modify it (§10.1). Defaults:

```json
{
  "eod_records": 200000,
  "eod_delay_seconds": 0,
  "skew_merchant": null,
  "skew_ratio": 0.0,
  "schema_drift": "none",
  "drop_records": 0,
  "duplicate_records": 0,
  "stream_rate_per_sec": 120,
  "stream_bad_json_ratio": 0.0
}
```

**`generators/eod_file.py`** writes `/data/landing/eod/TXN_EOD_<YYYYMMDD>_<run_id>.dat.tmp`,
then renames it to `.dat` (atomic arrival), and finally creates a `.done` trigger file.

- Header: `H|TXN_EOD|<business_date>|<run_id>|v1`
- Detail (pipe-delimited): `txn_id|card_hash|merchant_id|mcc|amount|currency|txn_ts|channel|country|auth_code|status`
- Trailer: `T|<approved_record_count>|<approved_total_amount_2dp>`, computed over the
  **intended APPROVED records only**, and this definition is load-bearing. `recon.py` (§7.3)
  compares the trailer against `SUM` over `settlement_summary`, which holds APPROVED rows
  only. If the trailer counted the 3% DECLINED rows too, **every clean run would report a
  ~3% recon BREAK** and F10's signal would be indistinguishable from the baseline. The
  trailer reflects what upstream claims to have settled; `drop_records` and
  `duplicate_records` are applied *after* it is computed, which is what creates a genuine
  recon break when a fault asks for one.
- `schema_drift` values: `none`; `extra_column` (inserts `wallet_type` after `channel`, and the header version becomes `v2`); `date_format` (`txn_ts` switches from ISO-8601 to `dd/MM/yyyy HH:mm:ss`).
- Skew: `skew_ratio` of rows get `merchant_id = skew_merchant`.
- The generator uses `random.Random(seed)` only. Amounts are log-normal, clipped to 1–9,999.99; 3% DECLINED; currencies CAD 85% / USD 15%.
- It must finish within 45 s for 200 K rows on the VM. Measure it. 200 K is the default
  because the BATCH window is 5:00 (§8.2); raise it only if Phase 4 timings show headroom.

**`generators/txn_stream.py`** is a Kafka producer (`kafka-python`) on `cards.txn.auth`, key =
`card_hash`, JSON value with the same fields plus `event_ts`. It runs from BOOT_DONE until
the end of the batch window, at `stream_rate_per_sec` with jitter. It logs the produced count
every 30 s to `$RUN_DIR/logs/txn_stream.log`. On broker errors it logs and retries with
backoff; it never exits early.

**`consumers/fraud_feed.py`** is consumer group `fraud-feed`. It consumes `cards.txn.auth` and
flags `amount > 5000` or `country != merchant country` (merchant map loaded from HDFS ref at
startup). Every 30 s it writes NDJSON part files through WebHDFS to
`/bank/raw/fraud/dt=…/run=…/part-<n>.json`, **then** commits offsets. Writes to `bank.alerts`
for flagged transactions. It logs consumed count and last committed offset per partition every 30 s.

**Commit cadence vs. lag thresholds.** Flushing every 30 s at 120 msg/s means committed
offsets sit up to ~3600 messages behind — so a *healthy* run would trip any lag threshold
set below that, and F06 (a genuinely hung consumer) would be indistinguishable from normal.
Commit every **3 s** (on a timer, independent of the 30 s HDFS flush, writing the part file
first so the ordering guarantee holds) to keep steady-state lag under ~400. The thresholds in
§7.6 and the Phase 4 acceptance bound both assume this.
It runs as a child process with its PID recorded in `$RUN_DIR/state/pids.json` (F06 needs it).

### 7.3 Batch jobs

| Job | Type | Input → output | Must fail loudly when |
|---|---|---|---|
| `load_eod.py` | Python | `/data/landing/eod/*.dat` → `/bank/raw/eod/dt=/run=/` | Header/trailer missing or malformed; header version unknown; landing file not found |
| `ingest.py` | PySpark | raw → `/bank/staged/txn/dt=/run=/` (parquet) | Column count ≠ expected; `txn_ts` fails to parse for > 0.1% of rows (write the bad rows to `/bank/quarantine/…` first, then exit non-zero with a clear message) |
| `enrich.py` | PySpark | staged + `/bank/ref/merchants` → `/bank/curated/txn_enriched/…` | Unmatched merchant_id > 1% |
| `settle.py` | PySpark | enriched → `/bank/curated/settlement/…` + JDBC write to `settlement_summary` | JDBC failure; output path permission denied |
| `recon.py` | Python | Trailer count/amount vs `SUM` from `settlement_summary` → `recon_result`. Both sides are APPROVED-only by construction (§7.2), so a clean run reconciles exactly | Any difference ⇒ status `BREAK`, exit code 2 |

`settle.py` computes per-merchant totals **and** a median ticket size. Implement the median
deliberately the legacy way: `collect_list(amount)` per merchant followed by a Python UDF,
**not** `percentile_approx`. With normal data this is fine. With one hot merchant, a single
executor must hold that merchant's whole list, which is exactly how F05 produces a
realistic OOM. Put a code comment saying this is intentional.

Every job logs to stdout in the standard format. The scheduler captures stdout/stderr to
`$RUN_DIR/jobs/<JOB>.log`, and for Spark jobs parses the `application_…` id from the
spark-submit output into `job_control.message`.

### 7.4 Scheduler (`scheduler.py` + `scheduler/jobs.yaml`)

A minimal Autosys-flavoured runner. `jobs.yaml`:

```yaml
jobs:
  FW_EOD_FILE:
    type: file_watcher
    path_glob: /data/landing/eod/TXN_EOD_{yyyymmdd}_{run_id}.done
    timeout_minutes: 2.5
    sla_minutes: 2
  LOAD_EOD:
    type: command
    command: "{venv}/bin/python -m jobs.python.load_eod --run-id {run_id}"
    condition: success(FW_EOD_FILE)
    sla_minutes: 1
    timeout_minutes: 1.5
  SPK_INGEST:
    type: spark
    script: jobs/spark/ingest.py
    condition: success(LOAD_EOD)
    sla_minutes: 1.5
    timeout_minutes: 2
  SPK_ENRICH:
    type: spark
    script: jobs/spark/enrich.py
    condition: success(SPK_INGEST)
    sla_minutes: 1.5
    timeout_minutes: 2
  SPK_SETTLE:
    type: spark
    script: jobs/spark/settle.py
    condition: success(SPK_ENRICH)
    sla_minutes: 1.5
    timeout_minutes: 2
  RECON:
    type: command
    command: "{venv}/bin/python -m jobs.python.recon --run-id {run_id}"
    condition: success(SPK_SETTLE)
    sla_minutes: 0.5
    timeout_minutes: 0.75
```

**The window bounds the chain, not the sum of the timeouts.** Those per-job timeouts add up
to 10:45, inside a BATCH window of 5:00 (§8.2) — deliberately. Each timeout is what that job
alone is allowed, and the scheduler additionally enforces a hard `batch_deadline` passed in
from the orchestrator: at the deadline anything RUNNING becomes `TERMINATED` and anything
unstarted becomes `ON_HOLD`. Without that global deadline the declared timeouts would be
decorative and a single slow job could eat the whole run. A clean chain measures ~4:35.

Status vocabulary written to `$RUN_DIR/logs/scheduler.log` and `job_control`:
`ACTIVATED`, `STARTING`, `RUNNING`, `SUCCESS`, `FAILURE`, `TERMINATED` (timeout), `ON_HOLD`
(upstream failed, so the job never ran), and `SLA_MISSED` as an additional event when the
runtime exceeds `sla_minutes`. Line format example:

```
2026-09-17T14:36:02-04:00|INFO|scheduler|job=SPK_INGEST status=RUNNING app=application_1726...
```

Rules: jobs run sequentially; a timeout sends `yarn application -kill` for Spark jobs, or
SIGTERM for commands; the scheduler exits when all jobs are terminal or the batch window
closes. It never raises; errors become job statuses.

### 7.5 Spark submission

`spark-submit --queue etl --name <JOB>-<run_id> --py-files <zip of jobs/ and bankdemo/util> <script> --run-id … --business-date …`.
Build the `--py-files` zip once per run into `$RUN_DIR/state/pyfiles.zip`.

### 7.6 Monitor (`monitor.py`)

It runs for the whole run as a background thread or process, polling every 15 s (Kafka lag
every 30 s), and writes `$RUN_DIR/logs/alerts.log` in a Geneos/Splunk-alert style:

```
2026-09-17T14:38:15-04:00|CRITICAL|HDFS|UNDER_REPLICATED_BLOCKS|value=37 threshold=0 host=bankdemo
```

| Check | Source | WARN / CRITICAL |
|---|---|---|
| Service down | `systemctl is-active` per unit | — / not active |
| NameNode safe mode | JMX `Hadoop:service=NameNode,name=NameNodeInfo` → `Safemode` | — / non-empty |
| Dead DataNodes | JMX FSNamesystemState `NumDeadDataNodes` | — / ≥ 1 |
| Under-replicated / missing blocks | JMX FSNamesystem `LowRedundancyBlocks` (Hadoop 3 name; `UnderReplicatedBlocks` is the retained legacy alias — read whichever your pinned 3.4.x actually serves, preferring the former, and **fail loudly if neither is present** rather than silently never alerting) / `MissingBlocks` | > 0 / missing > 0 |
| DataNode volume usage | `df` on dn1/dn2/nn mounts | 85% / 95% |
| Landing inode usage | `df -i /data/landing` | 85% / 95% |
| YARN apps pending | RM REST `/ws/v1/cluster/apps?states=ACCEPTED`, age > 60 s | 1 / age > 120 s |
| Unhealthy / lost NodeManagers | RM `/ws/v1/cluster/metrics` | — / ≥ 1 |
| Kafka consumer lag (`fraud-feed`) | `kafka-consumer-groups.sh --describe` total LAG | 1,000 / 5,000 — valid only with the 3 s commit cadence in §7.2; steady state is ~400 |
| Postgres | `pg_isready -h localhost` | — / not ready |
| Host memory | `/proc/meminfo` MemAvailable | < 1 GB / < 500 MB |
| Job SLA | `job_control` | SLA_MISSED ⇒ WARN; FAILURE/TERMINATED ⇒ CRITICAL |

Emit an alert on state **change** only (raise and clear), not on every poll. Include
`CLEARED` lines when a condition resolves.

**Acceptance (Phase 4)** (use a minimal `bankdemo run --no-faults` path: start stack → feed → batch → stop; Phase 5 builds the full lifecycle around it):
- Three consecutive `bankdemo run --no-faults` complete with all jobs SUCCESS, recon `MATCH`, zero CRITICAL alerts and zero WARN alerts, and total wall time ≤ 12:00. Record per-phase timings.
- Two runs with the same seed produce an **identical EOD detail block**. The whole file cannot match: the header carries `run_id`, which embeds a timestamp. Compare `sed '1d;$d' <file> | sha256sum` — detail rows only — and assert the trailer values match as numbers.
- Fraud feed: consumer lag stays < 500 during a normal run (this is what the 3 s commit cadence in §7.2 buys), and part files appear in HDFS.
- `pytest` covers generator determinism, trailer math with drop/duplicate, and scheduler condition evaluation.

If the batch chain exceeds its budget on 2 OCPU, first lower `eod_records` to 150 K.
If it's still slow, merge `ingest` and `enrich` into one Spark app — three sequential Spark
submissions is the dominant cost, and each one is ~30 s of AM startup before any work
happens. Record the decision in RUNBOOK.

---

## 8. Phase 5 — Run lifecycle, watchdog, and reset

### 8.1 Entry point and concurrency

`bin/bankdemo run` is a thin wrapper:

```bash
#!/usr/bin/env bash
set -Eeuo pipefail
PY=/opt/bankdemo/venv/bin/python

# Only run, test-fault and reset serialise on the lock; everything else passes
# straight through. Dispatching on $1 is not optional -- without it, `bankdemo health`
# becomes `run-inner health`.
case "${1:-}" in
  run|test-fault|reset) ;;
  *) exec "$PY" -m bankdemo.cli "$@" ;;
esac

exec 9>/run/lock/bankdemo.lock
flock -n 9 || { echo "BUSY: another bankdemo run is in progress"; exit 75; }

"$PY" -m bankdemo.cli preflight-run || exit $?   # deep reset if DIRTY (outside the run clock)

rc=0
timeout --signal=TERM --kill-after=180 810 "$PY" -m bankdemo.cli run-inner "$@" || rc=$?

# timeout(1) reports 124 on TERM-expiry and 128+n if the child died of a signal.
# Map both onto the documented ABORTED code; propagate everything else unchanged.
case $rc in
  124|137|143) rc=3 ;;
esac

if [ $rc -ne 0 ]; then
  "$PY" -m bankdemo.cli reset --after-abort || touch /var/lib/bankdemo/DIRTY
fi
exit $rc
```

Two things the earlier draft of this wrapper got wrong, both of which the Phase 5
acceptance checks below would have caught:

- **`--kill-after=30` is not survivable.** The documented SIGTERM handler has to revert every
  fault, stop six daemons, and write a bundle. 30 s guarantees a SIGKILL mid-revert, leaving
  the host dirty at exactly the moment reset matters most. 180 s is the minimum that fits.
- **`exit $rc` cannot produce exit 3.** `timeout` returns 124, so the acceptance check
  asserting exit code 3 could never pass. Hence the `case` above.

Exit codes: `0` run completed (faults are expected, so they don't count as failure), `2` INFRA_ERROR
(stack unhealthy for reasons unrelated to chosen faults), `3` ABORTED (watchdog or exception),
`64` bad arguments, `75` busy. The final stdout lines are always `RUN_ID=…` and
`BUNDLE=…` (empty if no bundle was produced) — and they are printed on **every** path,
including aborts, because §11.3 parses them.

**On the 15:00 constraint.** The phase budgets in §8.2 sum to ≤ 13:00 and the watchdog fires
at 13:30 (810 s), so a normal run (exit 0) finishes inside 15:00 as CLAUDE.md requires. The
aborted path may reach ~16:30 before SIGKILL. That is accepted: an abort is not a run, it is
a failure being cleaned up safely, and truncating the cleanup to protect a number is how you
get a dirty host.

Argument validation is strict because sudo allows wildcards (§11.1):
`--seed` matches `^[0-9]{1,9}$`; `--faults` matches `^F[0-9]{2}(,F[0-9]{2}){0,2}$` and each id must exist and be enabled;
`--source` is one of `cli|cron|github|test`. Reject everything else with exit 64.
`run_id` arguments must match `^R[0-9]{8}-[0-9]{6}-[0-9]{1,9}$`, and `grade` only accepts an RCA
file path under `/home/bankops/` or `/tmp/` that is a regular file < 1 MB (it runs as root).

### 8.2 Run state machine and time budget

| Phase | Starts at | Ends by | What happens |
|---|---|---|---|
| PREPARE | 0:00 | 0:30 | Create RUN_DIR; archive previous service logs to `/data/log-archive/`; pick faults, params, schedule; write answer key; write feed profile; apply `pre_feed` and `pre_boot` faults |
| BOOT | 0:30 | 3:00 | Start the stack in order (§6.6); health gate (§8.3); start monitor |
| FEED | 3:00 | 3:30 | Start fraud_feed consumer and txn_stream producer; apply `feed` faults; start scheduler (FW_EOD_FILE starts waiting); start EOD generator (honours `eod_delay_seconds`) |
| BATCH | 3:30 | 8:30 | Scheduler runs the chain against a hard `batch_deadline` of 8:30; `batch` faults fire in 4:00–7:45; `stream` faults in 3:45–8:00 |
| DRAIN | 8:30, or chain finished + 30 s | 9:00 | Stop producer; `kill -CONT` the consumer if it's stopped (F06), give it 20 s, then stop it; scheduler terminates anything still running (`TERMINATED`) |
| COLLECT | 9:00 | 11:00 | Collector (§9) runs **while faults are still active**, so snapshots show the broken state |
| CLEANUP | 11:00 | 13:00 | Kill stray YARN apps; revert all faults; remove run HDFS data and landing files beyond retention; stop stack; offline invariant checks (§8.5); write run_history; assemble and tar bundle; write the demo-feed preview for `--source cron` runs (§9.6); retention |
| END | | 13:00 | Print RUN_ID and BUNDLE |

The earlier draft had DRAIN blocked until 8:00 even on a clean run and gave CLEANUP 1:30 —
which had to cover 17 fault reverts, six daemon stops, HDFS deletes, the invariant sweep and
a 60 MB tar. It could not be met, and it made the 12:00 baseline arithmetically unreachable.
The floor is gone (a finished chain drains immediately) and CLEANUP now has 2:00.

Baseline path, for reference: the chain finishes ~8:05, DRAIN ~8:35, COLLECT ~10:00,
CLEANUP ~11:30. That is the ≤ 12:00 in CLAUDE.md, with real margin rather than none.

Implementation notes:

- A monotonic-clock `Deadline` object is passed everywhere; each phase checks remaining time and shortens itself. **At 11:00 elapsed, COLLECT is cut short and CLEANUP starts unconditionally.**
- The fault engine runs in a thread with a scheduled queue. If a fault's time falls after DRAIN starts, it's skipped and recorded as `SKIPPED_LATE` in the answer key (so grading stays honest).
- `timeline.log` lines: `…|INFO|orchestrator|phase=BOOT status=START`, `…|phase=BOOT status=END elapsed=97s`.
- The orchestrator sets `/proc/self/oom_score_adj` to -900 and ignores SIGHUP.
- SIGTERM from the watchdog: stop starting new work, run revert-all, stop the stack, write whatever bundle exists, exit 3.

### 8.3 Health gate

After BOOT, check: NN safe mode OFF, live DataNodes = 2, NodeManagers active = 1, Kafka API
reachable, Postgres ready, `/data` mounts present, MemAvailable > 3 GB. That last threshold is
a **boot-time** gate — §2.3 predicts ~5.1 GB with the stack idle and no containers running.
Do not reuse it mid-run, where ~2.1 GB is the healthy figure.
Components targeted by `pre_boot` faults are excluded from the gate (e.g. F08 excludes Kafka).
If the gate fails for a non-excluded component, retry once after 20 s. If it still fails,
outcome = `INFRA_ERROR`: skip remaining faults, **still run COLLECT** (the evidence is useful),
run CLEANUP, touch `DIRTY`, exit 2.

### 8.4 Service log archiving at run start

Move every file under `/var/log/hadoop/`, `/var/log/kafka/`, and the NM userlogs dir into
`/data/log-archive/<previous_run_id or timestamp>/`, then `gzip` in the background. Keep the
last 10 archives. PostgreSQL logs: record the current log file name and byte offset, and at
collect time copy only bytes after that offset. This way each bundle contains only this
run's service logs.

### 8.5 Reset

**Normal reset** (end of every run, and `bankdemo reset`):

1. If RM is up: `yarn application -kill` for every app in RUNNING/ACCEPTED.
2. Run `revert.sh` for **every** fault in the catalog (enabled or not), in reverse id order. Log each result.
   Because this runs unconditionally on every single run, **each revert must first detect
   whether its condition is actually present and return immediately if not** (§10.1). A revert
   that unconditionally starts a service and waits 60 s for its API costs nothing on the run
   that injected it and everything on the other sixteen — seventeen of those would blow the
   whole CLEANUP budget on their own.
3. Kill leftover workload processes from `pids.json`, then `pkill -f 'bankdemo.(generators|consumers)'` as a fallback.
4. Delete HDFS data for runs older than the retention (keep the last 3 runs under `/bank/*/dt=*/run=*`), plus `/spark-history` entries older than 3 days, while HDFS is still up. Also delete `/data/landing/eod/*` for all but the newest 3 runs and empty `/data/landing/spool/` — that filesystem has ~8192 inodes total and invariant 6 asserts usage stays under 20%, so nothing else keeps it in bounds.
5. Stop the stack (reverse order).
6. Offline invariants. Any failure touches `/var/lib/bankdemo/DIRTY` with the reason:
   - no fault fill files (`/data/hdfs/*/bankdemo_fill*`, `/data/landing/spool/*`)
   - `tc qdisc show dev lo` shows no netem
   - `systemctl cat kafka` shows no bankdemo drop-in
   - each live rendered config's sha256 equals a fresh render into a temp dir (which is why §6.2 forbids timestamps or other per-render values inside rendered configs, and why `/etc/hadoop/client` is rendered rather than symlinked)
   - mounts present; nn/dn usage < 60%; landing inode usage < 20%
   - postgresql active; no `bankdemo-stress` unit
   - no java processes owned by hdfs, yarn, or kafka remain

**Deep reset** (`bankdemo reset --deep`, or automatically before a run when `DIRTY` exists; runs outside the run clock):

1. Stop all units; `pkill -9 -u hdfs,yarn,kafka java` if needed.
2. Normal-reset steps 2–3 (reverts).
3. Re-run installer stages 50–70 with `--force-render` (configs and units).
4. `80-init-cluster.sh --force` (wipes and reformats HDFS and Kafka, reloads ref data, rebuilds spark archive, recreates topics). Postgres tables keep history; delete `job_control` rows older than 30 days.
5. Full health check with the stack started, then stop it. Remove `DIRTY` only if everything passed.

Target: deep reset ≤ 5 minutes. It runs outside the run clock, so it does not consume the
13:00 budget — but `preflight-run` invoking it does extend the user-visible wall time, which
§11.3's `timeout-minutes: 25` has to accommodate.

**Acceptance (Phase 5):**
- Five back-to-back `bankdemo run --no-faults` complete, each ≤ 12:00, each followed by clean invariants (no DIRTY).
- Simulate a hang (`--debug-hang-at BATCH`, a hidden flag that sleeps forever): the watchdog sends SIGTERM at 13:30, the handler reverts and stops the stack inside the 180 s grace, **exit code 3** (not 124 — see the `case` in §8.1), reset runs, next run starts clean.
- `bankdemo health` while a run holds the lock returns its normal output and exit status rather than BUSY — proving the §8.1 dispatch works and only the three mutating subcommands serialise.
- `touch /var/lib/bankdemo/DIRTY` → next `bankdemo run` performs a deep reset first and then a normal run.
- Two concurrent `bankdemo run` calls: the second exits 75 immediately.

---

## 9. Phase 6 — Evidence collection, ticket, answer key, grading

### 9.1 Snapshot commands (`collector.py`)

Each command runs with a 30 s timeout. Its output goes to `snapshots/<NN>_<name>.txt`. If a
command fails or times out, the file contains the command, exit code, and stderr. That is
itself evidence (e.g. `hdfs dfsadmin -report` failing because the NN is in trouble).

| Group | Commands |
|---|---|
| Services | `systemctl status <each unit> --no-pager -l`; `systemctl cat kafka hadoop-resourcemanager`; `journalctl -u <all bankdemo units> -u postgresql --since @<run_start_epoch> --no-pager` |
| HDFS | `hdfs dfsadmin -report`; `hdfs dfsadmin -safemode get`; `hdfs fsck /bank -blocks` (summary tail only if huge); `hdfs dfs -ls -R /bank/*/dt=<bd>/run=<run_id>`; `hdfs dfs -count -q -h /bank`; `curl -s bankdemo:9870/jmx?qry=Hadoop:service=NameNode,name=FSNamesystem*` |
| YARN | `yarn node -list -all`; RM REST `/ws/v1/cluster/apps?startedTimeBegin=<ms>` (JSON); `yarn queue -status etl`; `yarn queue -status adhoc`; RM REST `/ws/v1/cluster/scheduler` |
| App logs | For each app started in the run: wait ≤ 30 s for aggregation, then `yarn logs -applicationId <id>` → `jobs/yarn_<id>.log`; if aggregation isn't done, copy NM local userlogs for that app |
| Spark | `hdfs dfs -get /spark-history/<app_id>*` → `jobs/eventlogs/` |
| Kafka | `kafka-topics.sh --describe`; `kafka-consumer-groups.sh --describe --group fraud-feed`; the monitor's lag series → `snapshots/kafka_lag_series.log` |
| Database | `pg_isready`; `SELECT * FROM job_control WHERE run_id=…`; `recon_result` for run; top 10 of `settlement_summary` by amount |
| OS | `df -h`; `df -i`; `free -m`; `uptime`; `top -b -n1 \| head -40`; `ps -eo pid,ppid,user,stat,rss,etime,args --sort=-rss \| head -50`; `ss -tlnp`; `dmesg -T \| tail -200`; `tc qdisc show`; `cat /proc/meminfo`; `ls -la /data/landing/eod`; `find /data/landing -xdev -type f \| wc -l` |
| Config | Copies of `/etc/hadoop/yarn/*.xml`, `/etc/hadoop/nn/hdfs-site.xml`, `/etc/kafka/server.properties`, `/etc/spark/spark-defaults.conf` |

Service log files: copy `/var/log/hadoop/**`, `/var/log/kafka/*.log`, the PostgreSQL log slice,
and `$RUN_DIR/logs/*`. Any file > 20 MB keeps only its last 20 MB, with a first line
`[truncated by collector: kept last 20MB]`.

**Redaction.** The bundle is published as a CI artifact (§11.3), and on a public repo that is
world-downloadable, so treat this as a security control rather than tidiness.

- Never copy `/etc/bankdemo/secrets.env`, and never copy anything under `/var/lib/bankdemo/keys/`.
- **Redact on write, not as a post-pass.** A post-pass over a directory that has already been
  tarred, or that a later step reads, leaves a window; filter each artifact's bytes as it is
  written.
- The pattern set must cover more than `password=`. At minimum, case-insensitively:
  `PG_PASSWORD`, `PGPASSWORD`, `BANKDEMO_FAULT_SALT`, `password=`, `&password=`,
  `-Dspark\..*password`, `jdbc:postgresql://[^\s]*:[^\s]*@`, and `Authorization: *`.
  Replace the value with `***`.
- The high-risk sources are not the ones you would guess: the `ps -eo … args` snapshot, the
  YARN container launch command inside `yarn logs`, and the RM REST `/ws/v1/cluster/apps`
  diagnostics all carry full command lines. §6.3 keeps the password off the command line in
  the first place; this is the second layer.
- A unit test asserts that a bundle built from a fixture containing each pattern comes out
  clean.

### 9.2 Bundle layout

**The bundle is assembled in its own directory and `RUN_DIR` is never tarred.** This is the
single most important rule in this section. `RUN_DIR` (§7.1) contains `state/`, which holds
`feed_profile.json` (whose `skew_merchant`, `drop_records` and `schema_drift` fields give away
F05, F09 and F10 outright) and `state/faults/<id>/actions.log` (which §10.3 explicitly says
must never be bundled). Tarring `-C /data/runs <run_id>` would publish all of it.

Build into `/data/runs/<run_id>.bundle/` by **copying in an explicit whitelist** — never by
copying `RUN_DIR` and deleting things afterwards, which fails open the first time someone adds
a new subdirectory.

```
<run_id>.bundle/
├── README.txt                # how to approach the bundle; no hints about causes
├── ticket.json
├── RCA_TEMPLATE.yaml
├── run_meta.json             # run_id, seed, business_date, start/end, outcome, phase timings
├── logs/                     # timeline.log, scheduler.log, alerts.log, txn_stream.log, fraud_feed.log, generator.log
├── jobs/                     # per-job stdout/stderr, yarn app logs, spark event logs
├── services/                 # hadoop, kafka, postgres log files
├── snapshots/
└── config/
```

Explicitly **not** copied: `RUN_DIR/state/` in its entirety.

Tar with `tar -czf /data/runs/<run_id>.tar.gz -C /data/runs <run_id>.bundle`, `chown bankops`,
remove the staging directory, keep the newest 20 bundles. Target size < 60 MB. Log a warning
if exceeded.

### 9.6 Demo feed — publishing fresh incidents to the public site

The GitHub Pages demo serves visitors a **real, recent incident** rather than a canned
example, and it does so instantly. Cron runs six times a day (§11.2) and each run selects
1–3 faults at random, so the last 20 runs are a continuously replenishing pool of genuinely
distinct incidents. A visitor cannot tell whether the run happened on their click or ninety
minutes ago, which is the whole point: a live trigger would make them wait ~13 minutes for a
run to finish, and nobody waits.

**Two artefacts per run, deliberately split by size.**

| Artefact | Size | Where | Why |
|---|---|---|---|
| `demo_preview.json` | a few KB | committed to `docs/demo-feed/<run_id>.json` in the repo | Same origin as GitHub Pages, so the page `fetch()`es it with **no CORS configuration anywhere** |
| Full bundle `.tar.gz` | < 60 MB | stays on the VM; a curated few attached to a GitHub Release (roadmap B6.1) | A plain download link needs no CORS, and no page should pull 60 MB to render a summary |

`CLEANUP` writes the preview alongside the bundle into `/data/demo-feed/<run_id>.json` and
rewrites `/data/demo-feed/index.json` with the newest 20 (run_id, business_date, started_at,
duration, primary ticket `short_description`, fault count). The preview carries:

- `ticket.json` in full
- every line of `logs/alerts.log`
- the `job_control` rows for the run (job, status, timings, SLA outcome)
- phase timings from `run_meta.json`
- **for demo-class runs only**, the resolved fault list with `root_cause`, `resolution` and
  `preventive_action` — the published answer

**Demo class vs practice class.** These are different things and must not be conflated:

| Class | Trigger | Preview published | Answer published |
|---|---|---|---|
| **demo** | `--source cron` | yes | **yes, deliberately** — explaining the answer is the demo's job |
| **practice** | `--source cli` (you, working a bundle) | no | never — the key stays in `/var/lib/bankdemo/keys/` |

Because demo runs publish `(seed → faults)` pairs, they use a **separate salt**
(`BANKDEMO_DEMO_SALT`) from the practice pool's `BANKDEMO_FAULT_SALT` (§10.2). Publishing
demo answers then reveals nothing about any run you intend to practise on, and the two pools
stay independent no matter how many demo bundles accumulate.

**Publication keeps write credentials off the VM.** A scheduled workflow
(`.github/workflows/publish-demo-feed.yml`, every 6 hours) joins the tailnet exactly as
§11.3 does, rsyncs `/data/demo-feed/` into `docs/demo-feed/`, and commits only if something
changed. The VM never holds a GitHub token, a deploy key, or push access — it only ever
serves files to an authenticated puller. Guard the commit step with a check that
`index.json` parses and every referenced preview exists, so a half-written feed can never be
published.

**Acceptance:** after three cron runs, `docs/demo-feed/index.json` lists three entries, each
resolving to a preview file under 32 KB; the demo page renders a random entry and the
"different incident" control swaps it without a network round trip to anything but the repo's
own origin; and no preview for a `--source cli` run ever appears.

### 9.7 Visitor-triggered runs (optional, additive)

`infra/demo-broker/` specifies an optional Cloudflare Worker that lets a demo-site visitor
request a fresh run and watch it happen live, while the VM stays outbound-only. It is
**strictly additive**: the demo feed in §9.6 must keep working with the broker deleted, and
nothing in this guide may take a dependency on it.

Three hooks on this side, all of which are no-ops when the broker is absent:

1. **`bankdemo poll-requests`** — a subcommand driven by a 2-minute systemd timer. It tests
   `/run/lock/bankdemo.lock` with `flock -n` and exits immediately if held, so a visitor run
   can never contend with cron or with your own practice runs. It enforces its own daily cap
   from `/var/lib/bankdemo/state/demo-runs-<date>` rather than trusting the broker's.
2. **`--source demo`** — a demo-class run (§9.6): random seed, no other arguments accepted,
   `BANKDEMO_DEMO_SALT`, publishes a preview with the answer.
3. **`--progress-url URL`** — the orchestrator POSTs each `timeline.log` phase transition and
   each `alerts.log` line as it is written, so the visitor watches the stack boot, break and
   get collected in real time. **Every failure of this POST is logged and ignored.** A run must
   never fail because a telemetry endpoint is unreachable.

Add `BANKDEMO_POLL_TOKEN` and `BANKDEMO_SUBMIT_KEY` to the redaction pattern set in §9.1 when
implementing this; they live in `secrets.env`, which is never collected, but the belt-and-braces
rule there is to redact by pattern as well.

### 9.5 Bundle contract (what `agent/` consumes)

The bundle is an interface, not just an output. `agent/evidence.py` and the triage agent read
it, so changes to the layout above are breaking changes. Write the contract down in
`contract/bundle_v1.md` and version it:

- `run_meta.json` carries `bundle_version: 1`. A consumer that does not recognise the version
  refuses rather than guessing.
- `logs/alerts.log`, `logs/scheduler.log` and `logs/timeline.log` are the three files the
  agent parses structurally; everything else it treats as opaque text to grep. Their line
  format (`ISO8601|LEVEL|component|message`) is therefore frozen for v1.
- `ticket.json` is the agent's entry point and maps onto the existing incident record —
  `short_description` → the incident summary, `opened_at` → `opened_at`, `service` →
  `affected_job`. It deliberately does **not** carry a severity: severity is
  `agent/severity.py`'s job, from the signals, and pre-labelling it would make the scorecard
  circular.
- The answer key is not part of the contract and never appears in the bundle. Grading joins
  key to bundle by `run_id`, on the VM, after the fact.

### 9.3 Ticket and answer key

`ticket.json` is built from the primary fault: highest severity, earliest on ties. Secondary
faults surface only through alerts and logs, as they would in real life.

```json
{
  "number": "INC0<run date><seed, zero-padded to 4 digits>",
  "opened_at": "2026-09-17T14:39:10-04:00",
  "priority": "P2",
  "state": "New",
  "assignment_group": "Card Platforms App Support L2",
  "service": "Card Settlement Batch",
  "reported_by": "Batch Monitoring",
  "short_description": "EOD settlement batch running slow; HDFS health alert raised",
  "description": "Automated ticket. Business date 2026-09-17. Please investigate and update with RCA.",
  "business_impact": "Merchant settlement files for business date may be delayed"
}
```

`opened_at` = the first CRITICAL alert time after the primary fault's injection, or
injection + 90 s if no alert fired. For `--no-faults` runs, the ticket says
`"short_description": "Routine: confirm EOD batch health for business date"` with P4.

Answer key (`/var/lib/bankdemo/keys/<run_id>.json`, root 0600, never bundled).

**The key file is not the only thing that has to stay secret — the seed is too.** See §10.2:
because the seed is embedded in `run_id`, it appears in every path and log line in the bundle,
and it cannot be removed. If fault selection were a pure function of the bare seed, anyone
with the bundle and this repository could recompute the answers exactly, and the CI artifact
in §11.3 would publish them. The salted-HMAC rule in §10.2 is what closes that.

```json
{
  "run_id": "R20260917-143205-8812",
  "seed": 8812,
  "faults": [
    {
      "id": "F01", "title": "DataNode process crash", "params": {"datanode": 2},
      "scheduled_at": "…", "injected_at": "…", "verified": true, "reverted_at": "…",
      "status": "INJECTED",
      "root_cause": "…", "resolution": "…", "preventive_action": "…",
      "evidence": [{"file": "…", "pattern": "…", "found": true}]
    }
  ],
  "actions_log": "/var/lib/bankdemo/keys/<run_id>.actions/"
}
```

`status` is one of `INJECTED`, `INJECT_FAILED`, `INJECT_UNVERIFIED`, or `SKIPPED_LATE`. The
collector fills `evidence[].found` after the bundle is built.

### 9.4 Reveal and grade

`bankdemo reveal <run_id>` pretty-prints the answer key, with evidence found/not found.

`RCA_TEMPLATE.yaml` (included in the bundle):

```yaml
run_id: ""
time_spent_minutes: 0
issues:
  - summary: ""
    component: ""            # hdfs | yarn | spark | kafka | data | db | os | change
    fault_id_guess: ""       # optional, from docs/FAULTS.md
    first_evidence_file: ""
    first_evidence_time: ""  # ISO-8601
    root_cause: ""
    resolution: ""
    preventive_action: ""
business_impact: ""
```

`bankdemo grade <run_id> <path/to/rca.yaml>` (the file can be scp'd to the VM, or grading can
run locally with a copied key; implement both via `--key-file`).

It runs as root against a user-supplied file, so: parse with `yaml.safe_load`, never
`yaml.load`; and resolve the path with `os.path.realpath` **after** opening with `O_NOFOLLOW`,
then re-check that the resolved path is still under `/home/bankops/` or `/tmp/` — checking the
path before opening it is a symlink race, and `/tmp` is exactly where that matters.

- For each actual `INJECTED` fault, find the best matching issue. Component match = 1 point; fault id match = 1 point; `first_evidence_time` within [injected_at, injected_at + 5 min] = 1 point.
- The same scoring function grades a human's `rca.yaml` and the agent's incident record — `grading.py` takes a normalised list of issues, and there are two thin adapters. That is what makes `docs/scorecard.md` (roadmap Phase B5) a like-for-like comparison rather than two unrelated numbers.
- Issues that match no actual fault count as false positives. List them, with no penalty in v1.
- Print a table: actual fault, your matching issue, points, plus model root cause / resolution / preventive action side by side with yours. Free text isn't auto-scored.
- Append the result to `/var/lib/bankdemo/grades.jsonl`, and `bankdemo stats` prints averages by category.

**Acceptance (Phase 6):**
- A `--no-faults` run bundle contains every section in §9.2, is < 60 MB, and contains no password string (`grep -ri password` shows only redacted values).
- The answer key exists with 0600 root, and is absent from the tarball. Test it properly rather than with `grep -i key` on the file list: extract the bundle to a temp dir and assert that no file contains the run's `skew_merchant` value, any fault id from the key, or the string `root_cause`, and that `state/` does not exist in the archive.
- A bundle from a run with `--faults F05,F09,F10` contains no trace of `feed_profile.json`'s contents — the specific regression §9.2 exists to prevent.
- `bankdemo grade` works against a hand-written rca.yaml for a synthetic answer key in `tests/fixtures/`; unit tests cover matching and scoring.

---

## 10. Phase 7 — Fault framework and catalog

### 10.1 Fault package contract

Each fault lives in `faults/Fxx-<slug>/`:

`fault.yaml`
```yaml
id: F01
slug: datanode-crash
title: DataNode process crash
category: hdfs            # hdfs | yarn | spark | kafka | data | db | os | change
component_group: hdfs-datanode   # at most one fault per group per run
severity: P2              # drives ticket priority
enabled: true             # false excludes it from random selection AND from --faults;
                          # `bankdemo list-faults` prints this column
window: batch             # pre_boot | pre_feed | feed | batch | stream
duration_seconds: null    # null = persists until reset; or N = self-reverts after N s
requires: []              # e.g. [netem]
conflicts_with: [F03]
ticket:
  short_description: "EOD settlement batch running slow; HDFS health alert raised"
  reported_by: "Batch Monitoring"
  service: "Card Settlement Batch"
evidence:                  # regexes that MUST appear somewhere in the bundle; used by test-fault
  - file: "snapshots/*dfsadmin_report*"
    pattern: "Dead datanodes \\(1\\)"
  - file: "logs/alerts.log"
    pattern: "UNDER_REPLICATED_BLOCKS"
answer:                    # copied ONLY to the answer key
  root_cause: "hdfs-datanode@2 process terminated; blocks on it lost one replica"
  resolution: "Restart DataNode 2, confirm registration, watch re-replication to 0 under-replicated"
  preventive_action: "Process supervision with alerting on DataNode heartbeat loss"
```

Scripts: `inject.sh`, `revert.sh`, `verify.sh`. Each receives the environment
`RUN_ID`, `RUN_DIR`, `SEED`, `FAULT_PARAMS_JSON` (random params chosen by the orchestrator),
and everything from `/etc/bankdemo/cluster.env`.

Window bounds are **not** per-fault; they are one table in `config/cluster.env`, derived from
§8.2, so the scheduler and the orchestrator cannot drift apart:

| window | start | end | notes |
|---|---|---|---|
| `pre_boot` | — | — | applied in PREPARE, before the stack starts |
| `pre_feed` | — | — | applied in PREPARE; modifies the feed profile only |
| `feed` | 3:00 | 3:30 | service/OS actions before the generators write |
| `batch` | 4:00 | 7:45 | |
| `stream` | 3:45 | 8:00 | |

- `inject.sh`: performs the fault. Exit 0 on success.
- `verify.sh`: exits 0 if the fault's direct effect is present (e.g. the unit is inactive). Called 10–30 s after inject. A failure is logged to the orchestrator timeline as `INJECT_UNVERIFIED`; the run continues.
- `revert.sh`: **idempotent**, safe to run when the fault never ran, exits 0 if already clean. Must not depend on files that only `inject.sh` creates, except for optional state in `$RUN_DIR/state/faults/<id>/`.
  It must also be **fast when there is nothing to do**: §8.5 runs every fault's revert on every
  reset, so the first thing a revert does is *detect* its condition (is the unit inactive? does
  the fill file exist? is there a netem qdisc? is the drop-in present?) and return 0 immediately
  if not. Only after detecting drift may it act, wait, or restart anything. Budget: under 200 ms
  for the no-op path. Seventeen reverts that each start a service and wait for its API would
  exceed the entire CLEANUP window on their own.
- `pre_feed` faults don't touch services. They modify `$RUN_DIR/state/feed_profile.json` with `jq` (their revert is a no-op). A fault that needs both a service/OS change **and** a feed-profile change declares `window: pre_boot` and is allowed to touch the profile in PREPARE — F08 is the only one, and §10.3 flags it.
- `feed` faults run service/OS actions during the FEED phase, before the generators start writing.
- `pre_boot` faults apply before the stack starts. The health gate then excludes the components they target.

### 10.2 Selection and scheduling (`faults.py`)

1. Load all fault.yaml files and drop any whose `requires` aren't met or whose `enabled` is false.
2. Derive the fault RNG from a **salted** seed:
   ```python
   digest = hmac.new(salt.encode(), str(seed).encode(), hashlib.sha256).digest()
   rng = random.Random(int.from_bytes(digest[:8], "big"))
   ```
   where `salt` is `BANKDEMO_FAULT_SALT` from `/etc/bankdemo/secrets.env` (generated once at
   install, 0640 root:bankops). **This is what keeps the answer key secret.** The bare seed is
   embedded in `run_id` and therefore in every bundle path and log line; if selection were
   `random.Random(seed)`, the bundle would carry its own answers and §11.3 would publish them
   to anyone who can download a CI artifact. Generator randomness stays on the **bare** seed,
   so same-seed data reproducibility (Phase 4 acceptance) is unaffected. A consequence worth
   stating plainly: reproducing a run on a different host requires copying the salt.
3. If `--faults` was given, use exactly those. Otherwise `k = rng.choices([1,2,3], weights=[0.45,0.40,0.15])[0]`.
4. Sample faults one at a time, rejecting any candidate that shares a `component_group` with, or `conflicts_with`, an already chosen fault. Stop if no candidates remain.
5. Schedule each fault inside its window (bounds in §10.1) at `window_start + rng.uniform(0, window_len - 30)`, keeping ≥ 45 s between faults in the same window. **Bound the rejection loop**: try at most 50 draws, then fall back to placing the faults at evenly spaced deterministic offsets across the window. Unbounded rejection sampling can fail to terminate for 3 faults in a narrow window, and a non-terminating scheduler is not a deterministic one.
5. Choose parameters, e.g. which DataNode (`rng.choice([1,2])`), skew merchant id, drop count (`rng.randint(3,250)`).
6. Write the **answer key** (§9.3) to `/var/lib/bankdemo/keys/<run_id>.json` (0600) before injecting anything. Write only `seed` and `fault_count_hint: hidden` into `run_meta.json` in the bundle.
7. The timeline log (`$RUN_DIR/logs/timeline.log`) records phases and generic markers like `CHANGE_WINDOW_EVENT` for change-type faults (realistic: changes are logged), but **not** fault ids for other types.

Unit tests: the same seed yields the same selection, schedule, and params; conflicts are never violated; `--faults` overrides the random choice.

### 10.3 Catalog v1

| ID | Title | Window | Inject | Revert | Key evidence (examples) |
|---|---|---|---|---|---|
| F01 | DataNode crash | batch | `systemctl kill -s SIGKILL hadoop-datanode@N` | `systemctl start hadoop-datanode@N`; wait for 2 live | `Dead datanodes (1)`; under-replicated blocks > 0 in fsck; alerts UNDER_REPLICATED_BLOCKS |
| F02 | NameNode resource-low safe mode | batch | `fallocate` a file on `/data/hdfs/nn` leaving < 200 MB free | Remove the file; `hdfs dfsadmin -safemode leave`; confirm OFF | NN log `NameNode low on available disk space`; `SafeModeException` in job logs |
| F03 | DataNode volume full | batch | `fallocate` on dn1 **and** dn2 mounts to ≥ 98% | Remove the files | Job log `could only be written to 0 of the 1 minReplication nodes`; alerts volume CRITICAL |
| F04 | YARN queue starvation | batch | Submit 3 long-sleep apps to `adhoc` (`yarn jar …hadoop-mapreduce-examples… sleep -m 2 -mt 600000` or a PySpark `time.sleep`) just before SPK_INGEST | `yarn application -kill` every app in `adhoc` | `SPK_*` state ACCEPTED > 120 s; RM diagnostics mention queue/AM resource limit; SLA_MISSED |
| F05 | Executor OOM from data skew | pre_feed | Feed profile `skew_ratio=0.85`, `skew_merchant=<random>`, `eod_records=300000` | no-op | `Container killed by YARN for exceeding physical memory limits` or `java.lang.OutOfMemoryError`; SPK_SETTLE FAILURE |
| F06 | Fraud consumer hung | stream | `kill -STOP <fraud_feed pid>` | `kill -CONT <pid>` if the process exists | Lag climbs in `kafka-consumer-groups` snapshots; process state `T` in `ps` snapshot; no new part files |
| F07 | Kafka broker down | stream | `systemctl stop kafka` | `systemctl start kafka`; wait for API | Producer log `NoBrokersAvailable`/timeouts; alerts service down |
| F08 | Kafka too many open files | pre_boot | Drop-in `LimitNOFILE` for kafka.service; the same drop-in replaces `ExecStart` to add `--override log.segment.bytes=1048576`; set feed profile `stream_rate_per_sec` to 3× so segment files accumulate | Remove the drop-in; `daemon-reload` | `Too many open files` in server.log **while the broker is up and serving**. See the note below — this fault is invalid, not merely untuned, if the broker fails to start |
| F09 | Upstream schema drift | pre_feed | `schema_drift=extra_column` or `date_format` (random) | no-op | LOAD_EOD or SPK_INGEST FAILURE with a column-count or parse error; quarantine rows present |
| F10 | Reconciliation break | pre_feed | `drop_records` or `duplicate_records` = random 3–250 | no-op | RECON status BREAK; diff_count ≠ 0 in recon_result snapshot |
| F11 | Late upstream file | pre_feed | `eod_delay_seconds=240` (beyond the watcher timeout) | no-op | FW_EOD_FILE TERMINATED; downstream ON_HOLD; file appears later (landing listing at collect time) |
| F12 | Postgres down | batch | `systemctl stop postgresql` shortly before SPK_SETTLE | `systemctl start postgresql` | `Connection refused` / `PSQLException` in settle log; alert Postgres not ready |
| F13 | HDFS permission drift ("bad CR") | batch | As hdfs: `hdfs dfs -chown -R etlsvc:etlsvc /bank/curated` and `-chmod 750` (create the fake `etlsvc` principal name; no OS user is needed) | Restore `bankops:bankops 0750` recursively | `AccessControlException: Permission denied: user=bankops` |
| F14 | Bad config change on RM | batch | Timeline `CHANGE_WINDOW_EVENT CR=CHG<random>`; inject a malformed value (e.g. `yarn.nodemanager.resource.memory-mb=4O96` with a letter O) into `/etc/hadoop/yarn/yarn-site.xml`; restart RM | Re-render config from template; restart RM; wait for NM registration | RM fails to start; `NumberFormatException` in RM log; `journalctl -u hadoop-resourcemanager` |
| F15 | Landing zone inode exhaustion | feed | Create tiny files in `/data/landing/spool/` until `df -i` ≥ 99% (start 60 s before generator) | `find /data/landing/spool -type f -delete` | Generator `No space left on device` while `df -h` shows free space; `df -i` snapshot 100% |
| F16 | Host memory pressure | batch | `systemd-run --unit=bankdemo-stress --property=MemoryMax=2G stress-ng --vm 2 --vm-bytes 800M --timeout 180s` | `systemctl stop bankdemo-stress` (and it self-expires) | NM or executor lost; possibly `oom-kill` in `dmesg`/journal; MemAvailable alert |
| F17 | Network latency on loopback/host | batch | `tc qdisc add dev lo root netem delay 100ms 25ms` (duration 150 s) | `tc qdisc del dev lo root` (ignore "no such qdisc") | Heartbeat/RPC slowness; job runtimes spike; SLA_MISSED. Requires netem |

Notes for implementers:

- **F08** is the hardest to reproduce, and it has a specific failure mode that makes it *wrong*
  rather than merely flaky: if `LimitNOFILE` is low enough that the broker cannot **start**
  (512 very likely is), the run is indistinguishable from F07 "Kafka broker down" — same
  symptoms, same evidence, different answer key. Because §8.3 excludes Kafka from the health
  gate for `pre_boot` faults, nothing would catch it. So `verify.sh` for F08 must assert the
  broker came up and is serving the API, and *then* that the error appears under load. Start
  at `LimitNOFILE=2048` and walk down. If it cannot be made reliable within ~1 hour, mark it
  `enabled: false` with a RUNBOOK note.
- **F05** likewise depends on data volume. Increase `eod_records` or `skew_ratio` inside the
  fault until the OOM appears in 3 of 3 test runs, while staying within the time budget. The
  executor container is 1024 MB (§2.3), and `pmem-check-enabled` is what kills it.
- **F16** must never kill `sshd`, the orchestrator, the NameNode, the ResourceManager, or
  PostgreSQL. Three layers enforce that: `OOMScoreAdjust=-1000` on sshd and `-500` on
  NN/RM/PostgreSQL (§2.3); `/proc/self/oom_score_adj = -900` on the orchestrator; and
  `MemoryMax=2G` on the transient stress unit so the cgroup kills `stress-ng` first if it
  overshoots. The earlier `--vm-bytes 2200M` figure predates the §2.3 rework and would have
  driven `MemAvailable` to zero, taking the NameNode with it — which is unrevertable inside
  the run budget. Phase 3.5 (§6.8) is where these numbers get confirmed on real hardware.
- **F17** runs on `lo` because traffic to the host's own IP is routed through the loopback
  device, so this delays HDFS RPC, WebHDFS, Kafka, PostgreSQL, the monitor's JMX polls **and**
  the collector's own `curl` calls. That breadth is why 300 ms was too aggressive: it inflates
  every phase and cascades into unrelated failures. 100 ms is enough to spike job runtimes and
  miss an SLA without destabilising the run.
- **F14 and F04** share `component_group: yarn-rm`. F12 and F13 can combine with others. F06 and F07 share `kafka-stream`. F02 and F03 share `hdfs-storage`.
- Every inject and revert logs to `$RUN_DIR/state/faults/<id>/actions.log`. That file goes to the answer key location, **not** the bundle.

### 10.4 `bankdemo test-fault <ID>`

Runs a full run with only that fault and a fixed test seed. After collection, it checks
every `evidence` regex against the bundle and prints PASS/FAIL per signature. Then it runs
reset and `bankdemo health`, and fails if health isn't green. A fault is **done** when it
passes 3 consecutive times.

**Acceptance (Phase 7):** `bankdemo list-faults` shows the catalog with enabled flags;
`make test-faults` (runs on VM, sequentially) passes for every enabled fault; `pytest`
covers selection, conflict, and determinism rules; `docs/FAULTS.md` is generated **without**
the `answer` sections.

---

## 11. Phase 8 — Triggering: SSH, cron, GitHub Actions

### 11.1 Restricted sudo for `bankops` (`90-bankdemo.sh`)

Render `/etc/sudoers.d/50-bankops` and validate it with `visudo -cf` **before** installing:

```
Cmnd_Alias BANKDEMO = /opt/bankdemo/bin/bankdemo run, /opt/bankdemo/bin/bankdemo run *, \
                      /opt/bankdemo/bin/bankdemo test-fault *, /opt/bankdemo/bin/bankdemo reset, \
                      /opt/bankdemo/bin/bankdemo reset --deep, /opt/bankdemo/bin/bankdemo health, \
                      /opt/bankdemo/bin/bankdemo reveal *, /opt/bankdemo/bin/bankdemo grade *
Cmnd_Alias DEPLOY   = /usr/bin/rsync -a --delete /tmp/bankdemo-src/ /opt/bankdemo/, \
                      /opt/bankdemo/install/install.sh, /opt/bankdemo/install/install.sh *
bankops ALL=(root) NOPASSWD: BANKDEMO, DEPLOY
```

Only after the new file validates and `sudo -l -U bankops` shows the rules, delete the Phase 1
file `/etc/sudoers.d/99-bankops-bootstrap`. The `opc` user keeps its default OCI sudo as
break-glass access. **[HUMAN]** confirm you can still `ssh opc@VM sudo -v` before this stage runs.

Two practical notes:

- sudo matches the command **as invoked**. If `90-bankdemo.sh` also drops a
  `/usr/local/bin/bankdemo` symlink, then `sudo bankdemo run` is not the same string as
  `/opt/bankdemo/bin/bankdemo run` and will be refused. Either add the symlink path to the
  `Cmnd_Alias` as well, or document that the full path is required — §11.3 and the cron entry
  both already use it.
- The `DEPLOY` rsync rule is an exact-argument match, so `scripts/deploy.sh` must invoke rsync
  with those flags in that order, byte for byte. A later "harmless" flag addition breaks deploy
  with a confusing sudo error.

> Note: DEPLOY effectively gives `bankops` root (it can deploy any code that runs as root). That's
> acceptable for a personal lab VM. Document it in `docs/ARCHITECTURE.md` rather than pretending otherwise.

### 11.2 cron

`/etc/cron.d/bankdemo` (system timezone America/Toronto):

```
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin
20 6,9,12,15,18,21 * * * root /opt/bankdemo/bin/bankdemo run --source cron >> /var/log/bankdemo/cron.log 2>&1
```

Six runs a day produce fresh bundles and regular CPU, memory, and network activity. If the
optional visitor-request broker (§9.7) is deployed, its cap of 8 visitor runs/day lands on top
of these, for ~14 runs ≈ 3 hours of compute — which helps rather than hurts the reclamation
arithmetic below. Be honest about that arithmetic: idle reclamation is judged on a **95th-percentile** metric over 7 days,
so clearing it requires being busy for more than 5% of the week. Six 15-minute runs is 6.3% —
just over the line, where three runs a day (3.1%) was not. This mitigates the risk; it does not
eliminate it, and the idempotent installer remains the actual insurance. A logrotate rule for
`/var/log/bankdemo/*.log` (weekly, rotate 8, compress).

### 11.3 GitHub Actions: `.github/workflows/run-incident.yml`

**The runner reaches the VM over Tailscale, not the public internet.** The VM's security list
keeps a single `<your IP>/32` ingress rule on TCP 22 and never opens `0.0.0.0/0`; the VM dials
out to the tailnet and the runner joins it as an ephemeral node for the duration of the job.
Setup — the `tailscaled` install, the `tag:bankdemo` / `tag:ci` ACL, and the OAuth client — is
in [`docs/VM_SETUP.md`](docs/VM_SETUP.md) §4–§4a, including why `--accept-dns=false` is
mandatory on this host (MagicDNS would otherwise resolve the bare name `bankdemo` to a tailnet
address, and HDFS needs the private IP).

Repository secrets **[HUMAN]**:

| Secret | Value |
|---|---|
| `TS_OAUTH_CLIENT_ID` / `TS_OAUTH_SECRET` | Tailscale OAuth client scoped `auth_keys` write, tag `tag:ci` |
| `VM_HOST` | the VM's **tailnet** name or `100.x.y.z` address, not its public IP |
| `VM_USER` | `bankops` |
| `VM_SSH_KEY` | a **dedicated CI key**, not your personal key |
| `VM_KNOWN_HOSTS` | `ssh-keyscan -t ed25519 <tailnet address>`, taken from a machine already on the tailnet |

Tailscale provides reachability, not authentication — the SSH key is still required. In
`~bankops/.ssh/authorized_keys`, prefix the CI key with
`no-port-forwarding,no-agent-forwarding,no-X11-forwarding`.

(Tailscale SSH — `tailscale up --ssh` plus an `ssh` ACL section — would remove `VM_SSH_KEY` and
`VM_KNOWN_HOSTS` entirely by authenticating on tailnet identity. It is a reasonable later
refinement; the key-based path above is specified because it keeps working unchanged if
Tailscale is ever dropped.)

```yaml
name: Run incident simulation

on:
  workflow_dispatch:
    inputs:
      seed:
        description: "Seed (blank = random)"
        required: false
        default: ""
      mode:
        description: "Run mode"
        type: choice
        options: [random, no-faults]
        default: random

concurrency:
  group: bankdemo-vm
  cancel-in-progress: false

permissions:
  contents: read

jobs:
  simulate:
    runs-on: ubuntu-latest
    timeout-minutes: 25
    steps:
      - name: Validate inputs
        shell: bash
        env:
          SEED: ${{ inputs.seed }}
        run: |
          if [[ -n "$SEED" && ! "$SEED" =~ ^[0-9]{1,9}$ ]]; then
            echo "::error::seed must be 1-9 digits"; exit 1
          fi

      - name: Join the tailnet
        uses: tailscale/github-action@v3
        with:
          oauth-client-id: ${{ secrets.TS_OAUTH_CLIENT_ID }}
          oauth-secret: ${{ secrets.TS_OAUTH_SECRET }}
          tags: tag:ci
          # Ephemeral by default: the node deregisters when the job ends, so the
          # tailnet does not accumulate a dead machine per workflow run.

      - name: Configure SSH
        shell: bash
        env:
          SSH_KEY: ${{ secrets.VM_SSH_KEY }}
          KNOWN_HOSTS: ${{ secrets.VM_KNOWN_HOSTS }}
        run: |
          install -m 700 -d ~/.ssh
          printf '%s\n' "$SSH_KEY" > ~/.ssh/id_ed25519
          chmod 600 ~/.ssh/id_ed25519
          printf '%s\n' "$KNOWN_HOSTS" > ~/.ssh/known_hosts

      - name: Run simulation on VM
        id: run
        shell: bash
        env:
          TARGET: ${{ secrets.VM_USER }}@${{ secrets.VM_HOST }}
          SEED: ${{ inputs.seed }}
          MODE: ${{ inputs.mode }}
        run: |
          args=(run --source github)
          [[ -n "$SEED" ]] && args+=(--seed "$SEED")
          [[ "$MODE" == "no-faults" ]] && args+=(--no-faults)
          set +e
          ssh -o ServerAliveInterval=30 "$TARGET" sudo /opt/bankdemo/bin/bankdemo "${args[@]}" | tee run.out
          rc=${PIPESTATUS[0]}
          set -e
          # `|| true` is load-bearing: on a busy VM (rc=75) or an early abort there is no
          # BUNDLE= line, grep exits 1, and under `bash -e` the assignment would fail the
          # step here -- before the output is written and before the busy warning below.
          bundle=$(grep -E '^BUNDLE=' run.out | tail -1 | cut -d= -f2- || true)
          echo "bundle=$bundle" >> "$GITHUB_OUTPUT"
          if [[ $rc -eq 75 ]]; then echo "::warning::VM busy (another run in progress)"; fi
          exit $rc

      - name: Fetch bundle
        if: always() && steps.run.outputs.bundle != ''
        shell: bash
        env:
          TARGET: ${{ secrets.VM_USER }}@${{ secrets.VM_HOST }}
          BUNDLE: ${{ steps.run.outputs.bundle }}
        run: scp "$TARGET:$BUNDLE" .

      - name: Upload bundle
        if: always() && steps.run.outputs.bundle != ''
        uses: actions/upload-artifact@v4
        with:
          name: incident-bundle-${{ github.run_number }}
          path: "*.tar.gz"
          retention-days: 14
```

Security notes: inputs are passed only through `env` (never interpolated into shell text);
seed validation happens both here and in the CLI. The VM has **no inbound exposure to the
public internet** — reachability comes from the outbound tailnet connection, and the `tag:ci`
ACL permits port 22 only. Do not switch this workflow to a self-hosted runner on the VM: this
repository is public and forkable, and a self-hosted runner on a public repo is an
execution path for fork pull requests (`docs/VM_SETUP.md` §4, Option A). On a public repo,
uploaded artifacts are downloadable by anyone. Bundles contain internal hostnames and usernames but no secrets
(§9.1 redaction), no answer key, and — because of the salted selection in §10.2 — no way to
recompute the answer key from the published seed. All three of those are properties this
workflow depends on; if any of them regresses, this step publishes the answers.

**Acceptance (Phase 8):** a manual workflow dispatch produces an artifact whose bundle opens
and matches §9.2; dispatching while a cron run is active ends with the "VM busy" warning;
`grep -r "answer" bundle/` returns nothing from the answer key; three cron runs appear in
`run_history` over a day.

---

## 12. Phase 9 — Hardening, operations, documentation

- **fail2ban:** enable the `sshd` jail (EPEL package already installed): `maxretry=5`, `bantime=1h`.
- **firewalld:** default zone public, services `ssh` only. `90-bankdemo.sh` asserts this and fails otherwise.
- **Updates:** `dnf-automatic` with `upgrade_type = security`, apply without reboot. After any kernel update, stage `10-os-base.sh` re-checks `sch_netem` (the modules-extra package must match the new kernel) and updates NETEM_AVAILABLE.
- **Disk hygiene:** retention (20 bundles, 10 log archives, 3 runs of HDFS data) is enforced in CLEANUP; add `bankdemo health` checks for `/data` usage > 70% (WARN).
- **Reboot resilience:** after a VM reboot, mounts come back (including the four loop mounts), postgresql starts, the stack stays stopped, and the next run proceeds normally. Test this explicitly, and assert specifically that `hostname` is still `bankdemo` and `getent hosts bankdemo` still returns the **private IP, not 127.0.0.1** — cloud-init rewrites both on boot unless §5.2's override is in place, and the resulting WebHDFS failure surfaces days later looking like a DataNode problem.
- **Rebuild drill:** on a brand-new VM (or after `--deep` plus deleting `/opt/hadoop*`), `make deploy` to a successful `bankdemo run --no-faults` in < 30 minutes.
- **Docs:** `README.md` (what it is, a screenshot of a bundle tree, how to trigger), `docs/ARCHITECTURE.md`, `docs/RUNBOOK.md`, `docs/FAULTS.md` (generated, no answers), and `docs/PRACTICE.md` (Appendix B).

**Acceptance (Phase 9):** reboot test passes; rebuild drill passes; the fail2ban jail is
active; README is complete; all acceptance output is recorded in PROGRESS.md.

---

## 13. Troubleshooting starter kit (seed for `docs/RUNBOOK.md`)

| Symptom | Likely cause | Fix |
|---|---|---|
| OCI: "Out of host capacity" when creating or resizing the A1 VM | Arm capacity exhausted in that AD/fault domain | Try another fault domain or AD, or retry off-peak |
| `WARN util.NativeCodeLoader: Unable to load native-hadoop library` | Native libs not matched to the platform | Harmless for this project; ignore |
| DataNode exits with `Incompatible clusterIDs` | NN reformatted without wiping DataNode dirs | Deep reset wipes both; never format NN alone |
| WebHDFS writes fail with connection refused to `127.0.0.1:9864` | `bankdemo` mapped to loopback in `/etc/hosts` | Map `bankdemo` to the private IP (§5.2) |
| Baseline Spark app stays ACCEPTED | Container sizes exceed NM memory or AM percent | Recheck §2.3 math; check the RM scheduler page through the tunnel |
| PySpark `ModuleNotFoundError` in executors | `--py-files` zip missing, or venv not readable by `yarn` | Rebuild pyfiles.zip; `chmod -R o+rX /opt/bankdemo/venv` |
| Kafka `UnsupportedClassVersionError` | A Kafka 4.x tarball got installed despite the 3.9 pin | Check `config/versions.env`; 3.9 runs on Java 11 and is pinned precisely to avoid a second JDK (§4.1) |
| Kafka: `No readable meta.properties files found` | Storage not formatted | Run the KRaft format step (§6.4) |
| Hadoop log files missing, output only in journal | Foreground launcher forced the console logger | Use the `-Dhadoop.root.logger` opts approach (§6.2) |
| Service fails with permission denied on `/data/...` despite correct ownership | SELinux context | `ausearch -m avc -ts recent`, then `semanage fcontext -a -t <type> '/data/…(/.*)?'` and `restorecon` |
| `tc: Specified qdisc kind is unknown` | `sch_netem` not available for the running kernel | Install matching modules-extra, or disable F17 |
| Loop mounts missing after reboot | fstab ordering | Ensure `x-systemd.requires-mounts-for=/data` on loop entries |
| Instance stopped or disabled by Oracle | Idle reclamation or limit enforcement | Start it from the console; confirm shape ≤ 2 OCPU/12 GB; keep cron runs |
| Run exceeds 15:00 | Batch chain too slow on 2 OCPU | Check phase timings in `run_meta.json`; lower `eod_records`; merge Spark jobs |
| SSH hangs during F16 | Memory pressure starved sshd | Confirm the sshd OOM drop-in and swap; lower `--vm-bytes` |
| Hostname or `/etc/hosts` reverts after reboot | cloud-init's hostname / `update_etc_hosts` modules | `/etc/cloud/cloud.cfg.d/99-bankdemo.cfg` with `preserve_hostname: true`, `manage_etc_hosts: false` (§5.2) |
| NameNode killed during F16 instead of a container | Missing `OOMScoreAdjust` on NN/RM/PostgreSQL, or `--vm-bytes` too high | §2.3 drop-ins; re-run the Phase 3.5 gate (§6.8) |
| Installer aborts on a stage that changed nothing | `render_template` returning 0 for "changed" under `set -e` | Use the 0/10/1 convention in §4.1 |
| Clean run reports recon BREAK every time | Trailer counting DECLINED rows that `settlement_summary` excludes | §7.2 — the trailer is APPROVED-only |
| Every run fires a Kafka lag WARN | Consumer commits every 30 s, so steady-state lag exceeds the threshold | §7.2 — commit every 3 s, independent of the HDFS flush |
| `bankdemo health` reports BUSY | Wrapper not dispatching on subcommand; only run/test-fault/reset take the lock | §8.1 |
| Bundle contains `state/` or the feed profile | Bundle tarred from `RUN_DIR` instead of the staging dir | §9.2 — whitelist-copy into `<run_id>.bundle/` |

---

## 14. Definition of done

- [ ] `make deploy` on a fresh OL9 A1 VM builds everything unattended (except [HUMAN] steps).
- [ ] Phase 3.5 budget gate (§6.8) passed, with measured RSS recorded in PROGRESS.md.
- [ ] `bankdemo run --no-faults` ≤ 12:00; random runs ≤ 13:00 of phase budget and ≤ 15:00 wall clock in 20 of 20 trials.
- [ ] Every enabled fault passes `bankdemo test-fault` 3 times in a row.
- [ ] Same seed ⇒ same faults, schedule, params, and EOD **detail-block** hash (the header carries `run_id`, so whole-file hashes cannot match — §7 acceptance).
- [ ] Given only a bundle and this repository, the fault set cannot be recomputed (salted selection, §10.2).
- [ ] After every run: invariants clean or DIRTY set with a reason; deep reset recovers.
- [ ] Bundle complete, redacted, < 60 MB; conforms to `contract/bundle_v1.md`; answer key never leaves `/var/lib/bankdemo/keys`; `state/` absent from every archive.
- [ ] GitHub Actions dispatch, cron, and SSH triggers all work; concurrent runs are rejected cleanly.
- [ ] `docs/` complete; `make lint` clean.

---

## Appendix A — Prompts to paste into Claude Code

Use one prompt per session. Each assumes Claude Code has read `CLAUDE.md`.

**Phase 0**
> Read infra/bankdemo/CLAUDE.md and IMPLEMENTATION_GUIDE.md §0–§4.1, plus /ROADMAP.md for how
> this subtree relates to the rest of the repo. Plan first: list every file you'll create for
> the scaffold, the Makefile targets, and common.sh functions with signatures (note
> render_template's 0/10/1 convention). After I approve, implement Phase 0, run `make lint`,
> fill in docs/PROGRESS.md, and commit. Resolve exact versions for config/versions.env by
> checking the Apache download sites — Kafka is pinned to the 3.9 line, not 4.x, for the
> reasons in §4.1 — and show me the versions and checksum URLs before writing them.

**Phase 2**
> Implement installer stages 00–40 per §5. Every stage must be idempotent and begin with
> require_root and require_bankdemo_host (except 00, which creates the marker). Deploy with
> `make deploy`, run `install.sh --from 00 --to 40` twice, and run every Phase 2 acceptance
> check over ssh. Record results in PROGRESS.md. Stop and tell me if netem is unavailable.

**Phase 3**
> Implement stages 50–80 and the templates in config/templates per §6, plus a first
> `bankdemo health`. Keep all memory numbers from §2.3 — they budget RSS, not heap. Java 11
> only; there is no second JDK. Verify the Hadoop logging approach actually produces files and
> record which approach worked. Run all Phase 3 acceptance checks on the VM and record outputs,
> including start time, `free -m` with the stack idle, and summed daemon RSS.

**Phase 3.5 — budget truth gate**
> Run §6.8 exactly as written and paste the measurements into docs/PROGRESS.md. Do not start
> Phase 4 until the gate passes. If the measured numbers disagree with §2.3, stop and propose
> the revised budget rather than proceeding — five phases of code depend on these figures, and
> the faults that would disprove them aren't built until Phase 7.

**Phase 4**
> Implement §7: generators, fraud_feed consumer, batch jobs, scheduler with jobs.yaml,
> monitor, and a minimal `bankdemo run --no-faults` path. Write the pytest tests listed in
> the Phase 4 acceptance. Run three no-fault runs on the VM, report per-phase timings, and
> tune data volume if the budget is exceeded. Record decisions in RUNBOOK.

**Phase 5**
> Implement §8: the bin/bankdemo wrapper with flock and timeout, the orchestrator state
> machine with Deadline, health gate, log archiving, and normal plus deep reset with offline
> invariants. Add the hidden `--debug-hang-at` flag. Run every Phase 5 acceptance scenario
> and record results.

**Phase 6**
> Implement §9: collector snapshots, redaction, bundle layout, ticket.json, answer key,
> reveal, grade, and stats, with unit tests using fixtures. Run a no-fault run and verify
> every Phase 6 acceptance check, including that no key data is in the tarball.

**Phase 7**
> Implement the fault framework per §10.1–§10.2 with tests (including the salted-HMAC selection
> and the bounded scheduling retry), then implement faults in this order:
> **F01, F05, F16**, F12, F13, F10, F11, F09, F06, F07, F04, F02, F03, F15, F14, F17, F08.
> F05 and F16 come third and fourth on purpose: they are the two faults that can disprove the
> memory budget, and finding that out after thirteen other faults are built against it means
> rewriting the workload rather than tuning a fault.
> For each fault: write fault.yaml (including `enabled`), inject, revert, verify. Every revert
> must detect its condition first and no-op in under 200 ms — reset runs all seventeen on every
> run. Run `bankdemo test-fault` until it passes 3 consecutive times; tune as needed; record the
> passing output. If a fault can't be made reliable within a reasonable effort, set
> enabled: false and explain it in RUNBOOK. Then run 10 random-seed runs and confirm all finish
> within budget with clean invariants.

**Phase 8**
> Implement §11: sudoers (validate with visudo before install; do not remove the bootstrap
> sudoers until I confirm opc access), cron, logrotate, and the GitHub Actions workflow. Tell
> me exactly which secrets to create and the ssh-keyscan command. Then run the Phase 8
> acceptance checks.

**Phase 9**
> Implement §12 hardening, generate docs/FAULTS.md without answers, write README,
> ARCHITECTURE, and PRACTICE docs, run the reboot test (asserting hostname and /etc/hosts
> survive, per §5.2) and the rebuild drill, and complete the §14 checklist in PROGRESS.md.

---

## Appendix C — What changed from the first draft

This guide was revised after a full review. The substantive changes, so that notes taken
against the original are not silently invalidated:

| Area | Was | Now | Why |
|---|---|---|---|
| Kafka / Java | 4.x + dual JDK (11 and 17) | **3.9.x, Java 11 only** | Removes the dual-JDK apparatus and the `kafka-python`-vs-4.x client break in one move (§4.1) |
| Memory budget | Heap-only table totalling 11 GB, YARN 4096 | **RSS-based, YARN 3072**, +300 MB/JVM accounted, `OOMScoreAdjust` on NN/RM/PG | Heap ≠ RSS; six JVMs hid ~1.8 GB (§2.3) |
| Budget verification | none until Phase 7 | **Phase 3.5 gate** (§6.8) | The faults that disprove the budget were built last |
| Fault selection | `random.Random(seed)` | **`HMAC(salt, seed)`** | The seed is in `run_id`, so the bundle carried its own answers (§10.2) |
| Bundle | tarred from `RUN_DIR` | **whitelist-copied into `<run_id>.bundle/`** | `RUN_DIR/state/` holds the feed profile and fault action logs (§9.2) |
| Wrapper | no subcommand dispatch; `--kill-after=30`; `exit $rc` | **dispatch, 180 s grace, 124→3 mapping** | `health` became `run-inner health`; exit 3 was unreachable; SIGKILL landed mid-revert (§8.1) |
| Phase budget | DRAIN floored at 8:00, CLEANUP 1:30, END 14:30 | **floor removed, CLEANUP 2:00, END 13:00** | 12:00 baseline was arithmetically unreachable (§8.2) |
| Trailer | "intended records" | **APPROVED-only, explicitly** | 3% DECLINED made every clean run a recon BREAK (§7.2) |
| Consumer commits | every 30 s | **every 3 s** | Steady-state lag ~3600 tripped the thresholds and masked F06 (§7.2) |
| Reverts | idempotent | **idempotent _and_ state-detecting, <200 ms no-op** | All 17 run on every reset (§8.5, §10.1) |
| `render_template` | returns 0 if changed | **0 unchanged / 10 changed / 1 error** | Aborted the installer under `set -e` (§4.1) |
| F16 | `--vm-bytes 2200M`, `MemoryMax=5G` | **800M / 2G**, plus OOM protections | Would have killed the NameNode (§10.3) |
| F17 | 300 ms on `lo` | **100 ms** | `lo` carries all local traffic; 300 ms cascaded (§10.3) |
| F08 | "tune until reproducible" | **verify the broker _starts_**, else invalid | At `LimitNOFILE=512` it degenerates into F07 (§10.3) |
| Disk format | `mkfs.xfs /dev/sdb` | **`/dev/oracleoci/oraclevdb` + confirmation** | Kernel names aren't stable; the boot volume is adjacent (§4.2) |
| Hostname/hosts | set once | **cloud-init override** | Reverted on every reboot (§5.2) |
| CI bundle step | bare `grep` under `set -e` | **`|| true`** | Failed the step before the "VM busy" path (§11.3) |
| Cron | 3 runs/day | **6 runs/day**, with honest arithmetic | 3.1% doesn't clear a 95th-percentile threshold (§11.2) |
| Redaction | post-pass, 2 patterns | **on write, 8 patterns** | Command lines in `ps`/`yarn logs`/RM REST carry credentials (§9.1) |
| Grading | unspecified YAML load | **`safe_load` + `O_NOFOLLOW`** | Runs as root on a user-supplied file (§9.4) |
| Bundle consumers | human only | **human + `agent/`, contract in §9.5** | Makes the scorecard a real measurement |

---

## Appendix B — Practice loop (`docs/PRACTICE.md`)

1. Fetch the newest bundle (`make fetch RUN=latest`, or download the GitHub artifact). Don't look at the VM.
2. Start a 25-minute timer. Read `ticket.json` first, then work the way an L2 analyst would: timeline and alerts → scheduler/job status → the failing job's logs → service logs → snapshots.
3. Fill in `RCA_TEMPLATE.yaml`: symptom, first evidence, root cause, resolution, preventive action, business impact.
4. `bankdemo grade <run_id> rca.yaml`, then `bankdemo reveal <run_id>`.
5. For anything you missed, add a line to your personal "incident notebook": the log signature you should have searched for.
6. Every week, turn one solved incident into a STAR interview story (Situation: ticket; Task: restore settlement; Action: triage path and fix; Result: SLA restored, plus the preventive action you'd raise as a problem ticket).
