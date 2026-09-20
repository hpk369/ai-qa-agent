# RB-004 — Kafka Consumer Lag
**Severity guidance:** typically P2 (`sla_breach_projected`)
**Owner:** Application Support
**Last reviewed:** 2026-09-13

## Symptom
The Log Analyser (`log_analyser`) reports `kafka_lag` above
`KAFKA_LAG_THRESHOLD` (default 10,000 messages), surfaced as the
`sla_breach_projected: true` signal. The job log typically shows
`ERROR: Kafka consumer lag exceeded 10000 messages` and/or a run of `WARN:
Consumer group lag growing` lines.

## Impact
Downstream consumers relying on near-real-time data are falling further
behind and will show stale results. State the actual lag and its trend in
`impact_summary` — e.g. "downstream consumers are 15,000 messages behind
Kafka and falling further back; near-real-time dashboards will show stale
data" — not the raw log line.

## Diagnostic steps

1. Confirm the current lag against the cluster, not the log line that
   alerted:
   ```bash
   kafka-consumer-groups --bootstrap-server <broker>:9092 \
     --describe --group <consumer-group-name>
   ```
   The `LAG` column per partition tells you whether the lag is concentrated
   on one partition (a skew/hot-key problem) or spread evenly (a
   throughput/capacity problem).

2. Read the lag lines the alert fired on, and what preceded them:
   ```bash
   grep -n -B5 'lag exceeded threshold' reports/logsets/<session_id>/*.log
   ```

3. Check whether lag is growing, flat, or shrinking — a single point-in-time
   reading over threshold that's already trending down is a different
   situation from lag that's accelerating. Re-run step 1 a minute or two
   apart and compare.

4. Rule out a downstream consumer crash loop (lag grows because nothing is
   consuming, not because production outpaced consumption) — check the
   consumer process/pod is actually running and not restart-looping.

## Remediation options

| Option | Risk | Requires approval | Notes |
|---|---|---|---|
| Scale out the consumer group (add consumer instances, up to the partition count) | Low | Yes (P1/P2) | Standard fix for a genuine throughput shortfall. |
| Restart a crashed/stuck consumer | Low | Yes (P1/P2) | Correct fix when the consumer isn't running at all rather than merely slow. |
| Investigate and fix back-pressure in the downstream streaming job | Medium | Yes | Needed when the consumer is up but processing too slowly per message — scaling out alone won't fix a per-message bottleneck. |

## Escalation
Escalate immediately if lag is accelerating and projected to breach the
job's SLA window before the next scheduled run, or if it's concentrated on
a single partition (possible hot-key/skew issue needing a repartitioning
fix, not just more consumers).

## Prevention
Alert on lag trend (rate of growth), not just absolute threshold, so a
slow-building problem is caught well before the 10,000-message line.
Owner: the team operating the Kafka consumer group.
