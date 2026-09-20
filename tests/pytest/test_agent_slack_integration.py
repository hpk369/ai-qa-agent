"""
Tests for agent.agent's Slack integration: notify_slack() posting a
newly opened incident, and the /slack/action interactivity endpoint
(signature verification, immediate 200 ack, dispatch to a background
task). No live Slack anywhere here — SLACK_MODE=stub throughout.
"""

import hashlib
import hmac
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import pytest
from fastapi.testclient import TestClient

from agent import agent as agent_module
from agent import evidence as evidence_module
from agent import incident as incident_module
from agent import slack_client as slack_client_module
from agent.agent import app, build_response
from agent.incident import open_incident, persist
from agent.severity import SeverityResult, load_config

SIGNING_SECRET = "test-signing-secret"


@pytest.fixture(autouse=True)
def incidents_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(incident_module, "INCIDENTS_DIR", tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def evidence_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(evidence_module, "EVIDENCE_DIR", tmp_path / "evidence")


@pytest.fixture(autouse=True)
def stub_dir(tmp_path, monkeypatch):
    slack_dir = tmp_path / "slack"
    monkeypatch.setattr(slack_client_module, "STUB_DIR", slack_dir)
    return slack_dir


@pytest.fixture(autouse=True)
def slack_stub_mode(monkeypatch):
    monkeypatch.setenv("SLACK_MODE", "stub")
    monkeypatch.setenv("SLACK_CHANNEL_ALERTS", "C_ALERTS")
    monkeypatch.setenv("SLACK_CHANNEL_P1", "C_P1")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", SIGNING_SECRET)


@pytest.fixture(scope="module")
def config():
    return load_config()


def _p2_response(config) -> dict:
    agent_output = {
        "detected_by": "sql_validator",
        "signals": {"row_variance_pct": 40.0},
        "affected_job": "cx_customer_load",
        "affected_objects": ["tgt.transactions"],
        "impact_summary": "Target table missing ~40% of records.",
        "root_cause": "Dedup step dropped rows.",
        "recommended_action": "Inspect the dedup step.",
        "confidence": 0.9,
    }
    return build_response({"run_id": "run-1"}, agent_output, {"sql_validator"}, 10, config)


class TestNotifySlack:
    def test_clean_run_does_not_post(self, config, stub_dir):
        clean_response = build_response(
            {"run_id": "run-0"}, {"signals": {}, "impact_summary": "clean"}, {"sql_validator"}, 5, config
        )
        agent_module.notify_slack(clean_response)
        assert not stub_dir.exists() or not list(stub_dir.glob("*.json"))

    def test_incident_response_posts_and_writes_back_ts(self, config, stub_dir):
        response = _p2_response(config)
        agent_module.notify_slack(response)

        assert response["incident"]["slack_ts"] is not None
        assert response["incident"]["slack_channel"] == "C_ALERTS"

        incident_id = response["incident"]["incident_id"]
        files = list(stub_dir.glob(f"{incident_id}-*.json"))
        assert files, "expected a stub Slack payload to have been written"

    def test_p1_incident_is_also_mirrored(self, config, stub_dir):
        agent_output = {
            "detected_by": "schema_comparator",
            "signals": {"job_failed_no_path_to_sla": True},
            "impact_summary": "Required column missing.",
            "confidence": 0.9,
        }
        response = build_response({"run_id": "run-2"}, agent_output, {"schema_comparator"}, 5, config)
        assert response["incident"]["severity"] == "P1"

        agent_module.notify_slack(response)

        incident_id = response["incident"]["incident_id"]
        files = list(stub_dir.glob(f"{incident_id}-*.json"))
        # One postMessage for the parent, one for the #etl-prod-p1 mirror.
        assert len(files) == 2

    def test_slack_failure_does_not_raise(self, config, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("Slack is down")

        monkeypatch.setattr(slack_client_module.SlackClient, "post_incident", boom)
        response = _p2_response(config)

        agent_module.notify_slack(response)  # must not raise


def _sign(timestamp: int, body: bytes, secret: str = SIGNING_SECRET) -> str:
    base_string = f"v0:{timestamp}:".encode("utf-8") + body
    digest = hmac.new(secret.encode("utf-8"), base_string, hashlib.sha256).hexdigest()
    return f"v0={digest}"


def _action_body(incident_id: str, action_id: str = "incident_approve", user_id: str = "U123") -> bytes:
    payload = {
        "type": "block_actions",
        "user": {"id": user_id},
        "actions": [{"action_id": action_id, "value": incident_id}],
    }
    return ("payload=" + urllib_quote(json.dumps(payload))).encode("utf-8")


def urllib_quote(s: str) -> str:
    import urllib.parse

    return urllib.parse.quote(s)


class TestSlackActionEndpoint:
    def test_missing_signature_returns_401(self):
        client = TestClient(app)
        resp = client.post("/slack/action", content=_action_body("INC-x"), headers={})
        assert resp.status_code == 401

    def test_invalid_signature_returns_401(self):
        client = TestClient(app)
        body = _action_body("INC-x")
        headers = {
            "X-Slack-Request-Timestamp": str(int(time.time())),
            "X-Slack-Signature": "v0=not-a-real-signature",
        }
        resp = client.post("/slack/action", content=body, headers=headers)
        assert resp.status_code == 401

    def test_valid_signature_acknowledges_and_dispatches(self, monkeypatch, incidents_dir):
        # A real incident so the background task has something to load.
        result = SeverityResult(
            severity="P2",
            matched_conditions=["row_variance_pct >= 5.0 (actual: 7.3)"],
            rationale="row_variance_pct >= 5.0 (actual: 7.3)",
            response_expectation="Notify on-call",
        )
        incident = open_incident(
            {},
            result,
            {
                "detected_by": "sql_validator",
                "affected_job": "job",
                "impact_summary": "impact",
                "confidence": 0.9,
                "requires_approval": True,
            },
        )
        persist(incident)

        processed = []
        monkeypatch.setattr(
            agent_module, "process_slack_action", lambda payload: processed.append(payload)
        )

        body = _action_body(incident.incident_id)
        timestamp = int(time.time())
        headers = {
            "X-Slack-Request-Timestamp": str(timestamp),
            "X-Slack-Signature": _sign(timestamp, body),
            "Content-Type": "application/x-www-form-urlencoded",
        }

        client = TestClient(app)
        resp = client.post("/slack/action", content=body, headers=headers)

        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        assert len(processed) == 1
        assert processed[0]["actions"][0]["value"] == incident.incident_id


class TestProcessSlackAction:
    def test_unknown_incident_does_not_raise(self):
        agent_module.process_slack_action(
            {"user": {"id": "U1"}, "actions": [{"action_id": "incident_approve", "value": "INC-does-not-exist"}]}
        )  # must not raise

    def test_unrecognised_payload_shape_does_not_raise(self):
        agent_module.process_slack_action({"type": "something_else"})  # must not raise

    def test_records_decision_on_real_incident(self, incidents_dir):
        result = SeverityResult(
            severity="P2",
            matched_conditions=["row_variance_pct >= 5.0 (actual: 7.3)"],
            rationale="row_variance_pct >= 5.0 (actual: 7.3)",
            response_expectation="Notify on-call",
        )
        incident = open_incident(
            {}, result, {"detected_by": "sql_validator", "impact_summary": "impact", "confidence": 0.9}
        )
        persist(incident)

        agent_module.process_slack_action(
            {"user": {"id": "U123"}, "actions": [{"action_id": "incident_approve", "value": incident.incident_id}]}
        )

        reloaded = json.loads((incidents_dir / f"{incident.incident_id}.json").read_text())
        assert any(e["event"] == "approval_decision" for e in reloaded["timeline"])
