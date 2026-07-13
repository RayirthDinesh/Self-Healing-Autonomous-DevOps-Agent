"""Repository operations — clone, read source files, apply fixes, run tests, push."""

import logging
import os
import subprocess
import tempfile

logger = logging.getLogger("sre-agent-webhook")

# Directories we never want to send to the LLM (test files, agent code, git internals)
_SKIP_PREFIXES = ("tests", "agent", ".git", ".github")


def clone_branch(repo: str, branch: str, dest: str):
    """Clone a single branch of a public GitHub repo into dest."""
    url = f"https://github.com/{repo}.git"
    subprocess.run(
        ["git", "clone", "--branch", branch, "--depth", "1", url, dest],
        check=True,
        capture_output=True,
        text=True,
    )
    logger.info("Cloned %s@%s into %s", repo, branch, dest)


def read_source_files(repo_path: str) -> dict:
    """Walk the cloned repo and return {relative_path: content} for all source files."""
    files = {}
    for dirpath, _, filenames in os.walk(repo_path):
        for fname in filenames:
            full_path = os.path.join(dirpath, fname)
            rel_path = os.path.relpath(full_path, repo_path)

            # Skip anything we don't want the LLM to see
            if any(rel_path.startswith(p) for p in _SKIP_PREFIXES):
                continue
            if not (fname.endswith(".py") or fname == "requirements.txt"):
                continue

            with open(full_path) as f:
                files[rel_path] = f.read()

    return files


def apply_fixes(repo_path: str, fixes: list):
    """Write each fixed file back into the cloned repo."""
    for fix in fixes:
        filepath = os.path.join(repo_path, fix["filename"])
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "w") as f:
            f.write(fix["content"])
        logger.info("Applied fix to %s", fix["filename"])


def run_tests(repo_path: str) -> tuple:
    """Run pytest inside a throwaway Docker container.

    The container gets the cloned repo mounted in, installs dependencies,
    runs the suite, then is automatically deleted (--rm). Nothing the fix
    does can touch the VM or bleed into the next run.

    Returns (passed: bool, output: str).
    """
    result = subprocess.run(
        [
            "docker", "run", "--rm",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "-v", f"{repo_path}:/app",
            "-w", "/app",
            "python:3.11-slim",
            "sh", "-c",
            "pip install -r requirements.txt -q && python -m pytest -v --tb=long",
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    output = result.stdout + result.stderr
    passed = result.returncode == 0

    if passed:
        logger.info("Tests PASSED after fix")
    else:
        logger.warning("Tests still FAILING after fix:\n%s", output[-3000:])

    return passed, output


def commit_and_push(repo_path: str, fix_branch: str, github_token: str, repo: str):
    """Create a new branch in the clone, commit the fix, push it to GitHub."""
    env = os.environ.copy()

    subprocess.run(["git", "config", "user.email", "sre-agent@auto.fix"], cwd=repo_path, check=True)
    subprocess.run(["git", "config", "user.name", "SRE Agent"], cwd=repo_path, check=True)

    subprocess.run(["git", "checkout", "-b", fix_branch], cwd=repo_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", f"fix: auto-fix applied by SRE Agent on {fix_branch}"],
        cwd=repo_path,
        check=True,
    )

    # Embed the token in the remote URL so git can authenticate without a prompt
    auth_url = f"https://x-access-token:{github_token}@github.com/{repo}.git"
    subprocess.run(["git", "push", auth_url, fix_branch], cwd=repo_path, check=True)
    logger.info("Pushed fix branch %s to GitHub", fix_branch)
