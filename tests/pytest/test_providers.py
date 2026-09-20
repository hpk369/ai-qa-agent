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
from pathlib import Path
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


WIF_ENV = {
    "ANTHROPIC_FEDERATION_RULE_ID": "fdrl_test",
    "ANTHROPIC_ORGANIZATION_ID": "00000000-0000-0000-0000-000000000000",
    "ANTHROPIC_SERVICE_ACCOUNT_ID": "svac_test",
    "ANTHROPIC_IDENTITY_TOKEN_FILE": "/var/run/secrets/anthropic.com/token",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE",
                 "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY", "LLM_MODE", "LLM_PROVIDER",
                 *WIF_ENV):
        monkeypatch.delenv(name, raising=False)
    # An empty config dir, so a developer's real ~/.config/anthropic profile
    # never decides the result of a test.
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path / "anthropic"))
    monkeypatch.setattr(providers, "_ollama_running", lambda host=None: False)
    providers.reset_cache()
    yield
    providers.reset_cache()


@pytest.fixture
def federated(monkeypatch):
    for name, value in WIF_ENV.items():
        monkeypatch.setenv(name, value)


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


# ---------- Credentials: federation, profiles, keys ----------

def test_federation_needs_every_variable(monkeypatch, federated):
    assert AnthropicProvider.federation_configured() is True
    for name in WIF_ENV:
        monkeypatch.delenv(name)
        assert AnthropicProvider.federation_configured() is False, f"{name} should be required"
        monkeypatch.setenv(name, WIF_ENV[name])


def test_either_token_variable_satisfies_federation(monkeypatch, federated):
    monkeypatch.delenv("ANTHROPIC_IDENTITY_TOKEN_FILE")
    assert AnthropicProvider.federation_configured() is False
    monkeypatch.setenv("ANTHROPIC_IDENTITY_TOKEN", "eyJhbGciOiJSUzI1NiIs...")
    assert AnthropicProvider.federation_configured() is True


def test_federation_alone_is_enough_to_select_claude(federated):
    """No API key anywhere, and Claude is still selected and usable."""
    assert AnthropicProvider.credential_source() == "workload identity federation"
    assert AnthropicProvider.configured() is True
    assert isinstance(providers.resolve(), AnthropicProvider)


def test_credential_precedence_matches_the_sdk(monkeypatch, federated, tmp_path):
    """Documented order: key > named profile > federation > profile on disk."""
    assert AnthropicProvider.credential_source() == "workload identity federation"

    monkeypatch.setenv("ANTHROPIC_PROFILE", "staging")
    assert AnthropicProvider.credential_source() == "profile staging"

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real")
    assert AnthropicProvider.credential_source() == "api key"


def test_a_profile_on_disk_is_found(monkeypatch, tmp_path):
    config_dir = tmp_path / "anthropic"
    (config_dir / "configs").mkdir(parents=True)
    (config_dir / "configs" / "default.json").write_text("{}")
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(config_dir))

    assert AnthropicProvider.credential_source() == "profile default"

    (config_dir / "active_config").write_text("production\n")
    assert AnthropicProvider.credential_source() == "profile production"


def test_no_credentials_means_no_anthropic_provider():
    assert AnthropicProvider.credential_source() == "none"
    assert AnthropicProvider.configured() is False


def _fake_sdk(monkeypatch):
    """Inject a stand-in `anthropic` module and record the constructor args."""
    import sys
    import types

    calls = []

    class FakeAnthropic:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    module = types.ModuleType("anthropic")
    module.Anthropic = FakeAnthropic
    monkeypatch.setitem(sys.modules, "anthropic", module)
    return calls


def test_the_client_is_constructed_with_no_arguments_under_federation(monkeypatch, federated):
    """The SDK performs the token exchange itself — passing anything would
    override it."""
    calls = _fake_sdk(monkeypatch)
    AnthropicProvider()._client()
    assert calls == [{}]


def test_a_blank_api_key_is_cleared_so_federation_is_not_shadowed(monkeypatch, federated):
    """An exported ANTHROPIC_API_KEY="" occupies its slot in the SDK's
    precedence chain and authenticates with an empty key — a blank
    placeholder in .env must not silently break federation."""
    _fake_sdk(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")

    AnthropicProvider()._client()

    assert "ANTHROPIC_API_KEY" not in os.environ


def test_a_real_api_key_is_never_cleared(monkeypatch, federated):
    _fake_sdk(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real")

    AnthropicProvider()._client()

    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-real"


def test_a_blank_key_is_left_alone_when_it_is_the_only_credential(monkeypatch):
    """Nothing to fall through to, so the SDK's own error is the clearest
    thing to report."""
    _fake_sdk(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")

    AnthropicProvider()._client()

    assert os.environ["ANTHROPIC_API_KEY"] == ""


def test_the_banner_names_the_credential_source(monkeypatch, federated):
    monkeypatch.setattr(AnthropicProvider, "configured", staticmethod(lambda: True))
    assert "workload identity federation" in providers.describe()
    assert "claude-haiku-4-5" in providers.describe()


# ---------- The Console snippet's shape: a JWT in a plain variable ----------

FED_IDS = {
    "ANTHROPIC_FEDERATION_RULE_ID": "fdrl_test",
    "ANTHROPIC_ORGANIZATION_ID": "00000000-0000-0000-0000-000000000000",
    "ANTHROPIC_SERVICE_ACCOUNT_ID": "svac_test",
}
FAKE_JWT = "eyJhbGciOiJSUzI1NiJ9.payload.signature"


@pytest.fixture
def jwt_in_env(monkeypatch):
    """What the Console's "Authenticate from your workload" snippet expects:
    the identity token in a plain variable, read by a callable."""
    for name, value in FED_IDS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("JWT", FAKE_JWT)


def test_a_jwt_in_a_plain_variable_counts_as_federation(jwt_in_env):
    assert AnthropicProvider.federation_configured() is True
    assert AnthropicProvider.credential_source() == "workload identity federation"


@pytest.fixture
def credential_spy(monkeypatch):
    """Record what we hand the SDK's WorkloadIdentityCredentials. Asserting
    on the object's own attributes would be asserting on SDK internals —
    they are private and free to change."""
    import anthropic

    recorded = {}

    class Spy:
        def __init__(self, **kwargs):
            recorded.clear()
            recorded.update(kwargs)

    monkeypatch.setattr(anthropic, "WorkloadIdentityCredentials", Spy)
    return recorded


def test_explicit_credentials_carry_the_federation_ids(jwt_in_env, credential_spy):
    AnthropicProvider.explicit_federation_credentials()

    assert credential_spy["federation_rule_id"] == "fdrl_test"
    assert credential_spy["organization_id"] == FED_IDS["ANTHROPIC_ORGANIZATION_ID"]
    assert credential_spy["service_account_id"] == "svac_test"


def test_the_token_provider_reads_the_variable_at_call_time(jwt_in_env, credential_spy,
                                                            monkeypatch):
    """The JWT is fetched when the exchange happens, not captured at
    construction — a rotated token is picked up."""
    AnthropicProvider.explicit_federation_credentials()
    provider = credential_spy["identity_token_provider"]

    assert provider() == FAKE_JWT
    monkeypatch.setenv("JWT", "eyJhbGciOiJSUzI1NiJ9.rotated.signature")
    assert provider().split(".")[1] == "rotated"


def test_workspace_is_omitted_unless_set(jwt_in_env, credential_spy, monkeypatch):
    """A rule bound to one workspace expects the field absent — the server
    selects that workspace itself."""
    AnthropicProvider.explicit_federation_credentials()
    assert "workspace_id" not in credential_spy

    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_test")
    AnthropicProvider.explicit_federation_credentials()
    assert credential_spy["workspace_id"] == "wrkspc_test"


def test_the_client_is_given_those_credentials(jwt_in_env, monkeypatch):
    """The client is constructed with credentials= rather than zero-arg,
    which is the whole point of this path."""
    calls = _fake_sdk(monkeypatch)
    sentinel = object()
    monkeypatch.setattr(AnthropicProvider, "explicit_federation_credentials",
                        classmethod(lambda cls: sentinel))

    AnthropicProvider()._client()

    assert calls == [{"credentials": sentinel}]


# ---------- Reading a token's claims (diagnosing a rejected exchange) ----------

def _load_check_script():
    """scripts/ is not a package — load the CLI module by path."""
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "scripts" / "check_credentials.py"
    spec = importlib.util.spec_from_file_location("check_credentials", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def test_identity_token_claims_are_decoded_for_comparison(tmp_path, monkeypatch, capsys):
    """A 401 from the exchange means the rule rejected the token; the next
    question is always which claim it rejected."""
    import base64
    import json as json_module

    claims = {
        "iss": "https://token.actions.githubusercontent.com",
        "aud": "https://api.anthropic.com",
        "sub": "repo:owner/repo:ref:refs/heads/main",
        "repository_owner": "owner",
        "event_name": "workflow_dispatch",
    }

    def b64(data):
        return base64.urlsafe_b64encode(json_module.dumps(data).encode()).decode().rstrip("=")

    token_file = tmp_path / "jwt"
    token_file.write_text(f"{b64({'alg': 'RS256'})}.{b64(claims)}.the-signature")
    monkeypatch.setenv("ANTHROPIC_IDENTITY_TOKEN_FILE", str(token_file))

    _load_check_script().show_claims()

    printed = capsys.readouterr().out
    for value in claims.values():
        assert value in printed
    # The signature is what makes the token usable — it is never printed.
    assert "the-signature" not in printed


def test_a_malformed_token_is_reported_not_raised(tmp_path, monkeypatch, capsys):
    token_file = tmp_path / "jwt"
    token_file.write_text("not-a-jwt")
    monkeypatch.setenv("ANTHROPIC_IDENTITY_TOKEN_FILE", str(token_file))

    _load_check_script().show_claims()

    assert "could not decode" in capsys.readouterr().out


# ---------- One client, one token exchange ----------

def test_the_client_is_built_once_and_reused(jwt_in_env, monkeypatch):
    """Each federated client construction costs a token exchange. An
    identity token an issuer honours once makes a per-call client fail on
    the second call, which is exactly what a live run showed."""
    calls = _fake_sdk(monkeypatch)
    monkeypatch.setattr(AnthropicProvider, "explicit_federation_credentials",
                        classmethod(lambda cls: object()))

    provider = AnthropicProvider()
    first = provider._client()
    second = provider._client()

    assert first is second
    assert len(calls) == 1


def test_resolve_returns_the_same_provider_for_the_same_environment(jwt_in_env):
    """Caching the client only helps if the provider survives too."""
    providers.reset_cache()
    assert providers.resolve() is providers.resolve()


def test_changing_a_credential_resolves_again(jwt_in_env, monkeypatch):
    providers.reset_cache()
    first = providers.resolve()
    monkeypatch.setenv("ANTHROPIC_SERVICE_ACCOUNT_ID", "svac_different")
    assert providers.resolve() is not first


def test_switching_provider_kind_resolves_again(jwt_in_env, monkeypatch):
    providers.reset_cache()
    assert isinstance(providers.resolve(), AnthropicProvider)
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("LLM_MODEL", "llama3.2")
    assert isinstance(providers.resolve(), OpenAICompatibleProvider)
