"""LangGraph multi-agent pipeline: node behavior and end-to-end routing."""

import json

import pytest

import graph_nodes
import memory
from agent_graph import build_graph


REPO_MAP = {
    "files": {
        "src/app.py": {"lang": "python", "size": 40, "is_test": False, "imports": [],
                       "symbols": [{"name": "add", "kind": "function",
                                    "sig": "def add(a, b):", "doc": "", "line": 1}]},
        "tests/test_app.py": {"lang": "python", "size": 40, "is_test": True,
                              "imports": ["src/app.py"], "symbols": []},
    },
    "edges": [["tests/test_app.py", "src/app.py"]],
    "rank": {"src/app.py": 1.0},
}

LOG = (
    "FAILED tests/test_app.py::test_add - NameError\n"
    'File "src/app.py", line 2\n'
    "E       NameError: name 'totl' is not defined\n"
)

TRIAGE = '{"summary": "NameError in src/app.py", "suspects": ["src/app.py"]}'
LOCATE = '{"files": ["src/app.py"], "reason": "traceback"}'
FIX = json.dumps({"diagnosis": "typo: totl -> total",
                  "fixes": [{"filename": "src/app.py", "content": "def add(a, b):\n    return a + b\n"}]})
APPROVE = '{"verdict": "approve", "feedback": "looks right"}'
REVISE = '{"verdict": "revise", "feedback": "you deleted a function"}'


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Isolated memory DB, scripted LLM, stubbed git/docker/GitHub effects."""
    calls = {"chat": [], "test_results": [], "pushed": False, "pr": False, "cloned": 0}

    monkeypatch.setenv("MEMORY_DB", str(tmp_path / "memory.db"))
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(memory, "_embed", lambda text: None)

    workdir = tmp_path / "clone"
    (workdir / "src").mkdir(parents=True)
    (workdir / "src" / "app.py").write_text("def add(a, b):\n    return a + totl\n")

    script = []

    def fake_chat(prompt, model=None):
        calls["chat"].append(prompt)
        return script.pop(0)
    monkeypatch.setattr(graph_nodes, "_chat", fake_chat)

    monkeypatch.setattr(graph_nodes, "get_repo_map", lambda repo, sha, path: REPO_MAP)
    monkeypatch.setattr(graph_nodes, "apply_fixes", lambda path, fixes: None)
    monkeypatch.setattr(graph_nodes, "get_diff", lambda path: "fake diff")
    monkeypatch.setattr(graph_nodes, "run_tests",
                        lambda path: (calls["test_results"].pop(0), "suite output"))
    monkeypatch.setattr(graph_nodes, "clone_branch",
                        lambda repo, branch, dest: calls.__setitem__("cloned", calls["cloned"] + 1))
    monkeypatch.setattr(graph_nodes, "commit_and_push",
                        lambda *a, **k: calls.__setitem__("pushed", True))
    monkeypatch.setattr(graph_nodes, "create_pull_request",
                        lambda **k: calls.__setitem__("pr", True) or "https://github.com/o/r/pull/7")

    def invoke(extra=None):
        state = {"repo": "o/r", "branch": "bug/x", "commit_sha": "c0ffee1234567",
                 "test_logs": LOG, "workdir": str(workdir),
                 "attempt": 0, "llm_calls": 0, "critic_rounds": 0}
        state.update(extra or {})
        app = build_graph()
        return app.invoke(state, config={"recursion_limit": 60})

    return {"calls": calls, "script": script, "invoke": invoke, "workdir": workdir}


def test_happy_path_publishes(wired):
    wired["script"][:] = [TRIAGE, LOCATE, FIX, APPROVE]
    wired["calls"]["test_results"][:] = [True]

    final = wired["invoke"]()

    assert final["done"] == "published"
    assert final["pr_url"].endswith("/pull/7")
    assert wired["calls"]["pushed"] and wired["calls"]["pr"]
    assert final["llm_calls"] == 4
    with memory._connect() as conn:
        rows = conn.execute("SELECT suite_green, pr_state FROM incidents").fetchall()
        steps = [s for (s,) in conn.execute("SELECT step FROM agent_steps")]
    assert rows == [(1, "open")]
    for step in ("ingest", "router", "triage", "localizer", "fixer", "critic",
                 "validator", "publisher"):
        assert step in steps


def test_red_attempts_exhaust_to_reflect(wired):
    wired["script"][:] = [TRIAGE] + [LOCATE, FIX, APPROVE] * 3
    wired["calls"]["test_results"][:] = [False, False, False]

    final = wired["invoke"]()

    assert final["done"] == "gave_up"
    assert final["attempt"] == 3
    assert not wired["calls"]["pushed"]
    assert wired["calls"]["cloned"] == 3          # workdir reset after each red
    with memory._connect() as conn:
        greens = [g for (g,) in conn.execute("SELECT suite_green FROM incidents")]
    assert greens == [0, 0, 0]                    # negative examples recorded


def test_critic_revise_loops_back_to_fixer_once(wired):
    wired["script"][:] = [TRIAGE, LOCATE, FIX, REVISE, FIX, APPROVE]
    wired["calls"]["test_results"][:] = [True]

    final = wired["invoke"]()

    assert final["done"] == "published"
    assert final["llm_calls"] == 6
    # revise feedback made it into the second fixer prompt
    fixer_prompts = [p for p in wired["calls"]["chat"] if "fixer agent" in p]
    assert len(fixer_prompts) == 2
    assert "you deleted a function" in fixer_prompts[1]


def test_fast_path_skips_triage_and_localizer(wired):
    sig = memory.failure_signature(LOG)
    with memory._connect() as conn:
        conn.execute(
            "INSERT INTO fast_paths VALUES ('o/r', 'name-error', ?, ?, 2, 0, 0)",
            (sig, json.dumps(["src/app.py"])),
        )
    wired["script"][:] = [FIX, APPROVE]           # no triage, no localizer calls
    wired["calls"]["test_results"][:] = [True]

    final = wired["invoke"]()

    assert final["done"] == "published"
    assert final["fast_path_used"] is True
    assert final["llm_calls"] == 2


def test_fast_path_miss_demotes_and_reroutes_through_triage(wired):
    sig = memory.failure_signature(LOG)
    with memory._connect() as conn:
        conn.execute(
            "INSERT INTO fast_paths VALUES ('o/r', 'name-error', ?, ?, 2, 0, 0)",
            (sig, json.dumps(["src/app.py"])),
        )
    # fast-path fixer fails validation, then the full lane succeeds
    wired["script"][:] = [FIX, APPROVE, TRIAGE, LOCATE, FIX, APPROVE]
    wired["calls"]["test_results"][:] = [False, True]

    final = wired["invoke"]()

    assert final["done"] == "published"
    with memory._connect() as conn:
        miss = conn.execute("SELECT miss_count FROM fast_paths WHERE signature = ?",
                            (sig,)).fetchone()
    assert miss == (1,)


def test_localizer_runs_tools_then_answers(wired, monkeypatch):
    state = {"repo": "o/r", "branch": "b", "commit_sha": "c", "test_logs": LOG,
             "workdir": str(wired["workdir"]), "repo_map": REPO_MAP,
             "triage_summary": "NameError", "llm_calls": 1}
    wired["script"][:] = ['{"tool": "search_repo", "args": {"query": "totl"}}', LOCATE]

    update = graph_nodes.localizer(state)

    assert update["candidate_files"] == ["src/app.py"]
    assert "totl" in update["context"]["src/app.py"]
    assert update["llm_calls"] == 3
    # tool result was fed back into the conversation
    assert "src/app.py:2" in wired["calls"]["chat"][-1]


def test_localizer_falls_back_to_seeds_on_garbage(wired):
    state = {"repo": "o/r", "branch": "b", "commit_sha": "c", "test_logs": LOG,
             "workdir": str(wired["workdir"]), "repo_map": REPO_MAP,
             "blame": {"src/app.py": 1.0}, "llm_calls": 0}
    wired["script"][:] = ["not json at all"] * 6

    update = graph_nodes.localizer(state)

    assert update["candidate_files"] == ["src/app.py"]


def test_fallback_seeds_requirements_on_install_failure(wired):
    repo_map = {"files": dict(REPO_MAP["files"],
                              **{"requirements.txt": {"lang": "text", "size": 20,
                                                      "is_test": False, "imports": [],
                                                      "symbols": []}}),
                "edges": REPO_MAP["edges"], "rank": REPO_MAP["rank"]}
    state = {"repo": "o/r", "branch": "b", "commit_sha": "c",
             "test_logs": "ERROR: No matching distribution found for pandas==0.24.0",
             "workdir": str(wired["workdir"]), "repo_map": repo_map, "llm_calls": 0}
    wired["script"][:] = ["garbage"] * 6

    update = graph_nodes.localizer(state)

    assert "requirements.txt" in update["candidate_files"]


def test_fixer_guardrail_drops_protected_paths(wired):
    state = {"repo": "o/r", "test_logs": LOG, "context": {"src/app.py": "x"},
             "llm_calls": 0}
    wired["script"][:] = [json.dumps({
        "diagnosis": "d",
        "fixes": [{"filename": "tests/test_app.py", "content": "cheat"},
                  {"filename": "agent/pipeline.py", "content": "cheat"},
                  {"filename": "src/app.py", "content": "legit"}],
    })]

    update = graph_nodes.fixer(state)

    assert [f["filename"] for f in update["fixes"]] == ["src/app.py"]


def test_fixer_retry_sees_its_failed_diff(wired):
    """A failed diff is fed back so the model cannot repeat the same fix."""
    wired["script"][:] = [TRIAGE] + [LOCATE, FIX, APPROVE] * 2
    wired["calls"]["test_results"][:] = [False, True]

    wired["invoke"]()

    fixer_prompts = [p for p in wired["calls"]["chat"] if "fixer agent" in p]
    assert len(fixer_prompts) == 2
    assert "already tried and FAILED" not in fixer_prompts[0]
    assert "already tried and FAILED" in fixer_prompts[1]
    assert "fake diff" in fixer_prompts[1]      # the wired get_diff stub's output


def test_routing_budget_rails():
    assert graph_nodes.route_after_validator(
        {"passed": False, "llm_calls": 15, "attempt": 1}) == "reflect"
    assert graph_nodes.route_after_validator(
        {"passed": False, "llm_calls": 3, "attempt": 3}) == "reflect"
    assert graph_nodes.route_after_validator(
        {"passed": False, "llm_calls": 3, "attempt": 1, "demoted_fast_path": True}) == "triage"
    assert graph_nodes.route_after_validator(
        {"passed": False, "llm_calls": 3, "attempt": 1}) == "localizer"
    assert graph_nodes.route_after_validator({"passed": True}) == "publisher"
    assert graph_nodes.route_after_critic(
        {"critic_feedback": "fix it", "critic_rounds": 1, "llm_calls": 3,
         "fixes": [{"filename": "a", "content": "b"}]}) == "fixer"
    assert graph_nodes.route_after_critic(
        {"critic_feedback": "fix it", "critic_rounds": 2, "llm_calls": 3,
         "fixes": [{"filename": "a", "content": "b"}]}) == "validator"


def test_pipeline_dispatches_on_agent_mode(monkeypatch):
    import pipeline
    seen = {}
    monkeypatch.setenv("AGENT_MODE", "graph")
    import agent_graph
    monkeypatch.setattr(agent_graph, "run_graph",
                        lambda *a: seen.setdefault("args", a))
    pipeline.run("o/r", "b", "sha", "logs")
    assert seen["args"] == ("o/r", "b", "sha", "logs")
