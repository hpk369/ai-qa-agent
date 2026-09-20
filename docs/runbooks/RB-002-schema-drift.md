# RB-002 — Schema Drift
**Severity guidance:** typically P1 (a required column is missing entirely) or P3 (a cosmetic/non-critical column change)
**Owner:** Application Support
**Last reviewed:** 2026-09-13

## Symptom
The Metastore Comparator (`schema_comparator`) reports a column added,
removed, renamed, or type-changed between source and target. In this
codebase's current failure taxonomy, a rename or removal of a column the
load needs to complete (e.g. `account_balance`) is reported as the signal
`job_failed_no_path_to_sla: true` (see `agent/prompts.py` and
`agent/runbooks.py::select_runbook`) — the load cannot write that column at
all, not just late. Watch for `ERROR: Column '<name>' not found in target
schema` in the job log.

## Impact
Any downstream job or report reading the affected column will fail
outright or silently treat it as unknown/null. State this in business
terms in `impact_summary` — e.g. "account_balance is entirely absent from
the target load; any downstream job reading balances will fail or report
zero" — not "schema_comparator returned columns_renamed".

## Diagnostic steps

1. Get the exact column diff from the live tables — the log names the
   column that broke the write, the catalogue tells you what else moved
   with it:
   ```sql
   SELECT column_name, data_type FROM information_schema.columns
    WHERE table_schema = 'tgt' AND table_name = 'transactions'
   EXCEPT
   SELECT column_name, data_type FROM information_schema.columns
    WHERE table_schema = 'src' AND table_name = 'transactions';
   ```
   A column that moved rather than vanished (a rename) needs a different
   fix from one that was dropped.

2. Re-read the log set the alert came from, in full:
   ```bash
   python scripts/logset.py --show <session_id>
   grep -n -B3 -A3 'not found in target schema' reports/logsets/<session_id>/*.log
   ```

3. Confirm this isn't a detector false positive:
   ```bash
   pytest tests/pytest/test_schema_comparator.py -v
   ```
   Pay particular attention to the rename-detection heuristic
   (`agent_tools/schema_comparator.py`): it matches a removed and an added
   column by identical data type, so two unrelated columns that happen to
   share a type can be misreported as a rename. If the reported "rename"
   doesn't make semantic sense, treat it as an unrelated add + remove
   instead and re-check the diagnosis.

4. Check whether the change was a deliberate migration applied to the wrong
   environment (the most common real-world cause) versus an unreviewed
   change to the transformation job itself — check recent commits/deploys
   to both the schema migration tooling and the transform job.

## Remediation options

| Option | Risk | Requires approval | Notes |
|---|---|---|---|
| Revert the target schema migration | Low–Medium | Yes | Correct fix if the migration was applied to the wrong environment/table. |
| Update the transformation to align with the new column name/type | Medium | Yes | Correct fix if the schema change was intentional and the transform simply wasn't updated. |
| Add a compatibility view/alias in the interim | Low | Yes | Buys time to fix downstream consumers without another schema change under pressure. |

## Escalation
P1 (a required column can't be written at all) pages immediately and opens
an incident bridge per `config/severity.yml` — engage the team that owns
both the source schema and the transformation job, since this is very
often a coordination failure between the two, not a bug in either alone.

## Prevention
Schema changes to a source or target table should go through a contract
check (this project's own Recon/Metastore Comparator checks, run in CI
before a migration merges) rather than being caught first in production.
Owner: whichever team owns the schema migration tooling.
