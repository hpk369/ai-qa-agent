# RB-001 — Row Shortfall
**Severity guidance:** typically P2 (P3 if under 5% and otherwise complete)
**Owner:** Application Support
**Last reviewed:** 2026-09-13

## Symptom
The Recon Checker (`sql_validator`) reports `row_drop_pct` over `ROW_DROP_THRESHOLD`
(default 5%, `config/severity.yml` classifies it as `row_variance_pct`). In Slack,
the incident's parent message shows `*Rows loaded / expected:*` with the loaded
figure well under the expected one, and `severity_rationale` names
`row_variance_pct >= 5.0`. **Before treating this as a shortfall, rule out
RB-002**: a schema change that silently breaks a required column can also
present as fewer usable rows, and `stale_partitions` (Phase 3, once the
Hadoop stack lands) looks identical to a real shortfall from row counts alone.

## Impact
Downstream jobs and dashboards reading the target table are working from an
incomplete dataset. State the actual business impact in the incident's
`impact_summary` field — e.g. "customer_dim is missing ~40% of records;
three downstream reporting jobs cannot start" — not the Spark stage name.

## Diagnostic steps

1. Read the incident record for the exact counts and the tool that detected it:
   ```bash
   jq '.rows_expected, .rows_loaded, .detected_by, .evidence' reports/incidents/<incident_id>.json
   ```
   Confirms the magnitude of the shortfall and which check first saw it.

2. Re-run the Recon Checker directly against the tool server to confirm the
   shortfall is still present (not already self-corrected by a retry):
   ```bash
   curl -s -X POST http://localhost:8000/tools/sql_validator \
     -H 'Content-Type: application/json' \
     -d '{"source_table":"src.transactions","target_table":"tgt.transactions"}' | jq
   ```
   A `row_drop_pct` near zero here means the load has already recovered —
   downgrade or resolve rather than remediate.

3. Reproduce locally against the mock pipeline (useful when investigating the
   detection logic itself, not a live incident):
   ```bash
   INJECT_FAILURE=row_drop python mock_pipeline/producer.py
   ```

4. Run the targeted unit tests to confirm the Recon Checker's own logic isn't
   the thing that's wrong (a false positive):
   ```bash
   pytest tests/pytest/test_sql_validator.py -v
   ```

5. Check the evidence bundle collected at incident open (T1.7) for the job
   log and target DDL:
   ```bash
   cat reports/evidence/<incident_id>/manifest.json
   ```

## Remediation options

| Option | Risk | Requires approval | Notes |
|---|---|---|---|
| Re-run the load unchanged | Low | No (P3/P4) / Yes (P1/P2 — see `requires_approval` on the incident) | Fixes a one-off transient failure (a killed executor, a flaky source read). Confirm idempotency first — see RB's "duplicate_on_rerun" note in expansion-plan.md §5 B3 once that check exists. |
| Re-run with a corrected transform (fix a real dedup/partition bug) | Medium | Yes | Only once the root cause is identified — don't guess-and-retry against a real logic bug. |
| Manual backfill of the missing rows | High | Yes | Last resort; requires reconciling against the source to avoid duplicating rows already loaded correctly. |

## Escalation
Page data engineering on-call immediately if the shortfall exceeds ~25% or
affects a table feeding a regulatory or financial report (this should
already be P1/P2 per `config/severity.yml`, but use judgment — a table
tagged critical downstream is still worth a page even at a lower percentage).
Otherwise, ticket for next business day.

## Prevention
Add a natural-key uniqueness/idempotency check to the load so a re-run
cannot silently drop or duplicate rows, and alert on partition skew before
it causes a shortfall rather than after. Owner: Data Engineering.
