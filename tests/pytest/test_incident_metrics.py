"""Tests for scripts/incident_metrics.py."""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../scripts"))

from incident_metrics import compute_metrics, load_incidents, main


def _incident(**overrides) -> dict:
    base = {
        "incident_id": "INC-x",
        "severity": "P2",
        "status": "resolved",
        "mtta_seconds": 60,
        "mttr_seconds": 3600,
    }
    base.update(overrides)
    return base


class TestComputeMetricsCounts:
    def test_counts_by_severity(self):
        incidents = [_incident(severity="P1"), _incident(severity="P1"), _incident(severity="P3")]
        metrics = compute_metrics(incidents)
        assert metrics["by_severity"] == {"P1": 2, "P2": 0, "P3": 1, "P4": 0}
        assert metrics["total"] == 3

    def test_empty_incident_list(self):
        metrics = compute_metrics([])
        assert metrics["total"] == 0
        assert metrics["mtta"]["median_seconds"] is None
        assert metrics["false_positive_rate"] is None


class TestMedianAndP90:
    def test_median_and_p90_computed_over_incidents_with_a_value(self):
        incidents = [_incident(mtta_seconds=v) for v in [10, 20, 30, 40, 50]]
        metrics = compute_metrics(incidents)
        assert metrics["mtta"]["median_seconds"] == 30
        assert metrics["mtta"]["n"] == 5

    def test_incidents_missing_mtta_are_excluded_not_treated_as_zero(self):
        incidents = [_incident(mtta_seconds=100), _incident(mtta_seconds=None)]
        metrics = compute_metrics(incidents)
        assert metrics["mtta"]["n"] == 1
        assert metrics["mtta"]["median_seconds"] == 100

    def test_single_value_is_its_own_median_and_p90(self):
        metrics = compute_metrics([_incident(mttr_seconds=500)])
        assert metrics["mttr"]["median_seconds"] == 500
        assert metrics["mttr"]["p90_seconds"] == 500


class TestFalsePositiveRate:
    def test_rate_counts_false_positive_status(self):
        incidents = [
            _incident(status="resolved"),
            _incident(status="false_positive"),
            _incident(status="false_positive"),
            _incident(status="open"),
        ]
        metrics = compute_metrics(incidents)
        assert metrics["false_positive_count"] == 2
        assert metrics["false_positive_rate"] == 0.5

    def test_zero_false_positives_is_zero_not_none(self):
        metrics = compute_metrics([_incident(status="resolved")])
        assert metrics["false_positive_rate"] == 0.0


class TestLoadIncidentsAndMain:
    def test_load_incidents_reads_all_json_files(self, tmp_path):
        (tmp_path / "INC-1.json").write_text(json.dumps(_incident(incident_id="INC-1")))
        (tmp_path / "INC-2.json").write_text(json.dumps(_incident(incident_id="INC-2")))
        (tmp_path / "INC-1.md").write_text("# not a json file")  # markdown sidecar, must be ignored

        incidents = load_incidents(tmp_path)

        assert len(incidents) == 2

    def test_main_returns_zero_on_success(self, tmp_path, capsys):
        (tmp_path / "INC-1.json").write_text(json.dumps(_incident()))
        exit_code = main([str(tmp_path)])
        assert exit_code == 0
        assert "Incidents analysed: 1" in capsys.readouterr().out

    def test_main_json_flag_prints_valid_json(self, tmp_path, capsys):
        (tmp_path / "INC-1.json").write_text(json.dumps(_incident()))
        main([str(tmp_path), "--json"])
        output = capsys.readouterr().out
        parsed = json.loads(output)
        assert parsed["total"] == 1

    def test_main_on_missing_directory_returns_nonzero(self, tmp_path):
        exit_code = main([str(tmp_path / "does-not-exist")])
        assert exit_code != 0

    def test_main_on_empty_directory_reports_zero_incidents(self, tmp_path, capsys):
        exit_code = main([str(tmp_path)])
        assert exit_code == 0
        assert "Incidents analysed: 0" in capsys.readouterr().out
