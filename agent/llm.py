"""
The model layer — the two judgements in this system that are genuinely
linguistic, and nothing else.

1. ``narrate()`` turns an incident's matched log lines into the prose a
   human reads first: what this means for the business, the likely root
   cause, and what to do next. Log lines are precise and unreadable;
   that translation is the job.

2. ``judge_resolution()`` reads the human replies on an incident's Slack
   thread and decides whether they actually confirm the problem is
   fixed. "restarted the consumer, lag is draining now" confirms it;
   "looking into it" does not; "should be fine after the next run" is a
   maybe. No regex settles that, and on a streaming pipeline the answer
   decides whether the stream starts moving again.

What the model is deliberately *not* asked to do: decide severity, pick a
runbook, or decide whether an incident opens at all. Those stay in
agent/severity.py and agent/runbooks.py, where the same evidence always
produces the same answer and `matched_conditions` says exactly why.

Neither call needs a frontier model, so neither is tied to one.
agent/providers.py resolves whatever is configured — Claude, a local
Ollama or llama.cpp server, or a hosted free tier speaking the
OpenAI protocol — and this module only asks it for a validated object.

Every call degrades instead of failing. No provider configured, a rate
limit, a timeout, a response that doesn't match the schema — each
returns the deterministic fallback the rest of the code already had,
marked ``source="fallback"`` so the caller can tell the difference. An
alert that reads a little flatter is fine; an incident that fails to
open because an inference call failed is not.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from agent import providers
from agent.providers import Provider, ProviderError

# How many log lines of context each call gets. Enough to reason from,
# small enough to keep a per-incident call cheap and fast — and small
# enough for a 3B model running on a laptop.
MAX_CONTEXT_LINES = 40


class Narration(BaseModel):
    """What a responder reads at the top of the alert."""

    impact_summary: str = Field(
        description="Two sentences at most, in business terms — who or what is "
                    "affected and how, never the raw log line. No severity label."
    )
    root_cause: str = Field(
        description="The most likely technical cause, stated as a hypothesis with "
                    "the log evidence that supports it."
    )
    recommended_action: str = Field(
        description="The single next action the on-call engineer should take."
    )


class ResolutionJudgement(BaseModel):
    """Whether a human has confirmed the incident is actually fixed."""

    resolved: bool = Field(
        description="True only if a human states the underlying problem is fixed or "
                    "recovered. Acknowledgement, investigation, or intent to fix is not "
                    "resolution."
    )
    confidence: float = Field(description="0.0-1.0 confidence in that call.")
    reason: str = Field(description="One sentence quoting what decided it.")


@dataclass
class LLMResult:
    """A value plus where it came from, so callers never have to guess
    whether a model actually ran — and which one."""

    value: Any
    source: str  # the provider's name ("anthropic", "ollama", ...) or "fallback"
    detail: str = ""

    @property
    def from_model(self) -> bool:
        return self.source != "fallback"


def provider() -> Provider | None:
    """Whichever provider the environment resolves to, or None."""
    return providers.resolve()


def available() -> bool:
    """True when an inference call would actually be attempted."""
    return providers.resolve() is not None


def describe() -> str:
    """One line naming the provider and model in use."""
    return providers.describe()


def _parse(system: str, prompt: str, schema: type[BaseModel], effort: str):
    """One structured-output call against the resolved provider. Raises;
    callers handle the fallback."""
    active = providers.resolve()
    if active is None:
        raise ProviderError("no provider configured")
    return active.complete_json(system, prompt, schema, effort)


def _describe_exception(exc: Exception) -> str:
    """Never let an API key reach a log line or a Slack message."""
    name = type(exc).__name__
    message = str(exc)
    for variable in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "LLM_API_KEY"):
        secret = os.getenv(variable)
        if secret:
            message = message.replace(secret, "***REDACTED***")
    # Long enough for a federation error to arrive intact: those carry the
    # server's own remediation hint, and cutting it mid-sentence throws away
    # the most useful part of the failure.
    return f"{name}: {message[:800]}"


def _source_name() -> str:
    active = providers.resolve()
    return active.name if active else "fallback"


# ---------- 1. Narrating an incident ----------

NARRATE_SYSTEM = """You are an ETL production support analyst writing the first \
paragraph of an incident that has just been opened from log evidence.

You are given the signals a deterministic classifier derived from the logs, the \
severity it assigned, and the log lines those signals came from.

Rules:
- Write for whoever is woken up by this alert. Business impact first: which data, \
which consumers, which deadline.
- Never restate the severity, never argue with it, never suggest a different one. \
It was decided before you were called.
- Claim only what the log lines support. If the cause is ambiguous, say what it \
looks like and what would confirm it.
- No greetings, no markdown headers, no bullet lists. Plain sentences.
- Never invent table names, row counts, job names or times that are not in the \
evidence."""


def _fallback_narration(signals: dict[str, Any], findings: list[dict[str, Any]],
                        default_summary: str) -> Narration:
    titles = []
    for finding in findings:
        if finding["title"] not in titles:
            titles.append(finding["title"])
    if findings:
        worst = findings[0]
        root_cause = (f"{worst['title']} ({worst['signature_id']}) — "
                      f"`{worst['file']}` line {worst['line']}: {worst['line_text'][:200]}")
    else:
        root_cause = "No catalogued failure signature matched the errors in this log set."
    return Narration(
        impact_summary=default_summary,
        root_cause=root_cause,
        recommended_action="Work the linked runbook.",
    )


def narrate(
    severity: str,
    signals: dict[str, Any],
    findings: list[dict[str, Any]],
    log_lines: list[str],
    default_summary: str,
    source_label: str = "",
) -> LLMResult:
    """Write the human-facing part of an incident. Returns a Narration
    either way — from Claude, or the deterministic text."""
    fallback = _fallback_narration(signals, findings, default_summary)
    if not available():
        return LLMResult(fallback, "fallback", "no model provider configured")

    matched = "\n".join(
        f"- {f['signature_id']} ({f['title']}) at {f['file']}:{f['line']}: {f['line_text']}"
        for f in findings[:10]
    ) or "- none: errors were present but matched no catalogued signature"

    context = "\n".join(log_lines[:MAX_CONTEXT_LINES]) or "(no surrounding lines captured)"
    prompt = (
        f"Source under triage: {source_label or 'an ETL pipeline'}\n"
        f"Severity already assigned: {severity}\n\n"
        f"Signals derived from the logs:\n{signals}\n\n"
        f"Matched failure signatures:\n{matched}\n\n"
        f"Log lines around the failure:\n```\n{context}\n```"
    )

    try:
        return LLMResult(_parse(NARRATE_SYSTEM, prompt, Narration, "medium"), _source_name())
    except Exception as exc:  # noqa: BLE001 - an alert must not depend on an inference call
        detail = _describe_exception(exc)
        # Say why, loudly. Degrading silently is what turns a broken
        # credential or a rejected parameter into "the model seems quiet".
        print(f"[llm] WARNING: narration fell back to deterministic text — {detail}")
        return LLMResult(fallback, "fallback", detail)


# ---------- 2. Judging a resolution ----------

JUDGE_SYSTEM = """You decide whether an on-call incident has been confirmed \
resolved by a human in its Slack thread.

A paused data pipeline resumes on your answer, so the bar is: a person states the \
underlying problem is fixed, recovered, or no longer occurring.

resolved = true: "restarted the consumer, lag is draining", "namenode is back up, \
writes are succeeding", "rebalanced and the job completed", "false alarm — the \
column was renamed in a release, target is fine".

resolved = false: "looking into it", "paging the DBA", "I'll fix it after standup", \
"should be fine once the next run goes through", "any idea what this is?", or \
anything that only acknowledges the alert.

Ambiguity resolves to false. It is much worse to resume a broken pipeline than to \
keep it paused for another minute. Quote the words that decided it."""


# Phrases the no-model fallback accepts as an outright confirmation.
EXPLICIT_RESOLUTION = (
    "resolved", "is fixed", "has been fixed", "all clear", "back up and",
    "recovered", "we're good", "we are good", "issue is gone",
)


def judge_resolution(
    incident_summary: str,
    replies: list[dict[str, str]],
) -> LLMResult:
    """Decide whether the thread confirms the incident is fixed.

    With no provider available this falls back to a deliberately narrow
    keyword check — it recognises an explicit "resolved"/"fixed" and
    nothing more, because the safe failure here is to keep waiting.
    """
    transcript = "\n".join(f"{reply.get('user', 'unknown')}: {reply.get('text', '')}"
                           for reply in replies)

    if not available():
        # Deliberately narrow: an unambiguous statement of resolution and
        # nothing else. It cannot read "should be fine after the next run"
        # the way the model can, so it holds the gate shut on anything it
        # doesn't recognise outright.
        matched = next(
            (phrase for reply in replies for phrase in EXPLICIT_RESOLUTION
             if phrase in (reply.get("text") or "").lower()),
            None,
        )
        return LLMResult(
            ResolutionJudgement(
                resolved=matched is not None,
                confidence=0.7 if matched else 0.0,
                reason=(f'a reply contains "{matched}" (keyword match, no model available)'
                        if matched else "no reply states outright that the issue is fixed"),
            ),
            "fallback",
            "no model provider configured",
        )

    prompt = (
        f"Incident:\n{incident_summary}\n\n"
        f"Replies on its Slack thread, oldest first:\n{transcript or '(none yet)'}"
    )
    try:
        return LLMResult(_parse(JUDGE_SYSTEM, prompt, ResolutionJudgement, "low"), _source_name())
    except Exception as exc:  # noqa: BLE001 - a failed call must not resume the stream
        detail = _describe_exception(exc)
        print(f"[llm] WARNING: resolution judgement fell back, gate stays shut — {detail}")
        return LLMResult(
            ResolutionJudgement(resolved=False, confidence=0.0,
                                reason="could not reach the model; staying paused"),
            "fallback",
            detail,
        )
