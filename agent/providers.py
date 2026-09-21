"""
Where the model comes from — Claude, any OpenAI-compatible endpoint, or
nothing at all.

The two judgements in agent/llm.py are small and well-specified: a
paragraph of incident narration, and a yes/no on whether a Slack reply
confirms a fix. Nothing about them requires a frontier model, so nothing
here requires one either.

Two adapters cover essentially every option:

* ``AnthropicProvider`` — the Claude API through the official SDK.
* ``OpenAICompatibleProvider`` — one ``POST /chat/completions`` over
  httpx, which is the protocol spoken by Ollama and llama.cpp and vLLM
  and LM Studio running locally, and by Groq, OpenRouter, Together,
  Fireworks, DeepSeek and Gemini's compatibility endpoint remotely.
  Point ``LLM_BASE_URL`` at any of them.

Structured output is negotiated rather than assumed: the adapter asks
for a JSON schema first, and if the endpoint rejects that (many
open-source servers do), it falls back to plain JSON mode with the
schema inlined in the prompt, then validates the result itself. A
response that still doesn't validate is treated as a failed call, which
means agent/llm.py's deterministic fallback — never a half-parsed
object.

Selection is by configuration, in this order, and ``LLM_PROVIDER`` pins
it explicitly:
  1. LLM_BASE_URL set            → OpenAI-compatible (local or hosted)
  2. ANTHROPIC_API_KEY set       → Claude
  3. a local Ollama responding   → OpenAI-compatible against it
  4. nothing                     → no provider; deterministic fallbacks
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ValidationError

DEFAULT_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "60"))

# Ollama's OpenAI-compatible endpoint, if someone is running one locally.
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_DEFAULT_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")

# Effort maps to whatever each provider actually understands.
_ANTHROPIC_EFFORT = {"low": "low", "medium": "medium", "high": "high"}
_OPENAI_MAX_TOKENS = {"low": 700, "medium": 1200, "high": 2000}

# Workload Identity Federation: the SDK performs the token exchange itself
# when all of these are set, and refreshes before expiry. There is no static
# secret anywhere — the JWT comes from the platform the workload runs on.
# https://platform.claude.com/docs/en/manage-claude/wif-reference
_WIF_REQUIRED = ("ANTHROPIC_FEDERATION_RULE_ID", "ANTHROPIC_ORGANIZATION_ID",
                 "ANTHROPIC_SERVICE_ACCOUNT_ID")
_WIF_TOKEN = ("ANTHROPIC_IDENTITY_TOKEN_FILE", "ANTHROPIC_IDENTITY_TOKEN")

# An `ant auth login` profile — the keyless option on a developer machine,
# where there is no workload identity to federate.
_CONFIG_DIR_ENV = "ANTHROPIC_CONFIG_DIR"

# The Console's "Authenticate from your workload" snippet hands the JWT to
# the SDK through a callable reading an environment variable (JWT by
# default) rather than through ANTHROPIC_IDENTITY_TOKEN[_FILE]. Supporting
# that shape means the snippet works here unchanged.
_IDENTITY_TOKEN_ENV = os.getenv("ANTHROPIC_IDENTITY_TOKEN_ENV", "JWT")

# output_config.effort is not accepted by every Claude model — Haiku 4.5 and
# Sonnet 4.5 reject it with a 400. Sending it to them would fail every call,
# so it is only included for the families that take it.
_NO_EFFORT_SUPPORT = ("haiku", "sonnet-4-5", "sonnet-3")


class ProviderError(RuntimeError):
    """A call failed. agent/llm.py turns this into its fallback."""


class Provider(Protocol):
    name: str
    model: str

    def complete_json(self, system: str, prompt: str, schema: type[BaseModel],
                      effort: str) -> BaseModel:
        ...


# ---------- Claude ----------

class AnthropicProvider:
    """The Claude API, through the official SDK.

    Defaults to Haiku 4.5: this workload is a short paragraph and a yes/no,
    and Haiku is the cheapest current model at $1/$5 per million tokens —
    about $0.004 an incident. Set AGENT_MODEL to move up."""

    DEFAULT_MODEL = "claude-haiku-4-5"

    def __init__(self, model: str | None = None, api_key: str | None = None):
        self.name = "anthropic"
        self.model = model or os.getenv("AGENT_MODEL", self.DEFAULT_MODEL)
        self._api_key = api_key
        # One client per provider, reused. The SDK caches the access token
        # it gets from a federated exchange and refreshes it before expiry —
        # build a new client per call and every call performs a fresh
        # exchange instead, which is both wasteful and, with an identity
        # token an issuer only honours once, unreliable.
        self._client_instance = None

    def supports_effort(self) -> bool:
        return not any(family in self.model for family in _NO_EFFORT_SUPPORT)

    # ---- credentials ----

    @staticmethod
    def federation_configured() -> bool:
        """Workload Identity Federation: no static secret, short-lived
        tokens exchanged from a JWT the platform issues. The JWT may arrive
        as a file, as ANTHROPIC_IDENTITY_TOKEN, or in the plain variable the
        Console's snippet reads."""
        if not all(os.getenv(name) for name in _WIF_REQUIRED):
            return False
        return bool(any(os.getenv(name) for name in _WIF_TOKEN)
                    or os.getenv(_IDENTITY_TOKEN_ENV))

    @staticmethod
    def profile_configured() -> tuple[bool, str]:
        """An `ant auth login` profile on disk. Returns (found, name)."""
        named = os.getenv("ANTHROPIC_PROFILE")
        if named:
            return True, named
        config_dir = Path(os.getenv(_CONFIG_DIR_ENV)
                          or (Path.home() / ".config" / "anthropic"))
        try:
            active = config_dir / "active_config"
            if active.is_file():
                return True, active.read_text(encoding="utf-8").strip() or "default"
            if (config_dir / "configs" / "default.json").is_file():
                return True, "default"
        except OSError:
            pass
        return False, ""

    @classmethod
    def credential_source(cls) -> str:
        """Which credential the SDK will actually use, in its documented
        precedence order. Reported in the banner, because "it authenticated"
        and "it authenticated as who you meant" are different questions."""
        if os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"):
            return "api key"
        found, name = cls.profile_configured()
        if found and os.getenv("ANTHROPIC_PROFILE"):
            return f"profile {name}"
        if cls.federation_configured():
            return "workload identity federation"
        if found:
            return f"profile {name}"
        return "none"

    @classmethod
    def configured(cls) -> bool:
        if cls.credential_source() == "none":
            return False
        import importlib.util

        return importlib.util.find_spec("anthropic") is not None

    @classmethod
    def explicit_federation_credentials(cls):
        """Build WorkloadIdentityCredentials when the JWT lives in a plain
        environment variable, which is how the Console's snippet passes it.

        Returns None whenever the SDK's own detection already covers the
        case — an identity token file or ANTHROPIC_IDENTITY_TOKEN — so the
        zero-argument path stays the normal one.
        """
        if any(os.getenv(name) for name in _WIF_TOKEN):
            return None
        if not all(os.getenv(name) for name in _WIF_REQUIRED):
            return None
        token_env = _IDENTITY_TOKEN_ENV
        if not os.getenv(token_env):
            return None

        from anthropic import WorkloadIdentityCredentials

        kwargs = {
            "identity_token_provider": lambda: os.environ[token_env],
            "federation_rule_id": os.environ["ANTHROPIC_FEDERATION_RULE_ID"],
            "organization_id": os.environ["ANTHROPIC_ORGANIZATION_ID"],
            "service_account_id": os.environ["ANTHROPIC_SERVICE_ACCOUNT_ID"],
        }
        # Omitted when the rule covers a single workspace: the server picks it.
        if os.getenv("ANTHROPIC_WORKSPACE_ID"):
            kwargs["workspace_id"] = os.environ["ANTHROPIC_WORKSPACE_ID"]
        return WorkloadIdentityCredentials(**kwargs)

    def _client(self):
        if self._client_instance is not None:
            return self._client_instance
        self._client_instance = self._build_client()
        return self._client_instance

    def _build_client(self):
        import anthropic

        if self._api_key:
            return anthropic.Anthropic(api_key=self._api_key)

        # A credential variable set to an *empty string* still occupies its
        # slot in the SDK's precedence chain: an exported ANTHROPIC_API_KEY=""
        # authenticates with an empty key instead of falling through to
        # federation or a profile. A blank placeholder left in .env is the
        # usual way that happens, so clear it when something else is
        # configured rather than failing with a confusing 401.
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            if name in os.environ and not os.environ[name].strip():
                if self.federation_configured() or self.profile_configured()[0]:
                    del os.environ[name]

        credentials = self.explicit_federation_credentials()
        if credentials is not None:
            return anthropic.Anthropic(credentials=credentials)

        # Zero-arg: the SDK resolves the API key, the profile, or the
        # federation exchange itself, and refreshes federated tokens.
        return anthropic.Anthropic()

    def complete_json(self, system: str, prompt: str, schema: type[BaseModel],
                      effort: str) -> BaseModel:
        request = {
            "model": self.model,
            "max_tokens": 2000,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
            "output_format": schema,
        }
        if self.supports_effort():
            request["output_config"] = {"effort": _ANTHROPIC_EFFORT.get(effort, "medium")}

        try:
            response = self._client().messages.parse(**request)
        except Exception as exc:  # noqa: BLE001 - see below
            # Model naming drifts; if a model turns out not to take effort
            # after all, drop it and try once more rather than failing the
            # call over a parameter this workload does not need.
            if "effort" in str(exc) and "output_config" in request:
                request.pop("output_config")
                response = self._client().messages.parse(**request)
            else:
                raise ProviderError(str(exc)) from exc

        if getattr(response, "stop_reason", None) == "refusal":
            raise ProviderError(f"model declined: {getattr(response, 'stop_details', None)}")
        return response.parsed_output


# ---------- Anything that speaks /chat/completions ----------

class OpenAICompatibleProvider:
    """One adapter for every OpenAI-protocol endpoint — a local Ollama or
    llama.cpp server, or a hosted free tier like Groq or OpenRouter."""

    def __init__(self, base_url: str | None = None, model: str | None = None,
                 api_key: str | None = None, label: str = "openai-compatible"):
        self.name = label
        self.base_url = (base_url or os.getenv("LLM_BASE_URL", "")).rstrip("/")
        self.model = model or os.getenv("LLM_MODEL", "")
        self.api_key = api_key if api_key is not None else os.getenv("LLM_API_KEY", "")
        if not self.base_url:
            raise ProviderError("LLM_BASE_URL is not set")
        if not self.model:
            raise ProviderError("LLM_MODEL is not set")

    @staticmethod
    def configured() -> bool:
        return bool(os.getenv("LLM_BASE_URL") and os.getenv("LLM_MODEL"))

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            response = httpx.post(f"{self.base_url}/chat/completions", json=payload,
                                  headers=headers, timeout=DEFAULT_TIMEOUT)
        except httpx.RequestError as exc:
            raise ProviderError(f"could not reach {self.base_url}: {exc}") from exc
        if response.status_code >= 400:
            raise ProviderError(
                f"{self.base_url} returned {response.status_code}: {response.text[:200]}")
        return response.json()

    @staticmethod
    def _content(data: dict[str, Any]) -> str:
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"unexpected response shape: {str(data)[:200]}") from exc

    @staticmethod
    def _loads(content: str, schema: type[BaseModel]) -> BaseModel:
        """Parse the model's JSON. Small models like to wrap it in prose or
        a code fence, so strip to the outermost object before giving up."""
        text = content.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.lstrip().startswith("json"):
                text = text.lstrip()[4:]
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            text = text[start:end + 1]
        try:
            return schema.model_validate(json.loads(text))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise ProviderError(f"response did not match the schema: {exc}") from exc

    def complete_json(self, system: str, prompt: str, schema: type[BaseModel],
                      effort: str) -> BaseModel:
        json_schema = schema.model_json_schema()
        base = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": prompt}],
            "max_tokens": _OPENAI_MAX_TOKENS.get(effort, 1200),
            "temperature": 0,
        }

        # First choice: real schema enforcement, where the server supports it.
        strict = dict(base, response_format={
            "type": "json_schema",
            "json_schema": {"name": schema.__name__, "schema": json_schema, "strict": True},
        })
        try:
            return self._loads(self._content(self._post(strict)), schema)
        except ProviderError as exc:
            first_error = exc

        # Fallback: plain JSON mode with the schema in the prompt. This is
        # what most locally-hosted open-source models actually support.
        loose = dict(base, response_format={"type": "json_object"})
        loose["messages"] = [
            {"role": "system",
             "content": f"{system}\n\nReply with a single JSON object and nothing else. "
                        f"It must match this JSON Schema exactly:\n{json.dumps(json_schema)}"},
            {"role": "user", "content": prompt},
        ]
        try:
            return self._loads(self._content(self._post(loose)), schema)
        except ProviderError as exc:
            raise ProviderError(f"{exc} (schema mode also failed: {first_error})") from exc


# ---------- Picking one ----------

def _ollama_running(host: str = OLLAMA_HOST) -> bool:
    """A quick TCP check — cheap enough to run at startup, and only ever
    reached when nothing else is configured."""
    try:
        url = httpx.URL(host)
        with socket.create_connection((url.host or "localhost", url.port or 11434), timeout=0.3):
            return True
    except OSError:
        return False


# Providers are cached against the environment that produced them: the
# point is to keep one client (and therefore one exchanged access token)
# alive across calls, and a fresh provider each time would defeat that. A
# change to any credential variable resolves again, so tests and runtime
# reconfiguration both behave.
_CACHE_KEYS = (
    "LLM_PROVIDER", "LLM_MODE", "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY",
    "AGENT_MODEL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE",
    "ANTHROPIC_CONFIG_DIR", "ANTHROPIC_WORKSPACE_ID", _IDENTITY_TOKEN_ENV,
    *_WIF_REQUIRED, *_WIF_TOKEN,
)
_resolved: dict[tuple, Provider | None] = {}


def _cache_key(preference: str | None) -> tuple:
    return (preference, *(os.getenv(name) for name in _CACHE_KEYS))


def reset_cache() -> None:
    """Forget the resolved provider — for tests, and for anything that
    reconfigures credentials in-process."""
    _resolved.clear()


def resolve(preference: str | None = None) -> Provider | None:
    """Return the provider to use, or None for the deterministic path."""
    key = _cache_key(preference)
    if key in _resolved:
        return _resolved[key]
    provider = _resolve_uncached(preference)
    _resolved[key] = provider
    return provider


def _resolve_uncached(preference: str | None = None) -> Provider | None:
    choice = (preference or os.getenv("LLM_PROVIDER", "auto")).lower()

    # LLM_MODE=off is the older switch for the same thing; still honoured.
    if choice in {"off", "none", "disabled"} or os.getenv("LLM_MODE", "").lower() == "off":
        return None

    try:
        if choice in {"openai", "openai-compatible", "compatible"}:
            return OpenAICompatibleProvider()
        if choice == "ollama":
            return OpenAICompatibleProvider(
                base_url=f"{OLLAMA_HOST}/v1",
                model=os.getenv("LLM_MODEL") or OLLAMA_DEFAULT_MODEL,
                api_key="ollama", label="ollama")
        if choice == "anthropic":
            return AnthropicProvider()
    except ProviderError:
        return None

    if choice != "auto":
        return None

    if OpenAICompatibleProvider.configured():
        try:
            return OpenAICompatibleProvider()
        except ProviderError:
            pass
    if AnthropicProvider.configured():
        return AnthropicProvider()
    if _ollama_running():
        try:
            return OpenAICompatibleProvider(
                base_url=f"{OLLAMA_HOST}/v1",
                model=os.getenv("LLM_MODEL") or OLLAMA_DEFAULT_MODEL,
                api_key="ollama", label="ollama")
        except ProviderError:
            return None
    return None


def describe() -> str:
    """One line for a CLI banner."""
    provider = resolve()
    if provider is None:
        return "no model configured — deterministic fallbacks"
    if isinstance(provider, AnthropicProvider):
        return f"{provider.name} ({provider.model}, via {provider.credential_source()})"
    return f"{provider.name} ({provider.model})"
