"""
Slack request signature verification. Anyone who finds the interactivity
URL can forge a button click, so every inbound request from Slack must be
verified before its payload is trusted — see Slack's signing secrets docs.

Two things that are easy to get wrong and must be right: the base string
uses the RAW request body bytes, not a re-serialised parse (any reordering
of a form/JSON body breaks the HMAC); and the signature comparison must be
constant-time (hmac.compare_digest), not `==`.
"""

from __future__ import annotations

import hashlib
import hmac
import time

SIGNATURE_VERSION = "v0"
ALLOWED_SKEW_SECONDS = 300


def _get_header(headers: dict, name: str) -> str | None:
    """Case-insensitive header lookup — `headers` may come from different
    callers (a raw WSGI/ASGI dict, a framework's own casing, ...)."""
    name_lower = name.lower()
    for key, value in headers.items():
        if key.lower() == name_lower:
            return value
    return None


def verify_slack_request(
    headers: dict,
    raw_body: bytes,
    signing_secret: str,
    *,
    now: float | None = None,
) -> bool:
    """
    Verify an inbound Slack request per Slack's signing scheme:
    base string = "v0:{timestamp}:{raw_body}", HMAC-SHA256 with the
    signing secret, compared to X-Slack-Signature with a constant-time
    comparison. Returns False (never raises) on any malformed or missing
    input — a missing header is a rejection, not an error.

    `now` is injectable for deterministic tests; defaults to time.time().
    """
    timestamp_header = _get_header(headers, "X-Slack-Request-Timestamp")
    signature_header = _get_header(headers, "X-Slack-Signature")

    if not timestamp_header or not signature_header or not signing_secret:
        return False

    try:
        timestamp = int(timestamp_header)
    except (TypeError, ValueError):
        return False

    current_time = time.time() if now is None else now
    if abs(current_time - timestamp) > ALLOWED_SKEW_SECONDS:
        return False

    base_string = f"{SIGNATURE_VERSION}:{timestamp}:".encode("utf-8") + raw_body
    digest = hmac.new(signing_secret.encode("utf-8"), base_string, hashlib.sha256).hexdigest()
    computed_signature = f"{SIGNATURE_VERSION}={digest}"

    try:
        return hmac.compare_digest(computed_signature, signature_header)
    except TypeError:
        # signature_header wasn't a comparable string (e.g. wrong type from a
        # malformed caller) — treat as a rejection, not a crash.
        return False
