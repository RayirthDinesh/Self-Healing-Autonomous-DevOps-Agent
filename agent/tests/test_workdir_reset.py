"""Resetting the clone between attempts, against a real git repo.

Mocks cannot catch the bug this guards. The old implementation deleted the
tree and re-cloned; rmtree(ignore_errors=True) silently failed on .git, the
directory stayed non-empty, `git clone` refused it, and every later attempt
then patched a tree with its source files missing - failing on "file not
found" rather than on the merits of the fix.
"""

import subprocess

import pytest

import repo_ops


def _git(args, cwd):
    subprocess.run(["git"] + args, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def clone(tmp_path):
    """A repo shaped like one the agent has just cloned."""
    repo = tmp_path / "clone"
    repo.mkdir()
    _git(["init", "-q", "-b", "main"], repo)
    _git(["config", "user.email", "t@t"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "src").mkdir()
    (repo / "src" / "aggregator.py").write_text("def total(t):\n    return sum(transactins)\n")
    (repo / "requirements.txt").write_text("pytest\n")
    _git(["add", "-A"], repo)
    _git(["commit", "-qm", "initial"], repo)
    return repo


def test_applied_fix_is_discarded(clone):
    target = clone / "src" / "aggregator.py"
    target.write_text("def total(t):\n    return sum(t)  # patched\n")

    repo_ops.reset_to_head(str(clone))

    assert "transactins" in target.read_text()
    assert "patched" not in target.read_text()


def test_source_files_survive_the_reset(clone):
    """The regression that mattered: the next attempt must still find its files."""
    repo_ops.reset_to_head(str(clone))

    assert (clone / "src" / "aggregator.py").exists()
    assert (clone / "requirements.txt").exists()
    assert (clone / ".git").is_dir()


def test_files_the_fix_added_are_removed(clone):
    (clone / "src" / "leftover.py").write_text("# invented by a failed attempt\n")
    (clone / "src" / "__pycache__").mkdir()
    (clone / "src" / "__pycache__" / "x.pyc").write_bytes(b"\x00")

    repo_ops.reset_to_head(str(clone))

    assert not (clone / "src" / "leftover.py").exists()
    assert not (clone / "src" / "__pycache__").exists()


def test_deleted_files_come_back(clone):
    """A fix that removes a file must not leave the next attempt short of it."""
    (clone / "src" / "aggregator.py").unlink()

    repo_ops.reset_to_head(str(clone))

    assert (clone / "src" / "aggregator.py").exists()


def test_reset_is_repeatable(clone):
    """Three attempts means up to two resets, each on the previous one's tree."""
    target = clone / "src" / "aggregator.py"
    for n in range(3):
        target.write_text(f"# attempt {n}\n")
        repo_ops.reset_to_head(str(clone))
        assert "transactins" in target.read_text()


def test_failure_is_reported_not_swallowed(tmp_path):
    """A directory that is not a git repo has to raise, so the caller stops."""
    not_a_repo = tmp_path / "nope"
    not_a_repo.mkdir()

    with pytest.raises(RuntimeError):
        repo_ops.reset_to_head(str(not_a_repo))
