#!/usr/bin/env python3
"""
Prove the credential works — the thing to run right after wiring up
Workload Identity Federation.

    python scripts/check_credentials.py           # resolve only, no API call
    python scripts/check_credentials.py --live    # make one real call
    python scripts/check_credentials.py --live --expect "workload identity federation"

Without --live it reports which credential the SDK will use and stops.
With --live it makes one deliberately tiny request (a few tokens, a
fraction of a cent) and prints the reply, which is what completes the
Console wizard's connection test.

It reads the same environment as everything else in this repo, so there
are no identifiers baked into this file:

    ANTHROPIC_FEDERATION_RULE_ID    fdrl_...
    ANTHROPIC_ORGANIZATION_ID       the organization UUID
    ANTHROPIC_SERVICE_ACCOUNT_ID    svac_...
    ANTHROPIC_WORKSPACE_ID          wrkspc_...  (only for a multi-workspace rule)
    ANTHROPIC_IDENTITY_TOKEN_FILE   the JWT's path — or ANTHROPIC_IDENTITY_TOKEN,
                                    or JWT, the variable the Console's snippet reads

Exits non-zero when the credential is missing, is not the one you asked
for, or the call fails.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.env import load_env

load_env()

from agent import llm
from agent.providers import AnthropicProvider, resolve

PROBE = "Reply with the single word: ready"


def _report_federation() -> None:
    """Print the identifiers in play. They are identifiers, not secrets —
    printing them is what makes a misconfigured run diagnosable."""
    fields = {
        "federation rule": "ANTHROPIC_FEDERATION_RULE_ID",
        "organization": "ANTHROPIC_ORGANIZATION_ID",
        "service account": "ANTHROPIC_SERVICE_ACCOUNT_ID",
        "workspace": "ANTHROPIC_WORKSPACE_ID",
    }
    for label, name in fields.items():
        value = os.getenv(name)
        if value:
            print(f"  {label:16} {value}")
        elif name == "ANTHROPIC_WORKSPACE_ID":
            print(f"  {label:16} (unset — the rule's single workspace is used)")

    for name in ("ANTHROPIC_IDENTITY_TOKEN_FILE", "ANTHROPIC_IDENTITY_TOKEN",
                 os.getenv("ANTHROPIC_IDENTITY_TOKEN_ENV", "JWT")):
        if os.getenv(name):
            # Never the token itself: where it came from, and that it looks like one.
            token = (Path(os.environ[name]).read_text().strip()
                     if name.endswith("_FILE") and Path(os.environ[name]).exists()
                     else os.environ[name])
            shape = "looks like a JWT" if token.startswith("ey") else "does NOT look like a JWT"
            print(f"  identity token   from {name} ({len(token)} chars, {shape})")
            break


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true",
                        help="make one small real request instead of only resolving")
    parser.add_argument("--expect", metavar="SOURCE",
                        help="fail unless the credential source matches, e.g. "
                             "'workload identity federation'")
    parser.add_argument("--model", help="override the model for the probe")
    args = parser.parse_args()

    provider = resolve()
    if provider is None:
        print("No model provider is configured — the agent would run on its "
              "deterministic fallbacks.", file=sys.stderr)
        return 1

    print(f"provider          {provider.name}")
    print(f"model             {args.model or provider.model}")

    if isinstance(provider, AnthropicProvider):
        source = provider.credential_source()
        print(f"credential        {source}")
        if source == "workload identity federation":
            _report_federation()
        if args.expect and source != args.expect:
            print(f"\nExpected '{args.expect}' but the SDK will use '{source}'.",
                  file=sys.stderr)
            return 1
    elif args.expect:
        print(f"\nExpected '{args.expect}' but the provider is {provider.name}.",
              file=sys.stderr)
        return 1

    if not args.live:
        print("\nResolved only. Re-run with --live to exchange the token and call the API.")
        return 0

    if args.model:
        provider.model = args.model

    print("\ncalling the API...")
    try:
        response = provider._client().messages.create(
            model=provider.model,
            max_tokens=16,
            messages=[{"role": "user", "content": PROBE}],
        )
    except Exception as exc:  # noqa: BLE001 - this script exists to report the failure
        print(f"\nThe call failed: {llm._describe_exception(exc)}", file=sys.stderr)
        print("If this is a federation setup, check that the rule's subject, audience "
              "and workspace match the token the workload presents, and that the "
              "service account is a member of that workspace.", file=sys.stderr)
        return 1

    text = next((block.text for block in response.content if block.type == "text"), "")
    usage = response.usage
    print(f"reply             {text.strip()!r}")
    print(f"tokens            {usage.input_tokens} in / {usage.output_tokens} out")
    print("\nThe credential works end to end.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
