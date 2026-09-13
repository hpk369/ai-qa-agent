"""
Tests for scripts/demo_incident.py — the demo/verification tool for
project demonstrations and real-Slack-setup checks. SLACK_MODE=stub
throughout (the default), so these are exercising the exact same code
path a real demo run does, just without a live workspace.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../scripts"))

import pytest

from agent import evidence as evidence_module
from agent import incident as incident_module
from agent import slack_client as slack_client_module

import demo_incident


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(incident_module, "INCIDENTS_DIR", tmp_path / "incidents")
    monkeypatch.setattr(evidence_module, "EVIDENCE_DIR", tmp_path / "evidence")
    monkeypatch.setattr(slack_client_module, "STUB_DIR", tmp_path / "slack")
    return tmp_path


class TestCleanRun:
    def test_none_mode_opens_no_incident(self, capsys):
        exit_code = demo_incident.main(["none"])
        assert exit_code == 0
        assert "CLEAN RUN" in capsys.readouterr().out

    def test_lifecycle_flag_is_a_harmless_no_op_on_clean_run(self, capsys):
        exit_code = demo_incident.main(["none", "--lifecycle"])
        assert exit_code == 0
        assert "nothing to walk through" in capsys.readouterr().out


class TestEachFailureMode:
    @pytest.mark.parametrize(
        "mode,expected_severity",
        [
            ("row_drop", "P2"),
            ("schema_drift", "P1"),
            ("null_spike", "P3"),
            ("latency", "P2"),
        ],
    )
    def test_opens_incident_with_expected_severity(self, mode, expected_severity, capsys, isolated_dirs):
        exit_code = demo_incident.main([mode])
        assert exit_code == 0
        output = capsys.readouterr().out
        assert f"severity={expected_severity}" in output
        assert (isolated_dirs / "incidents").glob("INC-*.json")

    def test_posts_to_stub_slack(self, isolated_dirs):
        demo_incident.main(["row_drop"])
        stub_files = list((isolated_dirs / "slack").glob("*.json"))
        assert stub_files


class TestLifecycleFlag:
    def test_walks_incident_to_resolved(self, isolated_dirs, capsys):
        demo_incident.main(["row_drop", "--lifecycle", "--actor", "U_TEST"])
        output = capsys.readouterr().out
        assert "ACKNOWLEDGED by U_TEST" in output
        assert "APPROVED by U_TEST" in output
        assert "VERIFYING" in output
        assert "RESOLVED" in output

        incident_files = list((isolated_dirs / "incidents").glob("*.json"))
        assert len(incident_files) == 1
        persisted = json.loads(incident_files[0].read_text())
        assert persisted["status"] == "resolved"
        assert persisted["mtta_seconds"] is not None
        assert persisted["mttr_seconds"] is not None

    def test_json_flag_prints_valid_incident_json(self, isolated_dirs, capsys):
        demo_incident.main(["row_drop", "--lifecycle", "--json"])
        output = capsys.readouterr().out
        json_start = output.index("{")
        parsed = json.loads(output[json_start:])
        assert parsed["status"] == "resolved"


class TestInvalidMode:
    def test_unknown_mode_exits_nonzero(self):
        with pytest.raises(SystemExit) as exc_info:
            demo_incident.main(["not_a_real_mode"])
        assert exc_info.value.code != 0
