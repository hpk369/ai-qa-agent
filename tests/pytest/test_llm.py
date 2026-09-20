"""
Tests for agent.llm — the Claude layer.

No network: the single seam every call goes through (``_parse``) is
monkeypatched, so these cover what the code does with a model's answer,
not the model itself. The fallbacks are tested for real, because they are
what runs whenever there is no API key — including in this suite.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import pytest

from agent import llm
from agent.llm import Narration, ResolutionJudgement


@pytest.fixture(autouse=True)
def no_real_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(llm, "LLM_MODE", "auto")


FINDINGS = [{
    "signature_id": "SIG-006-DISK-FULL", "title": "Disk full on a data volume",
    "file": "hdfs-datanode.log", "line": 412,
    "line_text": "ERROR java.io.IOException: No space left on device while writing to /data/1/dfs/dn",
    "signals": {"target_unavailable": True},
}]


# ---------- availability ----------

def test_not_available_without_credentials():
    assert llm.available() is False


def test_not_available_when_switched_off(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(llm, "LLM_MODE", "off")
    assert llm.available() is False


def test_available_with_a_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert llm.available() is True


# ---------- narration ----------

def test_narration_falls_back_to_the_deterministic_summary():
    result = llm.narrate("P1", {"target_unavailable": True}, FINDINGS,
                         ["line one", "line two"], "deterministic summary")
    assert result.source == "fallback"
    assert result.value.impact_summary == "deterministic summary"
    assert "SIG-006-DISK-FULL" in result.value.root_cause
    assert result.value.recommended_action


def test_narration_uses_claudes_text_when_available(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    captured = {}

    def fake_parse(system, prompt, schema, effort):
        captured.update(system=system, prompt=prompt, schema=schema, effort=effort)
        return Narration(impact_summary="Settlement feed is stalled for the 09:00 cycle.",
                         root_cause="A datanode volume is full.",
                         recommended_action="Free space on /data/1/dfs/dn.")

    monkeypatch.setattr(llm, "_parse", fake_parse)
    result = llm.narrate("P1", {"target_unavailable": True}, FINDINGS,
                         ["ERROR No space left on device"], "deterministic summary")

    assert result.source == "claude"
    assert result.value.impact_summary.startswith("Settlement feed")
    # The model is told the severity, and told not to revisit it.
    assert "P1" in captured["prompt"]
    assert "never argue with it" in captured["system"]
    # It sees the matched signature and the surrounding lines, nothing else.
    assert "SIG-006-DISK-FULL" in captured["prompt"]
    assert captured["schema"] is Narration


def test_narration_falls_back_when_the_call_fails(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    def boom(*args, **kwargs):
        raise RuntimeError("rate limited")

    monkeypatch.setattr(llm, "_parse", boom)
    result = llm.narrate("P2", {}, FINDINGS, [], "deterministic summary")
    assert result.source == "fallback"
    assert result.value.impact_summary == "deterministic summary"
    assert "rate limited" in result.detail


def test_failure_detail_never_carries_the_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-supersecret")

    def boom(*args, **kwargs):
        raise RuntimeError("401 unauthorized for key sk-ant-supersecret")

    monkeypatch.setattr(llm, "_parse", boom)
    result = llm.narrate("P2", {}, FINDINGS, [], "summary")
    assert "sk-ant-supersecret" not in result.detail
    assert "***REDACTED***" in result.detail


# ---------- resolution judgement ----------

@pytest.mark.parametrize("text", [
    "looking into it",
    "paging the DBA",
    "I'll fix it after standup",
    "should be fine once the next run goes through",
    "any idea what this is?",
])
def test_fallback_judge_keeps_the_gate_shut_on_acknowledgements(text):
    result = llm.judge_resolution("INC-1 P1", [{"user": "U1", "text": text}])
    assert result.source == "fallback"
    assert result.value.resolved is False
    assert result.value.confidence == 0.0


@pytest.mark.parametrize("text", [
    "cleared the volume, writes are fine now — all clear",
    "consumer restarted, issue is fixed",
    "namenode recovered, job completed",
    "resolved — the column was renamed in a release",
])
def test_fallback_judge_releases_on_an_outright_confirmation(text):
    result = llm.judge_resolution("INC-1 P1", [{"user": "U1", "text": text}])
    assert result.value.resolved is True
    assert result.value.confidence >= 0.6
    assert "keyword match" in result.value.reason


def test_judge_uses_claude_when_available(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    captured = {}

    def fake_parse(system, prompt, schema, effort):
        captured.update(prompt=prompt, schema=schema)
        return ResolutionJudgement(resolved=True, confidence=0.9,
                                   reason='"lag is draining" states recovery')

    monkeypatch.setattr(llm, "_parse", fake_parse)
    result = llm.judge_resolution("INC-1 P2 consumer lag", [
        {"user": "U1", "text": "looking"},
        {"user": "U1", "text": "restarted it, lag is draining"},
    ])

    assert result.source == "claude"
    assert result.value.resolved is True
    # The whole thread goes in, not just the newest line — context decides this.
    assert "looking" in captured["prompt"] and "lag is draining" in captured["prompt"]
    assert captured["schema"] is ResolutionJudgement


def test_judge_fails_closed_when_the_call_fails(monkeypatch):
    """An unreachable model must never resume a blocked pipeline."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    def boom(*args, **kwargs):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(llm, "_parse", boom)
    result = llm.judge_resolution("INC-1 P1", [{"user": "U1", "text": "all clear"}])
    assert result.value.resolved is False
    assert result.value.confidence == 0.0
    assert "staying paused" in result.value.reason


def test_a_refusal_is_treated_as_a_failure(monkeypatch):
    """_parse raises on a refusal, so the caller takes the fallback."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    class FakeResponse:
        stop_reason = "refusal"
        stop_details = {"category": "cyber"}
        parsed_output = Narration(impact_summary="x", root_cause="y", recommended_action="z")

    class FakeMessages:
        def parse(self, **kwargs):
            return FakeResponse()

    class FakeClient:
        messages = FakeMessages()

    monkeypatch.setattr(llm, "_client", lambda: FakeClient())
    result = llm.narrate("P1", {}, FINDINGS, [], "deterministic summary")
    assert result.source == "fallback"
    assert "declined" in result.detail


def test_parse_sends_the_documented_request_shape(monkeypatch):
    """Model id, structured output format, and effort all reach the SDK."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    captured = {}

    class FakeMessages:
        def parse(self, **kwargs):
            captured.update(kwargs)

            class Response:
                stop_reason = "end_turn"
                parsed_output = ResolutionJudgement(resolved=False, confidence=0.1, reason="no")
            return Response()

    class FakeClient:
        messages = FakeMessages()

    monkeypatch.setattr(llm, "_client", lambda: FakeClient())
    llm.judge_resolution("INC-1", [{"user": "U1", "text": "hmm"}])

    assert captured["model"] == llm.MODEL
    assert captured["output_format"] is ResolutionJudgement
    assert captured["output_config"] == {"effort": "low"}
    assert captured["messages"][0]["role"] == "user"
    assert captured["max_tokens"] == llm.MAX_TOKENS
