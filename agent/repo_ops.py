"""Repository operations - clone, apply fixes, run tests, push."""

import logging
import os
import subprocess
import tempfile

logger = logging.getLogger("sre-agent-webhook")


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


def apply_fixes(repo_path: str, fixes: list):
    """Apply each fix to the cloned repo.

    Supports two formats from the LLM:
      search/replace - find an exact block and swap only those lines (preferred).
                       Leaves the rest of the file completely untouched.
      content        - full file overwrite (fallback for new files or total rewrites).
    """
    for fix in fixes:
        filepath = os.path.join(repo_path, fix["filename"])
        os.makedirs(os.path.dirname(filepath), exist_ok=True)

        if "search" in fix and "replace" in fix:
            # Targeted edit - only touch the lines that need to change
            try:
                with open(filepath, encoding="utf-8") as f:
                    original = f.read()
                search_text = fix["search"]
                if search_text not in original:
                    logger.error(
                        "apply_fixes: search block not found in %s - skipping",
                        fix["filename"],
                    )
                    continue
                updated = original.replace(search_text, fix["replace"], 1)
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(updated)
                logger.info("Applied search/replace fix to %s", fix["filename"])
            except FileNotFoundError:
                logger.error("apply_fixes: file not found: %s", fix["filename"])
        elif "content" in fix:
            # Full file overwrite (new file or total rewrite)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(fix["content"])
            logger.info("Applied full-file fix to %s", fix["filename"])
        else:
            logger.warning("apply_fixes: fix for %s has neither search/replace nor content - skipping", fix["filename"])


def run_tests(repo_path: str) -> tuple:
    logger.info("Running tests inside Docker container (python:3.11-slim)...")
    """Run pytest inside a throwaway Docker container.

    The container gets the cloned repo mounted in, installs dependencies,
    runs the suite, then is automatically deleted (--rm). Nothing the fix
    does can touch the VM or bleed into the next run.

    Returns (passed: bool, output: str).
    """
    result = subprocess.run(
        [
            "docker", "run", "--rm",
            "-v", f"{repo_path}:/app",
            "-w", "/app",
            "python:3.11-slim",
            "sh", "-c",
            "pip install -r requirements.txt -q --timeout 120 --retries 5 && "
            "python -m pytest -v --tb=long; "
            "PYTEST_EXIT=$?; "
            "chmod -R 777 /app 2>/dev/null || true; "
            "exit $PYTEST_EXIT",
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


def get_diff(repo_path: str) -> str:
    """Diff of the applied fix against the cloned HEAD (for incident memory)."""
    result = subprocess.run(
        # fileMode off: the Docker test run chmods the tree, which is not part of the fix
        ["git", "-c", "core.fileMode=false", "diff", "--no-color"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    return result.stdout if result.returncode == 0 else ""


def commit_and_push(repo_path: str, fix_branch: str, github_token: str, repo: str):
    """Create a new branch in the clone, commit the fix, push it to GitHub."""
    env = os.environ.copy()

    subprocess.run(["git", "config", "user.email", "sre-agent@auto.fix"], cwd=repo_path, check=True)
    subprocess.run(["git", "config", "user.name", "SRE Agent"], cwd=repo_path, check=True)
    subprocess.run(["git", "config", "core.fileMode", "false"], cwd=repo_path, check=True)

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
