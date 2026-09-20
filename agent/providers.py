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
    """The Claude API, through the official SDK."""

    def __init__(self, model: str | None = None, api_key: str | None = None):
        self.name = "anthropic"
        self.model = model or os.getenv("AGENT_MODEL", "claude-opus-5")
        self._api_key = api_key

    @staticmethod
    def configured() -> bool:
        if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")):
            return False
        import importlib.util

        return importlib.util.find_spec("anthropic") is not None

    def _client(self):
        import anthropic

        return anthropic.Anthropic(**({"api_key": self._api_key} if self._api_key else {}))

    def complete_json(self, system: str, prompt: str, schema: type[BaseModel],
                      effort: str) -> BaseModel:
        response = self._client().messages.parse(
            model=self.model,
            max_tokens=2000,
            system=system,
            output_config={"effort": _ANTHROPIC_EFFORT.get(effort, "medium")},
            messages=[{"role": "user", "content": prompt}],
            output_format=schema,
        )
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


def resolve(preference: str | None = None) -> Provider | None:
    """Return the provider to use, or None for the deterministic path."""
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
    return f"{provider.name} ({provider.model})"
