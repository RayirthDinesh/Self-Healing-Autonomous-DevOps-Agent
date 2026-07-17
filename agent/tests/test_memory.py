"""Tests for the SQLite memory — error classes, incidents, PR fates, blame."""

import json

import numpy as np
import pytest

import memory


@pytest.fixture(autouse=True)
def tmp_db(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMORY_DB", str(tmp_path / "memory.db"))


@pytest.fixture
def no_embeddings(monkeypatch):
    monkeypatch.setattr(memory, "_embed", lambda text: None)


NAME_LOG = "E       NameError: name 'totl' is not defined"
TYPE_LOG = "E       TypeError: unsupported operand type(s) for +: 'int' and 'str'"
ASSERT_LOG = "E       assert 100.0 == 500.0"
PIP_LOG = "ERROR: No matching distribution found for pandas==0.24.0"
IMPORT_LOG = "E   ImportError: cannot import name 'add' from 'src.core'"


def test_classify_error_buckets():
    assert memory.classify_error(NAME_LOG) == "name-error"
    assert memory.classify_error(TYPE_LOG) == "type-error"
    assert memory.classify_error(ASSERT_LOG) == "assertion-value"
    assert memory.classify_error(PIP_LOG) == "install-failure"
    assert memory.classify_error(IMPORT_LOG) == "import-error"
    assert memory.classify_error("something exploded") == "other"


def test_record_and_recall_incident_by_error_class(no_embeddings):
    memory.record_incident(
        "o/r", "bug/x", "sha1", NAME_LOG, "typo in variable name",
        ["src/aggregator.py"], "diff text", suite_green=True, attempt=1,
    )
    found = memory.similar_incidents("o/r", NAME_LOG)
    assert len(found) == 1
    assert found[0]["diagnosis"] == "typo in variable name"
    assert found[0]["files_fixed"] == ["src/aggregator.py"]


def test_validator_output_stored_for_postmortem(no_embeddings):
    memory.record_incident(
        "o/r", "b", "s", NAME_LOG, "d", ["src/a.py"], "diff",
        suite_green=False, attempt=1,
        validator_output="TimeoutError: The read operation timed out",
    )
    with memory._connect() as conn:
        row = conn.execute("SELECT validator_output FROM incidents").fetchone()
    assert "TimeoutError" in row[0]


def test_failed_attempts_not_recalled_as_examples(no_embeddings):
    memory.record_incident(
        "o/r", "bug/x", "sha1", NAME_LOG, "wrong guess",
        ["src/a.py"], "diff", suite_green=False, attempt=1,
    )
    assert memory.similar_incidents("o/r", NAME_LOG) == []


def test_recall_uses_cosine_similarity_and_threshold(monkeypatch):
    vecs = {
        "close": np.array([1.0, 0.0], dtype="float32"),
        "far": np.array([0.0, 1.0], dtype="float32"),
    }
    monkeypatch.setattr(
        memory, "_embed",
        lambda text: vecs["far"] if "unrelated" in text else vecs["close"],
    )
    memory.record_incident("o/r", "b", "s", NAME_LOG, "match me",
                           ["src/a.py"], "d", suite_green=True, attempt=1)
    memory.record_incident("o/r", "b", "s", "unrelated failure", "not me",
                           ["src/b.py"], "d", suite_green=True, attempt=1)

    found = memory.similar_incidents("o/r", NAME_LOG)
    assert [inc["diagnosis"] for inc in found] == ["match me"]


def test_recall_trims_long_diffs(no_embeddings):
    long_diff = "\n".join(f"line {i}" for i in range(200))
    memory.record_incident("o/r", "b", "s", NAME_LOG, "d",
                           ["src/a.py"], long_diff, suite_green=True, attempt=1)
    found = memory.similar_incidents("o/r", NAME_LOG)
    assert len(found[0]["fix_diff"].splitlines()) <= 40


def test_pr_fate_merged_builds_blame(no_embeddings, monkeypatch):
    incident_id = memory.record_incident(
        "o/r", "b", "s", NAME_LOG, "d", ["src/aggregator.py"], "diff",
        suite_green=True, attempt=1,
    )
    memory.set_incident_pr(incident_id, "https://github.com/o/r/pull/48")

    class FakeResponse:
        def raise_for_status(self):
            pass
        def json(self):
            return {"merged": True, "state": "closed"}
    monkeypatch.setattr(memory.requests, "get", lambda *a, **k: FakeResponse())

    resolved = memory.update_pr_fates("o/r", "tok")
    assert resolved == {48: "merged"}
    assert memory.blame_scores("o/r", "name-error") == {"src/aggregator.py": 1.0}
    # sweep is lazy: second call finds nothing open
    assert memory.update_pr_fates("o/r", "tok") == {}


def test_pr_fate_closed_decays_blame_and_excludes_incident(no_embeddings, monkeypatch):
    fates = iter([{"merged": True, "state": "closed"},
                  {"merged": False, "state": "closed"}])

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload
        def raise_for_status(self):
            pass
        def json(self):
            return self.payload
    monkeypatch.setattr(memory.requests, "get",
                        lambda *a, **k: FakeResponse(next(fates)))

    first = memory.record_incident("o/r", "b", "s", NAME_LOG, "good",
                                   ["src/a.py"], "d", suite_green=True, attempt=1)
    memory.set_incident_pr(first, "https://github.com/o/r/pull/1")
    memory.update_pr_fates("o/r", "tok")          # merged: weight 1.0

    second = memory.record_incident("o/r", "b", "s", NAME_LOG, "rejected",
                                    ["src/a.py"], "d", suite_green=True, attempt=1)
    memory.set_incident_pr(second, "https://github.com/o/r/pull/2")
    memory.update_pr_fates("o/r", "tok")          # closed: weight 1.0 -> 0.5

    assert memory.blame_scores("o/r", "name-error") == {"src/a.py": 1.0}  # normalized
    diagnoses = [inc["diagnosis"] for inc in memory.similar_incidents("o/r", NAME_LOG)]
    assert "rejected" not in diagnoses


def test_blame_scores_normalized(no_embeddings):
    with memory._connect() as conn:
        conn.execute("INSERT INTO blame VALUES ('o/r', 'src/a.py', 'name-error', 4.0)")
        conn.execute("INSERT INTO blame VALUES ('o/r', 'src/b.py', 'name-error', 1.0)")
    assert memory.blame_scores("o/r", "name-error") == {"src/a.py": 1.0, "src/b.py": 0.25}
    assert memory.blame_scores("o/r", "type-error") == {}


def test_update_repo_state_tracks_churn_and_co_change(tmp_path):
    clone = tmp_path / "clone"
    (clone / "src").mkdir(parents=True)
    (clone / "src" / "a.py").write_text("x = 1\n")
    (clone / "src" / "b.py").write_text("y = 1\n")
    repo_map = {"files": {
        "src/a.py": {"size": 6, "symbols": []},
        "src/b.py": {"size": 6, "symbols": []},
    }}

    # first sync = baseline, nothing counts as "changed"
    assert memory.update_repo_state("o/r", "sha1", str(clone), repo_map) == []

    (clone / "src" / "a.py").write_text("x = 2\n")
    (clone / "src" / "b.py").write_text("y = 2\n")
    changed = memory.update_repo_state("o/r", "sha2", str(clone), repo_map)
    assert sorted(changed) == ["src/a.py", "src/b.py"]

    with memory._connect() as conn:
        times = dict(conn.execute("SELECT path, times_changed FROM file_history"))
        assert times == {"src/a.py": 1, "src/b.py": 1}
        pairs = conn.execute("SELECT path_a, path_b, count FROM co_change").fetchall()
        assert pairs == [("src/a.py", "src/b.py", 1)]
        state = conn.execute("SELECT commit_sha FROM repo_state WHERE repo='o/r'").fetchone()
        assert state == ("sha2",)


def test_memory_failure_never_raises(monkeypatch):
    def boom():
        raise RuntimeError("db gone")
    monkeypatch.setattr(memory, "_connect", boom)
    monkeypatch.setattr(memory, "_embed", lambda text: None)
    assert memory.similar_incidents("o/r", NAME_LOG) == []
    assert memory.blame_scores("o/r", "name-error") == {}
    assert memory.update_pr_fates("o/r", "tok") == {}
    assert memory.record_incident("o/r", "b", "s", NAME_LOG, "d", [], "",
                                  suite_green=True, attempt=1) is None


TRACEBACK_LOG = (
    "FAILED tests/test_app.py::test_add - NameError\n"
    'File "src/core.py", line 3\n'
    "E       NameError: name 'totl' is not defined\n"
)


def test_failure_signature_composition():
    sig = memory.failure_signature(TRACEBACK_LOG)
    assert sig == "name-error|tests/test_app.py|src/core.py"
    # same shape -> same key; different error text -> same key too (shape-based)
    assert memory.failure_signature(TRACEBACK_LOG + "\nnoise") == sig


def _merge_incident(monkeypatch, n):
    """Record an incident for TRACEBACK_LOG, open PR n, sweep it as merged."""
    class FakeResponse:
        def raise_for_status(self):
            pass
        def json(self):
            return {"merged": True, "state": "closed"}
    monkeypatch.setattr(memory.requests, "get", lambda *a, **k: FakeResponse())
    incident_id = memory.record_incident(
        "o/r", "b", "s", TRACEBACK_LOG, "typo", ["src/core.py"], "diff",
        suite_green=True, attempt=1,
    )
    memory.set_incident_pr(incident_id, f"https://github.com/o/r/pull/{n}")
    memory.update_pr_fates("o/r", "tok")


def test_fast_path_needs_two_merged_prs(no_embeddings, monkeypatch):
    _merge_incident(monkeypatch, 1)
    assert memory.fast_path_lookup("o/r", TRACEBACK_LOG) is None  # 1 merge: not proven

    _merge_incident(monkeypatch, 2)
    fp = memory.fast_path_lookup("o/r", TRACEBACK_LOG)
    assert fp == {"signature": "name-error|tests/test_app.py|src/core.py",
                  "target_files": ["src/core.py"]}
    # different failure shape does not fire
    assert memory.fast_path_lookup("o/r", "E  TypeError: boom") is None


def test_fast_path_demoted_after_two_misses(no_embeddings, monkeypatch):
    _merge_incident(monkeypatch, 1)
    _merge_incident(monkeypatch, 2)
    sig = memory.failure_signature(TRACEBACK_LOG)
    memory.fast_path_miss("o/r", sig)
    assert memory.fast_path_lookup("o/r", TRACEBACK_LOG) is not None  # 1 miss: still on
    memory.fast_path_miss("o/r", sig)
    assert memory.fast_path_lookup("o/r", TRACEBACK_LOG) is None      # 2 misses: off


def test_agent_step_logged(no_embeddings):
    memory.log_agent_step(None, "retrieval", json.dumps({"files_full": 1}))
    with memory._connect() as conn:
        rows = conn.execute("SELECT step, detail FROM agent_steps").fetchall()
    assert rows == [("retrieval", '{"files_full": 1}')]
