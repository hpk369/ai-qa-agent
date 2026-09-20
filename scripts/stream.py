#!/usr/bin/env python3
"""
Stream logs the way a live environment produces them, and triage them as
they arrive.

    python scripts/stream.py                       # 3 minutes of live logs
    python scripts/stream.py --rate 12 --duration 600
    python scripts/stream.py --sources kafka-consumer,spark-executor
    python scripts/stream.py --seed 42             # same failures, same order
    python scripts/stream.py --auto-resolve 20     # unattended demo, nobody at Slack
    python scripts/stream.py --no-slack            # triage only, alert nothing

Lines arrive continuously; the agent tails them and alerts the moment a
failure signature appears — not at the end of a batch.

A blocking failure (P1/P2 by default) stops the pipeline advancing, as it
would in production. The stream drops to a trickle of backpressure lines
and waits. It resumes only when a human confirms in Slack that the
problem is fixed — in SLACK_MODE=stub, that is:

    python scripts/slack_reply.py <incident-id> "restarted the consumer, lag is draining"

A model decides whether a reply actually confirms resolution ("looking
into it" does not) — Claude, a local Ollama, or any OpenAI-compatible
endpoint; see LLM_PROVIDER in .env.example. With none configured the
check falls back to a narrow keyword match, and the gate stays shut on
anything it does not recognise outright — a ✅ reaction
(`--react white_check_mark`) always works.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import llm
from logsets.stream import StreamConfig, StreamEvent, StreamRunner

ICONS = {
    "started": "▶",
    "incident": "🚨",
    "continuing": "→",
    "blocked": "⏸",
    "reply": "💬",
    "judgement": "🤖",
    "resumed": "▶️",
    "still_blocked": "⛔",
    "warning": "⚠",
    "finished": "■",
}


def render(event: StreamEvent) -> None:
    icon = ICONS.get(event.kind, "·")
    print(f"[{event.at:7.1f}s] {icon} {event.detail}")

    if event.kind == "incident":
        payload = event.payload
        print(f"            {payload['impact_summary']}")
        print(f"            signatures: {', '.join(payload['signatures']) or 'none'}"
              f"   runbook: {payload.get('runbook') or 'none'}")
        if payload.get("narrated_by") == "fallback":
            print("            (deterministic summary — no model configured or the call failed)")
    elif event.kind == "blocked":
        print("            the pipeline is held here until someone confirms it is fixed:")
        print(f"            {event.payload['reply_with']}")
    sys.stdout.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rate", type=float, default=6.0, help="log lines per second (default: 6)")
    parser.add_argument("--duration", type=float, default=180.0,
                        help="seconds to stream for; 0 means until interrupted (default: 180)")
    parser.add_argument("--sources", help="comma-separated source names (default: a random mix)")
    parser.add_argument("--seed", type=int, help="reproduce a stream's failures and order")
    parser.add_argument("--gap", type=float, nargs=2, metavar=("MIN", "MAX"), default=(15.0, 45.0),
                        help="seconds between injected failures (default: 15 45)")
    parser.add_argument("--auto-resolve", type=float, metavar="SECONDS",
                        help="resolve a blocking incident automatically after N seconds, "
                             "for a demo with nobody watching Slack")
    parser.add_argument("--max-block-wait", type=float, default=600.0,
                        help="stop the stream if an incident stays unresolved this long "
                             "(default: 600)")
    parser.add_argument("--block-on", default="P1,P2",
                        help="severities that stop the stream until confirmed "
                             "(default: P1,P2; use P1,P2,P3,P4 to demo the gate on anything)")
    parser.add_argument("--poll", type=float, default=3.0,
                        help="how often to check Slack while blocked (default: 3s)")
    parser.add_argument("--real-background", action="store_true",
                        help="draw background lines from the fetched public corpus "
                             "(they carry their own original timestamps)")
    parser.add_argument("--no-slack", action="store_true", dest="no_slack",
                        help="triage without alerting")
    parser.add_argument("--json", action="store_true", help="print the result as JSON at the end")
    args = parser.parse_args()

    config = StreamConfig(
        blocking_severities=frozenset(
            level.strip().upper() for level in args.block_on.split(",") if level.strip()),
        rate=args.rate,
        duration=None if args.duration == 0 else args.duration,
        incident_gap=(args.gap[0], args.gap[1]),
        poll_interval=args.poll,
        auto_resolve_after=args.auto_resolve,
        max_block_wait=args.max_block_wait,
    )

    print(f"Model: {llm.describe()}")
    runner = StreamRunner(
        config=config,
        sources=args.sources.split(",") if args.sources else None,
        seed=args.seed,
        real_background=args.real_background,
        notify=not args.no_slack,
        on_event=None if args.json else render,
    )

    try:
        result = runner.run()
    except KeyboardInterrupt:
        print("\ninterrupted — writing the manifest and bundling what was streamed")
        runner._sync_manifest()
        return 130

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    print()
    print(f"Stream    {result['session_id']}  (seed {result['seed']})")
    print(f"Emitted   {result['lines_emitted']} lines")
    print(f"Incidents {len(result['incidents'])}")
    for incident in result["incidents"]:
        print(f"  {incident['at']:7.1f}s  {incident['incident_id']}  {incident['severity']}  "
              f"{', '.join(incident['signatures'])}  (narrated by {incident['narrated_by']})")
    print(f"Download  {result['download']['archive']}")
    if result["outcome"] == "blocked":
        print("\nThe stream stopped while still blocked — an incident was never confirmed "
              "resolved. That is the designed outcome, not a crash.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
