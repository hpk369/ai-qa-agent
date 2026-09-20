#!/usr/bin/env python3
"""
Fetch the public log corpus that session log sets are mixed from.

    python scripts/fetch_logs.py            # fetch what's missing
    python scripts/fetch_logs.py --force    # re-download everything
    python scripts/fetch_logs.py --status   # what's on disk right now

Downloads the LogHub sample logs (https://github.com/logpai/loghub) into
logsets/corpus/, which is gitignored — these are third-party research
datasets, so the repo fetches them rather than vendoring them.

Running this is optional. Without it, every session still gets a full log
set; the background lines are generated rather than real, and the session
manifest says so per file.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.env import load_env

load_env()

from logsets.corpus import CORPUS_DIR, LOGHUB_ATTRIBUTION, corpus_status, fetch_corpus


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true",
                        help="re-download files already present")
    parser.add_argument("--status", action="store_true",
                        help="report what is on disk and exit without downloading")
    parser.add_argument("--timeout", type=int, default=60,
                        help="per-file download timeout in seconds (default: 60)")
    args = parser.parse_args()

    if args.status:
        print(f"Corpus directory: {CORPUS_DIR}")
        for name, present in corpus_status().items():
            print(f"  {'real   ' if present else 'generated'}  {name}")
        return 0

    print(f"Fetching public log corpus into {CORPUS_DIR}")
    results = fetch_corpus(force=args.force, timeout=args.timeout)
    failures = 0
    for name, outcome in results.items():
        if outcome.startswith("unavailable"):
            failures += 1
        print(f"  {name:22} {outcome}")

    print()
    print(LOGHUB_ATTRIBUTION)
    if failures:
        print(f"\n{failures} source(s) unavailable — those fall back to generated "
              f"background, which is a supported mode, not a failure.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
