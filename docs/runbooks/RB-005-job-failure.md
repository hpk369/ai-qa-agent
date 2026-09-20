# RB-005 — Job Failure / General Financial or Availability Impact
**Severity guidance:** P1 (target unavailable, any control-total mismatch, or no path to complete before SLA) or P2/P3 (downstream jobs blocked, run duration far over baseline)
**Owner:** Application Support
**Last reviewed:** 2026-09-13

## Symptom
This is the catch-all runbook for the P1/P2 conditions that aren't a clean
row-count, schema, or lag signature on their own: `target_unavailable`,
`control_total_mismatch` (any monetary/control-total variance at all —
`config/severity.yml` treats this as P1 regardless of magnitude), one or
more `downstream_jobs_blocked`, or a run whose duration is far over
baseline (`job_duration_vs_baseline_pct >= 200`). Use `agent/runbooks.py`'s
`select_runbook` to see exactly which signal routed here for a given
incident — check the incident record's `severity_rationale` field first.

## Impact
This runbook covers the widest range of underlying causes, so the impact
statement matters even more than usual — read the incident's
`impact_summary` rather than assuming from the runbook alone. A target
table being entirely unavailable is a different conversation from one
downstream job being delayed.

## Diagnostic steps

1. Read the full incident record, not just the severity — this runbook's
   scope is broad enough that the details decide what to do next:
   ```bash
   jq '.' reports/incidents/<incident_id>.json
   ```

2. If `target_unavailable`: confirm whether the target table/service is
   actually unreachable versus merely slow —
   ```bash
   curl -s -X POST http://localhost:8000/tools/sql_validator \
     -H 'Content-Type: application/json' \
     -d '{"source_table":"src.transactions","target_table":"tgt.transactions"}'
   ```
   A connection-refused/timeout here (vs. a normal JSON response with a
   row-count issue) confirms genuine unavailability rather than a data
   quality problem being misclassified.

3. If `control_total_mismatch`: re-run the Recon Checker's control-total
   comparison (sum of balances or another monetary measure between source
   and target) and get the exact variance, not just that one exists — a
   $0.01 rounding difference and a $40,000 shortfall are both P1 today
   (any variance is P1 per `config/severity.yml`), but they call for very
   different next steps, so establish which one you're looking at before
   deciding how urgently to act beyond the mandatory page.

4. If `downstream_jobs_blocked`: identify exactly which jobs, and whether
   they're blocked on this table specifically or on something upstream of
   it too — check the scheduler's dependency graph, or the coordinator's
   dataset dependencies if the jobs are Airflow/Oozie-scheduled.

5. If duration far exceeds baseline: check for the two most common causes
   in a Spark job — partition skew (one task running far longer than the
   rest) and executor memory pressure/GC thrashing — via the job's own
   logs/metrics; on a YARN cluster, `yarn logs -applicationId <id>` is the
   primary tool here.

## Remediation options

| Option | Risk | Requires approval | Notes |
|---|---|---|---|
| Restore/failover the target if unavailable | High | Yes | Coordinate with whoever owns the target infrastructure. |
| Re-run after confirming root cause | Medium | Yes | Never guess-and-retry a control-total mismatch — confirm the cause first; a re-run that duplicates already-loaded rows makes reconciliation worse. |
| Manually unblock downstream jobs with a documented workaround | High | Yes | Only as a stopgap; document exactly what was done for the postmortem. |

## Escalation
Every condition this runbook covers is P1 or P2 by design
(`config/severity.yml`) — page immediately for `target_unavailable`,
`control_total_mismatch`, or a projected SLA breach with no path to
complete; engage the app-dev team that owns the affected job, and open an
incident bridge for P1.

## Prevention
Varies by underlying cause — the postmortem for the specific incident
carries the permanent fix and its owner. This runbook's breadth is itself
a signal that the failure modes it covers are worth splitting into more
specific, purpose-built checks as the failure taxonomy grows.
