"""Preflight checks. The point of doctor is to be right about being wrong.

A false PASS is the worst outcome here: it sends someone into a real run
believing the environment is sound, which is exactly the confusion doctor
exists to prevent. Several of these tests exist because the first version of
a check passed when the real thing would have failed.
"""

import subprocess
import urllib.error
from types import SimpleNamespace

import pytest

import doctor
from doctor import FAIL, OK, SKIP, WARN


class _HTTPError(urllib.error.HTTPError):
    def __init__(self, code):
        super().__init__("http://x", code, "err", {}, None)

    def read(self):
        return b"{}"


# --- Python and dependencies ---

def test_old_python_fails(monkeypatch):
    # SimpleNamespace, not a bare tuple: the check reads .major/.minor, and a
    # tuple would fail with an AttributeError instead of the verdict.
    monkeypatch.setattr(doctor.sys, "version_info",
                        SimpleNamespace(major=3, minor=10, micro=12))
    assert doctor.check_python().status == FAIL


def test_current_python_passes():
    assert doctor.check_python().status == OK


def test_missing_dependency_names_the_package(monkeypatch):
    monkeypatch.setattr(doctor.importlib.util, "find_spec",
                        lambda name: None if name == "langgraph" else object())
    result = doctor.check_dependencies()
    assert result.status == FAIL
    assert "langgraph" in result.detail


# --- Docker ---

def test_missing_docker_binary_fails(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    assert doctor.check_docker_binary().status == FAIL


def test_unreachable_daemon_fails(monkeypatch):
    """The CLI exists and exits non-zero: the most common real case."""
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/docker")

    class _Result:
        returncode = 1
        stdout = ""
        stderr = "Cannot connect to the Docker daemon at unix:///var/run/docker.sock."

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Result())
    result = doctor.check_docker_daemon()
    assert result.status == FAIL
    assert "daemon" in result.detail.lower()


def test_daemon_timeout_is_not_a_pass(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/docker")

    def fake_run(*a, **k):
        raise subprocess.TimeoutExpired(cmd="docker", timeout=45)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert doctor.check_docker_daemon().status == FAIL


# --- Model ---

def _stub_urlopen(monkeypatch, exc):
    def fake_urlopen(req, timeout=None):
        raise exc
    monkeypatch.setattr(doctor.urllib.request, "urlopen", fake_urlopen)


@pytest.mark.parametrize("code", [400, 401, 402, 403, 404])
def test_unusable_model_fails(monkeypatch, code):
    """400 is in here on purpose: OpenRouter answers an unknown model id with
    400 rather than 404, and a typo'd LLM_MODEL is the most common breakage."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "key")
    _stub_urlopen(monkeypatch, _HTTPError(code))
    result = doctor.check_model(offline=False)
    assert result.status == FAIL
    assert result.hint


@pytest.mark.parametrize("code", [429, 500, 503])
def test_transient_model_error_only_warns(monkeypatch, code):
    monkeypatch.setenv("OPENROUTER_API_KEY", "key")
    _stub_urlopen(monkeypatch, _HTTPError(code))
    assert doctor.check_model(offline=False).status == WARN


def test_model_probe_reserves_the_full_budget(monkeypatch):
    """max_tokens must NOT be capped.

    OpenRouter checks affordability against the reservation, not actual usage.
    A probe asking for 1 token succeeds on an account that cannot afford a
    real run, so doctor would report Ready and the first genuine call would
    402 - the precise false pass this check exists to avoid.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "key")
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(req, timeout=None):
        captured["body"] = req.data.decode()
        return _Resp()

    monkeypatch.setattr(doctor.urllib.request, "urlopen", fake_urlopen)
    doctor.check_model(offline=False)
    assert "max_tokens" not in captured["body"]


def test_dead_primary_with_live_fallback_only_warns(monkeypatch):
    """Degraded is not broken - doctor must not refuse a setup that works."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "key")
    monkeypatch.setenv("LLM_MODEL", "vendor/paid")
    monkeypatch.setenv("FALLBACK_LLM_MODEL", "vendor/free:free")
    monkeypatch.setattr(doctor, "_probe_model", lambda model, key: (
        doctor.Result("Model", FAIL, f"{model} -> HTTP 402", "no credit")
        if model == "vendor/paid"
        else doctor.Result("Model", OK, f"{model} answered")))

    result = doctor.check_model(offline=False)
    assert result.status == WARN
    assert "vendor/free:free" in result.detail


def test_both_models_dead_fails(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "key")
    monkeypatch.setenv("LLM_MODEL", "vendor/paid")
    monkeypatch.setenv("FALLBACK_LLM_MODEL", "vendor/free:free")
    monkeypatch.setattr(doctor, "_probe_model", lambda model, key:
                        doctor.Result("Model", FAIL, f"{model} -> HTTP 402", "no credit"))

    result = doctor.check_model(offline=False)
    assert result.status == FAIL
    assert "vendor/paid" in result.detail and "vendor/free:free" in result.detail


def test_disabled_fallback_means_the_primary_decides(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "key")
    monkeypatch.setenv("LLM_MODEL", "vendor/paid")
    monkeypatch.setenv("FALLBACK_LLM_MODEL", "none")
    probed = []

    def fake_probe(model, key):
        probed.append(model)
        return doctor.Result("Model", FAIL, f"{model} -> HTTP 402", "no credit")

    monkeypatch.setattr(doctor, "_probe_model", fake_probe)
    assert doctor.check_model(offline=False).status == FAIL
    assert probed == ["vendor/paid"]


def test_offline_skips_network(monkeypatch):
    # The key still has to be present: "unset" is a real problem whether or
    # not we are allowed to phone out, so that check runs before --offline.
    monkeypatch.setenv("OPENROUTER_API_KEY", "key")

    def explode(*a, **k):
        raise AssertionError("doctor made a network call with --offline")

    monkeypatch.setattr(doctor.urllib.request, "urlopen", explode)
    assert doctor.check_model(offline=True).status == SKIP
    assert doctor.check_openrouter(offline=True).status == SKIP
    assert doctor.check_github_token(offline=True).status in (SKIP, WARN)


# --- Secrets ---

def test_missing_openrouter_key_fails(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert doctor.check_openrouter(offline=True).status == FAIL


def test_whitespace_in_secret_is_caught(monkeypatch):
    """A trailing newline becomes an embedded newline in the HTTP header, and
    the request is rejected before anything parses it."""
    monkeypatch.setenv("WEBHOOK_SECRET", "a-perfectly-good-secret-value\n")
    result = doctor.check_webhook_secret()
    assert result.status == WARN
    assert "whitespace" in result.detail


def test_short_secret_warns(monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET", "hunter2")
    assert doctor.check_webhook_secret().status == WARN


def test_good_secret_passes(monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET", "x" * 43)
    assert doctor.check_webhook_secret().status == OK


def test_absent_github_token_warns_not_fails(monkeypatch):
    """Without a token the agent still diagnoses, fixes and validates. That is
    a reduced mode, not a broken one."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert doctor.check_github_token(offline=True).status == WARN


# --- Exit status ---

def test_exit_code_gates_on_failure(monkeypatch, capsys):
    monkeypatch.setattr(doctor, "run_checks",
                        lambda offline, repo: [doctor.Result("x", FAIL, "broken", "fix it")])
    assert doctor.main(["--offline"]) == 1
    assert "fix it" in capsys.readouterr().out


def test_warnings_alone_do_not_gate(monkeypatch):
    monkeypatch.setattr(doctor, "run_checks",
                        lambda offline, repo: [doctor.Result("x", WARN, "iffy")])
    assert doctor.main(["--offline"]) == 0
