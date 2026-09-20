"""
Tests for agent.incident — the incident record system of record.
Covers schema validation accept/reject, ID collision handling, timeline
ordering, MTTA with no human event, MTTR on a resolved incident, and a
round-trip persist/load.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import jsonschema
import pytest

from agent import incident as incident_module
from agent.incident import (
    append_timeline,
    compute_mtta,
    compute_mttr,
    load,
    open_incident,
    persist,
    render_markdown,
    set_status,
)
from agent.severity import SeverityResult


@pytest.fixture(autouse=True)
def incidents_dir(tmp_path, monkeypatch):
    """Redirect every persist()/load() in this test module to a scratch dir."""
    monkeypatch.setattr(incident_module, "INCIDENTS_DIR", tmp_path)
    return tmp_path


def _p2_result() -> SeverityResult:
    return SeverityResult(
        severity="P2",
        matched_conditions=["row_variance_pct >= 5.0 (actual: 7.3)"],
        rationale="row_variance_pct >= 5.0 (actual: 7.3)",
        response_expectation="Notify on-call, begin remediation, hourly updates",
    )


def _run_context(**overrides) -> dict:
    base = {
        "detected_by": "recon_checker",
        "affected_job": "cx_customer_load",
        "affected_objects": ["target.customer_dim"],
        "rows_expected": 1_000_000,
        "rows_loaded": 927_000,
        "impact_summary": "Customer dimension is missing roughly 7% of records.",
        "evidence": ["reports/evidence/INC-x/recon.json"],
        "root_cause": None,
        "confidence": 0.7,
        "runbook": "docs/runbooks/RB-001-row-shortfall.md",
        "recommended_action": "Re-run the load after checking for partition skew.",
        "requires_approval": True,
    }
    base.update(overrides)
    return base


class TestOpenIncident:
    def test_open_incident_sets_expected_fields(self):
        inc = open_incident({}, _p2_result(), _run_context())
        assert inc.severity == "P2"
        assert inc.status == "open"
        assert inc.affected_job == "cx_customer_load"
        assert inc.incident_id.startswith("INC-")

    def test_clean_run_cannot_open_an_incident(self):
        clean = SeverityResult(severity=None, matched_conditions=[], rationale="", response_expectation="")
        with pytest.raises(ValueError):
            open_incident({}, clean, _run_context())

    def test_opening_timeline_entry_present(self):
        inc = open_incident({}, _p2_result(), _run_context())
        assert len(inc.timeline) == 1
        assert inc.timeline[0]["event"] == "opened"


class TestIncidentIdCollision:
    def test_second_incident_same_minute_gets_suffix(self, incidents_dir):
        inc1 = open_incident({}, _p2_result(), _run_context())
        persist(inc1)

        inc2 = open_incident({}, _p2_result(), _run_context())
        assert inc2.incident_id != inc1.incident_id
        assert inc2.incident_id.startswith(inc1.incident_id)

    def test_third_incident_same_minute_gets_next_suffix(self, incidents_dir):
        inc1 = open_incident({}, _p2_result(), _run_context())
        persist(inc1)
        inc2 = open_incident({}, _p2_result(), _run_context())
        persist(inc2)
        inc3 = open_incident({}, _p2_result(), _run_context())

        ids = {inc1.incident_id, inc2.incident_id, inc3.incident_id}
        assert len(ids) == 3


class TestTimelineOrdering:
    def test_append_timeline_preserves_order(self):
        inc = open_incident({}, _p2_result(), _run_context())
        append_timeline(inc, actor="oncall_engineer", event="acknowledged", detail="ack via Slack")
        append_timeline(inc, actor="oncall_engineer", event="remediation_started", detail="re-running load")

        events = [entry["event"] for entry in inc.timeline]
        assert events == ["opened", "acknowledged", "remediation_started"]

    def test_set_status_appends_to_timeline_automatically(self):
        inc = open_incident({}, _p2_result(), _run_context())
        set_status(inc, "acknowledged", actor="oncall_engineer")

        assert inc.status == "acknowledged"
        assert inc.timeline[-1]["event"] == "status_change"
        assert "acknowledged" in inc.timeline[-1]["detail"]

    def test_set_status_rejects_unknown_status(self):
        inc = open_incident({}, _p2_result(), _run_context())
        with pytest.raises(ValueError):
            set_status(inc, "not_a_real_status")


class TestMTTA:
    def test_no_human_event_returns_none(self):
        inc = open_incident({}, _p2_result(), _run_context())
        # Only the system-authored "opened" entry exists so far.
        assert compute_mtta(inc) is None

    def test_human_event_gives_a_non_negative_mtta(self):
        inc = open_incident({}, _p2_result(), _run_context())
        append_timeline(inc, actor="oncall_engineer", event="acknowledged", detail="ack")
        mtta = compute_mtta(inc)
        assert mtta is not None
        assert mtta >= 0

    def test_bot_and_agent_actors_do_not_count_as_human(self):
        inc = open_incident({}, _p2_result(), _run_context())
        append_timeline(inc, actor="bot", event="posted", detail="parent message posted")
        append_timeline(inc, actor="agent", event="reclassified", detail="re-ran classification")
        assert compute_mtta(inc) is None


class TestMTTR:
    def test_unresolved_incident_has_no_mttr(self):
        inc = open_incident({}, _p2_result(), _run_context())
        assert compute_mttr(inc) is None

    def test_resolved_incident_has_non_negative_mttr(self):
        inc = open_incident({}, _p2_result(), _run_context())
        set_status(inc, "resolved", actor="oncall_engineer")
        assert inc.mttr_seconds is not None
        assert inc.mttr_seconds >= 0
        assert compute_mttr(inc) == inc.mttr_seconds


class TestSchemaValidation:
    def test_valid_incident_persists(self, incidents_dir):
        inc = open_incident({}, _p2_result(), _run_context())
        path = persist(inc)
        assert path.exists()

    def test_malformed_incident_raises_and_does_not_write(self, incidents_dir):
        inc = open_incident({}, _p2_result(), _run_context())
        inc.severity = "P9"  # not in the enum — schema must reject this

        with pytest.raises(jsonschema.ValidationError):
            persist(inc)
        assert not (incidents_dir / f"{inc.incident_id}.json").exists()

    def test_missing_required_field_is_rejected(self, incidents_dir):
        inc = open_incident({}, _p2_result(), _run_context())
        data = inc.to_dict()
        del data["impact_summary"]

        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=data, schema=incident_module._load_schema())


class TestRoundTrip:
    def test_persist_then_load_round_trips(self, incidents_dir):
        inc = open_incident({}, _p2_result(), _run_context())
        append_timeline(inc, actor="oncall_engineer", event="acknowledged", detail="ack")
        persist(inc)

        loaded = load(inc.incident_id)
        assert loaded.to_dict() == inc.to_dict()

    def test_persist_also_writes_markdown_sidecar(self, incidents_dir):
        inc = open_incident({}, _p2_result(), _run_context())
        persist(inc)
        md_path = incidents_dir / f"{inc.incident_id}.md"
        assert md_path.exists()
        assert inc.incident_id in md_path.read_text()

    def test_render_markdown_contains_key_fields(self):
        inc = open_incident({}, _p2_result(), _run_context())
        md = render_markdown(inc)
        assert inc.severity in md
        assert inc.impact_summary in md
        assert inc.affected_job in md
