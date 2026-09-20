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
present as fewer usable rows, and a stale partition looks identical to a
real shortfall from row counts alone.

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

2. Read the reconciliation line the alert fired on, in context — the lines
   around it usually name the stage that dropped the rows:
   ```bash
   grep -n -B5 -A5 'Row count reconciliation failed' reports/logsets/<session_id>/*.log
   ```

3. Re-run the count against the live tables to confirm the shortfall is still
   present rather than already self-corrected by a retry:
   ```sql
   SELECT (SELECT count(*) FROM src.transactions) AS source_rows,
          (SELECT count(*) FROM tgt.transactions) AS target_rows;
   ```
   Counts that now agree mean the load recovered — downgrade or resolve
   rather than remediate.

4. Re-read the whole log set the alert came from, including everything the
   catalogue did *not* recognise:
   ```bash
   python scripts/logset.py --show <session_id>
   ```

5. Reproduce the detection path itself, when the question is whether the
   check is right rather than what the pipeline did:
   ```bash
   python scripts/logset.py --seed <seed> --no-slack
   pytest tests/pytest/test_logsets.py -v
   ```

## Remediation options

| Option | Risk | Requires approval | Notes |
|---|---|---|---|
| Re-run the load unchanged | Low | No (P3/P4) / Yes (P1/P2 — see `requires_approval` on the incident) | Fixes a one-off transient failure (a killed executor, a flaky source read). Confirm idempotency first — a re-run that duplicates already-loaded rows makes reconciliation worse. |
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
