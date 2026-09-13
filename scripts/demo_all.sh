#!/usr/bin/env bash
#
# demo_all.sh — one-command walkthrough of every failure mode for a live
# demo: opens each incident, walks the non-clean ones through their full
# lifecycle (acknowledged -> approved -> remediating -> verifying ->
# resolved), then prints the resulting metrics.
#
# Respects SLACK_MODE like everything else in this repo: run as-is for a
# no-setup local preview (SLACK_MODE=stub, the default — payloads land in
# reports/slack/, nothing goes out over the network), or `source .env`
# first (SLACK_MODE=live) to actually narrate this into a real Slack
# workspace during a demo.
#
# Usage: scripts/demo_all.sh

set -euo pipefail
cd "$(dirname "$0")/.."

echo "=================================================================="
echo " ETL Production Support Triage Agent — full demo walkthrough"
echo " SLACK_MODE=${SLACK_MODE:-stub}"
echo "=================================================================="
echo

echo "--- Clean run ---"
python3 scripts/demo_incident.py none
echo

for mode in row_drop schema_drift null_spike latency; do
  echo "--- ${mode} (full lifecycle) ---"
  python3 scripts/demo_incident.py "${mode}" --lifecycle --actor U_DEMO
  echo
done

echo "=================================================================="
echo " Metrics across this demo run"
echo "=================================================================="
python3 scripts/incident_metrics.py

echo
echo "Done. Persisted incidents: reports/incidents/  |  evidence bundles: reports/evidence/"
if [ "${SLACK_MODE:-stub}" = "stub" ]; then
  echo "Stub Slack payloads (what would have been sent): reports/slack/"
fi
