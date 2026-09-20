"""
Tests for agent.slack_client. No live Slack in this suite — every test
either forces SLACK_MODE=stub (no network at all) or monkeypatches
httpx.post directly. Covers ts capture + persistence, thread replies
carrying the right thread_ts, retry on 429, stub mode writing files and
making no network call, and bot-token redaction in raised errors.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import pytest

from agent import incident as incident_module
from agent import slack_client as slack_client_module
from agent.incident import open_incident
from agent.severity import SeverityResult
from agent.slack_client import SlackAPIError, SlackClient


@pytest.fixture(autouse=True)
def incidents_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(incident_module, "INCIDENTS_DIR", tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def stub_dir(tmp_path, monkeypatch):
    slack_stub_dir = tmp_path / "slack"
    monkeypatch.setattr(slack_client_module, "STUB_DIR", slack_stub_dir)
    return slack_stub_dir


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Retries in these tests must not actually wait."""
    monkeypatch.setattr(slack_client_module.time, "sleep", lambda _seconds: None)


def _incident():
    result = SeverityResult(
        severity="P2",
        matched_conditions=["row_variance_pct >= 5.0 (actual: 7.3)"],
        rationale="row_variance_pct >= 5.0 (actual: 7.3)",
        response_expectation="Notify on-call, begin remediation, hourly updates",
    )
    run_context = {
        "detected_by": "logset-triage",
        "affected_job": "cx_customer_load",
        "affected_objects": ["target.customer_dim"],
        "impact_summary": "Customer dimension missing ~7% of records.",
        "confidence": 0.9,
        "requires_approval": True,
    }
    return open_incident({}, result, run_context)


def _blocks(text="test message"):
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]


class FakeResponse:
    def __init__(self, status_code, json_data=None, headers=None):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.headers = headers or {}

    def json(self):
        return self._json_data


class TestStubMode:
    def test_post_incident_writes_file_and_no_network_call(self, monkeypatch, stub_dir):
        called = {"count": 0}

        def fail_if_called(*args, **kwargs):
            called["count"] += 1
            raise AssertionError("stub mode must not make a network call")

        monkeypatch.setattr(slack_client_module.httpx, "post", fail_if_called)

        client = SlackClient(mode="stub", channel_alerts="C_ALERTS")
        incident = _incident()
        ts = client.post_incident(incident, _blocks(), "fallback text")

        assert called["count"] == 0
        assert ts  # a synthetic ts was returned
        files = list(stub_dir.glob(f"{incident.incident_id}-*.json"))
        assert len(files) == 1

        payload = json.loads(files[0].read_text())
        assert payload["method"] == "chat.postMessage"
        assert payload["payload"]["channel"] == "C_ALERTS"

    def test_stub_seq_increments_across_calls_for_same_incident(self, stub_dir):
        client = SlackClient(mode="stub", channel_alerts="C_ALERTS", channel_changes="C_CHANGES")
        incident = _incident()
        client.post_incident(incident, _blocks(), "text")
        client.post_change_log(incident, "approved", "U123")

        files = sorted(p.name for p in stub_dir.glob(f"{incident.incident_id}-*.json"))
        assert files == [f"{incident.incident_id}-1.json", f"{incident.incident_id}-2.json"]


class TestPostIncidentPersistence:
    def test_ts_and_channel_written_back_and_persisted(self, monkeypatch, incidents_dir):
        monkeypatch.setattr(
            slack_client_module.httpx,
            "post",
            lambda *a, **k: FakeResponse(200, {"ok": True, "ts": "1690000000.000100", "channel": "C_ALERTS"}),
        )
        client = SlackClient(mode="live", bot_token="xoxb-test", channel_alerts="C_ALERTS")
        incident = _incident()

        ts = client.post_incident(incident, _blocks(), "fallback text")

        assert ts == "1690000000.000100"
        assert incident.slack_ts == "1690000000.000100"
        assert incident.slack_channel == "C_ALERTS"

        persisted = json.loads((incidents_dir / f"{incident.incident_id}.json").read_text())
        assert persisted["slack_ts"] == "1690000000.000100"


class TestReplyThread:
    def test_reply_carries_parent_ts_as_thread_ts(self, monkeypatch):
        captured = {}

        def fake_post(url, json, headers, timeout):
            captured["payload"] = json
            return FakeResponse(200, {"ok": True, "ts": "1690000001.000200"})

        monkeypatch.setattr(slack_client_module.httpx, "post", fake_post)

        client = SlackClient(mode="live", bot_token="xoxb-test")
        incident = _incident()
        incident.slack_channel = "C_ALERTS"
        incident.slack_ts = "1690000000.000100"

        ts = client.reply_thread(incident, _blocks("ack"), "ack")

        assert ts == "1690000001.000200"
        assert captured["payload"]["thread_ts"] == "1690000000.000100"
        assert captured["payload"]["channel"] == "C_ALERTS"


class TestRetryOn429:
    def test_retries_then_succeeds(self, monkeypatch):
        calls = []

        def fake_post(url, json, headers, timeout):
            calls.append(1)
            if len(calls) == 1:
                return FakeResponse(429, headers={"Retry-After": "0"})
            return FakeResponse(200, {"ok": True, "ts": "1690000002.000300", "channel": "C_ALERTS"})

        monkeypatch.setattr(slack_client_module.httpx, "post", fake_post)

        client = SlackClient(mode="live", bot_token="xoxb-test", channel_alerts="C_ALERTS")
        incident = _incident()
        ts = client.post_incident(incident, _blocks(), "text")

        assert len(calls) == 2
        assert ts == "1690000002.000300"

    def test_exhausting_retries_raises(self, monkeypatch):
        def always_429(url, json, headers, timeout):
            return FakeResponse(429, headers={"Retry-After": "0"})

        monkeypatch.setattr(slack_client_module.httpx, "post", always_429)

        client = SlackClient(mode="live", bot_token="xoxb-test", channel_alerts="C_ALERTS")
        incident = _incident()

        with pytest.raises(SlackAPIError):
            client.post_incident(incident, _blocks(), "text")


class TestTokenRedaction:
    def test_network_error_message_redacts_token(self, monkeypatch):
        token = "xoxb-super-secret-token"

        def raise_with_token(url, json, headers, timeout):
            raise slack_client_module.httpx.RequestError(
                f"connection failed for Authorization Bearer {token}"
            )

        monkeypatch.setattr(slack_client_module.httpx, "post", raise_with_token)

        client = SlackClient(mode="live", bot_token=token, channel_alerts="C_ALERTS")
        incident = _incident()

        with pytest.raises(SlackAPIError) as exc_info:
            client.post_incident(incident, _blocks(), "text")

        assert token not in str(exc_info.value)
        assert "REDACTED" in str(exc_info.value)

    def test_api_error_message_redacts_token(self, monkeypatch):
        token = "xoxb-super-secret-token"
        monkeypatch.setattr(
            slack_client_module.httpx,
            "post",
            lambda *a, **k: FakeResponse(200, {"ok": False, "error": f"invalid_auth {token}"}),
        )

        client = SlackClient(mode="live", bot_token=token, channel_alerts="C_ALERTS")
        incident = _incident()

        with pytest.raises(SlackAPIError) as exc_info:
            client.post_incident(incident, _blocks(), "text")

        assert token not in str(exc_info.value)


class TestMirrorP1:
    def test_non_p1_incident_is_not_mirrored(self, stub_dir):
        client = SlackClient(mode="stub", channel_p1="C_P1")
        incident = _incident()  # P2 in this fixture
        assert client.mirror_p1(incident, _blocks(), "text") is None

    def test_p1_incident_is_mirrored_with_here(self, monkeypatch):
        captured = {}

        def fake_post(url, json, headers, timeout):
            captured["payload"] = json
            return FakeResponse(200, {"ok": True, "ts": "1690000003.000400"})

        monkeypatch.setattr(slack_client_module.httpx, "post", fake_post)

        client = SlackClient(mode="live", bot_token="xoxb-test", channel_p1="C_P1")
        incident = _incident()
        incident.severity = "P1"

        ts = client.mirror_p1(incident, _blocks(), "urgent text")

        assert ts == "1690000003.000400"
        assert captured["payload"]["channel"] == "C_P1"
        assert captured["payload"]["text"].startswith("<!here>")


class TestPostChangeLog:
    def test_posts_to_changes_channel(self, monkeypatch):
        captured = {}

        def fake_post(url, json, headers, timeout):
            captured["payload"] = json
            return FakeResponse(200, {"ok": True, "ts": "1690000004.000500"})

        monkeypatch.setattr(slack_client_module.httpx, "post", fake_post)

        client = SlackClient(mode="live", bot_token="xoxb-test", channel_changes="C_CHANGES")
        incident = _incident()

        ts = client.post_change_log(incident, "approved", "U123")

        assert ts == "1690000004.000500"
        assert captured["payload"]["channel"] == "C_CHANGES"
        assert "approved" in captured["payload"]["text"]
        assert "U123" in captured["payload"]["text"]


class TestGetThreadRepliesAndReactions:
    def test_no_slack_ts_returns_empty_without_calling_out(self, monkeypatch):
        def fail_if_called(*args, **kwargs):
            raise AssertionError("must not call out when the incident was never posted")

        monkeypatch.setattr(slack_client_module.httpx, "post", fail_if_called)

        client = SlackClient(mode="live", bot_token="xoxb-test")
        incident = _incident()  # slack_channel/slack_ts left as None

        assert client.get_thread_replies(incident) == []
        assert client.get_reactions(incident) == []

    def test_stub_mode_returns_empty_without_calling_out(self, monkeypatch):
        def fail_if_called(*args, **kwargs):
            raise AssertionError("stub mode must not make a network call")

        monkeypatch.setattr(slack_client_module.httpx, "post", fail_if_called)

        client = SlackClient(mode="stub")
        incident = _incident()
        incident.slack_channel = "C_ALERTS"
        incident.slack_ts = "1690000000.000100"

        assert client.get_thread_replies(incident) == []
        assert client.get_reactions(incident) == []

    def test_get_thread_replies_returns_messages(self, monkeypatch):
        captured = {}

        def fake_post(url, json, headers, timeout):
            captured["payload"] = json
            return FakeResponse(200, {"ok": True, "messages": [{"ts": "1", "user": "U1"}]})

        monkeypatch.setattr(slack_client_module.httpx, "post", fake_post)

        client = SlackClient(mode="live", bot_token="xoxb-test")
        incident = _incident()
        incident.slack_channel = "C_ALERTS"
        incident.slack_ts = "1690000000.000100"

        messages = client.get_thread_replies(incident)

        assert messages == [{"ts": "1", "user": "U1"}]
        assert captured["payload"]["channel"] == "C_ALERTS"
        assert captured["payload"]["ts"] == "1690000000.000100"

    def test_get_reactions_returns_reaction_list(self, monkeypatch):
        def fake_post(url, json, headers, timeout):
            return FakeResponse(
                200, {"ok": True, "message": {"reactions": [{"name": "eyes", "users": ["U1"]}]}}
            )

        monkeypatch.setattr(slack_client_module.httpx, "post", fake_post)

        client = SlackClient(mode="live", bot_token="xoxb-test")
        incident = _incident()
        incident.slack_channel = "C_ALERTS"
        incident.slack_ts = "1690000000.000100"

        reactions = client.get_reactions(incident)

        assert reactions == [{"name": "eyes", "users": ["U1"]}]
