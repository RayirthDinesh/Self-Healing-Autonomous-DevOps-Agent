"""run_tracker: lifecycle, ordering, and the never-fatal contract."""

import json

import pytest

import run_tracker


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_DB", str(tmp_path / "memory.db"))
    run_tracker.set_current_run(None)
    run_tracker.set_current_node("")
    yield


def test_run_lifecycle_records_status_and_outcome():
    run_id = run_tracker.start_run("acme/app", "bug/x", "abc1234", source="replay")
    assert run_tracker.current_run() == run_id

    run = run_tracker.get_run(run_id)
    assert run["status"] == "running"
    assert run["repo"] == "acme/app"
    assert run["source"] == "replay"
    assert run["finished_at"] is None

    run_tracker.finish_run("passed", "published", pr_url="https://pr/1", attempts=2)
    run = run_tracker.get_run(run_id)
    assert (run["status"], run["outcome"]) == ("passed", "published")
    assert run["pr_url"] == "https://pr/1"
    assert run["attempts"] == 2
    assert run["finished_at"] is not None


def test_events_are_sequenced_and_detail_round_trips():
    run_id = run_tracker.start_run("acme/app")
    run_tracker.step("ingest", phase="start")
    run_tracker.step("ingest", detail={"error_class": "type-error"}, duration_ms=12.7)
    run_tracker.step("fixer", status="error", detail={"error": "boom"})

    events = run_tracker.get_events(run_id)
    assert [e["seq"] for e in events] == [1, 2, 3]
    assert events[1]["detail"] == {"error_class": "type-error"}
    assert events[1]["duration_ms"] == 12
    assert events[2]["status"] == "error"

    # after_seq lets the SSE endpoint tail only what is new
    assert [e["seq"] for e in run_tracker.get_events(run_id, after_seq=2)] == [3]


def test_artifacts_index_excludes_body_and_fetch_returns_it():
    run_id = run_tracker.start_run("acme/app")
    art_id = run_tracker.artifact("diff", "attempt 1", "--- a\n+++ b\n", node="validator")

    index = run_tracker.get_artifact_index(run_id)
    assert len(index) == 1
    assert "body" not in index[0]
    assert index[0]["kind"] == "diff" and index[0]["node"] == "validator"
    assert index[0]["size"] == len("--- a\n+++ b\n")

    assert run_tracker.get_artifact(art_id)["body"] == "--- a\n+++ b\n"


def test_artifact_serializes_dicts_and_caps_huge_bodies():
    run_tracker.start_run("acme/app")
    art_id = run_tracker.artifact("proposed_fix", "src/a.py",
                                  {"filename": "src/a.py", "search": "x", "replace": "y"})
    assert json.loads(run_tracker.get_artifact(art_id)["body"])["replace"] == "y"

    big = run_tracker.artifact("test_output", "attempt 1", "x" * (run_tracker._BODY_CAP + 500))
    body = run_tracker.get_artifact(big)["body"]
    assert len(body) < run_tracker._BODY_CAP + 200
    assert body.endswith("chars]")


def test_llm_call_records_node_model_and_latency():
    run_tracker.start_run("acme/app")
    run_tracker.set_current_node("triage")
    art_id = run_tracker.llm_call("some/model", "prompt text", "response text", 1234)

    call = json.loads(run_tracker.get_artifact(art_id)["body"])
    assert call["node"] == "triage"
    assert call["model"] == "some/model"
    assert call["latency_ms"] == 1234
    assert call["prompt"] == "prompt text"


def test_tracked_decorator_emits_start_end_and_summary():
    run_id = run_tracker.start_run("acme/app")

    @run_tracker.tracked("localizer", lambda state, update: {"files": update["candidate_files"]})
    def node(state):
        assert run_tracker.current_node() == "localizer"
        return {"candidate_files": ["src/a.py"]}

    assert node({"attempt": 1})["candidate_files"] == ["src/a.py"]
    events = run_tracker.get_events(run_id)
    assert [(e["node"], e["phase"], e["status"]) for e in events] == [
        ("localizer", "start", "ok"), ("localizer", "end", "ok")]
    assert events[1]["detail"] == {"files": ["src/a.py"]}
    assert events[1]["duration_ms"] is not None


def test_tracked_decorator_records_failure_and_reraises():
    run_id = run_tracker.start_run("acme/app")

    @run_tracker.tracked("fixer")
    def node(state):
        raise ValueError("nope")

    with pytest.raises(ValueError):
        node({})

    end = run_tracker.get_events(run_id)[-1]
    assert end["status"] == "error"
    assert "ValueError: nope" in end["detail"]["error"]


def test_close_if_running_only_touches_unfinished_runs():
    crashed = run_tracker.start_run("acme/app")
    run_tracker.close_if_running(outcome="pipeline crashed")
    run = run_tracker.get_run(crashed)
    assert (run["status"], run["outcome"]) == ("error", "pipeline crashed")

    done = run_tracker.start_run("acme/app")
    run_tracker.finish_run("passed", "published")
    run_tracker.close_if_running(outcome="pipeline crashed")
    assert run_tracker.get_run(done)["outcome"] == "published"


def test_untracked_calls_are_noops_without_a_run():
    run_tracker.set_current_run(None)
    assert run_tracker.step("ingest") is None
    assert run_tracker.artifact("diff", "x", "y") is None


def test_tracking_never_raises_when_the_db_is_unusable(monkeypatch, tmp_path):
    # A directory where the DB file should be: every sqlite call fails
    bad = tmp_path / "not-a-file"
    bad.mkdir()
    monkeypatch.setenv("MEMORY_DB", str(bad))

    assert run_tracker.start_run("acme/app") is None
    assert run_tracker.step("ingest") is None
    assert run_tracker.list_runs() == []
    assert run_tracker.get_run("whatever") is None
