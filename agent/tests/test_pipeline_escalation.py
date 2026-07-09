"""Pipeline attempt loop: tiered context first, full-repo escalation on failure."""

import pytest

import pipeline


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Stub every external effect; record what the pipeline did."""
    calls = {"llm_contexts": [], "test_results": [], "pushed": False, "pr": False}

    monkeypatch.setattr(pipeline, "clone_branch", lambda repo, branch, dest: None)
    monkeypatch.setattr(
        pipeline, "get_repo_map",
        lambda repo, sha, path: {"files": {}, "edges": [], "rank": {}, "commit": sha},
    )

    def fake_select(logs, m, path):
        from retrieval import TieredContext
        return TieredContext(full={"src/a.py": "old"}, signatures={}, overview={},
                             metrics={"files_full": 1, "files_total": 1,
                                      "files_signatures": 0, "tokens_sent": 10,
                                      "tokens_full_repo": 40, "retrieval_ms": 1,
                                      "install_failure": False})
    monkeypatch.setattr(pipeline, "select_context", fake_select)
    monkeypatch.setattr(pipeline, "read_source_files", lambda path: {"src/a.py": "old", "src/b.py": "x"})

    def fake_llm(logs, context):
        calls["llm_contexts"].append(context)
        return {"diagnosis": "d", "fixes": [{"filename": "src/a.py", "content": "new"}]}
    monkeypatch.setattr(pipeline, "call_llm", fake_llm)
    monkeypatch.setattr(pipeline, "apply_fixes", lambda path, fixes: None)

    def fake_run_tests(path):
        result = calls["test_results"].pop(0)
        return result, "output"
    monkeypatch.setattr(pipeline, "run_tests", fake_run_tests)

    monkeypatch.setattr(pipeline, "commit_and_push",
                        lambda *a, **k: calls.__setitem__("pushed", True))
    monkeypatch.setattr(pipeline, "create_pull_request",
                        lambda **k: calls.__setitem__("pr", True) or "http://pr")
    monkeypatch.setattr(pipeline, "GITHUB_TOKEN", "tok")
    return calls


def test_first_attempt_green_no_escalation(wired):
    wired["test_results"][:] = [True]
    pipeline.run("o/r", "main", "c0ffee1234567", "log text")
    assert len(wired["llm_contexts"]) == 1          # one LLM call only
    assert wired["pushed"] and wired["pr"]


def test_escalates_to_full_repo_then_succeeds(wired):
    wired["test_results"][:] = [False, True]
    pipeline.run("o/r", "main", "c0ffee1234567", "log text")
    assert len(wired["llm_contexts"]) == 2
    assert isinstance(wired["llm_contexts"][1], dict)   # attempt 2 = legacy full dict
    assert wired["pushed"] and wired["pr"]


def test_gives_up_after_escalation_fails(wired):
    wired["test_results"][:] = [False, False]
    pipeline.run("o/r", "main", "c0ffee1234567", "log text")
    assert len(wired["llm_contexts"]) == 2
    assert not wired["pushed"] and not wired["pr"]
