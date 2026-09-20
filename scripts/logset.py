#!/usr/bin/env python3
"""
Mix a log set for this session, triage it, and alert Slack.

    python scripts/logset.py                      # a fresh mix, alert Slack
    python scripts/logset.py --seed 42            # reproduce an exact set
    python scripts/logset.py --sources spark-executor,kafka-consumer
    python scripts/logset.py --injections 3 --count 5
    python scripts/logset.py --clean --no-slack   # a set with nothing injected
    python scripts/logset.py --list               # sessions built so far
    python scripts/logset.py --show LS-...        # re-read one, no alert

With SLACK_MODE unset (the default) the alert is written to reports/slack/
instead of being posted, so this runs end to end with no Slack workspace.
Every run prints the path of a zip containing exactly the logs that
produced the alert.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.env import load_env

load_env()

from agent.incident import load, record_approval_decision, resolve_incident, set_status
from logsets.catalog import SOURCES
from logsets.session import bundle, list_sessions, load_session
from logsets.triage import analyse_logset, logset_summary, score, triage_logset


def _print_summary(result: dict) -> None:
    summary = result["logset"]
    print(f"Log set   {summary['session_id']}  (seed {summary['seed']})")
    print(f"Scanned   {summary['lines_scanned']} lines across {len(summary['files'])} file(s) — "
          f"{summary['error_count']} error / {summary['warn_count']} warn")
    print()
    print(f"  {'FILE':28} {'BACKGROUND':11} {'LINES':>6} {'ERR':>5} {'WARN':>5}")
    for entry in summary["files"]:
        print(f"  {entry['file']:28} {entry['provider']:11} {entry['line_count']:6} "
              f"{entry['error_count']:5} {entry['warn_count']:5}")

    print()
    if summary["findings"]:
        print("Recognised signatures:")
        for finding in summary["findings"]:
            print(f"  {finding['signature_id']:28} {finding['file']}:{finding['line']}  "
                  f"{finding['title']}")
    else:
        print("Recognised signatures: none")
    if summary["unrecognised_error_count"]:
        print(f"  ({summary['unrecognised_error_count']} unrecognised error line(s))")

    print()
    incident = result.get("incident")
    if incident:
        print(f"Incident  {incident['incident_id']}  {incident['severity']}  "
              f"— {'approval required' if incident['requires_approval'] else 'no approval gate'}")
        print(f"Rationale {incident['severity_rationale']}")
        print(f"Runbook   {incident['runbook'] or 'none'}")
        print(f"Summary   {incident['impact_summary']}")
        print(f"          (written by {result.get('narrated_by', 'fallback')})")
        if incident.get("slack_ts"):
            print(f"Slack     posted to {incident['slack_channel']} (ts {incident['slack_ts']})")
        else:
            print("Slack     not posted")
    else:
        print("Incident  none — clean log set, nothing to alert on")

    detection = result.get("score") or {}
    if detection.get("injected"):
        print(f"Detection {len(detection['detected'])}/{len(detection['injected'])} injected "
              f"signature(s) found (recall {detection['recall']})"
              + (f", missed: {', '.join(detection['missed'])}" if detection["missed"] else ""))

    print()
    print(f"Download  {summary['download']['archive']}")
    if summary["download"]["url"]:
        print(f"          {summary['download']['url']}")


def run_lifecycle(incident_id: str, actor: str) -> None:
    """Walk an opened incident through the states a real one goes through:
    acknowledged -> approved (the gate P1/P2 requires) -> verifying ->
    resolved, which is what produces MTTA/MTTR. Every step is the same
    code the Slack buttons drive, not a demo shortcut."""
    incident = load(incident_id)

    set_status(incident, "acknowledged", actor=actor)
    print(f"          ACKNOWLEDGED by {actor}")

    record_approval_decision(incident, "approved", actor)
    print(f"          APPROVED by {actor} -> status={incident.status}")

    set_status(incident, "verifying", actor=actor)
    print("          VERIFYING")

    resolve_incident(incident, actor=actor)
    print(f"          RESOLVED -> MTTA={incident.mtta_seconds}s MTTR={incident.mttr_seconds}s")
    print("          (both read ~0s here — this walked the lifecycle in "
          "milliseconds; a real incident's figures reflect real response time)")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seed", type=int, help="reproduce an exact log set")
    parser.add_argument("--sources", help="comma-separated source names (default: a random mix)")
    parser.add_argument("--count", type=int, dest="source_count",
                        help="how many sources to mix (default: 3-5)")
    parser.add_argument("--injections", type=int,
                        help="how many error signatures to inject (default: 1-4)")
    parser.add_argument("--clean", action="store_true",
                        help="inject nothing — background lines only")
    parser.add_argument("--no-slack", action="store_true", dest="no_slack",
                        help="triage without alerting")
    parser.add_argument("--lifecycle", action="store_true",
                        help="walk the opened incident acknowledged -> approved -> "
                             "verifying -> resolved, reporting MTTA/MTTR")
    parser.add_argument("--actor", default="U_DEMO",
                        help="Slack user ID attributed to lifecycle actions")
    parser.add_argument("--sessions", type=int, default=1,
                        help="how many sessions to run in one go (default: 1)")
    parser.add_argument("--json", action="store_true", help="print the raw result as JSON")
    parser.add_argument("--list", action="store_true", help="list sessions built so far")
    parser.add_argument("--show", metavar="SESSION_ID",
                        help="re-read a previously built set without alerting")
    parser.add_argument("--list-sources", action="store_true",
                        help="list the log sources a set can be mixed from")
    args = parser.parse_args()

    if args.list_sources:
        for source in SOURCES:
            role = "ETL (triaged)" if source.injectable else "infrastructure noise"
            corpus = source.corpus_path or "generated"
            print(f"  {source.name:22} {role:22} {source.description}  [{corpus}]")
        return 0

    if args.list:
        sessions = list_sessions()
        if not sessions:
            print("No log sets built yet — run scripts/logset.py")
        for session_id in sessions:
            print(f"  {session_id}")
        return 0

    if args.show:
        logset = load_session(args.show)
        analysis = analyse_logset(logset)
        archive = bundle(logset)
        result = {
            "logset": logset_summary(logset, analysis, archive),
            "signals": analysis["signals"],
            "incident": None,
            "score": score(logset, analysis),
        }
        if not args.json:
            _print_summary(result)
    else:
        sessions = max(1, args.sessions)
        for run in range(sessions):
            # A fixed seed mixes one specific set, so it applies to the
            # first run only — the rest of a batch are fresh mixes.
            result = triage_logset(
                seed=args.seed if run == 0 else None,
                sources=args.sources.split(",") if args.sources else None,
                source_count=args.source_count,
                injections=args.injections,
                clean=args.clean,
                notify=not args.no_slack,
            )
            if not args.json:
                _print_summary(result)

            if args.lifecycle and result["incident"]:
                print()
                print("Lifecycle")
                run_lifecycle(result["incident"]["incident_id"], args.actor)
                result["incident"] = load(result["incident"]["incident_id"]).to_dict()
            elif args.lifecycle:
                print("\nLifecycle nothing to walk — this set opened no incident.")

            if run < sessions - 1 and not args.json:
                print("\n" + "-" * 66 + "\n")

        if sessions > 1 and not args.json:
            print(f"\n{sessions} sessions run. For MTTA/MTTR across every incident "
                  f"on disk:\n  python scripts/incident_metrics.py")

    if args.json:
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
