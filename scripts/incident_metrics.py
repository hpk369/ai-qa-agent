#!/usr/bin/env python3
"""
incident_metrics.py — reads reports/incidents/*.json and prints count by
severity, median/p90 MTTA, median/p90 MTTR, and the false-positive rate.

These are the numbers that become a résumé line, so compute them
honestly — including the incidents where the agent was wrong (status
false_positive counts fully into the false-positive rate; there is no
mode that excludes them from the denominator).

Usage: scripts/incident_metrics.py [reports/incidents] [--json]
"""

from __future__ import annotations

import json
import os
import statistics
import sys
from pathlib import Path
from typing import Any

DEFAULT_INCIDENTS_DIR = Path(os.path.join(os.path.dirname(__file__), "..", "reports", "incidents"))
SEVERITIES = ["P1", "P2", "P3", "P4"]


def _median_and_p90(values: list[int]) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    if len(values) == 1:
        return float(values[0]), float(values[0])
    ordered = sorted(values)
    median = statistics.median(ordered)
    p90 = statistics.quantiles(ordered, n=100, method="inclusive")[89]
    return median, p90


def load_incidents(incidents_dir: Path) -> list[dict[str, Any]]:
    incidents = []
    for path in sorted(incidents_dir.glob("*.json")):
        with open(path) as f:
            incidents.append(json.load(f))
    return incidents


def compute_metrics(incidents: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(incidents)
    by_severity = {sev: 0 for sev in SEVERITIES}
    for incident in incidents:
        sev = incident.get("severity")
        if sev in by_severity:
            by_severity[sev] += 1

    mtta_values = [i["mtta_seconds"] for i in incidents if i.get("mtta_seconds") is not None]
    mttr_values = [i["mttr_seconds"] for i in incidents if i.get("mttr_seconds") is not None]
    mtta_median, mtta_p90 = _median_and_p90(mtta_values)
    mttr_median, mttr_p90 = _median_and_p90(mttr_values)

    false_positive_count = sum(1 for i in incidents if i.get("status") == "false_positive")
    false_positive_rate = (false_positive_count / total) if total else None

    return {
        "total": total,
        "by_severity": by_severity,
        "mtta": {"median_seconds": mtta_median, "p90_seconds": mtta_p90, "n": len(mtta_values)},
        "mttr": {"median_seconds": mttr_median, "p90_seconds": mttr_p90, "n": len(mttr_values)},
        "false_positive_count": false_positive_count,
        "false_positive_rate": false_positive_rate,
    }


def _fmt_seconds(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0f}s"


def print_report(metrics: dict[str, Any]) -> None:
    print(f"Incidents analysed: {metrics['total']}")
    if metrics["total"] == 0:
        print("(no incidents in reports/incidents/ — nothing to report)")
        return

    print("\nBy severity:")
    for sev in SEVERITIES:
        print(f"  {sev}: {metrics['by_severity'][sev]}")

    print(f"\nMTTA (n={metrics['mtta']['n']}):")
    print(f"  median: {_fmt_seconds(metrics['mtta']['median_seconds'])}")
    print(f"  p90:    {_fmt_seconds(metrics['mtta']['p90_seconds'])}")

    print(f"\nMTTR (n={metrics['mttr']['n']}):")
    print(f"  median: {_fmt_seconds(metrics['mttr']['median_seconds'])}")
    print(f"  p90:    {_fmt_seconds(metrics['mttr']['p90_seconds'])}")

    rate = metrics["false_positive_rate"]
    rate_str = "n/a" if rate is None else f"{rate * 100:.1f}%"
    print(f"\nFalse-positive rate: {rate_str} ({metrics['false_positive_count']}/{metrics['total']})")


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    as_json = "--json" in argv
    incidents_dir = Path(args[0]) if args else DEFAULT_INCIDENTS_DIR

    if not incidents_dir.exists():
        print(f"No such directory: {incidents_dir}", file=sys.stderr)
        return 1

    incidents = load_incidents(incidents_dir)
    metrics = compute_metrics(incidents)

    if as_json:
        print(json.dumps(metrics, indent=2))
    else:
        print_report(metrics)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
