# Hadoop stack (Phase 2) — status and how to verify it

This tracks `expansion-plan.md` Track B / `IMPLEMENTATION.md` Phase 2. Read
the honesty note below before assuming any of this runs — it is written
and config-validated, not run end-to-end anywhere yet.

## Honesty note — what's actually been verified

Everything in this document was written in a sandboxed session whose
network policy **blocks Docker image pulls** (`docker pull hello-world` and
`docker pull alpine:3.19` both returned `403 Forbidden` from the sandbox's
own egress proxy). That means:

- **Verified for real:** RAM (15Gi available, clears `IMPLEMENTATION.md`'s
  ~12GB gate), `docker-compose.yml` syntax and profile resolution
  (`docker compose --profile hadoop config` — parses cleanly, resolves to
  the expected service list, `--profile lite` is provably unaffected), and
  the Postgres JDBC driver URL `hive-jdbc-driver-init` downloads (fetched
  it directly in this sandbox and confirmed it's a real, valid JAR —
  Maven Central isn't behind the same block Docker Hub is here).
- **Written, not run:** every Hadoop/YARN/Hive service definition below.
  The `core-site.xml`/`hdfs-site.xml`/`yarn-site.xml`/`mapred-site.xml`
  values are standard, well-documented single-node settings, and the
  compose commands (`hdfs namenode`, `yarn resourcemanager`, ...) are the
  official Hadoop CLI entry points — but none of it has actually started a
  container, formatted a NameNode, or run a job. The acceptance checks
  below are written as what **you** should run once you're on a machine
  that can pull `apache/hadoop:3`, not as things this session confirmed.

If something below doesn't work as written, that's expected until someone
runs it for the first time — report back what actually happened and it
gets fixed against reality rather than re-guessed.

## T2.2 — HDFS + YARN

Services: `namenode`, `datanode`, `resourcemanager`, `nodemanager`, all
`apache/hadoop:3`, config mounted read-only from `hadoop/config/`.

```bash
docker compose --profile hadoop up namenode datanode resourcemanager nodemanager
```

**Acceptance checks** (`IMPLEMENTATION.md` T2.2 — run these yourself):

```bash
# HDFS is up and the filesystem is reachable
docker compose exec namenode hdfs dfs -ls /

# ResourceManager UI reachable
curl -f http://localhost:8088

# NameNode UI reachable
curl -f http://localhost:9870

# A sample job appears in the applications list
docker compose exec resourcemanager yarn jar \
  /opt/hadoop/share/hadoop/mapreduce/hadoop-mapreduce-examples-*.jar \
  pi 2 10
# then check http://localhost:8088/cluster/apps for the completed job
```

### Known pitfalls (from `IMPLEMENTATION.md`, plus one found writing this)

- **Container memory defaults are too low for any real Spark job** and
  will produce OOM kills you didn't intend to inject. `yarn-site.xml`
  here sets `yarn.nodemanager.resource.memory-mb=4096` as a starting
  point — tune it to whatever the host actually has free, and raise
  `yarn.scheduler.maximum-allocation-mb` to match before running T2.4's
  Spark job.
- **Hostname resolution between containers** is the usual source of
  NameNode connection failures. `hdfs-site.xml` disables
  `dfs.namenode.datanode.registration.ip-hostname-check` for exactly this
  reason on a single-node Docker Compose cluster; if DataNode
  registration still fails, check that every container's `hostname:` in
  `docker-compose.yml` matches what the other configs reference it as
  (`namenode`, `resourcemanager`, ...) — Compose's default network gives
  each service a resolvable DNS name matching its service name, but a
  mismatch between that and `hostname:` is a common source of confusion.
- **The `apache/hadoop:3` tag is unpinned** to an exact patch version
  deliberately for now — pin it (`apache/hadoop:3.3.6` or similar) once
  you've confirmed which patch version actually pulls and works, rather
  than trusting this session's guess.
- **NameNode formatting** only happens once (guarded by checking for
  `nameNode/current/`) — if you need to reformat during testing, `docker
  compose down -v` to drop the `namenode_data`/`datanode_data` volumes
  first, or the NameNode and DataNode will disagree about cluster ID.

## T2.3 — Hive

Services: `hive-metastore`, `hiveserver2` (both `apache/hive:4.0.0`), plus
a one-shot `hive-jdbc-driver-init` container that downloads the Postgres
JDBC driver (Hive images don't bundle one) into a shared volume mounted
at `/jars` in both Hive containers via `HIVE_AUX_JARS_PATH` — a
long-standing, well-documented Hive mechanism, not something specific to
this image, so it's the part of this setup worth trusting most.

```bash
docker compose --profile hadoop up hive-metastore hiveserver2
```

**Schema initialisation** (`IMPLEMENTATION.md`'s named pitfall — "the Hive
metastore schema must be initialised with `schematool` before first
use"): the `apache/hive` image's own entrypoint is documented to run this
automatically on first metastore start when `SERVICE_NAME=metastore` (set
here) detects an uninitialised schema. This is written to rely on that,
**not verified in this sandbox**. If `hive-metastore`'s logs show a
schema-not-found error instead, run it manually:

```bash
docker compose exec hive-metastore \
  /opt/hive/bin/schematool -dbType postgres -initSchema
```

**Acceptance check** (`IMPLEMENTATION.md` T2.3 — an external table over
HDFS Parquet is queryable):

```bash
docker compose exec hiveserver2 beeline -u jdbc:hive2://localhost:10000 \
  -e "CREATE EXTERNAL TABLE test_ext (id INT, name STRING)
      STORED AS PARQUET LOCATION 'hdfs://namenode:9000/test_ext';
      SELECT * FROM test_ext;"
```

### Additional pitfall found writing this (not in `IMPLEMENTATION.md`)

**Hive images don't bundle a Postgres JDBC driver** — only Derby (and
sometimes MySQL) ship by default, so pointing `ConnectionURL` at Postgres
without also supplying the driver jar fails with a
`ClassNotFoundException: org.postgresql.Driver`, not an obviously
metastore-related error. `hive-jdbc-driver-init` exists specifically to
avoid that. It needs a live internet connection to Maven Central
(`repo1.maven.org`) at first startup — if that's not available where this
runs, pre-populate the `hive_jdbc_driver` volume with
`postgresql-42.7.3.jar` some other way before starting `hive-metastore`.

## T2.4 — Spark transform (not yet written)

Planned: `spark_jobs/cx_customer_load.py`. Unlike T2.2/T2.3, this one
*can* be genuinely tested in this sandbox — PySpark runs fine in local
mode without HDFS/YARN (Java 21 + a `setuptools<60` venv were enough to
get `pyspark` installed and running a local Spark session here), so the
transformation logic itself (dedup, SCD handling, normalisation, PII
masking) will be tested for real before being submitted with
`--master yarn`, even though the YARN submission path itself can't be.

## T2.5 — Port validation (not yet written)

Planned: extend `agent_tools/sql_validator.py` and
`agent_tools/schema_comparator.py` to support a Hive/Spark SQL connection
mode alongside their existing mock/SQLite/Postgres paths.

## Resource usage — to fill in once run for real

`IMPLEMENTATION.md`'s Phase 2 stop gate asks for resource usage under
load and confirmation the lite profile still runs. The second half is
already true (`docker compose --profile lite config` resolves to the
original six services, unchanged). The first half needs a real run on a
machine that can pull these images — there's nothing to honestly report
here yet.
