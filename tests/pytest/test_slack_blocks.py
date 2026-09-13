"""
Tests for agent.slack_blocks. Golden-file tests commit the exact expected
Block Kit JSON per severity under tests/fixtures/blocks/ and assert
byte-equality (serialized with fixed, sorted-key JSON so the comparison is
stable) — regenerate a fixture deliberately with
`UPDATE_BLOCK_FIXTURES=1 pytest tests/pytest/test_slack_blocks.py` if a
builder change is intentional.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import pytest

from agent.incident import Incident
from agent.slack_blocks import (
    SlackBlockLimitError,
    build_evidence_section,
    build_parent_message,
    build_thread_reply,
    validate_blocks,
)

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "../fixtures/blocks")


def _fixed_incident(**overrides) -> Incident:
    base = dict(
        incident_id="INC-20260912-0214",
        opened_at="2026-09-12T02:14:33.000Z",
        detected_by="sql_validator",
        severity="P2",
        severity_rationale="row_variance_pct >= 5.0 (actual: 40.0)",
        affected_job="cx_customer_load",
        affected_objects=["target.customer_dim", "target.account_balance_fact"],
        rows_expected=1_482_930,
        rows_loaded=889_758,
        impact_summary="Customer dimension is missing roughly 40% of records; three downstream jobs blocked.",
        evidence=["reports/evidence/INC-20260912-0214/recon.json"],
        root_cause="40,000 records dropped during deduplication in CustomerTransformStep.",
        confidence=0.95,
        runbook="docs/runbooks/RB-001-row-shortfall.md",
        recommended_action="Inspect the deduplication step for partition skew.",
        requires_approval=True,
        slack_channel=None,
        slack_ts=None,
        status="open",
        timeline=[],
        mtta_seconds=None,
        mttr_seconds=None,
    )
    base.update(overrides)
    return Incident(**base)


def _dump(blocks) -> str:
    return json.dumps(blocks, indent=2, sort_keys=True) + "\n"


def _assert_matches_fixture(name: str, blocks: list[dict]):
    path = os.path.join(FIXTURES_DIR, f"{name}.json")
    actual = _dump(blocks)

    if os.environ.get("UPDATE_BLOCK_FIXTURES"):
        with open(path, "w") as f:
            f.write(actual)

    with open(path) as f:
        expected = f.read()

    assert actual == expected, f"blocks for {name} no longer match the committed fixture"


class TestGoldenFixturesPerSeverity:
    @pytest.mark.parametrize("severity", ["P1", "P2", "P3", "P4"])
    def test_parent_message_matches_fixture(self, severity):
        incident = _fixed_incident(severity=severity, requires_approval=(severity in {"P1", "P2"}))
        blocks, _ = build_parent_message(incident, run_id="run-001")
        _assert_matches_fixture(f"parent_{severity.lower()}", blocks)

    def test_resolved_incident_matches_fixture(self):
        incident = _fixed_incident(
            status="resolved",
            requires_approval=False,
            mtta_seconds=180,
            mttr_seconds=5400,
        )
        blocks, _ = build_parent_message(incident, run_id="run-001")
        _assert_matches_fixture("parent_resolved", blocks)


class TestSeverityVisualDistinction:
    def test_p1_and_p4_headers_differ(self):
        p1_blocks, _ = build_parent_message(_fixed_incident(severity="P1"))
        p4_blocks, _ = build_parent_message(_fixed_incident(severity="P4", requires_approval=False))
        assert p1_blocks[0]["text"]["text"] != p4_blocks[0]["text"]["text"]
        assert "🔴" in p1_blocks[0]["text"]["text"]
        assert "⚪" in p4_blocks[0]["text"]["text"]

    def test_resolved_uses_checkmark_regardless_of_severity(self):
        blocks, _ = build_parent_message(_fixed_incident(severity="P1", status="resolved", requires_approval=False))
        assert "✅" in blocks[0]["text"]["text"]


class TestNullFieldsOmitted:
    def test_null_root_cause_omits_block_entirely(self):
        blocks, _ = build_parent_message(_fixed_incident(root_cause=None))
        rendered = json.dumps(blocks)
        assert "Root cause" not in rendered
        assert "None" not in rendered

    def test_no_runbook_no_recommended_action_omits_block(self):
        blocks, _ = build_parent_message(_fixed_incident(runbook=None, recommended_action=None))
        rendered = json.dumps(blocks)
        assert "Recommended action" not in rendered
        assert "Runbook" not in rendered


class TestApprovalActionsBlock:
    def test_requires_approval_adds_actions_block(self):
        blocks, _ = build_parent_message(_fixed_incident(requires_approval=True, status="open"))
        action_blocks = [b for b in blocks if b["type"] == "actions"]
        assert len(action_blocks) == 1
        action_ids = {el["action_id"] for el in action_blocks[0]["elements"]}
        assert action_ids == {"incident_approve", "incident_reject", "incident_escalate"}

    def test_no_approval_needed_omits_actions_block(self):
        blocks, _ = build_parent_message(_fixed_incident(requires_approval=False))
        assert all(b["type"] != "actions" for b in blocks)

    def test_resolved_incident_never_shows_actions_even_if_flagged(self):
        blocks, _ = build_parent_message(_fixed_incident(requires_approval=True, status="resolved"))
        assert all(b["type"] != "actions" for b in blocks)

    def test_action_button_values_carry_incident_id(self):
        incident = _fixed_incident(requires_approval=True)
        blocks, _ = build_parent_message(incident)
        action_block = next(b for b in blocks if b["type"] == "actions")
        assert all(el["value"] == incident.incident_id for el in action_block["elements"])


class TestEvidenceTruncation:
    def test_no_evidence_says_none_collected(self):
        section = build_evidence_section([])
        assert "none collected" in section["text"]["text"]

    def test_shows_at_most_five_lines(self):
        evidence = [f"reports/evidence/x/file_{i}.log" for i in range(12)]
        section = build_evidence_section(evidence)
        text = section["text"]["text"]
        assert text.count("•") == 5
        assert "and 7 more" in text

    def test_never_embeds_raw_log_lines_only_paths(self):
        evidence = ["reports/evidence/x/driver_stdout.log"]
        section = build_evidence_section(evidence)
        assert "reports/evidence/x/driver_stdout.log" in section["text"]["text"]


class TestBlockLimits:
    def test_too_many_blocks_raises_at_build_time(self):
        blocks = [{"type": "divider"} for _ in range(51)]
        with pytest.raises(SlackBlockLimitError):
            validate_blocks(blocks)

    def test_text_over_3000_chars_raises(self):
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "x" * 3001}}]
        with pytest.raises(SlackBlockLimitError):
            validate_blocks(blocks)

    def test_text_at_3000_chars_is_fine(self):
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "x" * 3000}}]
        validate_blocks(blocks)  # should not raise

    def test_more_than_ten_fields_raises(self):
        blocks = [{"type": "section", "fields": [{"type": "mrkdwn", "text": f"f{i}"} for i in range(11)]}]
        with pytest.raises(SlackBlockLimitError):
            validate_blocks(blocks)

    def test_ten_fields_is_fine(self):
        blocks = [{"type": "section", "fields": [{"type": "mrkdwn", "text": f"f{i}"} for i in range(10)]}]
        validate_blocks(blocks)  # should not raise

    def test_impact_summary_over_limit_raises_at_build_not_post(self):
        incident = _fixed_incident(impact_summary="x" * 3001)
        with pytest.raises(SlackBlockLimitError):
            build_parent_message(incident)


class TestThreadReply:
    def test_returns_single_section_block(self):
        blocks, text = build_thread_reply("Approved by <@U123>")
        assert len(blocks) == 1
        assert blocks[0]["text"]["text"] == "Approved by <@U123>"
        assert text == "Approved by <@U123>"
