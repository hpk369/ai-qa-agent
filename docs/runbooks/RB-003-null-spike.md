# RB-003 — Null Spike
**Severity guidance:** P3 on a critical column (`config/severity.yml`'s `column_criticality.critical`), P4 otherwise
**Owner:** Application Support
**Last reviewed:** 2026-09-13

## Symptom
The Recon Checker (`sql_validator`) reports a column's null rate above
`NULL_RATE_THRESHOLD` (default 5%), and the Log Analyser typically shows a
correlated error such as `NullPointerException in CustomerTransformStep` or
`Null propagation detected in <column> join key`. The incident's signal is
`null_rate_increase_pct: {<column>: <points>}`; whether it's P3 or P4 depends
on whether the column is in `config/severity.yml`'s `column_criticality.critical`
list (`customer_id`, `account_balance`, `account_number` today).

## Impact
Records with a null critical key generally cannot be joined or attributed
correctly — e.g. "35% of loaded records have no customer_id and cannot be
matched to a customer." A null spike on a non-critical column (e.g.
`region_code`) is informational; say so plainly rather than dramatizing it.

## Diagnostic steps

1. Get the current null rate for the named column straight from the
   target, to confirm the incident's figure still holds:
   ```sql
   SELECT count(*) FILTER (WHERE customer_id IS NULL)::float / count(*) AS null_rate
     FROM tgt.transactions;
   ```

2. Read the null-rate line and its surroundings in the log set the alert
   came from:
   ```bash
   grep -n -B3 -A3 'Null rate for column' reports/logsets/<session_id>/*.log
   ```

3. Pull every error line from the same log set, not just the one that
   matched:
   ```bash
   grep -nE '\b(ERROR|FATAL)\b' reports/logsets/<session_id>/*.log
   ```
   A `NullPointerException` or a named join key in the error strongly
   suggests a broken null-safe join, not a genuinely null source value.

4. Distinguish "the source really has nulls" from "the transform broke a
   join": check the source's own null rate for the same column — if source
   is clean and only the target shows nulls, the transform is the culprit.

5. Confirm against the test suite that this isn't a detection false
   positive:
   ```bash
   pytest tests/pytest/test_sql_validator.py -k null -v
   ```

## Remediation options

| Option | Risk | Requires approval | Notes |
|---|---|---|---|
| Fix the null-safe join in the transform step | Medium | Depends on severity (P1/P2 always; P3/P4 per team norms) | Correct fix when the source is clean and the transform broke the join. |
| Add a `NOT NULL` constraint / reject bad rows at load time | Low–Medium | Yes | Prevents recurrence but may need a backfill/cleanup pass first. |
| Accept and document if the source genuinely has nulls for this column | Low | No | Valid outcome — not every null is a bug; update `impact_summary`/root_cause accordingly and close as expected behavior if so. |

## Escalation
Escalate to the team owning the transform job if the affected column is
`customer_id` or another join key used by multiple downstream jobs — a
broken join key tends to fan out further than a single table's shortfall.

## Prevention
Add a null-rate contract check on critical columns to the pre-deploy
validation suite (this repo's own Robot Framework acceptance checks are a
reasonable home for it) so a broken join is caught before it reaches
production. Owner: the team that owns the transform job.
