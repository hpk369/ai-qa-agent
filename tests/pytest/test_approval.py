"""
Tests for agent.incident.record_approval_decision — the approval gate.
No live Slack: SLACK_MODE=stub throughout (the default), or a stub
SlackClient instance is passed in explicitly.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import pytest

from agent import incident as incident_module
from agent import slack_client as slack_client_module
from agent.incident import open_incident, persist, record_approval_decision
from agent.severity import SeverityResult
from agent.slack_client import SlackClient


@pytest.fixture(autouse=True)
def incidents_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(incident_module, "INCIDENTS_DIR", tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def stub_dir(tmp_path, monkeypatch):
    slack_dir = tmp_path / "slack"
    monkeypatch.setattr(slack_client_module, "STUB_DIR", slack_dir)
    return slack_dir


def _incident(**overrides):
    result = SeverityResult(
        severity="P2",
        matched_conditions=["row_variance_pct >= 5.0 (actual: 7.3)"],
        rationale="row_variance_pct >= 5.0 (actual: 7.3)",
        response_expectation="Notify on-call, begin remediation, hourly updates",
    )
    run_context = {
        "detected_by": "sql_validator",
        "affected_job": "cx_customer_load",
        "impact_summary": "Customer dimension missing ~7% of records.",
        "confidence": 0.9,
        "requires_approval": True,
    }
    incident = open_incident({}, result, run_context)
    incident.slack_channel = "C_ALERTS"
    incident.slack_ts = "1690000000.000100"
    for key, value in overrides.items():
        setattr(incident, key, value)
    persist(incident)
    return incident


class TestApprove:
    def test_records_decision_and_sets_remediating(self, incidents_dir):
        incident = _incident()
        record_approval_decision(incident, "approved", "U123")

        assert incident.status == "remediating"
        events = [e["event"] for e in incident.timeline]
        assert "approval_decision" in events
        assert any("approved by U123" in e["detail"] for e in incident.timeline)

    def test_change_log_posted(self, stub_dir):
        incident = _incident()
        record_approval_decision(incident, "approved", "U123")

        files = list(stub_dir.glob(f"{incident.incident_id}-*.json"))
        assert files, "expected a #etl-changes post"


class TestReject:
    def test_records_decision_and_sets_acknowledged(self):
        incident = _incident()
        record_approval_decision(incident, "rejected", "U456")

        assert incident.status == "acknowledged"
        assert any("rejected by U456" in e["detail"] for e in incident.timeline)


class TestEscalate:
    def test_records_decision_and_reopens(self):
        incident = _incident(status="acknowledged")
        record_approval_decision(incident, "escalated", "U789")

        assert incident.status == "open"
        assert any("escalated by U789" in e["detail"] for e in incident.timeline)

    def test_mirrors_to_p1_regardless_of_actual_severity(self, stub_dir):
        incident = _incident()  # severity P2, not P1
        assert incident.severity == "P2"

        record_approval_decision(incident, "escalated", "U789")

        # Escalation posts a thread reply, a #etl-changes entry, a P1
        # mirror, and a parent chat.update -- four stub files.
        files = sorted(stub_dir.glob(f"{incident.incident_id}-*.json"))
        assert len(files) == 4


class TestDuplicateDecisionRejected:
    def test_second_decision_does_not_overwrite_the_first(self):
        incident = _incident()
        record_approval_decision(incident, "approved", "U123")
        record_approval_decision(incident, "rejected", "U456")

        # Status stays as the first decision set it (remediating), not
        # silently flipped to acknowledged by the second attempt.
        assert incident.status == "remediating"

    def test_duplicate_attempt_is_reported_not_silently_ignored(self):
        incident = _incident()
        record_approval_decision(incident, "approved", "U123")
        record_approval_decision(incident, "rejected", "U456")

        events = [e["event"] for e in incident.timeline]
        assert "approval_decision_rejected" in events
        detail = next(e["detail"] for e in incident.timeline if e["event"] == "approval_decision_rejected")
        assert "U456" in detail
        assert "rejected" in detail

    def test_duplicate_reply_posted_to_thread(self, stub_dir):
        incident = _incident()
        record_approval_decision(incident, "approved", "U123")
        before = len(list(stub_dir.glob(f"{incident.incident_id}-*.json")))

        record_approval_decision(incident, "rejected", "U456")
        after = len(list(stub_dir.glob(f"{incident.incident_id}-*.json")))

        assert after > before  # the duplicate-decision thread reply was posted

    def test_only_one_approval_decision_event_ever_recorded(self):
        incident = _incident()
        record_approval_decision(incident, "approved", "U123")
        record_approval_decision(incident, "rejected", "U456")
        record_approval_decision(incident, "escalated", "U789")

        decision_events = [e for e in incident.timeline if e["event"] == "approval_decision"]
        assert len(decision_events) == 1


class TestUnknownDecision:
    def test_raises_on_unrecognised_decision(self):
        incident = _incident()
        with pytest.raises(ValueError):
            record_approval_decision(incident, "maybe_later", "U123")


class TestPersistenceSurvivesSlackFailure:
    def test_decision_is_persisted_even_if_slack_notification_fails(self, monkeypatch, incidents_dir):
        def boom(*args, **kwargs):
            raise RuntimeError("Slack is down")

        monkeypatch.setattr(SlackClient, "reply_thread", boom)

        incident = _incident()
        record_approval_decision(incident, "approved", "U123")  # must not raise

        import json

        persisted = json.loads((incidents_dir / f"{incident.incident_id}.json").read_text())
        assert persisted["status"] == "remediating"
