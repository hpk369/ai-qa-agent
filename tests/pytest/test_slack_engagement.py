"""
Tests for agent.incident.sync_slack_engagement and resolve_incident.
A fake Slack client (not agent.slack_client.SlackClient) supplies
thread-reply/reaction fixtures directly, since real polling can't be
exercised without a live Slack workspace — see SlackClient.get_thread_replies's
docstring on the stub-mode limitation.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import pytest

from agent import incident as incident_module
from agent.incident import (
    compute_mtta,
    open_incident,
    persist,
    resolve_incident,
    sync_slack_engagement,
)
from agent.severity import SeverityResult


@pytest.fixture(autouse=True)
def incidents_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(incident_module, "INCIDENTS_DIR", tmp_path)
    return tmp_path


class FakeSlackClient:
    """Supplies canned get_thread_replies/get_reactions/update_parent
    results directly, bypassing any real or stub Slack transport."""

    def __init__(self, replies=None, reactions=None):
        self._replies = replies or []
        self._reactions = reactions or []
        self.update_parent_calls = []

    def get_thread_replies(self, incident):
        return self._replies

    def get_reactions(self, incident):
        return self._reactions

    def update_parent(self, incident, blocks, text):
        self.update_parent_calls.append((incident.incident_id, blocks, text))


def _incident():
    result = SeverityResult(
        severity="P2",
        matched_conditions=["row_variance_pct >= 5.0 (actual: 7.3)"],
        rationale="row_variance_pct >= 5.0 (actual: 7.3)",
        response_expectation="Notify on-call",
    )
    incident = open_incident(
        {}, result, {"detected_by": "logset-triage", "impact_summary": "impact", "confidence": 0.9}
    )
    incident.slack_channel = "C_ALERTS"
    incident.slack_ts = "1690000000.000100"
    persist(incident)
    return incident


class TestSyncSlackEngagementNoPostYet:
    def test_no_slack_ts_is_a_no_op(self):
        result = SeverityResult(severity="P2", matched_conditions=[], rationale="", response_expectation="")
        incident = open_incident({}, result, {"detected_by": "x", "impact_summary": "y", "confidence": 0.5})
        # slack_channel/slack_ts left as None (never posted)
        client = FakeSlackClient(replies=[{"ts": "999", "user": "U1"}])

        sync_slack_engagement(incident, slack_client=client)

        assert not any(e["event"] == "slack_thread_reply" for e in incident.timeline)


class TestThreadReplies:
    def test_human_reply_recorded_and_counts_toward_mtta(self):
        incident = _incident()
        client = FakeSlackClient(replies=[
            {"ts": incident.slack_ts, "user": "BOTUSER", "bot_id": "B123"},  # the parent itself
            {"ts": "1690000030.000200", "user": "U123"},  # a human reply 30s later
        ])

        sync_slack_engagement(incident, slack_client=client)

        assert any(e["event"] == "slack_thread_reply" and e["actor"] == "U123" for e in incident.timeline)
        assert compute_mtta(incident) is not None

    def test_bot_reply_does_not_count(self):
        incident = _incident()
        client = FakeSlackClient(replies=[
            {"ts": "1690000010.000100", "user": "BOTUSER", "bot_id": "B123"},
        ])

        sync_slack_engagement(incident, slack_client=client)

        assert not any(e["event"] == "slack_thread_reply" for e in incident.timeline)
        assert compute_mtta(incident) is None

    def test_parent_message_itself_is_skipped(self):
        incident = _incident()
        client = FakeSlackClient(replies=[{"ts": incident.slack_ts, "user": "U123"}])

        sync_slack_engagement(incident, slack_client=client)

        assert not any(e["event"] == "slack_thread_reply" for e in incident.timeline)

    def test_syncing_twice_does_not_duplicate_entries(self):
        incident = _incident()
        client = FakeSlackClient(replies=[{"ts": "1690000030.000200", "user": "U123"}])

        sync_slack_engagement(incident, slack_client=client)
        sync_slack_engagement(incident, slack_client=client)

        matches = [e for e in incident.timeline if e["event"] == "slack_thread_reply"]
        assert len(matches) == 1


class TestReactions:
    def test_reaction_recorded_as_human_event(self):
        incident = _incident()
        client = FakeSlackClient(reactions=[{"name": "white_check_mark", "users": ["U456"], "count": 1}])

        sync_slack_engagement(incident, slack_client=client)

        assert any(e["event"] == "slack_reaction" and e["actor"] == "U456" for e in incident.timeline)

    def test_syncing_twice_does_not_duplicate_reactions(self):
        incident = _incident()
        client = FakeSlackClient(reactions=[{"name": "eyes", "users": ["U456"], "count": 1}])

        sync_slack_engagement(incident, slack_client=client)
        sync_slack_engagement(incident, slack_client=client)

        matches = [e for e in incident.timeline if e["event"] == "slack_reaction"]
        assert len(matches) == 1


class TestResolveIncident:
    def test_resolve_sets_status_and_both_metrics(self):
        incident = _incident()
        client = FakeSlackClient(replies=[{"ts": "1690000030.000200", "user": "U123"}])

        resolve_incident(incident, actor="U123", slack_client=client)

        assert incident.status == "resolved"
        assert incident.mtta_seconds is not None
        assert incident.mttr_seconds is not None

    def test_resolve_updates_slack_parent(self):
        incident = _incident()
        client = FakeSlackClient()

        resolve_incident(incident, actor="U123", slack_client=client)

        assert len(client.update_parent_calls) == 1
        incident_id, blocks, text = client.update_parent_calls[0]
        assert incident_id == incident.incident_id
        rendered = str(blocks)
        assert "RESOLVED" in rendered or "✅" in rendered

    def test_resolve_persists(self, incidents_dir):
        import json

        incident = _incident()
        client = FakeSlackClient()

        resolve_incident(incident, actor="U123", slack_client=client)

        persisted = json.loads((incidents_dir / f"{incident.incident_id}.json").read_text())
        assert persisted["status"] == "resolved"

    def test_resolve_survives_slack_update_failure(self):
        class BoomClient(FakeSlackClient):
            def update_parent(self, incident, blocks, text):
                raise RuntimeError("Slack is down")

        incident = _incident()
        resolve_incident(incident, actor="U123", slack_client=BoomClient())  # must not raise
        assert incident.status == "resolved"
