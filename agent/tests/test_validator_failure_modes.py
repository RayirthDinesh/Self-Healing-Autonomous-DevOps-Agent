"""A validator that cannot run must read as failure, never as a pass.

Every one of these is a setup problem rather than a bug in the candidate fix,
which is exactly why they are dangerous: if any of them returned True the agent
would push and open a PR for code that was never executed.
"""

import subprocess

import pytest

import repo_ops


def test_timeout_is_a_failure(monkeypatch):
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="docker", timeout=300)

    monkeypatch.setattr(subprocess, "run", fake_run)
    passed, output = repo_ops.run_tests("/tmp/clone")

    assert passed is False
    assert "timed out" in output.lower()
    # The message has to name the knob, or the only clue is a red run.
    assert "VALIDATOR_TIMEOUT" in output


def test_missing_docker_aborts_rather_than_reporting_red(monkeypatch):
    """No suite ran, so there is nothing to call red.

    Returning False here would put "Tests still FAILING after fix" in the log
    and blame the patch for a missing daemon.
    """
    def fake_run(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "docker")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(repo_ops.ValidatorUnavailable) as excinfo:
        repo_ops.run_tests("/tmp/clone")
    assert "docker" in str(excinfo.value).lower()


@pytest.mark.parametrize("stderr", [
    # Linux / macOS
    "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
    "Is the docker daemon running?",
    # Windows named pipe - mentions no daemon at all
    "failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine; "
    "check if the path is correct and if the daemon is running: "
    "open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified.",
    "error during connect: Get \"http://%2F%2F.%2Fpipe%2Fdocker_engine/_ping\"",
])
def test_unreachable_daemon_aborts(monkeypatch, stderr):
    """The CLI exists and exits non-zero, but nothing was ever tested.

    This is the case the FileNotFoundError guard misses: `docker` is on PATH,
    so the call succeeds as a process and merely fails to reach a daemon.
    """
    class _Result:
        returncode = 1
        stdout = ""

    _Result.stderr = stderr
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Result())

    with pytest.raises(repo_ops.ValidatorUnavailable):
        repo_ops.run_tests("/tmp/clone")


def test_a_genuinely_red_suite_is_not_mistaken_for_a_dead_daemon(monkeypatch):
    """The guard must not swallow real failures - that would publish blind."""
    class _Result:
        returncode = 1
        stdout = ("FAILED tests/test_aggregator.py::test_total - "
                  "NameError: name 'transactins' is not defined")
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Result())
    passed, output = repo_ops.run_tests("/tmp/clone")

    assert passed is False
    assert "NameError" in output


def test_nonzero_exit_is_a_failure(monkeypatch):
    class _Result:
        returncode = 1
        stdout = "1 failed, 4 passed"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Result())
    passed, _ = repo_ops.run_tests("/tmp/clone")
    assert passed is False


def test_configured_image_is_the_one_that_runs(monkeypatch):
    monkeypatch.setenv("VALIDATOR_IMAGE", "python:3.12-bookworm")
    monkeypatch.setenv("VALIDATOR_TIMEOUT", "42")
    seen = {}

    class _Result:
        returncode = 0
        stdout = "5 passed"
        stderr = ""

    def fake_run(args, **kwargs):
        seen["args"] = args
        seen["timeout"] = kwargs.get("timeout")
        return _Result()

    monkeypatch.setattr(subprocess, "run", fake_run)
    passed, _ = repo_ops.run_tests("/tmp/clone")

    assert passed is True
    assert "python:3.12-bookworm" in seen["args"]
    assert "python:3.11-slim" not in seen["args"]
    assert seen["timeout"] == 42
