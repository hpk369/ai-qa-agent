"""Tests for agent.evidence.collect_evidence — the evidence bundle."""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import pytest

from agent import evidence as evidence_module
from agent.evidence import collect_evidence


@pytest.fixture(autouse=True)
def evidence_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(evidence_module, "EVIDENCE_DIR", tmp_path)
    return tmp_path


class TestBasicCollection:
    def test_bundle_directory_and_manifest_created(self, evidence_dir):
        bundle_dir = collect_evidence("INC-test-1")
        assert bundle_dir == evidence_dir / "INC-test-1"
        assert (bundle_dir / "manifest.json").exists()

    def test_manifest_lists_every_artifact_with_size_and_timestamp(self, evidence_dir):
        bundle_dir = collect_evidence("INC-test-2")
        manifest = json.loads((bundle_dir / "manifest.json").read_text())

        assert manifest["incident_id"] == "INC-test-2"
        assert manifest["artifacts"]
        for entry in manifest["artifacts"]:
            assert "name" in entry
            assert "status" in entry
            assert "size_bytes" in entry
            assert "collected_at" in entry

    def test_ok_artifacts_are_written_to_disk(self, evidence_dir):
        bundle_dir = collect_evidence("INC-test-3")
        manifest = json.loads((bundle_dir / "manifest.json").read_text())
        ok_entries = [e for e in manifest["artifacts"] if e["status"] == "ok"]
        assert ok_entries
        for entry in ok_entries:
            assert (evidence_dir / entry["path"].split("reports/evidence/")[-1]).exists() or \
                   (bundle_dir / entry["name"]).exists()

    def test_row_counts_and_schema_and_kafka_lag_present(self, evidence_dir):
        bundle_dir = collect_evidence("INC-test-4")
        names = {e["name"] for e in json.loads((bundle_dir / "manifest.json").read_text())["artifacts"]}
        assert {"row_counts.json", "target_schema.json", "kafka_lag.json", "disk_and_inode_snapshot.json"} <= names


class TestMissingSourcesDegradeGracefully:
    def test_no_stdout_path_reports_missing_not_a_crash(self, evidence_dir):
        bundle_dir = collect_evidence("INC-test-5", stdout_path=None)
        manifest = json.loads((bundle_dir / "manifest.json").read_text())
        stdout_entry = next(e for e in manifest["artifacts"] if e["name"] == "driver_stdout_tail.txt")
        assert stdout_entry["status"] == "missing"
        assert "note" in stdout_entry
        assert "path" not in stdout_entry  # nothing written for a missing artifact

    def test_nonexistent_stdout_file_reports_missing(self, evidence_dir):
        bundle_dir = collect_evidence("INC-test-6", stdout_path="/nonexistent/stdout.log")
        manifest = json.loads((bundle_dir / "manifest.json").read_text())
        stdout_entry = next(e for e in manifest["artifacts"] if e["name"] == "driver_stdout_tail.txt")
        assert stdout_entry["status"] == "missing"

    def test_stdout_tail_captures_last_n_lines(self, evidence_dir, tmp_path):
        stdout_file = tmp_path / "stdout.log"
        stdout_file.write_text("\n".join(f"line {i}" for i in range(300)) + "\n")

        bundle_dir = collect_evidence("INC-test-7", stdout_path=str(stdout_file))
        tail_content = (bundle_dir / "driver_stdout_tail.txt").read_text()

        assert "line 299" in tail_content
        assert "line 0" not in tail_content  # only the last 200 lines kept

    def test_collector_exception_produces_missing_entry_not_a_crash(self, monkeypatch, evidence_dir):
        def boom(*args, **kwargs):
            raise RuntimeError("tool server unreachable")

        monkeypatch.setattr(evidence_module, "SQLValidator", lambda: type("X", (), {"validate": boom})())

        bundle_dir = collect_evidence("INC-test-8")  # must not raise
        manifest = json.loads((bundle_dir / "manifest.json").read_text())
        row_counts_entry = next(e for e in manifest["artifacts"] if e["name"] == "row_counts.json")
        assert row_counts_entry["status"] == "missing"
        assert "tool server unreachable" in row_counts_entry["note"]


class TestSizeCap:
    def test_oversized_bundle_is_truncated_and_recorded(self, evidence_dir, tmp_path, monkeypatch):
        monkeypatch.setattr(evidence_module, "MAX_BUNDLE_BYTES", 1000)
        monkeypatch.setattr(evidence_module, "TRUNCATED_ARTIFACT_CAP_BYTES", 200)

        huge_stdout = tmp_path / "huge_stdout.log"
        huge_stdout.write_text("x" * 5000 + "\n")

        bundle_dir = collect_evidence("INC-test-9", stdout_path=str(huge_stdout))
        manifest = json.loads((bundle_dir / "manifest.json").read_text())

        stdout_entry = next(e for e in manifest["artifacts"] if e["name"] == "driver_stdout_tail.txt")
        assert stdout_entry["status"] == "truncated"
        assert "note" in stdout_entry
        assert stdout_entry["size_bytes"] <= 200

    def test_small_bundle_is_never_truncated(self, evidence_dir):
        bundle_dir = collect_evidence("INC-test-10")
        manifest = json.loads((bundle_dir / "manifest.json").read_text())
        assert all(e["status"] != "truncated" for e in manifest["artifacts"])
