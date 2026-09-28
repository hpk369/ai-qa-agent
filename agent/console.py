"""
The approval console: a small web page that stands in for Slack's
interactivity endpoint.

Why this exists rather than Slack buttons that POST to /slack/action:
an incident lives in ``reports/incidents/`` on the machine that opened
it, and the machine that opens incidents in CI is a GitHub runner that
is destroyed minutes later. A Slack button POSTing to a server
elsewhere finds no such incident and can only log "unknown incident".
So the runner *pushes* each incident here as it alerts (see
``push_incident``), this server keeps it on its own disk, and the Slack
alert links to a page here instead of calling back.

That swap also removes Slack's signing secret from the critical path:
a Block Kit button carrying ``url`` opens a link rather than asking
Slack to call us, so nothing inbound needs verifying.

Authentication is Cloudflare Access, in front of this app rather than
inside it — see ``approver_from_request``. The app never sees a
password and never stores one.
"""

from __future__ import annotations

import hmac
import os
from typing import Any

from agent.incident import Incident, load, persist

# Cloudflare Access puts the verified identity on every proxied request.
ACCESS_EMAIL_HEADER = "Cf-Access-Authenticated-User-Email"

# The shared token CI presents when pushing an incident to this server.
INGEST_TOKEN_ENV = "AGENT_INGEST_TOKEN"

DECISIONS = ("approved", "rejected", "escalated")
_DECISION_LABELS = {"approved": "Approve", "rejected": "Reject", "escalated": "Escalate"}


def console_base_url() -> str:
    """Where this console is reachable, or "" when it is not deployed.

    An empty value is the signal to keep the old in-Slack buttons: a link
    to a console that does not exist is worse than no link.
    """
    return os.getenv("AGENT_PUBLIC_URL", "").rstrip("/")


def incident_url(incident_id: str) -> str:
    base = console_base_url()
    return f"{base}/incident/{incident_id}" if base else ""


def ingest_token() -> str:
    return os.getenv(INGEST_TOKEN_ENV, "")


def token_accepted(presented: str | None) -> bool:
    """Compare the pushed token in constant time.

    An unset token on the server rejects everything rather than accepting
    everything: an ingest endpoint that is open by default would let
    anyone write incident records onto this disk, and those records are
    what the console then acts on.
    """
    expected = ingest_token()
    if not expected or not presented:
        return False
    return hmac.compare_digest(expected, presented)


def approver_from_request(headers: Any) -> str | None:
    """Who is acting, according to Cloudflare Access.

    Returns None when the header is absent, which means the request did
    not come through Access — either the tunnel is misrouted or someone
    reached the origin directly. Either way this app refuses to guess an
    identity, because the identity is what the audit trail records.
    """
    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    email = headers.get(ACCESS_EMAIL_HEADER) or headers.get(ACCESS_EMAIL_HEADER.lower())
    email = (email or "").strip()
    return email or None


def store_pushed_incident(payload: dict[str, Any]) -> Incident:
    """Write an incident pushed by the triage run onto this server's disk.

    Deliberately last-write-wins: the runner is the source of truth for
    everything up to the alert, and a re-push carries a fuller record
    (a ts, an updated status), not a stale one.
    """
    incident = Incident(**payload)
    persist(incident)
    return incident


def already_decided(incident: Incident) -> str | None:
    """The decision this incident already carries, if any.

    The console reads this to render a decided incident as a record
    rather than a form. record_approval_decision enforces the same rule
    server-side — this only keeps the page from offering an action that
    would be refused.
    """
    for entry in incident.timeline:
        if entry.get("event") == "approval_decision":
            return entry.get("detail", "decided")
    return None


def load_for_console(incident_id: str) -> Incident | None:
    try:
        return load(incident_id)
    except FileNotFoundError:
        return None


def push_incident(incident: Incident) -> bool:
    """Send an incident to the approval console, if one is configured.

    Called after the alert is posted, so the pushed record already carries
    its Slack ts and channel — the console needs those to reply in the
    thread and update the parent message.

    Returns whether the push happened. A failure here is reported and
    swallowed: the alert is already in Slack and the incident already
    persisted locally, so the only thing lost is the console's copy, and
    taking a triage run down over it would trade a working alert for no
    alert. The cost is a button that lands on "unknown incident", which
    says exactly that when it happens.
    """
    import dataclasses

    import httpx

    url, token = os.getenv("AGENT_INGEST_URL", "").strip(), ingest_token()
    if not url or not token:
        return False
    try:
        response = httpx.post(
            url.rstrip("/"),
            json=dataclasses.asdict(incident),
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
        )
        if response.status_code >= 400:
            # Never the token: this prints on a runner whose log is public.
            print(f"[console] WARNING: pushing {incident.incident_id} returned "
                  f"HTTP {response.status_code}; its buttons will report an unknown incident")
            return False
    except Exception as exc:  # noqa: BLE001 - the alert is already out; see the docstring
        print(f"[console] WARNING: could not push {incident.incident_id} to the console "
              f"({type(exc).__name__}); its buttons will report an unknown incident")
        return False
    return True
