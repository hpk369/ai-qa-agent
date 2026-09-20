"""
Tests for the log-set path: mixing a session's logs, reading errors out of
them, classifying, alerting Slack, and downloading the set.

Nothing here touches the network. The corpus fixture points at an empty
directory so background lines are generated, which also makes "clean"
genuinely clean — generated background carries no ERROR lines, whereas a
real fetched corpus legitimately does.
"""

import json
import os
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import pytest

from agent import incident as incident_module
from agent import slack_client as slack_client_module
from agent.severity import classify, load_config
from agent.slack_blocks import build_logset_reply, validate_blocks
from logsets.catalog import (
    SIGNATURES,
    SOURCES,
    format_line,
    signature_by_id,
    source_by_name,
)
from logsets.corpus import GENERATED, REAL, background_lines, corpus_status
from logsets.session import build_session, bundle, list_sessions, load_session
from logsets.triage import (
    _merge_signals,
    analyse_file,
    analyse_logset,
    score,
    triage_logset,
)

import random
from datetime import datetime, timezone


@pytest.fixture(autouse=True)
def incidents_dir(tmp_path, monkeypatch):
    path = tmp_path / "incidents"
    path.mkdir()
    monkeypatch.setattr(incident_module, "INCIDENTS_DIR", path)
    return path


@pytest.fixture(autouse=True)
def stub_dir(tmp_path, monkeypatch):
    path = tmp_path / "slack"
    monkeypatch.setattr(slack_client_module, "STUB_DIR", path)
    monkeypatch.setenv("SLACK_MODE", "stub")
    monkeypatch.setenv("SLACK_CHANNEL_ALERTS", "C_ALERTS")
    monkeypatch.setenv("SLACK_CHANNEL_P1", "C_P1")
    return path


@pytest.fixture
def root(tmp_path):
    """Where session log sets get written."""
    return tmp_path / "logsets"


@pytest.fixture
def empty_corpus(tmp_path):
    """No fetched corpus — every source falls back to generated background."""
    path = tmp_path / "corpus"
    path.mkdir()
    return path


# ---------- The signature catalogue ----------

@pytest.mark.parametrize("signature", SIGNATURES, ids=lambda s: s.id)
def test_every_signature_matches_what_it_renders(signature):
    """A signature the mixer can inject but the analyser cannot find is a
    silently undetectable failure mode — the worst kind."""
    rng = random.Random(11)
    for _ in range(25):  # render() is randomised; every variant must match
        lines = [
            format_line("spark", level, message, datetime(2026, 9, 20, 11, 0, 0), rng)
            for level, message in signature.render(rng)
        ]
        matches = [signature.pattern.search(line) for line in lines]
        assert any(matches), f"{signature.id} rendered lines no pattern matched: {lines}"
        for match in matches:
            if match:
                assert signature.to_signals(match), f"{signature.id} produced empty signals"


@pytest.mark.parametrize("signature", SIGNATURES, ids=lambda s: s.id)
def test_every_signature_signal_is_classifiable(signature):
    """Signals must be names config/severity.yml actually classifies —
    a signal nothing matches would open no incident at all."""
    rng = random.Random(5)
    config = load_config()
    for level, message in signature.render(rng):
        match = signature.pattern.search(
            format_line("spark", level, message, datetime(2026, 9, 20, 11, 0, 0), rng))
        if match:
            assert classify(signature.to_signals(match), config).severity in {"P1", "P2", "P3", "P4"}


def test_every_signature_has_at_least_one_injectable_home():
    injectable_families = {s.family for s in SOURCES if s.injectable}
    for signature in SIGNATURES:
        assert set(signature.families) & injectable_families, \
            f"{signature.id} can never be injected into any source"


def test_unknown_names_raise_with_a_useful_message():
    with pytest.raises(ValueError, match="unknown log source"):
        source_by_name("not-a-source")
    with pytest.raises(ValueError, match="unknown signature"):
        signature_by_id("SIG-999")


# ---------- Corpus ----------

def test_background_falls_back_to_generated_without_a_corpus(empty_corpus):
    lines, provider = background_lines(
        source_by_name("spark-executor"), random.Random(1), 50,
        datetime(2026, 9, 20, 11, 0, 0), empty_corpus)
    assert provider == GENERATED
    assert len(lines) == 50


def test_background_uses_real_lines_when_the_corpus_is_present(tmp_path):
    source = source_by_name("spark-executor")
    corpus = tmp_path / "corpus"
    (corpus / source.corpus_path).parent.mkdir(parents=True)
    (corpus / source.corpus_path).write_text(
        "\n".join(f"17/06/09 20:10:{n:02d} INFO executor.Executor: real line {n}"
                  for n in range(60)))
    lines, provider = background_lines(
        source, random.Random(1), 20, datetime(2026, 9, 20, 11, 0, 0), corpus)
    assert provider == REAL
    assert len(lines) == 20
    assert all("real line" in line for line in lines)
    assert corpus_status(corpus)["spark-executor"] is True


def test_generated_background_carries_no_error_lines(empty_corpus):
    """Background is background: the errors under triage are the injected
    ones, so a generated clean set must genuinely be clean."""
    for source in SOURCES:
        lines, _ = background_lines(source, random.Random(3), 80,
                                    datetime(2026, 9, 20, 11, 0, 0), empty_corpus)
        assert not [line for line in lines if "ERROR" in line], source.name


# ---------- Mixing a session's set ----------

def test_same_seed_and_anchor_reproduce_a_byte_identical_set(root, empty_corpus):
    anchor = datetime(2026, 9, 20, 11, 0, 0, tzinfo=timezone.utc)
    first = build_session(seed=4242, root=root, corpus_dir=empty_corpus,
                          session_id="A", started_at=anchor)
    second = build_session(seed=4242, root=root, corpus_dir=empty_corpus,
                           session_id="B", started_at=anchor)
    assert [f["source"] for f in first.files] == [f["source"] for f in second.files]
    for left, right in zip(first.log_paths, second.log_paths):
        assert left.read_text() == right.read_text()


def test_same_seed_reproduces_the_same_content_at_a_new_anchor(root, empty_corpus):
    """Without a fixed anchor a rebuild is dated now — but it is the same
    set: same sources, same signatures, same places."""
    first = build_session(seed=4242, root=root, corpus_dir=empty_corpus, session_id="A")
    second = build_session(seed=4242, root=root, corpus_dir=empty_corpus, session_id="B")
    assert [f["source"] for f in first.files] == [f["source"] for f in second.files]
    assert [f["line_count"] for f in first.files] == [f["line_count"] for f in second.files]
    assert ([(i["signature_id"], i["file"], i["line"]) for i in first.injected]
            == [(i["signature_id"], i["file"], i["line"]) for i in second.injected])


def test_different_seeds_produce_different_sets(root, empty_corpus):
    sets = [
        build_session(seed=seed, root=root, corpus_dir=empty_corpus, session_id=f"S{seed}")
        for seed in range(8)
    ]
    fingerprints = {
        (tuple(f["source"] for f in ls.files),
         tuple(i["signature_id"] for i in ls.injected))
        for ls in sets
    }
    assert len(fingerprints) > 1, "every session produced the same mix"


def test_a_set_always_has_something_to_triage(root, empty_corpus):
    for seed in range(12):
        logset = build_session(seed=seed, root=root, corpus_dir=empty_corpus,
                               session_id=f"T{seed}")
        etl_files = [f for f in logset.files
                     if source_by_name(f["source"]).injectable]
        assert etl_files, f"seed {seed} mixed no ETL-side source"
        assert logset.injected, f"seed {seed} injected nothing"


def test_ground_truth_line_numbers_point_at_the_injected_lines(root, empty_corpus):
    logset = build_session(seed=99, injections=4, root=root, corpus_dir=empty_corpus)
    assert logset.injected
    for item in logset.injected:
        lines = (logset.directory / item["file"]).read_text().splitlines()
        window = lines[item["line"] - 1:item["line"] - 1 + item["lines_written"]]
        signature = signature_by_id(item["signature_id"])
        assert any(signature.pattern.search(line) for line in window), item


def test_clean_sets_inject_nothing(root, empty_corpus):
    logset = build_session(seed=1, clean=True, root=root, corpus_dir=empty_corpus)
    assert logset.injected == []
    assert all(entry["injected"] == [] for entry in logset.files)


def test_explicit_sources_are_honoured(root, empty_corpus):
    logset = build_session(seed=1, sources=["kafka-consumer", "hive-metastore"],
                           root=root, corpus_dir=empty_corpus)
    assert [entry["source"] for entry in logset.files] == ["kafka-consumer", "hive-metastore"]
    for item in logset.injected:
        signature = signature_by_id(item["signature_id"])
        assert source_by_name(item["source"]).family in signature.families


def test_manifest_and_readme_are_written_and_reloadable(root, empty_corpus):
    logset = build_session(seed=77, root=root, corpus_dir=empty_corpus)
    manifest = json.loads((logset.directory / "manifest.json").read_text())
    assert manifest["session_id"] == logset.session_id
    assert manifest["seed"] == 77
    readme = (logset.directory / "README.md").read_text()
    assert f"--seed {logset.seed}" in readme

    reloaded = load_session(logset.session_id, root)
    assert reloaded.files == logset.files
    assert reloaded.injected == logset.injected
    assert logset.session_id in list_sessions(root)


def test_load_session_reports_a_missing_set(root):
    with pytest.raises(FileNotFoundError):
        load_session("LS-does-not-exist", root)


# ---------- Downloading the set ----------

def test_bundle_contains_every_log_file_the_manifest_and_the_readme(root, empty_corpus):
    logset = build_session(seed=8, root=root, corpus_dir=empty_corpus)
    archive = bundle(logset)
    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
    expected = {f"{logset.session_id}/{entry['file']}" for entry in logset.files}
    expected |= {f"{logset.session_id}/manifest.json", f"{logset.session_id}/README.md"}
    assert names == expected


def test_bundled_logs_are_byte_identical_to_what_was_triaged(root, empty_corpus):
    logset = build_session(seed=12, root=root, corpus_dir=empty_corpus)
    archive = bundle(logset, force=True)
    with zipfile.ZipFile(archive) as zf:
        for entry in logset.files:
            packed = zf.read(f"{logset.session_id}/{entry['file']}").decode()
            assert packed == (logset.directory / entry["file"]).read_text()


# ---------- Reading errors out of the logs ----------

def test_analyse_file_counts_levels_and_finds_signatures(tmp_path):
    log = tmp_path / "spark-executor.log"
    log.write_text(
        "17/06/09 20:10:40 INFO executor.Executor: Running task 3.0\n"
        "17/06/09 20:10:41 WARN executor.Executor: Partition skew detected\n"
        "17/06/09 20:10:42 ERROR executor.Executor: Row count reconciliation failed for "
        "tgt.transactions: expected 100000 rows, loaded 60000 (variance 40.00%)\n"
        "17/06/09 20:10:43 ERROR executor.Executor: something nobody catalogued\n"
    )
    result = analyse_file(log)
    assert result["error_count"] == 2
    assert result["warn_count"] == 1
    assert [f["signature_id"] for f in result["findings"]] == ["SIG-001-ROW-SHORTFALL"]
    assert result["findings"][0]["line"] == 3
    assert result["findings"][0]["signals"] == {"row_variance_pct": 40.0}
    assert [e["line"] for e in result["unmatched_errors"]] == [4]


def test_merge_signals_takes_the_worse_value_and_sums_blocked_jobs():
    signals = {}
    _merge_signals(signals, {"row_variance_pct": 3.0})
    _merge_signals(signals, {"row_variance_pct": 12.0})
    _merge_signals(signals, {"downstream_jobs_blocked": 2})
    _merge_signals(signals, {"downstream_jobs_blocked": 3})
    _merge_signals(signals, {"target_unavailable": False})
    _merge_signals(signals, {"target_unavailable": True})
    _merge_signals(signals, {"null_rate_increase_pct": {"customer_id": 4.0}})
    _merge_signals(signals, {"null_rate_increase_pct": {"customer_id": 19.0, "region_code": 2.0}})
    assert signals == {
        "row_variance_pct": 12.0,
        "downstream_jobs_blocked": 5,
        "target_unavailable": True,
        "null_rate_increase_pct": {"customer_id": 19.0, "region_code": 2.0},
    }


def test_unrecognised_errors_are_reported_not_guessed_at(root, empty_corpus):
    logset = build_session(seed=3, clean=True, root=root, corpus_dir=empty_corpus)
    target = logset.directory / logset.files[0]["file"]
    target.write_text(target.read_text() +
                      "17/06/09 20:10:44 ERROR executor.Executor: yesterday's weather\n")
    analysis = analyse_logset(logset)
    assert analysis["findings"] == []
    assert len(analysis["unmatched_errors"]) == 1
    assert analysis["signals"] == {"log_anomaly_no_data_impact": True}


def test_analysis_never_reads_the_manifest_ground_truth(root, empty_corpus):
    """The agent sees the logs, not the answer key: deleting the ground
    truth changes nothing about what it finds."""
    logset = build_session(seed=21, injections=2, root=root, corpus_dir=empty_corpus)
    before = analyse_logset(logset)
    logset.injected = []
    for entry in logset.files:
        entry["injected"] = []
    after = analyse_logset(logset)
    assert before["findings"] == after["findings"]
    assert before["signals"] == after["signals"]


def test_score_compares_findings_against_ground_truth(root, empty_corpus):
    logset = build_session(seed=64, injections=3, root=root, corpus_dir=empty_corpus)
    result = score(logset, analyse_logset(logset))
    assert result["missed"] == []
    assert result["recall"] == 1.0


# ---------- Triage end to end ----------

def test_triage_opens_an_incident_and_alerts_slack(root, empty_corpus, stub_dir, incidents_dir):
    result = triage_logset(seed=2026, sources=["spark-executor"], injections=2,
                           root=root, corpus_dir=empty_corpus)
    incident = result["incident"]
    assert incident is not None
    assert incident["severity"] in {"P1", "P2", "P3", "P4"}
    assert incident["detected_by"] == "logset-triage"
    assert result["clean"] is False

    # Persisted as the system of record...
    assert (incidents_dir / f"{incident['incident_id']}.json").exists()
    # ...and alerted: a parent message plus the log-set thread reply.
    posted = [json.loads(path.read_text())
              for path in sorted(stub_dir.glob(f"{incident['incident_id']}-*.json"))]
    parent = posted[0]
    assert parent["payload"]["channel"] == "C_ALERTS"

    replies = [p for p in posted if p["payload"].get("thread_ts")]
    assert len(replies) == 1, "the log-set breakdown should be one thread reply"
    assert replies[0]["payload"]["thread_ts"] == incident["slack_ts"]
    assert result["session_id"] in replies[0]["payload"]["text"]

    # A P1 is also mirrored to the P1 channel; nothing else is.
    mirrored = [p for p in posted if p["payload"]["channel"] == "C_P1"]
    assert len(mirrored) == (1 if incident["severity"] == "P1" else 0)


def test_triage_of_a_clean_set_opens_nothing_and_alerts_nothing(root, empty_corpus, stub_dir):
    result = triage_logset(seed=5, clean=True, root=root, corpus_dir=empty_corpus)
    assert result["incident"] is None
    assert result["clean"] is True
    assert not list(stub_dir.glob("*.json")) if stub_dir.exists() else True


def test_incident_evidence_points_at_the_downloadable_bundle(root, empty_corpus):
    result = triage_logset(seed=31, injections=2, root=root, corpus_dir=empty_corpus,
                           notify=False)
    evidence = result["incident"]["evidence"]
    archive = result["logset"]["download"]["archive"]
    assert evidence[0] == archive
    assert Path(archive).exists()
    for entry in result["logset"]["files"]:
        assert any(path.endswith(entry["file"]) for path in evidence)


def test_severity_stays_deterministic_for_a_given_seed(root, empty_corpus):
    first = triage_logset(seed=808, root=root, corpus_dir=empty_corpus,
                          session_id="one", notify=False)
    second = triage_logset(seed=808, root=root, corpus_dir=empty_corpus,
                           session_id="two", notify=False)
    assert first["signals"] == second["signals"]
    assert first["incident"]["severity"] == second["incident"]["severity"]
    assert first["incident"]["severity_rationale"] == second["incident"]["severity_rationale"]


def test_control_total_mismatch_is_a_p1_with_an_approval_gate(root, empty_corpus):
    triage_logset(seed=1, sources=["hive-metastore"], injections=0,
                  root=root, corpus_dir=empty_corpus, notify=False, session_id="ct")
    logset = load_session("ct", root)
    target = logset.directory / "hive-metastore.log"
    target.write_text(target.read_text() +
                      "2026-09-20 11:02:13,412 ERROR [HiveServer2-Handler-Pool: Thread-42] "
                      "org.apache.hive.service.cli.operation.SQLOperation: Control total "
                      "mismatch on settlement batch: source SUM(amount)=12345678.90 target "
                      "SUM(amount)=12340000.00 delta=5678.90\n")
    rerun = triage_logset(session_id="ct", root=root, corpus_dir=empty_corpus, notify=False)
    assert rerun["incident"]["severity"] == "P1"
    assert rerun["incident"]["requires_approval"] is True
    assert rerun["signals"]["control_total_mismatch"] is True


def test_reloading_a_session_by_id_does_not_remix_it(root, empty_corpus):
    first = triage_logset(seed=17, root=root, corpus_dir=empty_corpus,
                          session_id="stable", notify=False)
    contents = {entry["file"]: (root / "stable" / entry["file"]).read_text()
                for entry in first["logset"]["files"]}
    second = triage_logset(session_id="stable", root=root, corpus_dir=empty_corpus,
                           notify=False)
    assert second["logset"]["files"] == first["logset"]["files"]
    for name, text in contents.items():
        assert (root / "stable" / name).read_text() == text


# ---------- The Slack message ----------

def test_logset_reply_renders_files_findings_and_a_download_path(root, empty_corpus):
    result = triage_logset(seed=55, injections=2, root=root, corpus_dir=empty_corpus,
                           notify=False)
    blocks, text = build_logset_reply(result["logset"])
    validate_blocks(blocks)
    rendered = json.dumps(blocks)
    assert result["session_id"] in rendered
    for finding in result["logset"]["findings"][:1]:
        assert finding["signature_id"] in rendered
    assert result["logset"]["download"]["archive"] in rendered
    assert "recognised signature(s)" in text


def test_logset_reply_links_the_download_when_a_public_url_is_configured(monkeypatch, root, empty_corpus):
    monkeypatch.setenv("AGENT_PUBLIC_URL", "https://triage.example.com/")
    result = triage_logset(seed=56, injections=1, root=root, corpus_dir=empty_corpus,
                           notify=False)
    blocks, _ = build_logset_reply(result["logset"])
    buttons = [b for b in blocks if (b.get("accessory") or {}).get("type") == "button"]
    assert buttons, "no download button rendered"
    assert buttons[0]["accessory"]["url"] == (
        f"https://triage.example.com/logset/{result['session_id']}/download")


def test_logset_reply_survives_a_set_with_many_files_and_findings():
    """Slack's block limits must fail at build time, never at post time."""
    summary = {
        "session_id": "LS-big", "seed": 1, "lines_scanned": 100_000,
        "error_count": 900, "warn_count": 900,
        "files": [{"file": f"f{n}.log", "source": "spark-executor", "provider": "real",
                   "line_count": 500, "error_count": 5, "warn_count": 5} for n in range(40)],
        "findings": [{"signature_id": "SIG-001-ROW-SHORTFALL", "title": "Row shortfall",
                      "file": f"f{n}.log", "line": n} for n in range(40)],
        "unrecognised_error_count": 12,
        "download": {"archive": "/tmp/LS-big.zip", "url": ""},
    }
    blocks, _ = build_logset_reply(summary)
    validate_blocks(blocks)
    assert len(blocks) <= 50


# ---------- The HTTP surface ----------

@pytest.fixture
def client(root, empty_corpus, monkeypatch):
    from fastapi.testclient import TestClient

    from agent import agent as agent_module
    from logsets import session as session_module

    monkeypatch.setattr(session_module, "DEFAULT_ROOT", root)
    monkeypatch.setattr(agent_module, "DEFAULT_ROOT", root)
    monkeypatch.setattr("logsets.corpus.CORPUS_DIR", empty_corpus)
    return TestClient(agent_module.app)


def test_post_logset_run_returns_incident_and_download(client):
    response = client.post("/logset/run", json={"seed": 314, "injections": 2})
    assert response.status_code == 200
    body = response.json()
    assert body["session_id"].startswith("LS-")
    assert body["incident"] is not None
    assert body["logset"]["download"]["archive"].endswith(".zip")


def test_get_logset_rereads_without_alerting(client, stub_dir):
    session_id = client.post("/logset/run", json={"seed": 315, "notify": False}).json()["session_id"]
    posted_before = len(list(stub_dir.glob("*.json"))) if stub_dir.exists() else 0
    response = client.get(f"/logset/{session_id}")
    assert response.status_code == 200
    assert response.json()["logset"]["session_id"] == session_id
    posted_after = len(list(stub_dir.glob("*.json"))) if stub_dir.exists() else 0
    assert posted_after == posted_before


def test_download_endpoint_serves_the_zip(client):
    session_id = client.post("/logset/run", json={"seed": 316, "notify": False}).json()["session_id"]
    response = client.get(f"/logset/{session_id}/download")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.content[:2] == b"PK"


def test_unknown_and_unsafe_session_ids_are_refused(client):
    assert client.get("/logset/LS-nope").status_code == 404
    assert client.get("/logset/not a session id/download").status_code == 400
    assert client.post("/logset/run",
                       json={"sources": ["no-such-source"]}).status_code == 400


def test_list_endpoint_reports_built_sessions(client):
    session_id = client.post("/logset/run", json={"seed": 317, "notify": False}).json()["session_id"]
    assert session_id in client.get("/logset").json()["sessions"]


# ---------- The incident lifecycle the CLI walks ----------

def _load_cli():
    """scripts/ is not a package — load the CLI module by path."""
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "scripts" / "logset.py"
    spec = importlib.util.spec_from_file_location("logset_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_lifecycle_walks_an_incident_to_resolved(root, empty_corpus, incidents_dir):
    """--lifecycle drives the same functions the Slack buttons drive, so a
    demo run and a real approval take the same path through the record."""
    from agent.incident import load

    result = triage_logset(seed=4242, injections=2, root=root,
                           corpus_dir=empty_corpus, notify=False)
    incident_id = result["incident"]["incident_id"]

    _load_cli().run_lifecycle(incident_id, "U_TEST")

    incident = load(incident_id)
    assert incident.status == "resolved"
    assert incident.mtta_seconds is not None
    assert incident.mttr_seconds is not None
    events = [entry["event"] for entry in incident.timeline]
    assert events[0] == "opened"
    assert "approval_decision" in events
    decision = next(e for e in incident.timeline if e["event"] == "approval_decision")
    assert "approved" in decision["detail"]
    assert decision["actor"] == "U_TEST"
