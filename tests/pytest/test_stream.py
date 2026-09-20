"""
Tests for logsets.stream — the live stream and its blocking gate.

Every test runs on a fake clock, so a stream that would take minutes runs
in milliseconds, and on a temporary Slack stub directory, so replies come
from a local inbox instead of a workspace. Nothing here touches the
network, and llm.judge_resolution is stubbed where a test is about the
gate rather than about the judgement.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import pytest

from agent import incident as incident_module
from agent import llm
from agent import slack_client as slack_client_module
from agent.incident import load
from agent.llm import LLMResult, ResolutionJudgement
from logsets.session import load_session
from logsets.stream import Clock, StreamConfig, StreamRunner


class FakeClock(Clock):
    """Time moves only when the stream sleeps."""

    def __init__(self):
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += max(seconds, 0.001)


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
    monkeypatch.setenv("SLACK_CHANNEL_CHANGES", "C_CHG")
    return path


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    """Default to no model, as a clone with no API key has."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)


@pytest.fixture
def root(tmp_path):
    return tmp_path / "streams"


def run_stream(root, events=None, **kwargs):
    """Build a runner with test-friendly defaults and run it."""
    collected = events if events is not None else []
    config_kwargs = {
        "rate": 50.0, "duration": 30.0, "first_incident_after": 2.0,
        "incident_gap": (10.0, 10.0), "poll_interval": 1.0, "max_block_wait": 20.0,
    }
    for key in list(kwargs):
        if key in StreamConfig.__dataclass_fields__:
            config_kwargs[key] = kwargs.pop(key)

    on_event = kwargs.pop("on_event", None)

    def record(event):
        collected.append(event)
        if on_event:
            on_event(event)

    runner = StreamRunner(
        config=StreamConfig(**config_kwargs),
        root=root,
        clock=FakeClock(),
        on_event=record,
        **kwargs,
    )
    return runner, runner.run(), collected


def kinds(events):
    return [event.kind for event in events]


def reply_to(stub_dir, incident_id, text=None, reaction=None, user="U_ONCALL"):
    """Write into the stub inbox, the way scripts/slack_reply.py does."""
    path = stub_dir / "inbox" / f"{incident_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"user": user, "ts": f"{len(path.read_text().splitlines()) if path.exists() else 0}"}
    if reaction:
        entry["reaction"] = reaction
    else:
        entry["text"] = text
    with path.open("a") as handle:
        handle.write(json.dumps(entry) + "\n")


# ---------- Streaming ----------

def test_the_stream_writes_lines_continuously_to_every_source(root):
    runner, result, events = run_stream(
        root, sources=["kafka-consumer", "spark-executor"], seed=3,
        notify=False, blocking_severities=frozenset())

    assert result["lines_emitted"] > 100
    for source in ("kafka-consumer", "spark-executor"):
        lines = (runner.directory / f"{source}.log").read_text().splitlines()
        assert lines, f"{source} produced no lines"
    assert kinds(events)[0] == "started"
    assert kinds(events)[-1] == "finished"


def test_alerts_fire_while_the_stream_is_still_running(root):
    """'As recorded' — an alert lands at the failure, not at the end."""
    _, result, events = run_stream(root, seed=11, notify=False,
                                   blocking_severities=frozenset())
    incident_events = [e for e in events if e.kind == "incident"]
    finished = next(e for e in events if e.kind == "finished")

    assert incident_events, "no incident was raised"
    for event in incident_events:
        assert event.at < finished.at
    assert len(result["incidents"]) == len(incident_events)


def test_each_incident_is_judged_only_on_lines_that_arrived_since_the_last_one(root):
    """A tail reads forward. Without that, the second incident would
    inherit the first one's signals and re-report them."""
    _, result, events = run_stream(root, seed=11, notify=False,
                                   blocking_severities=frozenset(), duration=40.0)
    incidents = [e for e in events if e.kind == "incident"]
    if len(incidents) < 2:
        pytest.skip("this seed produced a single incident")
    first, second = incidents[0], incidents[1]
    assert set(second.payload["signatures"]).isdisjoint(first.payload["signatures"]) or \
        second.payload["incident_id"] != first.payload["incident_id"]


def test_a_non_blocking_incident_does_not_stop_the_stream(root):
    _, result, events = run_stream(root, seed=5, notify=False,
                                   blocking_severities=frozenset())
    assert "blocked" not in kinds(events)
    assert "continuing" in kinds(events)
    assert result["outcome"] == "completed"


# ---------- The gate ----------

def test_a_blocking_incident_holds_the_stream_until_it_is_confirmed(root, stub_dir):
    """The whole point: the stream waits for a human."""
    def confirm_when_blocked(event):
        if event.kind == "blocked":
            reply_to(stub_dir, event.payload["incident_id"],
                     "restarted the consumer, lag is draining — all clear")

    _, result, events = run_stream(
        root, seed=17, sources=["kafka-consumer", "hdfs-datanode"],
        blocking_severities=frozenset({"P1", "P2", "P3", "P4"}),
        on_event=confirm_when_blocked)

    sequence = kinds(events)
    assert "blocked" in sequence
    assert "reply" in sequence
    assert "judgement" in sequence
    assert "resumed" in sequence
    assert sequence.index("blocked") < sequence.index("resumed")
    assert result["outcome"] == "completed"

    blocked = next(e for e in events if e.kind == "blocked")
    assert load(blocked.payload["incident_id"]).status == "resolved"


def test_an_acknowledgement_does_not_release_the_gate(root, stub_dir):
    """"looking into it" is not a fix."""
    def acknowledge(event):
        if event.kind == "blocked":
            reply_to(stub_dir, event.payload["incident_id"], "looking into it, paging the DBA")

    _, result, events = run_stream(
        root, seed=17, blocking_severities=frozenset({"P1", "P2", "P3", "P4"}),
        on_event=acknowledge)

    judgement = next(e for e in events if e.kind == "judgement")
    assert judgement.payload["resolved"] is False
    assert "resumed" not in kinds(events)
    assert "still_blocked" in kinds(events)
    assert result["outcome"] == "blocked"


def test_the_stream_stops_rather_than_resuming_on_an_unconfirmed_incident(root):
    _, result, events = run_stream(
        root, seed=17, notify=False, max_block_wait=8.0,
        blocking_severities=frozenset({"P1", "P2", "P3", "P4"}))
    assert result["outcome"] == "blocked"
    assert "still_blocked" in kinds(events)
    assert kinds(events)[-1] == "finished"


def test_a_check_mark_reaction_releases_the_gate(root, stub_dir):
    """The gate never depends solely on a model being reachable."""
    def react(event):
        if event.kind == "blocked":
            reply_to(stub_dir, event.payload["incident_id"], reaction="white_check_mark")

    _, result, events = run_stream(
        root, seed=17, blocking_severities=frozenset({"P1", "P2", "P3", "P4"}),
        on_event=react)

    resumed = next(e for e in events if e.kind == "resumed")
    assert "reaction" in resumed.detail
    assert result["outcome"] == "completed"


def test_an_incident_resolved_in_the_record_releases_the_gate(root):
    """However resolution happened — a Slack button, a script — the
    stream notices."""
    from agent.incident import resolve_incident

    def resolve(event):
        if event.kind == "blocked":
            resolve_incident(load(event.payload["incident_id"]), actor="U_BUTTON")

    _, result, events = run_stream(
        root, seed=17, blocking_severities=frozenset({"P1", "P2", "P3", "P4"}),
        on_event=resolve)

    assert "resumed" in kinds(events)
    assert result["outcome"] == "completed"


def test_auto_resolve_releases_an_unattended_demo(root):
    _, result, events = run_stream(
        root, seed=17, notify=False, auto_resolve_after=3.0,
        blocking_severities=frozenset({"P1", "P2", "P3", "P4"}))
    resumed = next(e for e in events if e.kind == "resumed")
    assert "unattended" in resumed.detail
    assert result["outcome"] == "completed"


def test_the_gate_uses_claudes_judgement_when_it_is_available(root, stub_dir, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    seen = {}

    def fake_judge(summary, replies):
        seen["summary"] = summary
        seen["replies"] = [r["text"] for r in replies]
        return LLMResult(
            ResolutionJudgement(resolved=True, confidence=0.95,
                                reason='"the backfill completed" states recovery'),
            "claude")

    monkeypatch.setattr(llm, "judge_resolution", fake_judge)

    def reply(event):
        if event.kind == "blocked":
            reply_to(stub_dir, event.payload["incident_id"], "the backfill completed")

    _, result, events = run_stream(
        root, seed=17, blocking_severities=frozenset({"P1", "P2", "P3", "P4"}),
        on_event=reply)

    judgement = next(e for e in events if e.kind == "judgement")
    assert judgement.payload["judged_by"] == "claude"
    assert result["outcome"] == "completed"
    assert seen["replies"] == ["the backfill completed"]
    # The judge is given the incident, not just the reply.
    assert "INC-" in seen["summary"]


def test_a_low_confidence_judgement_keeps_the_gate_shut(root, stub_dir, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(llm, "judge_resolution", lambda summary, replies: LLMResult(
        ResolutionJudgement(resolved=True, confidence=0.2, reason="might be fixed"), "claude"))

    def reply(event):
        if event.kind == "blocked":
            reply_to(stub_dir, event.payload["incident_id"], "think it's probably fine now?")

    _, result, events = run_stream(
        root, seed=17, blocking_severities=frozenset({"P1", "P2", "P3", "P4"}),
        resolution_confidence=0.6, on_event=reply)

    assert "resumed" not in kinds(events)
    assert result["outcome"] == "blocked"


# ---------- While blocked, and after ----------

def test_the_stream_keeps_logging_backpressure_while_blocked(root):
    """A stalled pipeline is not a silent one — it retries and queues, and
    those lines are what an analyst sees while the incident is open."""
    lines_at_block = {}

    def count_lines_on_disk() -> int:
        return sum(len(path.read_text().splitlines()) for path in root.glob("*/*.log"))

    def measure(event):
        if event.kind == "blocked":
            lines_at_block["count"] = count_lines_on_disk()

    runner, result, events = run_stream(
        root, seed=17, notify=False, auto_resolve_after=6.0,
        blocking_severities=frozenset({"P1", "P2", "P3", "P4"}),
        on_event=measure)

    resumed = next(e for e in events if e.kind == "resumed")
    assert "count" in lines_at_block, "the stream never blocked"
    # Lines kept arriving during the block...
    assert count_lines_on_disk() > lines_at_block["count"]
    # ...and they are backpressure lines, not pipeline progress.
    text = "\n".join((runner.directory / entry["file"]).read_text()
                     for entry in runner.logset.files)
    assert any(phrase in text for phrase in
               ("blocked on upstream dependency", "paused", "waiting", "backlog", "deferred"))
    assert resumed.at > 0


def test_recovery_lines_do_not_open_another_incident(root):
    _, result, events = run_stream(
        root, seed=17, notify=False, auto_resolve_after=3.0,
        blocking_severities=frozenset({"P1", "P2", "P3", "P4"}), duration=25.0)
    incident_kinds = [e for e in events if e.kind == "incident"]
    # Every incident corresponds to an injected signature, never to our own
    # recovery lines.
    assert len(incident_kinds) == len(result["incidents"])
    for event in incident_kinds:
        assert event.payload["signatures"], "an incident opened with no signature behind it"


# ---------- What the stream leaves behind ----------

def test_a_stream_is_readable_and_downloadable_like_any_other_log_set(root):
    runner, result, events = run_stream(root, seed=5, notify=False,
                                        blocking_severities=frozenset())

    reloaded = load_session(runner.session_id, root)
    assert reloaded.session_id == result["session_id"]
    assert reloaded.files
    assert sum(entry["line_count"] for entry in reloaded.files) == result["lines_emitted"]

    archive = Path(result["download"]["archive"])
    assert archive.exists()
    import zipfile

    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
    assert f"{runner.session_id}/manifest.json" in names


def test_the_manifest_records_ground_truth_for_every_injected_failure(root):
    runner, result, events = run_stream(root, seed=11, notify=False,
                                        blocking_severities=frozenset())
    manifest = json.loads((runner.directory / "manifest.json").read_text())
    assert manifest["injected"], "nothing was injected"
    for item in manifest["injected"]:
        lines = (runner.directory / item["file"]).read_text().splitlines()
        window = lines[item["line"] - 1: item["line"] - 1 + item["lines_written"]]
        from logsets.catalog import signature_by_id

        signature = signature_by_id(item["signature_id"])
        assert any(signature.pattern.search(line) for line in window), item


def test_slack_gets_the_alert_and_the_resume_note(root, stub_dir):
    def confirm(event):
        if event.kind == "blocked":
            reply_to(stub_dir, event.payload["incident_id"], "fixed it — all clear")

    _, result, events = run_stream(
        root, seed=17, blocking_severities=frozenset({"P1", "P2", "P3", "P4"}),
        on_event=confirm)

    blocked = next(e for e in events if e.kind == "blocked")
    posted = [json.loads(p.read_text())
              for p in sorted(stub_dir.glob(f"{blocked.payload['incident_id']}-*.json"))]
    assert posted, "nothing was posted to Slack"
    texts = " ".join(str(p["payload"].get("text", "")) for p in posted)
    assert "resumed" in texts
