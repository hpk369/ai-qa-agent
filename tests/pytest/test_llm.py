"""
Tests for agent.llm — the model layer.

No network: the single seam every call goes through (``_parse``) is
monkeypatched, so these cover what the code does with a model's answer,
not the model itself, and not which provider served it. The fallbacks
are tested for real, because they are what runs whenever no provider is
configured — including in this suite.

agent/providers.py (which provider gets picked, and the wire format) is
tested separately in test_providers.py.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import pytest

from agent import llm
from agent.llm import Narration, ResolutionJudgement


@pytest.fixture(autouse=True)
def no_provider(monkeypatch):
    """A clone with nothing configured — the default everywhere."""
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "LLM_BASE_URL",
                 "LLM_MODEL", "LLM_API_KEY", "LLM_MODE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "auto")
    monkeypatch.setattr(llm.providers, "_ollama_running", lambda host=None: False)
    llm.providers.reset_cache()


@pytest.fixture
def local_model(monkeypatch):
    """A configured OpenAI-compatible endpoint — an Ollama, say."""
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("LLM_MODEL", "llama3.2")


FINDINGS = [{
    "signature_id": "SIG-006-DISK-FULL", "title": "Disk full on a data volume",
    "file": "hdfs-datanode.log", "line": 412,
    "line_text": "ERROR java.io.IOException: No space left on device while writing to /data/1/dfs/dn",
    "signals": {"target_unavailable": True},
}]


# ---------- availability ----------

def test_not_available_without_a_provider():
    assert llm.available() is False
    assert "deterministic" in llm.describe()


def test_not_available_when_switched_off(monkeypatch, local_model):
    monkeypatch.setenv("LLM_PROVIDER", "off")
    assert llm.available() is False


def test_available_with_a_local_model(local_model):
    assert llm.available() is True
    assert "llama3.2" in llm.describe()


# ---------- narration ----------

def test_narration_falls_back_to_the_deterministic_summary():
    result = llm.narrate("P1", {"target_unavailable": True}, FINDINGS,
                         ["line one", "line two"], "deterministic summary")
    assert result.source == "fallback"
    assert result.value.impact_summary == "deterministic summary"
    assert "SIG-006-DISK-FULL" in result.value.root_cause
    assert result.value.recommended_action


def test_narration_uses_the_models_text_when_available(monkeypatch, local_model):
    captured = {}

    def fake_parse(system, prompt, schema, effort):
        captured.update(system=system, prompt=prompt, schema=schema, effort=effort)
        return Narration(impact_summary="Settlement feed is stalled for the 09:00 cycle.",
                         root_cause="A datanode volume is full.",
                         recommended_action="Free space on /data/1/dfs/dn.")

    monkeypatch.setattr(llm, "_parse", fake_parse)
    result = llm.narrate("P1", {"target_unavailable": True}, FINDINGS,
                         ["ERROR No space left on device"], "deterministic summary")

    assert result.source == "openai-compatible"   # whichever provider served it
    assert result.from_model
    assert result.value.impact_summary.startswith("Settlement feed")
    # The model is told the severity, and told not to revisit it.
    assert "P1" in captured["prompt"]
    assert "never argue with it" in captured["system"]
    # It sees the matched signature and the surrounding lines, nothing else.
    assert "SIG-006-DISK-FULL" in captured["prompt"]
    assert captured["schema"] is Narration


def test_narration_falls_back_when_the_call_fails(monkeypatch, local_model, capsys):
    def boom(*args, **kwargs):
        raise RuntimeError("rate limited")

    monkeypatch.setattr(llm, "_parse", boom)
    result = llm.narrate("P2", {}, FINDINGS, [], "deterministic summary")
    assert result.source == "fallback"
    assert result.value.impact_summary == "deterministic summary"
    assert "rate limited" in result.detail
    # ...and says why. A silent degrade reads as "the model seems quiet"
    # rather than "the credential is broken".
    assert "rate limited" in capsys.readouterr().out


def test_a_missing_provider_does_not_warn(local_model, monkeypatch, capsys):
    """Only a *failure* is noisy; an unconfigured provider is a choice the
    banner already reports."""
    for name in ("LLM_BASE_URL", "LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    llm.narrate("P2", {}, FINDINGS, [], "deterministic summary")
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("variable", ["ANTHROPIC_API_KEY", "LLM_API_KEY"])
def test_failure_detail_never_carries_an_api_key(monkeypatch, local_model, variable):
    monkeypatch.setenv(variable, "sk-supersecret")

    def boom(*args, **kwargs):
        raise RuntimeError("401 unauthorized for key sk-supersecret")

    monkeypatch.setattr(llm, "_parse", boom)
    result = llm.narrate("P2", {}, FINDINGS, [], "summary")
    assert "sk-supersecret" not in result.detail
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


def test_judge_uses_the_model_when_available(monkeypatch, local_model):
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

    assert result.from_model
    assert result.value.resolved is True
    # The whole thread goes in, not just the newest line — context decides this.
    assert "looking" in captured["prompt"] and "lag is draining" in captured["prompt"]
    assert captured["schema"] is ResolutionJudgement


def test_judge_fails_closed_when_the_call_fails(monkeypatch, local_model, capsys):
    """An unreachable model must never resume a blocked pipeline."""
    def boom(*args, **kwargs):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(llm, "_parse", boom)
    result = llm.judge_resolution("INC-1 P1", [{"user": "U1", "text": "all clear"}])
    assert result.value.resolved is False
    assert result.value.confidence == 0.0
    assert "staying paused" in result.value.reason
    assert "connection reset" in capsys.readouterr().out
