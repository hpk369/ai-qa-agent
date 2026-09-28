"""
Tests for the approval console (agent/console.py and its routes).

The console exists because an incident opened by a CI runner does not
survive the runner. These tests therefore separate the two disks the
real system has — what the runner wrote, and what the console holds —
rather than assuming one process sees both.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import pytest
from fastapi.testclient import TestClient

from agent import console as console_module
from agent import incident as incident_module
from agent import slack_client as slack_client_module
from agent.console import ACCESS_EMAIL_HEADER, approver_from_request, token_accepted
from agent.incident import load, open_incident, persist
from agent.severity import SeverityResult

APPROVER = "admin@inkandinfra.com"
ACCESS = {ACCESS_EMAIL_HEADER: APPROVER}


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(incident_module, "INCIDENTS_DIR", tmp_path / "incidents")
    monkeypatch.setattr(slack_client_module, "STUB_DIR", tmp_path / "slack")
    monkeypatch.setenv("SLACK_MODE", "stub")
    monkeypatch.delenv("AGENT_PUBLIC_URL", raising=False)
    monkeypatch.delenv("AGENT_INGEST_URL", raising=False)
    monkeypatch.delenv("AGENT_INGEST_TOKEN", raising=False)
    return tmp_path


@pytest.fixture
def client():
    from agent.agent import app
    return TestClient(app)


def _incident(severity="P1"):
    result = SeverityResult(
        severity=severity,
        matched_conditions=["control_total_mismatch"],
        rationale="control_total_mismatch",
        response_expectation="Notify on-call",
    )
    incident = open_incident({}, result, {
        "detected_by": "logset-triage",
        "affected_job": "settlement_load",
        "affected_objects": ["target.settlement_fact"],
        "impact_summary": "Control totals do not reconcile.",
        "confidence": 0.9,
        "requires_approval": True,
    })
    persist(incident)  # open_incident builds the record; the caller stores it
    return incident


class TestIngestToken:
    def test_no_token_configured_rejects_everything(self):
        """An ingest endpoint open by default would let anyone write the
        records the console then acts on."""
        assert token_accepted("anything") is False
        assert token_accepted(None) is False

    def test_matching_token_accepted_and_others_not(self, monkeypatch):
        monkeypatch.setenv("AGENT_INGEST_TOKEN", "s3cret")
        assert token_accepted("s3cret") is True
        assert token_accepted("s3cre") is False
        assert token_accepted(None) is False


class TestAccessIdentity:
    def test_header_supplies_the_approver(self):
        assert approver_from_request({ACCESS_EMAIL_HEADER: APPROVER}) == APPROVER
        assert approver_from_request({ACCESS_EMAIL_HEADER.lower(): APPROVER}) == APPROVER

    def test_absent_or_blank_header_is_not_an_identity(self):
        """Reaching the origin directly must not be treated as anonymous-but-
        allowed: the identity is what the audit trail records."""
        assert approver_from_request({}) is None
        assert approver_from_request({ACCESS_EMAIL_HEADER: "   "}) is None


class TestConsoleRoutes:
    def test_ingest_requires_the_token(self, client):
        assert client.post("/incidents", json={"incident_id": "INC-1"}).status_code == 401

    def test_pushed_incident_becomes_actionable(self, client, monkeypatch, isolated_dirs):
        """The whole point: an incident this server never opened can still be
        loaded and decided, because the runner pushed it."""
        monkeypatch.setenv("AGENT_INGEST_TOKEN", "s3cret")
        import dataclasses
        incident = _incident()
        payload = dataclasses.asdict(incident)

        # Simulate the runner's disk vanishing before anyone clicks.
        (isolated_dirs / "incidents" / f"{incident.incident_id}.json").unlink()

        pushed = client.post("/incidents", json=payload,
                             headers={"Authorization": "Bearer s3cret"})
        assert pushed.status_code == 200

        page = client.get(f"/incident/{incident.incident_id}", headers=ACCESS)
        assert page.status_code == 200
        assert incident.incident_id in page.text

    def test_page_without_access_header_records_nothing(self, client):
        incident = _incident()
        page = client.get(f"/incident/{incident.incident_id}")
        assert "Not authenticated" in page.text
        assert load(incident.incident_id).status == "open"

    def test_unknown_incident_says_so_rather_than_erroring(self, client):
        page = client.get("/incident/INC-does-not-exist", headers=ACCESS)
        assert page.status_code == 200
        assert "Unknown incident" in page.text

    def test_opening_the_link_decides_nothing(self, client):
        """Slack unfurls URLs and clients prefetch them, so a GET carrying an
        intent must stay inert."""
        incident = _incident()
        client.get(f"/incident/{incident.incident_id}?intent=approved", headers=ACCESS)
        assert load(incident.incident_id).status == "open"

    def test_posting_a_decision_records_it_against_the_access_identity(self, client):
        incident = _incident()
        done = client.post(f"/incident/{incident.incident_id}/decision",
                           data={"decision": "approved"}, headers=ACCESS)
        assert done.status_code == 200
        stored = load(incident.incident_id)
        assert stored.status == "remediating"
        assert any(e.get("actor") == APPROVER for e in stored.timeline)

    def test_a_decision_without_an_identity_is_refused(self, client):
        incident = _incident()
        assert client.post(f"/incident/{incident.incident_id}/decision",
                           data={"decision": "approved"}).status_code == 401
        assert load(incident.incident_id).status == "open"

    def test_an_unknown_decision_is_refused(self, client):
        incident = _incident()
        assert client.post(f"/incident/{incident.incident_id}/decision",
                           data={"decision": "delete"}, headers=ACCESS).status_code == 400

    def test_a_decided_incident_offers_no_second_decision(self, client):
        incident = _incident()
        client.post(f"/incident/{incident.incident_id}/decision",
                    data={"decision": "approved"}, headers=ACCESS)
        page = client.get(f"/incident/{incident.incident_id}", headers=ACCESS)
        assert "Already decided" in page.text


class TestSlackButtons:
    def test_without_a_console_the_buttons_post_to_slack(self):
        from agent.slack_blocks import build_parent_message
        blocks, _ = build_parent_message(_incident())
        actions = next(b for b in blocks if b["type"] == "actions")
        assert [e["action_id"] for e in actions["elements"]] == [
            "incident_approve", "incident_reject", "incident_escalate"]
        assert all("url" not in e for e in actions["elements"])

    def test_with_a_console_the_buttons_link_to_it(self, monkeypatch):
        monkeypatch.setenv("AGENT_PUBLIC_URL", "https://triage.inkandinfra.com/")
        from agent.slack_blocks import build_parent_message
        incident = _incident()
        blocks, _ = build_parent_message(incident)
        actions = next(b for b in blocks if b["type"] == "actions")
        urls = [e["url"] for e in actions["elements"]]
        assert urls == [
            f"https://triage.inkandinfra.com/incident/{incident.incident_id}?intent=approved",
            f"https://triage.inkandinfra.com/incident/{incident.incident_id}?intent=rejected",
            f"https://triage.inkandinfra.com/incident/{incident.incident_id}?intent=escalated",
        ]
        # A url button needs no interactivity endpoint, so it must not also
        # carry a value that only the /slack/action path would read.
        assert all("value" not in e for e in actions["elements"])


class TestPush:
    def test_no_push_without_a_configured_console(self):
        assert console_module.push_incident(_incident()) is False

    def test_a_failing_push_warns_but_does_not_raise(self, monkeypatch, capsys):
        """The alert is already in Slack by this point. Losing the console's
        copy must not take the triage run down with it."""
        monkeypatch.setenv("AGENT_INGEST_URL", "https://triage.example.com/incidents")
        monkeypatch.setenv("AGENT_INGEST_TOKEN", "s3cret")
        import httpx

        def boom(*args, **kwargs):
            raise httpx.ConnectError("nope")

        monkeypatch.setattr(httpx, "post", boom)
        assert console_module.push_incident(_incident()) is False
        assert "could not push" in capsys.readouterr().out

    def test_the_push_carries_the_token_and_the_slack_ts(self, monkeypatch):
        monkeypatch.setenv("AGENT_INGEST_URL", "https://triage.example.com/incidents")
        monkeypatch.setenv("AGENT_INGEST_TOKEN", "s3cret")
        seen = {}
        import httpx

        class Resp:
            status_code = 200

        def fake_post(url, **kwargs):
            seen.update(url=url, json=kwargs.get("json"), headers=kwargs.get("headers"))
            return Resp()

        monkeypatch.setattr(httpx, "post", fake_post)
        incident = _incident()
        incident.slack_ts, incident.slack_channel = "111.222", "C_ALERTS"

        assert console_module.push_incident(incident) is True
        assert seen["headers"]["Authorization"] == "Bearer s3cret"
        assert seen["json"]["slack_ts"] == "111.222"
