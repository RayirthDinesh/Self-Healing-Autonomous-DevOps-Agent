"""run_tests must execute in a per-run venv, never the agent's own env."""

import sys
import textwrap

from repo_ops import run_tests


def test_run_tests_uses_isolated_venv(tmp_path):
    """The repo's pytest must run from a venv inside the workdir, so
    installing the repo's requirements can never mutate the host env."""
    (tmp_path / "requirements.txt").write_text("")  # nothing to install
    (tmp_path / "test_probe.py").write_text(textwrap.dedent("""
        import os
        import sys

        def test_probe():
            # Record which interpreter ran the suite
            with open(os.path.join(os.path.dirname(__file__), "probe.txt"), "w") as f:
                f.write(sys.executable)
    """))

    passed, output = run_tests(str(tmp_path))

    assert passed is True
    exe = (tmp_path / "probe.txt").read_text().strip()
    assert exe != sys.executable, "suite ran in the agent's own environment"
    assert "sre-run-" in exe, f"suite not in a per-run venv: {exe}"
    # venv must NOT live inside the clone — commit_and_push does `git add -A`
    assert str(tmp_path) not in exe
