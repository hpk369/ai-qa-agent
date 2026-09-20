#!/usr/bin/env python3
"""
Reply to an incident's Slack thread.

    python scripts/slack_reply.py INC-20260920-0042 "restarted the consumer, lag is draining"
    python scripts/slack_reply.py INC-20260920-0042 --react white_check_mark
    python scripts/slack_reply.py INC-20260920-0042 --list

In SLACK_MODE=stub (the default) there is no workspace to type into, so
this writes the reply into the local inbox the stub Slack client reads —
reports/slack/inbox/<incident_id>.jsonl. Everything downstream treats it
exactly as it treats a real thread reply: the streaming gate
(logsets/stream.py) picks it up on its next poll and asks Claude whether
it confirms the incident is resolved, and MTTA counts it as the first
human response.

With SLACK_MODE=live a human replies in Slack itself and this script is
unnecessary — the agent reads the real thread.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.incident import load
from agent.slack_client import SlackClient


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("incident_id")
    parser.add_argument("text", nargs="?", help="the reply text")
    parser.add_argument("--react", metavar="EMOJI",
                        help="add a reaction instead of a reply (e.g. white_check_mark)")
    parser.add_argument("--user", default="U_ONCALL", help="Slack user ID to attribute it to")
    parser.add_argument("--list", action="store_true", dest="show",
                        help="show what is already in this incident's thread")
    args = parser.parse_args()

    try:
        incident = load(args.incident_id)
    except FileNotFoundError:
        print(f"No such incident: {args.incident_id}", file=sys.stderr)
        return 1

    client = SlackClient()
    if client.mode != "stub":
        print("SLACK_MODE is not stub — reply in the real Slack thread instead; "
              "the agent reads it from there.", file=sys.stderr)
        return 1

    path = client.inbox_path(incident)

    if args.show:
        entries = client._read_inbox(incident)
        if not entries:
            print(f"Nothing in {args.incident_id}'s thread yet.")
        for entry in entries:
            if entry.get("reaction"):
                print(f"  {entry.get('user')} reacted :{entry['reaction']}:")
            else:
                print(f"  {entry.get('user')}: {entry.get('text')}")
        return 0

    if not args.text and not args.react:
        parser.error("give reply text, or --react EMOJI")

    entry = {"user": args.user, "ts": f"{time.time():.6f}"}
    if args.react:
        entry["reaction"] = args.react.strip(":")
    else:
        entry["text"] = args.text

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")

    what = f":{entry['reaction']}:" if args.react else f'"{args.text}"'
    print(f"Sent {what} to {args.incident_id}'s thread as {args.user}.")
    if os.getenv("SLACK_MODE", "stub") == "stub":
        print(f"  (stub mode — written to {path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
