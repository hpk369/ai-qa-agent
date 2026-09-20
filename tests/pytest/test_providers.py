"""
Tests for agent.providers — which model provider gets used, and the wire
format each one speaks.

The OpenAI-compatible adapter is tested against a real HTTP server
running in-process: these assert the actual bytes sent and the handling
of what comes back, including the things small local models really do
(wrap JSON in a code fence, chat around it, reject json_schema mode).
No external service is contacted.
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import pytest

from agent import providers
from agent.llm import ResolutionJudgement
from agent.providers import (
    AnthropicProvider,
    OpenAICompatibleProvider,
    ProviderError,
)

VALID = {"resolved": True, "confidence": 0.9, "reason": "the consumer was restarted"}


class FakeEndpoint:
    """A stand-in for Ollama / Groq / llama.cpp — whatever the test needs."""

    def __init__(self):
        self.requests: list[dict] = []
        self.headers: list[dict] = []
        self.responses: list = []          # str content, or an int status to fail with
        self.server = None
        self.thread = None

    def start(self):
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                endpoint.requests.append(body)
                endpoint.headers.append(dict(self.headers))

                reply = (endpoint.responses.pop(0) if endpoint.responses
                         else json.dumps(VALID))
                if isinstance(reply, int):
                    self.send_response(reply)
                    self.end_headers()
                    self.wfile.write(b'{"error": "unsupported response_format"}')
                    return

                payload = {"choices": [{"message": {"role": "assistant", "content": reply}}]}
                encoded = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()


@pytest.fixture
def endpoint():
    fake = FakeEndpoint()
    fake.base_url = fake.start()
    yield fake
    fake.stop()


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "LLM_BASE_URL",
                 "LLM_MODEL", "LLM_API_KEY", "LLM_MODE", "LLM_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(providers, "_ollama_running", lambda host=None: False)


def provider_for(endpoint, **kwargs):
    return OpenAICompatibleProvider(base_url=endpoint.base_url, model="llama3.2", **kwargs)


# ---------- The OpenAI-compatible wire format ----------

def test_it_asks_for_a_json_schema_first(endpoint):
    result = provider_for(endpoint).complete_json("sys", "prompt", ResolutionJudgement, "low")

    assert isinstance(result, ResolutionJudgement)
    assert result.resolved is True
    assert len(endpoint.requests) == 1

    sent = endpoint.requests[0]
    assert sent["model"] == "llama3.2"
    assert sent["response_format"]["type"] == "json_schema"
    assert sent["response_format"]["json_schema"]["schema"]["properties"].keys() >= {
        "resolved", "confidence", "reason"}
    assert sent["temperature"] == 0
    assert [m["role"] for m in sent["messages"]] == ["system", "user"]


def test_it_falls_back_to_plain_json_mode_when_schemas_are_unsupported(endpoint):
    """Most locally-hosted models reject json_schema. That must not be the
    end of the call."""
    endpoint.responses = [400, json.dumps(VALID)]

    result = provider_for(endpoint).complete_json("sys", "prompt", ResolutionJudgement, "low")

    assert result.resolved is True
    assert len(endpoint.requests) == 2
    second = endpoint.requests[1]
    assert second["response_format"] == {"type": "json_object"}
    # The schema has to reach the model some other way, so it goes in the prompt.
    assert "confidence" in second["messages"][0]["content"]
    assert "JSON Schema" in second["messages"][0]["content"]


@pytest.mark.parametrize("content", [
    "```json\n" + json.dumps(VALID) + "\n```",
    "```\n" + json.dumps(VALID) + "\n```",
    "Sure! Here is the result:\n" + json.dumps(VALID) + "\nHope that helps.",
])
def test_it_recovers_json_from_what_small_models_actually_return(endpoint, content):
    endpoint.responses = [content]
    result = provider_for(endpoint).complete_json("sys", "prompt", ResolutionJudgement, "low")
    assert result.resolved is True
    assert result.reason == "the consumer was restarted"


def test_a_response_that_does_not_match_the_schema_is_an_error(endpoint):
    """Better no answer than a half-parsed one — the caller falls back."""
    endpoint.responses = ['{"resolved": "maybe"}', '{"resolved": "maybe"}']
    with pytest.raises(ProviderError, match="did not match the schema"):
        provider_for(endpoint).complete_json("sys", "prompt", ResolutionJudgement, "low")


def test_unparseable_output_is_an_error(endpoint):
    endpoint.responses = ["I'm not going to answer that", "still not answering"]
    with pytest.raises(ProviderError):
        provider_for(endpoint).complete_json("sys", "prompt", ResolutionJudgement, "low")


def test_the_api_key_is_sent_as_a_bearer_token(endpoint):
    provider_for(endpoint, api_key="gsk-test-key").complete_json(
        "sys", "prompt", ResolutionJudgement, "low")
    assert endpoint.headers[0]["Authorization"] == "Bearer gsk-test-key"


def test_no_auth_header_when_no_key_is_needed(endpoint):
    """A local Ollama wants no credentials."""
    provider_for(endpoint, api_key="").complete_json("sys", "prompt", ResolutionJudgement, "low")
    assert "Authorization" not in endpoint.headers[0]


def test_effort_becomes_a_token_budget(endpoint):
    provider_for(endpoint).complete_json("sys", "prompt", ResolutionJudgement, "low")
    provider_for(endpoint).complete_json("sys", "prompt", ResolutionJudgement, "medium")
    assert endpoint.requests[0]["max_tokens"] < endpoint.requests[1]["max_tokens"]


def test_an_unreachable_endpoint_names_itself(endpoint):
    endpoint.stop()
    with pytest.raises(ProviderError, match="could not reach"):
        provider_for(endpoint).complete_json("sys", "prompt", ResolutionJudgement, "low")


def test_missing_configuration_is_refused_up_front():
    with pytest.raises(ProviderError, match="LLM_BASE_URL"):
        OpenAICompatibleProvider(base_url="", model="x")
    with pytest.raises(ProviderError, match="LLM_MODEL"):
        OpenAICompatibleProvider(base_url="http://localhost:1234/v1", model="")


# ---------- The Anthropic adapter ----------

class FakeAnthropicResponse:
    def __init__(self, parsed, stop_reason="end_turn"):
        self.parsed_output = parsed
        self.stop_reason = stop_reason
        self.stop_details = {"category": "cyber"}


def _fake_anthropic(monkeypatch, provider, captured, fail_on_effort=False):
    calls = []

    class FakeMessages:
        def parse(self, **kwargs):
            calls.append(dict(kwargs))
            captured.clear()
            captured.update(kwargs)
            if fail_on_effort and "output_config" in kwargs:
                raise TypeError(
                    "Error code: 400 - output_config.effort: Extra inputs are not permitted")
            return FakeAnthropicResponse(
                ResolutionJudgement(resolved=False, confidence=0.1, reason="no"))

    class FakeClient:
        messages = FakeMessages()

    monkeypatch.setattr(provider, "_client", lambda: FakeClient())
    return calls


def test_the_anthropic_adapter_sends_the_documented_request(monkeypatch):
    captured = {}
    provider = AnthropicProvider(model="claude-opus-5")
    _fake_anthropic(monkeypatch, provider, captured)
    provider.complete_json("sys", "prompt", ResolutionJudgement, "low")

    assert captured["model"] == "claude-opus-5"
    assert captured["output_format"] is ResolutionJudgement
    assert captured["output_config"] == {"effort": "low"}
    assert captured["system"] == "sys"
    assert captured["messages"] == [{"role": "user", "content": "prompt"}]


def test_haiku_is_the_default_model():
    """The cheapest current model, because this workload is small."""
    assert AnthropicProvider().model == "claude-haiku-4-5"


@pytest.mark.parametrize("model,expected", [
    ("claude-haiku-4-5", False),
    ("claude-sonnet-4-5", False),
    ("claude-opus-5", True),
    ("claude-sonnet-5", True),
])
def test_effort_is_only_sent_to_models_that_accept_it(monkeypatch, model, expected):
    """Haiku 4.5 and Sonnet 4.5 reject output_config.effort with a 400 —
    sending it would fail every call on the default model."""
    captured = {}
    provider = AnthropicProvider(model=model)
    _fake_anthropic(monkeypatch, provider, captured)
    provider.complete_json("sys", "prompt", ResolutionJudgement, "low")
    assert ("output_config" in captured) is expected


def test_a_rejected_effort_parameter_is_retried_without_it(monkeypatch):
    """Model naming drifts; a 400 about effort must not lose the call."""
    captured = {}
    provider = AnthropicProvider(model="claude-opus-5")
    calls = _fake_anthropic(monkeypatch, provider, captured, fail_on_effort=True)

    result = provider.complete_json("sys", "prompt", ResolutionJudgement, "low")

    assert result.resolved is False
    assert len(calls) == 2
    assert "output_config" in calls[0]
    assert "output_config" not in calls[1]


def test_other_api_errors_are_not_retried(monkeypatch):
    class FakeMessages:
        def parse(self, **kwargs):
            raise RuntimeError("Error code: 429 - rate limited")

    class FakeClient:
        messages = FakeMessages()

    provider = AnthropicProvider(model="claude-haiku-4-5")
    monkeypatch.setattr(provider, "_client", lambda: FakeClient())
    with pytest.raises(ProviderError, match="429"):
        provider.complete_json("sys", "prompt", ResolutionJudgement, "low")


def test_a_refusal_is_an_error_not_an_answer(monkeypatch):
    class FakeMessages:
        def parse(self, **kwargs):
            return FakeAnthropicResponse(
                ResolutionJudgement(resolved=True, confidence=1.0, reason="x"),
                stop_reason="refusal")

    class FakeClient:
        messages = FakeMessages()

    provider = AnthropicProvider()
    monkeypatch.setattr(provider, "_client", lambda: FakeClient())
    with pytest.raises(ProviderError, match="declined"):
        provider.complete_json("sys", "prompt", ResolutionJudgement, "low")


# ---------- Which provider gets picked ----------

def test_nothing_configured_means_no_provider():
    assert providers.resolve() is None
    assert "deterministic" in providers.describe()


def test_a_configured_endpoint_wins(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("LLM_MODEL", "qwen2.5:7b")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    provider = providers.resolve()
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.model == "qwen2.5:7b"


def test_anthropic_is_used_when_only_a_key_is_set(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(AnthropicProvider, "configured", staticmethod(lambda: True))
    assert isinstance(providers.resolve(), AnthropicProvider)


def test_a_running_ollama_is_found_with_nothing_else_configured(monkeypatch):
    monkeypatch.setattr(providers, "_ollama_running", lambda host=None: True)
    provider = providers.resolve()
    assert provider is not None
    assert provider.name == "ollama"
    assert provider.base_url.endswith("/v1")


def test_provider_can_be_pinned(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("LLM_MODEL", "qwen2.5:7b")
    monkeypatch.setenv("LLM_PROVIDER", "off")
    assert providers.resolve() is None

    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    assert providers.resolve().name == "ollama"


def test_the_older_off_switch_still_works(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("LLM_MODEL", "qwen2.5:7b")
    monkeypatch.setenv("LLM_MODE", "off")
    assert providers.resolve() is None
