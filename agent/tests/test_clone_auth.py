"""Authenticated clone: private repos work, and the token never escapes.

The clone URL carries the PAT, so a failure here is the easiest way for a
credential to reach the server log, journalctl, and the dashboard - all of
which render the exception text verbatim.
"""

import subprocess

import pytest

import repo_ops

TOKEN = "ghp_sup3rs3cr3ttokenvalue"


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_public_clone_stays_anonymous(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    calls = []
    monkeypatch.setattr(subprocess, "run",
                        lambda args, **kw: calls.append(args) or _FakeCompleted())

    repo_ops.clone_branch("owner/repo", "bug/x", "/tmp/dest")

    assert calls[0] == ["git", "clone", "--branch", "bug/x", "--depth", "1",
                        "https://github.com/owner/repo.git", "/tmp/dest"]
    # Nothing to strip, so no second git call.
    assert len(calls) == 1


def test_token_authenticates_the_clone(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    calls = []
    monkeypatch.setattr(subprocess, "run",
                        lambda args, **kw: calls.append(args) or _FakeCompleted())

    repo_ops.clone_branch("owner/private", "main", "/tmp/dest")

    assert f"https://x-access-token:{TOKEN}@github.com/owner/private.git" in calls[0]


def test_credential_is_stripped_from_the_clone(monkeypatch):
    """git writes the clone URL into .git/config, and the repo map walks that
    tree while the diff is rendered in the console."""
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    calls = []
    monkeypatch.setattr(subprocess, "run",
                        lambda args, **kw: calls.append(args) or _FakeCompleted())

    repo_ops.clone_branch("owner/private", "main", "/tmp/dest")

    assert calls[-1] == ["git", "remote", "set-url", "origin",
                         "https://github.com/owner/private.git"]


def test_failed_clone_never_reveals_the_token(monkeypatch):
    """The whole point of routing this through _git instead of check=True."""
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: _FakeCompleted(
        returncode=128,
        stderr=(f"fatal: could not read from "
                f"'https://x-access-token:{TOKEN}@github.com/owner/private.git'"),
    ))

    with pytest.raises(RuntimeError) as excinfo:
        repo_ops.clone_branch("owner/private", "main", "/tmp/dest")

    assert TOKEN not in str(excinfo.value)
    assert "***" in str(excinfo.value)
