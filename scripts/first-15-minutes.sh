#!/usr/bin/env bash
#
# first-15-minutes.sh — collect what an on-call engineer gathers before
# escalating, standalone: no Python, no agent, no dependency on this
# repo's services being up. Deliberately separate from agent/evidence.py
# (which the triage agent runs automatically at incident open) — this is
# for the human running it directly on a machine where the pipeline is
# broken, possibly including this very script's own working directory.
#
# Usage: scripts/first-15-minutes.sh <incident-id-or-label> [output-dir]
#
# Degrades gracefully: a missing source produces a note in manifest.json,
# never a crash. Every step is best-effort and independent of the others.

set -euo pipefail

INCIDENT_LABEL="${1:?usage: $0 <incident-id-or-label> [output-dir]}"
OUTPUT_ROOT="${2:-reports/evidence}"
BUNDLE_DIR="${OUTPUT_ROOT}/${INCIDENT_LABEL}"
MANIFEST="${BUNDLE_DIR}/manifest.json"

mkdir -p "${BUNDLE_DIR}"

# manifest.json is built up as a JSON array of {name,status,note?,size_bytes}
# objects, one line of a temp file per entry, joined at the end — avoids
# depending on jq being installed (this script has no Python dependency,
# and jq is not guaranteed present on a broken machine either).
MANIFEST_ENTRIES_FILE="$(mktemp)"
trap 'rm -f "${MANIFEST_ENTRIES_FILE}"' EXIT

now_iso() {
  date -u +"%Y-%m-%dT%H:%M:%S.000Z"
}

json_escape() {
  # Minimal JSON string escaping for values we embed by hand below.
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  s="${s//$'\n'/\\n}"
  printf '%s' "$s"
}

record_ok() {
  local name="$1" path="$2"
  local size
  size="$(wc -c < "${path}" 2>/dev/null || echo 0)"
  printf '{"name":"%s","status":"ok","path":"%s","size_bytes":%s,"collected_at":"%s"}\n' \
    "$(json_escape "$name")" "$(json_escape "$path")" "${size}" "$(now_iso)" >> "${MANIFEST_ENTRIES_FILE}"
}

record_missing() {
  local name="$1" note="$2"
  printf '{"name":"%s","status":"missing","note":"%s","collected_at":"%s"}\n' \
    "$(json_escape "$name")" "$(json_escape "$note")" "$(now_iso)" >> "${MANIFEST_ENTRIES_FILE}"
}

echo "== first-15-minutes.sh: collecting evidence for '${INCIDENT_LABEL}' into ${BUNDLE_DIR} =="

# 1. Application/job log — this repo's mock pipeline log convention.
JOB_LOG_CANDIDATE="/logs/spark_run_${INCIDENT_LABEL}.log"
if [ -f "${JOB_LOG_CANDIDATE}" ]; then
  cp "${JOB_LOG_CANDIDATE}" "${BUNDLE_DIR}/job_log.log" 2>/dev/null \
    && record_ok "job_log.log" "${BUNDLE_DIR}/job_log.log" \
    || record_missing "job_log.log" "found ${JOB_LOG_CANDIDATE} but failed to copy it"
else
  record_missing "job_log.log" "no log file at ${JOB_LOG_CANDIDATE}"
fi

# 2. Last 200 lines of driver stdout, if a container/process log is reachable.
if command -v docker >/dev/null 2>&1 && docker compose ps agent_server >/dev/null 2>&1; then
  if docker compose logs --tail 200 agent_server > "${BUNDLE_DIR}/driver_stdout_tail.txt" 2>/dev/null; then
    record_ok "driver_stdout_tail.txt" "${BUNDLE_DIR}/driver_stdout_tail.txt"
  else
    record_missing "driver_stdout_tail.txt" "docker compose logs failed for agent_server"
  fi
else
  record_missing "driver_stdout_tail.txt" "docker compose not available or agent_server not running"
fi

# 3. Row counts both sides — via the tool server if it's reachable.
TOOL_SERVER_URL="http://${TOOL_SERVER_HOST:-localhost}:${TOOL_SERVER_PORT:-8000}"
if command -v curl >/dev/null 2>&1 && curl -sf "${TOOL_SERVER_URL}/health" >/dev/null 2>&1; then
  if curl -sf -X POST "${TOOL_SERVER_URL}/tools/sql_validator" \
      -H 'Content-Type: application/json' \
      -d '{"source_table":"src.transactions","target_table":"tgt.transactions"}' \
      -o "${BUNDLE_DIR}/row_counts.json" 2>/dev/null; then
    record_ok "row_counts.json" "${BUNDLE_DIR}/row_counts.json"
  else
    record_missing "row_counts.json" "tool server reachable but sql_validator call failed"
  fi
else
  record_missing "row_counts.json" "tool server unreachable at ${TOOL_SERVER_URL}"
fi

# 4. Target DDL / schema — via the tool server's schema_comparator.
if command -v curl >/dev/null 2>&1 && curl -sf "${TOOL_SERVER_URL}/health" >/dev/null 2>&1; then
  if curl -sf -X POST "${TOOL_SERVER_URL}/tools/schema_comparator" \
      -H 'Content-Type: application/json' \
      -d '{"source_table":"src.transactions","target_table":"tgt.transactions"}' \
      -o "${BUNDLE_DIR}/target_schema.json" 2>/dev/null; then
    record_ok "target_schema.json" "${BUNDLE_DIR}/target_schema.json"
  else
    record_missing "target_schema.json" "tool server reachable but schema_comparator call failed"
  fi
else
  record_missing "target_schema.json" "tool server unreachable at ${TOOL_SERVER_URL}"
fi

# 5. Kafka consumer group offsets and lag, if a broker is reachable in this compose project.
if command -v docker >/dev/null 2>&1 && docker compose ps kafka >/dev/null 2>&1; then
  if docker compose exec -T kafka kafka-consumer-groups \
      --bootstrap-server localhost:9092 --all-groups --describe \
      > "${BUNDLE_DIR}/kafka_consumer_groups.txt" 2>/dev/null; then
    record_ok "kafka_consumer_groups.txt" "${BUNDLE_DIR}/kafka_consumer_groups.txt"
  else
    record_missing "kafka_consumer_groups.txt" "kafka reachable but consumer-groups query failed"
  fi
else
  record_missing "kafka_consumer_groups.txt" "kafka broker not running in this compose project"
fi

# 6. Disk and inode snapshot — always available on any POSIX machine.
{
  echo "--- df -h ---"
  df -h 2>/dev/null || echo "df -h unavailable"
  echo "--- df -i ---"
  df -i 2>/dev/null || echo "df -i unavailable"
} > "${BUNDLE_DIR}/disk_and_inode_snapshot.txt"
record_ok "disk_and_inode_snapshot.txt" "${BUNDLE_DIR}/disk_and_inode_snapshot.txt"

# Assemble manifest.json from the recorded entries — one compact object
# per line in MANIFEST_ENTRIES_FILE, joined with ",\n" between entries
# only (not within one), so each artifact stays on its own line.
{
  printf '{\n  "incident_id": "%s",\n  "collected_at": "%s",\n  "artifacts": [\n' \
    "$(json_escape "${INCIDENT_LABEL}")" "$(now_iso)"
  awk '{ printf "%s    %s", (NR > 1 ? ",\n" : ""), $0 } END { print "" }' "${MANIFEST_ENTRIES_FILE}"
  printf '  ]\n}\n'
} > "${MANIFEST}"

echo "== Done. Manifest: ${MANIFEST} =="
