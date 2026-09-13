"""
Deterministic severity classification for the ETL Production Support Triage Agent.

Thresholds live in config/severity.yml, never in code. This module reads
that config and classifies a dict of observed signals against it. The
Claude agent reports signals; it does not decide severity itself — the
same evidence must always yield the same call. See agent/prompts.py.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(__file__), "..", "config", "severity.yml"
)

# Numeric comparison operators supported inside a condition spec, e.g.
# {gte: 5.0, lt: 10.0}. Each maps to (symbol, comparison function).
_NUMERIC_OPS = {
    "gte": (">=", lambda value, threshold: value >= threshold),
    "lte": ("<=", lambda value, threshold: value <= threshold),
    "gt": (">", lambda value, threshold: value > threshold),
    "lt": ("<", lambda value, threshold: value < threshold),
    "eq": ("==", lambda value, threshold: value == threshold),
}


@dataclass
class SeverityResult:
    severity: str | None
    matched_conditions: list[str] = field(default_factory=list)
    rationale: str = ""
    response_expectation: str = ""


def load_config(path: str | None = None) -> dict[str, Any]:
    """Load and parse config/severity.yml (or an override path)."""
    with open(path or DEFAULT_CONFIG_PATH) as f:
        return yaml.safe_load(f)


def _eval_numeric_condition(name: str, spec: dict, signals: dict) -> tuple[bool, str | None]:
    value = signals.get(name)
    if value is None:
        return False, None

    parts = []
    for op, threshold in spec.items():
        if op not in _NUMERIC_OPS:
            continue
        symbol, cmp = _NUMERIC_OPS[op]
        if not cmp(value, threshold):
            return False, None
        parts.append(f"{symbol} {threshold}")

    if not parts:
        return False, None
    return True, f"{name} {' and '.join(parts)} (actual: {value})"


def _eval_null_rate_condition(spec: dict, signals: dict, config: dict) -> tuple[bool, str | None]:
    """
    null_rate_increase_pct conditions carry a `columns` bucket (critical |
    non_critical) instead of naming a single column — the signal is a dict
    of {column_name: pct_increase}, and a column not listed in
    column_criticality.critical defaults to non_critical rather than
    raising.
    """
    increases = signals.get("null_rate_increase_pct") or {}
    if not isinstance(increases, dict):
        return False, None

    bucket = spec.get("columns", "non_critical")
    critical_cols = set(config.get("column_criticality", {}).get("critical", []))

    bound_parts = []
    for op in ("gte", "lte", "gt", "lt", "eq"):
        if op in spec:
            symbol, _ = _NUMERIC_OPS[op]
            bound_parts.append(f"{symbol} {spec[op]}")

    matched_cols = []
    for column, pct in increases.items():
        ok = True
        for op, threshold in spec.items():
            if op not in _NUMERIC_OPS:
                continue
            _, cmp = _NUMERIC_OPS[op]
            if not cmp(pct, threshold):
                ok = False
                break
        if not ok:
            continue
        column_bucket = "critical" if column in critical_cols else "non_critical"
        if column_bucket == bucket:
            matched_cols.append((column, pct))

    if not matched_cols:
        return False, None

    detail = ", ".join(f"{col} +{pct}%" for col, pct in matched_cols)
    bound = " and ".join(bound_parts) if bound_parts else "matched"
    return True, f"null_rate_increase_pct {bound} on {bucket} column(s): {detail}"


def _eval_condition(name: str, spec: Any, signals: dict, config: dict) -> tuple[bool, str | None]:
    if name == "null_rate_increase_pct":
        return _eval_null_rate_condition(spec, signals, config)

    if spec is True:
        return (True, name) if bool(signals.get(name)) else (False, None)

    if isinstance(spec, dict):
        return _eval_numeric_condition(name, spec, signals)

    return False, None


def classify(signals: dict[str, Any], config: dict[str, Any]) -> SeverityResult:
    """
    Classify observed signals against config/severity.yml's rules.

    Evaluates severities in `evaluation.order` (most severe first) and
    returns on the first severity with at least one matching condition —
    this is what makes "the most severe wins" on a multi-match run: a
    signal set that would qualify for both P1 and P3 is reported as P1.
    """
    order = config.get("evaluation", {}).get("order") or list(config["severities"].keys())

    for severity_name in order:
        severity_cfg = config["severities"].get(severity_name)
        if not severity_cfg:
            continue

        matched: list[str] = []
        for condition in severity_cfg.get("conditions", []):
            for name, spec in condition.items():
                ok, description = _eval_condition(name, spec, signals, config)
                if ok and description:
                    matched.append(description)

        if matched:
            return SeverityResult(
                severity=severity_name,
                matched_conditions=matched,
                rationale="; ".join(matched),
                response_expectation=severity_cfg.get("response", ""),
            )

    default = config.get("evaluation", {}).get("default")
    return SeverityResult(
        severity=default,
        matched_conditions=[],
        rationale="No severity conditions matched — clean run.",
        response_expectation="",
    )
