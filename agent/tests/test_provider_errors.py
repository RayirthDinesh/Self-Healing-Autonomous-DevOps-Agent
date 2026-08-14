"""A provider that will never answer must stop the run, not consume it.

401/402/404 fail identically on every subsequent call. Treating them like a
garbled model reply means the whole attempt budget is spent in under a second
and the operator sees three "parse failed" warnings instead of "your credits
ran out".
"""

import pytest

import graph_nodes
from graph_nodes import ProviderUnavailable, _status_code


class _SdkError(Exception):
    """Shaped like the OpenAI SDK's APIStatusError."""

    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code


# --- classification ---

def test_status_read_from_the_attribute():
    assert _status_code(_SdkError(402, "no credits")) == 402


def test_status_recovered_from_the_string_when_rewrapped():
    """langchain re-wraps SDK errors, and then only the text survives."""
    err = RuntimeError("Error code: 404 - {'error': {'message': 'no endpoints'}}")
    assert _status_code(err) == 404


def test_unrelated_error_has_no_status():
    assert _status_code(ValueError("nope")) is None
    # A number that is not a status code must not be mistaken for one.
    assert _status_code(ValueError("took 429 ms")) is None


# --- _chat behaviour ---

def _stub_provider(monkeypatch, exc):
    class _LLM:
        def __init__(self, **kwargs):
            pass

        def invoke(self, prompt):
            raise exc

    module = type("m", (), {"ChatOpenAI": _LLM})
    monkeypatch.setitem(__import__("sys").modules, "langchain_openai", module)


@pytest.mark.parametrize("status", [401, 402, 403, 404])
def test_non_retryable_statuses_abort(monkeypatch, status):
    _stub_provider(monkeypatch, _SdkError(status, f"Error code: {status}"))
    with pytest.raises(ProviderUnavailable) as excinfo:
        graph_nodes._chat("prompt")
    # The message has to say what to do, not just what broke.
    assert str(status) in str(excinfo.value)
    assert len(str(excinfo.value)) > 40


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_transient_statuses_stay_retryable(monkeypatch, status):
    """A rate limit or a bad gateway may well work on the next attempt."""
    _stub_provider(monkeypatch, _SdkError(status, f"Error code: {status}"))
    with pytest.raises(Exception) as excinfo:
        graph_nodes._chat("prompt")
    assert not isinstance(excinfo.value, ProviderUnavailable)


def test_timeout_stays_retryable(monkeypatch):
    _stub_provider(monkeypatch, TimeoutError("read timed out"))
    with pytest.raises(TimeoutError):
        graph_nodes._chat("prompt")


# --- nodes must not swallow it ---

def _raise_provider_error(*args, **kwargs):
    raise ProviderUnavailable("test/model returned HTTP 402. Credits exhausted.")


@pytest.fixture
def state():
    return {
        "repo": "o/r", "branch": "bug/x", "commit_sha": "abc1234",
        "test_logs": "E   NameError: name 'x' is not defined",
        "workdir": "/tmp/x", "attempt": 1, "llm_calls": 0, "critic_rounds": 0,
        "error_class": "other", "candidate_files": ["src/a.py"],
        "repo_map": {"files": {}, "edges": [], "rank": {}},
        "context": {"src/a.py": "def f():\n    return x\n"},
        "fixes": [{"filename": "src/a.py", "search": "a", "replace": "b"}],
    }


@pytest.mark.parametrize("node_name", ["triage", "fixer", "critic"])
def test_nodes_propagate_rather_than_carry_on(monkeypatch, state, node_name):
    monkeypatch.setattr(graph_nodes, "_chat", _raise_provider_error)
    monkeypatch.setattr(graph_nodes, "_step", lambda *a, **k: None)

    node = getattr(graph_nodes, node_name)
    with pytest.raises(ProviderUnavailable):
        node(state)


def test_localizer_propagates_rather_than_falling_back(monkeypatch, state):
    """The localizer's fallback is for a model that answered badly.

    Silently falling back here would hide a dead API key behind a run that
    looks like it merely picked poor files.
    """
    monkeypatch.setattr(graph_nodes, "_chat", _raise_provider_error)
    monkeypatch.setattr(graph_nodes, "_step", lambda *a, **k: None)

    with pytest.raises(ProviderUnavailable):
        graph_nodes.localizer(state)
