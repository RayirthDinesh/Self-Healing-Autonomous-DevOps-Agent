"""run_tests must execute inside the Docker sandbox, never the agent's own env."""

import sys
import textwrap

from repo_ops import run_tests


def test_run_tests_runs_inside_container(tmp_path):
    """The repo's suite must run in the throwaway container, so nothing the
    fix does can touch the host or bleed into the next run."""
    (tmp_path / "requirements.txt").write_text("pytest\n")
    (tmp_path / "test_probe.py").write_text(textwrap.dedent("""
        import os
        import sys

        def test_probe():
            # Record which interpreter ran the suite
            with open(os.path.join(os.path.dirname(__file__), "probe.txt"), "w") as f:
                f.write(sys.executable)
    """))

    passed, output = run_tests(str(tmp_path))

    assert passed is True, output[-2000:]
    exe = (tmp_path / "probe.txt").read_text().strip()
    assert exe != sys.executable, "suite ran in the agent's own environment"
    # container interpreter is a POSIX path, never the host's Windows/venv python
    assert exe.startswith("/"), f"suite not inside the container: {exe}"
