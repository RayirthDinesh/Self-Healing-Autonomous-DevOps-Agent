"""Pushing a fix: the token never escapes, and a taken branch name never blocks a PR."""

import subprocess

import pytest

import repo_ops

TOKEN = "github_pat_11ABCDEF_secret"


def _fake_run(script):
    """subprocess.run stub driven by a {command-word: (rc, stdout, stderr)} script."""
    def run(args, cwd=None, capture_output=False, text=False, **kwargs):
        rc, out, err = script.get(args[1], (0, "", ""))
        return subprocess.CompletedProcess(args, rc, out, err)
    return run


def test_push_failure_never_carries_the_token(monkeypatch, tmp_path):
    # git puts the whole remote URL in its rejection message.
    rejected = (0, "",
                f"error: failed to push to "
                f"https://x-access-token:{TOKEN}@github.com/o/r.git\n"
                "hint: Updates were rejected because the remote contains work")
    monkeypatch.setattr(subprocess, "run", _fake_run({"push": (1, *rejected[1:])}))

    with pytest.raises(RuntimeError) as excinfo:
        repo_ops.commit_and_push(str(tmp_path), "autofix/bug-x-abc1234", TOKEN, "o/r")

    assert TOKEN not in str(excinfo.value)
    assert "***" in str(excinfo.value)
    assert "rejected" in str(excinfo.value)


def test_branch_name_is_suffixed_when_the_remote_has_it(monkeypatch, tmp_path):
    taken = "autofix/bug-x-abc1234"
    listing = (f"sha1\trefs/heads/{taken}\n"
               f"sha2\trefs/heads/{taken}-2\n")
    monkeypatch.setattr(subprocess, "run", _fake_run({"ls-remote": (0, listing, "")}))

    pushed = repo_ops.commit_and_push(str(tmp_path), taken, TOKEN, "o/r")

    assert pushed == f"{taken}-3"


def test_untaken_branch_name_is_used_as_is(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", _fake_run({"ls-remote": (0, "", "")}))

    pushed = repo_ops.commit_and_push(str(tmp_path), "autofix/bug-x-abc1234", TOKEN, "o/r")

    assert pushed == "autofix/bug-x-abc1234"


def test_unlistable_remote_does_not_stop_the_push(monkeypatch, tmp_path):
    """ls-remote is a nicety; failing it must not cost a validated fix its PR."""
    monkeypatch.setattr(subprocess, "run", _fake_run({"ls-remote": (1, "", "boom")}))

    pushed = repo_ops.commit_and_push(str(tmp_path), "autofix/bug-x-abc1234", TOKEN, "o/r")

    assert pushed == "autofix/bug-x-abc1234"
