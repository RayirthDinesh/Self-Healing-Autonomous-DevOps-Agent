"""A provider that will never answer must stop the run, not consume it.

401/402/404 fail identically on every subsequent call. Treating them like a
garbled model reply means the whole attempt budget is spent in under a second
and the operator sees three "parse failed" warnings instead of "your credits
ran out".
"""

import contextvars

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


def _stub_per_model(monkeypatch, behaviour):
    """Stub the provider with a per-model script.

    behaviour maps a model id to either an exception to raise or a string to
    return, so a test can make the paid model fail and the free one answer.
    Records the models actually called, in order.
    """
    called = []

    class _Reply:
        def __init__(self, content):
            self.content = content

    class _LLM:
        def __init__(self, **kwargs):
            self.model = kwargs.get("model")

        def invoke(self, prompt):
            called.append(self.model)
            outcome = behaviour[self.model]
            if isinstance(outcome, Exception):
                raise outcome
            return _Reply(outcome)

    module = type("m", (), {"ChatOpenAI": _LLM})
    monkeypatch.setitem(__import__("sys").modules, "langchain_openai", module)
    monkeypatch.setattr(graph_nodes.run_tracker, "llm_call", lambda *a, **k: None)
    monkeypatch.setattr(graph_nodes.run_tracker, "step", lambda *a, **k: None)
    return called


@pytest.fixture(autouse=True)
def _reset_degraded(monkeypatch):
    """Each test starts undegraded, inside a run whose id is known."""
    graph_nodes._degraded_by_run.clear()
    monkeypatch.setattr(graph_nodes.run_tracker, "current_run", lambda: "run-1")
    yield
    graph_nodes._degraded_by_run.clear()


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


# --- falling back to a free model ---

PAID, FREE = "vendor/paid-model", "vendor/free-model:free"


@pytest.fixture
def models(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "key")
    monkeypatch.setenv("LLM_MODEL", PAID)
    monkeypatch.setenv("FALLBACK_LLM_MODEL", FREE)


@pytest.mark.parametrize("status", [400, 402, 403, 404])
def test_unusable_paid_model_degrades_to_free(monkeypatch, models, status):
    """Exhausted credit should slow a run down, not end it."""
    called = _stub_per_model(monkeypatch, {
        PAID: _SdkError(status, f"Error code: {status}"),
        FREE: "answer from the free model",
    })

    assert graph_nodes._chat("prompt") == "answer from the free model"
    assert called == [PAID, FREE]


def test_a_bad_key_does_not_bother_with_the_fallback(monkeypatch, models):
    """401 is about the credential, and both models use the same key."""
    called = _stub_per_model(monkeypatch, {
        PAID: _SdkError(401, "Error code: 401"),
        FREE: "should never be reached",
    })

    with pytest.raises(ProviderUnavailable):
        graph_nodes._chat("prompt")
    assert called == [PAID]


def test_degradation_sticks_for_the_rest_of_the_run(monkeypatch, models):
    """Re-learning the same failure on every node would cost a round trip each."""
    called = _stub_per_model(monkeypatch, {
        PAID: _SdkError(402, "Error code: 402"),
        FREE: "answer",
    })

    graph_nodes._chat("first")
    graph_nodes._chat("second")
    graph_nodes._chat("third")

    # The paid model is tried once, not once per call.
    assert called == [PAID, FREE, FREE, FREE]


def test_degradation_survives_node_boundaries(monkeypatch, models):
    """The regression that shipped: stickiness has to cross node calls.

    LangGraph invokes each node in a COPIED context, so a ContextVar set
    inside one node is discarded when that node returns. The first version
    used a ContextVar, passed a test that called _chat directly, and then
    retried the dead model once per node in production - four wasted round
    trips in a real run.
    """
    called = _stub_per_model(monkeypatch, {
        PAID: _SdkError(402, "Error code: 402"),
        FREE: "answer",
    })

    # Each _chat runs in its own copied context, as a node would.
    for _ in range(4):
        contextvars.copy_context().run(graph_nodes._chat, "prompt")

    assert called == [PAID, FREE, FREE, FREE, FREE], (
        "the dead model was retried after a node boundary")


def test_degradation_does_not_leak_across_runs(monkeypatch, models):
    """A topped-up balance must start working again without a restart."""
    called = _stub_per_model(monkeypatch, {
        PAID: _SdkError(402, "Error code: 402"),
        FREE: "answer",
    })
    graph_nodes._chat("prompt")
    assert called == [PAID, FREE]

    # A later run gets its own id, so it starts by trying the preferred model.
    graph_nodes.forget_degraded("run-1")
    monkeypatch.setattr(graph_nodes.run_tracker, "current_run", lambda: "run-2")
    called = _stub_per_model(monkeypatch, {PAID: "paid works again", FREE: "answer"})

    assert graph_nodes._chat("prompt") == "paid works again"
    assert called == [PAID]


def test_degraded_state_does_not_grow_without_bound(monkeypatch, models):
    """A run that crashes before closing must not leak an entry forever."""
    _stub_per_model(monkeypatch, {
        PAID: _SdkError(402, "Error code: 402"),
        FREE: "answer",
    })
    for n in range(graph_nodes._DEGRADED_CAP + 20):
        monkeypatch.setattr(graph_nodes.run_tracker, "current_run", lambda n=n: f"run-{n}")
        graph_nodes._chat("prompt")

    assert len(graph_nodes._degraded_by_run) <= graph_nodes._DEGRADED_CAP


def test_both_models_dead_reports_both(monkeypatch, models):
    called = _stub_per_model(monkeypatch, {
        PAID: _SdkError(402, "Error code: 402"),
        FREE: _SdkError(404, "Error code: 404"),
    })

    with pytest.raises(ProviderUnavailable) as excinfo:
        graph_nodes._chat("prompt")
    message = str(excinfo.value)
    assert PAID in message and FREE in message
    assert "402" in message and "404" in message
    assert called == [PAID, FREE]


@pytest.mark.parametrize("off", ["none", "off", "disabled", "NONE"])
def test_fallback_can_be_disabled(monkeypatch, models, off):
    """A run on a weaker model is sometimes worse than no run at all."""
    monkeypatch.setenv("FALLBACK_LLM_MODEL", off)
    called = _stub_per_model(monkeypatch, {
        PAID: _SdkError(402, "Error code: 402"),
        FREE: "should never be reached",
    })

    with pytest.raises(ProviderUnavailable):
        graph_nodes._chat("prompt")
    assert called == [PAID]


def test_no_fallback_when_it_is_the_same_model(monkeypatch, models):
    """Calling the identical model twice just doubles the latency."""
    monkeypatch.setenv("FALLBACK_LLM_MODEL", PAID)
    called = _stub_per_model(monkeypatch, {PAID: _SdkError(402, "Error code: 402")})

    with pytest.raises(ProviderUnavailable):
        graph_nodes._chat("prompt")
    assert called == [PAID]


def test_transient_error_does_not_switch_models(monkeypatch, models):
    """A 429 is not about the model, and switching would mask that."""
    called = _stub_per_model(monkeypatch, {
        PAID: _SdkError(429, "Error code: 429"),
        FREE: "should never be reached",
    })

    with pytest.raises(Exception) as excinfo:
        graph_nodes._chat("prompt")
    assert not isinstance(excinfo.value, ProviderUnavailable)
    assert called == [PAID]


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
